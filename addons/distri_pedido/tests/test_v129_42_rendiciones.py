"""19.0.1.129.42 (item 4): the «Rendiciones» dead end and the kit's 403/404.

- «Recibir rendiciones» (/caja/rendicion, the cashier's screen) is shown on
  /rendiciones only to a cashier (bug fix, any flag): administración and
  gerencia got a bare English 403 there;
- with ui_v2 on, a refusal of a kit screen without website=True (/caja,
  /caja/rendicion, /cobro, /deposito...) is the branded Spanish page, with
  «Rutas para rendir» as the next step on the cash screens; fetch() calls
  and JSON APIs keep their plain answer; flag off: as in 129.41.
Nothing is written but the flag."""
from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.rendiciones import rows


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Rendiciones(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_v142r", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v142r", "distri_pedido.grupo_gerencia")
        cls.cajero = user("cajero_v142r", "distri_pedido.grupo_administracion,"
                                          "distri_pedido.grupo_cajero")
        cls.armador = user("armador_v142r", "distri_pedido.grupo_armador")

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def test_receive_row_only_for_cashiers(self):
        before = self._ledger()
        for flag in (False, True):
            self._flag(flag)
            for user in (self.admin, self.gerente):
                keys = [r["key"] for r in rows(self.env(user=user))]
                self.assertNotIn("recibir", keys, user.login)
                self.assertIn("rutas", keys, user.login)
            self.assertIn("recibir", [r["key"] for r in rows(self.env(user=self.cajero))])
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/rendiciones").text
        self.assertNotIn('href="/caja/rendicion"', page)
        self.assertIn('href="/rendiciones/rutas"', page)
        self.authenticate(self.cajero.login, self.cajero.login)
        self.assertIn('href="/caja/rendicion"', self.url_open("/rendiciones").text)
        self.assertEqual(self._ledger(), before)

    def test_branded_spanish_refusal_with_the_next_step(self):
        self._flag(True)
        self.authenticate(self.admin.login, self.admin.login)
        res = self.url_open("/caja/rendicion")
        self.assertEqual(res.status_code, 403)
        self.assertIn("Esta pantalla no es para tu usuario", res.text)
        self.assertIn('href="/rendiciones/rutas"', res.text, "the right next step")
        self.assertIn("Ir a mi inicio", res.text)
        self.assertNotIn("You don't have the permission", res.text)
        self.assertIn("text/html", res.headers["Content-Type"])
        # another kit screen without website=True, as a packer
        self.authenticate(self.armador.login, self.armador.login)
        for url in ("/caja", "/caja/historial"):
            res = self.url_open(url)
            self.assertEqual(res.status_code, 403, url)
            self.assertIn("Esta pantalla no es para tu usuario", res.text, url)
            self.assertNotIn("/rendiciones/rutas", res.text, "no settle step for a packer")
        # fetch() keeps the plain refusal (the page shows its own toast)
        res = self.url_open("/caja", headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("Esta pantalla no es para tu usuario", res.text)

    def test_flag_off_keeps_the_old_answer(self):
        self._flag(False)
        self.authenticate(self.admin.login, self.admin.login)
        res = self.url_open("/caja/rendicion")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("Esta pantalla no es para tu usuario", res.text)
