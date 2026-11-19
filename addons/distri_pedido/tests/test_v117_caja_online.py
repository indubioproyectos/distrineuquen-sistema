"""19.0.1.117.0: «Cobros en efectivo cargados en Online» in the «Caja del día».

A cash payment the cashier registered in Odoo Online (the read-only mirror,
distri.online.payment) enters his open caja once («Traer a mi caja»), without
a lab payment nor a row of the customer's account history; «Sacar de mi
caja» undoes it while the caja is open; cancelled in Online it leaves the
open caja; the closing books the cash against the «cobros de Online» account
(the suspense account by default), never the customer's receivable, also
after reopening.
"""
import re
from datetime import timedelta

from psycopg2.errors import UniqueViolation

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import file_path, mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja import ONLINE_CASH_DAYS, PARAM_ONLINE_CASH_SINCE
from odoo.addons.distri_pedido.models.caja_contabilidad import (
    ACCOUNTS, PARAM_ACCOUNT, PARAM_JOURNAL)
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.puesta_al_dia import (
    PARAM_ONLINE_BANK, PARAM_ONLINE_CASH, online_cash_journals)
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin

PARTNER_OID = 99117000
FIRST_ID = 99117001


class OnlineCashMixin(CajaMixin):

    @classmethod
    def _setup_online(cls):
        cls.partner = cls.env["res.partner"].create({
            "name": "DARIO MAYORISTA V117", "ref": "ODOO-%s" % PARTNER_OID})
        cls.Online = cls.env["distri.online.payment"]
        cls.next_id = FIRST_ID
        # the copy has its go-live day (migrations/19.0.1.117.0): the tests
        # set it themselves
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_ONLINE_CASH_SINCE, "")

    @classmethod
    def _row(cls, amount, days_ago=0, journal=15, state="paid", memo="0001/2026/08054",
             online_id=None):
        if online_id is None:
            online_id = cls.next_id
            cls.next_id += 1
        name = "%s/2026/%05d" % ("PBNK1" if journal == 13 else "PCSH1", online_id % 100000)
        return {"online_id": online_id, "name": name,
                "date": (today_ar() - timedelta(days=days_ago)).isoformat(), "amount": amount,
                "journal_id": journal, "journal_name": "Bank" if journal == 13 else "EFECTIVO",
                "memo": memo, "state": state, "partner_id": PARTNER_OID,
                "move_name": "%s (%s)" % (name, memo)}

    def _load(self, *rows):
        """The sync's mirror load (as the read-only sync runs it)."""
        self.Online.sudo()._distri_load(list(rows))
        return self.Online.sudo().search([("online_id", "in", [r["online_id"] for r in rows])])

    def _pending(self, user=None):
        return self.env["distri.caja"].with_user(user or self.cashier).pending_online_cash()


@tagged("post_install", "-at_install", "distri_v117", "distri_caja")
class TestV117CajaOnline(OnlineCashMixin, AccountTestInvoicingCommon):

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
            cls.env, login="cajero_v117", name="Gonzalo V117",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        cls._setup_online()

    def _account_rows(self):
        """What the sync's balance check adds up for this customer, plus the
        lab's own receivable (account.move.line) through the balance."""
        Mov = self.env["distri.cuenta.movimiento"].sudo().with_context(active_test=False)
        rows = Mov.search([("partner_id", "child_of", self.partner.id)])
        return (len(rows), round(sum(rows.mapped("debe")) - sum(rows.mapped("haber")), 2),
                round(self.partner._distri_account_balance(), 2))

    # ------------------------------------------------------------ the list

    def test_1_cash_journals_from_the_parameter(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param(PARAM_ONLINE_CASH, "")
        self.assertEqual(online_cash_journals(self.env), (15, 17, 18))
        ICP.set_param(PARAM_ONLINE_CASH, "15, 21;22")
        self.assertEqual(online_cash_journals(self.env), (15, 21, 22))
        # the bank is never a cash journal
        ICP.set_param(PARAM_ONLINE_BANK, "21")
        self.assertEqual(online_cash_journals(self.env), (15, 22))

    def test_2_only_recent_posted_cash_not_taken(self):
        rows = {
            "today": self._row(590000),
            "csh2": self._row(1000, journal=18, state="in_process"),
            "edge": self._row(2000, days_ago=ONLINE_CASH_DAYS),
            "old": self._row(3000, days_ago=ONLINE_CASH_DAYS + 1),
            "bank": self._row(4000, journal=13),
            "draft": self._row(5000, state="draft"),
            "cancelled": self._row(6000, state="canceled"),
        }
        loaded = self._load(*rows.values())
        by_key = {k: loaded.filtered(lambda p, r=r: p.online_id == r["online_id"])
                  for k, r in rows.items()}
        pending = self._pending()
        for key in ("today", "csh2", "edge"):
            self.assertIn(by_key[key], pending, key)
        for key in ("old", "bank", "draft", "cancelled"):
            self.assertNotIn(by_key[key], pending, key)
        # the sync itself created no account rows and no lab payment
        self.assertFalse(self.env["account.payment"].search(
            [("partner_id", "=", self.partner.id)]))
        # taken by one cashier: no longer listed for anyone
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).take_online(by_key["today"])
        self.assertNotIn(by_key["today"], self._pending())
        self.assertNotIn(by_key["today"], self._pending(self.other_cashier))
        self.assertIn(by_key["csh2"], self._pending(self.other_cashier))

    def test_2b_not_before_the_first_caja(self):
        older, newer = self._load(self._row(1000, days_ago=3)), self._load(self._row(2000))
        self.assertIn(older, self._pending())
        self.env["ir.config_parameter"].sudo().set_param(
            PARAM_ONLINE_CASH_SINCE, (today_ar() - timedelta(days=1)).isoformat())
        pending = self._pending()
        self.assertNotIn(older, pending)
        self.assertIn(newer, pending)
        caja = self._caja().open_session("0")
        with self.assertRaisesRegex(UserError, "no es un cobro en efectivo de Online"):
            caja.with_user(self.cashier).take_online(older)

    def test_2c_migration_starts_at_the_first_caja(self):
        import importlib.util
        path = file_path("distri_pedido/migrations/19.0.1.117.0/post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v117_post_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        caja = self._caja().open_session("0")
        first = min(self.env["distri.caja"].sudo().search([]).mapped("date"))
        self.assertLessEqual(first, caja.date)
        ICP = self.env["ir.config_parameter"].sudo()

        def stored():
            self.env.cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s",
                                [PARAM_ONLINE_CASH_SINCE])
            return self.env.cr.fetchone()[0]

        self.env.flush_all()
        module.migrate(self.env.cr, "19.0.1.116.0")
        self.assertEqual(stored(), first.isoformat())
        # a value already there is kept
        self.env.registry.clear_cache()
        ICP.set_param(PARAM_ONLINE_CASH_SINCE, "2026-11-01")
        self.env.flush_all()
        module.migrate(self.env.cr, "19.0.1.116.0")
        self.assertEqual(stored(), "2026-11-01")
        self.env.registry.clear_cache()

    # ------------------------------------------------------------ take

    def test_3_take_counts_like_a_collection_and_touches_no_account(self):
        pay = self._load(self._row(590000))
        before = self._account_rows()
        payments = self.env["account.payment"].search_count([])
        caja = self._caja().open_session("10000")
        movement = caja.with_user(self.cashier).take_online(pay)
        self.assertEqual(movement.kind, "online")
        self.assertEqual(movement.online_payment_id, pay)
        self.assertEqual(movement.amount, 590000)
        self.assertEqual(movement.partner_id, self.partner)
        self.assertFalse(movement.payment_id)
        self.assertIn(pay.name, movement.note)
        self.assertIn("0001/2026/08054", movement.note)
        self.assertEqual(movement.label, "Cobro cargado en Online · DARIO MAYORISTA V117")
        self.assertEqual(movement.with_user(self.cashier).online_payment_label,
                         "%s del %s" % (pay.name, pay.date.strftime("%d/%m/%Y")))
        self.assertEqual(caja.total_cobros, 590000)
        self.assertEqual(caja.expected, 600000)
        self.assertTrue(any("trajo a la caja el cobro en efectivo cargado en Online %s"
                            % pay.name in (m.body or "") for m in caja.message_ids))
        # no lab payment, no history row: the sync's balance check stays at zero
        self.assertEqual(self.env["account.payment"].search_count([]), payments)
        self.assertEqual(self._account_rows(), before)

    def test_4_once_for_every_caja(self):
        pay = self._load(self._row(590000))
        mine = self._caja().open_session("0")
        mine.with_user(self.cashier).take_online(pay)
        with self.assertRaisesRegex(UserError, "ya está en tu caja"):
            mine.with_user(self.cashier).take_online(pay)
        other = self._caja(self.other_cashier).open_session("0")
        with self.assertRaisesRegex(UserError, "ya está en la caja de Cajero Test"):
            other.with_user(self.other_cashier).take_online(pay)
        self.assertFalse(other.movement_ids)
        # the database guarantees it too (concurrent clicks)
        with mute_logger("odoo.sql_db"), self.assertRaises(UniqueViolation):
            with self.env.cr.savepoint():
                self.env["distri.caja.movimiento"].sudo().create({
                    "caja_id": other.id, "kind": "online", "amount": 590000,
                    "online_payment_id": pay.id})
        # a bank, draft or old payment cannot be forced in either
        for row in (self._row(10, journal=13), self._row(20, state="draft"),
                    self._row(30, days_ago=ONLINE_CASH_DAYS + 3)):
            with self.assertRaisesRegex(UserError, "no es un cobro en efectivo de Online"):
                other.with_user(self.other_cashier).take_online(self._load(row))

    def test_5_only_a_cashier_with_an_open_caja(self):
        pay = self._load(self._row(590000))
        Caja = self.env["distri.caja"]
        with self.assertRaises(AccessError):
            Caja.with_user(self.seller).pending_online_cash()
        caja = self._caja().open_session("0")
        for user in (self.seller, self.manager):
            with self.assertRaises(AccessError):
                caja.with_user(user).take_online(pay)
        caja.with_user(self.cashier).close_session({}, left="0")
        with self.assertRaisesRegex(UserError, "ya se cerró"):
            caja.with_user(self.cashier).take_online(pay)
        self.assertFalse(caja.movement_ids)

    # ------------------------------------------------------------ undo

    def test_6_release_while_open(self):
        pay = self._load(self._row(590000))
        caja = self._caja().open_session("1000")
        movement = caja.with_user(self.cashier).take_online(pay)
        # another cashier cannot take it out of this caja
        other = self._caja(self.other_cashier).open_session("0")
        with self.assertRaisesRegex(UserError, "no está en esta caja"):
            other.with_user(self.other_cashier).release_online(movement)
        caja.with_user(self.cashier).release_online(movement)
        self.assertFalse(movement.exists())
        self.assertEqual(caja.expected, 1000)
        self.assertIn(pay, self._pending(self.other_cashier))
        self.assertTrue(any("sacó de la caja el cobro en efectivo cargado en Online" in
                            (m.body or "") for m in caja.message_ids))
        # only that kind of movement
        outflow = caja.with_user(self.cashier).add_outflow("nafta", "500", who="YPF")
        with self.assertRaisesRegex(UserError, "no está en esta caja"):
            caja.with_user(self.cashier).release_online(outflow)
        moved = caja.with_user(self.cashier).take_online(pay)
        caja.with_user(self.cashier).close_session(self._count(b10000=59, b500=1), left="0")
        with self.assertRaisesRegex(UserError, "ya se cerró"):
            caja.with_user(self.cashier).release_online(moved)
        self.assertTrue(moved.exists())

    # ------------------------------------------------------------ closing

    def _close_with(self, caja, total, left="0"):
        """Count `total` in $ 1.000 / $ 100 bills."""
        return caja.with_user(self.cashier).close_session(
            self._count(b1000=int(total // 1000), b100=int(total % 1000 // 100)), left=left)

    def _statement_checks(self, caja, pay):
        statement = caja.statement_id
        self.assertTrue(statement)
        self.assertEqual(statement.balance_end, statement.balance_end_real)
        line = caja.movement_ids.filtered(lambda m: m.kind == "online").statement_line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.amount, pay.amount)
        self.assertIn(pay.name, line.payment_ref)
        items = line.move_id.line_ids
        receivable = self.partner.property_account_receivable_id
        self.assertFalse(items.filtered(lambda l: l.account_id == receivable))
        self.assertFalse(items.filtered(lambda l: l.account_id.account_type == "asset_receivable"))
        self.assertFalse(statement.line_ids.move_id.line_ids.filtered(
            lambda l: l.partner_id == self.partner))
        cash = items.filtered(lambda l: l.account_id == self.cash_journal.default_account_id)
        self.assertEqual(cash.debit, pay.amount)
        return line

    def test_7_closing_books_cash_not_the_receivable(self):
        pay = self._load(self._row(590000))
        before = self._account_rows()
        caja = self._caja().open_session("10000")
        caja.with_user(self.cashier).take_online(pay)
        self._collect_cash(2500)
        caja.sudo()._sync_own_collections()
        self.assertEqual(caja.expected, 602500)
        self._close_with(caja, 602500)
        self.assertEqual(caja.state, "closed")
        self.assertFalse(caja.difference)
        self.assertFalse(caja.case_id)
        self.assertEqual(caja.withdrawn, 602500)
        line = self._statement_checks(caja, pay)
        suspense = self.env.company.account_journal_suspense_account_id
        counterpart = line.move_id.line_ids.filtered(lambda l: l.credit)
        self.assertEqual(counterpart.account_id, suspense)
        self.assertEqual(counterpart.credit, 590000)
        self.assertEqual(self._account_rows(), before)

    def test_8_configured_online_account(self):
        account = self.env["account.account"].create({
            "name": "Cobros de Online a conciliar", "code": "1.1.3.99.117",
            "account_type": "asset_current"})
        self.env["ir.config_parameter"].sudo().set_param(PARAM_ACCOUNT % "online", account.code)
        pay = self._load(self._row(150000))
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).take_online(pay)
        self._close_with(caja, 150000)
        line = self._statement_checks(caja, pay)
        self.assertEqual(line.move_id.line_ids.filtered(lambda l: l.credit).account_id, account)

    def test_9_reopen_rebuilds_once_and_drops_a_cancelled_one(self):
        kept_row, void_row = self._row(590000), self._row(40000)
        kept, void = self._load(kept_row), self._load(void_row)
        before = self._account_rows()
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).take_online(kept)
        caja.with_user(self.cashier).take_online(void)
        self._close_with(caja, 630000)
        first = caja.statement_id
        self.assertTrue(first)
        caja.with_user(self.cashier).action_reopen("faltaba un gasto")
        self.assertFalse(first.exists())
        # cancelled in Online while the caja is open again: the sync drops it
        void_row["state"] = "canceled"
        self._load(void_row)
        self.assertEqual(caja.movement_ids.online_payment_id, kept)
        self.assertTrue(any("Se quitó de la caja el cobro cargado en Online %s" % void.name
                            in (m.body or "") and "cancelado" in m.body
                            for m in caja.message_ids))
        self.assertEqual(caja.expected, 590000)
        self._close_with(caja, 590000)
        self.assertFalse(caja.difference)
        statements = self.env["account.bank.statement"].search([("name", "=", caja.name)])
        self.assertEqual(statements, caja.statement_id)
        self._statement_checks(caja, kept)
        self.assertEqual(self._account_rows(), before)

    def test_10_closed_caja_left_as_is(self):
        row = self._row(75000)
        pay = self._load(row)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).take_online(pay)
        self._close_with(caja, 75000)
        row["state"] = "draft"
        self._load(row)
        caja.sudo()._sync_own_collections()
        self.assertEqual(caja.movement_ids.online_payment_id, pay)
        self.assertEqual(caja.expected, 75000)
        self.assertEqual(caja.state, "closed")

    def test_11_mirror_row_gone_leaves_the_open_caja(self):
        pay = self._load(self._row(12000))
        caja = self._caja().open_session("0")
        movement = caja.with_user(self.cashier).take_online(pay)
        pay.sudo().unlink()
        self.assertFalse(movement.online_payment_id)
        caja.with_user(self.cashier).close_session({}, left="0")
        self.assertFalse(movement.exists())
        self.assertEqual(caja.expected, 0)
        self.assertFalse(caja.difference)
        self.assertTrue(any("ya no está en la copia de Online" in (m.body or "")
                            for m in caja.message_ids))

    # ------------------------------------------------------------ helpers

    def _collect_cash(self, amount):
        return self.env["distri.cash.collection"].with_user(self.cashier).cash_register(
            self.partner_a.id, str(amount), confirmed=True, route_ok=True, over_ok=True)


@tagged("post_install", "-at_install", "distri_v117", "distri_caja")
class TestV117CajaOnlineHttp(OnlineCashMixin, HttpCase):
    """/caja: the section, «Traer a mi caja» and «Sacar de mi caja»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)
        cls._setup_online()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})

    def _csrf(self, page):
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def test_page_take_and_release(self):
        pay = self._load(self._row(590000))
        self.authenticate("cajero_test", "cajero_test")
        # without an open caja the section is not there and posting goes home
        page = self.url_open("/caja")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Cobros en efectivo cargados en Online", page.text)
        token = self._csrf(page)
        res = self.url_open("/caja/online/%s/traer" % pay.id, data={"csrf_token": token},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertFalse(self.env["distri.caja.movimiento"].search(
            [("online_payment_id", "=", pay.id)]))
        caja = self._caja().open_session("0")
        page = self.url_open("/caja")
        self.assertIn("Cobros en efectivo cargados en Online", page.text)
        self.assertIn("/caja/online/%s/traer" % pay.id, page.text)
        self.assertIn("DARIO MAYORISTA V117", page.text)
        self.assertIn(pay.name, page.text)
        self.assertIn("Traer a mi caja", page.text)
        res = self.url_open("/caja/online/%s/traer" % pay.id, data={"csrf_token": token},
                            headers={"X-Distri-Fetch": "1"})
        self.assertTrue(res.json()["ok"], res.text)
        movement = caja.movement_ids
        self.assertEqual(movement.online_payment_id, pay)
        # twice: the error, not a second movement
        res = self.url_open("/caja/online/%s/traer" % pay.id, data={"csrf_token": token},
                            headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("ya está en tu caja", res.json()["error"])
        page = self.url_open("/caja")
        self.assertNotIn("/caja/online/%s/traer" % pay.id, page.text)
        self.assertIn("Sacar de mi caja", page.text)
        self.assertIn("/caja/online/%s/sacar" % movement.id, page.text)
        res = self.url_open("/caja/online/%s/sacar" % movement.id, data={"csrf_token": token},
                            headers={"X-Distri-Fetch": "1"})
        self.assertTrue(res.json()["ok"], res.text)
        self.assertFalse(movement.exists())
        self.assertIn("/caja/online/%s/traer" % pay.id, self.url_open("/caja").text)

    def test_not_a_cashier(self):
        pay = self._load(self._row(590000))
        self.authenticate("vendedor_caja", "vendedor_caja")
        page = self.url_open("/cobro?cliente=%s" % self.partner.id)
        token = re.search(r'csrf_token"?\s*(?:value=|:)\s*"([^"]+)"', page.text)
        self.assertTrue(token, page.text[:300])
        res = self.url_open("/caja/online/%s/traer" % pay.id, data={"csrf_token": token.group(1)})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.env["distri.caja.movimiento"].search(
            [("online_payment_id", "=", pay.id)]))
