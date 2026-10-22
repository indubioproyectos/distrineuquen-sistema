from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.deposito import COUNTER_REASON, WITHOUT_CODE_REASON
from odoo.addons.distri_pedido.models.factura_numeracion import PARAMETRO
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.models.reparto import DEPOT_JOURNAL_CODE
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_deposito")
class TestDeposito(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.partner = cls.partner_a
        # «Venta en mostrador» needs the customer's WhatsApp on file (decision 19)
        cls.partner.distri_whatsapp = "2995001234"
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Deposito test", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def _quotation(self, qty_a=10, qty_b=5):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": qty_a,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": qty_b,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ],
        })

    def _packed(self):
        """Confirmed and packed by the packer's steps: posted invoice of 1200."""
        order = self._quotation()
        order.action_confirm()
        self.assertEqual(order.distri_tomar(), "")
        self.assertEqual(order.distri_terminar(2), "")
        return order

    def _old_invoice(self, amount=500):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": "2026-01-10",
            "invoice_line_ids": [Command.create({
                "name": "deuda vieja", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def _wizard(self, order, **vals):
        action = order.action_distri_depot_delivery()
        values = {"returns_answer": "none", "code_option": "without_code"}
        values.update(vals)
        returns = values.pop("returns", [])
        wizard = self.env[action["res_model"]].with_context(action["context"]).create(values)
        for line, qty, reason in returns:
            self.env["distri.depot.delivery.return"].create({
                "wizard_id": wizard.id, "order_line_id": line.id, "quantity": qty,
                "reason": reason})
        return wizard

    def _stop(self, order):
        return order.distri_stop_ids.sorted("id")[-1:]

    def _depot_journal(self):
        return self.env["account.journal"].search([
            ("code", "=", DEPOT_JOURNAL_CODE), ("company_id", "=", self.env.company.id)])

    # ------------------------------------------------------------ delivery

    def test_depot_delivery_cash_oldest_first(self):
        old = self._old_invoice(500)
        order = self._packed()
        invoice = order.distri_factura_auto_id
        self.assertEqual(invoice.amount_residual, 1200)
        code = order._distri_generate_receipt_code()

        wizard = self._wizard(order, code_option="code", code=code, cash=1300)
        self.assertEqual(wizard.order_amount, 1200)
        self.assertEqual(wizard.previous_debt, 500)
        self.assertEqual(wizard.parcels, 2)
        wizard.action_confirm()

        stop = self._stop(order)
        route = stop.route_id
        self.assertEqual(route.kind, "depot")
        self.assertEqual(route.driver_id, self.env.user)
        self.assertEqual(stop.state, "paid")
        self.assertEqual(stop.delivery_state, "delivered_code")
        self.assertFalse(stop.no_code_reason)
        self.assertEqual(order.distri_delivery_state, "delivered")

        journal = self._depot_journal()
        self.assertEqual(journal.type, "cash")
        self.assertEqual(journal.name, "Caja depósito")
        payment = route.payment_ids
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.journal_id, journal)
        self.assertEqual(payment.amount, 1300)
        self.assertEqual(payment.partner_id, self.partner.commercial_partner_id)
        self.assertEqual(old.amount_residual, 0, "oldest invoice is paid first")
        self.assertEqual(invoice.amount_residual, 400)

        # delivered once: the button is refused a second time
        with self.assertRaises(UserError):
            order.action_distri_depot_delivery()

    def test_depot_delivery_wrong_code_is_refused(self):
        order = self._packed()
        code = order._distri_generate_receipt_code()
        wrong = "%04d" % ((int(code) + 1) % 10000)
        with self.assertRaises(UserError):
            self._wizard(order, code_option="code", code=wrong, cash=1200).action_confirm()
        self.assertFalse(order.distri_stop_ids)

    def test_depot_delivery_with_return_credit_note(self):
        order = self._packed()
        invoice = order.distri_factura_auto_id
        line_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        wizard = self._wizard(order, returns_answer="yes", returns=[(line_a, 2, "Vencido")],
                              returns_received=True, cash=1000)
        self.assertEqual(wizard.returns_amount, 200)
        self.assertEqual(wizard.expected_balance, 1000, "what to charge drops with the return")
        wizard.action_confirm()

        stop = self._stop(order)
        ret = stop.return_ids
        self.assertEqual(len(ret), 1)
        self.assertEqual(ret.settlement_state, "received")
        self.assertEqual(stop.expected_balance, 1000)
        refund = ret.credit_note_id
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.reversed_entry_id, invoice)
        self.assertEqual(refund.amount_total, 200)
        self.assertEqual(refund.state, "posted")
        # 1200 - 200 of the credit note - 1000 cash
        self.assertEqual(invoice.amount_residual, 0)

    def test_depot_return_not_confirmed_waits_for_the_close(self):
        order = self._packed()
        line_b = order.order_line.filtered(lambda l: l.product_id == self.product_b)
        self._wizard(order, returns_answer="yes", returns=[(line_b, 1, "Roto")],
                     cash=1160).action_confirm()
        route = self._stop(order).route_id
        ret = route.return_ids
        self.assertEqual(ret.settlement_state, "pending")
        self.assertFalse(ret.credit_note_id)
        route.cash_counted = 1160
        with self.assertRaises(UserError):
            route.action_close_depot()
        ret.action_received()
        route.action_close_depot()
        self.assertEqual(route.state, "settled")
        self.assertEqual(ret.credit_note_id.amount_total, 40)

    def test_depot_delivery_transfer_creates_pending_receipt_not_payment(self):
        order = self._packed()
        with self.assertRaises(UserError):
            # a transfer needs the answer about the receipt
            self._wizard(order, transfer="yes", transfer_amount=1200).action_confirm()
        self._wizard(order, transfer="yes", transfer_amount=1200, receipt_answer="not_yet",
                     comment="Desde la cuenta del hijo").action_confirm()
        stop = self._stop(order)
        self.assertEqual(stop.state, "paid")
        promise = self.env["distri.promised.transfer"].search([("stop_id", "=", stop.id)])
        self.assertEqual(promise.state, "open")
        self.assertEqual(promise.receipt_answer, "not_yet")
        self.assertEqual(promise.amount, 1200)
        self.assertEqual(promise.route_id.kind, "depot")
        self.assertFalse(stop.route_id.payment_ids)
        self.assertFalse(self.env["account.payment"].search([
            ("partner_id", "=", self.partner.id), ("journal_id", "=", self._depot_journal().id)]))
        self.assertEqual(order.distri_factura_auto_id.amount_residual, 1200)

    def test_depot_delivery_without_code_is_flagged(self):
        order = self._packed()
        with self.assertRaises(UserError):
            # no cash, no transfer: "No pagó nada" has to be explicit
            self._wizard(order).action_confirm()
        with self.assertRaises(UserError):
            self._wizard(order, counter_present=True, no_payment=True).action_confirm()
        self._wizard(order, no_payment=True).action_confirm()
        stop = self._stop(order)
        self.assertEqual(stop.delivery_state, "delivered_no_code")
        self.assertEqual(stop.no_code_reason, WITHOUT_CODE_REASON)
        self.assertEqual(stop.state, "not_paid")
        self.assertIn("Sin código de recibido", order.message_ids[0].body)

    def test_depot_delivery_refused_while_on_a_route(self):
        order = self._packed()
        self._route(order)
        with self.assertRaises(UserError):
            order.action_distri_depot_delivery()

    # ------------------------------------------------------------ counter sale

    def test_counter_sale_end_to_end(self):
        journal = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.env.company.id)], limit=1)
        year = fields.Date.today().year
        last = max(self.env["account.move"].search(
            [("journal_id", "=", journal.id), ("state", "=", "posted")]
        ).mapped("sequence_number") + [0])
        floor = last + 6275
        self.env["ir.config_parameter"].sudo().set_param(
            PARAMETRO, "%s/%s/%05d" % (journal.code, year, floor))

        order = self._quotation(qty_a=3, qty_b=2)
        self.assertEqual(order.state, "draft")
        action = order.action_distri_counter_sale()

        self.assertEqual(order.state, "sale")
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.distri_bultos, 1)
        for line in order.order_line:
            self.assertEqual(line.qty_delivered, line.product_uom_qty, "packed as ordered")
        remito = order._distri_remitos()
        self.assertEqual(len(remito), 1)
        self.assertRegex(remito.name, r"^R-\d{5}$")
        invoice = order.distri_factura_auto_id
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.name, "%s/%s/%05d" % (journal.code, year, floor + 1),
                         "the invoice continues the series")
        self.assertEqual(invoice.amount_total, 380)
        self.assertTrue(order.sudo().distri_receipt_hash)

        self.assertEqual(action["res_model"], "distri.depot.delivery")
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({
            "returns_answer": "none", "cash": 380, "parcels": 2})
        self.assertTrue(wizard.counter_sale)
        self.assertTrue(wizard.counter_present)
        wizard.action_confirm()

        stop = self._stop(order)
        self.assertEqual(stop.route_id.kind, "depot")
        self.assertEqual(stop.no_code_reason, COUNTER_REASON)
        self.assertEqual(stop.state, "paid")
        self.assertEqual(order.distri_bultos, 2)
        self.assertEqual(order.distri_delivery_state, "delivered")
        self.assertEqual(stop.route_id.payment_ids.journal_id, self._depot_journal())
        self.assertEqual(invoice.amount_residual, 0)

    def test_counter_sale_needs_a_real_quotation(self):
        order = self._packed()
        with self.assertRaises(UserError):
            order.action_distri_counter_sale()

    # ------------------------------------------------------------ depot cash close

    def test_depot_cash_close_difference_opens_case(self):
        first, second = self._packed(), self._packed()
        self._wizard(first, cash=1200).action_confirm()
        self._wizard(second, cash=500).action_confirm()
        route = self._stop(first).route_id
        self.assertEqual(self._stop(second).route_id, route, "one depot box per user and day")
        self.assertEqual(route.cash_expected, 1700)
        self.assertEqual(route.cash_expected, sum(route.payment_ids.mapped("amount")))

        # another user's box is separate
        cashier = new_test_user(
            self.env, login="cajero_deposito", name="Cajero Depósito",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)])
        third = self._packed()
        action = third.with_user(cashier).action_distri_depot_delivery()
        self.env["distri.depot.delivery"].with_user(cashier).with_context(action["context"]).create({
            "returns_answer": "none", "code_option": "without_code", "cash": 100,
        }).action_confirm()
        other_box = self._stop(third).route_id
        self.assertNotEqual(other_box, route)
        self.assertEqual(other_box.driver_id, cashier)

        with self.assertRaises(UserError):
            route.action_close_depot()  # counted cash missing
        route.write({"cash_counted": 1650, "driver_explanation": "Faltan 50"})
        self.assertEqual(route.cash_difference, -50)
        route.action_close_depot()
        self.assertEqual(route.state, "settled")
        self.assertEqual(route.settled_by_id, self.env.user)
        case = route.case_ids.filtered(lambda c: c.kind == "cash")
        self.assertEqual(len(case), 1)
        self.assertEqual(case.amount, -50)
        self.assertEqual(case.driver_explanation, "Faltan 50")
        self.assertIn("Depósito", case.name)
        with self.assertRaises(UserError):
            route.cash_counted = 1700

        # a delivery after the close opens a new box
        fourth = self._packed()
        self._wizard(fourth, cash=10).action_confirm()
        self.assertNotEqual(self._stop(fourth).route_id, route)

    def test_depot_routes_stay_out_of_the_phone_and_settlement(self):
        order = self._packed()
        self._wizard(order, cash=1200).action_confirm()
        route = self._stop(order).route_id
        with self.assertRaises(UserError):
            route.action_settle()
        res = self._sync([self._event(route, "close", {})], user=self.env.user)
        self.assertEqual(res[0]["status"], "error")

    # ------------------------------------------------------------ departures

    def test_departure_control_and_missing_parcels(self):
        first, second = self._packed(), self._packed()
        route = self._route(first | second)
        s1, s2 = route.stop_ids
        self._sync([self._event(route, "load", {"stop_id": s1.id, "loaded": True})])
        self.assertEqual(route.parcel_count, 4)
        self.assertEqual(route.parcel_loaded_count, 2)
        self.assertTrue(route.load_missing)
        self.assertEqual(route.load_color, 2)
        self.assertIn("sin cargar", route.orders_summary)
        self.assertIn("falta el depósito", route.orders_summary)

        with self.assertRaises(UserError):
            route.with_user(self.driver).action_mark_controlled()
        # «Controlado» needs the double check of every order (despacho.py)
        with self.assertRaises(UserError):
            route.action_mark_controlled()
        self.assertFalse(route.departure_controlled_at)

        self._sync([self._event(route, "load", {"stop_id": s2.id, "loaded": True})])
        self.assertEqual(route.parcel_loaded_count, 4)
        self.assertFalse(route.load_missing)
        (s1 | s2).action_dispatch_check()
        self.assertFalse(route.dispatch_missing)
        self.assertEqual(route.load_color, 0)
        route.departure_note = "Salió con una caja abierta de %s" % second.name
        route.action_mark_controlled()
        self.assertEqual(route.departure_controlled_by_id, self.env.user)
        self.assertTrue(route.departure_controlled_at)
        self.assertIn("2 pedidos y 4 bultos con doble control", route.message_ids[0].body)

    def test_departure_control_of_a_route_that_already_left(self):
        """Routes that left before the double check are not blocked."""
        route = self._route(self._packed())
        route.state = "on_route"
        route.action_mark_controlled()
        self.assertTrue(route.departure_controlled_at)
        self.assertIn("0 de 2 bultos", route.message_ids[0].body)

        action = self.env.ref("distri_pedido.action_departure")
        self.assertIn(route, self.env["distri.route"].search(eval(action.domain)))


@tagged("post_install", "-at_install", "distri_deposito")
class TestManagersAsDrivers(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_driver = new_test_user(
            cls.env, login="admin_chofer", name="Administración Chofer",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cls.manager_driver = new_test_user(
            cls.env, login="gerente_chofer", name="Gerencia Chofer",
            groups="base.group_user,distri_pedido.grupo_gerencia",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        partner = cls.env["res.partner"].create({"name": "Y - KIOSCO GERENTE", "city": "Plottier"})
        product = cls.env["product.product"].create({
            "name": "Producto gerente", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.order = cls._order(partner, product, qty=2, price=100)
        cls.order2 = cls._order(partner, product, qty=1, price=100)

    def test_depot_box_of_a_day_before_must_be_closed_first(self):
        """19.0.1.37.0 (T6): never two depot boxes of the same user open."""
        Route = self.env["distri.route"]
        user = self.admin_driver
        old = Route._depot_route_for(user)
        old.sudo().write({"date": old.date - timedelta(days=1)})
        with self.assertRaisesRegex(UserError, "sigue abierta"):
            Route._depot_route_for(user)
        old.sudo().write({"state": "settled"})
        today = Route._depot_route_for(user)
        self.assertNotEqual(today, old)
        self.assertEqual(today.state, "on_route")
        # other users are not blocked by it
        old.sudo().write({"state": "on_route"})
        self.assertTrue(Route._depot_route_for(self.manager_driver))

    def test_driver_domain_includes_admin_and_manager(self):
        Route = self.env["distri.route"]
        drivers = self.env["res.users"].search(Route._driver_domain())
        self.assertIn(self.admin_driver, drivers)
        self.assertIn(self.manager_driver, drivers)

    def test_admin_and_manager_load_their_single_order_route(self):
        for user, order in ((self.admin_driver, self.order), (self.manager_driver, self.order2)):
            route = self._route(order, driver=user)
            self.assertEqual(route.stop_count, 1)
            # a depot box of the same user never reaches the phone
            depot = self.env["distri.route"].with_user(user)._depot_route_for(user)
            self.authenticate(user.login, user.login)
            self.assertEqual(self.url_open("/reparto").status_code, 200)
            data = self.url_open("/reparto/api/route").json()
            self.assertTrue(data["ok"], data)
            self.assertEqual(data["route"]["id"], route.id)
            self.assertEqual([r["id"] for r in data["routes"]], [route.id])
            self.assertEqual(len(data["route"]["stops"]), 1)
            self.assertEqual(self.url_open("/reparto/api/route?id=%s" % depot.id).status_code, 404)
