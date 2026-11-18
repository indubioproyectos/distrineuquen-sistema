"""Legacy pages save without stacking history (19.0.1.95.0,
models/ir_http.py, static/src/js/sin_historial.js): every such page loads the
script, the server turns a redirect for the script into 200 +
X-Distri-Location (keeping the #fragment), and without JavaScript the forms
work exactly as before."""
import gzip
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import kit_version
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
NAV = {"X-Distri-Nav": "1"}
MACRO = 't-call="distri_pedido.sin_historial"'
# the legacy pages with forms or same-page links; alta_revendedor_head is
# the <head> of «Soy revendedor» and «Mi perfil»
LEGACY = ("login_page", "cobro_page", "pantalla", "seguimiento",
          "cuenta_corriente", "mi_clave", "gracias", "datos_entrega", "alta_revendedor_head")
# distri_ui.css single-page apps: no forms, hash routes, nothing to fix
SPA = {"reparto_app", "despacho_app", "cuentas_app"}


@tagged("post_install", "-at_install", "distri_sin_historial")
class TestSinHistorial(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls.staff = new_test_user(
            cls.env, login="empleado_historial", name="Empleado Historial",
            groups="base.group_user", password="empleado-historial-1",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)],
        )
        cls.partner = cls.env["res.partner"].create({
            "name": "ALMACEN SIN HISTORIAL", "street": "Calle Falsa 123", "city": "Neuquén"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto historial", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.order = cls._order(cls.partner, cls.product, qty=3, price=100)
        cls.order._portal_ensure_token()
        cls.token = cls.order.access_token

    def _script(self):
        return "/dn/sin_historial.js?v=%s" % kit_version()

    def _entrega(self, data, headers=None):
        url = "/pedido/%s/entrega" % self.token
        csrf = CSRF_RE.search(self.url_open(url).text).group(1)
        return self.url_open(url, data=dict(data, csrf_token=csrf), headers=headers,
                             allow_redirects=False)

    # ---------------------------------------------------------- the pages

    def test_every_legacy_page_loads_the_script(self):
        views = self.env["ir.ui.view"].sudo().search([
            ("key", "=like", "distri_pedido.%"), ("type", "=", "qweb")])
        by_name = {view.key.split(".", 1)[1]: view.arch_db for view in views}
        for name in LEGACY:
            self.assertIn(MACRO, by_name[name], name)
        for name, arch in by_name.items():
            if "distri_ui.css" in arch or "shared_css" in arch:
                self.assertTrue(MACRO in arch or name in SPA, "%s uses distri_ui.css" % name)
            if "distri_pedido.dn_head" in arch:
                # /armado: the kit router does it; never both
                self.assertNotIn(MACRO, arch, name)
        self.assertEqual({n for n in SPA if MACRO in by_name[n]}, set())

        script = self._script()
        self.assertIn(script, self.url_open("/web/login").text)
        self.assertIn(script, self.url_open("/pedido/%s" % self.token).text)
        self.assertIn(script, self.url_open("/pedido/%s/entrega" % self.token).text)
        self.authenticate(self.staff.login, "empleado-historial-1")
        page = self.url_open("/cobro").text
        self.assertIn(script, page)
        self.assertRegex(page, r'<form [^>]*action="/cobro" role="search" data-side="">')
        # QWeb writes the static attributes before the t-att ones
        self.assertRegex(page, r'<a class="back" data-up="" href="[^"]+">‹ Volver</a>')

    def test_mercado_pago_form_posts_natively(self):
        arch = self.env.ref("distri_pedido.seguimiento").arch_db
        self.assertRegex(arch, r"/pagar' % token\" data-native=\"\"")

    def test_script_is_served_compact_and_cached(self):
        res = self.url_open(self._script())
        self.assertEqual(res.status_code, 200)
        self.assertIn("javascript", res.headers["Content-Type"])
        self.assertIn("immutable", res.headers["Cache-Control"])
        body = res.content
        self.assertFalse(body.lstrip().startswith(b"/*"))
        self.assertIn(b"X-Distri-Nav", body)
        self.assertIn(b"X-Distri-Location", body)
        self.assertLess(len(gzip.compress(body, 9)), 3 * 1024)

    # ------------------------------------------- redirects and #fragments

    def test_entrega_without_javascript_is_unchanged(self):
        res = self._entrega({"accion": "aviso", "tipo": "other_day", "texto": "el jueves"})
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/pedido/%s#aviso" % self.token),
                        res.headers["Location"])
        self.assertNotIn("X-Distri-Location", res.headers)
        self.assertEqual(self.order.distri_customer_notice, "el jueves")

    def test_entrega_with_the_script_keeps_the_fragment(self):
        res = self._entrega({"accion": "aviso", "tipo": "other_day", "texto": "el viernes"},
                            headers=NAV)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("Location", res.headers)
        self.assertTrue(res.headers["X-Distri-Location"].endswith("/pedido/%s#aviso" % self.token),
                        res.headers["X-Distri-Location"])
        self.assertEqual(res.content, b"")
        self.assertEqual(self.order.distri_customer_notice, "el viernes")

        res = self._entrega({"accion": "horarios", "dia_0": "1", "desde_0": "08:00",
                             "hasta_0": "12:00"}, headers=NAV)
        self.assertTrue(res.headers["X-Distri-Location"].endswith(
            "/pedido/%s/entrega?listo=horarios#horarios" % self.token),
            res.headers["X-Distri-Location"])

        # an error renders the page (the script writes it over the current one)
        res = self._entrega({"accion": "aviso", "tipo": "other", "texto": ""}, headers=NAV)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("X-Distri-Location", res.headers)
        self.assertIn("Contanos qué pasa", res.text)
        self.assertIn(self._script(), res.text)

    # ---------------------------------------------------------- the login

    def _login(self, password, headers=None):
        self.url_open("/web/session/logout")
        csrf = CSRF_RE.search(self.url_open("/web/login").text).group(1)
        return self.url_open("/web/login", data={
            "csrf_token": csrf, "login": self.staff.login, "password": password,
            "redirect": "/cobro", "type": "password"}, headers=headers, allow_redirects=False)

    def test_login_with_and_without_the_script(self):
        res = self._login("empleado-historial-1")
        self.assertIn(res.status_code, (302, 303))
        self.assertTrue(res.headers["Location"].endswith("/cobro"), res.headers["Location"])

        res = self._login("empleado-historial-1", headers=NAV)
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.headers["X-Distri-Location"].endswith("/cobro"))
        # the session is open: the page the script goes to answers
        self.assertEqual(self.url_open("/cobro", allow_redirects=False).status_code, 200)

        res = self._login("clave-equivocada", headers=NAV)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("X-Distri-Location", res.headers)
        self.assertIn('name="password"', res.text)
        self.assertIn(self._script(), res.text)
