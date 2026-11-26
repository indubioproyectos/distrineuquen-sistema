"""19.0.1.117.0: «Facturados sin armar» in /armado (models/factura_sin_armar.py).

An order invoiced by hand before it was packed (real case S00239) is listed
apart; «Ya lo armé» marks it packed with its bultos, validates its depot
delivery as ordered, keeps its single invoice and makes it a free order of
the route planning.
"""
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.bloqueo_factura import REABRIR_OK
from odoo.addons.distri_pedido.models.factura_armado import RESUMEN_ACTIVIDAD
from odoo.addons.distri_pedido.models.factura_sin_armar import DIAS_FACTURADOS_SIN_ARMAR
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


class YaArmadoMixin(RepartoMixin):

    @classmethod
    def _setup_ya_armado(cls):
        if not hasattr(cls, "product_a"):  # HttpCase: no accounting fixtures
            Product = cls.env["product.product"]
            cls.product_a = Product.create({"name": "Producto A V117", "type": "consu",
                                            "list_price": 100.0})
            cls.product_b = Product.create({"name": "Producto B V117", "type": "consu",
                                            "list_price": 40.0})
            cls.partner_a = cls.env["res.partner"].create({"name": "Cliente V117"})
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        company = cls.env.company
        cls.armador = new_test_user(
            cls.env, login="armador_v117", name="Armador V117",
            groups="base.group_user,distri_pedido.grupo_armador",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.admin_user = new_test_user(
            cls.env, login="administracion_v117", name="Denis V117",
            groups="base.group_user,distri_pedido.grupo_administracion,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.seller = new_test_user(
            cls.env, login="vendedor_v117", name="Vendedor V117",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Ya armado V117", "company_id": company.id,
            "currency_id": company.currency_id.id})

    def _order(self, invoiced=True):
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": 10,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": 5,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ],
        })
        order.action_confirm()
        if invoiced:
            # by hand, before packing («Facturar igual»)
            invoice = order.with_context(distri_facturar_sin_armar=True)._create_invoices()
            invoice.action_post()
        return order

    def _listed(self, user=None):
        return self.env["sale.order"].with_user(user or self.armador)._distri_facturados_sin_armar()

    def _invoices(self, order):
        return order.invoice_ids.filtered(lambda f: f.move_type == "out_invoice")


@tagged("post_install", "-at_install", "distri_v117")
class TestV117YaArmado(YaArmadoMixin, AccountTestInvoicingCommon):

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

    def test_1_listed_only_when_invoiced_and_never_packed(self):
        invoiced = self._order()
        plain = self._order(invoiced=False)
        held = self._order()
        held.sudo().distri_retenido = True
        waiting = self._order()
        waiting.sudo().distri_wa_state = "waiting"
        old = self._order()
        old.sudo().date_order = fields.Datetime.now() - timedelta(
            days=DIAS_FACTURADOS_SIN_ARMAR + 1)
        cancelled_invoice = self._order()
        # the old manual cancel (129.49 hotfix: only past REABRIR_OK)
        self._invoices(cancelled_invoice).with_context(**{REABRIR_OK: True}).button_draft()
        self._invoices(cancelled_invoice).with_context(**{REABRIR_OK: True}).button_cancel()
        listed = self._listed()
        self.assertIn(invoiced, listed)
        for order in (plain, held, waiting, old, cancelled_invoice):
            self.assertNotIn(order, listed, order.name)
        # the normal list hides it (it has an invoice): that is the problem
        self.assertFalse(invoiced._distri_para_armar())
        # once packed it leaves the block
        self.assertEqual(invoiced.with_user(self.armador).distri_ya_armado(2), "")
        self.assertNotIn(invoiced, self._listed())

    def test_2_packs_delivers_one_invoice_and_goes_to_planning(self):
        order = self._order()
        invoice = self._invoices(order)
        self.assertEqual(len(invoice), 1)
        Route = self.env["distri.route"]
        free = Route._planning_free_domain()
        self.assertFalse(self.env["sale.order"].search(free + [("id", "=", order.id)]))
        pickings = order.picking_ids
        self.assertTrue(pickings)
        self.assertEqual(order.with_user(self.armador).distri_ya_armado(3), "")
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.distri_bultos, 3)
        self.assertEqual(order.distri_armador_id, self.armador)
        self.assertTrue(order.distri_armado_inicio)
        self.assertTrue(order.distri_armado_fin)
        self.assertEqual(set(order.picking_ids.mapped("state")), {"done"})
        for line in order.order_line:
            self.assertEqual(line.qty_delivered, line.product_uom_qty, line.product_id.name)
        self.assertEqual(self._invoices(order), invoice)
        self.assertFalse(order.distri_factura_auto_id)
        self.assertFalse(order.distri_factura_error)
        self.assertFalse(order.activity_ids.filtered(lambda a: a.summary == RESUMEN_ACTIVIDAD))
        self.assertTrue(any(
            "Marcado como armado sin pasar por el armado (ya estaba facturado: %s) por Armador "
            "V117" % invoice.name in (m.body or "") for m in order.message_ids))
        self.assertTrue(self.env["sale.order"].search(free + [("id", "=", order.id)]))
        # the same rule as «Asignar pedidos armados de las zonas»
        rule = Route.new({"company_id": order.company_id.id})._free_orders_domain()
        self.assertTrue(self.env["sale.order"].search(rule + [("id", "=", order.id)]))

    def test_3_administracion_too_seller_refused(self):
        order = self._order()
        with self.assertRaises(AccessError):
            order.with_user(self.seller).distri_ya_armado(1)
        self.assertEqual(order.distri_armado, "pendiente")
        self.assertIn(order, self._listed(self.admin_user))
        self.assertEqual(order.with_user(self.admin_user).distri_ya_armado(1), "")
        self.assertEqual(order.distri_armador_id, self.admin_user)

    def test_4_double_tap_and_bultos(self):
        order = self._order()
        packer = order.with_user(self.armador)
        for wrong in (0, -1, 1000, None):
            self.assertEqual(packer.distri_ya_armado(wrong),
                             "Poné en cuántos bultos quedó el pedido.")
        self.assertEqual(order.distri_armado, "pendiente")
        self.assertEqual(packer.distri_ya_armado(2), "")
        moves = order.picking_ids.move_ids
        done = moves.filtered(lambda m: m.state == "done")
        # the second tap: nothing changes
        self.assertEqual(packer.distri_ya_armado(5), "")
        self.assertEqual(order.distri_bultos, 2)
        self.assertEqual(order.picking_ids.move_ids, moves)
        self.assertEqual(order.picking_ids.move_ids.filtered(lambda m: m.state == "done"), done)
        self.assertFalse(order.picking_ids.filtered(lambda p: p.backorder_id))
        self.assertEqual(len(self._invoices(order)), 1)
        # somebody else's tap after it: told, not redone
        self.assertEqual(order.with_user(self.admin_user).distri_ya_armado(2),
                         "Este pedido ya está armado.")
        # an order not in the block is refused
        plain = self._order(invoiced=False)
        self.assertEqual(plain.with_user(self.armador).distri_ya_armado(2),
                         "Este pedido ya no está entre los facturados sin armar.")
        self.assertEqual(plain.distri_armado, "pendiente")


@tagged("post_install", "-at_install", "distri_v117")
class TestV117YaArmadoHttp(YaArmadoMixin, HttpCase):
    """/armado: the block, «Ya lo armé» and who may use it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_ya_armado()

    def _token(self, page):
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def test_block_and_action(self):
        order = self._order()
        url = "/armado/facturado/%s/ya-armado" % order.id
        self.authenticate("administracion_v117", "administracion_v117")
        page = self.url_open("/armado")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Facturados sin armar", page.text)
        self.assertIn(url, page.text)
        self.assertIn(self._invoices(order).name, page.text)
        token = self._token(page)
        res = self.url_open(url, data={"csrf_token": token, "bultos": "0"})
        self.assertIn("Poné en cuántos bultos quedó el pedido.", res.text)
        self.assertEqual(order.distri_armado, "pendiente")
        res = self.url_open(url, data={"csrf_token": token, "bultos": "4"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.distri_bultos, 4)
        self.assertIn("quedó armado en", res.text)
        self.assertNotIn(url, res.text)
        # the seller: no /armado, no action
        self.authenticate("vendedor_v117", "vendedor_v117")
        other = self._order()
        res = self.url_open("/armado/facturado/%s/ya-armado" % other.id,
                            data={"csrf_token": token, "bultos": "1"})
        self.assertIn(res.status_code, (400, 403))
        self.assertEqual(other.distri_armado, "pendiente")

    def test_draft_invoice_does_not_break_armado(self):
        """19.0.1.129.21: a draft invoice (no number yet) listed the order
        and its name False took the whole page down (TypeError on join)."""
        order = self._order(invoiced=False)
        order.with_context(distri_facturar_sin_armar=True)._create_invoices()  # left in draft
        self.assertEqual(order._distri_factura_viva_texto(), "borrador")
        self.authenticate("administracion_v117", "administracion_v117")
        page = self.url_open("/armado")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Factura borrador", page.text)
