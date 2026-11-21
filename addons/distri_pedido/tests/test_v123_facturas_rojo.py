"""19.0.1.123.0: red invoices by priority passes (models/estado_cuenta.py mark_owed).

Credits cover debits first by reconciliation, then by the invoice a memo
names, then by an exact amount nearest in date, and last oldest first. A
paid invoice no longer stays red because an older unpaid one took its
payment. The red total still equals the balance. Synthetic data only.
"""
import random
from datetime import date

from odoo import Command
from odoo.tests import TransactionCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.estado_cuenta import _memo_refs, mark_owed


def _debit(day, amount, number="", line_id=False):
    return {"date": day, "number": number, "debit": amount, "credit": 0.0,
            "line_id": line_id, "reconciled": [], "memo": ""}


def _credit(day, amount, number="", memo="", reconciled=()):
    return {"date": day, "number": number, "debit": 0.0, "credit": amount,
            "line_id": False, "reconciled": list(reconciled), "memo": memo}


def _owed(rows):
    return [r["owed"] for r in rows]


D = date


@tagged("post_install", "-at_install", "distri_v123", "distri_v123_rojo")
class TestMarkOwed(TransactionCase):
    """The pure rule, one pass at a time."""

    def test_reconciliation_beats_fifo(self):
        rows = [_debit(D(2026, 1, 1), 500.0, line_id=11),
                _debit(D(2026, 1, 5), 700.0, line_id=12),
                _credit(D(2026, 1, 9), 900.0, reconciled=[(12, 700.0)])]
        count, total = mark_owed(rows)
        # FIFO would leave the second invoice with 300; it is the one paid
        self.assertEqual(_owed(rows), [300.0, 0.0, 0.0])
        self.assertEqual((count, total), (1, 300.0))

    def test_memo_names_the_invoice(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "0001/2026/00001"),
                _debit(D(2026, 1, 5), 800.0, "0001/2026/00002"),
                _credit(D(2026, 1, 9), 800.0, "PCSH1/2026/00010",
                        memo="Efectivo · cobró SIMON · planilla 12/11 · 0001/2026/00001")]
        mark_owed(rows)
        # the memo wins over the equal amount of the second invoice
        self.assertEqual(_owed(rows), [200.0, 800.0, 0.0])

    def test_online_label_names_the_invoice(self):
        rows = [_debit(D(2026, 1, 1), 300.0, "0001/2026/00001"),
                _debit(D(2026, 1, 5), 300.0, "0001/2026/00002"),
                _credit(D(2026, 1, 9), 300.0, "CSH1/2026/00011",
                        memo="Manual Payment: 0001/2026/00001")]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 300.0, 0.0])

    def test_tail_match_ignores_sale_orders(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "0001/2026/05616"),
                _debit(D(2026, 1, 5), 1000.0, "0001/2026/00241"),
                _credit(D(2026, 1, 9), 1000.0, "PCSH1/2026/01830", memo="S00241 fact 05616")]
        mark_owed(rows)
        # «S00241» is an order, not the tail of 0001/2026/00241
        self.assertEqual(_owed(rows), [0.0, 1000.0, 0.0])

    def test_memo_refs(self):
        self.assertEqual(_memo_refs("S00241"), [])
        self.assertEqual(_memo_refs("0001/2026/06798 MP 123456789012"),
                         [("full", "0001/2026/06798")])
        self.assertEqual(_memo_refs("EFECTIVO: 0001/2026/08157"), [("full", "0001/2026/08157")])
        self.assertEqual(_memo_refs("pago 05616."), [("tail", "05616")])
        self.assertEqual(_memo_refs("$ 12345,50 planilla 12/11"), [])

    def test_same_amount_prefers_the_nearest_debit_before(self):
        rows = [_debit(D(2026, 1, 1), 500.0, "A"),
                _debit(D(2026, 1, 2), 300.0, "B"),
                _debit(D(2026, 1, 5), 500.0, "C"),
                _credit(D(2026, 1, 6), 500.0)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [500.0, 300.0, 0.0, 0.0])

    def test_same_amount_takes_the_nearest_after_when_none_before(self):
        rows = [_credit(D(2026, 1, 1), 500.0),
                _debit(D(2026, 1, 2), 300.0, "A"),
                _debit(D(2026, 1, 5), 500.0, "B"),
                _debit(D(2026, 1, 9), 500.0, "C")]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 300.0, 0.0, 500.0])

    def test_same_amount_tie_takes_the_oldest(self):
        rows = [_debit(D(2026, 1, 5), 500.0, "A"),
                _debit(D(2026, 1, 5), 500.0, "B"),
                _credit(D(2026, 1, 6), 500.0)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 500.0, 0.0])

    def test_fifo_takes_the_leftover(self):
        rows = [_debit(D(2026, 1, 1), 500.0), _debit(D(2026, 1, 2), 400.0),
                _credit(D(2026, 1, 3), 700.0), _debit(D(2026, 1, 4), 100.0)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 200.0, 0.0, 100.0])

    def test_a_newly_reconciled_payment_clears_its_invoice(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, line_id=1),
                _debit(D(2026, 1, 3), 1000.0, line_id=2),
                _credit(D(2026, 1, 4), 1000.0)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [1000.0, 0.0, 0.0], "same amount: the nearest")
        rows[2]["reconciled"] = [(1, 1000.0)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 1000.0, 0.0])

    def test_net_zero_rows_and_credit_balance(self):
        rows = [{"date": D(2026, 1, 1), "number": "X", "debit": 50.0, "credit": 50.0},
                _debit(D(2026, 1, 2), 100.0), _credit(D(2026, 1, 3), 150.0)]
        self.assertEqual(mark_owed(rows), (0, 0.0))
        self.assertFalse(any(_owed(rows)))
        self.assertEqual(mark_owed([]), (0, 0.0))

    def test_sum_of_red_equals_the_positive_balance(self):
        rng = random.Random(123)
        for _ in range(300):
            rows, numbers = [], []
            for i in range(rng.randint(0, 15)):
                day = D(2026, 1, 1 + rng.randint(0, 27))
                amount = rng.choice((100.0, 250.5, rng.randint(1, 900000) / 100))
                if rng.random() < 0.55:
                    number = "0001/2026/%05d" % (10000 + i)
                    numbers.append((number, i + 1, amount))
                    rows.append(_debit(day, amount, number, line_id=i + 1))
                else:
                    memo, reconciled = "", []
                    if numbers and rng.random() < 0.4:
                        memo = "EFECTIVO: %s" % rng.choice(numbers)[0]
                    if numbers and rng.random() < 0.3:
                        _n, line_id, debit = rng.choice(numbers)
                        reconciled = [(line_id, min(debit, amount))]
                    rows.append(_credit(day, amount, memo=memo, reconciled=reconciled))
            rows.sort(key=lambda r: r["date"])
            balance = sum(r["debit"] - r["credit"] for r in rows)
            _count, total = mark_owed(rows)
            self.assertAlmostEqual(total, max(balance, 0.0), places=1)
            self.assertAlmostEqual(sum(_owed(rows)), max(balance, 0.0), places=1)
            self.assertTrue(all(r["owed"] <= max(r["debit"] - r["credit"], 0) + 0.01
                                for r in rows))


@tagged("post_install", "-at_install", "distri_v123", "distri_v123_rojo")
class TestStatementRedPasses(AccountTestInvoicingCommon):
    """The Canaviri shape on a real statement (Online history + this Odoo)."""

    def _invoice(self, partner, day, amount):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": day,
            "invoice_line_ids": [Command.create({"name": "mercadería", "quantity": 1,
                                                 "price_unit": amount,
                                                 "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def test_canaviri_shape(self):
        partner = self.env["res.partner"].create({"name": "KIOSCO CANAVIRI V123"})
        self.env["distri.cuenta.movimiento"].sudo().create([
            {"partner_id": partner.id, "fecha": "2026-03-01", "comprobante": "0001/2026/91001",
             "tipo": "factura", "debe": 50000.0},
            {"partner_id": partner.id, "fecha": "2026-03-03", "comprobante": "PCSH1/2026/91101",
             "tipo": "pago", "haber": 50000.0, "concepto": "EFECTIVO"},
            {"partner_id": partner.id, "fecha": "2026-03-05", "comprobante": "0001/2026/91002",
             "tipo": "factura", "debe": 110300.0},
            {"partner_id": partner.id, "fecha": "2026-03-10", "comprobante": "0001/2026/91003",
             "tipo": "factura", "debe": 80000.0},
            {"partner_id": partner.id, "fecha": "2026-03-12", "comprobante": "CSH1/2026/91102",
             "tipo": "pago", "haber": 80000.0, "concepto": "Manual Payment: 0001/2026/91003"},
            {"partner_id": partner.id, "fecha": "2026-04-01", "comprobante": "0001/2026/91004",
             "tipo": "factura", "debe": 65000.0},
            {"partner_id": partner.id, "fecha": "2026-04-03", "comprobante": "PCSH1/2026/91103",
             "tipo": "pago", "haber": 65000.0, "concepto": "EFECTIVO: 0001/2026/91004"},
        ])
        reconciled = self._invoice(partner, "2026-11-01", 124800.0)
        unpaid = self._invoice(partner, "2026-11-05", 124800.0)
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=reconciled.ids).create({
                "journal_id": self.company_data["default_journal_cash"].id,
                "payment_date": "2026-11-06",
                "communication": "Efectivo · cobró SIMON",
            }).action_create_payments()
        self.assertIn(reconciled.payment_state, ("paid", "in_payment"))

        rows = partner._distri_statement_rows()
        red = {r["number"]: r["owed"] for r in rows if r["owed"]}
        self.assertEqual(red, {"0001/2026/91002": 110300.0, unpaid.name: 124800.0})
        self.assertNotIn(reconciled.name, red)
        self.assertAlmostEqual(sum(red.values()), partner._distri_account_balance())
        data = partner._distri_statement()
        self.assertEqual((data["owed_count"], data["owed_total"]), (2, 235100.0))

        # without the reconciliation the equal amount would pick the nearer
        # unpaid invoice: pass 1 is what keeps the paid one out of the red
        for row in rows:
            row["reconciled"] = []
        mark_owed(rows)
        red = {r["number"]: r["owed"] for r in rows if r["owed"]}
        self.assertIn(reconciled.name, red)
        self.assertNotIn(unpaid.name, red)
