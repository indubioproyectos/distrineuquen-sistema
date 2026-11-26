"""19.0.1.30.0: «Deudas de empleados» (only administración and gerencia) and
Catriel picking up at the depot. Synthetic data only."""
from odoo import Command, fields
from odoo.exceptions import AccessError, ValidationError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar


@tagged("post_install", "-at_install", "distri_deudas")
class TestEmployeeDebts(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.product_a.invoice_policy = "order"
        cls.seller = new_test_user(
            cls.env, login="vendedor_deudas", name="Vendedor Deudas",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cls.manager = new_test_user(
            cls.env, login="gerente_deudas", name="Gerente Deudas",
            groups="base.group_user,distri_pedido.grupo_gerencia",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cls.customer = cls.env["res.partner"].create({"name": "Almacén Deudas Prueba",
                                                      "company_type": "company"})
        cls.Debt = cls.env["distri.employee.debt"]

    def _charge(self, kind="unpaid", invoiced=True, price=100.0):
        order = self._order(self.customer, self.product_a, price=price)
        invoice = False
        if invoiced:
            invoice = order._create_invoices()
            invoice.action_post()
        route = self._route(order)
        payload = self._visit(route.stop_ids, no_payment=True) if kind == "unpaid" else \
            self._visit(route.stop_ids, transfer="yes", transfer_amount=price * 10,
                        receipt_answer="not_yet")
        self._sync([self._event(route, "visit", payload)])
        self.env["distri.promised.transfer"]._distri_cron_expire()
        case = route.case_ids
        self.assertEqual(len(case), 1)
        return case, invoice

    def _debts(self):
        return self.Debt.search([("employee_id", "=", self.driver.id)])

    def test_open_charges_are_owed_by_the_employee(self):
        unpaid, invoice = self._charge("unpaid")
        receipt, _invoice = self._charge("done", price=50.0)
        debts = self._debts()
        self.assertEqual(len(debts), 2)
        by_case = {d.case_id: d for d in debts}
        self.assertEqual(by_case[unpaid].origin, "unpaid")
        self.assertEqual(by_case[receipt].origin, "receipt")
        self.assertEqual(by_case[unpaid].amount_owed, 1000)
        self.assertEqual(by_case[unpaid].state, "owed")
        self.assertEqual(by_case[unpaid].partner_id, self.customer)
        self.assertEqual(by_case[unpaid].document, "Factura %s" % invoice.name)
        self.assertTrue(by_case[unpaid].delivered_at)
        self.assertEqual(sum(debts.mapped("amount_owed")), 1500)
        # the gerencia sees the same
        self.assertEqual(len(self.Debt.with_user(self.manager).search(
            [("employee_id", "=", self.driver.id)])), 2)

    def test_partial_payment_lowers_the_debt_with_its_history(self):
        case, invoice = self._charge("unpaid")
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_cash"].id, "amount": 400,
                "payment_difference_handling": "open"})._create_payments()
        debt = self._debts()
        self.assertEqual(debt.amount_owed, 600)
        self.assertEqual(debt.amount_history_ids.mapped("new_amount"), [600])

    def test_manual_decisions_show_as_settled(self):
        replenished, _inv = self._charge("unpaid")
        closed, _inv2 = self._charge("unpaid")
        replenished.replenished = True
        closed.resolution = "Se descontó del sueldo de octubre."
        closed.action_close()
        debts = {d.case_id: d for d in self._debts()}
        self.assertEqual(debts[replenished].state, "settled")
        self.assertEqual(debts[replenished].amount_owed, 0)
        self.assertEqual(debts[replenished].settled_reason, "Repuesto por el repartidor")
        self.assertEqual(debts[closed].state, "settled")
        self.assertEqual(debts[closed].settled_reason, "Se descontó del sueldo de octubre.")
        self.assertEqual(debts[closed].charge_amount, 1000)

    def test_no_accounting_entries(self):
        moves = self.env["account.move"].search_count([])
        self._charge("unpaid", invoiced=False)
        self._debts().mapped("amount_owed")
        self.assertEqual(self.env["account.move"].search_count([]), moves)

    def test_drivers_and_sellers_do_not_see_it(self):
        self._charge("unpaid")
        menu = self.env.ref("distri_pedido.menu_reparto_employee_debts")
        for user in (self.driver, self.seller):
            with self.assertRaises(AccessError):
                self.Debt.with_user(user).search([])
            visible = self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()
            self.assertNotIn(menu.id, visible, user.name)
        self.assertIn(menu.id, self.env["ir.ui.menu"].with_user(self.manager)._visible_menu_ids())
        # the record rules hold even with an access added by mistake
        rules = self.env["ir.rule"].search([("model_id.model", "=", "distri.employee.debt")])
        denied = rules.filtered(lambda r: r.domain_force.replace(" ", "") == "[(0,'=',1)]")
        self.assertEqual(denied.groups, self.env.ref("distri_pedido.grupo_vendedor")
                         | self.env.ref("distri_pedido.grupo_reparto"))

    def test_pdf_summary_per_employee(self):
        self._charge("unpaid")
        closed, _inv = self._charge("unpaid")
        closed.resolution = "Repuesto en efectivo."
        closed.action_close()
        debts = self._debts()
        html = self.env["ir.actions.report"]._render_qweb_html(
            "distri_pedido.report_deudas_empleado", debts.ids)[0].decode()
        self.assertIn(self.driver.name, html)
        self.assertIn("Adeudado: $ 1.000", html)
        self.assertIn("Repuesto en efectivo.", html)
        self.assertIn("sin asientos contables", html)


@tagged("post_install", "-at_install", "distri_catriel")
class TestCatrielPicksUp(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.catriel = cls.env.ref("distri_pedido.zona_catriel")
        cls.zapala = cls.env.ref("distri_pedido.zona_zapala")
        cls.customer = cls.env["res.partner"].create({"name": "W - Kiosco Catriel Prueba",
                                                      "company_type": "company"})

    def test_catriel_is_a_pickup_zone(self):
        self.assertTrue(self.catriel.retira_deposito)
        self.assertFalse(self.catriel.encomienda)
        self.assertTrue(self.zapala.encomienda, "Zapala still ships by parcel")
        with self.assertRaises(ValidationError):
            self.catriel.encomienda = True

    def test_catriel_order_is_picked_up_at_the_depot(self):
        order = self._order(self.customer, self.product_a)
        self.assertEqual(order._distri_route_zone(), self.catriel)
        self.assertEqual(order.distri_entrega, "retira")
        self.assertFalse(order.distri_encomienda)
        # never on a route, even one of the Catriel zone
        route = self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "date": today_ar(),
            "zone_ids": [Command.set(self.catriel.ids)]})
        route.action_assign_orders()
        self.assertNotIn(order, route.stop_ids.order_id)
        # the depot delivery takes it
        action = order.action_distri_depot_delivery()
        self.assertEqual(action["res_model"], "distri.depot.delivery")

    def test_typed_catriel_in_soy_del_interior_does_not_ship(self):
        other = self.env["res.partner"].create({"name": "Kiosco Neuquén Prueba",
                                                "company_type": "company"})
        order = self._order(other, self.product_a)
        self.assertEqual(order.distri_entrega or "domicilio", "domicilio")
        order.write({"distri_encomienda_pedida": True, "distri_encomienda_localidad": "Catriel"})
        self.assertFalse(order.distri_encomienda)
        self.assertEqual(order.distri_entrega, "retira")

    def test_zapala_still_ships(self):
        customer = self.env["res.partner"].create({"name": "Z - Almacén Zapala Prueba",
                                                   "company_type": "company"})
        order = self._order(customer, self.product_a)
        self.assertTrue(order.distri_encomienda)
        self.assertNotEqual(order.distri_entrega, "retira")
