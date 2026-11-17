"""«Cobro en efectivo» (19.0.1.92.0, models/cobro_efectivo.py,
controllers/cobro.py): any internal user registers a customer's cash, posted
at once on his depot cash box of the day, which «Cierre de caja depósito»
closes. Synthetic data only."""
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.cobro_efectivo import DuplicateCashError
from odoo.addons.distri_pedido.models.reparto import DEPOT_JOURNAL_CODE
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar


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
        route.action_close_depot()
        self.assertEqual(route.state, "settled")

    def test_cash_on_account(self):
        invoice = self._invoice(500)
        cash = self._cash().cash_register(self.partner.id, 150)
        self.assertFalse(cash.invoice_id)
        self.assertTrue(cash.memo.endswith("· a cuenta"), cash.memo)
        self.assertEqual(invoice.amount_residual, 500, "a cuenta: no invoice is touched")
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
        self.assertEqual(invoice.amount_residual, 199)

    def test_validations(self):
        invoice = self._invoice(500)
        other = self._invoice(300, partner=self.partner_b)
        self.assertEqual(self._cash()._cash_amount("1.500"), 1500)
        self.assertEqual(self._cash()._cash_amount("1.500,50"), 1500.5)
        self.assertEqual(self._cash()._cash_amount("$ 1500.5"), 1500.5)
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
        self.assertTrue(self._cash().cash_register(self.partner_b.id, 100))


@tagged("post_install", "-at_install", "distri_cobro_efectivo")
class TestCobroEfectivoHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "KIOSCO COBRO EFECTIVO", "ref": "EFE-HTTP-1", "customer_rank": 1})
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
