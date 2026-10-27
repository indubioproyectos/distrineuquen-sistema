"""19.0.1.39.0: «Revisión por cliente» and «Venta sin registrar»
(models/revision_cliente.py). Every customer, amount and operation here is
fictitious."""
from datetime import date, datetime, timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install", "distri_revision_cliente")
class TestRevisionCliente(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        Partner = cls.env["res.partner"]
        cls.alpha = Partner.create({"name": "Almacén Revisión Alfa", "company_type": "company"})
        cls.beta = Partner.create({"name": "Kiosco Revisión Beta", "company_type": "company"})
        cls.gamma = Partner.create({"name": "Despensa Revisión Gama", "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Review = cls.env["distri.balance.review"]
        cls.Mov = cls.env["distri.cuenta.movimiento"]
        cls.cash = cls.company_data["default_journal_cash"]
        cls.seller = new_test_user(cls.env, login="vendedor_revision",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        # «Confirmar» needs the Online mirror synced in the last 24 h (19.0.1.41.0)
        cls.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.online_sync_at", fields.Datetime.to_string(fields.Datetime.now()))

    # ------------------------------------------------------------ helpers

    def _history(self, partner, debe=0.0, haber=0.0, tipo="factura", **kw):
        return self.Mov.create(dict({"partner_id": partner.id, "fecha": date(2026, 8, 1),
                                     "comprobante": "0001-%s" % partner.id, "tipo": tipo,
                                     "debe": debe, "haber": haber}, **kw))

    def _cash(self, partner, amount, when):
        pay = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": partner.id,
            "amount": amount, "journal_id": self.cash.id, "date": when,
            "memo": "Planilla de prueba"})
        pay.action_post()
        return pay

    def _card(self, partner, amount, when, source, receipt=True):
        """A pending card; with its WhatsApp receipt by default (19.0.1.41.0:
        without one, a movement never reduces the debt)."""
        card = self.Collection.create({
            "source_id": source, "date": when, "amount": amount, "method_type": "bank_transfer",
            "partner_id": partner.id, "proposal": "manual",
            "proposal_note": "Comprobante de prueba %s" % source})
        if receipt:
            msg = self.env["distri.whatsapp.message"].sudo().create({
                "wa_message_id": "wamid.revision.%s" % source, "direction": "in",
                "wa_id": "5492990000777", "message_type": "image",
                "timestamp": when + timedelta(minutes=1), "media_mime_type": "image/jpeg",
                "media_id": "1", "media_state": "pending"})
            msg.with_context(distri_receipt_matching=True).write({
                "distri_is_receipt": True, "distri_operation": source, "distri_amount": amount,
                "distri_receipt_state": "matched", "distri_collection_id": card.id})
            card.sudo().receipt_id = msg
        return card

    def _review(self, partner):
        self.Review._distri_refresh(partner)
        return self.Review.search([("partner_id", "=", partner.id)])

    def _vsr(self, record, **vals):
        """«Venta sin registrar» like the button: the action, then its wizard."""
        action = record.action_unrecorded_sale()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create(vals)
        wizard.action_apply()
        return wizard

    def _vsr_of(self, payment):
        return self.env["account.move"].search([("distri_vsr_payment_id", "=", payment.id)])

    def _receivable(self, payment):
        return payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")

    def _line(self, review, record):
        field = "collection_id" if record._name == "distri.mp.collection" else "payment_id"
        return review.line_ids.filtered(lambda l: l[field] == record)

    # ------------------------------------------------------------ tests

    def test_balance_follows_the_online_formula(self):
        self._history(self.alpha, debe=10000)
        self._history(self.alpha, haber=4000, tipo="pago", comprobante="REC-1")
        same_money = self._cash(self.alpha, 1000, date(2026, 9, 1))
        # an Online receipt archived because it is the same money as a lab payment
        self._history(self.alpha, haber=1000, tipo="pago", comprobante="REC-2", active=False,
                      lab_payment_id=same_money.id)
        mp = self._card(self.alpha, 2000, datetime(2026, 9, 5, 15, 0), "910000000001")
        mp.action_confirm()
        pending = self._card(self.alpha, 3000, datetime(2026, 9, 10, 15, 0), "910000000002")

        review = self._review(self.alpha)
        movs = self.Mov.with_context(active_test=False).search(
            [("partner_id", "=", self.alpha.id)])
        online = sum(movs.mapped("debe")) - sum(movs.mapped("haber"))
        active = movs.filtered("active")
        posted = self.env["account.payment"].search([
            ("partner_id", "=", self.alpha.id), ("payment_type", "=", "inbound"),
            ("state", "in", ("in_process", "paid"))])
        lab = sum(active.mapped("debe")) - sum(active.mapped("haber")) - sum(
            posted.mapped("amount"))
        self.assertEqual(review.online_balance, online)
        self.assertEqual(review.online_balance, 5000)
        self.assertEqual(review.lab_balance, lab)
        self.assertAlmostEqual(review.lab_balance, self.alpha._distri_account_balance())
        self.assertEqual(review.final_balance, 0)
        self.assertEqual(review.history_receipt_count, 2)
        self.assertEqual(review.history_receipt_amount, 5000)
        self.assertEqual(review.matched_count, 1)
        # the payment already in Online is not a new line
        self.assertFalse(self._line(review, same_money))
        self.assertEqual(review.line_ids.mapped("origin"), ["mp", "card"])
        self.assertEqual(review.line_ids.mapped("balance_after"), [3000, 0])
        self.assertEqual(self._line(review, pending).note, "Comprobante de prueba 910000000002")
        self.assertFalse(review.has_flag)

    def test_red_flag_and_unrecorded_sale_for_the_excess_only(self):
        self._history(self.beta, debe=1000)
        pay = self._cash(self.beta, 1500, date(2026, 9, 1))
        card = self._card(self.beta, 700, datetime(2026, 9, 3, 12, 0), "920000000001")
        review = self._review(self.beta)
        cash_line, card_line = self._line(review, pay), self._line(review, card)
        self.assertTrue(review.has_flag)
        self.assertEqual(review.flag_count, 2)
        self.assertTrue(cash_line.red_flag)
        self.assertEqual(cash_line.excess, 500)
        self.assertEqual(card_line.balance_after, -1200)
        self.assertEqual(card_line.excess, 700)

        sale_journal = self.company_data["default_journal_sale"]
        before = self.init_invoice("out_invoice", partner=self.gamma, amounts=[100],
                                   journal=sale_journal, post=True)
        self._vsr(cash_line)
        invoice = self._vsr_of(pay)
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_total, 500)
        self.assertFalse(invoice.invoice_line_ids.tax_ids)
        self.assertEqual(invoice.invoice_line_ids.product_id,
                         self.env.ref("distri_pedido.product_venta_sin_detalle"))
        self.assertEqual(invoice.journal_id.code, "VSR")
        self.assertTrue(invoice.name.startswith("VSR/2026/"), invoice.name)
        self.assertEqual(invoice.invoice_date, date(2026, 9, 1))
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        pay_line = pay.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        self.assertEqual(pay_line.matched_debit_ids.debit_move_id.move_id, invoice)
        self.assertTrue(invoice.message_ids.filtered(lambda m: "Venta sin registrar" in m.body))
        self.assertTrue(pay.message_ids.filtered(lambda m: invoice.name in m.body))
        # the ordinary sales series goes on as if nothing happened
        after = self.init_invoice("out_invoice", partner=self.gamma, amounts=[100],
                                  journal=sale_journal, post=True)
        self.assertEqual(after.sequence_number, before.sequence_number + 1)
        self.assertEqual(after.sequence_prefix, before.sequence_prefix)

        cash_line.invalidate_recordset()
        self.assertEqual(cash_line.review_state, "unrecorded_sale")
        self.assertFalse(cash_line.red_flag)
        self.assertEqual(cash_line.balance_after, 0)
        self.assertEqual(review.vsr_amount, 500)
        self.assertEqual(review.lab_balance, 0)
        self.assertAlmostEqual(review.lab_balance, self.beta._distri_account_balance())
        self.assertEqual(review.flag_count, 1)

        # idempotent: never a second invoice for the same payment, not even
        # from a wizard opened before the first one was applied
        with self.assertRaises(UserError):
            cash_line.action_unrecorded_sale()
        stale = self.env["distri.unrecorded.sale.wizard"].create(dict(
            cash_line._distri_vsr_defaults(), amount=100))
        with self.assertRaises(UserError):
            stale.action_apply()
        self.assertEqual(self.env["account.move"].search_count(
            [("distri_vsr_payment_id", "=", pay.id)]), 1)

        # «Confirmar» is the board's own confirmation
        card_line.action_confirm_card()
        self.assertEqual(card.state, "confirmed")
        self.assertTrue(card.payment_id)
        card_line = self._line(review, card)
        self.assertEqual(card_line.payment_id, card.payment_id)
        self.assertEqual(card_line.origin, "mp")
        self._vsr(card_line)
        second = self._vsr_of(card.payment_id)
        # the balance was already 0: the whole payment is the excess
        self.assertEqual(second.amount_total, 700)
        self.assertTrue(card.message_ids.filtered(lambda m: second.name in m.body))
        self.assertFalse(review.has_flag)
        self.assertEqual(review.state, "reviewed")

    def test_refuses_when_the_payment_leaves_no_credit(self):
        self._history(self.gamma, debe=5000)
        pay = self._cash(self.gamma, 1000, date(2026, 9, 1))
        review = self._review(self.gamma)
        line = self._line(review, pay)
        self.assertFalse(line.red_flag)
        with self.assertRaises(UserError):
            line.action_unrecorded_sale()
        self.assertFalse(self.env["account.move"].search(
            [("distri_vsr_payment_id", "=", pay.id)]))
        line.action_mark_reviewed()
        self.assertEqual(line.review_state, "reviewed")
        self.assertEqual(line.reviewed_by_id, self.env.user)
        self.assertEqual(review.state, "reviewed")

    def test_only_administration_uses_it(self):
        self._history(self.beta, debe=100)
        pay = self._cash(self.beta, 900, date(2026, 9, 1))
        line = self._line(self._review(self.beta), pay)
        self.assertTrue(line.red_flag)
        with self.assertRaises(AccessError):
            line.with_user(self.seller).action_unrecorded_sale()
        with self.assertRaises(AccessError):
            line.with_user(self.seller).action_mark_reviewed()
        with self.assertRaises(AccessError):
            self.Review.with_user(self.seller).search([])
        with self.assertRaises(AccessError):
            self.Review.with_user(self.seller).action_open_reviews()
        self.assertFalse(self.env["account.move"].search(
            [("distri_vsr_payment_id", "=", pay.id)]))

    def test_resellers_are_left_out(self):
        Partner = self.env["res.partner"]
        hidden = Partner.create({"name": "Revendedor Prueba", "company_type": "company",
                                 "distri_lista_oculta": True})
        dist_list = self.env["product.pricelist"].create({"name": "Distribuidoras prueba",
                                                          "distri_mayorista": True})
        wholesale = Partner.create({"name": "Distribuidora Prueba", "company_type": "company"})
        # like the partner form: the list goes through the computed field
        wholesale.property_product_pricelist = dist_list
        generic = Partner._distri_generic_partner_for(wholesale=True)
        for n, partner in enumerate((hidden, wholesale, generic, self.alpha)):
            self._card(partner, 500, datetime(2026, 9, 1, 10, n), "930000000%03d" % n)
        self.Review._distri_refresh(hidden + wholesale + generic + self.alpha)
        reviewed = self.Review.search([]).mapped("partner_id")
        self.assertIn(self.alpha, reviewed)
        self.assertNotIn(hidden, reviewed)
        self.assertNotIn(wholesale, reviewed)
        self.assertNotIn(generic, reviewed)

    # ------------------------------------------------------------ from the board

    def test_board_pending_card_full_amount_keeps_older_invoices(self):
        older = self.init_invoice("out_invoice", partner=self.gamma, amounts=[1000],
                                  invoice_date=date(2026, 8, 20), post=True)
        card = self.Collection.create({
            "source_id": "940000000001", "date": datetime(2026, 9, 2, 12, 0), "amount": 800,
            "method_type": "bank_transfer"})
        action = card.action_unrecorded_sale()
        self.assertEqual(action["context"]["default_amount"], 800)
        self.assertEqual(action["context"]["default_invoice_date"], card.date_day)
        wizard = self.env["distri.unrecorded.sale.wizard"].with_context(
            action["context"]).create({"partner_id": self.gamma.id, "note": "Venta en el local"})
        self.assertTrue(wizard.card_pending)
        wizard.action_apply()
        # the card was confirmed with the board's logic, for that customer
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.partner_id, self.gamma)
        invoice = self._vsr_of(card.payment_id)
        self.assertEqual(invoice.amount_total, 800)
        # the wizard's default_* context never leaks into the invoice
        self.assertFalse(invoice.line_ids.payment_id)
        self.assertEqual(invoice.invoice_date, card.date_day)
        self.assertEqual(invoice.journal_id.code, "VSR")
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertTrue(self._receivable(card.payment_id).reconciled)
        # the older invoice keeps its debt: the payment went to this sale
        self.assertEqual(older.amount_residual, older.amount_total)
        self.assertIn("Venta en el local", card.message_ids[0].body)
        self.assertTrue(card.payment_id.message_ids.filtered(lambda m: invoice.name in m.body))

    def test_board_confirmed_card_partial_amount(self):
        card = self._card(self.alpha, 1000, datetime(2026, 9, 4, 12, 0), "940000000002")
        card.action_confirm()
        payment = card.payment_id
        with self.assertRaises(UserError):
            self._vsr(card, amount=1500)
        with self.assertRaises(UserError):
            self._vsr(card, amount=0)
        wizard = self._vsr(card, amount=600)
        self.assertEqual(wizard.invoice_date, payment.date)
        invoice = self._vsr_of(payment)
        self.assertEqual(invoice.amount_total, 600)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        # exactly 600 reconciled: 400 of the payment stay open
        self.assertAlmostEqual(self._receivable(payment).amount_residual, -400)
        with self.assertRaises(UserError):
            card.action_unrecorded_sale()
        self.assertEqual(len(self._vsr_of(payment)), 1)

    def test_board_entry_is_for_administration_only(self):
        card = self._card(self.alpha, 500, datetime(2026, 9, 6, 12, 0), "940000000003")
        with self.assertRaises(AccessError):
            card.with_user(self.seller).action_unrecorded_sale()
        with self.assertRaises(AccessError):
            self.env["distri.unrecorded.sale.wizard"].with_user(self.seller).create(
                card._distri_vsr_defaults())
        self.assertEqual(card.state, "draft")
