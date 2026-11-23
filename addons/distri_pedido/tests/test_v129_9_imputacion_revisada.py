"""19.0.1.129.9: «Imputación revisada» (models/imputacion_revisada.py), pass 0
of the red rule (models/estado_cuenta.py mark_owed).

The reviewed allocations of a customer (credit line_key, debit line_key,
amount) are applied before every other pass, each up to what both rows
still have; unknown keys or a wrong side are skipped and counted. The red
total still equals the balance. Staff see «Revisado: <reason>»; the
customer never does. Synthetic data only.
"""
import random
from datetime import date

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.estado_cuenta import mark_owed, share_token

D = date


def _debit(key, day, amount, line_id=False):
    return {"line_key": key, "date": day, "number": key, "debit": amount, "credit": 0.0,
            "line_id": line_id, "reconciled": [], "memo": ""}


def _credit(key, day, amount, reconciled=()):
    return {"line_key": key, "date": day, "number": key, "debit": 0.0, "credit": amount,
            "line_id": False, "reconciled": list(reconciled), "memo": ""}


def _alloc(credit_key, debit_key, amount, reason="", alloc_id=1):
    return {"id": alloc_id, "credit_key": credit_key, "debit_key": debit_key,
            "amount": amount, "reason": reason}


def _owed(rows):
    return [r["owed"] for r in rows]


def _balance(rows):
    return round(sum(r["debit"] - r["credit"] for r in rows), 2)


@tagged("post_install", "-at_install", "distri_v129_9")
class TestPassZero(TransactionCase):
    """The pure rule."""

    def test_overrides_fifo(self):
        rows = [_debit("inv:a", D(2026, 1, 1), 1000.0),
                _debit("inv:b", D(2026, 1, 20), 800.0),
                _credit("pay:1", D(2026, 2, 15), 500.0)]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [500.0, 800.0, 0.0], "FIFO without a review")
        stats = {}
        count, total = mark_owed(rows, stats, allocations=[
            _alloc("pay:1", "inv:b", 500.0, "pago por transferencia de B")])
        self.assertEqual(_owed(rows), [1000.0, 300.0, 0.0])
        self.assertEqual((count, total), (2, 1300.0))
        self.assertEqual(rows[1]["reviewed_reasons"], ["pago por transferencia de B"])
        self.assertEqual(rows[0]["reviewed_reasons"], [])
        self.assertEqual([amount for _a, amount in stats["allocated"]], [500.0])

    def test_overrides_a_wrong_reconciliation(self):
        rows = [_debit("inv:a", D(2026, 1, 1), 700.0, line_id=11),
                _debit("inv:b", D(2026, 1, 5), 700.0, line_id=12),
                _credit("pay:1", D(2026, 1, 9), 700.0, reconciled=[(11, 700.0)])]
        mark_owed(rows)
        self.assertEqual(_owed(rows), [0.0, 700.0, 0.0])
        mark_owed(rows, allocations=[_alloc("pay:1", "inv:b", 700.0)])
        # the reconciliation finds nothing left: no over-allocation
        self.assertEqual(_owed(rows), [700.0, 0.0, 0.0])

    def test_respects_remaining_amounts(self):
        rows = [_debit("inv:a", D(2026, 1, 1), 300.0),
                _debit("inv:b", D(2026, 1, 5), 800.0),
                _credit("pay:1", D(2026, 1, 9), 1000.0),
                _credit("pay:2", D(2026, 1, 10), 50.0)]
        stats = {}
        mark_owed(rows, stats, allocations=[
            # more than the debit owes: only 800
            _alloc("pay:1", "inv:b", 900.0, alloc_id=1),
            # more than the credit has left (200): only 200
            _alloc("pay:1", "inv:a", 500.0, alloc_id=2),
            # the credit is spent
            _alloc("pay:1", "inv:a", 10.0, alloc_id=3),
        ])
        self.assertEqual([(a["id"], amount) for a, amount in stats["allocated"]],
                         [(1, 800.0), (2, 200.0)])
        self.assertEqual([(a["id"], why) for a, why in stats["allocation_skipped"]],
                         [(3, "spent")])
        # pay:2 (50) goes on by the other passes
        self.assertEqual(_owed(rows), [50.0, 0.0, 0.0, 0.0])
        self.assertEqual(sum(_owed(rows)), _balance(rows))

    def test_unknown_keys_and_wrong_side_are_skipped(self):
        rows = [_debit("inv:a", D(2026, 1, 1), 1000.0),
                _debit("inv:b", D(2026, 1, 20), 800.0),
                _credit("pay:1", D(2026, 2, 15), 500.0)]
        stats = {}
        mark_owed(rows, stats, allocations=[
            _alloc("pay:404", "inv:b", 500.0, alloc_id=1),
            _alloc("pay:1", "inv:404", 500.0, alloc_id=2),
            _alloc("inv:a", "inv:b", 500.0, alloc_id=3),
            _alloc("pay:1", "pay:1", 500.0, alloc_id=4),
        ])
        self.assertEqual([(a["id"], why) for a, why in stats["allocation_skipped"]],
                         [(1, "missing"), (2, "missing"), (3, "not_credit"), (4, "not_debit")])
        self.assertFalse(stats.get("allocated"))
        # untouched: FIFO as before
        self.assertEqual(_owed(rows), [500.0, 800.0, 0.0])

    def test_invariant_holds_with_random_allocations(self):
        rng = random.Random(1299)
        for _round in range(200):
            rows, debit_keys, credit_keys = [], [], []
            for n in range(rng.randint(1, 12)):
                day = D(2026, rng.randint(1, 12), rng.randint(1, 28))
                amount = round(rng.uniform(1, 5000), 2)
                if rng.random() < 0.55:
                    rows.append(_debit("d:%d" % n, day, amount, line_id=n + 1))
                    debit_keys.append("d:%d" % n)
                else:
                    rows.append(_credit("c:%d" % n, day, amount))
                    credit_keys.append("c:%d" % n)
            for row in rows:
                if row["credit"] and debit_keys and rng.random() < 0.5:
                    row["reconciled"] = [(rng.randint(1, 12), round(rng.uniform(1, 6000), 2))]
            keys = debit_keys + credit_keys + ["x:1"]
            allocations = [_alloc(rng.choice(keys), rng.choice(keys),
                                  round(rng.uniform(1, 6000), 2), alloc_id=i)
                           for i in range(rng.randint(0, 8))]
            rows.sort(key=lambda r: r["date"])
            count, total = mark_owed(rows, {}, allocations=allocations)
            self.assertAlmostEqual(total, max(_balance(rows), 0.0), places=2)
            self.assertTrue(all(r["owed"] >= 0 for r in rows))
            self.assertTrue(all(r["owed"] <= r["debit"] + 0.005 for r in rows))


@tagged("post_install", "-at_install", "distri_v129_9")
class TestImputacionRevisada(HttpCase):
    """The stored allocations on the real statement, the views and the ACL."""

    REASON = "MOTIVO V1299 secreto 0001/2026/129901"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.tag = cls.env["res.partner.category"].create({"name": "Cartera V1299"})

        def user(login, groups):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups=groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            new.etiqueta_cartera_id = cls.tag
            return new

        cls.admin_u = user("v1299_admin", "base.group_user,distri_pedido.grupo_administracion")
        cls.seller = user("v1299_vendedor", "base.group_user,distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({
            "name": "IMPUTACION V1299", "customer_rank": 1,
            "category_id": [Command.link(cls.tag.id)]})
        cls.env["distri.cuenta.movimiento"].sudo().create([
            dict(partner_id=cls.partner.id, fecha="2026-01-10", comprobante="0001/2026/129901",
                 tipo="factura", debe=1000.0, haber=0.0, origen_id=129901),
            dict(partner_id=cls.partner.id, fecha="2026-02-10", comprobante="0001/2026/129902",
                 tipo="factura", debe=500.0, haber=0.0, origen_id=129902),
            dict(partner_id=cls.partner.id, fecha="2026-03-20", comprobante="PBNK1/2026/129903",
                 tipo="pago", debe=0.0, haber=500.0, origen_id=129903),
        ])
        cls.Alloc = cls.env["distri.owed.allocation"]

    def _red(self):
        return {r["line_key"]: r["owed"] for r in self.partner._distri_statement_rows()
                if r["owed"]}

    def _allocate(self, **extra):
        vals = {"partner_id": self.partner.id, "credit_key": "online:129903",
                "debit_key": "online:129901", "amount": 500.0, "confidence": "segura",
                "reason": self.REASON, "source": "Revisión Fable 23/11/2026"}
        vals.update(extra)
        return self.Alloc.with_user(self.admin_u).create(vals)

    def test_statement_applies_stored_allocations_and_ignores_archived(self):
        # same amount: the payment pays 129902
        self.assertEqual(self._red(), {"online:129901": 1000.0})
        alloc = self._allocate()
        self.assertEqual(self._red(), {"online:129901": 500.0, "online:129902": 500.0})
        rows = {r["line_key"]: r for r in self.partner._distri_statement_rows()}
        self.assertEqual(rows["online:129901"]["review_note"], "Revisado: " + self.REASON)
        self.assertFalse(rows["online:129902"]["review_note"])
        self.assertIn("Revisado: MOTIVO V1299", self.partner.distri_statement_html)
        alloc.active = False
        self.assertEqual(self._red(), {"online:129901": 1000.0})
        # also when the caller reads with active_test=False (the loader does)
        loose = self.partner.with_context(active_test=False)
        self.assertEqual(loose._distri_owed_allocations(), [])
        self.assertNotIn("MOTIVO V1299", self.partner.sudo()._distri_statement_table(
            self.partner._distri_statement()))

    def test_customer_views_do_not_leak_the_reason(self):
        self._allocate()
        self.partner.sudo().distri_cuenta_visible = True
        token, _expires = share_token(self.env, self.partner)
        res = self.url_open("/estado-de-cuenta/%s" % token)
        self.assertEqual(res.status_code, 200)
        self.assertIn("IMPUTACION V1299", res.text)
        self.assertNotIn("MOTIVO V1299", res.text)
        self.assertNotIn("Revisado", res.text)
        self.assertNotIn("0001/2026/12990", res.text)

    def test_seller_reads_but_cannot_write(self):
        alloc = self._allocate()
        Alloc = self.Alloc.with_user(self.seller)
        self.assertEqual(Alloc.search([("partner_id", "=", self.partner.id)]), alloc)
        with self.assertRaises(AccessError):
            Alloc.create({"partner_id": self.partner.id, "credit_key": "online:129903",
                          "debit_key": "online:129902", "amount": 1.0})
        with self.assertRaises(AccessError):
            alloc.with_user(self.seller).write({"amount": 1.0})
        with self.assertRaises(AccessError):
            alloc.with_user(self.seller).unlink()

    def test_contact_normalized_to_commercial_partner(self):
        contact = self.env["res.partner"].create({"name": "Contacto V1299",
                                                  "parent_id": self.partner.id})
        alloc = self._allocate(partner_id=contact.id)
        self.assertEqual(alloc.partner_id, self.partner)
