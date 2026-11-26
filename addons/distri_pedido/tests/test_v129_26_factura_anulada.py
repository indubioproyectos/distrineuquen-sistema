"""19.0.1.129.26 (#97, part 1): cancelling the invoice of a packed or
delivered order warns before and leaves a note and a to-do after
(models/factura_anulada.py). The cancel is never blocked, the order is never
unpacked, and nothing new creates, cancels or reconciles a payment, an
invoice or a journal entry.
"""
from odoo import Command
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models import factura_anulada
from odoo.addons.distri_pedido.models.bloqueo_factura import REABRIR_OK
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_v129_26")
class TestFacturaAnulada(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin = new_test_user(
            cls.env, login="admin_v129_26", name="Administración V129 26",
            groups="base.group_user,distri_pedido.grupo_administracion,"
                   "account.group_account_invoice,sales_team.group_sale_salesman_all_leads",
            **company)
        cls.armador = new_test_user(
            cls.env, login="armador_v129_26", name="Armador V129 26",
            groups="base.group_user,distri_pedido.grupo_armador", **company)
        cls._setup_reparto()
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Factura anulada V129 26", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})

    def _order(self):
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": 4,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
            ],
        })
        order.action_confirm()
        return order

    def _packed(self):
        """Packed and invoiced by the end of packing: posted invoice, done
        depot delivery."""
        order = self._order()
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        self.assertEqual(pedido.distri_terminar(1), "")
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.invoice_ids.state, "posted")
        return order

    def _money_counts(self):
        return {model: self.env[model].sudo().search_count([]) for model in (
            "account.move", "account.payment", "account.partial.reconcile",
            "account.full.reconcile")}

    def _notes(self, order):
        return order.message_ids.filtered(lambda m: "se anuló" in (m.body or ""))

    def _activities(self, order):
        return order.activity_ids.filtered(
            lambda a: a.summary == factura_anulada.ACTIVITY_SUMMARY)

    def test_cancel_of_a_packed_order_invoice_leaves_note_and_todo(self):
        order = self._packed()
        invoice = order.invoice_ids
        pickings_done = order.picking_ids.filtered(lambda p: p.state == "done")
        self.assertTrue(pickings_done)
        self.assertTrue(invoice.distri_cancel_warning)
        before = self._money_counts()

        # 129.49 hotfix: staff can no longer cancel an invoice of the
        # circuit; the note still follows any cancel that is let through
        invoice.with_user(self.admin).with_context(**{REABRIR_OK: True}).button_cancel()

        # the cancel goes through and nothing is undone around it
        self.assertEqual(invoice.state, "cancel")
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.distri_armado, "armado")
        self.assertTrue(all(p.state == "done" for p in pickings_done))
        # no new move, payment or reconciliation
        self.assertEqual(self._money_counts(), before)

        notes = self._notes(order)
        self.assertEqual(len(notes), 1)
        self.assertIn(factura_anulada.NOTE % invoice.name, notes.body.replace("&amp;", "&"))
        todo = self._activities(order)
        self.assertIn(self.admin, todo.user_id)
        self.assertEqual(todo.activity_type_id, self.env.ref("mail.mail_activity_data_todo"))
        self.assertFalse(invoice.distri_cancel_warning, "a cancelled invoice warns no more")

    def test_notify_creates_no_money_and_no_duplicate_todo(self):
        order = self._packed()
        invoice = order.invoice_ids
        before = self._money_counts()
        order._distri_notify_cancelled_invoice(invoice.name)
        order._distri_notify_cancelled_invoice(invoice.name)
        # receivable untouched: the invoice is still posted and unchanged
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_residual, invoice.amount_total)
        self.assertEqual(self._money_counts(), before)
        todo = self._activities(order)
        self.assertEqual(len(todo), len(todo.user_id), "one to-do per person")
        self.assertEqual(len(self._notes(order)), 2)

    def test_not_packed_order_has_no_warning_nor_note(self):
        order = self._order()
        self.assertEqual(order.distri_armado, "pendiente")
        invoice = order.with_context(distri_facturar_sin_armar=True)._create_invoices()
        invoice.action_post()
        self.assertFalse(invoice.distri_cancel_warning)
        invoice.button_cancel()
        self.assertEqual(invoice.state, "cancel")
        self.assertFalse(self._notes(order))
        self.assertFalse(self._activities(order))

    def test_delivered_on_a_route_counts_as_delivered(self):
        order = self._packed()
        order.sudo().write({"distri_armado": "pendiente"})
        # still delivered: its depot delivery (remito) is done
        self.assertTrue(order._distri_packed_or_delivered())
        self.assertTrue(order.invoice_ids.distri_cancel_warning)

    def test_reset_to_draft_then_cancel_also_notifies(self):
        order = self._packed()
        # 129.49 hotfix: only a flow passing REABRIR_OK resets it
        invoice = order.invoice_ids.with_context(**{REABRIR_OK: True})
        invoice.button_draft()
        self.assertTrue(invoice.distri_cancel_warning)
        invoice.button_cancel()
        self.assertEqual(len(self._notes(order)), 1)

    def test_rearmar_gets_no_note(self):
        order = self._packed()
        order.with_user(self.admin).action_distri_rearmar()
        self.assertEqual(order.state, "cancel")
        self.assertFalse(self._notes(order))
        self.assertFalse(self._activities(order))

    def test_form_asks_before_cancelling(self):
        arch = self.env["account.move"].get_view(
            self.env.ref("account.view_move_form").id, "form")["arch"]
        self.assertIn(factura_anulada.CONFIRM, arch)
        self.assertIn("distri_cancel_warning", arch)
