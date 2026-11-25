"""19.0.1.129.42 (item 5): the kit's buttons.

- dn.css has the one small button (.dn-btn--sm, 44 px) and the one «‹
  Volver» text link (.dn-back); the local variants (.nv-btn-sm 34 px,
  .dp-btn-sm, .caja-btn-auto) are gone from the CSS and the templates;
- with ui_v2 on, /cobro, /despacho and /cuentas: the plain navy «.btn»
  takes the kit's green (CSS scoped to .dn-legado), and the floating
  «Inicio» pill is not drawn next to a bottom bar that has «Inicio»;
- flag off: the pill as before.
Nothing is written but the flag."""
import re

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Botones(HttpCase):

    def _read(self, rel):
        with open("%s/%s" % (get_module_path("distri_pedido"), rel), encoding="utf-8") as f:
            return f.read()

    def test_one_small_button_and_one_back_link(self):
        css = self._read("static/src/kit/dn.css")
        rule = re.search(r"\.dn-btn--sm\{([^}]*)\}", css)
        self.assertTrue(rule)
        self.assertIn("min-height:44px", rule.group(1))
        self.assertIn(".dn-back{", css)
        self.assertRegex(css, r"\.dn-back\{[^}]*min-height:44px")
        for rel, gone in (("static/src/nueva_venta/nueva_venta.css", ".nv-btn-sm"),
                          ("static/src/deposito/deposito.css", ".dp-btn-sm"),
                          ("static/src/deposito/deposito.css", ".dp-back"),
                          ("static/src/caja/caja.css", ".caja-btn-auto"),
                          ("views/nueva_venta_app.xml", "nv-btn-sm"),
                          ("views/deposito_app.xml", "dp-btn-sm"),
                          ("views/caja.xml", "caja-btn-auto")):
            self.assertNotIn(gone, self._read(rel), rel)
        # «summary» of the folded parts of «Nueva venta» at 44 px too
        self.assertIn("min-height:44px", re.search(r"\.nv-mas>summary\{[^}]*\}",
                                                   self._read("static/src/nueva_venta/nueva_venta.css")).group(0))
        self.assertIn(".dn-legado .btn:not(.outline)", css)

    def test_inicio_pill_only_without_a_bar(self):
        cajero = new_test_user(self.env, login="cajero_v142k", name="Cajero V142k",
                               groups="base.group_user,distri_pedido.grupo_cajero,"
                                      "distri_pedido.grupo_administracion",
                               company_id=self.env.company.id,
                               company_ids=[Command.set(self.env.company.ids)])
        self.authenticate(cajero.login, cajero.login)
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_UI_V2, "1")
        for url in ("/cobro", "/despacho", "/cuentas"):
            page = self.url_open(url).text
            self.assertIn('class="dn-nav"', page, url)
            self.assertNotIn('id="dn-home"', page, url)
        Param.set_param(PARAM_UI_V2, "0")
        page = self.url_open("/cobro").text
        self.assertIn('id="dn-home"', page, "flag off: the pill as before")
        self.assertIn('class="dn-back"', self.url_open("/deposito/mostrador").text)
