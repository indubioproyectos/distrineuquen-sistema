"""19.0.1.118.0: «Cobro registrado en la factura» in the «Caja del día».

A cash payment the cashier registers himself with «Registrar pago» on the
invoice, in the caja's cash journal, enters his open caja by itself, once.
It stays out when it reaches a caja another way (a route or depot box
payment), when another user registered it, when it is not plain cash (bank,
the same money as an Online history row) or when it is dated before
distri_pedido.caja_factura_desde. Reset, cancelled or linked to a route
afterwards, it leaves the open caja. The closing reconciles its «Recibos
pendientes» line like a «Cobro en el local»: the cash is booked once.
"""
from datetime import timedelta

from odoo import Command
from odoo.tests import new_test_user, tagged
from odoo.tools import file_path

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja import PARAM_INVOICE_CASH_SINCE
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin


@tagged("post_install", "-at_install", "distri_v118", "distri_caja")
class TestV118CajaFactura(CajaMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.other_cashier = new_test_user(
            cls.env, login="cajero_v118", name="Gonzalo V118",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
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
        # the copy has the rule's first day (migrations/19.0.1.118.0): the
        # tests set it themselves
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_INVOICE_CASH_SINCE, "")

    # ------------------------------------------------------------ helpers

    def _register(self, amount, user=None, journal=None, date=None):
        """«Registrar pago» on a new invoice of the customer, as `user`
        (default the cashier), in `journal` (default the caja's)."""
        invoice = self.init_invoice("out_invoice", partner=self.partner, amounts=[2000000],
                                    post=True)
        values = {"journal_id": (journal or self.cash_journal).id, "amount": amount,
                  "payment_difference_handling": "open"}
        if date:
            values["payment_date"] = date
        payments = self.env["account.payment.register"].with_user(user or self.cashier).sudo(
        ).with_context(active_model="account.move", active_ids=invoice.ids).create(
            values)._create_payments()
        return self.env["account.payment"].browse(payments.ids)

    def _factura(self, caja):
        caja.sudo()._sync_own_collections()
        return caja.movement_ids.filtered(lambda m: m.kind == "factura")

    def _close_with(self, caja, total, left="0"):
        """Count `total` in $ 1.000 / $ 100 bills."""
        return caja.with_user(self.cashier).close_session(
            self._count(b1000=int(total // 1000), b100=int(total % 1000 // 100)), left=left)

    def _route_of(self, payment):
        route = self._route(self._order(self.partner, self.product_a))
        route.sudo().payment_ids = [Command.link(payment.id)]
        return route

    # ------------------------------------------------------------ included

    def test_1_own_invoice_cash_enters_the_open_caja(self):
        before = self._register(250000)
        caja = self._caja().open_session("10000")
        # registered before opening: it enters at the opening
        moves = caja.movement_ids.filtered(lambda m: m.kind == "factura")
        self.assertEqual(moves.payment_id, before)
        after = self._register(130000)
        moves = self._factura(caja)
        self.assertEqual(moves.payment_id, before | after)
        move = moves.filtered(lambda m: m.payment_id == after)
        self.assertEqual(move.amount, 130000)
        self.assertEqual(move.partner_id, self.partner)
        self.assertEqual(move.user_id, self.cashier)
        self.assertFalse(move.box_id)
        self.assertIn(after.name, move.note)
        self.assertEqual(move.label, "Cobro registrado en la factura · %s" % self.partner.name)
        self.assertEqual(caja.total_cobros, 380000)
        self.assertEqual(caja.expected, 390000)

    # ------------------------------------------------------------ excluded

    def test_2_route_payment_stays_out(self):
        routed = self._register(100000)
        self._route_of(routed)
        caja = self._caja().open_session("0")
        self.assertFalse(self._factura(caja))
        self.assertEqual(caja.expected, 0)

    def test_3_other_users_payment_stays_out(self):
        theirs = self._register(70000, user=self.other_cashier)
        by_admin = self._register(80000, user=self.env.user)
        caja = self._caja().open_session("0")
        self.assertFalse(self._factura(caja))
        # the other cashier's own caja takes his
        other = self._caja(self.other_cashier).open_session("0")
        self.assertEqual(self._factura(other).payment_id, theirs)
        self.assertNotIn(by_admin, (caja | other).movement_ids.payment_id)

    def test_4_bank_and_online_history_stay_out(self):
        bank = self._register(90000, journal=self.bank_journal)
        online = self._register(60000)
        self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": today_ar(), "comprobante": "0001/2026/99118",
            "tipo": "factura", "haber": 60000, "active": False, "lab_payment_id": online.id})
        cash = self._register(50000)
        caja = self._caja().open_session("0")
        self.assertEqual(self._factura(caja).payment_id, cash)
        self.assertNotIn(bank, caja.movement_ids.payment_id)
        self.assertNotIn(online, caja.movement_ids.payment_id)

    def test_5_not_before_the_since_day(self):
        yesterday = today_ar() - timedelta(days=1)
        older = self._register(40000, date=yesterday)
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param(PARAM_INVOICE_CASH_SINCE, today_ar().isoformat())
        newer = self._register(45000)
        caja = self._caja().open_session("0")
        self.assertEqual(self._factura(caja).payment_id, newer)
        # an unreadable value is no bound
        ICP.set_param(PARAM_INVOICE_CASH_SINCE, "not a date")
        self.assertEqual(self._factura(caja).payment_id, older | newer)

    def test_5b_migration_sets_the_first_day(self):
        import importlib.util
        path = file_path("distri_pedido/migrations/19.0.1.118.0/post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v118_post_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        ICP = self.env["ir.config_parameter"].sudo()

        def stored():
            self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s",
                                [PARAM_INVOICE_CASH_SINCE])
            row = self.env.cr.fetchone()
            return row and row[0]

        self.env.flush_all()
        module.migrate(self.env.cr, "19.0.1.117.0")
        self.assertEqual(stored(), "2026-11-19")
        self.env.registry.clear_cache()
        ICP.set_param(PARAM_INVOICE_CASH_SINCE, "2026-12-01")
        self.env.flush_all()
        module.migrate(self.env.cr, "19.0.1.117.0")
        self.assertEqual(stored(), "2026-12-01")
        self.env.registry.clear_cache()

    # ------------------------------------------------------------ once

    def test_6_taken_once(self):
        pay = self._register(120000)
        caja = self._caja().open_session("0")
        for _i in range(3):
            self._factura(caja)
        self.assertEqual(len(caja.movement_ids.filtered(lambda m: m.payment_id == pay)), 1)
        self._close_with(caja, 120000)
        # a later session of the same cashier does not take it again
        self.env.flush_all()
        later = self.env["distri.caja"].sudo().create({
            "user_id": self.cashier.id, "date": today_ar() + timedelta(days=1),
            "journal_id": caja.journal_id.id, "previous_id": caja.id})
        self.assertFalse(later._pending_invoice_cash())
        self.assertFalse(self._factura(later))
        self.assertEqual(self.env["distri.caja.movimiento"].search_count(
            [("payment_id", "=", pay.id)]), 1)

    def test_6b_own_depot_box_payment_stays_a_collection(self):
        collection = self.env["distri.cash.collection"].with_user(self.cashier).cash_register(
            self.partner.id, "25000", confirmed=True, route_ok=True, over_ok=True)
        caja = self._caja().open_session("0")
        caja.sudo()._sync_own_collections()
        payment = caja.movement_ids.payment_id
        self.assertEqual(len(caja.movement_ids), 1, collection)
        self.assertEqual(caja.movement_ids.kind, "cobro")
        self.assertEqual(caja.expected, payment.amount)

    # ------------------------------------------------------------ dropped

    def test_7_dropped_when_cancelled_or_reset(self):
        # the cancelled one last: only the last entry of the journal's chain
        # can be cancelled (its draft entry is deleted)
        kept, reset, cancelled = self._register(30000), self._register(20000), self._register(10000)
        caja = self._caja().open_session("0")
        self.assertEqual(len(self._factura(caja)), 3)
        cancelled.sudo().action_draft()
        cancelled.sudo().action_cancel()
        reset.sudo().action_draft()
        self.assertEqual(self._factura(caja).payment_id, kept)
        self.assertEqual(caja.expected, 30000)
        self.assertTrue(any("Se quitó de la caja el cobro %s" % cancelled.name in (m.body or "")
                            for m in caja.message_ids))
        # posted again: it comes back, once
        reset.sudo().action_post()
        self.assertEqual(self._factura(caja).payment_id, kept | reset)

    def test_8_dropped_when_linked_to_a_route(self):
        pay = self._register(55000)
        caja = self._caja().open_session("0")
        self.assertEqual(self._factura(caja).payment_id, pay)
        route = self._route_of(pay)
        self.assertFalse(self._factura(caja))
        self.assertEqual(caja.expected, 0)
        self.assertTrue(any("ahora es de %s" % route.name in (m.body or "")
                            for m in caja.message_ids))

    def test_9_closed_caja_left_as_is(self):
        pay = self._register(35000)
        caja = self._caja().open_session("0")
        self._factura(caja)
        self._close_with(caja, 35000)
        self._route_of(pay)
        caja.sudo()._sync_own_collections()
        self.assertEqual(caja.movement_ids.payment_id, pay)
        self.assertEqual(caja.expected, 35000)

    # ------------------------------------------------------------ closing

    def _booked_once(self, caja, payment):
        """The payment's cash reaches the cash account exactly once, through
        the closing's statement line, and «Recibos pendientes» is emptied."""
        statement = caja.statement_id
        self.assertTrue(statement)
        self.assertEqual(statement.balance_end, statement.balance_end_real)
        cash = self.cash_journal.default_account_id
        # the payment's own entry never touches the cash account
        self.assertFalse(payment.move_id.line_ids.filtered(lambda l: l.account_id == cash))
        move = caja.movement_ids.filtered(lambda m: m.payment_id == payment)
        self.assertEqual(move.kind, "factura")
        self.assertEqual(len(move.statement_line_ids), 1)
        line = move.statement_line_ids
        self.assertEqual(line.amount, payment.amount)
        self.assertIn(payment.name, line.payment_ref)
        self.assertEqual(line.move_id.line_ids.filtered(
            lambda l: l.account_id == cash).debit, payment.amount)
        outstanding = payment.move_id.line_ids.filtered(
            lambda l: l.account_id == payment.outstanding_account_id)
        self.assertTrue(outstanding.reconciled)
        self.assertEqual(payment.state, "paid")
        # no other line of the closing mentions it
        self.assertEqual(len(statement.line_ids.filtered(
            lambda l: payment.name in (l.payment_ref or ""))), 1)

    def test_10_closing_books_the_cash_once(self):
        pay = self._register(250000)
        box = self.env["distri.cash.collection"].with_user(self.cashier).cash_register(
            self.partner.id, "2500", confirmed=True, route_ok=True, over_ok=True)
        self.assertTrue(box)
        caja = self._caja().open_session("10000")
        self._factura(caja)
        self.assertEqual(caja.expected, 262500)
        self._close_with(caja, 262500)
        self.assertEqual(caja.state, "closed")
        self.assertFalse(caja.difference)
        self.assertFalse(caja.case_id)
        self._booked_once(caja, pay)
        # the catch-up has nothing left of it
        backlog = self.env["distri.caja.conciliacion.inicial"]._backlog(self.env.company)
        for payments, _lines in backlog.values():
            self.assertNotIn(pay, payments)
        # reopened and closed again: still once, no difference
        caja.with_user(self.cashier).action_reopen("faltaba contar")
        self.assertFalse(pay.move_id.line_ids.filtered(
            lambda l: l.account_id == pay.outstanding_account_id).reconciled)
        self._close_with(caja, 262500)
        self.assertFalse(caja.difference)
        self.assertEqual(self.env["account.bank.statement"].search([("name", "=", caja.name)]),
                         caja.statement_id)
        self._booked_once(caja, pay)

    def test_11_already_reconciled_payment_gets_no_second_line(self):
        pay = self._register(40000)
        # the catch-up reconciled it before any caja took it
        wizard = self.env["distri.caja.conciliacion.inicial"].create({})
        wizard.action_apply()
        self.assertTrue(pay.move_id.line_ids.filtered(
            lambda l: l.account_id == pay.outstanding_account_id).reconciled)
        caja = self._caja().open_session("0")
        move = self._factura(caja)
        self.assertEqual(move.payment_id, pay)
        self._close_with(caja, 40000)
        self.assertFalse(caja.difference)
        self.assertFalse(move.statement_line_ids)
