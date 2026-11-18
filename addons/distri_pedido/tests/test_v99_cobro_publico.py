"""19.0.1.99.0: /cobro asks before an amount over the debt (audit F4) and
the public site without sample data (audit F14). Synthetic data only."""
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.cobro_efectivo import OverDebtWarning

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_cobro")
class TestCobroSuperaDeuda(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.cobrador = new_test_user(
            cls.env, login="cobrador_v99", name="Cobrador V99", groups="base.group_user",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.partner = cls.env["res.partner"].create({"name": "Kiosco Deuda V99",
                                                     "company_type": "company"})
        cls.env["distri.route"]._depot_journal(company)
        cls.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.partner.id, "fecha": "2026-10-01", "comprobante": "0001/2026/99951",
            "tipo": "factura", "debe": 1000.0})

    def _cash(self):
        return self.env["distri.cash.collection"].with_user(self.cobrador)

    def test_over_the_debt_is_asked_first(self):
        with self.assertRaises(OverDebtWarning) as caught:
            self._cash().cash_register(self.partner.id, "1500")
        self.assertEqual(str(caught.exception.args[0]),
                         "El importe supera la deuda de $ 1.000. ¿Registrar igual? La diferencia "
                         "queda a favor del cliente ($ 500).")
        self.assertFalse(self.env["distri.cash.collection"].sudo().search(
            [("partner_id", "=", self.partner.id)]))
        # up to the debt: no question
        self.assertTrue(self._cash().cash_register(self.partner.id, "400"))
        # confirmed: registered, the difference in his favor
        cash = self._cash().cash_register(self.partner.id, "1500", over_ok=True)
        self.assertEqual(cash.amount, 1500)
        self.assertAlmostEqual(self.partner._distri_account_balance(), -900.0)
        # nothing owed: every amount is asked
        with self.assertRaises(OverDebtWarning) as caught:
            self._cash().cash_register(self.partner.id, "10")
        self.assertIn("supera la deuda de $ 0", caught.exception.args[0])

    def test_page_shows_the_confirm_step(self):
        self.authenticate("cobrador_v99", "cobrador_v99")
        page = self.url_open("/cobro?cliente=%d" % self.partner.id)
        csrf = CSRF.search(page.text).group(1)
        res = self.url_open("/cobro/registrar", data={
            "csrf_token": csrf, "cliente": self.partner.id, "importe": "2.000", "factura": ""})
        self.assertEqual(res.status_code, 409)
        self.assertIn('id="cobro-supera"', res.text)
        self.assertIn("El importe supera la deuda de $ 1.000. ¿Registrar igual? La diferencia queda "
                      "a favor", res.text)
        self.assertIn('name="deuda_ok" value="1"', res.text)
        self.assertIn("Sí, registrar igual", res.text)
        self.assertIn('value="2.000"', res.text, "the amount stays typed")
        res = self.url_open("/cobro/registrar", data={
            "csrf_token": csrf, "cliente": self.partner.id, "importe": "2.000", "factura": "",
            "deuda_ok": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/cobro?hecho=", res.headers["Location"])


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_publico")
class TestSitioPublico(HttpCase):

    def test_contactus_goes_home(self):
        for url in ("/contactus", "/contactus-thank-you"):
            res = self.url_open(url, allow_redirects=False)
            self.assertEqual(res.status_code, 301, url)
            self.assertTrue(res.headers["Location"].rstrip("/").endswith(
                self.base_url().rstrip("/")) or res.headers["Location"] == "/", res.headers)

    def test_website_info_hidden(self):
        self.assertEqual(self.url_open("/website/info").status_code, 404)

    def test_sitemap_without_products_or_contactus(self):
        res = self.url_open("/sitemap.xml")
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("/shop/", res.text)
        self.assertNotIn("/contactus", res.text)
        self.assertNotIn("/website/info", res.text)

    def test_own_error_pages(self):
        res = self.url_open("/esto-no-existe-v99")
        self.assertEqual(res.status_code, 404)
        self.assertIn("No encontramos la página", res.text)
        self.assertNotIn("Somos un equipo", res.text)
        self.assertNotIn("o_wsale_my_cart", res.text)
        seller = new_test_user(self.env, login="vendedor_403_v99", name="Vendedor 403 V99",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        self.authenticate(seller.login, seller.login)
        res = self.url_open("/armado")
        self.assertEqual(res.status_code, 403)
        self.assertIn("Esta pantalla no es para tu usuario", res.text)
        self.assertIn("Ir a mi inicio", res.text)
