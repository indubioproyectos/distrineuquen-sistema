from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.bloqueo_factura import REABRIR_OK
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid


@tagged("post_install", "-at_install", "distri_devolucion")
class TestDevolucion(RepartoMixin, AccountTestInvoicingCommon):
    """A received return goes back to stock and is never invoiced again."""

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order", "is_storable": True})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.partner = cls.partner_a
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Devolucion test", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})
        numerar_remitos(cls.env)

    def _packed(self):
        """Packed by the packer's steps: remito R-… validated, invoice of 1200 posted."""
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": 10,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": 5,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ],
        })
        order.action_confirm()
        self.assertEqual(order.distri_tomar(), "")
        self.assertEqual(order.distri_terminar(2), "")
        self.assertEqual(order.distri_factura_auto_id.state, "posted")
        self.assertEqual(order.invoice_status, "invoiced")
        return order

    def _line(self, order, product):
        return order.order_line.filtered(lambda l: l.product_id == product)

    def _assert_returned(self, order, ret, qty):
        line = ret.order_line_id
        self.assertTrue(ret.processed)
        picking = ret.return_picking_id
        self.assertEqual(picking.state, "done")
        self.assertTrue(picking.name.startswith("DEV-"), picking.name)
        self.assertEqual(picking.picking_type_code, "incoming")
        self.assertEqual(picking.move_ids.quantity, qty)
        self.assertEqual(picking.location_dest_id,
                         picking.picking_type_id.warehouse_id.lot_stock_id)
        self.assertEqual(line.qty_delivered, line.product_uom_qty - qty)
        self.assertEqual(line.distri_qty_returned, qty)
        self.assertEqual(line.qty_to_invoice, 0)
        self.assertEqual(order.invoice_status, "invoiced")
        # the customer still sees only the remito
        remitos = order._distri_remitos()
        self.assertEqual(len(remitos), 1)
        self.assertTrue(remitos.name.startswith("R-"))
        self.assertNotIn(picking, remitos)
        # "Crear factura" has nothing to invoice
        moves_before = self.env["account.move"].search_count([])
        with self.assertRaises(UserError):
            order._create_invoices()
        self.assertEqual(self.env["account.move"].search_count([]), moves_before)

    def test_route_return_goes_back_to_stock(self):
        order = self._packed()
        line = self._line(order, self.product_a)
        stock = self.product_a.qty_available
        route = self._route(order)
        stop = route.stop_ids
        self._sync([
            self._event(route, "visit", self._visit(stop, returns_answer="yes", returns=[
                {"uuid": _uuid(), "line_id": line.id, "qty": 2, "reason": "Vencido"}],
                cash=1000)),
            self._event(route, "close", {}),
        ])
        ret = stop.return_ids
        ret.action_received()
        # received but not settled yet: nothing changes
        self.assertFalse(ret.return_picking_id)
        route.cash_counted = 1000
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()

        self.assertEqual(ret.credit_note_id.amount_total, 200)
        self._assert_returned(order, ret, 2)
        self.assertEqual(self.product_a.qty_available, stock + 2)
        # the credit note is posted at settlement: still nothing to invoice
        self.assertEqual(ret.credit_note_id.state, "posted")
        self.assertEqual(order.invoice_status, "invoiced")
        self.assertEqual(line.qty_to_invoice, 0)

    def test_depot_return_goes_back_to_stock(self):
        order = self._packed()
        line = self._line(order, self.product_b)
        action = order.action_distri_depot_delivery()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({
            "returns_answer": "yes", "code_option": "without_code",
            "returns_received": True, "cash": 1160})
        self.env["distri.depot.delivery.return"].create({
            "wizard_id": wizard.id, "order_line_id": line.id, "quantity": 1, "reason": "Roto"})
        wizard.action_confirm()
        ret = order.distri_stop_ids.return_ids
        self.assertEqual(ret.credit_note_id.amount_total, 40)
        self._assert_returned(order, ret, 1)
        # once back in stock it cannot be flipped to "No volvió"
        with self.assertRaises(UserError):
            ret.action_missing()
        # closing the depot cash does not apply it twice
        route = ret.route_id
        route.cash_counted = 1160
        route._close_depot_box()
        self.assertEqual(len(order.picking_ids.filtered(
            lambda p: p.picking_type_code == "incoming")), 1)
        self.assertEqual(len(order.invoice_ids.filtered(lambda m: m.move_type == "out_refund")), 1)

    def test_invoicing_returned_units_is_refused(self):
        """Safety net: even if the quantity to invoice were forced back."""
        order = self._packed()
        line = self._line(order, self.product_a)
        action = order.action_distri_depot_delivery()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({
            "returns_answer": "yes", "code_option": "without_code",
            "returns_received": True, "cash": 1000})
        self.env["distri.depot.delivery.return"].create({
            "wizard_id": wizard.id, "order_line_id": line.id, "quantity": 2, "reason": "Vencido"})
        wizard.action_confirm()
        line.qty_to_invoice = 2
        with self.assertRaises(UserError) as error:
            order._create_invoices()
        self.assertIn("devolvió", str(error.exception))

    def test_return_without_invoice_is_left_out_of_the_invoice(self):
        """Delivered without an invoice yet: the invoice skips the returned units."""
        order = self._packed()
        # the old manual cancel (129.49 hotfix: only past REABRIR_OK)
        factura = order.distri_factura_auto_id.with_context(**{REABRIR_OK: True})
        factura.button_draft()
        factura.button_cancel()
        line = self._line(order, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self._sync([
            self._event(route, "visit", self._visit(stop, returns_answer="yes", returns=[
                {"uuid": _uuid(), "line_id": line.id, "qty": 3, "reason": "No lo pidió"}],
                no_payment=True)),
            self._event(route, "close", {}),
        ])
        stop.return_ids.action_received()
        route.cash_counted = 0
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()
        self.assertFalse(stop.return_ids.credit_note_id)
        self.assertEqual(line.qty_to_invoice, 7)
        invoice = order._create_invoices()
        self.assertEqual(invoice.invoice_line_ids.filtered(
            lambda l: l.product_id == self.product_a).quantity, 7)
        self.assertEqual(order.invoice_status, "invoiced")

    # ------------------------------------------- reported returns count at once

    def _reported_return(self, qty=2, cash=0):
        """Packed (1200), delivered with a return of product_a (100 each)."""
        order = self._packed()
        line = self._line(order, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        visit = dict(returns_answer="yes", returns=[
            {"uuid": _uuid(), "line_id": line.id, "qty": qty, "reason": "Vencido"}])
        if cash:
            visit["cash"] = cash
        else:
            visit["no_payment"] = True
        self._sync([self._event(route, "visit", self._visit(stop, **visit)),
                    self._event(route, "close", {})])
        return order, route, stop.return_ids

    def _statement_rows(self):
        return self.partner._distri_statement()["rows"]

    def test_reported_return_shows_right_after_sync(self):
        order, route, ret = self._reported_return(qty=2)
        self.assertTrue(ret.provisional)
        self.assertFalse(ret.credit_note_id)
        # the customer's balance already subtracts it
        self.assertEqual(self.partner._distri_account_balance(), 1000)
        rows = self._statement_rows()
        provisional = [r for r in rows if r["provisional"]]
        self.assertEqual(len(provisional), 1)
        self.assertEqual(provisional[0]["credit"], 200)
        self.assertEqual(provisional[0]["customer_type"], "Devolución informada")
        self.assertIn("a controlar", provisional[0]["customer_ref"])
        self.assertNotIn(order.distri_factura_auto_id.name, provisional[0]["customer_ref"])
        self.assertEqual(rows[-1]["balance"], 1000)
        # «Cómo pagar» of the order drops too
        self.assertEqual(order._distri_importe_a_pagar(), 1000)

    def test_settled_received_return_posts_the_credit_note(self):
        order, route, ret = self._reported_return(qty=2)
        balance = self.partner._distri_account_balance()
        ret.action_received()
        self.assertEqual(self.partner._distri_account_balance(), balance)
        route.cash_counted = 0
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()

        refund = ret.credit_note_id
        self.assertEqual(refund.state, "posted")
        self.assertEqual(refund.amount_total, 200)
        self.assertEqual(order.distri_factura_auto_id.amount_residual, 1000)
        self.assertFalse(ret.provisional)
        self._assert_returned(order, ret, 2)
        # the provisional line is replaced by the credit note, same balance
        self.assertEqual(self.partner._distri_account_balance(), balance)
        rows = self._statement_rows()
        self.assertFalse([r for r in rows if r["provisional"]])
        refunds = [r for r in rows if r["number"] == refund.name]
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["credit"], 200)
        self.assertEqual(refunds[0]["customer_type"], "Devolución")
        self.assertEqual(rows[-1]["balance"], balance)

    def test_settled_missing_return_keeps_the_credit_and_opens_a_case(self):
        order, route, ret = self._reported_return(qty=3)
        balance = self.partner._distri_account_balance()
        self.assertEqual(balance, 900)
        stock = self.product_a.qty_available
        ret.action_missing()
        case = ret.case_id
        self.assertEqual(case.kind, "return")
        self.assertEqual(case.amount, -300)
        route.cash_counted = 0
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()

        refund = ret.credit_note_id
        self.assertEqual(refund.state, "posted")
        self.assertEqual(refund.amount_total, 300)
        self.assertTrue(ret.processed)
        self.assertFalse(ret.return_picking_id, "the goods never came back")
        self.assertEqual(self.product_a.qty_available, stock)
        self.assertEqual(case.state, "open")
        self.assertEqual(self.partner._distri_account_balance(), balance)
        self.assertFalse([r for r in self._statement_rows() if r["provisional"]])
        # the credited units are not invoiced again
        line = self._line(order, self.product_a)
        self.assertEqual(line.qty_to_invoice, 0)
        self.assertEqual(order.invoice_status, "invoiced")

    def test_return_never_counts_twice(self):
        order, route, ret = self._reported_return(qty=2, cash=1000)
        ret.action_received()
        route.cash_counted = 1000
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()
        # a second settlement pass changes nothing
        route._credit_received_returns()
        self.assertEqual(len(order.invoice_ids.filtered(lambda m: m.move_type == "out_refund")), 1)
        self.assertEqual(self.partner._distri_account_balance(), 0)
        data = self.partner._distri_statement()
        self.assertEqual(data["balance"], 0)
        self.assertEqual(data["provisional"], 0)
        self.assertEqual(sum(r["credit"] for r in data["rows"]), 1200)
        self.assertEqual(order.distri_factura_auto_id.amount_residual, 0)
