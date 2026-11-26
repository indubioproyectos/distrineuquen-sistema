"""19.0.1.129.28: «Reparto v2» behind distri_pedido.reparto_v2
(models/reparto_v2.py, static/src/reparto/app5.js), the owner-approved «cobro
del repartidor» proposal, stages 1 and 2.

Flag OFF: the route payload, the visit rules, the charges and the late
receipts are exactly 129.27's. Flag ON: what is due in the payload, «Ya estaba
pagado» with debt accepted «a revisar», a partial payment charged to the
driver with the salesperson told (decision C exemption, pending owner
confirmation), a late receipt closing the charge, «Mis comprobantes a
reclamar», the open-route reminder and the driver's own order. The old app's
payloads keep working with the flag on. Every path is checked against the
receivable fingerprint and the customer's balance: no customer's receivable
moves. Synthetic data only.
"""
import json
import os
import shutil
import subprocess
from datetime import timedelta
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.reparto_v2 import (
    PARAM_EXEMPT, PARAM_REPARTO_V2, PARTIAL_SUMMARY, REVIEW_SUMMARY, exempt_on, reparto_v2_on)
from odoo.addons.distri_pedido.tests.test_v129_25_reparto import V12925Mixin


class V12928Mixin(V12925Mixin):

    @classmethod
    def _setup_v129_28(cls):
        cls._setup_v129_25()
        company = cls.env.company
        cls.seller = new_test_user(
            cls.env, login="vendedor_v129_28", name="Vendedora V12928",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=company.id, company_ids=[Command.set(company.ids)])

    def _v2(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_REPARTO_V2, "1" if on else "0")
        self.assertEqual(reparto_v2_on(self.env), on)


@tagged("post_install", "-at_install", "distri_v129_28")
class TestV12928RepartoV2(V12928Mixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_v129_28()
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Reparto V2 V12928",
                                                     "company_type": "company"})
        cls.Promise = cls.env["distri.promised.transfer"].sudo()
        cls.Case = cls.env["distri.route.case"].sudo()

    def _invoiced_order(self, partner=None, qty=10, price=100.0):
        order = self._order(partner or self.partner, self.product, qty=qty, price=price)
        order.sudo().user_id = self.seller
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _ledger(self, partner=None):
        partner = (partner or self.partner).commercial_partner_id
        self.env.flush_all()
        partner.invalidate_recordset(["credit"])
        return self.Promise._distri_ledger_fingerprint(), partner.credit

    def _visit_ok(self, route, stop, captured="2026-09-26T15:00:00.000Z", **kw):
        res = self._sync([self._event(route, "visit", self._visit(stop, **kw), captured=captured)])
        self.assertEqual(res[0]["status"], "ok", res)
        return res[0]

    def _short(self, stop):
        return self.Case.search([("stop_id", "=", stop.id), ("kind", "=", "short")])

    def _todos(self, record, summary):
        return record.sudo().activity_ids.filtered(lambda a: a.summary == summary)

    # ------------------------------------------------------------ flag off

    def test_flag_off_is_129_27(self):
        self._v2(False)
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        data = route.with_user(self.driver).sudo()._app_payload()
        self.assertNotIn("reparto_v2", data)
        self.assertFalse({"v2_due", "v2_exempt"} & set(data["stops"][0]))
        before = self._ledger()
        # «Ya estaba pagado» on an order that owes: «No pagó» with the warning
        res = self._visit_ok(route, stop, already_paid=True)
        self.assertIn("warning", res)
        self.assertEqual(stop.state, "not_paid")
        self.assertFalse(stop.paid_confirm_state)
        self.assertEqual(self.Promise.search([("stop_id", "=", stop.id)]).kind, "unpaid")
        self.assertEqual(self._ledger(), before)
        # a partial cash payment charges nothing
        self._visit_ok(route, stop, captured="2026-09-26T15:01:00.000Z", cash=600)
        self.assertEqual(stop.state, "paid")
        self.assertFalse(self._short(stop))
        self.assertFalse(self._todos(order, PARTIAL_SUMMARY))
        # a short transfer: the 129.27 case and text
        self._visit_ok(route, stop, captured="2026-09-26T15:02:00.000Z", transfer="yes",
                       transfer_amount=600, receipt_answer="not_yet")
        case = self._short(stop)
        self.assertEqual(case.amount, -400)
        self.assertIn("transfirió", case.candidates)
        self.assertIn("y el pedido de hoy era de", case.candidates)
        self.assertEqual(invoice.amount_residual, 1000)

    def test_flag_off_late_receipt_still_waits_for_administracion(self):
        self._v2(False)
        order = self._order(self.partner, self.product, qty=1, price=5000)
        route = self._route(order)
        self._visit_ok(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z", transfer="yes",
                       transfer_amount=5000, receipt_answer="not_yet")
        promise = self.Promise.search([("stop_id", "=", route.stop_ids.id)])
        self.Promise._distri_cron_expire()
        case = promise.charge_case_id
        self.assertEqual((promise.state, case.state), ("overdue", "open"))
        promise.action_mark_arrived()
        self.assertEqual((promise.state, case.state), ("fulfilled", "open"))

    # ------------------------------------------------------------ payload

    def test_payload_with_flag_on(self):
        self._v2()
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        data = route.with_user(self.driver).sudo()._app_payload()
        self.assertTrue(data["reparto_v2"])
        stop_data = data["stops"][0]
        self.assertEqual(stop_data["v2_due"], 1000)
        self.assertFalse(stop_data["v2_exempt"])
        self.partner.sudo().distri_cuenta_corriente = True
        self.assertTrue(route.with_user(self.driver).sudo()._app_payload()["stops"][0]["v2_exempt"])
        self.env["ir.config_parameter"].sudo().set_param(PARAM_EXEMPT, "0")
        self.assertFalse(exempt_on(self.env))
        self.assertFalse(route.with_user(self.driver).sudo()._app_payload()["stops"][0]["v2_exempt"])

    # ------------------------------------------------------------ «Ya estaba pagado»

    def test_already_paid_with_debt_is_accepted_to_review(self):
        self._v2()
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        before = self._ledger()
        res = self._visit_ok(route, stop, already_paid=True)
        self.assertNotIn("warning", res, "accepted: the phone keeps «Ya estaba pagado»")
        self.assertEqual(stop.state, "already_paid")
        self.assertEqual(stop.paid_confirm_state, "to_review")
        self.assertFalse(self.Promise.search([("stop_id", "=", stop.id)]), "nothing at his name")
        self.assertFalse(self.Case.search([("stop_id", "=", stop.id)]))
        todos = self._todos(order, REVIEW_SUMMARY)
        self.assertTrue(todos, "administración reviews it")
        self.assertIn(order.name, todos[0].note)
        self.assertIn(self.admin_user, todos.user_id)
        self.assertTrue(any(REVIEW_SUMMARY in (m.body or "") for m in route.message_ids))
        since = stop.paid_confirm_since
        # an edit of the same visit: no second To-Do, the same «since»
        self._visit_ok(route, stop, captured="2026-09-26T15:05:00.000Z", already_paid=True,
                       comment="Mostró el pago")
        self.assertEqual(len(self._todos(order, REVIEW_SUMMARY)), len(todos))
        self.assertEqual(stop.paid_confirm_since, since)
        self.assertEqual(self._ledger(), before)
        self.assertEqual(invoice.amount_residual, 1000)
        # the close goes through
        res = self._sync([self._event(route, "close", {}, captured="2026-09-26T17:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        # once the order has nothing left to pay the cron confirms it
        self.env["distri.route.stop"]._distri_cron_paid_to_confirm()
        self.assertEqual(stop.paid_confirm_state, "to_review")
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_bank"].id,
            }).action_create_payments()
        self.assertEqual(invoice.amount_residual, 0)
        self.env["distri.route.stop"]._distri_cron_paid_to_confirm()
        self.assertEqual(stop.paid_confirm_state, "confirmed")
        self.assertFalse(self._todos(order, REVIEW_SUMMARY), "the To-Dos are done")

    def test_already_paid_covered_by_a_receipt_stays_to_confirm(self):
        self._v2()
        order, _invoice = self._invoiced_order()
        card = self._card(self.partner, 1000, order=order)
        route = self._route(order)
        stop = route.stop_ids
        self._visit_ok(route, stop, already_paid=True, to_confirm=True)
        self.assertEqual((stop.state, stop.paid_confirm_state, stop.paid_confirm_card_id),
                         ("already_paid", "to_confirm", card))
        self.assertFalse(self._todos(order, REVIEW_SUMMARY))

    # ------------------------------------------------------------ partial

    def test_partial_payment_is_the_drivers_and_the_seller_hears(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        # the phone downloads the route: «Le toca pagar $ 1.000»
        self._v2()
        self.assertEqual(route.with_user(self.driver).sudo()._app_payload()["stops"][0]["v2_due"], 1000)
        self.assertEqual((stop.v2_due_known, stop.v2_due_at_download), (True, 1000))
        # the cash itself moves the balance as in 129.27 (its payment «a
        # rendir»): the same visit with the flag off is the reference
        self._v2(False)
        self._visit_ok(route, stop, cash=600)
        self.assertFalse(self._short(stop))
        before = self._ledger()
        self._v2()
        self._visit_ok(route, stop, captured="2026-09-26T15:01:00.000Z", cash=600)
        case = self._short(stop)
        self.assertEqual((case.amount, case.state), (-400, "open"))
        self.assertIn("pagó $ 600", case.candidates.replace("\xa0", " "))
        self.assertIn("queda a cargo del repartidor", case.candidates)
        todo = self._todos(order, PARTIAL_SUMMARY)
        self.assertEqual(todo.user_id, self.seller)
        self.assertIn("a cargo del repartidor", todo.note)
        self.assertEqual(stop.partial_notice_amount, 400)
        self.assertEqual(self._ledger(), before, "the charge moves no receivable")
        # a download after the visit keeps what was due before it
        self.env["distri.route"].browse(route.id).with_user(self.driver).sudo()._app_payload()
        self.assertEqual(stop.v2_due_at_download, 1000)
        # the same amount again: no second To-Do
        self._visit_ok(route, stop, captured="2026-09-26T15:02:00.000Z", cash=600)
        self.assertEqual(len(self._todos(order, PARTIAL_SUMMARY)), 1)
        self.assertEqual(self._ledger(), before)
        # mixed short: cash plus transfer
        self._visit_ok(route, stop, captured="2026-09-26T15:03:00.000Z", cash=300, transfer="yes",
                       transfer_amount=500, receipt_answer="not_yet")
        self.assertEqual(self._short(stop).amount, -200)
        # paid in full: the charge goes
        self._visit_ok(route, stop, captured="2026-09-26T15:04:00.000Z", cash=1000)
        self.assertFalse(self._short(stop))
        self.assertEqual(stop.partial_notice_amount, 0)

    def test_decision_c_current_account_customer_is_exempt(self):
        self.partner.sudo().distri_cuenta_corriente = True
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        # 19.0.1.129.32: the phone downloaded the route with the flag on
        self._v2()
        route.with_user(self.driver).sudo()._app_payload()
        self._v2(False)
        self._visit_ok(route, stop, cash=600)
        before = self._ledger()
        self._v2()
        self._visit_ok(route, stop, captured="2026-09-26T15:01:00.000Z", cash=600)
        self.assertFalse(self._short(stop), "the difference stays on his account")
        todo = self._todos(order, PARTIAL_SUMMARY)
        self.assertEqual(todo.user_id, self.seller)
        self.assertIn("a cuenta del cliente", todo.note)
        self.assertEqual(self._ledger(), before)
        # the exemption has its own switch
        self.env["ir.config_parameter"].sudo().set_param(PARAM_EXEMPT, "0")
        self._visit_ok(route, stop, captured="2026-09-26T15:02:00.000Z", cash=600)
        self.assertEqual(self._short(stop).amount, -400)
        self.assertEqual(self._ledger(), before)

    def test_due_counts_what_was_paid_before(self):
        self._v2()
        order, invoice = self._invoiced_order()
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_bank"].id, "amount": 300,
            }).action_create_payments()
        self.assertEqual(invoice.amount_residual, 700)
        route = self._route(order)
        stop = route.stop_ids
        self.assertEqual(route.with_user(self.driver).sudo()._app_payload()["stops"][0]["v2_due"], 700)
        self._visit_ok(route, stop, cash=700)
        self.assertFalse(self._short(stop), "he collected all that was due")
        self._visit_ok(route, stop, captured="2026-09-26T15:01:00.000Z", cash=500)
        self.assertEqual(self._short(stop).amount, -200)

    def test_decision_c_hidden_list_customer_is_exempt(self):
        self._v2()
        wholesale = self.env["res.partner"].create({"name": "Mayorista V12928", "company_type": "company",
                                                   "distri_es_mayorista": True})
        order, _invoice = self._invoiced_order(partner=wholesale)
        route = self._route(order)
        self._visit_ok(route, route.stop_ids, cash=100)
        self.assertFalse(self._short(route.stop_ids))

    def test_old_app_payloads_still_work_with_the_flag_on(self):
        self._v2()
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        before = self._ledger()
        # the app of before 19.0.1.25.0: «Ya transfirió»
        self._visit_ok(route, stop, transfer="done")
        self.assertEqual((stop.transfer, stop.transfer_amount, stop.receipt_answer),
                         ("yes", 1000, "not_yet"))
        # the app of before 19.0.1.86.0: «Ya lo reenvié»
        self._visit_ok(route, stop, captured="2026-09-26T15:01:00.000Z", transfer="yes",
                       transfer_amount=1000, receipt_answer="forwarded")
        self.assertEqual(stop.receipt_answer, "forwarded")
        # 129.27's app: «No pagó nada», «Entregar igual», no v2 keys at all
        self._visit_ok(route, stop, captured="2026-09-26T15:02:00.000Z", no_payment=True)
        self.assertEqual(stop.state, "not_paid")
        self.assertEqual(stop.delivery_state, "delivered_no_code")
        self.assertEqual(self._ledger(), before)
        self.assertEqual(invoice.amount_residual, 1000)

    # ------------------------------------------------------------ late receipt

    def test_late_receipt_closes_the_charge(self):
        self._v2()
        order = self._order(self.partner, self.product, qty=1, price=5000)
        route = self._route(order)
        self._visit_ok(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z", transfer="yes",
                       transfer_amount=5000, receipt_answer="not_yet")
        promise = self.Promise.search([("stop_id", "=", route.stop_ids.id)])
        self.Promise._distri_cron_expire()
        case = promise.charge_case_id
        self.assertEqual((promise.state, case.state), ("overdue", "open"),
                         "the deadline does not change")
        before = self._ledger()
        promise.action_mark_arrived()
        self.assertEqual((promise.state, case.state), ("fulfilled", "closed"))
        self.assertIn("se cerró solo", case.resolution)
        self.assertFalse(any("Administración decide" in (m.body or "") for m in case.message_ids))
        self.assertEqual(self._ledger(), before)
        # the receipt turns out to be a copy: the charge is the driver's again
        promise._distri_reopen()
        self.assertEqual((promise.state, case.state), ("overdue", "open"))
        self.assertFalse(case.resolution)

    # ------------------------------------------------------------ home

    def test_home_lists_the_drivers_own_items_and_open_routes(self):
        self._v2()
        old_order = self._order(self.partner, self.product, qty=1, price=25000)
        old_route = self._route(old_order)
        old_route.sudo().date = fields.Date.today() - timedelta(days=3)
        self._visit_ok(old_route, old_route.stop_ids, transfer="yes", transfer_amount=25000,
                       receipt_answer="not_yet")
        order, _invoice = self._invoiced_order()
        route = self._route(order)
        # 19.0.1.129.32: the phone downloaded the route («Le toca pagar»)
        route.with_user(self.driver).sudo()._app_payload()
        self._visit_ok(route, route.stop_ids, cash=600)
        other = self._route(self._order(self.partner, self.product, qty=1, price=700),
                            driver=self.other_driver)
        self._sync([self._event(other, "visit", self._visit(other.stop_ids, no_payment=True))],
                   user=self.other_driver)
        home = self.env["distri.route"]._distri_v2_home(self.driver)
        receipts = {r["stop_id"]: r for r in home["receipts"]}
        self.assertIn(old_route.stop_ids.id, receipts)
        self.assertEqual(receipts[old_route.stop_ids.id]["route_id"], old_route.id)
        self.assertNotIn(other.stop_ids.id, receipts, "another driver's")
        self.assertEqual([c["amount"] for c in home["charges"]], [400])
        self.assertEqual(home["charges"][0]["kind"], "short")
        self.assertEqual([r["id"] for r in home["stale_routes"]], [old_route.id])
        # an expired receipt is listed once, with its charge
        self.Promise._distri_cron_expire()
        home = self.env["distri.route"]._distri_v2_home(self.driver)
        self.assertEqual([c["kind"] for c in home["charges"]], ["short"])
        self.assertEqual({r["state"] for r in home["receipts"]}, {"overdue"})

    # ------------------------------------------------------------ #4 order

    def test_driver_order(self):
        orders = self._order(self.partner, self.product, qty=1) | self._order(
            self.partner, self.product, qty=2) | self._order(self.partner, self.product, qty=3)
        route = self._route(orders)
        s1, s2, s3 = route.stop_ids.sorted(lambda s: (s.sequence, s.id))
        route._distri_v2_set_order([s3.id, s1.id])
        self.assertEqual(route.stop_ids.sorted(lambda s: (s.sequence, s.id)).ids, [s3.id, s1.id, s2.id])
        ids = [s["id"] for s in route.with_user(self.driver).sudo()._app_payload()["stops"]]
        self.assertEqual(ids, [s3.id, s1.id, s2.id], "another phone downloads the same order")
        other = self._route(self._order(self.partner, self.product, qty=1))
        with self.assertRaises(UserError):
            route._distri_v2_set_order([other.stop_ids.id])
        route.sudo().state = "closed"
        with self.assertRaises(UserError):
            route._distri_v2_set_order([s1.id])

    # ------------------------------------------------------------ app (node)

    def test_app_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v129_28.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # flag off: the same screens and payload as without app5.js
        self.assertTrue(out["flagOffSame"])
        self.assertTrue(out["flagOffOldVisit"])
        self.assertTrue(out["flagOffNoOrderLink"])
        self.assertTrue(out["flagOffNoHome"])
        self.assertTrue(out["flagOffNoDrag"])
        self.assertTrue(out["oldServerSame"], "a server without /reparto/api/home")
        self.assertEqual(out["flagOffPayload"]["cash"], 1000)
        self.assertEqual(out["flagOffPayload"]["keys"], out["oldAppPayload"]["keys"])
        # the old app against the flag-on server
        self.assertTrue(out["oldAppOldVisit"])
        self.assertEqual(out["oldAppPayload"]["cash"], 600)
        # the new visit
        self.assertTrue(all(out["v2Visit"].values()), out["v2Visit"])
        self.assertEqual(out["cash"], {"taps": 4, "cash": 1000, "transfer": "no", "without_code": True,
                                       "returns_answer": "none", "already_paid": False,
                                       "no_payment": False, "state": 1000})
        self.assertTrue(out["visitLines"])
        self.assertTrue(out["transferChoices"])
        self.assertIn("Lo manda después", out["transferNeedsReceipt"])
        self.assertEqual(out["transfer"], {"cash": 0, "transfer": "yes", "amount": 1000,
                                           "receipt": "not_yet"})
        self.assertTrue(out["partialWarning"])
        self.assertIn("Faltan $ 400: quedan a tu cargo", out["partialConfirm"])
        self.assertEqual(out["partial"], {"cash": 600, "transfer": "no"})
        self.assertTrue(out["exemptWarning"])
        self.assertEqual(out["mixed"], 700)
        self.assertTrue(out["alreadyWithDebt"])
        self.assertEqual(out["already"], {"already_paid": True, "cash": 0, "transfer": "no",
                                          "no_payment": False})
        self.assertTrue(out["paidPreselected"])
        # «Mis comprobantes a reclamar», the reminder, the order
        self.assertTrue(out["homeCard"])
        self.assertTrue(out["loadBlock"])
        self.assertTrue(out["reminder"])
        self.assertTrue(out["reminderOff"])
        self.assertTrue(out["homeWithoutRoute"])
        # 19.0.1.129.48: the block on the home screens
        self.assertEqual(out["block"].pop("rows"), 1)
        self.assertTrue(all(out["block"].values()), out["block"])
        self.assertEqual(out["blockMany"], {"rows": 3, "more": True, "first": True, "hidden": True})
        self.assertEqual(out["blockEmpty"], {"calm": True, "rows": 0, "noLink": True})
        self.assertTrue(out["blockWording"])
        self.assertTrue(all(out["claims"].values()), out["claims"])
        self.assertEqual(out["claimPhoto"], {"route": 55, "stop": 501, "amount": 25000})
        # 19.0.1.129.48: the block after the visits on #visitas, on top elsewhere
        self.assertTrue(out["blockAtEnd"])
        self.assertTrue(out["reminderOnTop"])
        self.assertTrue(out["loadBlockOnTop"])
        # visits acted on below the pending ones, muted, not draggable
        self.assertTrue(all(out["grouped"].values()), out["grouped"])
        self.assertTrue(out["recordedMovesDown"])
        # the order by long press and drag, no arrows and no #orden screen
        self.assertTrue(all(out["dragUi"].values()), out["dragUi"])
        self.assertTrue(all(out["tap"].values()), out["tap"])
        self.assertTrue(out["scrollNoLift"])
        self.assertTrue(out["lifted"])
        self.assertTrue(out["noRedrawWhileDragging"])
        self.assertEqual(out["domWhileDragging"], "802,803,801,804,805")
        self.assertEqual(out["orderAfterDrag"], [802, 803, 801, 804, 805])
        self.assertTrue(out["clickAfterDropSwallowed"])
        self.assertTrue(out["tapAfterDropOpens"])
        self.assertEqual(out["orderPosted"], [802, 803, 801, 804, 805])
        self.assertTrue(out["orderRerendered"])
        # the visit acted on (803) keeps its place when the others move
        self.assertEqual(out["actedKeepsPlace"], [805, 803, 802, 801, 804])
        self.assertEqual(out["orderPosted2"], [805, 803, 802, 801, 804])
        self.assertEqual(out["orderAfterDownload"], [805, 803, 802, 801, 804])
        # the outbox of the older app survives the update
        self.assertTrue(out["queueSent"])
        self.assertTrue(out["queueEmpty"])


@tagged("post_install", "-at_install", "distri_v129_28")
class TestV12928Http(V12928Mixin, HttpCase):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_v129_28()
        cls.product = cls.env["product.product"].create({
            "name": "Producto v129.28", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.customer = cls.env["res.partner"].create({"name": "KIOSCO V12928 API",
                                                      "city": "Centenario"})

    def _post_order(self, route, stop_ids):
        return self.url_open("/reparto/api/order", data=json.dumps({
            "route_id": route.id, "stop_ids": stop_ids}), headers={"Content-Type": "application/json"})

    def test_endpoints_flag_off(self):
        self._v2(False)
        route = self._route(self._order(self.customer, self.product, qty=1)
                            | self._order(self.customer, self.product, qty=2))
        self.authenticate(self.driver.login, self.driver.login)
        self.assertEqual(self.url_open("/reparto/api/home").json(), {"ok": True, "v2": False})
        res = self._post_order(route, list(reversed(route.stop_ids.ids)))
        self.assertEqual(res.status_code, 404)
        data = self.url_open("/reparto/api/route?id=%s" % route.id).json()
        self.assertNotIn("reparto_v2", data["route"])
        self.assertIn("app5.js?v=", self.url_open("/reparto").text)

    def test_endpoints_flag_on(self):
        self._v2()
        route = self._route(self._order(self.customer, self.product, qty=1)
                            | self._order(self.customer, self.product, qty=2))
        self.authenticate(self.driver.login, self.driver.login)
        data = self.url_open("/reparto/api/home").json()
        self.assertTrue(data["v2"])
        self.assertEqual(data["user_id"], self.driver.id)
        self.assertEqual((data["receipts"], data["charges"], data["stale_routes"]), ([], [], []))
        wanted = list(reversed(route.stop_ids.sorted(lambda s: (s.sequence, s.id)).ids))
        res = self._post_order(route, wanted)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(route.stop_ids.sorted(lambda s: (s.sequence, s.id)).ids, wanted)
        data = self.url_open("/reparto/api/route?id=%s" % route.id).json()
        self.assertTrue(data["route"]["reparto_v2"])
        self.assertEqual([s["id"] for s in data["route"]["stops"]], wanted)
        # not JSON, another driver's route
        res = self.url_open("/reparto/api/order", data="x=1")
        self.assertEqual(res.status_code, 415)
        self.authenticate(self.other_driver.login, self.other_driver.login)
        self.assertEqual(self._post_order(route, wanted).status_code, 404)

    def test_new_phone_gets_todays_route(self):
        """#37: the phone keeps its state per user (19.0.1.113.0); a new phone
        asks without an id and, with the flag on, gets today's route before
        an older one left open."""
        old = self._route(self._order(self.customer, self.product, qty=1))
        old.sudo().date = fields.Date.today() - timedelta(days=4)
        today = self._route(self._order(self.customer, self.product, qty=2))
        self.authenticate(self.driver.login, self.driver.login)
        self._v2(False)
        self.assertEqual(self.url_open("/reparto/api/route").json()["route"]["id"], old.id,
                         "129.27: the oldest route «En reparto»")
        self._v2()
        data = self.url_open("/reparto/api/route").json()
        self.assertEqual(data["route"]["id"], today.id)
        self.assertEqual({r["id"] for r in data["routes"]}, {old.id, today.id})
        # the old one still opens by its id, and the reminder lists it
        self.assertEqual(self.url_open("/reparto/api/route?id=%s" % old.id).json()["route"]["id"],
                         old.id)
        home = self.url_open("/reparto/api/home").json()
        self.assertEqual([r["id"] for r in home["stale_routes"]], [old.id])
