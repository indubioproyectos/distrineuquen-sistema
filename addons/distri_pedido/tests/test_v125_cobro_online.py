"""19.0.1.125.0 (pendientes L2): /cobro pays an old Odoo Online invoice.

- The invoices /cobro offers are the statement's rows still owed, this
  Odoo's and the Online history ones, so the page and the statement agree.
- Paying an Online invoice creates an unreconciled payment whose memo ends
  with the invoice number and whose distri_history_invoice_id links it to
  that history row; the statement's red clears on that row (mark_owed pass
  1b, by id), never on a lab invoice with the same number (L4).
- The cash goes to the same box as any /cobro cash: a cashier's caja takes
  it from his box, a non-cashier keeps his «Caja depósito».
- «A cuenta» and lab invoices keep their flow.
Synthetic data only.
"""
import re
from datetime import date

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja_contabilidad import PARAM_JOURNAL
from odoo.addons.distri_pedido.models.cobro_efectivo import DuplicateCashError
from odoo.addons.distri_pedido.models.estado_cuenta import mark_owed
from odoo.addons.distri_pedido.models.reparto import DEPOT_JOURNAL_CODE
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin

D = date


def _debit(day, amount, number="", history_id=False):
    return {"date": day, "number": number, "debit": amount, "credit": 0.0, "line_id": False,
            "reconciled": [], "memo": "", "history_id": history_id, "history_target_id": False}


def _credit(day, amount, number="", memo="", target=False):
    return {"date": day, "number": number, "debit": 0.0, "credit": amount, "line_id": False,
            "reconciled": [], "memo": memo, "history_id": False, "history_target_id": target}


def _owed(rows):
    return [r["owed"] for r in rows]


@tagged("post_install", "-at_install", "distri_v125")
class TestMarkOwedHistoryLink(TransactionCase):
    """The pure rule: a credit linked to a history row covers that row by id."""

    def test_link_beats_a_colliding_number(self):
        memo = "Efectivo · cobró CAJA · caja 21/11 · 0001/2026/08053"
        rows = [_debit(D(2025, 11, 1), 1000.0, "0001/2026/08053", history_id=7),
                _debit(D(2026, 1, 10), 700.0, "0001/2026/08053"),
                _credit(D(2026, 11, 21), 1000.0, "PCDEP/2026/00001", memo=memo, target=7)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 700.0, 0.0])
        # without the link the memo picks the lab one (nearest before)
        rows[2]["history_target_id"] = False
        mark_owed(rows)
        self.assertEqual(_owed(rows), [700.0, 0.0, 0.0], "the L4 bug the link fixes")

    def test_partial_leaves_the_rest_on_the_same_row(self):
        rows = [_debit(D(2025, 10, 1), 500.0, "0001/2025/00001"),
                _debit(D(2025, 11, 1), 1000.0, "0001/2025/00002", history_id=9),
                _credit(D(2026, 11, 21), 300.0, target=9)]
        count, total = mark_owed(rows)
        self.assertEqual(_owed(rows), [500.0, 700.0, 0.0])
        self.assertEqual((count, total), (2, 1200.0))

    def test_link_skips_the_memo_and_the_leftover_goes_on(self):
        rows = [_debit(D(2025, 10, 1), 400.0, "0001/2025/00001"),
                _debit(D(2025, 11, 1), 300.0, "0001/2025/00002", history_id=9),
                _credit(D(2026, 11, 21), 500.0, memo="0001/2025/00001", target=9)]
        mark_owed(rows)
        # 300 to the linked row; the memo does not apply; the rest is FIFO
        self.assertEqual(_owed(rows), [200.0, 0.0, 0.0])

    def test_missing_target_falls_back_to_the_memo(self):
        rows = [_debit(D(2025, 10, 1), 400.0, "0001/2025/00001"),
                _debit(D(2025, 11, 1), 300.0, "0001/2025/00002"),
                _credit(D(2026, 11, 21), 300.0, memo="0001/2025/00001", target=99)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [100.0, 300.0, 0.0])


class _OnlineMixin:

    def _history(self, number, amount, day="2025-11-01", partner=None):
        return self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": (partner or self.partner).id, "fecha": day, "comprobante": number,
            "tipo": "factura", "debe": amount})

    def _lab_invoice(self, amount, day="2026-01-10", name=None):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id, "invoice_date": day,
            "invoice_line_ids": [Command.create({
                "name": "deuda", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        if name:
            invoice.name = name
        invoice.action_post()
        return invoice

    def _cash(self, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cobrador)

    def _row(self, history):
        return next(r for r in self.partner._distri_statement_rows()
                    if r["history_id"] == history.id)

    def _balance(self):
        return self.partner._distri_statement()["balance"]


@tagged("post_install", "-at_install", "distri_v125")
class TestV125CobroOnline(_OnlineMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.cobrador = new_test_user(
            cls.env, login="cobrador_v125", name="Cobrador Online",
            groups="base.group_user", company_id=company.id,
            company_ids=[Command.set(company.ids)])
        cls.partner = cls.partner_a
        cls.env["distri.route"]._depot_journal(company)

    def test_the_list_is_the_statement(self):
        online = self._history("0001/2025/07001", 1200.0)
        paid = self._history("0001/2025/07002", 300.0, day="2025-11-05")
        self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": "2025-11-06", "comprobante": "CSH1/2025/0001",
            "tipo": "pago", "haber": 300.0, "concepto": "Pago 0001/2025/07002"})
        invoice = self._lab_invoice(500)
        account = self._cash().cash_account(self.partner.id)
        listed = {inv["value"]: inv["owed"] for inv in account["invoices"]}
        self.assertEqual(listed, {"online-%d" % online.id: 1200.0, str(invoice.id): 500.0})
        self.assertNotIn("online-%d" % paid.id, listed, "paid on the statement: not offered")
        first = account["invoices"][0]
        self.assertTrue(first["online"])
        self.assertEqual((first["name"], first["date"]), ("0001/2025/07001", "01/11/2025"))
        self.assertFalse(account["invoices"][1]["online"])
        # the same rows and amounts as the statement's red
        red = {(r["res_model"], r["res_id"]): r["owed"]
               for r in self.partner._distri_statement_rows() if r["owed"]}
        self.assertEqual(red, {("distri.cuenta.movimiento", online.id): 1200.0,
                               ("account.move", invoice.id): 500.0})

    def test_pay_an_online_invoice(self):
        online = self._history("0001/2025/07011", 1000.0)
        invoice = self._lab_invoice(800)
        before = self._balance()
        cash = self._cash().cash_register(self.partner.id, "1000", history_id=online.id,
                                          note="pagó en el local")
        payment = cash.payment_id
        self.assertFalse(cash.invoice_id)
        self.assertEqual(payment.distri_history_invoice_id, online)
        self.assertEqual(payment.memo, "Efectivo · cobró Cobrador Online · caja %s · pagó en el "
                         "local · 0001/2025/07011" % today_ar().strftime("%d/%m"))
        self.assertEqual(cash.memo, payment.memo)
        receivable = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        self.assertFalse(receivable.reconciled)
        self.assertFalse(receivable.matched_debit_ids, "never oldest first")
        self.assertEqual(invoice.amount_residual, 800, "no lab invoice reconciled")
        self.assertEqual(self._row(online)["owed"], 0.0)
        self.assertAlmostEqual(self._balance(), before - 1000)
        data = self.partner._distri_statement()
        self.assertEqual((data["owed_count"], data["owed_total"]), (1, 800.0))
        # not Online money for the caja (no lab_payment_id link)
        self.assertFalse(self.env["distri.caja"]._not_plain_cash(payment))
        self.assertIn("factura 0001/2025/07011 (Odoo Online)", self.partner.message_ids[:1].body)

    def test_partial_payment_keeps_the_rest_red(self):
        older = self._history("0001/2025/07020", 400.0, day="2025-10-01")
        online = self._history("0001/2025/07021", 1000.0)
        self._cash().cash_register(self.partner.id, "350", history_id=online.id)
        self.assertEqual(self._row(online)["owed"], 650.0)
        self.assertEqual(self._row(older)["owed"], 400.0, "not oldest first")
        listed = {inv["value"]: inv["owed"]
                  for inv in self._cash().cash_account(self.partner.id)["invoices"]}
        self.assertEqual(listed["online-%d" % online.id], 650.0)
        # more than what is left on that row: refused, like a lab invoice
        with self.assertRaisesRegex(UserError, "supera lo que falta pagar"):
            self._cash().cash_register(self.partner.id, "651", history_id=online.id)
        self._cash().cash_register(self.partner.id, "650", history_id=online.id)
        self.assertEqual(self._row(online)["owed"], 0.0)
        with self.assertRaisesRegex(UserError, "ya está pagada"):
            self._cash().cash_register(self.partner.id, "1", history_id=online.id)

    def test_l4_same_number_pays_only_the_online_one(self):
        number = "0001/2026/08053"
        online = self._history(number, 1000.0)
        invoice = self._lab_invoice(700, name=number)
        self.assertEqual(invoice.name, number)
        self._cash().cash_register(self.partner.id, "1000", history_id=online.id)
        self.assertEqual(self._row(online)["owed"], 0.0)
        self.assertEqual(invoice.amount_residual, 700)
        lab = next(r for r in self.partner._distri_statement_rows()
                   if r["res_model"] == "account.move" and r["res_id"] == invoice.id)
        self.assertEqual(lab["owed"], 700.0)

    def test_refusals(self):
        online = self._history("0001/2025/07031", 500.0)
        other = self._history("0001/2025/07032", 500.0, partner=self.partner_b)
        invoice = self._lab_invoice(500)
        with self.assertRaisesRegex(UserError, "no es de este cliente"):
            self._cash().cash_register(self.partner.id, "100", history_id=other.id)
        with self.assertRaisesRegex(UserError, "una sola factura"):
            self._cash().cash_register(self.partner.id, "100", invoice_id=invoice.id,
                                       history_id=online.id)
        with self.assertRaisesRegex(UserError, "supera lo que falta pagar"):
            self._cash().cash_register(self.partner.id, "600", history_id=online.id)
        self.assertFalse(self.env["account.payment"].sudo().search(
            [("distri_history_invoice_id", "!=", False)]))

    def test_double_submit_is_asked_per_target(self):
        online = self._history("0001/2025/07041", 1000.0)
        self._cash().cash_register(self.partner.id, "100", history_id=online.id)
        with self.assertRaisesRegex(DuplicateCashError, r"factura 0001/2025/07041 \(Odoo Online\)"):
            self._cash().cash_register(self.partner.id, "100", history_id=online.id)
        # «a cuenta» of the same amount is another payment
        self._cash().cash_register(self.partner.id, "100")

    def test_on_account_and_lab_invoice_unchanged(self):
        online = self._history("0001/2025/07051", 1000.0, day="2025-11-01")
        invoice = self._lab_invoice(500)
        on_account = self._cash().cash_register(self.partner.id, "150")
        self.assertTrue(on_account.memo.endswith("· a cuenta"))
        self.assertFalse(on_account.payment_id.distri_history_invoice_id)
        self.assertEqual(invoice.amount_residual, 350, "oldest lab invoice first, as before")
        chosen = self._cash().cash_register(self.partner.id, "350", invoice_id=invoice.id)
        self.assertEqual(invoice.amount_residual, 0)
        self.assertEqual(chosen.invoice_id, invoice)
        self.assertFalse(chosen.payment_id.distri_history_invoice_id)
        self.assertEqual(self._row(online)["owed"], 1000.0, "«a cuenta» reconciled to the lab one")


@tagged("post_install", "-at_install", "distri_v125", "distri_caja")
class TestV125CobroOnlineCaja(_OnlineMixin, CajaMixin, AccountTestInvoicingCommon):
    """The Online payment reaches the same box as any /cobro cash."""

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
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        cls.partner = cls.partner_a
        cls.cobrador = new_test_user(
            cls.env, login="no_cajero_v125", name="No Cajero",
            groups="base.group_user", company_id=company.id,
            company_ids=[Command.set(company.ids)])

    def _movements(self, payment):
        return self.env["distri.caja.movimiento"].sudo().search([("payment_id", "=", payment.id)])

    def test_cashier_caja_takes_it(self):
        caja = self._caja().open_session("0")
        online = self._history("0001/2025/07061", 5000.0)
        cash = self._cash(self.cashier).cash_register(self.partner.id, "5000",
                                                      history_id=online.id)
        payment = cash.payment_id
        self.assertEqual(cash.route_id.driver_id, self.cashier)
        # /caja syncs his own box: a «Cobro en el local», once
        caja.sudo()._sync_own_collections()
        caja.sudo()._sync_own_collections()
        move = self._movements(payment)
        self.assertEqual(len(move), 1)
        self.assertEqual((move.caja_id, move.kind, move.amount), (caja, "cobro", 5000))
        self.assertEqual(self._row(online)["owed"], 0.0)

    def test_non_cashier_keeps_his_depot_box(self):
        online = self._history("0001/2025/07071", 2000.0)
        cash = self._cash().cash_register(self.partner.id, "2000", history_id=online.id)
        self.assertEqual(cash.route_id.kind, "depot")
        self.assertEqual(cash.route_id.driver_id, self.cobrador)
        self.assertEqual(cash.payment_id.journal_id.code, DEPOT_JOURNAL_CODE)
        self.assertEqual(cash.route_id.cash_expected, 2000)
        self.assertFalse(self._movements(cash.payment_id))


@tagged("post_install", "-at_install", "distri_v125")
class TestV125CobroOnlineHttp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({
            "name": "KIOSCO ONLINE V125", "ref": "V125-HTTP", "customer_rank": 1})
        cls.online = cls.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.partner.id, "fecha": "2025-11-10", "comprobante": "0001/2025/07081",
            "tipo": "factura", "debe": 3000.0})
        new_test_user(cls.env, login="cobro_v125", name="Cobro V125", groups="base.group_user,distri_pedido.grupo_vendedor")

    def test_pay_online_from_the_page(self):
        self.authenticate("cobro_v125", "cobro_v125")
        page = self.url_open("/cobro?cliente=%d" % self.partner.id).text
        value = "online-%d" % self.online.id
        self.assertIn('value="%s"' % value, page)
        self.assertIn(">Online</em>", page)
        self.assertIn("A cuenta", page)
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        res = self.url_open("/cobro/registrar", allow_redirects=False, data={
            "csrf_token": csrf, "cliente": self.partner.id, "importe": "1000",
            "factura": value, "nota": ""})
        self.assertEqual(res.status_code, 303, res.text[:500])
        payment = self.env["account.payment"].sudo().search(
            [("partner_id", "=", self.partner.id)])
        self.assertEqual(payment.distri_history_invoice_id, self.online)
        self.assertTrue(payment.memo.endswith("· 0001/2025/07081"))
        # a forged id of another customer's history: refused, nothing created
        other = self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.env["res.partner"].create({"name": "OTRO V125"}).id,
            "fecha": "2025-11-10", "comprobante": "0001/2025/07082", "tipo": "factura",
            "debe": 3000.0})
        res = self.url_open("/cobro/registrar", data={
            "csrf_token": csrf, "cliente": self.partner.id, "importe": "500",
            "factura": "online-%d" % other.id, "nota": ""})
        self.assertEqual(res.status_code, 400)
        self.assertEqual(len(self.env["account.payment"].sudo().search(
            [("partner_id", "=", self.partner.id)])), 1)
