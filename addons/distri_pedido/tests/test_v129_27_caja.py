"""19.0.1.129.27: «Caja v2» behind distri_pedido.caja_v2 (models/caja_v2.py).

Flag OFF: the closing books exactly what 129.26 booked (every difference in
«Diferencias de caja», nothing in «Deudores varios», no advance). Flag ON:
each line of the mapping table, the employee's partner (never a namesake
customer), «Adelanto para la ruta» (#103) and the opening debts."""
from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja_contabilidad import (
    ACCOUNTS, PARAM_JOURNAL, caja_account)
from odoo.addons.distri_pedido.models.caja_v2 import (
    DEFAULT_DEBT_CODE, PARAM_CAJA_V2, caja_v2_on, debt_account, debt_account_ids)
from odoo.addons.distri_pedido.models.deudores import direct_customers
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin, photo_url
from odoo.addons.distri_pedido.tests.test_reparto import _uuid


class CajaV2Mixin(CajaMixin):

    @classmethod
    def _setup_v2(cls):
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.cashier2 = new_test_user(
            cls.env, login="cajero_dos", name="Cajera Dos",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        cls.partner = cls.partner_a
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        from odoo.addons.distri_pedido.tests import receipt_images
        cls.ticket = receipt_images.fuel_ticket(20000)

    def _collect(self, amount, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cashier).cash_register(
            self.partner.id, str(amount), confirmed=True, route_ok=True, over_ok=True)

    def _v2(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_CAJA_V2, "1" if on else "0")
        self.assertEqual(caja_v2_on(self.env), on)

    def _road_route(self, driver=None):
        order = self._order(self.partner, self.product_a)
        return self._route(order, driver=driver)

    def _deliver(self, route, cash, expenses=()):
        driver = route.driver_id
        res = self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=cash))],
                         user=driver)
        self.assertEqual(res[0]["status"], "ok", res)
        for amount in expenses:
            payload = {"expense_uuid": _uuid(), "kind": "fuel", "amount": amount,
                       "comment": "YPF", "photo": photo_url(self.ticket)}
            res = self._sync([self._event(route, "expense", payload)], user=driver)
            self.assertEqual(res[0]["status"], "ok", res)
        route.sudo().state = "closed"
        return route

    def _closed_route(self, cash=100000, expenses=(), driver=None):
        return self._deliver(self._road_route(driver), cash, expenses)

    def _close(self, caja, amount, user=None, reason=""):
        return caja.with_user(user or self.cashier).close_session(
            {}, other=str(int(amount)), left="0", reason=reason)

    def _lines(self, caja, account):
        return caja.statement_id.line_ids.move_id.line_ids.filtered(
            lambda l: l.account_id == account)

    def _debt_lines(self, partner=None):
        ids = debt_account_ids(self.env)
        domain = [("account_id", "in", ids), ("parent_state", "=", "posted")]
        if partner is not None:
            domain.append(("partner_id", "=", partner.id))
        return self.env["account.move.line"].search(domain) if ids else \
            self.env["account.move.line"]

    def _customer_balance(self):
        return round(self.partner.sudo()._distri_account_balance(), 2)

    def _scenario(self):
        """The same day twice (flag off / on): a route with a rejected
        expense and a shortfall he brings back, a route with a surplus, and
        the cashier short at the count."""
        caja = self._caja().open_session("0")
        Caja = caja.with_user(self.cashier)
        route = self._closed_route(cash=100000, expenses=(20000, 5000))
        fuel, small = route.expense_ids.sorted("amount", reverse=True)
        short = Caja.receive_route(route, "74000", papers=[small.id], no_paper_ok=True,
                                   difference_mode="manana")
        Caja.bring_shortage(short)
        other = self._closed_route(cash=50000, driver=self.other_driver)
        Caja.receive_route(other, "50500", difference_mode="cargo")
        self.assertEqual(caja.expected, 74000 + 1000 + 50500)
        self._close(caja, 125000, reason="faltan 500")
        self.assertEqual(caja.difference, -500)
        return caja, route, short, fuel


@tagged("post_install", "-at_install", "distri_v129_27")
class TestCajaV2(CajaV2Mixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_v2()

    # ------------------------------------------------------------ flag off

    def test_off_by_default_closing_is_129_26(self):
        """No parameter at all: every difference in «Diferencias de caja»
        (rejected expense, shortfall, «ajuste», surplus, the cashier's), no
        «Deudores varios» account, no debt recorded."""
        self.assertFalse(self.env["ir.config_parameter"].sudo().get_param(PARAM_CAJA_V2))
        self.assertFalse(caja_v2_on(self.env))
        balance = self._customer_balance()
        caja, route, short, fuel = self._scenario()
        diff = caja_account(self.env, self.env.company, "diferencias")
        lines = self._lines(caja, diff)
        self.assertEqual(sorted(lines.mapped("balance")), [-1000, -500, 500, 1000, 20000])
        self.assertFalse(lines.partner_id, "no partner on the differences")
        self.assertEqual(debt_account_ids(self.env), [], "«Deudores varios» never created")
        self.assertFalse(self.env["account.account"].search([("code", "=", DEFAULT_DEBT_CODE)]))
        self.assertFalse(short.debt_partner_id)
        self.assertFalse(short.case_id.debt_partner_id)
        self.assertFalse(fuel.case_id.debt_partner_id)
        self.assertFalse(caja.case_id.debt_partner_id)
        self.assertFalse(self.env["distri.caja.movimiento"].search(
            [("caja_id", "=", caja.id), ("debt_partner_id", "!=", False)]))
        self.assertAlmostEqual(caja.statement_id.balance_end, 0, msg="all withdrawn")
        # the customer's cash payments only (100000 + 50000): the flag-on
        # twin test gets the same
        self.assertEqual(self._customer_balance(), balance - 150000)
        # the cash cases are not «Deudas de empleados» without the flag
        self.assertFalse(self.env["distri.employee.debt"].search(
            [("origin", "in", ("cash", "expense"))]))

    def test_off_explicit_zero_same_as_absent(self):
        self._v2(False)
        caja, *_ = self._scenario()
        diff = caja_account(self.env, self.env.company, "diferencias")
        self.assertEqual(sorted(self._lines(caja, diff).mapped("balance")),
                         [-1000, -500, 500, 1000, 20000])
        self.assertEqual(debt_account_ids(self.env), [])

    def test_off_no_advance_and_expected_cash_unchanged(self):
        caja = self._caja().open_session("300000")
        route = self._road_route()
        with self.assertRaisesRegex(UserError, "todavía no está habilitado"):
            caja.with_user(self.cashier).add_advance(route, "200000")
        self._deliver(route, 100000, expenses=(5000,))
        self.assertEqual(route.advance_total, 0)
        self.assertEqual(route.cash_expected, 95000, "collected − expenses, as 129.26")

    # ------------------------------------------------------------ flag on: A

    def test_on_mapping_of_the_same_day(self):
        self._v2()
        balance = self._customer_balance()
        caja, route, short, fuel = self._scenario()
        diff = caja_account(self.env, self.env.company, "diferencias")
        debts = debt_account(self.env, self.env.company)
        self.assertEqual(debts.code, DEFAULT_DEBT_CODE)
        self.assertEqual(debts.account_type, "asset_receivable")
        # only the surplus of the other route stays in «Diferencias de caja»
        self.assertEqual(self._lines(caja, diff).mapped("balance"), [-500])
        driver, cashier = self.driver.partner_id, self.cashier.partner_id
        mine = self._lines(caja, debts)
        self.assertEqual(set(mine.partner_id.ids), {driver.id, cashier.id})
        # rejected 20000 + shortfall 1000 − brought back 1000
        self.assertEqual(sorted(mine.filtered(lambda l: l.partner_id == driver).mapped("balance")),
                         [-1000, 1000, 20000])
        self.assertAlmostEqual(sum(self._debt_lines(driver).mapped("balance")), 20000)
        self.assertAlmostEqual(sum(self._debt_lines(cashier).mapped("balance")), 500)
        self.assertFalse(self._debt_lines(self.other_driver.partner_id), "a surplus is no debt")
        # the shortfall he brought nets to zero, reconciled
        pair = mine.filtered(lambda l: l.partner_id == driver and abs(l.balance) == 1000)
        self.assertTrue(all(pair.mapped("reconciled")))
        # the decisions are stored where they were taken
        self.assertEqual(short.debt_partner_id, driver)
        self.assertEqual(short.case_id.debt_partner_id, driver)
        self.assertEqual(fuel.case_id.debt_partner_id, driver)
        self.assertEqual(caja.case_id.debt_partner_id, cashier)
        # same cash as with the flag off
        self.assertAlmostEqual(caja.statement_id.balance_end, 0, msg="all withdrawn")
        self.assertEqual(self._customer_balance(), balance - 150000,
                         "the customer's payments only, as with the flag off")

    def test_on_cargo_shortfall_is_the_driver_debt_and_case_follows(self):
        self._v2()
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=100000)
        caja.with_user(self.cashier).receive_route(route, "99000", difference_mode="cargo")
        self._close(caja, 99000)
        debts = debt_account(self.env, self.env.company)
        line = self._lines(caja, debts)
        self.assertEqual(line.balance, 1000)
        self.assertEqual(line.partner_id, self.driver.partner_id)
        self.assertFalse(line.reconciled, "open until administración settles it")
        case = route._cash_case() or route.case_ids.filtered(lambda c: c.kind == "cash")
        self.assertEqual(case.state, "open", "the case is the follow-up")
        self.assertEqual(case.debt_partner_id, self.driver.partner_id)
        debt = self.env["distri.employee.debt"].search([("case_id", "=", case.id)])
        self.assertEqual(debt.origin, "cash")
        self.assertEqual(debt.employee_id, self.driver)
        self.assertEqual(debt.amount_owed, 1000)

    def test_on_lo_trae_manana_across_two_cajas_nets_and_reconciles(self):
        self._v2()
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=100000)
        short = caja.with_user(self.cashier).receive_route(route, "98000",
                                                           difference_mode="manana")
        self._close(caja, 98000)
        debts = debt_account(self.env, self.env.company)
        debit = self._lines(caja, debts)
        self.assertEqual(debit.balance, 2000)
        self.assertEqual(debit.partner_id, self.driver.partner_id)
        # he brings it to the other cashier's caja
        other = self._caja(self.cashier2).open_session("0")
        adjust = other.with_user(self.cashier2).bring_shortage(short)
        self.assertEqual(adjust.debt_partner_id, self.driver.partner_id)
        self._close(other, 2000, user=self.cashier2)
        credit = self._lines(other, debts)
        self.assertEqual(credit.balance, -2000)
        self.assertEqual(credit.partner_id, self.driver.partner_id)
        self.assertTrue(debit.reconciled and credit.reconciled)
        self.assertAlmostEqual(sum(self._debt_lines(self.driver.partner_id).mapped("balance")), 0)
        diff = caja_account(self.env, self.env.company, "diferencias")
        self.assertFalse(self._lines(caja, diff) | self._lines(other, diff))
        self.assertFalse(self.env["distri.employee.debt"].search(
            [("case_id", "=", short.case_id.id), ("state", "=", "owed")]))

    def test_on_surpluses_stay_in_differences(self):
        self._v2()
        caja = self._caja().open_session("1000")
        route = self._closed_route(cash=10000)
        caja.with_user(self.cashier).receive_route(route, "10300", difference_mode="cargo")
        self._close(caja, 11500, reason="sobran 200")
        diff = caja_account(self.env, self.env.company, "diferencias")
        self.assertEqual(sorted(self._lines(caja, diff).mapped("balance")), [-300, -200])
        self.assertEqual(debt_account_ids(self.env), [], "no debt, no account")
        self.assertFalse(caja.case_id.debt_partner_id)

    def test_on_opening_float_difference_stays(self):
        self._v2()
        first = self._caja().open_session("5000")
        first.with_user(self.cashier).close_session({}, other="5000", left="5000")
        first.sudo().date = fields.Date.subtract(first.date, days=1)
        caja = self._caja().open_session("4000", note="faltaban monedas")
        self._close(caja, 4000)
        diff = caja_account(self.env, self.env.company, "diferencias")
        self.assertEqual(self._lines(caja, diff).mapped("balance"), [1000])
        self.assertEqual(debt_account_ids(self.env), [])

    def test_on_cashier_shortfall_reopen_undoes_the_debt(self):
        self._v2()
        caja = self._caja().open_session("0")
        self._collect(10000)
        self._close(caja, 9000, reason="vuelto mal dado")
        debts = debt_account(self.env, self.env.company)
        self.assertEqual(self._lines(caja, debts).partner_id, self.cashier.partner_id)
        self.assertEqual(caja.case_id.debt_partner_id, self.cashier.partner_id)
        caja.with_user(self.manager).action_reopen("contó mal")
        self.assertFalse(self._debt_lines(self.cashier.partner_id))
        self._close(caja, 10000)
        self.assertFalse(self._debt_lines(self.cashier.partner_id))

    def test_on_debt_partner_is_the_users_partner_never_a_namesake(self):
        """Production has customers named like employees («Denis Barros»,
        «SIMON RENDICION»): the debt goes to the res.users partner."""
        self._v2()
        Partner = self.env["res.partner"]
        namesakes = Partner.create({"name": self.driver.name, "customer_rank": 4}) | Partner.create(
            {"name": self.cashier.name, "customer_rank": 4}) | Partner.create(
            {"name": "%s RENDICION" % self.driver.name.upper(), "customer_rank": 3})
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=100000, expenses=(20000,))
        caja.with_user(self.cashier).receive_route(route, "79000", no_paper_ok=True,
                                                   difference_mode="cargo")
        self._close(caja, 78000, reason="faltan")
        debts = debt_account(self.env, self.env.company)
        lines = self._lines(caja, debts)
        self.assertEqual(len(lines), 3)
        self.assertEqual(lines.partner_id, self.driver.partner_id | self.cashier.partner_id)
        driver = lines.filtered(lambda l: l.partner_id == self.driver.partner_id)
        self.assertEqual(sorted(driver.mapped("balance")), [1000, 20000])
        cashier = lines.filtered(lambda l: l.partner_id == self.cashier.partner_id)
        self.assertEqual(cashier.mapped("balance"), [1000])
        self.assertFalse(lines.partner_id & namesakes)
        self.assertFalse(self.env["account.move.line"].search([("partner_id", "in", namesakes.ids)]))
        for user in (self.driver, self.cashier):
            self.assertEqual(user.partner_id.user_ids, user)

    def test_on_employee_debts_stay_out_of_customer_balances(self):
        self._v2()
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=10000)
        caja.with_user(self.cashier).receive_route(route, "9000", difference_mode="cargo")
        self._close(caja, 9000)
        driver = self.driver.partner_id
        self.assertTrue(self._debt_lines(driver))
        self.assertEqual(driver.customer_rank, 0, "a debt does not make him a customer")
        self.assertNotIn(driver, direct_customers(self.env), "not in «Deudores»")
        self.assertEqual(driver._distri_account_balance(), 0, "no customer statement balance")
        self.assertFalse([r for r in driver._distri_statement_rows()])

    # ------------------------------------------------------------ flag on: B

    def _advance_day(self, returned, mode="", giver=None):
        """300000 float; an advance of 200000; the route collects 100000
        and spends 5000 with paper; the driver returns `returned`."""
        giver = giver or self.cashier
        giver_caja = self._caja(giver).open_session("300000")
        route = self._road_route()
        advance = giver_caja.with_user(giver).add_advance(route, "200.000", note="cambio")
        self.assertEqual(advance.kind, "adelanto")
        self.assertEqual(advance.amount, -200000)
        self.assertEqual(advance.debt_partner_id, self.driver.partner_id)
        self.assertEqual(giver_caja.expected, 100000)
        self.assertEqual(giver_caja.total_adelantos, -200000)
        self.assertEqual(route.advance_total, 200000)
        self._deliver(route, 100000, expenses=(5000,))
        self.assertEqual(route.cash_expected, 100000 + 200000 - 5000)
        caja = giver_caja if giver == self.cashier else self._caja().open_session("0")
        movement = caja.with_user(self.cashier).receive_route(
            route, str(returned), papers=route.expense_ids.ids, difference_mode=mode)
        return giver_caja, caja, route, advance, movement

    def test_advance_full_return(self):
        self._v2()
        caja, _same, route, advance, movement = self._advance_day(295000)
        self.assertFalse(movement.route_difference)
        self.assertEqual(caja.expected, 395000)
        self._close(caja, 395000)
        debts = debt_account(self.env, self.env.company)
        lines = self._lines(caja, debts)
        self.assertEqual(sorted(lines.mapped("balance")), [-200000, 200000])
        self.assertEqual(lines.partner_id, self.driver.partner_id)
        self.assertTrue(all(lines.mapped("reconciled")))
        self.assertAlmostEqual(caja.statement_id.balance_end, 0, msg="all withdrawn")
        self.assertEqual(advance.statement_line_ids.amount, -200000)
        self.assertFalse(caja.difference)

    def test_advance_short_return_follows_a(self):
        self._v2()
        caja, _same, route, advance, movement = self._advance_day(290000, mode="cargo")
        self.assertEqual(movement.route_difference, -5000)
        self._close(caja, 390000)
        self.assertAlmostEqual(sum(self._debt_lines(self.driver.partner_id).mapped("balance")),
                               5000, msg="advance returned, the shortfall is his debt")
        diff = caja_account(self.env, self.env.company, "diferencias")
        self.assertFalse(self._lines(caja, diff))
        expense = route.expense_ids
        self.assertEqual(expense.state, "approved")
        self.assertEqual(self._lines(caja, expense._expense_account()).balance, 5000)

    def test_advance_from_another_caja(self):
        self._v2()
        giver, caja, route, advance, movement = self._advance_day(295000, giver=self.cashier2)
        self.assertNotEqual(giver, caja)
        self._close(giver, 100000, user=self.cashier2)
        self._close(caja, 295000)
        debts = debt_account(self.env, self.env.company)
        out, back = self._lines(giver, debts), self._lines(caja, debts)
        self.assertEqual((out.balance, back.balance), (200000, -200000))
        self.assertTrue(out.reconciled and back.reconciled)
        self.assertIn("Devuelve el adelanto", back.name)

    def test_advance_guards_and_removal(self):
        self._v2()
        caja = self._caja().open_session("1000")
        Caja = caja.with_user(self.cashier)
        route = self._road_route()
        self.assertIn(route, self.env["distri.caja"].with_user(self.cashier).advance_routes())
        with self.assertRaisesRegex(UserError, "No alcanza"):
            Caja.add_advance(route, "2000")
        advance = Caja.add_advance(route, "500")
        with self.assertRaisesRegex(UserError, "ya se guardó"):
            Caja.add_advance(route, "500")
        Caja.remove_advance(advance)
        self.assertFalse(advance.exists())
        self.assertEqual(route.advance_total, 0)
        self.assertEqual(caja.expected, 1000)
        route.sudo().state = "closed"
        with self.assertRaisesRegex(UserError, "ya volvió"):
            Caja.add_advance(route, "500")
        self.assertNotIn(route, self.env["distri.caja"].advance_routes())

    # ------------------------------------------------------------ opening debts

    def test_opening_debts_dry_run_apply_and_idempotent(self):
        # the shortfalls of before the flag: in «Diferencias de caja»
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=10000)
        short = caja.with_user(self.cashier).receive_route(route, "9400", difference_mode="manana")
        surplus = self._closed_route(cash=1000, driver=self.other_driver)
        caja.with_user(self.cashier).receive_route(surplus, "1100", difference_mode="cargo")
        self._close(caja, 10300, reason="faltan 200")
        replenished = self.env["distri.route.case"].create({
            "route_id": surplus.id, "kind": "cash", "amount": -50, "replenished": True})
        cases = short.case_id | caja.case_id
        self.assertEqual(caja.case_id.amount, -200)
        Case = self.env["distri.route.case"]
        self.assertEqual(Case._distri_opening_debt_cases(), cases)
        balance = self._customer_balance()
        dry = Case._distri_book_opening_debts()
        self.assertEqual({r["case_id"] for r in dry["rows"]}, set(cases.ids))
        self.assertFalse(any(r["move"] for r in dry["rows"]))
        self.assertFalse(cases.debt_move_id)
        self.assertFalse(self._debt_lines())
        res = Case._distri_book_opening_debts(apply=True, date="2026-11-24")
        rows = {r["case_id"]: r for r in res["rows"]}
        self.assertEqual(rows[short.case_id.id]["partner_id"], self.driver.partner_id.id)
        self.assertEqual(rows[caja.case_id.id]["partner_id"], self.cashier.partner_id.id)
        self.assertEqual(res["counterpart"], caja_account(self.env, self.env.company,
                                                         "diferencias").code)
        move = short.case_id.debt_move_id
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.date, fields.Date.to_date("2026-11-24"))
        debit = move.line_ids.filtered(lambda l: l.debit)
        self.assertEqual((debit.account_id.code, debit.partner_id, debit.debit),
                         (DEFAULT_DEBT_CODE, self.driver.partner_id, 600))
        self.assertEqual(caja.case_id.debt_move_id.line_ids.filtered("debit").partner_id,
                         self.cashier.partner_id)
        self.assertNotIn(replenished, cases)
        self.assertEqual(self._customer_balance(), balance)
        self.assertFalse(Case._distri_book_opening_debts(apply=True)["rows"], "idempotent")
        # he brings the «lo trae mañana» later: the ajuste cancels that debt
        other = self._caja(self.cashier2).open_session("0")
        adjust = other.with_user(self.cashier2).bring_shortage(short)
        self.assertEqual(adjust.debt_partner_id, self.driver.partner_id)
        self._close(other, 600, user=self.cashier2)
        self.assertTrue(debit.reconciled)
        self.assertAlmostEqual(sum(self._debt_lines(self.driver.partner_id).mapped("balance")), 0)

    def test_opening_debts_refuse_a_receivable_counterpart(self):
        caja = self._caja().open_session("0")
        self._collect(1000)
        self._close(caja, 900, reason="faltan")
        receivable = self.partner.property_account_receivable_id
        with self.assertRaisesRegex(UserError, "no puede ser una cuenta a cobrar"):
            self.env["distri.route.case"]._distri_book_opening_debts(
                apply=True, counterpart_code=receivable.code)
        self.assertFalse(caja.case_id.debt_move_id)


@tagged("post_install", "-at_install", "distri_v129_27")
class TestCajaV2Http(CajaMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)

    def _csrf(self, path):
        import re
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def _route_on_road(self):
        return self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "date": fields.Date.today(), "state": "on_route"})

    def test_pages_flag_off(self):
        self._caja().open_session("1000")
        self.authenticate("cajero_test", "cajero_test")
        home = self.url_open("/caja")
        self.assertIn("Tiene que haber", home.text)
        self.assertNotIn("Adelanto para la ruta", home.text)
        res = self.url_open("/caja/adelanto", allow_redirects=False)
        self.assertEqual(res.status_code, 303)

    def test_pages_flag_on(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_CAJA_V2, "1")
        caja = self._caja().open_session("300000")
        route = self._route_on_road()
        self.authenticate("cajero_test", "cajero_test")
        self.assertIn("/caja/adelanto", self.url_open("/caja").text)
        page = self.url_open("/caja/adelanto")
        self.assertIn(route.name, page.text)
        token = self._csrf("/caja/adelanto")
        res = self.url_open("/caja/adelanto", data={
            "csrf_token": token, "ruta": str(route.id), "importe": "200.000"},
            headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.json()["ok"], True, res.text)
        self.assertEqual(caja.expected, 100000)
        home = self.url_open("/caja").text
        self.assertIn("Adelantos para rutas", home)
        self.assertIn("Quitar el adelanto", home)
        route.state = "closed"
        page = self.url_open("/caja/rendicion/%s" % route.id).text
        self.assertIn("Adelanto para la ruta", page)
        self.assertIn("200.000", page)
