"""19.0.1.124.0: the caja at post time and the double credit on Mercado Pago.

- A cash payment the cashier registers on the invoice enters his open caja
  as soon as it is posted; the later syncs do not add it again. Without an
  open caja he gets a note (and a notification from «Registrar pago») and
  the cash enters the next caja he opens.
- «Registrar pago» proposes the caja's cash journal to a cashier; other
  users keep core's default, and «Banco» stays in the choices.
- Confirming a Mercado Pago income that a cashier already registered by hand
  in «Banco» ties the card to that payment instead of creating a second one;
  two candidates stop the confirmation so a person decides.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja import PARAM_INVOICE_CASH_SINCE
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.caja_pago import NO_OPEN_CAJA_NOTICE
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin


@tagged("post_install", "-at_install", "distri_v124", "distri_caja")
class TestV124CajaMp(CajaMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            # 19.0.1.129.11: confirming a report income needs grupo_ingresos_reporte
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.bank_journal = cls.company_data["default_journal_bank"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        cls.partner = cls.partner_a
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_INVOICE_CASH_SINCE, "")
        cls.mp_journal = cls.env["account.journal"]._distri_mp_journal(company)
        cls.seq = 0

    # ------------------------------------------------------------ helpers

    def _register(self, amount, user=None, journal=None):
        """«Registrar pago» on a new invoice, as `user` (default the
        cashier), in `journal` (default the caja's), as in test_v118."""
        invoice = self.init_invoice("out_invoice", partner=self.partner, amounts=[2000000],
                                    post=True)
        payments = self.env["account.payment.register"].with_user(user or self.cashier).sudo(
        ).with_context(active_model="account.move", active_ids=invoice.ids).create({
            "journal_id": (journal or self.cash_journal).id, "amount": amount,
            "payment_difference_handling": "open"})._create_payments()
        return self.env["account.payment"].browse(payments.ids)

    def _factura(self, caja):
        caja.sudo()._sync_own_collections()
        return caja.movement_ids.filtered(lambda m: m.kind == "factura")

    def _movements(self, payment):
        return self.env["distri.caja.movimiento"].sudo().search([("payment_id", "=", payment.id)])

    def _wizard(self, user, journal=None):
        invoice = self.init_invoice("out_invoice", partner=self.partner, amounts=[50000], post=True)
        # as in production: no preferred payment method on the invoices
        invoice.preferred_payment_method_line_id = False
        values = {"journal_id": journal.id} if journal else {}
        return self.env["account.payment.register"].with_user(user).sudo().with_context(
            active_model="account.move", active_ids=invoice.ids).create(values)

    def _bank_payment(self, amount, date=None, partner=None):
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": (partner or self.partner).id, "amount": amount,
            "journal_id": self.bank_journal.id, "date": date or today_ar(),
            "memo": "0001/2026/12400"})
        payment.action_post()
        return payment

    def _income(self, amount, partner=None):
        TestV124CajaMp.seq += 1
        return self.env["distri.mp.collection"].create({
            "source_id": "124%09d" % self.seq, "date": fields.Datetime.now(), "amount": amount,
            "method_type": "bank_transfer", "method": "cvu_transfer",
            "payer_name": "PEREZ JUAN V124", "partner_id": (partner or self.partner).id,
            "proposal": "manual"})

    def _mp_payments(self):
        return self.env["account.payment"].sudo().search([("journal_id", "=", self.mp_journal.id)])

    # ------------------------------------------------------------ instant caja

    def test_cash_enters_the_open_caja_on_post_once(self):
        caja = self._caja().open_session("0")
        pay = self._register(140000)
        move = self._movements(pay)
        self.assertEqual(len(move), 1)
        self.assertEqual(move.caja_id, caja)
        self.assertEqual(move.kind, "factura")
        self.assertEqual(caja.total_cobros, 140000)
        # the /caja sync (and the closing's) finds it taken
        caja.sudo()._sync_own_collections()
        caja.sudo()._sync_invoice_collections()
        self.assertEqual(len(self._movements(pay)), 1)

    def test_no_open_caja_note_then_enters_on_opening(self):
        pay = self._register(60000)
        self.assertFalse(self._movements(pay))
        self.assertTrue(any(NO_OPEN_CAJA_NOTICE in (m.body or "") for m in pay.message_ids))
        caja = self._caja().open_session("0")
        self.assertEqual(self._movements(pay).caja_id, caja)

    def test_no_open_caja_notification_from_the_wizard(self):
        action = self._wizard(self.cashier, self.cash_journal).action_create_payments()
        self.assertEqual(action["tag"], "display_notification")
        self.assertEqual(action["params"]["message"], NO_OPEN_CAJA_NOTICE)
        self.assertEqual(action["params"]["next"]["res_model"], "account.payment")
        # with the caja open: the usual action, no notice
        self._caja().open_session("0")
        action = self._wizard(self.cashier, self.cash_journal).action_create_payments()
        self.assertEqual(action["type"], "ir.actions.act_window")

    def test_other_users_and_bank_stay_out(self):
        caja = self._caja().open_session("0")
        by_admin = self._register(70000, user=self.env.user)
        bank = self._register(80000, journal=self.bank_journal)
        self.assertFalse(self._movements(by_admin))
        self.assertFalse(self._movements(bank))
        self.assertFalse(any(NO_OPEN_CAJA_NOTICE in (m.body or "")
                             for m in (by_admin | bank).message_ids))
        self.assertFalse(caja.movement_ids)

    def test_route_payment_not_taken_on_post(self):
        caja = self._caja().open_session("0")
        route = self._route(self._order(self.partner, self.product_a))
        route.sudo().journal_id = self.cash_journal
        stop = route.stop_ids[:1]
        stop.sudo().cash_amount = 25000
        payment = route.with_user(self.cashier).sudo()._register_stop_cash(stop)
        self.assertFalse(self._movements(payment))
        caja.sudo()._sync_own_collections()
        self.assertFalse(caja.movement_ids.filtered(lambda m: m.kind == "factura"))

    def test_cancel_after_instant_add_is_dropped(self):
        caja = self._caja().open_session("0")
        pay = self._register(30000)
        self.assertEqual(self._movements(pay).caja_id, caja)
        pay.sudo().action_draft()
        pay.sudo().action_cancel()
        self.assertFalse(self._factura(caja))
        self.assertEqual(caja.expected, 0)

    def test_sync_problem_never_blocks_the_post(self):
        self._caja().open_session("0")
        Caja = type(self.env["distri.caja"])

        def broken(self, only=None):
            raise RuntimeError("boom")

        original = Caja._sync_invoice_collections
        Caja._sync_invoice_collections = broken
        try:
            with self.assertLogs("odoo.addons.distri_pedido.models.caja_pago", "ERROR"):
                pay = self._register(20000)
        finally:
            Caja._sync_invoice_collections = original
        self.assertIn(pay.state, ("in_process", "paid"))
        self.assertFalse(self._movements(pay))

    # ------------------------------------------------------------ default journal

    def test_wizard_default_journal(self):
        wizard = self._wizard(self.cashier)
        self.assertEqual(wizard.journal_id, self.cash_journal)
        self.assertIn(self.bank_journal, wizard.available_journal_ids)
        admin = self._wizard(self.env.user)
        self.assertEqual(admin.journal_id, admin._get_batch_journal(admin.batches[0]))
        # a journal chosen by the cashier stays
        self.assertEqual(self._wizard(self.cashier, self.bank_journal).journal_id,
                         self.bank_journal)

    # ------------------------------------------------------------ Mercado Pago

    def test_mp_confirm_ties_the_bank_payment(self):
        bank = self._bank_payment(45000, date=today_ar() - timedelta(days=2))
        card = self._income(45000)
        before = self._mp_payments()
        # 19.0.1.129.0: proposed, never tied by itself; a person confirms it
        with self.assertRaisesRegex(UserError, "Es el pago cargado a mano"):
            card.action_confirm()
        self.assertEqual(card.state, "draft")
        card.action_tie_bank_twin()
        self.assertEqual(self._mp_payments(), before)
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.payment_id, bank)
        self.assertTrue(card.confirmed_by_id)
        self.assertIn(bank.name, card.proposal_note)
        self.assertTrue(any(bank.name in (m.body or "") for m in card.message_ids))
        self.assertTrue(any(card.name in (m.body or "") for m in bank.message_ids))
        # tied: a second income of the same amount gets its own payment
        other = self._income(45000)
        other.action_confirm()
        self.assertNotEqual(other.payment_id, bank)
        self.assertEqual(other.payment_id.journal_id, self.mp_journal)

    def test_mp_confirm_two_candidates_refused(self):
        first, second = self._bank_payment(52000), self._bank_payment(52000)
        card = self._income(52000)
        with self.assertRaises(UserError) as error:
            card.action_confirm()
        self.assertIn(first.name, str(error.exception))
        self.assertIn(second.name, str(error.exception))
        self.assertEqual(card.state, "draft")

    def test_mp_confirm_without_candidate_creates_the_payment(self):
        # out of the window, another amount, another customer, the MP journal
        self._bank_payment(33000, date=today_ar() - timedelta(days=3))
        self._bank_payment(33001)
        self._bank_payment(33000, partner=self.partner_b)
        card = self._income(33000)
        card.action_confirm()
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.payment_id.journal_id, self.mp_journal)
        self.assertEqual(card.payment_id.amount, 33000)

    def test_mp_bulk_confirm(self):
        bank = self._bank_payment(61000)
        tied, plain = self._income(61000), self._income(62000)
        # the board's «Confirmar» on the chosen ones, through its notice:
        # 19.0.1.129.0, a possible bank twin stops it until a person decides
        notice = self.env["distri.mp.confirm.notice"].create({
            "mode": "selected", "collection_ids": [Command.set((tied | plain).ids)]})
        with self.assertRaisesRegex(UserError, "cargado a mano"):
            notice.action_apply()
        self.assertEqual((tied | plain).mapped("state"), ["draft", "draft"])
        self.assertEqual(tied._bulk_problem(), "puede ser un pago cargado a mano en el banco")
        tied.action_tie_bank_twin()
        self.env["distri.mp.confirm.notice"].create({
            "mode": "selected", "collection_ids": [Command.set(plain.ids)]}).action_apply()
        self.assertEqual(tied.payment_id, bank)
        self.assertEqual(plain.payment_id.journal_id, self.mp_journal)
        # an ambiguous one stops the whole selection before anything is confirmed
        self._bank_payment(63000), self._bank_payment(63000)
        ambiguous, waiting = self._income(63000), self._income(64000)
        with self.assertRaises(UserError):
            (ambiguous | waiting).action_confirm()
        self.assertEqual((ambiguous | waiting).mapped("state"), ["draft", "draft"])
