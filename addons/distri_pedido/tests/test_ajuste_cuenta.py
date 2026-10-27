"""19.0.1.44.0: «Ajuste de cuentas» (models/ajuste_cuenta.py). Every
customer, amount and motive here is fictitious."""
from datetime import date, datetime

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon


@tagged("post_install", "-at_install", "distri_ajuste_cuenta")
class TestAjusteCuenta(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        Partner = cls.env["res.partner"]
        cls.alpha = Partner.create({"name": "Almacén Ajuste Alfa", "company_type": "company",
                                    "ref": "ODOO-990001"})
        cls.beta = Partner.create({"name": "Kiosco Ajuste Beta", "company_type": "company"})
        cls.Review = cls.env["distri.balance.review"]
        cls.Adjustment = cls.env["distri.balance.adjustment"]
        cls.cash = cls.company_data["default_journal_cash"]
        cls.seller = new_test_user(cls.env, login="vendedor_ajuste",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")

    # ------------------------------------------------------------ helpers

    def _history(self, partner, debe=0.0, haber=0.0, tipo="factura"):
        return self.env["distri.cuenta.movimiento"].create({
            "partner_id": partner.id, "fecha": date(2026, 8, 1),
            "comprobante": "0001-%s-%s" % (partner.id, debe), "tipo": tipo,
            "debe": debe, "haber": haber})

    def _cash(self, partner, amount, when):
        pay = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": partner.id,
            "amount": amount, "journal_id": self.cash.id, "date": when,
            "memo": "Planilla de prueba"})
        pay.action_post()
        return pay

    def _review(self, partner):
        self.Review._distri_refresh(partner)
        return self.Review.search([("partner_id", "=", partner.id)])

    def _wizard(self, partner, **vals):
        """«Ajustar saldo» like the button: the action, then its wizard."""
        action = partner.action_distri_balance_adjustment()
        vals.setdefault("motive", "Saldo confirmado por la gerencia")
        vals.setdefault("authorized_by", "Gerente de prueba")
        return self.env[action["res_model"]].with_context(action["context"]).create(vals)

    def _adjust(self, partner, **vals):
        wizard = self._wizard(partner, **vals)
        wizard.action_apply()
        return self.Adjustment.search([("partner_id", "=", partner.id)], limit=1)

    # ------------------------------------------------------------ tests

    def test_credit_to_zero(self):
        self._history(self.alpha, debe=10000)
        self._cash(self.alpha, 2000, date(2026, 9, 1))
        review = self._review(self.alpha)
        self.assertEqual(review.lab_balance, 8000)
        wizard = self._wizard(self.alpha, date=date(2026, 9, 10))
        # the wizard shows the same figure as the review
        self.assertEqual(wizard.current_balance, review.lab_balance)
        self.assertEqual(wizard.target_balance, 0)
        self.assertEqual(wizard.difference, -8000)
        self.assertFalse(wizard.is_debit)
        wizard.action_apply()

        adjustment = self.Adjustment.search([("partner_id", "=", self.alpha.id)])
        self.assertEqual(len(adjustment), 1)
        self.assertEqual(adjustment.balance_before, 8000)
        self.assertEqual(adjustment.target_balance, 0)
        self.assertEqual(adjustment.difference, -8000)
        self.assertEqual(adjustment.kind, "credit")
        self.assertEqual(adjustment.online_ref, "ODOO-990001")
        self.assertEqual(adjustment.authorized_by, "Gerente de prueba")
        self.assertEqual(adjustment.create_uid, self.env.user)
        self.assertFalse(adjustment.online_done)
        move = adjustment.move_id
        self.assertEqual(move.move_type, "out_refund")
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.amount_total, 8000)
        self.assertFalse(move.invoice_line_ids.tax_ids)
        self.assertEqual(move.journal_id.code, "AJC")
        self.assertTrue(move.name.startswith("AJC/2026/"), move.name)
        self.assertEqual(move.invoice_date, date(2026, 9, 10))
        # no money came in: no payment for the customer besides the cash one
        self.assertEqual(self.env["account.payment"].search_count(
            [("partner_id", "=", self.alpha.id)]), 1)

        review.invalidate_recordset()
        self.assertEqual(review.adjustment_amount, -8000)
        self.assertEqual(review.lab_balance, 0)
        self.assertEqual(review.final_balance, 0)
        self.assertAlmostEqual(self.alpha._distri_account_balance(), 0)
        self.assertEqual(self.Review._distri_lab_balance(self.alpha), 0)
        note = self.alpha.message_ids.filtered(lambda m: "Ajuste de cuenta" in (m.body or ""))
        self.assertTrue(note)
        self.assertIn("Saldo confirmado por la gerencia", note[0].body)
        self.assertIn("Gerente de prueba", note[0].body)

        # the statement shows it as an adjustment and its balance follows
        rows = self.alpha._distri_statement_rows()
        row = [r for r in rows if r["res_model"] == "account.move" and r["res_id"] == move.id]
        self.assertEqual(row[0]["doc_type"], "Ajuste de cuenta")
        self.assertEqual(row[0]["customer_type"], "Ajuste de cuenta")
        self.assertNotIn("Gerente", row[0]["concept"])
        self.assertAlmostEqual(self.alpha._distri_statement()["balance"], 0)

        # a payment after the adjustment leaves the customer in credit
        later = self._cash(self.alpha, 500, date(2026, 9, 20))
        review = self._review(self.alpha)
        line = review.line_ids.filtered(lambda l: l.payment_id == later)
        self.assertTrue(line.red_flag)
        self.assertEqual(line.balance_before, 0)
        self.assertEqual(review.lab_balance, -500)

    def test_credit_to_a_positive_target(self):
        self._history(self.alpha, debe=3468615)
        self._cash(self.alpha, 100000, date(2026, 9, 1))
        adjustment = self._adjust(self.alpha, target_balance=3116000)
        self.assertEqual(adjustment.balance_before, 3368615)
        self.assertEqual(adjustment.difference, -252615)
        self.assertEqual(adjustment.move_id.move_type, "out_refund")
        self.assertEqual(adjustment.move_id.amount_total, 252615)
        review = self._review(self.alpha)
        self.assertEqual(review.lab_balance, 3116000)
        self.assertEqual(review.final_balance, 3116000)
        self.assertEqual(review.adjustment_amount, -252615)
        # the list for the administrator: «Pasado a Online»
        adjustment.action_mark_online()
        self.assertTrue(adjustment.online_done)
        self.assertEqual(adjustment.online_done_by_id, self.env.user)
        adjustment.action_unmark_online()
        self.assertFalse(adjustment.online_done)

    def test_debit_requires_confirmation(self):
        self._history(self.beta, debe=1000)
        wizard = self._wizard(self.beta, target_balance=1500)
        self.assertTrue(wizard.is_debit)
        with self.assertRaises(UserError):
            wizard.action_apply()
        self.assertFalse(self.Adjustment.search([("partner_id", "=", self.beta.id)]))
        wizard.confirm_debit = True
        wizard.action_apply()
        adjustment = self.Adjustment.search([("partner_id", "=", self.beta.id)])
        self.assertEqual(adjustment.kind, "debit")
        self.assertEqual(adjustment.difference, 500)
        self.assertEqual(adjustment.move_id.move_type, "out_invoice")
        self.assertEqual(adjustment.move_id.journal_id.code, "AJC")
        self.assertEqual(self._review(self.beta).lab_balance, 1500)

    def test_zero_difference_is_refused(self):
        self._history(self.beta, debe=1000)
        with self.assertRaises(UserError):
            self._adjust(self.beta, target_balance=1000)
        self.assertFalse(self.Adjustment.search([("partner_id", "=", self.beta.id)]))

    def test_motive_and_authorizer_are_required(self):
        self._history(self.beta, debe=1000)
        with self.assertRaises(UserError):
            self._adjust(self.beta, motive="   ")
        with self.assertRaises(UserError):
            self._adjust(self.beta, authorized_by="")
        self.assertFalse(self.Adjustment.search([("partner_id", "=", self.beta.id)]))
        self.assertEqual(self._review(self.beta).lab_balance, 1000)

    def test_only_administration_adjusts(self):
        self._history(self.beta, debe=1000)
        with self.assertRaises(AccessError):
            self.beta.with_user(self.seller).action_distri_balance_adjustment()
        with self.assertRaises(AccessError):
            self.env["distri.balance.adjustment.wizard"].with_user(self.seller).create({
                "partner_id": self.beta.id, "motive": "x", "authorized_by": "y"})
        adjustment = self._adjust(self.beta)
        with self.assertRaises(AccessError):
            adjustment.with_user(self.seller).action_mark_online()
        with self.assertRaises(AccessError):
            self.Adjustment.with_user(self.seller).search([])
        self.assertFalse(adjustment.online_done)

    def test_journal_is_internal(self):
        sale_journal = self.company_data["default_journal_sale"]
        before = self.init_invoice("out_invoice", partner=self.beta, amounts=[100],
                                   journal=sale_journal, post=True)
        self._history(self.alpha, debe=1000)
        journal = self._adjust(self.alpha).move_id.journal_id
        self.assertEqual(journal, self.env["account.journal"]._distri_ajc_journal())
        self.assertEqual(journal.type, "sale")
        self.assertNotIn(journal.code, ("MP", "VSR"))
        self.assertFalse(journal.show_on_dashboard)
        if "l10n_latam_use_documents" in journal._fields:
            self.assertFalse(journal.l10n_latam_use_documents)
        # never the default sales journal, and its series goes on untouched
        default = self.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", self.env.company.id)], limit=1)
        self.assertNotEqual(default, journal)
        after = self.init_invoice("out_invoice", partner=self.beta, amounts=[100],
                                  journal=sale_journal, post=True)
        self.assertEqual(after.sequence_number, before.sequence_number + 1)
        # created once per company
        self.env["account.journal"]._distri_ensure_ajc_journal()
        self.assertEqual(self.env["account.journal"].search_count(
            [("code", "=", "AJC"), ("company_id", "=", self.env.company.id)]), 1)

    def test_review_shows_the_target_after_adjusting(self):
        """From «Revisión por cliente»: the row refreshes with the target."""
        self._history(self.alpha, debe=1606000)
        card = self.env["distri.mp.collection"].create({
            "source_id": "970000000001", "date": datetime(2026, 9, 2, 12, 0),
            "amount": 1000, "method_type": "bank_transfer", "partner_id": self.alpha.id,
            "proposal": "manual"})
        review = self._review(self.alpha)
        self.assertEqual(review.lab_balance, 1606000)
        action = review.action_balance_adjustment()
        self.assertEqual(action["context"]["default_partner_id"], self.alpha.id)
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({
            "motive": "La gerencia confirma deuda cero", "authorized_by": "Gerencia"})
        wizard.action_apply()
        review.invalidate_recordset()
        self.assertEqual(review.lab_balance, 0)
        self.assertEqual(review.adjustment_amount, -1606000)
        # a pending card without receipt never applies: the final balance stays
        self.assertFalse(card.receipt_id)
        self.assertEqual(review.final_balance, 0)
