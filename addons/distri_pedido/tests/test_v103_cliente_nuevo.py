"""19.0.1.103.0: «Cliente nuevo» in «Cobro en efectivo»: a provisional
customer with the cash «a cuenta», likely matches first, confirmed or merged
by administración (balances unchanged), listed on the cash closing without
blocking it. Synthetic data only."""
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.cliente_provisorio import (
    PROVISIONAL_LABEL, NewCustomerMatches,
)
from odoo.addons.distri_pedido.models.cobro_efectivo import DuplicateCashError, OverDebtWarning
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_cliente_nuevo")
class TestClienteNuevoModelo(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.cobrador = new_test_user(
            cls.env, login="cobrador_nuevo_v103", name="Cobrador Nuevo V103",
            groups="base.group_user", company_id=company.id,
            company_ids=[Command.set(company.ids)])
        cls.admin = new_test_user(
            cls.env, login="admin_nuevo_v103", name="Admin Nuevo V103",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.env["distri.route"]._depot_journal(company)
        cls.target = cls.partner_a
        cls.target.write({"name": "Almacen Existente V103", "phone": "0299 15 000-4104"})

    def _cash(self, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cobrador)

    def _invoice(self, amount, partner):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": "2026-01-10",
            "invoice_line_ids": [Command.create({"name": "deuda", "quantity": 1,
                                                 "price_unit": amount,
                                                 "tax_ids": [Command.clear()]})]})
        invoice.action_post()
        return invoice

    def _new(self, name="Kiosco Recien Llegado", phone="299 000-4103", amount="300", **kw):
        return self._cash().cash_register_new(name, amount, phone=phone, new_ok=True,
                                              over_ok=True, **kw)

    def test_payment_lands_on_the_provisional_customer(self):
        cash = self._new(street="Belgrano 10", city="Neuquén", note="pagó en la vereda")
        partner = cash.partner_id
        self.assertTrue(partner.distri_cliente_provisorio)
        self.assertEqual(partner.distri_provisorio_user_id, self.cobrador)
        self.assertEqual(partner.distri_provisorio_cobro_id, cash)
        self.assertEqual(partner.phone, "+54 9 299 000-4103")
        self.assertEqual((partner.street, partner.city), ("Belgrano 10", "Neuquén"))
        self.assertEqual(partner.distri_provisorio_etiqueta, PROVISIONAL_LABEL)
        self.assertEqual(cash.payment_id.partner_id, partner)
        self.assertTrue(cash.memo.endswith("a cuenta · pagó en la vereda"), cash.memo)
        self.assertAlmostEqual(partner._distri_account_balance(), -300)
        self.assertIn("Cliente nuevo creado por Cobrador Nuevo V103", partner.message_ids[0].body)
        # found by the search afterwards, marked
        rows = self._cash().cash_search("Recien Llegado")
        self.assertEqual([r["provisional"] for r in rows if r["id"] == partner.id], [True])

    def test_asks_first(self):
        Cash = self._cash()
        with self.assertRaises(UserError):
            Cash.cash_register_new("", "300", new_ok=True, over_ok=True)
        with self.assertRaises(UserError):
            Cash.cash_register_new("Kiosco Telefono Malo", "300", phone="4123456",
                                   new_ok=True, over_ok=True)
        with self.assertRaises(OverDebtWarning) as warning:
            Cash.cash_register_new("Kiosco Sin Parecidos Qwz", "300", new_ok=True)
        self.assertIn("Cliente nuevo: el importe queda a favor del cliente", str(warning.exception))
        # the same normalized phone, written another way, is shown first
        with self.assertRaises(NewCustomerMatches) as found:
            Cash.cash_register_new("Otro Nombre Qwz", "300", phone="+54 9 299 000-4104")
        first = found.exception.matches[0]
        self.assertEqual((first["id"], first["why"]), (self.target.id, "Mismo teléfono"))
        with self.assertRaises(NewCustomerMatches) as found:
            Cash.cash_register_new("Existente Qwz", "300")
        self.assertIn(self.target.id, [m["id"] for m in found.exception.matches])
        self.assertFalse(self.env["res.partner"].search([("distri_cliente_provisorio", "=", True),
                                                         ("name", "like", "Qwz")]))

    def test_double_submit_is_asked(self):
        self._new()
        with self.assertRaises(DuplicateCashError):
            self._new()
        self._new(confirmed=True)
        self.assertEqual(self.env["res.partner"].search_count(
            [("name", "=", "Kiosco Recien Llegado")]), 2)

    def test_confirm_needs_administracion(self):
        partner = self._new().partner_id
        with self.assertRaises(AccessError):
            partner.with_user(self.cobrador).action_distri_provisorio_confirmar()
        partner.with_user(self.admin).action_distri_provisorio_confirmar()
        self.assertFalse(partner.distri_cliente_provisorio)
        self.assertTrue(partner.active)
        self.assertIn("confirmado por Admin Nuevo V103", partner.message_ids[0].body)

    def test_merge_moves_the_payment_and_keeps_the_balances(self):
        invoice = self._invoice(500, self.target)
        partner = self._new(phone="299 000-4104").partner_id
        cash = partner.distri_provisorio_cobro_id
        before = partner._distri_account_balance() + self.target._distri_account_balance()
        self.assertAlmostEqual(before, 200)
        with self.assertRaises(AccessError):
            partner.with_user(self.cobrador).action_distri_provisorio_unir()
        action = partner.with_user(self.admin).action_distri_provisorio_unir()
        wizard = self.env["distri.provisorio.unir"].with_user(self.admin).browse(action["res_id"])
        self.assertIn(self.target, wizard.suggestion_ids)
        wizard.target_id = self.target
        wizard.action_unir()
        self.env.invalidate_all()
        self.assertFalse(partner.active)
        self.assertFalse(partner.distri_cliente_provisorio)
        self.assertEqual(partner.distri_provisorio_unido_id, self.target)
        self.assertEqual(cash.partner_id, self.target)
        self.assertEqual(cash.payment_id.partner_id, self.target)
        self.assertEqual(partner.commercial_partner_id, partner)
        self.assertAlmostEqual(self.target._distri_account_balance(), before)
        self.assertAlmostEqual(partner._distri_account_balance(), 0)
        self.assertAlmostEqual(invoice.amount_residual, 200, msg="«a cuenta»: oldest invoice")
        self.assertIn("Se unió el cliente nuevo", self.target.message_ids[0].body)
        self.assertIn("Unido con", partner.message_ids[0].body)

    def test_closing_is_not_blocked(self):
        cash = self._new()
        route = cash.route_id
        self.assertIn(cash.partner_id, route.distri_provisorio_ids)
        route.cash_counted = route.cash_expected
        route._close_depot_box()
        self.assertEqual(route.state, "settled")
        self.assertTrue(cash.partner_id.distri_cliente_provisorio)


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_cliente_nuevo")
class TestClienteNuevoPagina(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        new_test_user(cls.env, login="cobro_nuevo_http", name="Cobro Nuevo Http",
                      groups="base.group_user")
        cls.existing = cls.env["res.partner"].create({
            "name": "Despensa Parecida V103", "phone": "299 000-4105", "customer_rank": 1})

    def _page(self, path):
        res = self.url_open(path)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.text

    def test_new_customer_from_the_page(self):
        self.authenticate("cobro_nuevo_http", "cobro_nuevo_http")
        found = self._page("/cobro?q=Zzqx Inexistente")
        self.assertIn("¿No está? Cliente nuevo", found)
        form = self._page("/cobro?nuevo=1&q=Zzqx Inexistente")
        self.assertIn('action="/cobro/nuevo"', form)
        self.assertIn('value="Zzqx Inexistente"', form)
        data = {"csrf_token": CSRF.search(form).group(1), "nombre": "Zzqx Wqrtz",
                "telefono": "0299 15 000-4103", "importe": "2.500", "nota": "primera compra"}
        res = self.url_open("/cobro/nuevo", data=data)
        self.assertEqual(res.status_code, 409)
        self.assertIn("Cliente nuevo: el importe queda a favor del cliente", res.text)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Zzqx Wqrtz")]))
        data["csrf_token"] = CSRF.search(res.text).group(1)
        res = self.url_open("/cobro/nuevo", data=dict(data, deuda_ok="1"), allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:500])
        partner = self.env["res.partner"].search([("name", "=", "Zzqx Wqrtz")])
        self.assertTrue(partner.distri_cliente_provisorio)
        self.assertEqual(partner.distri_provisorio_cobro_id.amount, 2500)
        self.assertIn("Cobro registrado", self.url_open(res.headers["Location"]).text)
        self.assertIn(PROVISIONAL_LABEL, self._page("/cobro?q=Zzqx Wqrtz"))
        self.assertIn(PROVISIONAL_LABEL, self._page("/cobro?cliente=%d" % partner.id))

    def test_duplicate_phone_is_suggested(self):
        self.authenticate("cobro_nuevo_http", "cobro_nuevo_http")
        form = self._page("/cobro?nuevo=1")
        data = {"csrf_token": CSRF.search(form).group(1), "nombre": "Otra Cosa Zzqx",
                "telefono": "+54 9 299 000-4105", "importe": "100"}
        res = self.url_open("/cobro/nuevo", data=data)
        self.assertEqual(res.status_code, 409)
        self.assertIn("¿Es alguno de estos?", res.text)
        self.assertIn("Despensa Parecida V103", res.text)
        self.assertIn("Mismo teléfono", res.text)
        self.assertIn("/cobro?cliente=%d" % self.existing.id, res.text)
        self.assertIn('name="nuevo_ok" value="1"', res.text)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Otra Cosa Zzqx")]))
        # «No es ninguno»: created
        data["csrf_token"] = CSRF.search(res.text).group(1)
        res = self.url_open("/cobro/nuevo", data=dict(data, nuevo_ok="1", deuda_ok="1"),
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertTrue(self.env["res.partner"].search([("name", "=", "Otra Cosa Zzqx")])
                        .distri_cliente_provisorio)

    def test_portal_cannot(self):
        new_test_user(self.env, login="portal_nuevo_v103", groups="base.group_portal")
        self.authenticate("portal_nuevo_v103", "portal_nuevo_v103")
        res = self.url_open("/cobro/nuevo", data={"nombre": "Portal Zzqx", "importe": "10"})
        self.assertIn(res.status_code, (400, 403))
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Portal Zzqx")]))


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_cliente_nuevo")
class TestClienteNuevoEnLaCaja(CajaMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)
        cls.admin_cashier = new_test_user(
            cls.env, login="cajero_admin_v103", name="Cajero Admin V103",
            groups="base.group_user,distri_pedido.grupo_cajero,distri_pedido.grupo_administracion")

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF.search(page.text).group(1), page.text

    def test_listed_on_the_closing_which_is_not_blocked(self):
        partner = self.env["distri.cash.collection"].with_user(self.seller).cash_register_new(
            "Zzqx Caja Nueva", "400", new_ok=True, over_ok=True).partner_id
        self.authenticate("cajero_test", "cajero_test")
        token, _html = self._csrf("/caja")
        self.url_open("/caja/abrir", data={"csrf_token": token, "fondo": "1000"})
        caja = self.env["distri.caja"].current_for(self.cashier)
        token, html = self._csrf("/caja/cierre")
        self.assertIn("Clientes nuevos a confirmar", html)
        self.assertIn("Zzqx Caja Nueva", html)
        self.assertNotIn("/caja/cliente-nuevo/%d/confirmar" % partner.id, html,
                         "a cashier without administración only sees it")
        self.assertIn("Zzqx Caja Nueva", self.url_open("/caja/rendicion").text)
        res = self.url_open("/caja/cliente-nuevo/%d/confirmar" % partner.id,
                            data={"csrf_token": token})
        self.assertEqual(res.status_code, 403)
        self.assertTrue(partner.distri_cliente_provisorio)
        data = {"csrf_token": token, "queda": "0", "motivo": "prueba",
                "cant_%s" % self.bills[500].id: "2"}
        res = self.url_open("/caja/cierre", data=data, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertEqual(caja.state, "closed")
        self.assertTrue(partner.distri_cliente_provisorio)

    def test_administracion_confirms_from_the_caja(self):
        partner = self.env["distri.cash.collection"].with_user(self.seller).cash_register_new(
            "Zzqx Caja Confirmar", "400", new_ok=True, over_ok=True).partner_id
        self.authenticate("cajero_admin_v103", "cajero_admin_v103")
        token, _html = self._csrf("/caja")
        self.url_open("/caja/abrir", data={"csrf_token": token, "fondo": "1000"})
        token, html = self._csrf("/caja/rendicion")
        self.assertIn("/caja/cliente-nuevo/%d/confirmar" % partner.id, html)
        res = self.url_open("/caja/cliente-nuevo/%d/confirmar" % partner.id,
                            data={"csrf_token": token, "volver": "/caja/rendicion"},
                            headers={"X-Distri-Fetch": "1"})
        self.assertTrue(res.json()["ok"], res.text)
        self.assertTrue(res.json()["url"].startswith("/caja/rendicion?ok="))
        self.assertFalse(partner.distri_cliente_provisorio)
