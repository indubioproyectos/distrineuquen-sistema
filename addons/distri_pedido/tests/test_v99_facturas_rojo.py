"""19.0.1.99.0: red invoices, FIFO (models/estado_cuenta.py mark_owed_fifo).

Every credit of the customer (payments, credit notes, refunds, reported
returns) covers his debit rows oldest to newest; what stays uncovered is red
(«debe $ X» when covered in part). The red total equals the balance; a
customer up to date or in credit has no red; the review marks
(distri_owed*) no longer decide the color, their note stays on the row.
Both surfaces: the backend statement and /cuentas. Synthetic data only.
"""
import os
from datetime import date

from odoo import Command
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.estado_cuenta import mark_owed_fifo


def _row(debit=0.0, credit=0.0):
    return {"debit": debit, "credit": credit}


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_rojo")
class TestMarkOwedFifo(TransactionCase):
    """The pure rule."""

    def test_payment_covers_the_oldest_and_leaves_the_newest_red(self):
        rows = [_row(1000), _row(500), _row(credit=1200), _row(800)]
        count, total = mark_owed_fifo(rows)
        self.assertEqual([r["owed"] for r in rows], [0.0, 300.0, 0.0, 800.0])
        self.assertEqual((count, total), (2, 1100.0))
        self.assertEqual(total, sum(r["debit"] - r["credit"] for r in rows))

    def test_partial_cover_shows_what_is_owed(self):
        rows = [_row(1500), _row(credit=400)]
        mark_owed_fifo(rows)
        self.assertEqual(rows[0]["owed"], 1100.0)

    def test_up_to_date_or_in_credit_has_no_red(self):
        for rows in ([_row(1000), _row(credit=1000)],
                     [_row(1000), _row(credit=1500), _row(200)],
                     [_row(credit=50)], []):
            count, total = mark_owed_fifo(rows)
            self.assertEqual((count, total), (0, 0.0), rows)
            self.assertFalse(any(r["owed"] for r in rows))

    def test_payment_before_the_invoice_still_covers_it(self):
        rows = [_row(credit=700), _row(1000)]
        mark_owed_fifo(rows)
        self.assertEqual(rows[1]["owed"], 300.0)

    def test_cents_tolerance(self):
        rows = [_row(100.004), _row(credit=100.0)]
        count, _total = mark_owed_fifo(rows)
        self.assertEqual(count, 0)

    def test_sum_of_red_equals_the_positive_balance(self):
        import random
        rng = random.Random(99)
        for _ in range(200):
            rows = [_row(rng.choice((0, rng.randint(1, 900000) / 100)),
                         rng.choice((0, rng.randint(1, 900000) / 100))) for _ in range(rng.randint(0, 15))]
            balance = sum(r["debit"] - r["credit"] for r in rows)
            _count, total = mark_owed_fifo(rows)
            self.assertAlmostEqual(total, max(balance, 0.0), places=1)
            self.assertAlmostEqual(sum(r["owed"] for r in rows), max(balance, 0.0), places=1)
            self.assertTrue(all(r["owed"] <= max(r["debit"] - r["credit"], 0) + 0.01 for r in rows))


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_rojo")
class TestStatementRed(TransactionCase):
    """The rule on the customer's statement (history + this Odoo)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "KIOSCO ROJO V99"})
        Mov = cls.env["distri.cuenta.movimiento"].sudo()
        cls.rows = Mov.create([
            {"partner_id": cls.partner.id, "fecha": "2026-08-01", "comprobante": "0001/2026/99901",
             "tipo": "factura", "debe": 1000.0,
             "distri_owed": True, "distri_owed_note": "el chofer no la cobró el 01/08"},
            {"partner_id": cls.partner.id, "fecha": "2026-08-10", "comprobante": "0001/2026/99902",
             "tipo": "factura", "debe": 600.0},
            # a credit note of the history: tipo «factura» with only «haber»
            {"partner_id": cls.partner.id, "fecha": "2026-08-15", "comprobante": "0001/2026/99903",
             "tipo": "factura", "haber": 100.0},
            {"partner_id": cls.partner.id, "fecha": "2026-09-01", "comprobante": "PBNK1/2026/99904",
             "tipo": "pago", "haber": 1200.0},
            {"partner_id": cls.partner.id, "fecha": "2026-10-01", "comprobante": "0001/2026/99905",
             "tipo": "factura", "debe": 900.0},
        ])

    def _red(self, partner=None):
        rows = (partner or self.partner)._distri_statement_rows()
        return {r["number"]: r["owed"] for r in rows if r["owed"]}, rows

    def test_fifo_on_the_statement(self):
        red, rows = self._red()
        # 1000 + 600 + 900 - 100 - 1200 = 1200: the first invoice is paid,
        # the second keeps 300 and the newest is all owed
        self.assertEqual(red, {"0001/2026/99902": 300.0, "0001/2026/99905": 900.0})
        balance = self.partner._distri_account_balance()
        self.assertAlmostEqual(sum(red.values()), balance)
        first = next(r for r in rows if r["number"] == "0001/2026/99901")
        self.assertEqual(first["owed"], 0.0, "the review mark no longer paints it")
        self.assertEqual(first["review_note"], "el chofer no la cobró el 01/08")
        data = self.partner._distri_statement()
        self.assertEqual((data["owed_count"], data["owed_total"]), (2, 1200.0))

    def test_backend_table(self):
        html = str(self.partner.distri_statement_html)
        self.assertIn("Facturas adeudadas: 2 por $ 1.200", html)
        self.assertNotIn("revisadas", html)
        self.assertEqual(html.count("o_distri_estado_adeudada'"), 2)
        self.assertIn("debe $ 300", html)
        self.assertIn("debe $ 900", html)
        self.assertIn("Revisión: el chofer no la cobró el 01/08", html)

    def test_date_filter_does_not_change_the_red(self):
        data = self.partner._distri_statement(date(2026, 9, 15), False)
        self.assertEqual([r["number"] for r in data["rows"]], ["0001/2026/99905"])
        self.assertEqual(data["rows"][0]["owed"], 900.0)
        # counted over the whole statement, not the period
        self.assertEqual((data["owed_count"], data["owed_total"]), (2, 1200.0))
        wizard = self.env["distri.estado.cuenta"].create({
            "partner_id": self.partner.id, "date_from": "2026-09-15"})
        self.assertIn("Facturas adeudadas: 2 por $ 1.200", str(wizard.statement_html))

    def test_a_new_payment_corrects_the_red(self):
        self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": "2026-11-01", "comprobante": "PBNK1/2026/99906",
            "tipo": "pago", "haber": 1000.0})
        red, _rows = self._red()
        self.assertEqual(red, {"0001/2026/99905": 200.0})
        self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": "2026-11-02", "comprobante": "PBNK1/2026/99907",
            "tipo": "pago", "haber": 500.0})
        red, _rows = self._red()
        self.assertEqual(red, {}, "in credit: nothing red")
        self.assertNotIn("table-danger", str(self.partner._distri_statement_table(
            self.partner._distri_statement())))

    def test_review_mark_on_a_paid_customer_paints_nothing(self):
        partner = self.env["res.partner"].create({"name": "KIOSCO AL DIA V99"})
        self.env["distri.cuenta.movimiento"].sudo().create([
            {"partner_id": partner.id, "fecha": "2026-08-01", "comprobante": "0001/2026/99911",
             "tipo": "factura", "debe": 184000.0, "distri_owed": True,
             "distri_owed_amount": 184000.0},
            {"partner_id": partner.id, "fecha": "2026-08-06", "comprobante": "0001/2026/99912",
             "tipo": "factura", "haber": 184000.0},
        ])
        red, rows = self._red(partner)
        self.assertEqual(red, {})
        self.assertEqual(rows[0]["review_note"], "Marcada adeudada en la revisión")


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_rojo")
class TestStatementRedOdoo(AccountTestInvoicingCommon):

    def test_odoo_invoice_and_history_on_one_customer(self):
        """A posted invoice of this Odoo takes its place in the same order."""
        partner = self.env["res.partner"].create({"name": "KIOSCO MIXTO V99"})
        self.env["distri.cuenta.movimiento"].sudo().create([
            {"partner_id": partner.id, "fecha": "2026-01-01", "comprobante": "0001/2026/99921",
             "tipo": "factura", "debe": 500.0},
            {"partner_id": partner.id, "fecha": "2026-01-05", "comprobante": "PBNK1/2026/99922",
             "tipo": "pago", "haber": 700.0},
        ])
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": "2026-02-01",
            "invoice_line_ids": [Command.create({"name": "mercadería", "quantity": 1,
                                                 "price_unit": 1000, "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        rows = partner._distri_statement_rows()
        red = {r["number"]: r["owed"] for r in rows if r["owed"]}
        self.assertEqual(red, {invoice.name: 800.0})
        self.assertAlmostEqual(sum(red.values()), partner._distri_account_balance())


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_rojo")
class TestCuentasRed(TransactionCase):
    """/cuentas reads the same rule."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_u = new_test_user(cls.env, login="admin_rojo_v99", name="Admin Rojo V99",
                                    groups="base.group_user,distri_pedido.grupo_administracion")
        cls.partner = cls.env["res.partner"].create({"name": "KIOSCO CUENTAS ROJO V99"})
        cls.env["distri.cuenta.movimiento"].sudo().create([
            {"partner_id": cls.partner.id, "fecha": "2025-01-01", "comprobante": "0001/2025/99931",
             "tipo": "factura", "debe": 2000.0, "distri_owed": True,
             "distri_owed_note": "revisada en noviembre", "origen_id": 9993101},
            {"partner_id": cls.partner.id, "fecha": "2025-02-01", "comprobante": "PBNK1/2025/99932",
             "tipo": "pago", "haber": 1500.0, "origen_id": 9993102},
            {"partner_id": cls.partner.id, "fecha": "2026-11-01", "comprobante": "0001/2026/99933",
             "tipo": "factura", "debe": 400.0, "origen_id": 9993103},
        ])

    def test_account_screen(self):
        app = self.env["distri.account.app"].with_user(self.admin_u)
        # three months: the old invoice is out of the period, its red still counts
        data = app.app_account(self.partner.id)
        self.assertEqual(data["owed_count"], 2)
        self.assertAlmostEqual(data["owed_total"], 900.0)
        self.assertAlmostEqual(data["owed_total"], data["balance"])
        shown = {r["number"]: r["owed"] for r in data["rows"]}
        self.assertEqual(shown, {"0001/2026/99933": 400.0})
        full = app.app_account(self.partner.id, period="all")
        old = next(r for r in full["rows"] if r["number"] == "0001/2025/99931")
        self.assertEqual(old["owed"], 500.0)
        self.assertEqual(old["review_note"], "revisada en noviembre")
        line = app.app_line(self.partner.id, "online:9993101")
        self.assertEqual((line["owed"], line["review_note"]), (500.0, "revisada en noviembre"))

    def test_heading_text(self):
        # 19.0.1.113.0: the page's script is cuentas_app.js (cuentas.js is retired)
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "cuentas",
                            "cuentas_app.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertNotIn("(revisadas)", source)
        self.assertIn("Facturas adeudadas", source)
