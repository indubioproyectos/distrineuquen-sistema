"""19.0.1.126.3: «pago dividido», pass 3b of the red rule (models/estado_cuenta.py
mark_owed, _split_payments).

After the same-amount pass and before FIFO, 2 or 3 open credits whose
remaining amounts add up exactly (to the cent) to an open debit, all dated
from the debit's day to 7 days after it, pay that debit. Smallest maximum
date gap first, then fewer credits, then the earliest; a full tie is skipped
and recorded as ambiguous. Real case: SALAZAR YANET LORENA, invoice
0001/2026/05777 paid the same day by PCSH1/2026/01889 and PBNK1/2026/03670.
Synthetic data only.
"""
import random
from datetime import date, timedelta

from odoo.tests import TransactionCase, tagged

from odoo.addons.distri_pedido.models.estado_cuenta import mark_owed
from odoo.addons.distri_pedido.tests.test_v123_facturas_rojo import _credit, _debit, _owed

D = date


@tagged("post_install", "-at_install", "distri_v126_3", "distri_v123_rojo")
class TestSplitPayment(TransactionCase):
    """The pure rule: pass 3b only."""

    def test_salazar_shape(self):
        rows = [_debit(D(2026, 9, 1), 120000.0, "0001/2026/05601"),
                _debit(D(2026, 9, 5), 60000.0, "0001/2026/05690"),
                _debit(D(2026, 9, 11), 145650.0, "0001/2026/05777"),
                _credit(D(2026, 9, 11), 70000.0, "PCSH1/2026/01889", memo="EFECTIVO: SIMON"),
                _credit(D(2026, 9, 11), 75650.0, "PBNK1/2026/03670",
                        memo="Manual Payment: SIMON"),
                _credit(D(2026, 9, 20), 90000.0, "PBNK1/2026/03801")]
        stats = {}
        count, total = mark_owed(rows, stats)
        # FIFO alone sent both to 05601/05690 and left 90.000 red on 05777
        self.assertEqual(_owed(rows), [30000.0, 60000.0, 0.0, 0.0, 0.0, 0.0])
        balance = sum(r["debit"] - r["credit"] for r in rows)
        self.assertEqual((count, total), (2, round(balance, 2)))
        self.assertEqual(len(stats["split"]), 1)
        debit, credits = stats["split"][0]
        self.assertEqual(debit["number"], "0001/2026/05777")
        self.assertEqual([c["number"] for c in credits],
                         ["PCSH1/2026/01889", "PBNK1/2026/03670"])
        self.assertFalse(stats.get("ambiguous"))

    def test_three_credits_within_seven_days(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "OLD"),
                _debit(D(2026, 1, 10), 300.0, "INV"),
                _credit(D(2026, 1, 10), 100.0),
                _credit(D(2026, 1, 12), 120.0),
                _credit(D(2026, 1, 17), 80.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [1000.0, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual(len(stats["split"][0][1]), 3)

    def test_a_credit_on_day_eight_is_not_used(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "OLD"),
                _debit(D(2026, 1, 10), 300.0, "INV"),
                _credit(D(2026, 1, 10), 150.0),
                _credit(D(2026, 1, 18), 150.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [700.0, 300.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))

    def test_a_credit_before_the_invoice_is_not_used(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "OLD"),
                _credit(D(2026, 1, 9), 150.0),
                _debit(D(2026, 1, 10), 300.0, "INV"),
                _credit(D(2026, 1, 10), 150.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [700.0, 0.0, 300.0, 0.0])
        self.assertFalse(stats.get("split"))

    def test_an_inexact_sum_is_not_used(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "OLD"),
                _debit(D(2026, 1, 10), 300.0, "INV"),
                _credit(D(2026, 1, 10), 150.0),
                _credit(D(2026, 1, 10), 149.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [701.0, 300.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))
        # one cent off is off too
        rows[3]["credit"] = 149.99
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [700.01, 300.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))

    def test_a_full_tie_is_skipped(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "OLD"),
                _debit(D(2026, 1, 10), 300.0, "INV"),
                _credit(D(2026, 1, 10), 100.0),
                _credit(D(2026, 1, 10), 200.0),
                _credit(D(2026, 1, 10), 200.0)]
        stats = {}
        mark_owed(rows, stats)
        # 100 + either 200: same gap, size and dates; no guess, FIFO decides
        self.assertEqual(_owed(rows), [500.0, 300.0, 0.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))
        self.assertEqual([r["number"] for r in stats["ambiguous"]], ["INV"])

    def test_same_day_then_fewer_credits_win(self):
        rows = [_debit(D(2026, 1, 1), 1000.0, "OLD"),
                _debit(D(2026, 1, 10), 300.0, "INV"),
                _credit(D(2026, 1, 10), 100.0),
                _credit(D(2026, 1, 10), 100.0),
                _credit(D(2026, 1, 10), 100.0),
                _credit(D(2026, 1, 10), 170.0),
                _credit(D(2026, 1, 10), 130.0),
                _credit(D(2026, 1, 12), 180.0),
                _credit(D(2026, 1, 12), 120.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual([c["credit"] for c in stats["split"][0][1]], [170.0, 130.0])
        self.assertEqual(_owed(rows)[1], 0.0)

    def test_reconciled_and_memo_credits_keep_priority(self):
        rows = [_debit(D(2026, 1, 10), 300.0, "0001/2026/00001", line_id=1),
                _debit(D(2026, 1, 10), 500.0, "0001/2026/00002", line_id=2),
                _credit(D(2026, 1, 10), 200.0),
                _credit(D(2026, 1, 10), 300.0, memo="EFECTIVO: 0001/2026/00001")]
        stats = {}
        mark_owed(rows, stats)
        # 200 + 300 would sum 00002, but the 300 already paid 00001 by memo
        self.assertEqual(_owed(rows), [0.0, 300.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))
        rows[3]["memo"] = ""
        rows[3]["reconciled"] = [(1, 300.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [0.0, 300.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))

    def test_same_amount_keeps_priority(self):
        rows = [_debit(D(2026, 1, 10), 300.0, "A"),
                _debit(D(2026, 1, 10), 500.0, "B"),
                _credit(D(2026, 1, 10), 200.0),
                _credit(D(2026, 1, 10), 300.0)]
        stats = {}
        mark_owed(rows, stats)
        self.assertEqual(_owed(rows), [0.0, 300.0, 0.0, 0.0])
        self.assertFalse(stats.get("split"))

    def test_sum_of_red_equals_the_positive_balance(self):
        rng = random.Random(1263)
        splits = 0
        for _ in range(400):
            rows, numbers = [], []
            for i in range(rng.randint(0, 18)):
                day = D(2026, 1, 1) + timedelta(days=rng.randint(0, 40))
                amount = rng.choice((100.0, 250.5, rng.randint(1, 900000) / 100))
                if rng.random() < 0.5:
                    number = "0001/2026/%05d" % (10000 + i)
                    numbers.append((number, i + 1, amount))
                    rows.append(_debit(day, amount, number, line_id=i + 1))
                    if rng.random() < 0.4:
                        # a split payment, sometimes a cent off or late
                        part = round(amount * rng.uniform(0.2, 0.8), 2)
                        rest = round(amount - part + rng.choice((0, 0, 0, 0.01)), 2)
                        for value in (part, rest):
                            if value > 0:
                                late = day + timedelta(days=rng.randint(0, 9))
                                rows.append(_credit(late, value))
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
            stats = {}
            _count, total = mark_owed(rows, stats)
            self.assertAlmostEqual(total, max(balance, 0.0), places=1)
            self.assertAlmostEqual(sum(_owed(rows)), max(balance, 0.0), places=1)
            self.assertTrue(all(r["owed"] <= max(r["debit"] - r["credit"], 0) + 0.01
                                for r in rows))
            used = []
            for debit, credits in stats.get("split", []):
                splits += 1
                self.assertIn(len(credits), (2, 3))
                self.assertEqual(debit["owed"], 0.0)
                for credit in credits:
                    self.assertTrue(0 <= (credit["date"] - debit["date"]).days <= 7)
                used.extend(id(c) for c in credits)
            self.assertEqual(len(used), len(set(used)), "a credit split twice")
        self.assertGreater(splits, 20, "the generator exercises the pass")
