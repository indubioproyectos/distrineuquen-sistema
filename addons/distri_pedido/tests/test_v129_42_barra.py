"""19.0.1.129.42 (item 6, ui_v2): the bottom bar.

- /deposito and /despacho share the depot bar (Armado · Despacho · Cobrar ·
  Entregas), so /despacho has a way back to «Entregas»;
- a person with several roles gets the screen's role first and then the
  screens of his other roles that fit: administración + cajero gets
  «Cobrar» on /caja;
- with the flag off /deposito keeps its 129.41 bar.
Synthetic users; nothing is written but the flag."""
from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import kit
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, depot_nav, nav_keys, role_nav


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Barra(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_v142b", "distri_pedido.grupo_administracion")
        cls.admin_cajero = user("admcaj_v142b", "distri_pedido.grupo_administracion,"
                                                "distri_pedido.grupo_cajero")
        cls.cajero = user("cajero_v142b", "distri_pedido.grupo_cajero")
        cls.armador = user("armador_v142b", "distri_pedido.grupo_armador")

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _keys(self, user, current):
        return [i["key"] for i in role_nav(self.env(user=user), current)]

    def test_nav_keys_takes_the_screens_role_first(self):
        self.assertEqual(nav_keys(["administracion", "cajero"], "caja")[:3],
                         ["caja", "cobro", "comprobantes"])
        self.assertEqual(nav_keys(["administracion", "cajero"], "revisar")[:4],
                         ["revisar", "rutas", "comprobantes", "cuentas"])
        self.assertEqual(nav_keys(["administracion", "deposito"], "entregas")[:4],
                         ["armado", "despacho", "cobro", "entregas"])
        self.assertEqual(nav_keys(["administracion", "deposito"], "despacho")[:4],
                         ["armado", "despacho", "cobro", "entregas"])
        # /armado: the packer's bar (with «Faltantes»), not the depot one
        self.assertEqual(nav_keys(["administracion", "armador", "deposito"], "armado")[:3],
                         ["armado", "faltantes", "despacho"])
        self.assertEqual(nav_keys(["administracion", "armador", "deposito", "repartidor"],
                                  "cobro")[:4], ["armado", "despacho", "cobro", "entregas"])
        # the cashier's bar wins on /cobro over the depot one
        self.assertEqual(nav_keys(["administracion", "cajero", "deposito"], "cobro")[0], "caja")
        # a screen of no role keeps the first role's bar
        self.assertEqual(nav_keys(["armador"], "contactos")[:3], ["armado", "faltantes", "despacho"])
        self.assertEqual(nav_keys([], "caja"), [])
        # every key once
        keys = nav_keys(["gerencia", "administracion", "cajero", "deposito", "armador",
                         "repartidor", "vendedor"], "caja")
        self.assertEqual(len(keys), len(set(keys)))

    def test_admin_and_cashier_gets_cobrar_on_caja(self):
        self._flag(True)
        keys = self._keys(self.admin_cajero, "caja")
        self.assertEqual(keys[0], "inicio")
        self.assertIn("cobro", keys, "«Cobrar» on /caja for administración + cajero")
        self.assertIn("caja", keys)
        self.assertLessEqual(len(keys), kit.NAV_MAX)
        # on his administración screen he keeps that bar first
        self.assertEqual(self._keys(self.admin_cajero, "revisar")[1], "revisar")
        # a cashier alone: as in 129.41
        self.assertEqual(self._keys(self.cajero, "caja")[:3], ["inicio", "caja", "cobro"])

    def test_deposito_and_despacho_share_the_bar(self):
        self._flag(True)
        env = self.env(user=self.admin)
        deposito = [i["key"] for i in depot_nav(env, "entregas")]
        despacho = self._keys(self.admin, "despacho")
        self.assertEqual(deposito, despacho, "the same bar on both screens")
        self.assertIn("entregas", despacho, "a way back to Entregas from /despacho")
        self.assertEqual(deposito[0], "inicio")
        entregas = [i for i in depot_nav(env, "entregas") if i["key"] == "entregas"][0]
        self.assertEqual((entregas["url"], entregas["label"], entregas["on"]),
                         ("/deposito", "Entregas", True))
        # over HTTP: /despacho draws it
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/despacho").text
        self.assertIn('href="/deposito"', page)
        self.assertIn("Entregas", page)
        # a packer without the depot role keeps his own bar on /despacho
        self.assertNotIn("entregas", self._keys(self.armador, "despacho"))

    def test_flag_off_keeps_the_old_depot_bar(self):
        self._flag(False)
        keys = [i["key"] for i in depot_nav(self.env(user=self.admin), "entregas")]
        self.assertNotIn("inicio", keys)
        self.assertEqual(keys[-2:], ["cobro", "entregas"])
        self.assertEqual(role_nav(self.env(user=self.admin), "despacho") and True, True)
