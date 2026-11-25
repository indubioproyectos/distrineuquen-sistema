"""19.0.1.129.25: «Ya estaba pagado · a confirmar» (owner's decision A,
24/11/2026; models/reparto_a_confirmar.py, reparto_pagos.py) and «Reabrir»
a closed route (#105; models/reparto.py `_distri_reopen`, /reparto/api/reopen,
/caja/rendicion/<id>/reabrir, /rendiciones/rutas/<id>/reabrir).

Owner's rule until the accounts audit closes: no customer balance changes.
Every path here is checked against the receivable total, the number of
entries, payments and reconciliations (`_distri_ledger_fingerprint`) and the
customer's own balance. Synthetic data only.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import timedelta
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.reparto_a_confirmar import CONFIRM_SUMMARY
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


class V12925Mixin(CajaMixin):

    seq = 0

    @classmethod
    def _setup_v129_25(cls):
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)
        company = cls.env.company
        cls.admin_user = new_test_user(
            cls.env, login="denis_v129_25", name="Denis Prueba",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=company.id, company_ids=[Command.set(company.ids)])

    def _card(self, partner, amount, state="draft", order=None, **receipt):
        """A receipt card uploaded by the customer from the order page
        (`order`), not credited unless `state` says so."""
        type(self).seq += 1
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "comprobante-v12925-%d.png" % self.seq, "raw": PNG,
            "mimetype": "image/png"})
        values = {"wa_message_id": "wamid.v12925.%d" % self.seq, "direction": "in",
                  "message_type": "image", "media_mime_type": "image/png",
                  "attachment_id": attachment.id, "distri_partner_id": partner.id}
        if order:
            values["distri_customer_order_id"] = order.id
        values.update(receipt)
        message = self.env["distri.whatsapp.message"].sudo().create(values)
        return self.env["distri.mp.collection"].sudo().create({
            "kind": "verify", "amount": amount, "date": fields.Datetime.now(),
            "partner_id": partner.id, "state": state, "receipt_id": message.id})

    def _closed_route(self, order):
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            stop, outcome="not_delivered", reason="Cerrado"))])
        self.assertEqual(res[0]["status"], "ok", res)
        res = self._sync([self._event(route, "close", {}, captured="2026-09-26T17:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(route.state, "closed")
        return route


@tagged("post_install", "-at_install", "distri_v129_25")
class TestV12925ToConfirm(V12925Mixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_v129_25()
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén A Confirmar V12925",
                                                     "company_type": "company"})

    def _invoiced_order(self, qty=10, price=100.0):
        order = self._order(self.partner, self.product, qty=qty, price=price)
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def _info(self, stop):
        return stop.with_user(self.driver)._app_payload()

    def _ledger(self):
        self.env.flush_all()
        self.partner.invalidate_recordset(["credit"])
        return (self.env["distri.promised.transfer"]._distri_ledger_fingerprint(),
                self.partner.commercial_partner_id.credit)

    def _promise(self, stop):
        return self.env["distri.promised.transfer"].sudo().search([("stop_id", "=", stop.id)])

    def _to_confirm_stop(self):
        order, invoice = self._invoiced_order()  # $ 1.000
        card = self._card(self.partner, 1000, order=order)
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            stop, already_paid=True, to_confirm=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertNotIn("warning", res[0])
        return order, invoice, card, route, stop

    # ------------------------------------------------------------ «covers»

    def test_uncredited_receipt_covering_the_due_enables_to_confirm(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        stop = route.stop_ids
        data = self._info(stop)
        self.assertFalse(data["payments_to_confirm"], "no receipt at all")
        short = self._card(self.partner, 900, order=order)
        data = self._info(stop)
        self.assertFalse(data["payments_to_confirm"], "$ 900 does not cover $ 1.000")
        card = self._card(self.partner, 999.5, order=order)
        data = self._info(stop)
        self.assertFalse(data["payments_paid"])
        self.assertTrue(data["payments_to_confirm"], "within the tolerance")
        self.assertEqual(data["payments_to_confirm_amount"], 999.5)
        self.assertEqual(data["payments_due"], 1000)
        info = route._load_status_payload()["payments"][str(stop.id)]
        self.assertTrue(info["to_confirm"])
        self.assertEqual(info["to_confirm_card_id"], card.id)
        # it only informs: nothing was credited
        self.assertEqual(invoice.amount_residual, 1000)
        self.assertEqual((card.state, short.state), ("draft", "draft"))

    def test_what_does_not_cover(self):
        order, _invoice = self._invoiced_order()
        stop = self._route(order).stop_ids
        # the customer's receipt of another order (not tied to this one)
        self._card(self.partner, 5000)
        # a photo taken in the driver's app is the driver's own transfer
        self._card(self.partner, 1000, distri_app_order_id=order.id, distri_from_app=True)
        # discarded on the board
        self._card(self.partner, 1000, order=order, state="ignored")
        self.assertFalse(self._info(stop)["payments_to_confirm"])
        # «Autorizado para entregar» is still not credited: it covers
        self._card(self.partner, 1000, order=order, state="authorized")
        self.assertTrue(self._info(stop)["payments_to_confirm"])

    # ------------------------------------------------------------ the visit

    def test_to_confirm_visit_charges_nothing_and_keeps_the_balance(self):
        order, invoice = self._invoiced_order()  # $ 1.000
        card = self._card(self.partner, 1000, order=order)
        route = self._route(order)
        stop = route.stop_ids
        before = self._ledger()
        res = self._sync([self._event(route, "visit", self._visit(
            stop, already_paid=True, to_confirm=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertNotIn("warning", res[0])
        self.assertEqual(stop.state, "already_paid", "delivered, not «No pagó»")
        self.assertEqual(stop.paid_confirm_state, "to_confirm")
        self.assertEqual(stop.paid_confirm_card_id, card)
        self.assertTrue(stop.paid_confirm_since)
        self.assertEqual((stop.cash_amount, stop.transfer, stop.transfer_amount), (0, "no", 0))
        self.assertFalse(self._promise(stop), "no pending item at the driver's name")
        self.assertFalse(self.env["distri.route.case"].sudo().search([("stop_id", "=", stop.id)]),
                         "no charge to the driver")
        self.assertFalse(self.env["account.payment"].sudo().search(
            [("distri_driver_stop_id", "=", stop.id)]), "no cash payment «a rendir»")
        driver_items = self.env["distri.promised.transfer"].sudo()._distri_driver_payload(
            self.driver)
        self.assertFalse([i for i in driver_items if i["stop_id"] == stop.id])
        self.assertTrue(stop._app_visit()["to_confirm"])
        self.assertEqual(card.state, "draft", "nothing credited")
        self.assertEqual(invoice.amount_residual, 1000)
        # the card is now taken: another visit cannot rely on it
        self.assertEqual(self._info(stop)["payments_to_confirm_amount"], 1000,
                         "its own visit still sees it")
        res = self._sync([self._event(route, "close", {}, captured="2026-09-26T17:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(self._ledger(), before,
                         "no entry, payment or reconciliation; the customer's balance is the same")

    def test_the_visit_alone_does_not_move_the_balance(self):
        order, invoice = self._invoiced_order()
        self._card(self.partner, 1000, order=order)
        route = self._route(order)
        before = self._ledger()
        res = self._sync([self._event(route, "visit", self._visit(
            route.stop_ids, already_paid=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(route.stop_ids.paid_confirm_state, "to_confirm",
                         "an older app without the flag gets the same")
        self.assertEqual(self._ledger(), before)

    def test_edited_visit_clears_the_flag(self):
        _order, _invoice, card, route, stop = self._to_confirm_stop()
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=1000),
                                      captured="2026-09-26T16:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.state, "paid")
        self.assertFalse(stop.paid_confirm_state)
        self.assertFalse(stop.paid_confirm_card_id)
        self.assertEqual(card.state, "draft")

    def test_receipt_discarded_before_the_sync_is_not_paid_with_the_warning(self):
        order, _invoice = self._invoiced_order()
        card = self._card(self.partner, 1000, order=order)
        route = self._route(order)
        card.write({"state": "ignored"})
        res = self._sync([self._event(route, "visit", self._visit(
            route.stop_ids, already_paid=True, to_confirm=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertIn("todavía debe", res[0]["warning"])
        self.assertEqual(route.stop_ids.state, "not_paid")
        self.assertFalse(route.stop_ids.paid_confirm_state)

    # ------------------------------------------------------------ 48 h

    def test_confirmed_when_the_report_brings_it(self):
        _order, _invoice, card, route, stop = self._to_confirm_stop()
        Stop = self.env["distri.route.stop"]
        before = self._ledger()
        confirmed, escalated = Stop._distri_cron_paid_to_confirm()
        self.assertFalse(confirmed | escalated, "nothing yet")
        income = self.env["distri.mp.collection"].sudo().create({
            "kind": "income", "amount": 1000, "date": fields.Datetime.now(),
            "source_id": "v12925-income-1"})
        card.write({"income_id": income.id})
        confirmed, escalated = Stop._distri_cron_paid_to_confirm()
        self.assertEqual(confirmed, stop)
        self.assertEqual(stop.paid_confirm_state, "confirmed")
        self.assertTrue(stop.paid_confirm_done_at)
        self.assertIn("Pago confirmado", route.message_ids[:1].body)
        self.assertEqual(self._ledger(), before)

    def test_escalated_to_administracion_after_48h(self):
        _order, _invoice, card, route, stop = self._to_confirm_stop()
        Stop = self.env["distri.route.stop"]
        since = stop.paid_confirm_since
        before = self._ledger()
        _c, escalated = Stop._distri_cron_paid_to_confirm(now=since + timedelta(hours=47))
        self.assertFalse(escalated)
        _c, escalated = Stop._distri_cron_paid_to_confirm(now=since + timedelta(hours=49))
        self.assertEqual(escalated, stop)
        self.assertEqual(stop.paid_confirm_state, "escalated")
        todos = card.activity_ids.filtered(lambda a: a.summary == CONFIRM_SUMMARY)
        admins = self.env.ref("distri_pedido.grupo_administracion").all_user_ids.filtered(
            lambda u: u.active and not u.share and u != self.env.ref("base.user_root"))
        self.assertEqual(todos.user_id, admins, "one To-Do per administración user")
        self.assertIn(self.admin_user, todos.user_id)
        self.assertIn(self.manager, todos.user_id, "gerencia is administración too")
        self.assertNotIn(self.cashier, todos.user_id)
        self.assertIn("48 h", todos[0].note)
        self.assertIn("No se cargó nada al repartidor", todos[0].note)
        self.assertIn(CONFIRM_SUMMARY, route.message_ids[:1].body)
        self.assertFalse(self.env["distri.route.case"].sudo().search([("stop_id", "=", stop.id)]))
        # never twice
        Stop._distri_cron_paid_to_confirm(now=since + timedelta(hours=60))
        self.assertEqual(card.activity_ids.filtered(lambda a: a.summary == CONFIRM_SUMMARY),
                         todos)
        # credited later on the board (simulated: only the card's state)
        card.write({"state": "confirmed"})
        confirmed, _e = Stop._distri_cron_paid_to_confirm(now=since + timedelta(hours=61))
        self.assertEqual(confirmed, stop)
        self.assertFalse(card.activity_ids.filtered(lambda a: a.summary == CONFIRM_SUMMARY),
                         "the To-Dos are done")
        self.assertEqual(self._ledger(), before)

    def test_the_hourly_cron_runs_it(self):
        _order, _invoice, card, _route, stop = self._to_confirm_stop()
        stop.sudo().paid_confirm_since = fields.Datetime.now() - timedelta(hours=49)
        self.env["distri.promised.transfer"]._distri_cron_expire()
        self.assertEqual(stop.paid_confirm_state, "escalated")
        self.assertTrue(card.activity_ids.filtered(lambda a: a.summary == CONFIRM_SUMMARY))

    # ------------------------------------------------------------ app (node)

    def test_app_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v129_25.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # «Ya estaba pagado · a confirmar»
        self.assertTrue(out["confirmButton"], "the button with a covering receipt")
        self.assertIn("Ya estaba pagado · a confirmar", out["confirmView"])
        self.assertIn("$ 1.000", out["confirmView"])
        self.assertFalse(out["noButton"], "no button without one")
        self.assertEqual(out["confirmTitle"], "¿Ya estaba pagado · a confirmar?")
        self.assertIn("administración lo verifica con Mercado Pago", out["confirmText"])
        self.assertIn("No queda nada a tu nombre", out["confirmText"])
        self.assertEqual(out["sent"], {"already_paid": True, "to_confirm": True, "cash": 0,
                                       "transfer": "no", "no_payment": False})
        self.assertEqual(out["state"], "already_paid")
        self.assertIn("A CONFIRMAR", out["list"])
        self.assertIn("a confirmar por administración", out["list"])
        self.assertEqual(out["summary"], {"already_paid": 1, "not_paid": 0, "toConfirm": 1})
        self.assertIn("1 a confirmar", out["closeView"])
        self.assertIn("Ya estaba pagado · a confirmar", out["saveZero"])
        self.assertIn("Cubre lo que debe", out["notice"])
        self.assertTrue(out["polled"], "the poll brings «a confirmar»")
        # «Reabrir la ruta»
        self.assertTrue(out["reopenButton"])
        self.assertFalse(out["reopenLockedButton"], "hidden once the cash was counted")
        self.assertEqual(out["reopen"]["posted"], {"route_id": 60, "user_id": 7})
        self.assertFalse(out["reopen"]["closed"], "the route opens again on the phone")
        self.assertEqual(out["reopenRefused"]["toast"],
                         "Ya se cargó el efectivo contado de Ruta 60: solo caja o gerencia "
                         "reabren la ruta. Pediles que la reabran.")
        self.assertTrue(out["reopenRefused"]["locked"])
        self.assertTrue(out["reopenRefused"]["closed"])
        self.assertFalse(out["byOthers"]["closed"], "reopened by administración: the poll")


@tagged("post_install", "-at_install", "distri_v129_25")
class TestV12925Reopen(V12925Mixin, HttpCase):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_v129_25()
        cls.product = cls.env["product.product"].create({
            "name": "Producto v129.25", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.customer = cls.env["res.partner"].create({"name": "KIOSCO V12925 REABRIR",
                                                      "city": "Centenario"})

    def _closed(self):
        return self._closed_route(self._order(self.customer, self.product, qty=5))

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    @staticmethod
    def _said(route, text):
        return any(text in (m.body or "") for m in route.message_ids)

    def test_who_reopens(self):
        route = self._closed()
        before = self._ledger()
        for user in (self.seller, self.driver):
            with self.assertRaisesRegex(UserError, "Solo administración, caja o gerencia"):
                route._distri_reopen(user, via="backend")
        for user in (self.admin_user, self.cashier, self.manager):
            if user == self.cashier:
                # no backend access to routes: /caja (sudo) calls the same rule
                route._distri_reopen(user, via="caja")
            else:
                route.with_user(user).action_reopen()
            self.assertEqual(route.state, "on_route")
            self.assertEqual(route.reopened_by_id, user)
            self.assertTrue(route.reopened_at)
            self.assertTrue(self._said(route, "Ruta reabierta para el repartidor por %s" % user.name))
            res = self._sync([self._event(route, "close", {},
                                          captured="2026-09-26T18:00:00.000Z")])
            self.assertEqual(res[0]["status"], "ok", res)
            self.assertEqual(route.state, "closed")
        self.assertEqual(route.reopen_count, 3)
        # 25/11/2026: the driver no longer reopens it from the app
        with self.assertRaisesRegex(UserError, "Esa ruta no es tuya"):
            route._distri_reopen(self.other_driver, via="app", by_driver=True)
        with self.assertRaisesRegex(UserError, "administración o caja"):
            route._distri_reopen(self.driver, via="app", by_driver=True)
        self.assertEqual(route.state, "closed")
        self.assertTrue(route._app_payload()["reopen_locked"])
        self.assertEqual(self._ledger(), before, "reopening touches no balance")

    def test_cash_counted_locks_it_to_caja_and_gerencia(self):
        route = self._closed()
        route.sudo().cash_counted = 500
        self.assertTrue(route.cash_count_done)
        self.assertTrue(route._app_payload()["reopen_locked"])
        with self.assertRaisesRegex(UserError, "solo caja o gerencia"):
            route.with_user(self.admin_user).action_reopen()
        with self.assertRaisesRegex(UserError, "administración o caja"):
            route._distri_reopen(self.driver, via="app", by_driver=True)
        route._distri_reopen(self.cashier, via="caja")
        self.assertEqual(route.state, "on_route")
        self._sync([self._event(route, "close", {}, captured="2026-09-26T18:00:00.000Z")])
        route.with_user(self.manager).action_reopen()
        self.assertEqual(route.state, "on_route")

    def test_never_settled_nor_open(self):
        route = self._closed()
        route.sudo().state = "settled"
        with self.assertRaisesRegex(UserError, "ya se rindió"):
            route.with_user(self.manager).action_reopen()
        route.sudo().state = "on_route"
        with self.assertRaisesRegex(UserError, "Solo se reabre una ruta cerrada"):
            route.with_user(self.manager).action_reopen()

    def test_backend_button_is_restricted(self):
        arch = self.env.ref("distri_pedido.view_route_form").arch
        button = re.search(r'<button name="action_reopen"[^>]*>', arch, re.S).group(0)
        for xmlid in ("grupo_administracion", "grupo_cajero", "grupo_gerencia"):
            self.assertIn("distri_pedido.%s" % xmlid, button)

    # ------------------------------------------------------------ HTTP

    def _csrf(self, url):
        page = self.url_open(url).text
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def test_driver_app_endpoint(self):
        route = self._closed()
        self.authenticate(self.driver.login, self.driver.login)
        # 25/11/2026: the driver's own route is refused too, and stays closed
        res = self.url_open("/reparto/api/reopen", data=json.dumps({"route_id": route.id}),
                            headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 409)
        self.assertFalse(res.json()["ok"])
        self.assertTrue(res.json()["locked"], "an old cached page hides its button")
        self.assertIn("administración o caja", res.json()["error"])
        self.assertEqual(route.state, "closed")
        data = self.url_open("/reparto/api/route?id=%s" % route.id).json()
        self.assertTrue(data["route"]["reopen_locked"])
        # another driver's route
        self.authenticate(self.other_driver.login, self.other_driver.login)
        res = self.url_open("/reparto/api/reopen", data=json.dumps({"route_id": route.id}),
                            headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 404)

    def test_caja_reception_button(self):
        route = self._closed()
        arch = self.env.ref("distri_pedido.caja_rendicion").arch
        self.assertIn("/caja/rendicion/%s/reabrir", arch)
        self.assertIn("route.state == 'closed'", arch)
        self.authenticate(self.cashier.login, self.cashier.login)
        # the session's token (from «Abrir la caja»): the reopen needs no open caja
        token = self._csrf("/caja")
        res = self.url_open("/caja/rendicion/%s/reabrir" % route.id,
                            data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text)
        self.assertIn("reabierta", res.headers["Location"])
        self.assertEqual(route.state, "on_route")
        self.assertEqual(route.reopened_by_id, self.cashier)
        self.assertTrue(self._said(route, "desde la caja"))

    def test_rendiciones_button(self):
        route = self._closed()
        self.authenticate(self.admin_user.login, self.admin_user.login)
        page = self.url_open("/rendiciones/rutas").text
        self.assertIn("/rendiciones/rutas/%s/reabrir" % route.id, page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        res = self.url_open("/rendiciones/rutas/%s/reabrir" % route.id,
                            data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text)
        self.assertEqual(route.state, "on_route")
        self.assertEqual(route.reopened_by_id, self.admin_user)
        # locked for administración once counted: the page says why
        self._sync([self._event(route, "close", {}, captured="2026-09-26T18:00:00.000Z")])
        route.sudo().cash_counted = 500
        res = self.url_open("/rendiciones/rutas/%s/reabrir" % route.id,
                            data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("solo caja o gerencia", res.text)
        self.assertEqual(route.state, "closed")
