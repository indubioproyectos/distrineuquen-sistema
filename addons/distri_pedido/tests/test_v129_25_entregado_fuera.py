"""19.0.1.129.25: «Ya lo armé y ya se entregó» in «Facturados sin armar» of
/armado (models/factura_sin_armar.py).

An invoiced order packed and handed to the customer outside the system is
packed and flagged delivered: it leaves route planning, «Asignar pedidos»,
/despacho and the depot delivery. Owner's rule: no payment, invoice or
journal entry is created, cancelled or reconciled; no pending collection,
no charge; the customer's balance stays the same. Synthetic data only.
"""
import re

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers.deposito import deliverable_domain
from odoo.addons.distri_pedido.tests.test_v117_ya_armado import YaArmadoMixin


@tagged("post_install", "-at_install", "distri_v129_25")
class TestV12925EntregadoFuera(YaArmadoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls._setup_ya_armado()
        cls.mp_journal = cls.company_data["default_journal_bank"]

    def _ledger(self):
        self.env.flush_all()
        self.partner_a.invalidate_recordset(["credit"])
        return (self.env["distri.promised.transfer"]._distri_ledger_fingerprint(),
                self.partner_a.commercial_partner_id.credit)

    def _pay(self, order):
        invoice = self._invoices(order)
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.mp_journal.id, "payment_date": fields.Date.today(),
            }).action_create_payments()
        self.assertFalse(invoice.amount_residual)

    def _check_delivered_outside(self, order):
        invoices = self._invoices(order)
        residual = sum(invoices.mapped("amount_residual"))
        before = self._ledger()
        self.assertEqual(order.with_user(self.armador).distri_ya_armado(3, entregado=True), "")
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.distri_bultos, 3)
        self.assertTrue(order.distri_entregado_fuera)
        self.assertEqual(order.distri_entregado_fuera_por_id, self.armador)
        self.assertTrue(order.distri_entregado_fuera_el)
        self.assertEqual(order.distri_delivery_state, "delivered")
        self.assertEqual(self._invoices(order), invoices, "no second invoice")
        self.assertEqual(sum(invoices.mapped("amount_residual")), residual)
        self.assertFalse(self.env["distri.promised.transfer"].sudo().search(
            [("order_id", "=", order.id)]), "no pending collection nor receipt")
        self.assertFalse(self.env["distri.route.case"].sudo().search(
            [("stop_id.order_id", "=", order.id)]), "no charge")
        self.assertFalse(order.distri_stop_ids, "no route, no depot box")
        body = order.message_ids.filtered(
            lambda m: "entregado fuera del sistema" in (m.body or ""))[:1].body
        self.assertIn("Marcado como armado y entregado fuera del sistema por Armador V117", body)
        self.assertIn("en 3 bultos (ya estaba facturado: %s)" % invoices.name, body)
        self.assertEqual(self._ledger(), before, "the customer's balance did not move")

    def test_paid_order(self):
        order = self._order()
        self._pay(order)
        self._check_delivered_outside(order)

    def test_unpaid_order(self):
        self._check_delivered_outside(self._order())

    def test_left_every_delivery_queue(self):
        plain = self._order()
        outside = self._order()
        self.assertEqual(plain.with_user(self.armador).distri_ya_armado(2), "")
        self.assertEqual(outside.with_user(self.armador).distri_ya_armado(2, entregado=True), "")
        Order = self.env["sale.order"]
        Route = self.env["distri.route"]
        unrouted = Order.search(Route._planning_free_domain())
        self.assertIn(plain, unrouted, "«Ya lo armé» alone still goes to planning")
        self.assertNotIn(outside, unrouted, "not in /rutas «Sin ruta · depósito»")
        depot = Order.search(deliverable_domain())
        self.assertIn(plain, depot)
        self.assertNotIn(outside, depot, "not in the depot delivery list")
        with self.assertRaisesRegex(UserError, "ya se entregó"):
            outside._distri_check_depot_delivery()
        route = self._route(self.env["sale.order"], on_road=False)
        self.assertFalse(route._order_is_free(outside), "never on a route again")
        self.assertTrue(route._order_is_free(plain))

    def test_double_tap(self):
        order = self._order()
        self.assertEqual(order.with_user(self.armador).distri_ya_armado(2, entregado=True), "")
        before = self._ledger()
        self.assertEqual(order.with_user(self.armador).distri_ya_armado(2, entregado=True), "")
        self.assertEqual(len(order.message_ids.filtered(
            lambda m: "entregado fuera del sistema" in (m.body or ""))), 1)
        self.assertEqual(order.with_user(self.admin_user).distri_ya_armado(2, entregado=True),
                         "Este pedido ya está armado.")
        self.assertEqual(self._ledger(), before)

    def test_wrong_group(self):
        order = self._order()
        with self.assertRaises(AccessError):
            order.with_user(self.seller).distri_ya_armado(2, entregado=True)
        self.assertFalse(order.distri_entregado_fuera)
        self.assertEqual(order.distri_armado, "pendiente")


@tagged("post_install", "-at_install", "distri_v129_25")
class TestV12925EntregadoFueraForm(YaArmadoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_ya_armado()

    def test_form_posts_with_its_token(self):
        order = self._order()
        url = "/armado/facturado/%s/ya-armado" % order.id
        self.authenticate("armador_v117", "armador_v117")
        page = self.url_open("/armado")
        self.assertEqual(page.status_code, 200)
        form = re.search(r'<form[^>]*action="%s"[^>]*>(.*?)</form>' % re.escape(url),
                         page.text, re.S).group(1)
        self.assertIn('name="entregado"', form)
        self.assertIn("Ya lo armé y ya se entregó al cliente", form)
        token = re.search(r'name="csrf_token" value="([^"]+)"', form).group(1)
        res = self.url_open(url, data={"csrf_token": token, "bultos": "2", "entregado": "1"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(order.distri_armado, "armado")
        self.assertTrue(order.distri_entregado_fuera)
        # a seller is refused before anything
        self.authenticate("vendedor_v117", "vendedor_v117")
        other = self._order()
        res = self.url_open("/armado/facturado/%s/ya-armado" % other.id,
                            data={"csrf_token": token, "bultos": "2", "entregado": "1"})
        self.assertNotEqual(res.status_code, 200)
        self.assertFalse(other.distri_entregado_fuera)
