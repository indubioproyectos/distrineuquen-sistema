"""19.0.1.129.47: Inicio and «Nueva venta» on the design system (ui_v2), and
no «Instalar la app» for visitors.

- Inicio with ui_v2: the same tiles as staff_tiles() for every role (same
  visibility and links), every role in labelled groups, exactly one main
  tile (the first of his order); with the flag off, the 129.46 page.
- «Nueva venta» and its picker with ui_v2: the system's header and progress,
  same ids and data for the scripts; flag off as before.
- Ajustes: visitors (public user) get no install block and no manifest;
  logged-in customers and staff keep both.
Synthetic data only; nothing here writes an order, a payment or a move."""
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.tablero import (
    TILE_GROUPS, badge_short, grouped_tiles, staff_tiles)
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin

TILE_RE = re.compile(r'<a class="(dn-tile(?: dn-tile--main)?)" href="([^"]*)"[^>]*data-tile="([^"]+)"')
LABEL_RE = re.compile(r'<h2 class="dn-label">([^<]+)</h2>')
PARAM_VERIFICACION = "distri_pedido.verificacion_v2"


@tagged("post_install", "-at_install", "distri_v129_47")
class TestV12947Diseno(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        env["website"].search([]).company_id = env.company
        if not env["product.pricelist"].search([("name", "=", LISTA_GENERAL)], limit=1):
            env["product.pricelist"].create({"name": LISTA_GENERAL,
                                             "currency_id": env.company.currency_id.id})
        company = {"company_id": env.company.id, "company_ids": [Command.set(env.company.ids)]}

        def user(login, groups, **extra):
            return new_test_user(env, login=login, name=login.replace("_", " ").title(),
                                 groups=groups, **company, **extra)

        cls.cartera = env["res.partner.category"].create({"name": "Cartera Prueba147"})
        Partner = env["res.partner"]
        cls.cliente = Partner.create({
            "name": "Almacen Prueba147", "company_type": "company",
            "distri_whatsapp": "2995004701", "street": "Roca 147", "city": "Neuquén",
            "category_id": [Command.link(cls.cartera.id)]})
        env["product.template"].create({
            "name": "Gaseosa Prueba147", "type": "consu", "sale_ok": True, "list_price": 5000.0,
            "is_published": True, "taxes_id": [Command.clear()]})
        cls.roles = {
            "vendedor": user("vendedor_d147", "base.group_user,distri_pedido.grupo_vendedor"),
            "armador": user("armador_d147", "base.group_user,distri_pedido.grupo_armador"),
            "cajero": user("cajero_d147", "base.group_user,distri_pedido.grupo_cajero"),
            "mixto": user("mixto_d147", "base.group_user,distri_pedido.grupo_armador,"
                                        "distri_pedido.grupo_reparto"),
            "admin": user("admin_d147", "base.group_user,distri_pedido.grupo_administracion"),
            "gerencia": user("gerencia_d147", "base.group_user,distri_pedido.grupo_gerencia"),
            "sistemas": user("sistemas_d147", "base.group_user,base.group_system"),
        }
        cls.roles["vendedor"].etiqueta_cartera_id = cls.cartera
        cls.portal = user("cliente_d147", "base.group_portal", partner_id=Partner.create({
            "name": "Contacto Prueba147", "parent_id": cls.cliente.id}).id)

    def setUp(self):
        super().setUp()
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1")
        Param.set_param(PARAM_VERIFICACION, "1")

    def _flag(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _login(self, user):
        if user:
            self.authenticate(user.login, user.login)
        else:
            self.authenticate(None, None)

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    # ------------------------------------------------------------ Inicio

    def test_inicio_same_tiles_and_links_per_role(self):
        self._flag(True)
        before = self._ledger()
        expected = {
            "vendedor": ({"nuevaventa", "capacitacion"}, {"revisar", "caja", "armado", "equipo"}),
            "armador": ({"armado", "capacitacion"}, {"nuevaventa", "revisar", "caja", "equipo"}),
            "cajero": ({"caja", "capacitacion"}, {"nuevaventa", "armado", "equipo"}),
            "mixto": ({"armado", "mireparto"}, {"nuevaventa", "revisar", "caja", "equipo"}),
            "admin": ({"nuevaventa", "revisar", "equipo", "capacitacion"}, set()),
            "gerencia": ({"nuevaventa", "revisar", "equipo"}, set()),
            "sistemas": ({"equipo", "capacitacion"}, set()),
        }
        for role, user in self.roles.items():
            self._login(user)
            res = self.url_open("/tablero")
            self.assertEqual(res.status_code, 200, role)
            page = res.text
            self.assertIn('class="tab2"', page, role)
            tiles = staff_tiles(self.env(user=user))
            if user._distri_seller_only_accounts():
                # a seller locked out of the backend gets no backend tile
                tiles = [t for t in tiles if not t["url"].startswith("/odoo")]
            groups = grouped_tiles(tiles)
            drawn = TILE_RE.findall(page)
            # every tile of the role once, in the grouped order, with its link
            self.assertEqual([k for _c, _h, k in drawn],
                             [t["key"] for g in groups for t in g["tiles"]], role)
            urls = {t["key"]: t["url"] for t in tiles}
            for _cls, href, key in drawn:
                self.assertEqual(href, urls[key], "%s %s" % (role, key))
            # one main tile: the first of his order, on top of the first group
            mains = [k for c, _h, k in drawn if c.endswith("--main")]
            self.assertEqual(mains, [tiles[0]["key"]], role)
            self.assertEqual(drawn[0][2], tiles[0]["key"], role)
            # every role in groups, labelled
            labels = LABEL_RE.findall(page)
            self.assertEqual([lb for lb in labels if lb != "Para el repartidor"],
                             [g["label"] for g in groups], role)
            have, never = expected[role]
            keys = {k for _c, _h, k in drawn}
            self.assertLessEqual(have, keys, role)
            self.assertFalse(keys & never, role)
            # the old page's classes are gone
            self.assertNotIn('class="tab-tile"', page, role)
        self.assertEqual(self._ledger(), before)

    def test_inicio_flag_off_is_the_old_page(self):
        self._flag(False)
        self._login(self.roles["admin"])
        page = self.url_open("/tablero").text
        self.assertIn('class="tab-tile"', page)
        self.assertNotIn('class="tab2"', page)
        self.assertNotIn("dn-tile", page)

    def test_grouped_tiles_main_group_first_and_short_badges(self):
        tiles = [{"key": "cuentas"}, {"key": "nuevaventa"}, {"key": "capacitacion"}]
        groups = grouped_tiles(tiles)
        self.assertEqual([g["label"] for g in groups], ["Clientes", "Ventas", "Sistema"])
        self.assertEqual(groups[0]["tiles"][0]["key"], "cuentas")
        default = [{"key": "nuevaventa"}, {"key": "cuentas"}, {"key": "capacitacion"}]
        self.assertEqual([g["label"] for g in grouped_tiles(default)],
                         [lb for lb, _k in TILE_GROUPS if lb in ("Ventas", "Clientes", "Sistema")])
        self.assertEqual(grouped_tiles([]), [])
        self.assertEqual(badge_short(None), "")
        self.assertEqual(badge_short({"text": "5", "n": 5}), "5")
        self.assertEqual(badge_short({"text": "Abierta", "n": 1}), "Abierta")
        self.assertEqual(badge_short({"text": "12 sin confirmar · 2 armados sin factura", "n": 12}), "12")
        self.assertEqual(badge_short({"text": "1 armado sin factura", "n": 0, "sin_factura": 1}), "1")
        self.assertEqual(badge_short({"text": "441 · 436 vencidos", "n": 441}), "441")

    # ------------------------------------------------------------ Nueva venta

    def test_nueva_venta_design_system_with_the_flag(self):
        self._flag(True)
        self._login(self.roles["vendedor"])
        page = self.url_open("/nueva-venta").text
        self.assertIn("nv nv--v2 dn-has-nav", page)
        self.assertIn("nv-top--v2", page)
        self.assertIn('class="dn-progress nv-avance"', page)
        self.assertIn('data-dn-back=""', page)
        # the ids and markup the script and the older tests rely on
        for needed in ('<div id="nv-clientes" class="nv-list" aria-live="polite"></div>',
                       'id="nv-q-cliente"', 'id="nv-elegir"', 'id="nv-nuevo-abrir"',
                       'id="nv-paso-carrito"', 'id="nv-paso-salida"', 'id="nv-ok"',
                       'name="salida"', 'id="nv-renglones"'):
            self.assertIn(needed, page)
        self.assertNotIn('class="dn-back"', page, "one back: the header arrow")
        # the picker: the task header instead of the yellow bar and the shop's
        res = self.url_open("/nueva-venta/productos?cliente_id=%s" % self.cliente.id)
        self.assertEqual(res.status_code, 200, res.text[:300])
        html = res.text
        self.assertIn('class="nvp-top" id="nv-picker"', html)
        self.assertIn('data-cliente-id="%s"' % self.cliente.id, html)
        self.assertIn("Paso 2 de 3", html)
        self.assertIn("dn-nv-picker", html)
        self.assertIn("Confirmar productos", html)
        self.assertNotIn('<div class="cab">', html)
        self.assertNotIn('id="dn-home"', html, "the arrow goes back, no Inicio pill")

    def test_nueva_venta_flag_off_as_before(self):
        self._flag(False)
        self._login(self.roles["vendedor"])
        page = self.url_open("/nueva-venta").text
        self.assertIn('class="nv dn-has-nav"', page)
        self.assertIn('class="dn-back"', page)
        self.assertNotIn("nv--v2", page)
        self.assertNotIn("nv-avance", page)
        html = self.url_open("/nueva-venta/productos?cliente_id=%s" % self.cliente.id).text
        self.assertIn('<div class="editando" id="nv-picker"', html)
        self.assertIn('<div class="cab">', html)
        self.assertNotIn("nvp-top", html)

    # ------------------------------------------------------------ Ajustes

    def test_no_install_for_visitors(self):
        for on in (False, True):
            self._flag(on)
            self._login(None)
            for url in ("/pedido", "/", "/web/login"):
                res = self.url_open(url)
                self.assertEqual(res.status_code, 200, url)
                page = res.text
                self.assertIn('id="dn-gear"', page, url)
                self.assertIn("Tamaño de letra", page, url)
                self.assertNotIn("Instalar la app", page, url)
                self.assertNotIn("data-aj-instalar", page, url)
                self.assertNotIn("dn-aj__inst", page, url)
                self.assertNotIn("data-manifest", page, url)
                self.assertNotIn("cliente.webmanifest", page, url)

    def test_install_kept_for_customers_and_staff(self):
        self._flag(True)
        self._login(self.portal)
        page = self.url_open("/pedido").text
        self.assertIn("Instalar la app", page)
        self.assertIn('data-manifest="/dn/cliente.webmanifest"', page)
        self._login(self.roles["vendedor"])
        for url in ("/tablero", "/nueva-venta"):
            page = self.url_open(url).text
            self.assertIn("Instalar la app", page, url)
            self.assertIn('data-manifest="/tablero/manifest.webmanifest"', page, url)
        # the customers' manifest itself still answers
        self.assertEqual(self.url_open("/dn/cliente.webmanifest").json()["start_url"], "/pedido")

    def test_ajustes_script_without_install_block(self):
        """ajustes.js leaves the install block alone when it is not there."""
        from odoo.tools.misc import file_path
        with open(file_path("distri_pedido/static/src/kit/ajustes.js")) as f:
            js = f.read()
        self.assertIn("if (!box || !tip || !btn) { return; }", js)
