"""19.0.1.129.2: the driver's cash credited at the visit, verified at the
settlement (models/efectivo_a_rendir.py).

- The visit with cash makes the customer payment at once, «a rendir», in the
  driver's own cash journal, applied oldest invoice first.
- A resend never duplicates it; an edited amount remakes it; «No pagó» and
  «No se entregó» cancel it.
- The settlement (through the caja) reuses it, verifies it and the caja
  totals and closing stay right; a shortfall is the driver's case while the
  customer stays credited.
- The audit flags an «a rendir» payment older than a day and alerts Gerencia,
  never «sin comprobante».
- Routes of before the upgrade keep the old path (and the migration marks
  them); red total = balance.
Synthetic data only.
"""
import importlib.util
import os
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.auditoria import (
    DRIVER_CASH_ALERT_SUMMARY, PARAM_DRIVER_CASH_DAYS)
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.efectivo_a_rendir import DRIVER_CASH_LABEL
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin
from odoo.addons.distri_pedido.tests.test_reparto import _uuid


@tagged("post_install", "-at_install", "distri_v129_2")
class TestV1292DriverCash(CajaMixin, AccountTestInvoicingCommon):

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
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Rendir V1292",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Rendir V1292",
                                                           "company_type": "company"})
        cls.Payment = cls.env["account.payment"]

    # ------------------------------------------------------------ helpers

    def _invoiced_order(self, partner=None, price=100.0, qty=10):
        """An order of qty x price with its posted invoice."""
        order = self._order(partner or self.partner, self.product, qty=qty, price=price)
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _old_invoice(self, amount, partner=None):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": (partner or self.partner).id,
            "invoice_date": "2026-01-10",
            "invoice_line_ids": [Command.create({
                "name": "deuda vieja", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def _visit_sync(self, route, stop, captured="2026-11-22T15:00:00.000Z", **kw):
        payload = self._visit(stop, **kw)
        res = self._sync([self._event(route, "visit", payload, captured=captured)])
        self.assertEqual(res[0]["status"], "ok", res)
        return payload

    def _live(self, stop):
        return self.Payment.sudo().search([("distri_driver_stop_id", "=", stop.id),
                                           ("state", "in", ("in_process", "paid"))])

    def _all(self, stop):
        return self.Payment.sudo().search([("distri_driver_stop_id", "=", stop.id)])

    def _partner_payments(self, partner=None):
        return self.Payment.sudo().search([
            ("partner_id", "=", (partner or self.partner).id), ("payment_type", "=", "inbound"),
            ("state", "in", ("in_process", "paid"))])

    def _open_lines(self, payments):
        from odoo.addons.distri_pedido.models.caja_contabilidad import open_outstanding_lines
        return open_outstanding_lines(payments.sudo())

    def _balance(self, partner=None):
        return (partner or self.partner)._distri_account_balance()

    def _close(self, route):
        res = self._sync([self._event(route, "close", {}, captured="2026-11-22T18:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(route.state, "closed")

    # ------------------------------------------------------------ at the visit

    def test_instant_payment_on_visit_close(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self.assertEqual(self._balance(), 1000)
        self._visit_sync(route, stop, cash=600)
        payment = self._live(stop)
        self.assertEqual(len(payment), 1)
        self.assertEqual(payment.amount, 600)
        self.assertEqual(payment.partner_id, self.partner)
        self.assertEqual(payment.distri_driver_cash_state, "a_rendir")
        self.assertEqual(payment.distri_driver_route_id, route)
        self.assertEqual(payment.distri_driver_cash_label, DRIVER_CASH_LABEL)
        self.assertEqual(payment.journal_id.type, "cash")
        self.assertEqual(payment.journal_id.distri_driver_cash_user_id, self.driver)
        self.assertEqual(payment.journal_id.name, "Efectivo a rendir · %s" % self.driver.name)
        self.assertNotEqual(payment.journal_id, self.cash_journal)
        self.assertEqual(payment.create_uid, self.driver, "the driver credited it")
        self.assertEqual(invoice.amount_residual, 400, "applied to the invoice at once")
        self.assertEqual(self._balance(), 400, "the customer balance drops at once")
        self.assertFalse(route.payment_ids, "the settlement links it, not the visit")
        self.assertEqual(route.driver_cash_payment_ids, payment)
        # the statement says so (staff), and it is not «Pago confirmado» yet
        rows = self.partner._distri_statement()["rows"]
        [row] = [r for r in rows if r["res_id"] == payment.move_id.id]
        self.assertTrue(row["driver_cash_pending"])
        self.assertIn(DRIVER_CASH_LABEL, row["concept"])
        self.assertFalse(row["confirmed"])
        self.assertIn("a rendir", self.partner.sudo().distri_statement_html)
        # a second driver gets a journal of his own; the same driver reuses his
        Journal = self.env["account.journal"]
        mine = Journal._distri_driver_cash_journal(self.driver, self.env.company)
        self.assertEqual(mine, payment.journal_id)
        other = Journal._distri_driver_cash_journal(self.other_driver, self.env.company)
        self.assertNotEqual(other, mine)
        self.assertLessEqual(len(other.code), 5)

    def test_app_resend_does_not_duplicate(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        payload = self._visit(stop, cash=600)
        event = self._event(route, "visit", payload, captured="2026-11-22T15:00:00.000Z")
        self.assertEqual(self._sync([event])[0]["status"], "ok")
        first = self._live(stop)
        # the same event again (a retried POST)
        self.assertEqual(self._sync([dict(event)])[0]["status"], "duplicate")
        # the same visit in a new event (the app resends after a timeout)
        again = self._event(route, "visit", dict(payload), captured="2026-11-22T15:01:00.000Z")
        self.assertEqual(self._sync([again])[0]["status"], "ok")
        # and the sync itself run again
        stop._distri_sync_driver_cash()
        stop._distri_sync_driver_cash()
        self.assertEqual(self._live(stop), first, "the same payment, untouched")
        self.assertEqual(len(self._all(stop)), 1, "nothing cancelled and remade")
        self.assertEqual(invoice.amount_residual, 400)
        # the unique index: never two live payments for one stop
        with self.assertRaises(Exception), self.cr.savepoint(), mute_logger("odoo.sql_db"):
            route._distri_create_driver_cash(stop)
            self.env.flush_all()

    def test_edited_amount_remakes_the_payment(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        payload = self._visit_sync(route, stop, cash=600)
        first = self._live(stop)
        payload = dict(payload, cash=800)
        res = self._sync([self._event(route, "visit", payload,
                                      captured="2026-11-22T15:05:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        live = self._live(stop)
        self.assertEqual(len(live), 1)
        self.assertNotEqual(live, first)
        self.assertEqual(live.amount, 800)
        self.assertEqual(first.state, "canceled")
        self.assertEqual(invoice.amount_residual, 200)
        self.assertEqual(self._balance(), 200)
        self.assertIn("ahora cobró", "".join(route.message_ids.mapped("body")))

    def test_no_pago_cancels_the_payment(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        payload = self._visit_sync(route, stop, cash=600)
        first = self._live(stop)
        payload = dict(payload, cash=0, no_payment=True)
        res = self._sync([self._event(route, "visit", payload,
                                      captured="2026-11-22T15:05:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.state, "not_paid")
        self.assertFalse(self._live(stop))
        self.assertEqual(first.state, "canceled")
        self.assertEqual(invoice.amount_residual, 1000, "the invoice owes again")
        self.assertEqual(self._balance(), 1000)
        # «No pagó nada» keeps its pending item at the driver's name, open
        promise = self.env["distri.promised.transfer"].sudo().search([("stop_id", "=", stop.id)])
        self.assertEqual(promise.kind, "unpaid")
        self.assertEqual(promise.amount, 1000)
        self.assertIn(promise.state, ("open", "overdue"))
        # paid again, then «No se entregó»
        self._visit_sync(route, stop, captured="2026-11-22T15:10:00.000Z", cash=300)
        self.assertEqual(self._live(stop).amount, 300)
        res = self._sync([self._event(route, "visit", {
            "visit_uuid": _uuid(), "stop_id": stop.id, "outcome": "not_delivered",
            "reason": "Cerrado"}, captured="2026-11-22T15:20:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertFalse(self._live(stop))
        self.assertEqual(invoice.amount_residual, 1000)

    def test_payment_failure_never_rejects_the_visit(self):
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        Route = type(self.env["distri.route"])
        with patch.object(Route, "_distri_create_driver_cash",
                          side_effect=UserError("Diario cerrado")), \
                mute_logger("odoo.addons.distri_pedido.models.efectivo_a_rendir"):
            self._visit_sync(route, stop, cash=600)
        self.assertEqual(stop.cash_amount, 600)
        self.assertFalse(self._live(stop))
        self.assertIn("No se pudo registrar al momento", "".join(route.message_ids.mapped("body")))
        # the settlement makes it (and verifies it)
        self._close(route)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).receive_route(route, "600")
        live = self._live(stop)
        self.assertEqual(live.amount, 600)
        self.assertEqual(live.distri_driver_cash_state, "verificado")
        self.assertEqual(route.payment_ids, live)

    # ------------------------------------------------------------ settlement

    def test_settlement_reuses_payment_and_caja_totals(self):
        order, invoice = self._invoiced_order()
        order2, invoice2 = self._invoiced_order(self.other_partner, price=50.0)
        route = self._route(order | order2)
        stop = route.stop_ids.filtered(lambda s: s.order_id == order)
        stop2 = route.stop_ids - stop
        self._visit_sync(route, stop, cash=600)
        self._visit_sync(route, stop2, cash=300)
        credited = self._live(stop) | self._live(stop2)
        self.assertEqual(len(credited), 2)
        self._close(route)
        self.assertEqual(route.cash_expected, 900)
        caja = self._caja().open_session("0")
        movement = caja.with_user(self.cashier).receive_route(route, "900")
        self.assertEqual(route.state, "settled")
        # no second payment: the same two, now verified and on the route
        self.assertEqual(route.payment_ids, credited)
        self.assertEqual(self._partner_payments(), self._live(stop))
        self.assertEqual(self._partner_payments(self.other_partner), self._live(stop2))
        self.assertEqual(len(self._all(stop) | self._all(stop2)), 2)
        for payment in credited:
            self.assertEqual(payment.distri_driver_cash_state, "verificado")
            self.assertEqual(payment.distri_driver_cash_verified_by_id, route.settled_by_id)
            self.assertTrue(payment.distri_driver_cash_verified_at)
        self.assertEqual(invoice.amount_residual, 400)
        self.assertEqual(invoice2.amount_residual, 200)
        # ✓ «Pago confirmado» in the statement
        rows = self.partner._distri_statement()["rows"]
        [row] = [r for r in rows if r["res_id"] == self._live(stop).move_id.id]
        self.assertTrue(row["confirmed"])
        self.assertFalse(row.get("driver_cash_pending"))
        self.assertEqual(self._live(stop).distri_driver_cash_label,
                         "Efectivo cobrado por el repartidor, rendido")
        # the caja counts the route as before
        self.assertEqual(movement.kind, "rendicion")
        self.assertEqual(movement.amount, 900)
        self.assertFalse(movement.route_difference)
        self.assertEqual(caja.total_rendiciones, 900)
        self.assertEqual(caja.expected, 900)
        caja.with_user(self.cashier).close_session(self._count(b500=1, b200=2), left="0")
        self.assertFalse(caja.difference)
        credited.invalidate_recordset()
        self.assertEqual(set(credited.mapped("state")), {"paid"}, "the closing booked the cash")
        self.assertFalse(self._open_lines(credited))
        self.assertAlmostEqual(caja.statement_id.balance_end, 0)
        # a settled route never moves its payments again
        stop._distri_sync_driver_cash()
        self.assertEqual(self._live(stop).distri_driver_cash_state, "verificado")

    def test_shortfall_charged_to_driver_customer_stays_credited(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._visit_sync(route, stop, cash=1000)
        payment = self._live(stop)
        self.assertEqual(invoice.amount_residual, 0)
        self._close(route)
        caja = self._caja().open_session("0")
        Caja = caja.with_user(self.cashier)
        movement = Caja.receive_route(route, "800", difference_mode="cargo")
        self.assertEqual(movement.route_difference, -200)
        case = route._cash_case()
        self.assertEqual(case.kind, "cash")
        self.assertEqual(case.amount, -200)
        self.assertIn("a cargo", case.driver_explanation)
        # the customer payment stands, whole and verified
        self.assertEqual(self._live(stop), payment)
        self.assertEqual(payment.amount, 1000)
        self.assertEqual(payment.distri_driver_cash_state, "verificado")
        self.assertEqual(invoice.amount_residual, 0)
        self.assertEqual(self._balance(), 0)
        self.assertEqual(caja.expected, 800)
        caja.with_user(self.cashier).close_session(self._count(b500=1, b200=1, b100=1), left="0")
        self.assertFalse(caja.difference)
        self.assertEqual(payment.state, "paid")

    def test_settlement_fixes_a_stale_payment(self):
        """A payment left out of step with its visit (a failed sync) is put
        right before the settlement reuses it."""
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._visit_sync(route, stop, cash=600)
        stale = self._live(stop)
        stop.sudo().cash_amount = 700  # as if the edit's sync had failed
        self._close(route)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).receive_route(route, "700")
        self.assertEqual(stale.state, "canceled")
        self.assertEqual(route.payment_ids.amount, 700)
        self.assertEqual(route.payment_ids, self._live(stop))
        self.assertEqual(invoice.amount_residual, 300)

    # ------------------------------------------------------------ audit

    def test_audit_alerts_after_one_day(self):
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._visit_sync(route, stop, cash=600)
        payment = self._live(stop)
        Audit = self.env["distri.mp.audit"]
        Anomaly = self.env["distri.mp.anomaly"].sudo()
        run = Audit._run(company=self.env.company)
        self.assertFalse(Anomaly.search([("payment_id", "=", payment.id)]), "fresh: no alert")
        self.assertFalse(run.driver_cash_count)
        # never an electronic payment of the audit
        self.assertNotIn(payment, run._scope_payments())
        # two days later, still «a rendir»
        self.env.cr.execute("UPDATE account_payment SET create_date = %s WHERE id = %s",
                            [fields.Datetime.now() - timedelta(days=2), payment.id])
        payment.invalidate_recordset()
        run = Audit._run(company=self.env.company)
        anomaly = Anomaly.search([("payment_id", "=", payment.id)])
        self.assertEqual(anomaly.kind, "efectivo_a_rendir")
        self.assertEqual(anomaly.state, "open")
        self.assertTrue(anomaly.overdue)
        self.assertEqual(anomaly.credited_by_id, self.driver)
        self.assertIn(route.name, anomaly.detail)
        self.assertEqual(run.driver_cash_count, 1)
        self.assertIn("sin rendir", run.summary)
        alerts = anomaly.activity_ids.filtered(lambda a: a.summary == DRIVER_CASH_ALERT_SUMMARY)
        self.assertIn(self.manager, alerts.user_id)
        self.assertFalse(Anomaly.search([("payment_id", "=", payment.id),
                                         ("kind", "=", "sin_comprobante")]))
        self.assertFalse(payment.distri_without_receipt)
        # «Anular pago» never cancels the customer's payment for the driver's cash
        with self.assertRaisesRegex(UserError, "cobró el repartidor"):
            anomaly._annul_payment("probando")
        # found again: no second alert
        Audit._run(company=self.env.company)
        self.assertEqual(len(anomaly.activity_ids.filtered(
            lambda a: a.summary == DRIVER_CASH_ALERT_SUMMARY)), len(alerts))
        # the daily cron runs it without any Mercado Pago report
        with patch.object(type(Audit), "_run", autospec=True,
                          side_effect=lambda self_, **kw: self_.browse()) as ran:
            with patch.object(type(self.env["distri.credit.summary"]), "_cron_daily"):
                Audit._cron_audit()
        self.assertTrue(ran.called)
        # settled: resolved by itself, the alert closes
        self._close(route)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).receive_route(route, "600")
        Audit._run(company=self.env.company)
        self.assertEqual(anomaly.state, "resolved")
        self.assertFalse(anomaly.activity_ids.filtered(
            lambda a: a.summary == DRIVER_CASH_ALERT_SUMMARY))

    def test_audit_days_parameter(self):
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._visit_sync(route, stop, cash=600)
        payment = self._live(stop)
        self.env.cr.execute("UPDATE account_payment SET create_date = %s WHERE id = %s",
                            [fields.Datetime.now() - timedelta(days=2), payment.id])
        payment.invalidate_recordset()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_DRIVER_CASH_DAYS, "3")
        self.env["distri.mp.audit"]._run(company=self.env.company)
        self.assertFalse(self.env["distri.mp.anomaly"].sudo().search(
            [("payment_id", "=", payment.id)]))

    # ------------------------------------------------------------ legacy

    def test_legacy_route_keeps_payments_at_settlement(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        route.sudo().distri_cash_at_settlement = True
        stop = route.stop_ids
        self._visit_sync(route, stop, cash=600)
        self.assertFalse(self._all(stop), "the old path: nothing at the visit")
        self.assertEqual(invoice.amount_residual, 1000)
        self._close(route)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).receive_route(route, "600")
        payment = route.payment_ids
        self.assertEqual(payment.amount, 600)
        self.assertEqual(payment.journal_id, self.cash_journal, "the route's journal, as before")
        self.assertFalse(payment.distri_driver_cash_state)
        self.assertFalse(payment.distri_driver_stop_id)
        self.assertEqual(invoice.amount_residual, 400)

    def test_migration_marks_routes_in_progress(self):
        order_a, _i = self._invoiced_order()
        order_b, _i = self._invoiced_order()
        order_c, _i = self._invoiced_order()
        on_road = self._route(order_a)
        closed = self._route(order_b)
        closed.sudo().state = "closed"
        assigned = self._route(order_c, on_road=False)
        depot = self.env["distri.route"]._depot_route_for(self.cashier)
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "migrations", "19.0.1.129.2", "post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v129_2_migration", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.env.flush_all()
        module.migrate(self.env.cr, "19.0.1.129.1")
        self.env.invalidate_all()
        self.assertTrue(on_road.distri_cash_at_settlement)
        self.assertTrue(closed.distri_cash_at_settlement)
        self.assertFalse(assigned.distri_cash_at_settlement)
        self.assertFalse(depot.distri_cash_at_settlement)
        # a fresh install does nothing
        module.migrate(self.env.cr, None)

    def test_depot_delivery_untouched(self):
        """The depot box already registers its payment at the delivery: no
        «a rendir» payment there."""
        route = self.env["distri.route"]._depot_route_for(self.cashier)
        self.assertFalse(route._distri_driver_cash_enabled())

    # ------------------------------------------------------------ red rule

    def test_red_total_equals_balance(self):
        self._old_invoice(500)
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._visit_sync(route, stop, cash=700)
        data = self.partner._distri_statement()
        self.assertAlmostEqual(data["balance"], 800)
        self.assertAlmostEqual(data["owed_total"], data["balance"])
        self.assertEqual(invoice.amount_residual, 800, "the old invoice was paid first")
        # an edit, then the settlement: still red = balance
        self._visit_sync(route, stop, captured="2026-11-22T15:30:00.000Z", cash=1200)
        data = self.partner._distri_statement()
        self.assertAlmostEqual(data["balance"], 300)
        self.assertAlmostEqual(data["owed_total"], data["balance"])
        self._close(route)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).receive_route(route, "1200")
        data = self.partner._distri_statement()
        self.assertAlmostEqual(data["balance"], 300)
        self.assertAlmostEqual(data["owed_total"], data["balance"])
        self.assertEqual(data["owed_count"], 1)

    # ------------------------------------------------------------ /cuentas order

    def test_cuentas_pending_receipts_above_movements(self):
        """/cuentas «Estado de cuenta» (static/src/cuentas/cuentas_app.js,
        accountBody: the same body on the phone and on the PC): «Comprobantes
        pendientes» renders before «Movimientos»."""
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "static", "src", "cuentas", "cuentas_app.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        body = source[source.index("function accountBody("):source.index("function viewAccount(")]
        self.assertLess(body.index("receiptsView(d)"), body.index("Movimientos · lo más nuevo"))
        self.assertEqual(body.count("receiptsView(d)"), 1, "rendered once")
        self.assertIn('<div class="label">Comprobantes pendientes</div>',
                      source[source.index("function receiptsView("):])
