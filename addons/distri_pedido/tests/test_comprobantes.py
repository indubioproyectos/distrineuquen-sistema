"""«Comprobantes pendientes»: the driver answers for the receipts of the
transfers he collects (design approved by management on 22/10/2026,
models/comprobante_pendiente.py). One block per approved rule.

Synthetic data only: no real customer, phone or receipt.
"""
import json
import os
import re
import shutil
import subprocess
from datetime import datetime, timedelta
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.comprobante_pendiente import (
    PARAM_RED, claim_text, document_of, red_rule_on, relative_when,
)
from odoo.addons.distri_pedido.models.reparto import STOP_STATES, TRANSFER
from odoo.addons.distri_pedido.tests.test_cobros import _fixture
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

DRIVER_PHONE = "5492995000222"
CUSTOMER_PHONE = "5492995000333"
SELLER_PHONE = "5492995000444"
# the phone's moment of the visits (_event default): 26/09/2026 12:00 in Argentina
VISIT_AT = datetime(2026, 9, 26, 15, 0)


@tagged("post_install", "-at_install", "distri_comprobantes")
class TestComprobantes(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            # 19.0.1.129.11: confirming a report income needs grupo_ingresos_reporte
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.driver.sudo().distri_whatsapp_vendedor = "299 5000222"
        cls.seller = new_test_user(
            cls.env, login="vendedor_comprobantes", name="Vendedor Prueba",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)],
        )
        cartera = cls.env["res.partner.category"].create({"name": "Cartera Prueba Comprobantes"})
        cls.seller.sudo().write({"distri_whatsapp_vendedor": "299 5000444",
                                 "etiqueta_cartera_id": cartera.id})
        cls.customer = cls.env["res.partner"].create({
            "name": "Almacén Prueba Comprobantes", "phone": "299 5000333",
            "company_type": "company", "category_id": [Command.set(cartera.ids)]})
        cls.Promise = cls.env["distri.promised.transfer"].sudo()
        cls.product_a.invoice_policy = "order"
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_RED, "0")

    # ------------------------------------------------------------ helpers

    def _deliver(self, route, stop, captured="2026-09-26T15:00:00.000Z", **kw):
        payload = dict({"transfer": "yes", "transfer_amount": 1000,
                        "receipt_answer": "not_yet"}, **kw)
        res = self._sync([self._event(route, "visit", self._visit(stop, **payload),
                                      captured=captured)], user=route.driver_id)
        self.assertEqual(res[0]["status"], "ok", res)
        return self.Promise.search([("stop_id", "=", stop.id)])

    def _message(self, wa_id, when, kind="image", body="", mime="image/jpeg"):
        vals = {
            "wa_message_id": "wamid.comprobantes.%s.%s" % (wa_id, when.isoformat()),
            "direction": "in", "wa_id": wa_id, "message_type": kind, "body": body,
            "timestamp": when,
        }
        if kind != "text":
            vals.update({"media_mime_type": mime, "media_id": "123", "media_state": "pending"})
        return self.env["distri.whatsapp.message"].sudo().create(vals)

    def _settle(self, route):
        route.cash_counted = route.cash_expected
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()

    def _invoice(self, partner, amount):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id,
            "invoice_date": fields.Date.today() - timedelta(days=3),
            "invoice_line_ids": [Command.create({
                "name": "mercadería", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    # ---------------------------------------- rule 1: claim on the spot

    def test_transfer_leaves_a_pending_receipt_at_the_driver_name(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        promise = self._deliver(route, stop)
        self.assertEqual(stop.state, "paid")
        self.assertEqual(stop.transfer_amount, 1000)
        self.assertEqual(promise.state, "open")
        self.assertEqual(promise.driver_id, self.driver)
        self.assertEqual(promise.amount, 1000)
        self.assertEqual(promise.receipt_answer, "not_yet")
        self.assertFalse(self.env["account.payment"].search([
            ("partner_id", "=", self.customer.id)]), "a transfer never creates a payment")

    def test_transfer_needs_amount_and_receipt_answer(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        for payload in ({"transfer": "yes", "transfer_amount": 1000},
                        {"transfer": "yes", "receipt_answer": "forwarded"}):
            res = self._sync([self._event(route, "visit", self._visit(stop, **payload))])
            self.assertEqual(res[0]["status"], "error", payload)
            self.assertFalse(res[0]["retry"])

    def test_old_app_answer_counts_as_transfer_of_what_was_left(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        promise = self._deliver(route, stop, transfer="done", transfer_amount=0,
                                receipt_answer=None, cash=300)
        self.assertEqual(stop.transfer, "yes")
        self.assertEqual(promise.amount, 700)

    def test_route_payload_for_forwarding_and_claiming(self):
        # 19.0.1.129.26 (#109): the company number is a parameter loaded in
        # production (scripts/cargar-datos-empresa.sh); none in a fresh database
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.whatsapp_empresa", "5492990000143")
        self.customer.distri_whatsapp = "299 5000333"
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        self._deliver(route, route.stop_ids)
        payload = route._app_payload()
        self.assertTrue(payload["company_whatsapp"])
        self.assertFalse(payload["red_rule"])
        [item] = payload["receipts"]
        self.assertEqual(item["order"], order.name)
        self.assertEqual(item["state"], "open")
        self.assertTrue(item["claim"].startswith(
            "Hola, te entregué el pedido %s " % order.name), item["claim"])
        self.assertIn("comprobante de la transferencia de $ 1.000", item["claim"])
        self.assertTrue(item["whatsapp"].endswith("2995000333"))

    def test_seller_or_admin_mark_it_arrived_the_driver_does_not(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        with self.assertRaises(UserError):
            promise.with_user(self.driver).action_mark_arrived()
        promise.with_user(self.seller).action_mark_arrived()
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.resolved_via, "manual")
        self.assertEqual(promise.resolved_by_id, self.seller)
        self.assertEqual(promise.resolved_by_label, "Vendedor Prueba (vendedor)")
        with self.assertRaises(UserError):
            promise.with_user(self.seller).action_mark_arrived()

    def test_claim_by_whatsapp_from_the_backend(self):
        self.customer.distri_whatsapp = "299 5000333"
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        action = promise.with_user(self.seller).action_remind_whatsapp()
        self.assertIn("wa.me/5492995000333", action["url"])
        self.assertIn(order.name, action["url"])
        self.assertEqual(promise.reminded_by_id, self.seller)

    # ------------------------------------ rule 2: full amount, next day

    def test_short_transfer_is_charged_to_the_driver(self):
        order = self._order(self.customer, self.product_a)  # 1.000
        route = self._route(order)
        stop = route.stop_ids
        self._deliver(route, stop, transfer_amount=600)
        case = route.case_ids.filtered(lambda c: c.kind == "short")
        self.assertEqual(case.amount, -400)
        self.assertEqual(case.driver_id, self.driver)
        self.assertIn("a cargo del repartidor", case.candidates)
        # the driver fixes the visit: he did collect the total
        self._deliver(route, stop, captured="2026-09-26T15:10:00.000Z", transfer_amount=1000)
        self.assertFalse(route.case_ids.filtered(lambda c: c.kind == "short"))

    def test_receipt_not_rendered_expires_at_next_settlement_and_is_charged(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)  # delivered on 26/09
        self._sync([self._event(route, "close", {})])
        self._settle(route)
        self.assertEqual(promise.state, "overdue")
        self.assertEqual(promise.overdue_since, fields.Date.context_today(promise))
        case = promise.charge_case_id
        self.assertEqual(case.kind, "receipt")
        self.assertEqual(case.amount, -1000)
        self.assertEqual(case.driver_id, self.driver)
        self.assertEqual(case.state, "open")
        self.assertIn("Vencida desde", promise.status_label)

    def test_receipts_of_today_stay_pending_at_a_same_day_settlement(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        now = fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        promise = self._deliver(route, route.stop_ids, captured=now)
        self._sync([self._event(route, "close", {}, captured=now)])
        self._settle(route)
        self.assertEqual(promise.state, "open")
        self.assertFalse(promise.charge_case_id)

    def test_cron_expires_receipts_without_a_settlement(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "overdue")
        self.assertTrue(promise.charge_case_id)

    def test_payment_verified_in_mercado_pago_closes_the_charge(self):
        # the fixture's transfer 900000000004: $ 51.234 on 24/09 15:00
        order = self._order(self.customer, self.product_a, qty=1, price=51234)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z",
                                transfer_amount=51234)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "overdue")
        case = promise.charge_case_id
        self.env["distri.mp.report"]._import_file("mp_report_25col.csv",
                                                  _fixture("mp_report_25col.csv"))
        card = self.env["distri.mp.collection"].search([("source_id", "=", "900000000004")])
        self.assertEqual(card.promise_id, promise)
        card.action_confirm()
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.resolved_via, "mp")
        self.assertEqual(promise.resolved_by_label, "Reporte Mercado Pago")
        self.assertEqual(case.state, "closed")
        self.assertIn("Pago verificado en Mercado Pago", case.resolution)
        # undone: the charge opens again
        card.action_undo()
        self.assertEqual(promise.state, "overdue")
        self.assertEqual(case.state, "open")

    def test_late_receipt_resolves_but_the_charge_waits_for_administration(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        promise.with_user(self.seller).action_mark_arrived()
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.charge_case_id.state, "open")
        self.assertIn("después del vencimiento",
                      promise.charge_case_id.message_ids[:1].body or "")

    # ------------------------- «No pagó nada» at the driver's name (19.0.1.26.0)

    def _no_pay(self, route, stop, captured="2026-09-26T15:00:00.000Z"):
        res = self._sync([self._event(route, "visit", self._visit(stop, no_payment=True),
                                      captured=captured)], user=route.driver_id)
        self.assertEqual(res[0]["status"], "ok", res)
        return self.Promise.search([("stop_id", "=", stop.id)])

    def _invoiced_order(self, price=100.0):
        order = self._order(self.customer, self.product_a, price=price)
        invoice = order._create_invoices()
        invoice.action_post()
        return order, invoice

    def test_unpaid_order_stays_at_the_driver_name(self):
        self.customer.distri_whatsapp = "299 5000333"
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        item = self._no_pay(route, stop)
        self.assertEqual(stop.state, "not_paid")
        self.assertEqual(item.kind, "unpaid")
        self.assertEqual(item.amount, 1000)
        self.assertEqual(item.state, "open")
        self.assertEqual(item.driver_id, self.driver)
        [entry] = route._app_payload()["receipts"]
        self.assertEqual(entry["kind"], "unpaid")
        self.assertIn("todavía no está pago: faltan $ 1.000", entry["claim"])
        self.assertIn("todavía no está pago: faltan $ 1.000", item._reminder_text())
        # the driver fixes the visit: it was paid in cash after all
        self._sync([self._event(route, "visit", self._visit(stop, cash=1000),
                                captured="2026-09-26T15:10:00.000Z")])
        self.assertFalse(self.Promise.search([("stop_id", "=", stop.id)]))

    def test_unpaid_order_is_charged_at_the_next_settlement(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self._sync([self._event(route, "close", {})])
        self._settle(route)
        self.assertEqual(item.state, "overdue")
        case = item.charge_case_id
        self.assertEqual(case.kind, "unpaid")
        self.assertEqual(case.amount, -1000)
        self.assertEqual(case.driver_id, self.driver)
        # it shows up in «Cargos al repartidor»
        action = self.env.ref("distri_pedido.action_driver_charges")
        self.assertIn("unpaid", action.domain)

    def test_unpaid_order_today_and_cron_from_day_two(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        now = fields.Datetime.now().strftime("%Y-%m-%dT%H:%M:%S.000Z")
        item = self._no_pay(route, route.stop_ids, captured=now)
        self.Promise._distri_cron_expire()
        self.assertEqual(item.state, "open", "the same day it is only pending")
        other = self._order(self.customer, self.product_a)
        old_route = self._route(other)
        old = self._no_pay(old_route, old_route.stop_ids)  # 26/09
        self.Promise._distri_cron_expire()
        self.assertEqual(old.state, "overdue")
        self.assertEqual(old.charge_case_id.kind, "unpaid")

    def test_unpaid_charge_closes_when_administration_registers_the_payment(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.assertEqual(item.amount, 1000)
        self.Promise._distri_cron_expire()
        case = item.charge_case_id
        self.assertEqual(case.state, "open")
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_cash"].id,
            }).action_create_payments()
        self.assertEqual(invoice.payment_state in ("paid", "in_payment"), True)
        self.assertEqual(item.state, "fulfilled")
        self.assertEqual(item.resolved_via, "payment")
        self.assertEqual(case.state, "closed")
        self.assertIn("no se carga al repartidor", case.resolution)

    def test_partial_payment_keeps_the_unpaid_charge(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_cash"].id, "amount": 400,
                "payment_difference_handling": "open",
            }).action_create_payments()
        self.assertEqual(item.state, "overdue")
        self.assertEqual(item.charge_case_id.state, "open")

    def test_unpaid_charge_closes_when_the_receipt_arrives(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        self._message(CUSTOMER_PHONE, fields.Datetime.now(),
                      body="$ 1.000 Operación 900000000790 pedido %s" % order.name)
        self.assertEqual(item.state, "fulfilled")
        self.assertEqual(item.resolved_via, "whatsapp")
        self.assertEqual(item.charge_case_id.state, "closed")
        self.assertIn("Llegó el comprobante", item.charge_case_id.resolution)

    def test_unpaid_charge_closes_with_mercado_pago(self):
        order = self._order(self.customer, self.product_a, qty=1, price=51234)
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z")
        self.Promise._distri_cron_expire()
        case = item.charge_case_id
        self.env["distri.mp.report"]._import_file("mp_report_25col.csv",
                                                  _fixture("mp_report_25col.csv"))
        card = self.env["distri.mp.collection"].search([("source_id", "=", "900000000004")])
        self.assertEqual(card.promise_id, item)
        card.action_confirm()
        self.assertEqual(item.state, "fulfilled")
        self.assertEqual(item.resolved_via, "mp")
        self.assertEqual(case.state, "closed")
        self.assertIn("Mercado Pago", case.resolution)

    def test_unpaid_marked_by_hand_leaves_the_charge_to_administration(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        item.with_user(self.seller).action_mark_arrived()
        self.assertEqual(item.state, "fulfilled")
        self.assertEqual(item.charge_case_id.state, "open")
        item.charge_case_id.resolution = "Pagó en mano al vendedor."
        item.charge_case_id.action_close()
        self.assertEqual(item.charge_case_id.state, "closed")

    # ------------------- partial payments follow the invoice (19.0.1.27.0)

    def _pay(self, invoice, amount):
        return self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_cash"].id, "amount": amount,
                "payment_difference_handling": "open",
            })._create_payments()

    def _overdue_unpaid(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        self.assertEqual(item.state, "overdue")
        return item, invoice

    def test_partial_payment_lowers_item_and_charge(self):
        item, invoice = self._overdue_unpaid()
        case = item.charge_case_id
        payment = self._pay(invoice, 400)
        self.assertEqual(item.amount, 600)
        self.assertEqual(item.original_amount, 1000)
        self.assertEqual(item.state, "overdue")
        self.assertEqual(case.amount, -600)
        self.assertEqual(case.state, "open")
        [change] = item.amount_history_ids
        self.assertEqual((change.old_amount, change.new_amount), (1000, 600))
        self.assertEqual(change.move_id, payment.move_id)
        self.assertEqual(case.amount_history_ids, item.amount_history_ids,
                         "administración sees why the charge went down")

    def test_second_payment_completes_the_order(self):
        item, invoice = self._overdue_unpaid()
        case = item.charge_case_id
        self._pay(invoice, 400)
        self._pay(invoice, 600)
        self.assertEqual(item.amount, 0)
        self.assertEqual(item.state, "fulfilled")
        self.assertEqual(item.resolved_via, "payment")
        self.assertEqual(case.state, "closed")
        self.assertEqual(case.amount, 0)
        self.assertEqual(item.amount_history_ids.sorted("id").mapped("new_amount"), [600, 0])

    def test_undoing_a_payment_raises_the_amount_again(self):
        item, invoice = self._overdue_unpaid()
        case = item.charge_case_id
        first = self._pay(invoice, 400)
        second = self._pay(invoice, 600)
        self.assertEqual(case.state, "closed")
        # the payment is cancelled (Odoo 19: draft keeps the matching, cancel undoes it)
        second.action_draft()
        second.action_cancel()
        self.assertEqual(item.amount, 600)
        self.assertEqual(item.state, "overdue", "it had expired: back to «Vencida»")
        self.assertEqual(case.state, "open")
        self.assertEqual(case.amount, -600)
        # «Quitar» on the invoice: the other payment taken off it
        partial = invoice.line_ids.matched_credit_ids.filtered(
            lambda p: p.credit_move_id.move_id == first.move_id)
        invoice.js_remove_outstanding_partial(partial.id)
        self.assertEqual(item.amount, 1000)
        self.assertEqual(case.amount, -1000)
        self.assertEqual(item.amount_history_ids.sorted("id").mapped("new_amount"),
                         [600, 0, 600, 1000])

    def test_amount_never_goes_above_what_was_left_unpaid(self):
        order, invoice = self._invoiced_order()
        earlier = self._pay(invoice, 300)  # paid before the visit
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.assertEqual(item.amount, 700)
        self.assertEqual(item.original_amount, 700)
        earlier.action_draft()
        earlier.action_cancel()  # the residual is 1.000 again
        self.assertEqual(item.amount, 700, "never above the original amount")
        self.assertFalse(item.amount_history_ids)

    def test_charge_decided_by_administration_is_left_alone(self):
        item, invoice = self._overdue_unpaid()
        case = item.charge_case_id
        case.resolution = "Se descuenta del sueldo."
        case.action_close()
        self._pay(invoice, 400)
        self.assertEqual(item.amount, 1000)
        self.assertEqual(case.amount, -1000)
        self.assertEqual(case.state, "closed")
        self.assertEqual(case.resolution, "Se descuenta del sueldo.")
        # also when the driver already replenished it
        item2, invoice2 = self._overdue_unpaid()
        item2.charge_case_id.replenished = True
        self._pay(invoice2, 1000)
        self.assertEqual(item2.charge_case_id.amount, -1000)
        self.assertEqual(item2.charge_case_id.state, "open")

    def test_order_without_invoice_keeps_its_amount(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        item._distri_check_paid_items()
        self.assertEqual(item.amount, 1000)
        self.assertFalse(item.amount_history_ids)

    # ------------------- what the driver claims (19.0.1.27.0)

    def test_relative_delivery_wording_in_argentina(self):
        # now: Thursday 22/10/2026 15:00 in Argentina (18:00 UTC)
        now = datetime(2026, 10, 22, 18, 0)
        cases = [
            (datetime(2026, 10, 22, 16, 30), "hace un rato"),      # 13:30, 1 h 30 ago
            (datetime(2026, 10, 22, 15, 1), "hace un rato"),       # 2 h 59 ago
            (datetime(2026, 10, 22, 14, 59), "hoy a la mañana"),   # 11:59, 3 h 01 ago
            (datetime(2026, 10, 22, 3, 0), "hoy a la mañana"),     # 00:00 in Argentina
            (datetime(2026, 10, 22, 2, 59), "ayer"),               # 23:59 of the 21st there
            (datetime(2026, 10, 21, 3, 0), "ayer"),                # 00:00 of the 21st
            (datetime(2026, 10, 21, 2, 59), "el martes 20/10"),    # 23:59 of the 20th
            (datetime(2026, 10, 16, 15, 0), "el viernes 16/10"),   # 6 days
            (datetime(2026, 10, 15, 15, 0), "el 15/10"),           # 7 days
        ]
        for moment, wanted in cases:
            self.assertEqual(relative_when(moment, now), wanted, moment)
        evening = datetime(2026, 10, 22, 23, 0)  # 20:00 in Argentina
        self.assertEqual(relative_when(datetime(2026, 10, 22, 16, 0), evening), "hoy a la tarde")
        self.assertEqual(relative_when(datetime(2026, 10, 22, 15, 59), evening),
                         "hoy a la mañana")
        # just after midnight in Argentina (03:10 UTC): 23:50 of the day before is «ayer»
        self.assertEqual(relative_when(datetime(2026, 10, 22, 2, 50),
                                       datetime(2026, 10, 22, 3, 10)), "ayer")

    def test_item_shows_amount_invoice_date_and_type(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        item = self._no_pay(route, route.stop_ids)
        now = item.captured_at + timedelta(days=1)
        self.assertEqual(item._distri_item_line(now),
                         "Faltan $ 1.000 · Factura %s · entregado ayer" % invoice.name)
        [entry] = route._app_payload()["receipts"]
        self.assertEqual(entry["document"], "Factura %s" % invoice.name)
        self.assertEqual(entry["kind"], "unpaid")
        self.assertTrue(entry["delivered_at"])
        self.assertEqual(route._app_payload()["stops"][0]["document"], "Factura %s" % invoice.name)
        # without an invoice: the order
        other = self._order(self.customer, self.product_a)
        self.assertEqual(document_of(other), ("Pedido %s" % other.name,
                                              "el pedido %s" % other.name))

    def test_claim_of_one_item(self):
        order, invoice = self._invoiced_order()
        route = self._route(order)
        item = self._deliver(route, route.stop_ids)
        now = item.captured_at + timedelta(days=1)
        self.assertEqual(
            self.Promise._distri_customer_claim(item, now),
            "Hola, te entregué el pedido de la factura %s ayer y todavía no tengo el comprobante "
            "de la transferencia de $ 1.000. ¿Me lo podés mandar por acá?" % invoice.name)
        other, other_invoice = self._invoiced_order()
        route2 = self._route(other)
        unpaid = self._no_pay(route2, route2.stop_ids)
        self.assertIn("todavía no está pago: faltan $ 1.000",
                      self.Promise._distri_customer_claim(unpaid, now))
        self.assertIn("Si ya transferiste, mandame el comprobante",
                      self.Promise._distri_customer_claim(unpaid, now))

    def test_claim_lists_every_item_oldest_first_with_total(self):
        first, first_invoice = self._invoiced_order()
        second, second_invoice = self._invoiced_order(price=40.0)  # 400
        route = self._route(first | second)
        newer = self._no_pay(route, route.stop_ids.filtered(lambda s: s.order_id == second),
                             captured="2026-10-21T15:00:00.000Z")
        older = self._deliver(route, route.stop_ids.filtered(lambda s: s.order_id == first),
                              captured="2026-10-14T15:00:00.000Z")
        text = self.Promise._distri_customer_claim(newer | older, datetime(2026, 10, 22, 18, 0))
        self.assertEqual(text, (
            "Hola, todavía me faltan estos pagos y comprobantes:\n"
            "- Factura %s, entregado el 14/10: falta el comprobante de la transferencia de "
            "$ 1.000\n"
            "- Factura %s, entregado ayer: falta el pago de $ 400\n"
            "Total: $ 1.400. ¿Me mandás los comprobantes por acá? Si alguno no está pago, ¿me lo "
            "podés pagar? Si ya transferiste, mandame el comprobante.") % (
                first_invoice.name, second_invoice.name))
        # the driver's list carries that message on each item of the customer
        claims = {e["claim"] for e in route._app_payload()["receipts"]}
        self.assertEqual(len(claims), 1)
        self.assertIn("Total: $ 1.400", claims.pop())

    # ------------------- one claim message for app and Odoo (19.0.1.28.0)

    def _node_claims(self, cases):
        """Runs static/src/reparto/claim.js (the app's code) with Node."""
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("Node is not installed")
        root = get_module_path("distri_pedido")
        script = (
            "const make = require(process.argv[1]);"
            "const T = require(process.argv[2]);"
            "const cases = JSON.parse(require('fs').readFileSync(0, 'utf8'));"
            "const c = make(T);"
            "process.stdout.write(JSON.stringify(cases.map(x => "
            "c.customerClaim(x.items, x.voice, Date.parse(x.now), x.company))));")
        out = subprocess.run(
            [node, "-e", script, os.path.join(root, "static/src/reparto/claim.js"),
             os.path.join(root, "static/src/reparto/claim_texts.json")],
            input=json.dumps(cases), capture_output=True, text=True, timeout=30, check=True)
        return json.loads(out.stdout)

    def test_backend_and_app_build_the_same_message(self):
        now = datetime(2026, 10, 22, 18, 0)  # Thursday 15:00 in Argentina
        moments = [datetime(2026, 10, 22, 16, 30), datetime(2026, 10, 22, 14, 59),
                   datetime(2026, 10, 22, 3, 0), datetime(2026, 10, 22, 2, 59),
                   datetime(2026, 10, 21, 2, 59), datetime(2026, 10, 16, 15, 0),
                   datetime(2026, 10, 15, 15, 0), datetime(2026, 10, 22, 17, 0)]
        amounts = [40000, 1234.5, 99.99, 1000000]
        docs = [("Factura 0001-00006281", "el pedido de la factura 0001-00006281"),
                ("Remito R-00046", "el pedido del remito R-00046"),
                ("Pedido S00231", "el pedido S00231")]
        cases, expected = [], []
        for voice in ("driver", "company"):
            company = "Empresa de ejemplo" if voice == "company" else ""
            for n, moment in enumerate(moments):
                kind = "unpaid" if n % 2 else "done"
                doc, phrase = docs[n % 3]
                single = [{"id": n, "kind": kind, "amount": amounts[n % 4], "document": doc,
                           "phrase": phrase, "delivered_at": moment}]
                several = single + [{"id": 100 + n, "kind": "done", "amount": amounts[(n + 1) % 4],
                                     "document": docs[(n + 1) % 3][0],
                                     "phrase": docs[(n + 1) % 3][1],
                                     "delivered_at": moments[(n + 3) % len(moments)]}]
                for items in (single, several):
                    expected.append(claim_text(items, voice, now, company))
                    cases.append({"voice": voice, "company": company,
                                  "now": now.isoformat() + "Z",
                                  "items": [dict(i, delivered_at=fields.Datetime.to_string(
                                      i["delivered_at"]), state="open") for i in items]})
        self.assertEqual(self._node_claims(cases), expected)

    def test_backend_claim_speaks_for_the_company(self):
        self.customer.distri_whatsapp = "299 5000333"
        order, invoice = self._invoiced_order()
        route = self._route(order)
        item = self._deliver(route, route.stop_ids)
        now = item.captured_at + timedelta(days=1)
        driver = self.Promise._distri_customer_claim(item, now)
        company = self.Promise._distri_customer_claim(item, now, voice="company")
        self.assertTrue(driver.startswith("Hola, te entregué el pedido de la factura %s ayer"
                                          % invoice.name))
        self.assertTrue(company.startswith("Hola, te entregamos el pedido de la factura %s ayer"
                                           % invoice.name))
        self.assertIn("todavía no tenemos el comprobante", company)
        self.assertNotIn("te entregué", company)
        self.assertTrue(company.endswith("\n" + self.env.company.name))

    def test_backend_claim_lists_every_open_item_of_the_customer(self):
        self.customer.distri_whatsapp = "299 5000333"
        first, first_invoice = self._invoiced_order()
        second, second_invoice = self._invoiced_order(price=40.0)
        route = self._route(first | second)
        older = self._deliver(route, route.stop_ids.filtered(lambda s: s.order_id == first),
                              captured="2026-10-14T15:00:00.000Z")
        self._no_pay(route, route.stop_ids.filtered(lambda s: s.order_id == second),
                     captured="2026-10-21T15:00:00.000Z")
        text = older.with_user(self.seller)._reminder_text()
        self.assertTrue(text.startswith("Hola, todavía nos faltan estos pagos y comprobantes:\n"
                                        "- Factura %s" % first_invoice.name), text)
        self.assertIn("- Factura %s" % second_invoice.name, text)
        self.assertIn("Total: $ 1.400", text)
        action = older.with_user(self.seller).action_remind_whatsapp()
        self.assertIn("Total%3A%20%24%201.400", action["url"])

    def test_seller_claims_only_customers_of_his_portfolio(self):
        stranger = self.env["res.partner"].create({
            "name": "Cliente Fuera De Cartera", "company_type": "company",
            "distri_whatsapp": "299 5000555"})
        order = self._order(stranger, self.product_a)
        route = self._route(order)
        item = self._deliver(route, route.stop_ids)
        with self.assertRaises(AccessError):
            item.with_user(self.seller).action_remind_whatsapp()
        # administración claims any customer
        self.assertIn("wa.me/", item.action_remind_whatsapp()["url"])

    def test_depot_pickup_without_payment_is_not_at_anyone_name(self):
        order = self._order(self.customer, self.product_a)
        route = self.env["distri.route"]._depot_route_for()
        stop = route._add_orders(order)
        payload = self._visit(stop, no_payment=True)
        self.env["distri.sync.event"].sudo()._record_visit(
            stop.sudo(), payload["visit_uuid"], payload, fields.Datetime.now())
        self.assertEqual(stop.state, "not_paid")
        self.assertFalse(self.Promise.search([("stop_id", "=", stop.id)]))
        self.Promise._distri_cron_expire()
        self.assertFalse(route.case_ids)

    # ------------------------------------------- rule 3 and 8: in red

    def test_customer_in_red_is_off_by_default(self):
        self.env["ir.config_parameter"].sudo().search([("key", "=", PARAM_RED)]).unlink()
        self.assertFalse(red_rule_on(self.env))
        self._invoice(self.customer, 500)
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self.assertEqual(stop.previous_debt, 500)
        self.assertFalse(stop.red)
        payload = route._app_payload()
        self.assertFalse(payload["red_rule"])
        self.assertIsNone(payload["stops"][0]["red"])
        self.assertFalse(order._distri_red_notice())
        # the module ships it off
        root = get_module_path("distri_pedido")
        with open(os.path.join(root, "data/parametros.xml"), encoding="utf-8") as handle:
            data = handle.read()
        self.assertRegex(data, r'<field name="key">distri_pedido.cliente_en_rojo</field>\s*'
                               r'<field name="value">0</field>')

    def test_customer_in_red_with_old_debt(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_RED, "1")
        self._invoice(self.customer, 500)
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self.assertTrue(stop.red)
        self.assertEqual(stop.red_debt, 500)
        payload = route._app_payload()
        self.assertTrue(payload["red_rule"])
        self.assertEqual(payload["stops"][0]["red"]["debt"], 500)
        notice = order._distri_red_notice()
        self.assertEqual(notice["debe"], "$ 500")
        # today's order paid, the old debt not: serious fault
        self._sync([self._event(route, "visit", self._visit(stop, cash=1000))])
        self.assertTrue(stop.red_breach)
        self.assertIn("Falta grave", route.message_ids[:1].body)
        # everything collected: no fault
        self._sync([self._event(route, "visit", self._visit(stop, cash=1500),
                                captured="2026-09-26T15:10:00.000Z")])
        self.assertFalse(stop.red_breach)

    def test_customer_in_red_with_a_missing_receipt_of_another_day(self):
        first = self._order(self.customer, self.product_a)
        old_route = self._route(first)
        old = self._deliver(old_route, old_route.stop_ids)  # 26/09, never arrived
        self.env["ir.config_parameter"].sudo().set_param(PARAM_RED, "1")
        second = self._order(self.customer, self.product_a)
        route = self._route(second, driver=self.other_driver)
        stop = route.stop_ids
        self.assertTrue(stop.red)
        self.assertEqual(stop.red_receipts, 1)
        red = stop._distri_red_payload()
        self.assertEqual(red["receipts"][0]["order"], first.name)
        self.assertEqual(second._distri_red_notice()["comprobantes"][0]["pedido"], first.name)
        # the receipt arrives: no longer in red at the next download
        old.with_user(self.seller).action_mark_arrived()
        stop._refresh_amounts()
        self.assertFalse(stop.red)

    # ------------------------------------ rule 5: counted once

    def test_same_receipt_from_customer_and_driver_counts_once(self):
        first = self._order(self.customer, self.product_a)
        second = self._order(self.customer, self.product_a)
        route = self._route(first | second)
        p1 = self._deliver(route, route.stop_ids[0])
        p2 = self._deliver(route, route.stop_ids[1])
        caption = "$ 1.000 Operación 900000000777"
        mine = self._message(CUSTOMER_PHONE, VISIT_AT + timedelta(minutes=30), body=caption)
        self.assertEqual(mine.distri_partner_id, self.customer)
        self.assertEqual(p1.state, "fulfilled")
        self.assertEqual(p1.receipt_id, mine)
        self.assertEqual(p1.resolved_by_label, "Cliente")
        # the driver forwards the same receipt: a copy, nothing else resolved
        copy = self._message(DRIVER_PHONE, VISIT_AT + timedelta(minutes=40), body=caption)
        self.assertEqual(copy.distri_receipt_state, "duplicate")
        self.assertEqual(p2.state, "open")

    def test_copy_detected_late_gives_back_what_it_resolved(self):
        first = self._order(self.customer, self.product_a)
        second = self._order(self.customer, self.product_a)
        route = self._route(first | second)
        p1 = self._deliver(route, route.stop_ids[0])
        p2 = self._deliver(route, route.stop_ids[1])
        original = self._message(CUSTOMER_PHONE, VISIT_AT + timedelta(minutes=30),
                                 body="$ 1.000 Operación 900000000778")
        self.assertEqual(p1.receipt_id, original)
        # forwarded with the second order's number, before its photo is read
        self._message(DRIVER_PHONE, VISIT_AT + timedelta(minutes=39), kind="text",
                      body="Comprobante del pedido %s" % second.name)
        copy = self._message(DRIVER_PHONE, VISIT_AT + timedelta(minutes=40))
        self.assertEqual(p2.receipt_id, copy)
        # reading the photo shows the same operation: a copy
        copy.write({"distri_operation": "900000000778"})
        self.assertEqual(copy.distri_receipt_state, "duplicate")
        self.assertEqual(p2.state, "open")
        self.assertFalse(p2.receipt_id)
        self.assertEqual(p1.state, "fulfilled")

    # ------------------------------ rule 6: photo + text, same number

    def test_driver_text_then_forwarded_photo(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        self._message(DRIVER_PHONE, VISIT_AT + timedelta(minutes=5), kind="text",
                      body="Comprobante del pedido %s" % order.name)
        photo = self._message(DRIVER_PHONE, VISIT_AT + timedelta(minutes=6))
        self.assertEqual(photo.distri_forwarded_by_id, self.driver)
        self.assertEqual(photo.distri_order_ref, order.name)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.resolved_via, "whatsapp")
        self.assertEqual(promise.resolved_by_id, self.driver)
        self.assertEqual(promise.resolved_by_label, "Repartidor Test (repartidor)")

    def test_photo_then_text_from_the_same_number(self):
        first = self._order(self.customer, self.product_a)
        second = self._order(self.customer, self.product_a)
        route = self._route(first | second)
        p1 = self._deliver(route, route.stop_ids[0])
        p2 = self._deliver(route, route.stop_ids[1])
        # the seller forwards a photo and writes the order a minute later
        photo = self._message(SELLER_PHONE, VISIT_AT + timedelta(minutes=10))
        self.assertFalse(photo.distri_partner_id)
        self.assertEqual((p1 | p2).mapped("state"), ["open", "open"])
        self._message(SELLER_PHONE, VISIT_AT + timedelta(minutes=11), kind="text",
                      body="Comprobante del pedido %s" % second.name)
        self.assertEqual(photo.distri_order_ref, second.name)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertEqual(p2.state, "fulfilled")
        self.assertEqual(p2.resolved_by_label, "Vendedor Prueba (vendedor)")
        self.assertEqual(p1.state, "open")

    def test_customer_photo_then_text_with_the_order(self):
        order = self._order(self.customer, self.product_a)
        other = self.env["res.partner"].create({"name": "Otro Cliente Prueba",
                                                "company_type": "company"})
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        # from a number nobody knows: the text names the order afterwards
        photo = self._message("5492995000999", VISIT_AT + timedelta(minutes=10))
        self.assertFalse(photo.distri_partner_id)
        self._message("5492995000999", VISIT_AT + timedelta(minutes=15), kind="text",
                      body="Te mando el comprobante del pedido %s" % order.name)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertNotEqual(photo.distri_partner_id, other)
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.resolved_by_label, "Cliente")

    def test_receipt_for_less_than_the_transfer_does_not_resolve(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids)
        self._message(CUSTOMER_PHONE, VISIT_AT + timedelta(minutes=30),
                      body="$ 400 Operación 900000000779")
        self.assertEqual(promise.state, "open")
        self.assertIn("no cubre", promise.partial_note)

    # ------------------------------ rule 4 and 7: wording removed

    def _module_texts(self):
        root = get_module_path("distri_pedido")
        for folder in ("models", "controllers", "views", "data", "static/src", "security"):
            for base, _dirs, files in os.walk(os.path.join(root, folder)):
                for name in files:
                    if name.endswith((".py", ".xml", ".js", ".csv")):
                        path = os.path.join(base, name)
                        with open(path, encoding="utf-8") as handle:
                            yield path, handle.read()

    def _module_menus(self):
        data = self.env["ir.model.data"].sudo().search([("module", "=", "distri_pedido"),
                                                         ("model", "=", "ir.ui.menu")])
        return self.env["ir.ui.menu"].sudo().browse(data.mapped("res_id")).exists()

    def test_transfer_later_is_gone_from_the_app(self):
        self.assertNotIn("later", dict(TRANSFER))
        self.assertNotIn("promised", dict(STOP_STATES))
        root = get_module_path("distri_pedido")
        with open(os.path.join(root, "static/src/reparto/app.js"), encoding="utf-8") as handle:
            app = handle.read()
        self.assertNotIn("Transfiere después", app)
        self.assertNotIn("Ya transfirió", app)
        names = self._module_menus().mapped("name")
        for name in ("Prometidas vencidas", "Transferencias prometidas", "Vencidas",
                     "Cobranza (48 h)", "Prometidas de hoy"):
            self.assertNotIn(name, names)
        self.assertIn("Comprobantes pendientes", names)
        actions = self.env["ir.actions.act_window"].sudo().search(
            [("res_model", "=", "distri.promised.transfer")])
        self.assertTrue(actions)
        for action in actions:
            self.assertNotIn("romet", action.name)

    def test_later_is_rejected_from_an_old_phone(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        res = self._sync([self._event(route, "visit",
                                      self._visit(route.stop_ids, transfer="later"))])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("ya no existe", res[0]["error"])

    def test_no_text_says_delivery_without_receipt(self):
        """The manager must never read «entrega sin comprobante»."""
        wording = re.compile(r"entreg\w*\s+sin\s+comprobante", re.IGNORECASE)
        for path, text in self._module_texts():
            self.assertFalse(wording.search(text), path)
        for name in self._module_menus().mapped("name"):
            self.assertFalse(wording.search(name), name)
