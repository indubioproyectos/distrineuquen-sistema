"""19.0.1.99.0 (audit F7, F12): the receipt code exists from the moment the
customer places the order, he sees it on the confirmation and on his order
page (also an order a seller took for him), packing keeps it, and wrong
codes lock it after distri_pedido.codigo_recibido_intentos until
administración unlocks it. Synthetic data only."""
import json
import re

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.reparto_cliente import PARAM_CODE_ATTEMPTS
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

NOMBRE = "Kiosco Codigo V99"


def _wrong(code):
    return "%04d" % ((int(code) + 1) % 10000)


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_codigo")
class TestCodigoEnLaPagina(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.plantilla = cls.env["product.template"].create({
            "name": "Producto Codigo V99", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.vendedor = new_test_user(cls.env, login="vendedor_codigo_v99", name="Vendedor Codigo",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.cliente = cls.env["res.partner"].create({"name": "Almacen Codigo V99",
                                                     "company_type": "company"})

    def _data(self, **extra):
        data = {"renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 1}]),
                "nombre": NOMBRE, "telefono": "2995550199", "domicilio": "Calle V99 123",
                "entrega": "domicilio"}
        data.update(extra)
        return data

    def _last(self):
        return self.env["sale.order"].sudo().search([], order="id desc", limit=1)

    def test_customer_sees_the_code_from_the_start(self):
        res = post_pedido(self, self._data())
        self.assertEqual(res.status_code, 200)
        order = self._last()
        code = order.distri_receipt_code
        self.assertRegex(code, r"^\d{4}$")
        self.assertTrue(order._distri_check_receipt_code(code))
        # the confirmation
        self.assertIn('id="codigo-recibido"', res.text)
        self.assertIn(">%s<" % code, res.text)
        # his order page
        page = self.url_open("/pedido/%s" % order.access_token)
        self.assertEqual(page.status_code, 200)
        self.assertIn('id="codigo-recibido"', page.text)
        self.assertIn(">%s<" % code, page.text)
        # packing (distri_terminar) keeps the same code
        self.assertEqual(order._distri_ensure_receipt_code(), code)
        self.assertEqual(order.distri_receipt_code, code)
        # cancelled: not shown any more
        order._action_cancel()
        self.assertNotIn('id="codigo-recibido"', self.url_open("/pedido/%s" % order.access_token).text)

    def test_staff_order_code_on_the_order_link(self):
        self.authenticate("vendedor_codigo_v99", "vendedor_codigo_v99")
        res = post_pedido(self, self._data(cliente_id=str(self.cliente.id)))
        self.assertEqual(res.status_code, 200)
        order = self._last()
        self.assertEqual(order.partner_id, self.cliente)
        code = order.distri_receipt_code
        self.assertRegex(code, r"^\d{4}$")
        # the seller's confirmation does not show it, it says where it is
        self.assertNotIn('id="codigo-recibido"', res.text)
        self.assertIn("ve su código de", res.text)
        # the link sent to the customer shows it
        self.authenticate(None, None)
        page = self.url_open("/pedido/%s" % order.access_token)
        self.assertIn(">%s<" % code, page.text)


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_codigo")
class TestCodigoIntentos(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.partner = cls.partner_a
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_CODE_ATTEMPTS, "3")
        cls.vendedor = new_test_user(cls.env, login="vendedor_intentos_v99", name="Vendedor Intentos",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")

    def setUp(self):
        super().setUp()
        self.order = self._order(self.partner, self.product_a)
        self.code = self.order._distri_generate_receipt_code()

    def test_wrong_codes_lock_and_administracion_unlocks(self):
        order = self.order
        for n in range(2):
            self.assertFalse(order._distri_check_receipt_code(_wrong(self.code)))
        self.assertEqual(order.distri_receipt_attempts, 2)
        self.assertFalse(order.distri_receipt_locked)
        self.assertFalse(order._distri_check_receipt_code(_wrong(self.code)))
        self.assertTrue(order.distri_receipt_locked)
        self.assertIn("se bloqueó", order.message_ids[0].body)
        # locked: not even the right code
        self.assertFalse(order._distri_check_receipt_code(self.code))
        self.assertEqual(order.distri_receipt_attempts, 3, "a locked code counts no more")
        # the phone gets no hash to try against
        stop = self._route(order)._app_payload()["stops"][0]
        self.assertTrue(stop["code_locked"])
        self.assertEqual((stop["code_hash"], stop["code_salt"]), ("", ""))
        self.assertEqual(stop["code_tries_left"], 0)
        with self.assertRaises(UserError):
            order.with_user(self.vendedor).action_distri_unlock_receipt_code()
        order.action_distri_unlock_receipt_code()
        self.assertFalse(order.distri_receipt_locked)
        self.assertTrue(order._distri_check_receipt_code(self.code))
        # a new code starts the counter again
        order._distri_check_receipt_code(_wrong(self.code))
        order._distri_generate_receipt_code()
        self.assertEqual(order.distri_receipt_attempts, 0)

    def test_phone_tries_count_once_per_visit(self):
        route = self._route(self.order)
        stop = route.stop_ids
        payload = route._app_payload()["stops"][0]
        self.assertEqual(payload["code_tries_left"], 3)
        self.assertFalse(payload["code_locked"])
        visit = self._visit(stop, code=self.code, without_code=False, cash=1000, code_attempts=2)
        res = self._sync([self._event(route, "visit", visit)])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.delivery_state, "delivered_code")
        self.assertEqual(self.order.distri_receipt_attempts, 2)
        self.assertEqual(stop.code_attempts, 2)
        # an edit of the same visit sends the same running count: not added again
        edit = dict(visit, comment="editada")
        self._sync([self._event(route, "visit", edit, captured="2026-09-26T15:05:00Z")])
        self.assertEqual(self.order.distri_receipt_attempts, 2)
        self.assertEqual(route._app_payload()["stops"][0]["code_attempts_recorded"], 2)

    def test_phone_reaching_the_limit_locks(self):
        route = self._route(self.order)
        stop = route.stop_ids
        visit = self._visit(stop, without_code=True, no_payment=True, code_attempts=3)
        self._sync([self._event(route, "visit", visit)])
        self.assertEqual(stop.delivery_state, "delivered_no_code")
        self.assertTrue(self.order.distri_receipt_locked)

    def test_whatsapp_and_form_show_the_same_code(self):
        self.assertEqual(self.order.distri_receipt_code, self.code)
        self.assertNotIn(self.code, (self.order.distri_receipt_hash, self.order.distri_receipt_salt))
        self.assertTrue(re.fullmatch(r"\d{4}", self.order._distri_ensure_receipt_code()))
        self.assertEqual(self.order._distri_ensure_receipt_code(), self.code)
