"""19.0.1.129.42 (item 7, ui_v2): Inicio.

- administración's tiles in labelled groups (Ventas, Reparto, Cobros y caja,
  Clientes, Sistema), every tile once and in its order;
- «Tablero de verificación» before «Capacitación»;
- no Odoo apps («Todas las aplicaciones del sistema») for a packer or a
  driver; administración keeps them;
- the flag off: the plain grid, verification last, the apps as before."""
from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.tablero import (
    TILE_GROUPS, grouped_tiles, hides_apps, staff_tiles)

PARAM_VERIFICACION = "distri_pedido.verificacion_v2"


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Inicio(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_v142i", "distri_pedido.grupo_administracion")
        cls.mixto = user("mixto_v142i", "distri_pedido.grupo_armador,distri_pedido.grupo_reparto")
        cls.cajero = user("cajero_v142i", "distri_pedido.grupo_cajero")

    def _flags(self, on=True):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_UI_V2, "1" if on else "0")
        Param.set_param(PARAM_VERIFICACION, "1")

    def test_groups_cover_every_tile_once(self):
        self._flags(True)
        tiles = staff_tiles(self.env(user=self.admin))
        groups = grouped_tiles(tiles)
        flat = [t["key"] for g in groups for t in g["tiles"]]
        self.assertEqual(sorted(flat), sorted(t["key"] for t in tiles))
        labels = [g["label"] for g in groups]
        self.assertEqual(labels, [label for label, _k in TILE_GROUPS if label in labels])
        self.assertEqual(labels[-1], "Sistema")
        self.assertIn("capacitacion", [t["key"] for t in groups[-1]["tiles"]])

    def test_admin_inicio_in_groups_with_verification_before_training(self):
        self._flags(True)
        keys = [t["key"] for t in staff_tiles(self.env(user=self.admin))]
        if "verificacion" in keys:
            self.assertLess(keys.index("verificacion"), keys.index("capacitacion"))
            self.assertEqual(keys[-1], "capacitacion")
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/tablero").text
        self.assertIn('class="tab-grupos"', page)
        for label in ("Ventas", "Reparto", "Sistema"):
            self.assertIn('class="dn-label tab-grupo__t">%s<' % label, page)
        self.assertIn("tab tab--v2", page)
        self.assertIn("Todas las aplicaciones del sistema", page, "administración keeps the apps")

    def test_packer_and_driver_without_odoo_apps(self):
        self._flags(True)
        self.assertTrue(hides_apps(self.mixto))
        self.assertFalse(hides_apps(self.admin))
        self.assertFalse(hides_apps(self.cajero))
        self.authenticate(self.mixto.login, self.mixto.login)
        page = self.url_open("/tablero").text
        self.assertNotIn("Todas las aplicaciones del sistema", page)
        self.assertNotIn('class="tab-grupos"', page, "only administración gets the groups")

    def test_flag_off_is_the_old_inicio(self):
        self._flags(False)
        keys = [t["key"] for t in staff_tiles(self.env(user=self.admin))]
        if "verificacion" in keys:
            self.assertEqual(keys[-1], "verificacion", "appended last, as in 129.41")
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/tablero").text
        self.assertNotIn("tab-grupos", page)
        self.assertNotIn("tab--v2", page)
        self.authenticate(self.mixto.login, self.mixto.login)
        self.assertEqual(self.url_open("/tablero").status_code, 200)
