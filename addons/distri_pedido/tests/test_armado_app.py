"""19.0.1.94.0: /armado on the Distrineuquén kit. The kit files and their
cache, the pages, every action through fetch() (JSON) and the plain form
fallback (redirects). Synthetic data only."""
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import kit_version

FETCH = {"X-Distri-Fetch": "1"}


@tagged("post_install", "-at_install", "distri_armado_app")
class TestArmadoApp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        groups = "base.group_user,distri_pedido.grupo_armador"
        cls.packer = new_test_user(cls.env, login="armador_kit", name="Armador Kit", groups=groups,
                                   company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.other = new_test_user(cls.env, login="armador_kit_2", name="Otro Kit", groups=groups,
                                  company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.partner = cls.env["res.partner"].create({"name": "X - KIOSCO KIT PRUEBA", "city": "Centenario"})
        cls.product_a = cls.env["product.product"].create({
            "name": "Producto kit A", "list_price": 10, "type": "consu", "taxes_id": [Command.clear()]})
        cls.product_b = cls.env["product.product"].create({
            "name": "Producto kit B", "list_price": 20, "type": "consu", "taxes_id": [Command.clear()]})
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Kit armado", "company_id": company.id, "currency_id": company.currency_id.id})

    def setUp(self):
        super().setUp()
        self.order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": 3,
                                "price_unit": 10, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": 2,
                                "price_unit": 20, "tax_ids": [Command.clear()]}),
            ],
        })
        self.order.action_confirm()
        self.line_a = self.order.order_line.filtered(lambda l: l.product_id == self.product_a)
        self.line_b = self.order.order_line.filtered(lambda l: l.product_id == self.product_b)
        self.url = "/armado/pedido/%s" % self.order.id

    # -------------------------------------------------------------- helpers

    def _login(self, login="armador_kit"):
        self.authenticate(login, login)
        page = self.url_open(self.url)
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        return page

    def _post(self, action, fetch=True, **data):
        data["csrf_token"] = self.csrf
        return self.url_open("%s/%s" % (self.url, action), data=data,
                             headers=FETCH if fetch else None, allow_redirects=False)

    # ---------------------------------------------------------------- tests

    def test_kit_files_are_versioned_and_immutable(self):
        version = kit_version()
        res = self.url_open("/dn/dn.css?v=%s" % version)
        self.assertEqual(res.status_code, 200)
        self.assertIn("text/css", res.headers["Content-Type"])
        self.assertIn("immutable", res.headers["Cache-Control"])
        self.assertIn("--dn-navy:#10243f", res.text)
        res = self.url_open("/dn/dn.js?v=old")
        self.assertEqual(res.headers["Cache-Control"], "no-cache", "a stale version is not immutable")
        self.assertIn("javascript", res.headers["Content-Type"])
        self.assertIn('id="check"', self.url_open("/dn/dn-icons.svg?v=%s" % version).text)
        self.assertEqual(self.url_open("/dn/__manifest__.py").status_code, 404)

    def test_pages_on_the_kit(self):
        page = self._login()
        self.assertIn("/dn/dn.css?v=%s" % kit_version(), page.text)
        self.assertIn("/dn/armado.js?v=", page.text)
        self.assertIn('data-parent="/armado"', page.text)
        self.assertIn("Tomar pedido", page.text)
        self.assertEqual(page.headers["Cache-Control"], "no-store")
        lista = self.url_open("/armado")
        self.assertEqual(lista.status_code, 200)
        self.assertIn('class="dn-nav"', lista.text)
        self.assertIn("Kiosco Kit Prueba".upper(), lista.text.upper())
        self.assertIn('href="/armado/faltantes"', lista.text)
        self.assertIn('data-dn="down"', lista.text)
        # a packer whose only screen is /armado has no «Inicio» to go up to
        self.assertNotIn('href="/tablero"', lista.text)
        self.assertEqual(self.url_open("/armado/faltantes").status_code, 200)

    def test_fetch_flow_never_reloads(self):
        self._login()
        res = self._post("tomar")
        self.assertEqual(res.json(), {"ok": True, "url": self.url})
        page = self.url_open(self.url).text
        self.assertIn("Listo, 3", page)
        self.assertIn("Terminar y facturar", page)
        # «Listo» and back
        res = self._post("listo", linea_id=self.line_a.id, accion="listo")
        self.assertTrue(res.json()["ok"])
        self.assertIn("ren--listo", res.json()["html"])
        self.assertTrue(self.line_a.distri_listo)
        res = self._post("listo", linea_id=self.line_a.id, accion="deshacer")
        self.assertIn("ren--pend", res.json()["html"])
        self.assertFalse(self.line_a.distri_listo)
        # a shortage, then «No falta nada» counts as checked
        res = self._post("falta", linea_id=self.line_b.id, accion="todas")
        self.assertIn("ren--falta", res.json()["html"])
        self.assertEqual(self.line_b.distri_faltante, 2)
        self._post("falta", linea_id=self.line_b.id, accion="deshacer")
        self.assertEqual((self.line_b.distri_faltante, self.line_b.distri_listo), (0, True))
        # a line of another order is refused
        res = self._post("listo", linea_id=0)
        self.assertEqual(res.status_code, 422)
        # terminar: a typing mistake keeps the page (422), then it ends
        res = self._post("terminar", bultos="0")
        self.assertEqual(res.status_code, 422)
        self.assertEqual(self.order.distri_armado, "en_armado")
        res = self._post("terminar", bultos="2")
        self.assertEqual(res.json(), {"ok": True, "url": "/armado?terminado=%s" % self.order.id})
        self.assertEqual((self.order.distri_armado, self.order.distri_bultos), ("armado", 2))
        # double tap: the first one already finished it
        self.assertTrue(self._post("terminar", bultos="5").json()["ok"])
        self.assertEqual(self.order.distri_bultos, 2)
        # the list shows «Imprimir etiquetas» for it
        self.assertIn("/armado/pedido/%s/etiquetas" % self.order.id,
                      self.url_open("/armado?terminado=%s" % self.order.id).text)
        # corregir bultos
        res = self._post("bultos", bultos="3")
        self.assertEqual(res.json(), {"ok": True, "bultos": 3})
        self.assertEqual(self._post("bultos", bultos="x").status_code, 422)

    def test_plain_forms_still_redirect(self):
        self._login()
        res = self._post("tomar", fetch=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith(self.url))
        res = self._post("listo", fetch=False, linea_id=self.line_a.id, accion="listo")
        self.assertEqual(res.status_code, 303)
        # back on the next line to pack
        self.assertTrue(res.headers["Location"].endswith("#r%s" % self.line_b.id), res.headers["Location"])
        res = self._post("terminar", fetch=False, bultos="")
        self.assertIn("error=", res.headers["Location"])
        res = self._post("terminar", fetch=False, bultos="1")
        self.assertTrue(res.headers["Location"].endswith("/armado?terminado=%s" % self.order.id))

    def test_mas_tarde_clears_ticks_and_shortages(self):
        self._login()
        self._post("tomar")
        self._post("listo", linea_id=self.line_a.id)
        self._post("falta", linea_id=self.line_b.id, accion="faltan", faltan="1")
        res = self._post("mas-tarde")
        self.assertEqual(res.json(), {"ok": True, "url": "/armado?dejado=%s" % self.order.id})
        self.assertEqual(self.order.distri_armado, "pendiente")
        self.assertFalse(self.line_a.distri_listo)
        self.assertEqual((self.line_b.distri_faltante, self.line_b.product_uom_qty), (0, 2))

    def test_another_packer_cannot_touch_it(self):
        self.order.with_user(self.packer).sudo().distri_tomar()
        self._login("armador_kit_2")
        res = self._post("listo", linea_id=self.line_a.id)
        self.assertEqual(res.status_code, 409)
        self.assertIn("Armador Kit", res.json()["error"])
        self.assertEqual(self._post("terminar", bultos="1").status_code, 409)
        self.assertEqual(self.order.distri_armado, "en_armado")
        self.assertFalse(self.line_a.distri_listo)

    def test_non_packer_gets_json_403(self):
        new_test_user(self.env, login="vendedor_kit", groups="base.group_user")
        self.authenticate("vendedor_kit", "vendedor_kit")
        self.csrf = "x"
        res = self._post("listo", linea_id=self.line_a.id)
        self.assertIn(res.status_code, (400, 403))
        self.assertEqual(self.url_open("/armado").status_code, 403)
