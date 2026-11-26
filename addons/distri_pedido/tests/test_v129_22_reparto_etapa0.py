"""19.0.1.129.22: «etapa 0» hotfixes of the driver app.

1. The phone reconciles with the server: each rejection in the route and in
   the load poll names its stop and visit, so a visit resolved or resent on
   the server stops blocking the phone's close (app3.js, node).
2. «Ya estaba pagado»: `paid`/`due` of the visit come from the invoice
   residual also without a receipt card (a Mercado Pago report payment
   loaded by administración has none), with how and when; the visit saved
   as «Ya estaba pagado» is its own stop state, without a promise, cash or
   charge; when the order still owes it is recorded as «No pagó» and the
   phone gets a warning, never a rejection.
3. The honest message when there is nothing to collect.
4. «Entregar igual» in one tap (node).
Synthetic data only.
"""
import json
import os
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin


@tagged("post_install", "-at_install", "distri_v129_22")
class TestV12922RepartoEtapa0(CajaMixin, AccountTestInvoicingCommon):

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
        cls.mp_journal = cls.company_data["default_journal_bank"]
        cls.mp_journal.name = "Mercado Pago V12922"
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Etapa0 V12922",
                                                     "company_type": "company"})

    # ------------------------------------------------------------ helpers

    def _invoiced_order(self, qty=10, price=100.0):
        order = self._order(self.partner, self.product, qty=qty, price=price)
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _pay(self, invoice):
        """A payment by administración with no receipt card (like the
        Mercado Pago report)."""
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.mp_journal.id,
                "payment_date": fields.Date.today(),
            }).action_create_payments()
        self.assertFalse(invoice.amount_residual, "the invoice is paid")

    def _info(self, stop):
        return stop.with_user(self.driver)._app_payload()

    def _transfer(self, stop, amount, **kw):
        return self._visit(stop, transfer="yes", transfer_amount=amount,
                           receipt_answer="not_yet", **kw)

    def _promise(self, stop):
        return self.env["distri.promised.transfer"].sudo().search([("stop_id", "=", stop.id)])

    def _driver_payments(self, stop):
        return self.env["account.payment"].sudo().search([("distri_driver_stop_id", "=", stop.id)])

    # ------------------------------------------------------------ 2. paid / due

    def test_paid_and_due_from_the_residual_without_cards(self):
        order, invoice = self._invoiced_order()  # $ 1.000
        route = self._route(order)
        stop = route.stop_ids
        data = self._info(stop)
        self.assertEqual(data["payments"], [], "no receipt card at all")
        self.assertFalse(data["payments_paid"])
        self.assertEqual(data["payments_due"], 1000)
        self.assertEqual(data["payments_paid_how"], "")

        self._pay(invoice)
        data = self._info(stop)
        self.assertEqual(data["payments"], [])
        self.assertTrue(data["payments_paid"], "paid by the residual, without a card")
        self.assertEqual(data["payments_due"], 0)
        self.assertIn("Mercado Pago V12922", data["payments_paid_how"])
        self.assertIn(fields.Date.today().strftime("%d/%m"), data["payments_paid_how"])
        self.assertEqual(stop.order_amount, 1000, "it only informs: the snapshot stays")
        # the load poll brings the same
        info = route._load_status_payload()["payments"][str(stop.id)]
        self.assertTrue(info["paid"])
        self.assertEqual(info["due"], 0)
        self.assertIn("Mercado Pago V12922", info["paid_how"])

    def test_order_without_invoice_and_cards_is_not_paid(self):
        order = self._order(self.partner, self.product, qty=5)
        stop = self._route(order).stop_ids
        data = self._info(stop)
        self.assertFalse(data["payments_paid"])
        self.assertEqual(data["payments_due"], 500)

    # ------------------------------------------------------------ 2. already_paid

    def test_already_paid_is_its_own_state_without_promise_cash_or_charge(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._pay(invoice)
        res = self._sync([self._event(route, "visit", self._visit(stop, already_paid=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertNotIn("warning", res[0])
        self.assertEqual(stop.state, "already_paid")
        self.assertEqual(dict(stop._fields["state"].selection)["already_paid"], "Ya estaba pagado")
        self.assertEqual((stop.cash_amount, stop.transfer, stop.transfer_amount), (0, "no", 0))
        self.assertFalse(self._promise(stop), "no «Comprobante pendiente» nor «No pagó»")
        self.assertFalse(self._driver_payments(stop), "no cash payment «a rendir»")
        self.assertFalse(self.env["distri.route.case"].sudo().search([("stop_id", "=", stop.id)]))
        visit = stop._app_visit()
        self.assertTrue(visit["already_paid"])
        self.assertFalse(visit["no_payment"])
        # never with money
        res = self._sync([self._event(route, "visit", self._visit(stop, already_paid=True, cash=500),
                                      captured="2026-09-26T16:00:00.000Z")])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("no lleva efectivo ni transferencia", res[0]["error"])
        # the route closes with it
        res = self._sync([self._event(route, "close", {}, captured="2026-09-26T17:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)

    def test_already_paid_but_still_owed_is_not_paid_with_a_warning(self):
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(stop, already_paid=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertIn("todavía debe $ 1.000", res[0]["warning"])
        self.assertIn(order.name, res[0]["warning"])
        self.assertEqual(stop.state, "not_paid")
        promise = self._promise(stop)
        self.assertEqual(promise.kind, "unpaid", "the existing «No pagó» item, following the residual")
        self.assertEqual(promise.amount, 1000)
        self.assertFalse(self.env["distri.sync.rejection"].sudo().search([("stop_id", "=", stop.id)]))

    def test_old_app_payload_without_the_flag_behaves_as_before(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        self._pay(invoice)
        payload = self._visit(stop, no_payment=True)
        payload.pop("already_paid", None)
        res = self._sync([self._event(route, "visit", payload)])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertNotIn("warning", res[0])
        self.assertEqual(stop.state, "not_paid")

    # ------------------------------------------------------------ 3. honest message

    def test_honest_message_when_nothing_is_due(self):
        order, invoice = self._invoiced_order()
        self._pay(invoice)
        route = self._route(order)  # downloaded after the payment: $ 0 to collect
        stop = route.stop_ids
        self.assertFalse(stop.order_amount)
        res = self._sync([self._event(route, "visit", self._transfer(stop, 492000))])
        self.assertEqual(res[0]["status"], "error", res)
        self.assertIn("Este pedido ya figura pagado ($ 0 a cobrar)", res[0]["error"])
        self.assertIn('Elegí "Ya estaba pagado"', res[0]["error"])
        self.assertNotIn("14.307,60", res[0]["error"])
        # a real typo still gets the format hint
        other, _inv = self._invoiced_order()
        route2 = self._route(other)
        res = self._sync([self._event(route2, "visit", self._visit(route2.stop_ids, cash=1430760))])
        self.assertEqual(res[0]["status"], "error", res)
        self.assertIn("14.307,60", res[0]["error"])

    # ------------------------------------------------------------ 1. rejections

    def test_rejection_resolution_reaches_the_phone(self):
        order, invoice = self._invoiced_order()
        self._pay(invoice)
        route = self._route(order)
        stop = route.stop_ids
        rejected = self._event(route, "visit", self._transfer(stop, 492000))
        res = self._sync([rejected])
        self.assertEqual(res[0]["status"], "error", res)
        Rejection = self.env["distri.sync.rejection"].sudo()

        def item():
            items = [x for x in route._app_payload()["rejections"] if x["uuid"] == rejected["uuid"]]
            self.assertEqual(len(items), 1)
            return items[0]

        first = item()
        self.assertEqual(first["state"], "open")
        self.assertEqual(first["stop_id"], stop.id)
        self.assertEqual(first["type"], "visit")
        self.assertEqual(first["visit_uuid"], rejected["payload"]["visit_uuid"])
        # the load poll brings them too
        polled = route._load_status_payload()["rejections"]
        self.assertIn(rejected["uuid"], [x["uuid"] for x in polled])

        # resent as «Ya estaba pagado»: closed on the server, the phone sees it
        resend = self._event(route, "visit", self._visit(stop, already_paid=True),
                             captured="2026-09-26T16:00:00.000Z")
        resend["resends"] = rejected["uuid"]
        res = self._sync([resend])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.state, "already_paid")
        self.assertEqual(item()["state"], "resent")
        self.assertEqual(item()["stop_id"], stop.id)
        payload = route._app_payload()
        [served] = [s for s in payload["stops"] if s["id"] == stop.id]
        self.assertEqual(served["visit"]["visit_uuid"], resend["payload"]["visit_uuid"])
        self.assertTrue(served["visit"]["already_paid"])
        self.assertFalse(route.rejection_open_count)

        # resolved by administración: also not open for the phone
        other, _inv = self._invoiced_order()
        route2 = self._route(other)
        bad = self._event(route2, "visit", self._visit(route2.stop_ids, cash=1430760))
        self._sync([bad])
        rec = Rejection.search([("uuid", "=", bad["uuid"])])
        rec.resolution = "Se cobró en el local."
        rec.action_resolve()
        [mine] = [x for x in route2._app_payload()["rejections"] if x["uuid"] == bad["uuid"]]
        self.assertEqual(mine["state"], "resolved")
        self.assertEqual(mine["resolution"], "Se cobró en el local.")
        self.assertEqual(mine["stop_id"], route2.stop_ids.id)

    # ------------------------------------------------------------ app (node)

    def test_app_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v129_22.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # 1. rejected visit: blocks while open, then the server's visit wins
        self.assertEqual(out["stillOpen"], {"unsynced": 1, "error": True, "rejected": 1})
        self.assertEqual(out["resolved"], {"unsynced": 0, "visit_uuid": "v-new", "error": None,
                                           "resolved": True, "rejected": 0,
                                           "state": "already_paid", "listed": True})
        self.assertEqual(out["resolvedNoVisit"], {"visit": None, "unsynced": 0})
        self.assertEqual(out["closeRefresh"], {"asked": True, "unsynced": 0, "closeQueued": True})
        # 4. «Entregar igual»
        # 26/11/2026 (owner): «¿Devolvió algo?» starts in «No», only «No» / «Sí»
        self.assertEqual(out["defaultNo"], {"answer": "none", "on": True, "yesOff": True,
                                            "noMandatory": True, "oldLabels": False})
        self.assertTrue(out["yesOpensReturns"], "«Sí» opens the returns editor")
        self.assertEqual(out["savedKeepsYes"], {"answer": "yes", "on": True, "listed": True})
        self.assertTrue(out["undoNdNo"])
        # «Sí» with nothing loaded still locks «Entregar igual»
        self.assertEqual(out["noCodeLocked"], {
            "on": False, "toast": "Primero cargá lo que devolvió, o tocá «No» en ¿Devolvió algo?"})
        self.assertTrue(out["noCode"]["first"])
        self.assertTrue(out["noCode"]["second"], "a second tap keeps it")
        self.assertIn("Entregado sin código", out["noCode"]["status"])
        self.assertIn("deshacer", out["noCode"]["status"])
        self.assertTrue(out["noCode"]["button"])
        self.assertFalse(out["noCodeUndo"])
        self.assertIn("Entregado sin código", out["lockedStatus"])
        # 2. «Ya estaba pagado»
        self.assertTrue(out["paidView"])
        self.assertFalse(out["unpaidView"])
        self.assertIn("Mercado Pago · 20/11", out["confirmText"])
        self.assertIn("transferencia de $ 492.000", out["confirmText"])
        self.assertEqual(out["alreadyPaid"], {"already_paid": True, "cash": 0, "transfer": "no",
                                              "transfer_amount": 0, "no_payment": False,
                                              "state": "already_paid", "summary": 1})
        self.assertEqual(out["saveZero"], "¿Ya estaba pagado?")
        self.assertEqual(out["warned"]["state"], "not_paid")
        self.assertTrue(out["warned"]["no_payment"])
        self.assertIn("todavía debe", out["warned"]["warning"])
        self.assertIn("Ya está pagado", out["notice"])
        self.assertIn("Mercado Pago · 20/11", out["notice"])
        self.assertEqual(out["noNotice"], "")
