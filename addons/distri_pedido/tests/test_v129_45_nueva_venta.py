"""19.0.1.129.45 (release rehearsal, finding 1): «Nueva venta»'s done page
/nueva-venta/hecho/<id> checks res.users._distri_may_reach_record (129.39)
after the backend's read access, as /pedidos/<id> does. The standard
«Personal Orders» rule lets any seller read an order without salesperson
(a web order); the page reads the rest with sudo, so an external seller
saw the number, total, state and WhatsApp of another customer's order.
Synthetic data only."""
from odoo import Command
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA


@tagged("post_install", "-at_install", "distri_v129_45")
class TestV12945NuevaVentaHecho(AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.externo = new_test_user(
            cls.env, login="externo_v145nv", name="Externo V145nv",
            groups="base.group_user,distri_pedido.grupo_vendedor", **company)
        cls.externo.sudo().distri_tipo_vendedor = "externo"
        cls.admin = new_test_user(
            cls.env, login="admin_v145nv", name="Admin V145nv",
            groups="base.group_user,distri_pedido.grupo_administracion", **company)
        Partner = cls.env["res.partner"]
        cls.ajeno = Partner.create({"name": "Cliente Web Ajeno V145", "company_type": "company",
                                    "distri_whatsapp": "+5492995001450"})
        cls.suyo = Partner.with_user(cls.externo).sudo().create(
            {"name": "Cliente Del Externo V145", "company_type": "company"})
        product = cls.product_a
        line = [Command.create({"product_id": product.id, "product_uom_qty": 1,
                                "price_unit": 1450, "tax_ids": [Command.clear()]})]
        Order = cls.env["sale.order"].sudo()
        # a web order without salesperson (AgilPedido's, the shop's)
        cls.web = Order.create({"partner_id": cls.ajeno.id, "order_line": line})
        cls.web.user_id = False
        cls.mine = Order.create({"partner_id": cls.suyo.id, "user_id": cls.externo.id,
                                 "order_line": line})

    def setUp(self):
        super().setUp()
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1")
        Param.set_param(PARAM_UI_V2, "1")

    def test_external_seller_does_not_reach_web_orders_without_salesperson(self):
        # the scenario: the backend rule does let him read the order
        self.assertFalse(self.web.user_id)
        self.assertTrue(self.web.with_user(self.externo).has_access("read"))
        self.authenticate(self.externo.login, self.externo.login)
        for url in ("/nueva-venta/hecho/%s" % self.web.id,
                    "/nueva-venta/hecho/%s?mostrador=1" % self.web.id):
            res = self.url_open(url, allow_redirects=False)
            self.assertEqual(res.status_code, 404, url)
            self.assertNotIn(self.web.name, res.text)
            self.assertNotIn(self.ajeno.name, res.text)
        # the same order on /pedidos/<id>: 404 too (129.39)
        self.assertEqual(self.url_open("/pedidos/%s" % self.web.id).status_code, 404)
        # his own order still opens
        res = self.url_open("/nueva-venta/hecho/%s" % self.mine.id)
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertIn(self.mine.name, res.text)

    def test_administracion_still_reaches_every_order(self):
        self.authenticate(self.admin.login, self.admin.login)
        res = self.url_open("/nueva-venta/hecho/%s" % self.web.id)
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertIn(self.web.name, res.text)
