import io
from datetime import date

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.deudores import debtor_balances, seller_tags

try:
    from pypdf import PdfReader
except ImportError:  # older images
    from PyPDF2 import PdfReader

# far above any real debt of the copied data: the tests read only their own lines
BIG = 900_000_000.0


@tagged("post_install", "-at_install", "distri_deudores")
class TestDeudores(TransactionCase):
    """«Deudores», «Incobrable» and the seller tags (models/deudores.py)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.admin_user = new_test_user(
            cls.env, login="admin_deudores", name="Admin Deudores",
            groups="base.group_user,distri_pedido.grupo_administracion")
        cls.vendedor = new_test_user(
            cls.env, login="vendedor_deudores", name="Vendedor Deudores",
            groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.italo, cls.distri = seller_tags(cls.env)
        if not cls.italo:
            cls.italo = cls.env["res.partner.category"].create({"name": "Italo"})
        Partner = cls.env["res.partner"]
        cls.direct = Partner.create({"name": "DEUDOR TEST DIRECTO", "customer_rank": 1})
        cls.italo_customer = Partner.create({
            "name": "DEUDOR TEST ITALO", "customer_rank": 1,
            "category_id": [Command.link(cls.italo.id)]})
        cls.reseller = Partner.create({"name": "DEUDOR TEST MAYORISTA", "customer_rank": 1,
                                       "distri_es_mayorista": True})
        cls.lost = Partner.create({"name": "DEUDOR TEST INCOBRABLE", "customer_rank": 1})
        cls.paid = Partner.create({"name": "DEUDOR TEST AL DIA", "customer_rank": 1})
        Mov = cls.env["distri.cuenta.movimiento"].sudo()

        def history(partner, rows):
            Mov.create([dict(partner_id=partner.id, concepto=r[1], comprobante=r[1], fecha=r[0],
                             tipo=r[2], debe=r[3], haber=r[4]) for r in rows])

        history(cls.direct, [
            ("2026-01-10", "0001/2026/91001", "factura", BIG + 3000.0, 0.0),
            ("2026-02-15", "PBNK1/2026/91001", "pago", 0.0, 1000.0),
            ("2026-03-20", "0001/2026/91002", "factura", 500.0, 0.0),
            ("2026-04-05", "PBNK1/2026/91002", "pago", 0.0, 1200.0),
        ])
        history(cls.italo_customer, [("2026-01-12", "0001/2026/92001", "factura", BIG + 7000.0, 0.0)])
        history(cls.reseller, [("2026-01-12", "0001/2026/93001", "factura", BIG + 9000.0, 0.0)])
        history(cls.lost, [("2025-11-14", "0001/2025/94001", "factura", BIG + 5000.0, 0.0)])
        history(cls.paid, [("2026-01-12", "0001/2026/95001", "factura", 800.0, 0.0),
                           ("2026-01-20", "PBNK1/2026/95001", "pago", 0.0, 800.0)])
        cls.lost.with_user(cls.admin_user).write({
            "distri_uncollectible": True, "distri_uncollectible_date": "2026-10-28",
            "distri_uncollectible_note": "Dejó de pagar"})

    def _report(self, **vals):
        return self.env["distri.debtor.report"].with_user(self.admin_user).create(
            dict({"min_amount": BIG}, **vals))

    def _lines(self, report, partner, kind="debtor"):
        lines = report.line_ids if kind == "debtor" else report.uncollectible_line_ids
        return lines.filtered(lambda l: l.partner_id == partner)

    def test_bulk_balance_matches_statement_and_account(self):
        partners = self.direct | self.italo_customer | self.paid
        bulk = debtor_balances(self.env, partners)
        for partner in partners:
            self.assertAlmostEqual(bulk[partner.id]["balance"], partner._distri_account_balance())
        cut = date(2026, 3, 1)
        bulk = debtor_balances(self.env, partners, cut)
        for partner in partners:
            self.assertAlmostEqual(bulk[partner.id]["balance"],
                                   partner._distri_statement(False, cut)["closing"])

    def test_direct_customers_only_ordered_by_debt(self):
        report = self._report()
        partners = report.line_ids.mapped("partner_id")
        self.assertIn(self.direct, partners)
        self.assertIn(self.italo_customer, partners)
        # 19.0.1.120.0 (owner): administración sees the wholesale customers' debts
        self.assertIn(self.reseller, partners)
        self.assertNotIn(self.paid, partners)
        balances = report.line_ids.sorted("rank").mapped("balance")
        self.assertEqual(balances, sorted(balances, reverse=True))
        self.assertLess(self._lines(report, self.italo_customer).rank,
                        self._lines(report, self.direct).rank)
        line = self._lines(report, self.direct)
        self.assertAlmostEqual(line.balance, BIG + 1300.0)
        self.assertEqual(line.last_payment_date, date(2026, 4, 5))
        self.assertAlmostEqual(line.last_payment_amount, 1200.0)
        self.assertEqual(line.last_invoice_date, date(2026, 3, 20))
        self.assertAlmostEqual(line.last_invoice_amount, 500.0)

    def test_uncollectible_out_of_totals(self):
        report = self._report()
        self.assertFalse(self._lines(report, self.lost))
        apart = self._lines(report, self.lost, "uncollectible")
        self.assertAlmostEqual(apart.balance, BIG + 5000.0)
        self.assertAlmostEqual(report.total_debt, sum(report.line_ids.mapped("balance")), 2)
        self.assertGreaterEqual(report.uncollectible_total, BIG + 5000.0)
        # nothing is written off: the balance stays
        self.assertAlmostEqual(self.lost._distri_account_balance(), BIG + 5000.0)

    def test_seller_filter(self):
        report = self._report(seller="italo")
        self.assertEqual(set(report.line_ids.mapped("seller")), {"italo"})
        self.assertIn(self.italo_customer, report.line_ids.mapped("partner_id"))
        self.assertNotIn(self.direct, report.line_ids.mapped("partner_id"))
        report.seller = "distrineuquen"
        self.assertIn(self.direct, report.line_ids.mapped("partner_id"))
        self.assertNotIn(self.italo_customer, report.line_ids.mapped("partner_id"))

    def test_filters_only_select_from_the_snapshot(self):
        report = self._report()
        snapshot, built = report.snapshot_line_ids, report.built_at
        # 19.0.1.120.0: the wholesale customer (highest debt) is listed first
        self.assertEqual(self._lines(report, self.reseller).rank, 1)
        self.assertEqual(self._lines(report, self.italo_customer).rank, 2)
        self.assertEqual(self._lines(report, self.direct).rank, 3)
        report.seller = "distrineuquen"
        self.assertEqual(report.snapshot_line_ids, snapshot)
        self.assertEqual(report.built_at, built)
        self.assertEqual(self._lines(report, self.direct).rank, 1)
        self.assertNotIn(self.italo_customer, report.line_ids.mapped("partner_id"))
        self.assertAlmostEqual(report.total_debt, sum(report.line_ids.mapped("balance")), 2)
        self.assertEqual(report.debtor_count, len(report.line_ids))
        self.assertEqual(set(report.uncollectible_line_ids.mapped("seller")), {"distrineuquen"})
        report.write({"seller": "all", "min_amount": BIG + 2000.0})
        self.assertEqual(report.snapshot_line_ids, snapshot)
        self.assertIn(self.italo_customer, report.line_ids.mapped("partner_id"))
        self.assertNotIn(self.direct, report.line_ids.mapped("partner_id"))
        # the uncollectible ones do not depend on the minimum
        self.assertTrue(self._lines(report, self.lost, "uncollectible"))
        # a new cut date rebuilds
        report.date_to = date(2026, 3, 1)
        self.assertNotEqual(report.snapshot_line_ids, snapshot)

    def test_onchange_shows_the_new_selection(self):
        report = self._report().with_user(self.admin_user)
        snapshot = report.snapshot_line_ids
        direct_line = self._lines(report, self.direct)
        spec = {"seller": {}, "min_amount": {}, "debtor_count": {}, "total_debt": {},
                "total_italo": {}, "line_ids": {"fields": {"partner_id": {}, "rank": {},
                                                           "balance": {}}}}
        result = report.onchange({"seller": "italo"}, ["seller"], spec)["value"]
        self.assertAlmostEqual(result["total_debt"], report.total_italo, 2)
        self.assertIn(Command.delete(direct_line.id), result["line_ids"])
        # nothing written: the save that follows the change does that
        self.assertEqual(report.seller, "all")
        self.assertEqual(report.snapshot_line_ids, snapshot)
        result = report.onchange({"min_amount": BIG + 2000.0}, ["min_amount"], spec)["value"]
        self.assertIn(Command.delete(direct_line.id), result["line_ids"])
        self.assertAlmostEqual(result["total_debt"], report.total_debt - direct_line.balance, 2)

    def test_exports_follow_the_selection(self):
        report = self._report()
        report.seller = "italo"
        values = report._report_values()
        self.assertEqual(values["lines"], report.line_ids)
        self.assertEqual(values["selection"], "Clientes de Italo")
        self.assertNotIn(self.direct, values["lines"].mapped("partner_id"))
        report.action_preset_distri()
        self.assertEqual(report.seller, "distrineuquen")
        values = report._report_values()
        self.assertIn(self.direct, values["lines"].mapped("partner_id"))
        self.assertNotIn(self.italo_customer, values["lines"].mapped("partner_id"))
        self.assertEqual([l.rank for l in values["lines"]], list(range(1, len(values["lines"]) + 1)))

    def test_cut_date(self):
        report = self._report(date_to=date(2026, 3, 1))
        line = self._lines(report, self.direct)
        self.assertAlmostEqual(line.balance, BIG + 2000.0)
        self.assertEqual(line.last_payment_date, date(2026, 2, 15))
        self.assertEqual(line.last_invoice_date, date(2026, 1, 10))
        report.date_to = date(2026, 1, 11)
        self.assertAlmostEqual(self._lines(report, self.direct).balance, BIG + 3000.0)
        self.assertFalse(self._lines(report, self.direct).last_payment_date)

    def test_new_customer_gets_distrineuquen(self):
        self.assertIn(self.distri, self.direct.category_id)
        self.assertNotIn(self.distri, self.italo_customer.category_id)
        self.assertNotIn(self.distri, self.reseller.category_id)
        later = self.env["res.partner"].create({"name": "DEUDOR TEST SIN RANGO"})
        self.assertFalse(later.category_id)
        later.customer_rank = 1
        self.assertIn(self.distri, later.category_id)

    def test_only_admin(self):
        with self.assertRaises(AccessError):
            self.direct.with_user(self.vendedor).write({"distri_uncollectible": True})
        with self.assertRaises(AccessError):
            self.env["distri.debtor.report"].with_user(self.vendedor).create({})

    def test_pdf_and_excel(self):
        report = self._report(seller="italo")
        values = report._report_values()
        self.assertEqual(len(values["statements"]), len(report.line_ids))
        html, _kind = self.env["ir.actions.report"].with_user(self.admin_user)._render_qweb_html(
            "distri_pedido.report_deudores", report.ids)
        html = html.decode()
        self.assertIn("Clientes de Italo", html)
        self.assertIn("DEUDOR TEST ITALO", html)
        self.assertIn("0001/2026/92001", html)
        self.assertIn("Deudas incobrables", html)
        Report = self.env["ir.actions.report"].with_user(self.admin_user).with_context(
            force_report_rendering=True)
        pdf, kind = Report._render_qweb_pdf("distri_pedido.report_deudores", report.ids)
        self.assertEqual(kind, "pdf")
        self.assertGreaterEqual(len(PdfReader(io.BytesIO(pdf)).pages), 2)
        action = report.action_export_xlsx()
        self.assertTrue(action["url"].startswith("/web/content/"))

    def test_pdf_in_parts(self):
        """19.0.1.53.0: summary first, then the statements in groups, merged."""
        from odoo.addons.distri_pedido.models.deudores import ReportDeudoresParts
        report = self._report(seller="all")
        self.assertGreaterEqual(len(report.line_ids), 2)
        summary = report.with_context(distri_debtor_part="summary")._report_values()
        self.assertEqual((summary["statements"], summary["only_statements"]), ([], False))
        one = report.with_context(distri_debtor_part=(1, 2))._report_values()
        self.assertEqual((len(one["statements"]), one["only_statements"]), (1, True))
        html = self.env["ir.actions.report"].with_user(self.admin_user).with_context(
            distri_debtor_part=(0, 1))._render_qweb_html("distri_pedido.report_deudores",
                                                          report.ids)[0].decode()
        self.assertNotIn("Clientes que deben", html)
        Report = self.env["ir.actions.report"].with_user(self.admin_user).with_context(
            force_report_rendering=True)
        whole = len(PdfReader(io.BytesIO(Report._render_qweb_pdf(
            "distri_pedido.report_deudores", report.ids)[0])).pages)
        self.patch(ReportDeudoresParts, "STATEMENTS_PER_PART", 1)
        pdf, kind = Report._render_qweb_pdf("distri_pedido.report_deudores", report.ids)
        self.assertEqual(kind, "pdf")
        self.assertEqual(len(PdfReader(io.BytesIO(pdf)).pages), whole)

    def test_review_seller(self):
        Review = self.env["distri.balance.review"].sudo()
        review = Review.create({"partner_id": self.italo_customer.id})
        self.assertEqual(review.seller, "italo")
        other = Review.create({"partner_id": self.direct.id})
        self.assertEqual(other.seller, "distrineuquen")
        self.direct.category_id = [Command.link(self.italo.id)]
        self.assertEqual(other.seller, "italo")
