"""«Cobro en efectivo» (19.0.1.92.0, models/cobro_efectivo.py,
controllers/cobro.py): any internal user registers a customer's cash, posted
at once on his depot cash box of the day, which «Cierre de caja depósito»
closes. Synthetic data only."""
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.cobro_efectivo import DuplicateCashError, RouteCashWarning
from odoo.addons.distri_pedido.models.reparto import DEPOT_JOURNAL_CODE
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_cobro_efectivo")
class TestCobroEfectivo(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.cobrador = new_test_user(
            cls.env, login="cobrador_efectivo", name="Cobrador Efectivo",
            groups="base.group_user", company_id=company.id,
            company_ids=[Command.set(company.ids)])
        cls.portal = new_test_user(cls.env, login="portal_efectivo", groups="base.group_portal")
        cls.partner = cls.partner_a
        cls.env["distri.route"]._depot_journal(company)

    def _invoice(self, amount=500, partner=None):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": (partner or self.partner).id,
            "invoice_date": "2026-01-10",
            "invoice_line_ids": [Command.create({
                "name": "deuda", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def _cash(self, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cobrador)

    def test_cash_against_an_invoice(self):
        invoice = self._invoice(500)
        cash = self._cash().cash_register(self.partner.id, "200", invoice_id=invoice.id,
                                          note="pagó en el local")
        self.assertEqual(invoice.amount_residual, 300)
        route = cash.route_id
        self.assertEqual(route.kind, "depot")
        self.assertEqual(route.driver_id, self.cobrador)
        self.assertEqual(route.journal_id.code, DEPOT_JOURNAL_CODE)
        payment = cash.payment_id
        self.assertIn(payment.state, ("in_process", "paid"))
        self.assertEqual(payment.journal_id, route.journal_id)
        self.assertEqual(payment.amount, 200)
        self.assertIn(payment, route.payment_ids)
        memo = "Efectivo · cobró Cobrador Efectivo · caja %s · %s · pagó en el local" % (
            today_ar().strftime("%d/%m"), invoice.name)
        self.assertEqual(cash.memo, memo)
        self.assertEqual(payment.memo, memo)
        # it is in the user's box: «Cierre de caja depósito» counts and closes it
        self.assertEqual(route.cash_expected, 200)
        self.assertEqual(route.cash_collection_ids, cash)
        note = self.partner.message_ids[:1].body
        self.assertIn("Cobro en efectivo", note)
        self.assertIn("Cobrador Efectivo", note)
        route.cash_counted = 200
        route._close_depot_box()
        self.assertEqual(route.state, "settled")

    def test_cash_on_account(self):
        invoice = self._invoice(500)
        cash = self._cash().cash_register(self.partner.id, 150)
        self.assertFalse(cash.invoice_id)
        self.assertTrue(cash.memo.endswith("· a cuenta"), cash.memo)
        # 19.0.1.93.0 (E2E-C C13): applied to the oldest open invoice, like
        # the route's cash
        self.assertEqual(invoice.amount_residual, 350)
        second = self._cash().cash_register(self.partner.id, 75, note="  resto  ")
        self.assertEqual(second.route_id, cash.route_id, "one box per user and day")
        self.assertEqual(second.note, "resto")
        self.assertEqual(cash.route_id.cash_expected, 225)
        self.assertEqual(self._cash().cash_today(), second | cash)

    def test_double_submit_is_asked(self):
        invoice = self._invoice(500)
        self._cash().cash_register(self.partner.id, 100, invoice_id=invoice.id)
        with self.assertRaises(DuplicateCashError):
            self._cash().cash_register(self.partner.id, "100,00", invoice_id=invoice.id)
        self.assertEqual(invoice.amount_residual, 400)
        # another amount, or on account, is another payment
        self._cash().cash_register(self.partner.id, 100)
        self._cash().cash_register(self.partner.id, 101, invoice_id=invoice.id)
        # confirmed: it goes through
        self._cash().cash_register(self.partner.id, 100, invoice_id=invoice.id, confirmed=True)
        # 19.0.1.93.0 (C13): the 100 «a cuenta» went to this invoice too
        self.assertEqual(invoice.amount_residual, 99)

    def test_validations(self):
        invoice = self._invoice(500)
        other = self._invoice(300, partner=self.partner_b)
        self.assertEqual(self._cash()._cash_amount("1.500"), 1500)
        self.assertEqual(self._cash()._cash_amount("1.500,50"), 1500.5)
        self.assertEqual(self._cash()._cash_amount("$ 1500.5"), 1500.5)
        self.assertEqual(self._cash()._cash_amount("1500,50"), 1500.5)
        self.assertEqual(self._cash()._cash_amount("1.500.000"), 1500000)
        # 19.0.1.93.0 (E2E-C C12): a dot that may be a thousands one is ambiguous
        for amount in ("1.5", "12.50", "1.50", "1.5,00", "150.5"):
            with self.assertRaisesRegex(UserError, "No se entiende el importe", msg=amount):
                self._cash()._cash_amount(amount)
        for amount in (0, -5, "abc", "", 1e12):
            with self.assertRaises(UserError, msg=amount):
                self._cash().cash_register(self.partner.id, amount)
        with self.assertRaises(UserError):
            self._cash().cash_register(self.partner.id, 100, invoice_id=other.id)
        with self.assertRaises(UserError):
            self._cash().cash_register(self.partner.id, 600, invoice_id=invoice.id)
        generic = self.env["res.partner"]._distri_generic_partner_for()
        with self.assertRaises(UserError):
            self._cash().cash_register(generic.id, 100)
        contact = self.env["res.partner"].create({"name": "Contacto", "parent_id": self.partner.id})
        with self.assertRaises(UserError):
            self._cash().cash_register(contact.id, 100)
        with self.assertRaises(AccessError):
            self._cash(self.portal).cash_register(self.partner.id, 100)
        with self.assertRaises(AccessError):
            self._cash(self.portal).cash_search(self.partner.name)
        self.assertFalse(self.env["distri.cash.collection"].sudo().search(
            [("user_id", "=", self.cobrador.id)]), "nothing was registered")

    def test_only_real_customers(self):
        """19.0.1.93.0 (E2E-C C2): not the company, not an employee (internal
        user, archived too), not the public user; search and register alike."""
        employee = new_test_user(self.env, login="empleado_efectivo", name="Empleado Efectivo",
                                 groups="base.group_user")
        former = new_test_user(self.env, login="exempleado_efectivo",
                               name="Exempleado Efectivo", groups="base.group_user")
        former.active = False
        public = self.env.ref("base.public_user").sudo().partner_id
        for partner in (self.env.company.partner_id, employee.partner_id, former.partner_id,
                        self.cobrador.partner_id, public):
            self.assertNotIn(partner.id, [p["id"] for p in self._cash().cash_search(
                partner.name[:8])], partner.name)
            with self.assertRaisesRegex(UserError, "no se puede cobrar", msg=partner.name):
                self._cash().cash_register(partner.id, 100)
        self.assertTrue(self._cash().cash_register(self.partner.id, 100, over_ok=True))

    def test_search_and_account(self):
        self.partner.write({"ref": "REF-EFECTIVO-1", "phone": "+54 299 456-7788"})
        invoice = self._invoice(500)
        for query in ("REF-EFECTIVO", "4567788", self.partner.name[:5]):
            found = self._cash().cash_search(query)
            self.assertIn(self.partner.id, [p["id"] for p in found], query)
        self.assertEqual(self._cash().cash_search("x"), [])
        account = self._cash().cash_account(self.partner.id)
        self.assertEqual(account["balance"], 500)
        self.assertEqual([i["id"] for i in account["invoices"]], [invoice.id])
        self.assertEqual(account["invoices"][0]["residual"], 500)
        self.assertTrue(account["rows"])

    def test_seller_does_not_reach_a_distributor(self):
        seller = new_test_user(
            self.env, login="vendedor_efectivo", groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)])
        self.partner_b.distri_lista_oculta = True
        self.assertNotIn(self.partner_b.id, [p["id"] for p in self._cash(seller).cash_search(
            self.partner_b.name)])
        with self.assertRaises(UserError):
            self._cash(seller).cash_register(self.partner_b.id, 100)
        # a driver collects from anyone
        self.assertTrue(self._cash().cash_register(self.partner_b.id, 100, over_ok=True))


@tagged("post_install", "-at_install", "distri_cobro_efectivo")
class TestCobroEfectivoRutas(RepartoMixin, AccountTestInvoicingCommon):
    """19.0.1.93.0 (E2E-C C1): /cobro and the driver never collect the same
    order twice without someone noticing."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product_a.invoice_policy = "order"
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls.cobrador = new_test_user(
            cls.env, login="cobrador_rutas", name="Cobrador Rutas",
            groups="base.group_user", company_id=company.id,
            company_ids=[Command.set(company.ids)])
        cls.admin_cobro = new_test_user(
            cls.env, login="admin_cobro_rutas", name="Admin Cobro Rutas",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.env["distri.route"]._depot_journal(company)
        cls.partner = cls.partner_a

    def _invoiced_order(self):
        order = self._order(self.partner, self.product_a, qty=10, price=100.0)
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _cash(self, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cobrador)

    def test_order_on_a_route_asks_first_and_the_stop_learns_it(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self.assertEqual(stop.order_amount, 1000)
        page = self._cash().cash_account(self.partner.id)
        self.assertIn("Este pedido está en la ruta de Repartidor Test",
                      page["invoices"][0]["route_note"])
        with self.assertRaisesRegex(RouteCashWarning, "si ya le pagó al repartidor, no lo "
                                                      "cobres de nuevo") as ctx:
            self._cash().cash_register(self.partner.id, 400, invoice_id=invoice.id)
        self.assertEqual(ctx.exception.field, "ruta_ok")
        self.assertEqual(invoice.amount_residual, 1000, "nothing registered before confirming")
        # «a cuenta» would also reach that invoice (oldest first): asked too
        with self.assertRaises(RouteCashWarning):
            self._cash().cash_register(self.partner.id, 400)
        self._cash().cash_register(self.partner.id, 400, invoice_id=invoice.id, route_ok=True)
        self.assertEqual(invoice.amount_residual, 600)
        # the stop to visit now asks for what is left, and says why
        self.assertEqual(stop.order_amount, 600)
        self.assertEqual(stop.paid_elsewhere, 400)
        payload = route._app_payload()["stops"][0]
        self.assertEqual(payload["paid_elsewhere"], 400)
        self.assertEqual(payload["order_amount"], 600)
        self.assertIn("cobró $ 400 en el local", route.message_ids[:1].body)

    def test_driver_collected_first_the_invoice_is_refused(self):
        order, invoice = self._invoiced_order()
        route = self._route(order, on_road=True)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=1000))])
        self.assertEqual(res[0]["status"], "ok", res)
        page = self._cash().cash_account(self.partner.id)
        self.assertIn("Repartidor Test ya cobró $ 1.000", page["invoices"][0]["route_note"])
        with self.assertRaisesRegex(UserError, "No se puede cobrar de nuevo esta factura"):
            self._cash().cash_register(self.partner.id, 1000, invoice_id=invoice.id,
                                       route_ok=True, force=True)
        # «a cuenta» is asked, not refused (it may be another debt)
        with self.assertRaisesRegex(RouteCashWarning, "ya cobró"):
            self._cash().cash_register(self.partner.id, 1000)
        # administración may register it anyway, explicitly
        with self.assertRaises(RouteCashWarning) as ctx:
            self._cash(self.admin_cobro).cash_register(self.partner.id, 1000,
                                                       invoice_id=invoice.id)
        self.assertEqual(ctx.exception.field, "forzar")
        cash = self._cash(self.admin_cobro).cash_register(
            self.partner.id, 1000, invoice_id=invoice.id, force=True)
        self.assertEqual(invoice.amount_residual, 0)

        # at the settlement the customer is left with a credit: a case says so
        self.assertIn("cobró $ 1.000 en la ruta y $ 1.000 en el local", route.double_cash_note)
        self._sync([self._event(route, "close", {})])
        route.write({"cash_counted": 1000,
                     "journal_id": self.company_data["default_journal_cash"].id})
        route.action_settle()
        case = route.case_ids.filtered(lambda c: c.kind == "other")
        self.assertEqual(len(case), 1)
        self.assertEqual(case.amount, 1000)
        self.assertEqual(case.stop_id, stop)
        self.assertIn("quedó con saldo a favor de $ 1.000", case.candidates)
        self.assertIn(cash.user_id.name, case.candidates)

    def test_no_case_without_a_credit(self):
        """Paying old debt at the local and today's order to the driver is
        not a double payment."""
        old = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": "2026-01-10", "invoice_line_ids": [Command.create({
                "name": "deuda", "quantity": 1, "price_unit": 300, "tax_ids": [Command.clear()]})]})
        old.action_post()
        order, invoice = self._invoiced_order()
        route = self._route(order, on_road=True)
        self._cash().cash_register(self.partner.id, 300, invoice_id=old.id)
        self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=1000)),
                    self._event(route, "close", {})])
        route.write({"cash_counted": 1000,
                     "journal_id": self.company_data["default_journal_cash"].id})
        route.action_settle()
        self.assertFalse(route.case_ids.filtered(lambda c: c.kind == "other"))
        self.assertEqual(invoice.amount_residual, 0)


@tagged("post_install", "-at_install", "distri_cobro_efectivo")
class TestCobroEfectivoHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "KIOSCO COBRO EFECTIVO", "ref": "EFE-HTTP-1", "customer_rank": 1})
        # 19.0.1.99.0 (audit F4): an amount over the debt is asked first; this
        # customer owes enough for the amounts below
        cls.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.partner.id, "fecha": "2026-01-10", "comprobante": "0001/2026/98801",
            "tipo": "factura", "debe": 10000.0})
        new_test_user(cls.env, login="cobro_http", name="Cobro Http", groups="base.group_user")
        new_test_user(cls.env, login="cobro_portal", groups="base.group_portal")

    def _csrf(self, html):
        return re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)

    def test_anonymous_and_portal_are_refused(self):
        res = self.url_open("/cobro", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/web/login", res.headers.get("Location", ""))
        self.authenticate("cobro_portal", "cobro_portal")
        self.assertEqual(self.url_open("/cobro").status_code, 403)
        res = self.url_open("/cobro/registrar", data={"cliente": self.partner.id, "importe": "10"})
        self.assertIn(res.status_code, (400, 403))
        self.assertFalse(self.env["distri.cash.collection"].sudo().search(
            [("partner_id", "=", self.partner.id)]))

    def test_register_on_account_from_the_page(self):
        self.authenticate("cobro_http", "cobro_http")
        found = self.url_open("/cobro?q=EFE-HTTP").text
        self.assertIn("KIOSCO COBRO EFECTIVO", found)
        self.assertIn("/cobro?cliente=%d" % self.partner.id, found)
        page = self.url_open("/cobro?cliente=%d" % self.partner.id).text
        self.assertIn("Registrar cobro en efectivo", page)
        self.assertIn("A cuenta", page)
        data = {"csrf_token": self._csrf(page), "cliente": self.partner.id, "importe": "1500",
                "factura": "", "nota": "en la vereda"}
        res = self.url_open("/cobro/registrar", data=data, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:500])
        cash = self.env["distri.cash.collection"].sudo().search(
            [("partner_id", "=", self.partner.id)])
        self.assertEqual(cash.amount, 1500)
        self.assertEqual(cash.user_id.login, "cobro_http")
        self.assertIn("a cuenta · en la vereda", cash.memo)
        self.assertEqual(cash.route_id.cash_expected, 1500)
        done = self.url_open(res.headers["Location"]).text
        self.assertIn("Cobro registrado", done)
        # the same again: asked first, not registered twice
        res = self.url_open("/cobro/registrar", data=data)
        self.assertEqual(res.status_code, 409)
        self.assertIn("¿Es otro cobro?", res.text)
        self.assertIn('name="confirmado" value="1"', res.text)
        self.assertEqual(len(self.env["distri.cash.collection"].sudo().search(
            [("partner_id", "=", self.partner.id)])), 1)
        res = self.url_open("/cobro/registrar", data=dict(data, confirmado="1"),
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        # a bad amount is refused with the form shown again
        res = self.url_open("/cobro/registrar", data=dict(data, importe="0"))
        self.assertEqual(res.status_code, 400)
        self.assertIn("Poné un importe mayor que cero", res.text)

    def test_without_csrf_token_nothing_is_registered(self):
        self.authenticate("cobro_http", "cobro_http")
        res = self.url_open("/cobro/registrar", data={"cliente": self.partner.id, "importe": "10"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.env["distri.cash.collection"].sudo().search(
            [("partner_id", "=", self.partner.id)]))

    def test_links_from_the_apps(self):
        self.authenticate("cobro_http", "cobro_http")
        self.assertIn('href="/cobro"', self.url_open("/tablero").text)
