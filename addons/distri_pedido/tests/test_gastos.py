"""Driver expenses paid with the day's cash (models/gasto.py).

The photos are synthetic tickets drawn by tests/receipt_images.py.
"""
import base64
import unittest

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.ocr_comprobante import ocr_available
from odoo.addons.distri_pedido.tests import receipt_images
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid


def photo_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


@tagged("post_install", "-at_install", "distri_gastos")
class TestGastos(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.order = cls._order(cls.partner_a, cls.product_a)
        # the accounts of the Argentine chart the expenses go to
        Account = cls.env["account.account"]
        cls.accounts = {}
        for code in set(EXPENSE_ACCOUNTS.values()):
            cls.accounts[code] = Account.search([("code", "=", code)], limit=1) or Account.create({
                "name": "Gasto %s" % code, "code": code, "account_type": "expense"})
        cls.ticket = receipt_images.fuel_ticket(45000)

    def _route_with_visit(self, cash=100000):
        route = self._route(self.order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=cash))])
        self.assertEqual(res[0]["status"], "ok", res)
        route.journal_id = self.company_data["default_journal_cash"]
        return route

    def _expense(self, route, kind="fuel", amount=45000, photo=None, **kw):
        payload = {"expense_uuid": _uuid(), "kind": kind, "amount": amount, "comment": "",
                   "photo": photo_url(photo or self.ticket)}
        payload.update(kw)
        return payload

    # ------------------------------------------------------------ sync

    def test_offline_expense_sync_is_idempotent(self):
        route = self._route_with_visit()
        payload = self._expense(route, comment="YPF Ruta 22")
        event = self._event(route, "expense", payload)
        first = self._sync([event])
        again = self._sync([event])
        self.assertEqual(first[0]["status"], "ok", first)
        self.assertEqual(again[0]["status"], "duplicate")
        # the phone retried with a new event id: the expense id still dedups
        other = self._sync([self._event(route, "expense", payload)])
        self.assertEqual(other[0]["status"], "ok")
        expense = route.expense_ids
        self.assertEqual(len(expense), 1)
        self.assertEqual(expense.kind, "fuel")
        self.assertEqual(expense.amount, 45000)
        self.assertEqual(expense.comment, "YPF Ruta 22")
        self.assertTrue(expense.photo, "the receipt photo is kept")
        self.assertEqual(expense.ocr_state, "pending")
        # the sync log never keeps the photo itself
        log = self.env["distri.sync.event"].sudo().search([("uuid", "=", event["uuid"])])
        self.assertNotIn(payload["photo"][30:90], log.payload)

    def test_expense_needs_photo_and_amount(self):
        route = self._route_with_visit()
        res = self._sync([
            self._event(route, "expense", dict(self._expense(route), photo="")),
            self._event(route, "expense", dict(self._expense(route), photo="data:image/png;base64,QUJD")),
            self._event(route, "expense", self._expense(route, amount=0)),
            self._event(route, "expense", self._expense(route, kind="lunch")),
        ])
        self.assertEqual([r["status"] for r in res], ["error"] * 4, res)
        self.assertFalse(any(r["retry"] for r in res))
        self.assertFalse(route.expense_ids)

    def test_cancelled_by_the_driver(self):
        route = self._route_with_visit()
        payload = self._expense(route)
        self._sync([self._event(route, "expense", payload)])
        self.assertEqual(route.expense_total, 45000)
        res = self._sync([self._event(route, "expense", {"expense_uuid": payload["expense_uuid"],
                                                         "deleted": True})])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(route.expense_total, 0)
        self.assertEqual(route.cash_expected, 100000)

    def test_expenses_reduce_the_cash_to_settle(self):
        route = self._route_with_visit(cash=100000)
        self._sync([self._event(route, "expense", self._expense(route, amount=45000)),
                    self._event(route, "expense", self._expense(route, kind="parking", amount=1500))])
        self.assertEqual(route.cash_collected, 100000)
        self.assertEqual(route.expense_total, 46500)
        self.assertEqual(route.cash_expected, 53500)
        payload = route._app_payload()
        self.assertEqual(len(payload["expenses"]), 2, "a phone that lost them gets them back")
        self.assertNotIn("photo", payload["expenses"][0])

    def test_no_expense_after_the_route_closed(self):
        route = self._route_with_visit()
        self._sync([self._event(route, "close", {})])
        res = self._sync([self._event(route, "expense", self._expense(route))])
        self.assertEqual(res[0]["status"], "error")

    # ------------------------------------------------------------ settlement

    def test_settlement_posts_approved_expenses_and_opens_cases(self):
        route = self._route_with_visit(cash=100000)
        fuel, parking, other = (self._expense(route, amount=45000),
                                self._expense(route, kind="parking", amount=1500),
                                self._expense(route, kind="other", amount=2000, comment="Candado"))
        self._sync([self._event(route, "expense", p) for p in (fuel, parking, other)] +
                   [self._event(route, "close", {})])
        self.assertEqual(route.state, "closed")
        Expense = self.env["distri.route.expense"]
        e_fuel = Expense.search([("uuid", "=", fuel["expense_uuid"])])
        e_parking = Expense.search([("uuid", "=", parking["expense_uuid"])])
        e_other = Expense.search([("uuid", "=", other["expense_uuid"])])
        # the administrator counts 51.500: exactly the cash to settle
        route.cash_counted = 51500
        self.assertEqual(route.cash_difference, 0)
        e_fuel.paper_received = True
        # parking without its paper; the other one is rejected
        e_other.reject_reason = "No es un gasto de la ruta"
        e_other.action_reject()
        self.assertEqual(e_other.case_id.kind, "expense")
        self.assertEqual(e_other.case_id.amount, -2000)
        route.action_settle()

        self.assertEqual(route.state, "settled")
        self.assertEqual(e_fuel.state, "approved")
        move = e_fuel.move_id
        self.assertEqual(move.state, "posted")
        self.assertEqual(move.journal_id, route.journal_id)
        debit = move.line_ids.filtered(lambda l: l.debit)
        credit = move.line_ids.filtered(lambda l: l.credit)
        self.assertEqual(debit.account_id.code, EXPENSE_ACCOUNTS["fuel"])
        self.assertEqual(debit.debit, 45000)
        self.assertEqual(credit.account_id, route.journal_id.default_account_id)
        self.assertFalse(move.line_ids.tax_ids, "no taxes")
        self.assertTrue(self.env["ir.attachment"].search_count(
            [("res_model", "=", "account.move"), ("res_id", "=", move.id)]),
            "the receipt photo goes with the entry")

        self.assertEqual(e_parking.state, "rejected")
        self.assertFalse(e_parking.move_id)
        self.assertEqual(e_parking.case_id.amount, -1500)
        self.assertIn("sin comprobante en papel", e_parking.case_id.candidates)
        self.assertFalse(e_other.move_id)
        cases = route.case_ids.filtered(lambda c: c.kind == "expense")
        self.assertEqual(len(cases), 2)
        # the customer's cash is still registered as a payment
        self.assertEqual(sum(route.payment_ids.mapped("amount")), 100000)

        with self.assertRaises(UserError):
            e_parking.paper_received = True  # settled

    def test_cash_candidates_include_an_expense(self):
        route = self._route_with_visit(cash=100000)
        self._sync([self._event(route, "expense", self._expense(route, amount=45000)),
                    self._event(route, "close", {})])
        # the driver forgot to hand in the fuel money: 45.000 missing
        route.cash_counted = 10000
        self.assertEqual(route.cash_difference, -45000)
        self.assertIn("Gasto Nafta de $ 45.000", route.cash_candidates)

    def test_account_fallback_and_parameter(self):
        route = self._route_with_visit()
        self._sync([self._event(route, "expense", self._expense(route, kind="parts", amount=900))])
        expense = route.expense_ids
        self.assertEqual(expense.account_id.code, EXPENSE_ACCOUNTS["parts"])
        other = self.env["account.account"].create({
            "name": "Mantenimiento de rodados", "code": "5.2.1.01.990", "account_type": "expense"})
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.cuenta_gasto_parts", "5.2.1.01.990")
        expense.invalidate_recordset(["account_id"])
        self.assertEqual(expense.account_id, other)

    # ------------------------------------------------------------ OCR

    @unittest.skipUnless(ocr_available(), "tesseract is not installed")
    def test_ocr_flags_a_different_amount(self):
        route = self._route_with_visit()
        right = self._expense(route, amount=45000)
        wrong = self._expense(route, amount=54000)
        self._sync([self._event(route, "expense", right), self._event(route, "expense", wrong)])
        self.env["distri.route.expense"]._cron_read_photos()
        Expense = self.env["distri.route.expense"]
        e_right = Expense.search([("uuid", "=", right["expense_uuid"])])
        e_wrong = Expense.search([("uuid", "=", wrong["expense_uuid"])])
        self.assertEqual(e_right.ocr_amount, 45000)
        self.assertEqual(e_right.ocr_state, "ok")
        self.assertFalse(e_right.amount_review)
        self.assertEqual(e_wrong.ocr_state, "review")
        self.assertTrue(e_wrong.amount_review, "«Revisar importe»")
