"""19.0.1.129.42 (item 9): the one-line link fixes of the audit.

- /rutas «Rendiciones» opens the kit page /rendiciones (ui_v2), the backend
  action with the flag off;
- /deposito/mostrador «Tomar un pedido» is «Nueva venta» (/nueva-venta)
  while that flag is on;
- verification: «Abrir» of a customer row is the kit customer screen
  (/clientes/<id>, done in 129.41; checked here), receipt cards stay.
Nothing is written but the flags."""
from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.models.verificacion import _partner_url

PARAM_NUEVA_VENTA = "distri_pedido.nueva_venta"


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Enlaces(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin = new_test_user(
            cls.env, login="admin_v142e", name="Admin V142e",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cls.partner = cls.env["res.partner"].create({"name": "Cliente Enlaces V142",
                                                     "customer_rank": 1})

    def _flags(self, ui_v2, nueva_venta=False):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_UI_V2, "1" if ui_v2 else "0")
        Param.set_param(PARAM_NUEVA_VENTA, "1" if nueva_venta else "0")

    def test_rutas_rendiciones_link(self):
        self.authenticate(self.admin.login, self.admin.login)
        self._flags(True)
        page = self.url_open("/rutas").text
        self.assertIn('<a href="/rendiciones">Rendiciones</a>', page)
        self._flags(False)
        page = self.url_open("/rutas").text
        self.assertIn('<a href="/odoo/action-distri_pedido.action_route_settle">Rendiciones</a>', page)

    def test_mostrador_label_names_nueva_venta(self):
        self.authenticate(self.admin.login, self.admin.login)
        self._flags(False, nueva_venta=True)
        page = self.url_open("/deposito/mostrador?q=zzzz-nada-v142").text
        self.assertIn('href="/nueva-venta">Cargar el pedido con «Nueva venta»', page)
        self.assertNotIn("«Tomar un pedido»", page)
        self._flags(False, nueva_venta=False)
        page = self.url_open("/deposito/mostrador?q=zzzz-nada-v142").text
        self.assertIn('href="/pedido">Cargar el pedido con «Tomar un pedido»', page)

    def test_verification_customer_opens_the_kit_screen(self):
        self._flags(True)
        self.assertTrue(_partner_url(self.env, self.partner, self.admin).startswith(
            "/clientes/%s" % self.partner.id))
        self._flags(False)
        self.assertEqual(_partner_url(self.env, self.partner, self.admin),
                         "/odoo/res.partner/%s" % self.partner.id)
