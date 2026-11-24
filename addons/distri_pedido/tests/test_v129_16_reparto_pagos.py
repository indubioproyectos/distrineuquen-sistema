"""19.0.1.129.16: «Pagado / comprobante cargado» in the driver's visit
(models/reparto_pagos.py, /reparto/api/receipt, static/src/reparto/app4.js).

Every customer, amount and operation here is fictitious."""
import json
import os
import shutil
import subprocess
from datetime import timedelta
from unittest import SkipTest

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import reparto as reparto_controller
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

PNG = (b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00"
       b"\x00\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N"
       b"\x00\x00\x00\x00IEND\xaeB`\x82")


@tagged("post_install", "-at_install", "distri_v129_16")
class TestV12916RepartoPagos(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls.admin = new_test_user(cls.env, login="gonzalo_v129_16_pagos", name="Gonzalo Prueba",
                                  groups="base.group_user,distri_pedido.grupo_administracion",
                                  company_id=cls.env.company.id,
                                  company_ids=[Command.set(cls.env.company.ids)])
        cls.product = cls.env["product.product"].create({
            "name": "Producto v129.16", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.customer = cls.env["res.partner"].create({"name": "KIOSCO V12916 UNO",
                                                      "city": "Centenario"})
        cls.other = cls.env["res.partner"].create({"name": "KIOSCO V12916 DOS",
                                                   "city": "Centenario"})
        cls.wholesale = cls.env["res.partner"].create({
            "name": "MAYORISTA V12916", "city": "Centenario", "distri_es_mayorista": True})

    seq = 0

    def _card(self, partner, amount, state="confirmed", order=None, audit="pendiente",
              by=None, mime="image/png", **receipt):
        """A receipt card: credited (`state` confirmed) or waiting (draft),
        tied to `order` when given (uploaded from the order page)."""
        type(self).seq += 1
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "comprobante-v12916-%d.png" % self.seq, "raw": PNG, "mimetype": mime})
        values = {"wa_message_id": "wamid.v12916.%d" % self.seq, "direction": "in",
                  "message_type": "image", "media_mime_type": mime,
                  "attachment_id": attachment.id, "distri_partner_id": partner.id}
        if order:
            values["distri_customer_order_id"] = order.id
        values.update(receipt)
        message = self.env["distri.whatsapp.message"].sudo().create(values)
        credited = state == "confirmed"
        return self.env["distri.mp.collection"].sudo().create({
            "kind": "verify", "amount": amount, "date": fields.Datetime.now(),
            "partner_id": partner.id, "state": state, "receipt_id": message.id,
            "credited_at": fields.Datetime.now() if credited else False,
            "credited_by_id": (by or self.admin).id if credited else False,
            "audit_state": audit if credited else False})

    def _payments(self, stop):
        return stop.with_user(self.driver)._app_payload()

    # ------------------------------------------------------------ payload

    def test_credited_partial_and_pending_payloads(self):
        order = self._order(self.customer, self.product, qty=20)  # $ 2.000, no invoice
        route = self._route(order)
        stop = route.stop_ids
        self.assertFalse(order._distri_facturas_emitidas(), "an order without an invoice")
        data = self._payments(stop)
        self.assertEqual((data["payments"], data["payments_paid"]), ([], False))

        # waiting: amber, the balance did not move
        pending = self._card(self.customer, 1500, state="draft", order=order)
        data = self._payments(stop)
        [item] = data["payments"]
        self.assertEqual(item["state"], "sin_acreditar")
        self.assertEqual(item["amount"], 1500)
        self.assertEqual(item["receipt_id"], pending.id)
        self.assertEqual(item["channel"], "order")
        self.assertEqual(item["by"], "el cliente")
        self.assertTrue(item["has_file"])
        self.assertFalse(data["payments_paid"])
        self.assertEqual(data["payments_due"], 2000)
        self.assertEqual(stop.order_amount, 2000, "it only informs: the snapshot stays")

        # credited part of it: green, «Saldo a cobrar»
        pending.sudo().write({"state": "ignored"})
        self._card(self.customer, 1200, order=order)
        data = self._payments(stop)
        [item] = data["payments"]
        self.assertEqual(item["state"], "acreditado")
        self.assertEqual(item["by"], "Gonzalo")
        self.assertTrue(item["when_label"])
        self.assertFalse(item["verified"])
        self.assertFalse(data["payments_paid"])
        self.assertEqual(data["payments_due"], 800)

        # credited the rest and verified with Mercado Pago: paid
        self._card(self.customer, 800, order=order, audit="verificado")
        data = self._payments(stop)
        self.assertEqual(len(data["payments"]), 2)
        self.assertTrue(data["payments_paid"])
        self.assertEqual(data["payments_due"], 0)
        self.assertTrue(data["payments"][0]["verified"])
        # a credited payment annulled in the audit is no payment
        self.env["distri.mp.collection"].sudo().browse(
            data["payments"][0]["receipt_id"]).write({"audit_state": "anulado"})
        data = self._payments(stop)
        self.assertEqual(len(data["payments"]), 1)
        self.assertFalse(data["payments_paid"])

    def test_customer_receipts_since_the_stop_exists(self):
        order = self._order(self.customer, self.product, qty=5)
        stop = self._route(order).stop_ids
        before = self._card(self.customer, 300, state="draft")
        self.env.flush_all()
        self.env.cr.execute(
            "UPDATE distri_mp_collection SET create_date = %s WHERE id = %s",
            (stop.create_date - timedelta(days=1), before.id))
        before.invalidate_recordset(["create_date"])
        self.assertFalse(self._payments(stop)["payments"], "loaded before the visit existed")
        after = self._card(self.customer, 400, state="draft")
        self._card(self.other, 999, state="draft")
        data = self._payments(stop)
        self.assertEqual([p["receipt_id"] for p in data["payments"]], [after.id])
        # a receipt tied to the order wins over the customer's loose ones
        tied = self._card(self.customer, 500, state="draft", order=order)
        self.assertEqual([p["receipt_id"] for p in self._payments(stop)["payments"]], [tied.id])

    def test_wholesale_customer_gets_no_data(self):
        order = self._order(self.wholesale, self.product, qty=5)
        stop = self._route(order).stop_ids
        card = self._card(self.wholesale, 500, order=order)
        data = self._payments(stop)
        self.assertEqual(data["payments"], [])
        self.assertFalse(data["payments_paid"])
        self.assertTrue(stop.with_user(self.admin)._app_payment_cards(), "administración sees it")
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/api/receipt?stop=%s&receipt=%s" % (stop.id, card.id))
        self.assertEqual(res.status_code, 404)

    # ------------------------------------------------------------ endpoints

    def test_receipt_only_for_the_drivers_own_stop(self):
        mine = self._order(self.customer, self.product, qty=5)
        theirs = self._order(self.other, self.product, qty=5)
        stop = self._route(mine).stop_ids
        other_stop = self._route(theirs, driver=self.other_driver).stop_ids
        card = self._card(self.customer, 500, order=mine)
        other_card = self._card(self.other, 500, order=theirs)
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/api/receipt?stop=%s&receipt=%s" % (stop.id, card.id))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Content-Type"], "image/png")
        self.assertEqual(res.content, PNG)
        # another route's receipt, even naming his own stop
        for query in ("stop=%s&receipt=%s" % (other_stop.id, other_card.id),
                      "stop=%s&receipt=%s" % (stop.id, other_card.id),
                      "stop=%s&receipt=abc" % stop.id):
            res = self.url_open("/reparto/api/receipt?" + query)
            self.assertEqual(res.status_code, 404, query)
        # another user (administración, not this route's driver)
        self.authenticate(self.admin.login, self.admin.login)
        res = self.url_open("/reparto/api/receipt?stop=%s&receipt=%s" % (stop.id, card.id))
        self.assertIn(res.status_code, (403, 404))
        self.assertNotEqual(res.content, PNG)

    def test_poll_and_route_bring_the_payments(self):
        order = self._order(self.customer, self.product, qty=5)
        route = self._route(order)
        stop = route.stop_ids
        self.authenticate(self.driver.login, self.driver.login)
        data = self.url_open("/reparto/api/load?id=%s" % route.id).json()
        self.assertEqual(data["route"]["payments"][str(stop.id)]["payments"], [])
        card = self._card(self.customer, 500, order=order)
        data = self.url_open("/reparto/api/load?id=%s" % route.id).json()
        info = data["route"]["payments"][str(stop.id)]
        self.assertEqual([p["receipt_id"] for p in info["payments"]], [card.id])
        self.assertTrue(info["paid"])
        # the old keys of the poll stay for the cached app3.js
        self.assertIn("stops", data["route"])
        route_data = self.url_open("/reparto/api/route?id=%s" % route.id).json()["route"]
        self.assertEqual(route_data["stops"][0]["payments"][0]["receipt_id"], card.id)

    # ------------------------------------------------------------ the app

    def test_page_loads_app3_then_app4(self):
        self.authenticate(self.driver.login, self.driver.login)
        page = self.url_open("/reparto").text
        self.assertIn("app3.js?v=", page)
        self.assertIn("app4.js?v=", page)
        self.assertLess(page.index("app3.js?v="), page.index("app4.js?v="))
        self.assertIn("app4.js", reparto_controller.ASSETS)
        self.assertIn("app3.js", reparto_controller.ASSETS)
        for name in ("app3.js", "app4.js"):
            res = self.url_open("/distri_pedido/static/src/reparto/%s" % name)
            self.assertEqual(res.status_code, 200, name)
        root = get_module_path("distri_pedido")
        with open(os.path.join(root, "static/src/reparto/app3.js"), encoding="utf-8") as f:
            app3 = f.read()
        # app3.js works alone: the extension is optional and guarded
        self.assertIn('extHook("noticeHtml"', app3)
        self.assertIn("window.RepartoApp", app3)

    def test_app4_notices_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v129_16.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertIn("pay-ok", out["paid"])
        self.assertIn("Pagado por transferencia: $ 2.000", out["paid"])
        self.assertIn("acreditó Gonzalo 14:05", out["paid"])
        self.assertIn("Ver comprobante", out["paid"])
        self.assertIn("✓ Verificado con Mercado Pago", out["paid"])
        self.assertIn("Pagó $ 1.200 por transferencia.", out["partial"])
        self.assertIn("Saldo a cobrar: $ 800", out["partial"])
        self.assertNotIn("Verificado", out["partial"])
        self.assertIn("pay-wait", out["pending"])
        self.assertIn("Comprobante cargado, sin acreditar: $ 1.500", out["pending"])
        self.assertIn("Todavía no bajó el saldo", out["pending"])
        self.assertEqual((out["none"], out["old"]), ("", ""))
        self.assertIn("Comprobante cargado, sin acreditar: $ 300", out["polled"])
        self.assertEqual(out["rerenders"], 1)
