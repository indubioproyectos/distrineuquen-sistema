"""Transfer receipt photographed in the driver's app (models/comprobante_app.py).

The photo reaches Cobros as the same record a receipt photo received by
WhatsApp becomes. Synthetic data only: no real customer, phone or receipt.
"""
import base64
import io
import unittest

from PIL import Image

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.comprobante_pendiente import PARAM_RED
from odoo.addons.distri_pedido.models.ocr_comprobante import ocr_available
from odoo.addons.distri_pedido.tests import receipt_images
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid


def _png(color=(250, 250, 250)):
    buffer = io.BytesIO()
    Image.new("RGB", (40, 60), color).save(buffer, format="PNG")
    return buffer.getvalue()


def photo_url(raw):
    return "data:image/png;base64," + base64.b64encode(raw).decode()


@tagged("post_install", "-at_install", "distri_comprobante_app")
class TestComprobanteApp(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.customer = cls.env["res.partner"].create({
            "name": "Almacén Prueba Foto App", "company_type": "company"})
        cls.Promise = cls.env["distri.promised.transfer"].sudo()
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        # a copy of the lab already has photos taken in the app: only this test's count
        cls.photo_floor = cls.Message.search([], order="id desc", limit=1).id or 0
        cls.product_a.invoice_policy = "order"
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_RED, "0")

    # ------------------------------------------------------------ helpers

    def _deliver(self, route, stop, captured="2026-09-26T15:00:00.000Z", **kw):
        payload = dict({"transfer": "yes", "transfer_amount": 1000,
                        "receipt_answer": "photo"}, **kw)
        res = self._sync([self._event(route, "visit", self._visit(stop, **payload),
                                      captured=captured)], user=route.driver_id)
        self.assertEqual(res[0]["status"], "ok", res)
        return self.Promise.search([("stop_id", "=", stop.id)])

    def _receipt_event(self, route, stop, photo_uuid=None, raw=None, amount=1000,
                       captured="2026-09-26T15:01:00.000Z"):
        return self._event(route, "receipt", {
            "photo_uuid": photo_uuid or _uuid(), "stop_id": stop.id,
            "visit_uuid": stop.visit_uuid or _uuid(), "transfer_amount": amount,
            "photo": photo_url(raw or _png()),
        }, captured=captured)

    def _photos(self, stop=None):
        domain = [("distri_from_app", "=", True), ("id", ">", self.photo_floor)]
        if stop:
            domain.append(("distri_app_stop_id", "=", stop.id))
        return self.Message.search(domain)

    # ------------------------------------------------- one review item

    def test_photo_becomes_one_receipt_to_review_also_on_retry(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self._deliver(route, stop)
        event = self._receipt_event(route, stop)
        res = self._sync([event])
        self.assertEqual(res[0]["status"], "ok", res)
        photo = self._photos(stop)
        self.assertEqual(len(photo), 1)
        # the same record a WhatsApp photo becomes, in the same list
        self.assertEqual(photo.direction, "in")
        self.assertEqual(photo.message_type, "image")
        self.assertTrue(photo.distri_is_receipt)
        self.assertEqual(photo.distri_receipt_state, "pending")
        self.assertTrue(photo.attachment_id, "the original photo is kept")
        self.assertEqual(photo.attachment_id.mimetype, "image/png")
        self.assertEqual(photo.attachment_id.raw, _png())
        self.assertEqual(photo.distri_forwarded_by_id, self.driver)
        self.assertEqual(photo.distri_amount, 1000)
        self.assertEqual(photo.distri_order_ref, order.name)
        self.assertEqual(photo.distri_partner_id, self.customer)
        self.assertIn(order.name, photo.distri_partner_source)
        self.assertTrue(self.Message.search_count([
            ("id", "=", photo.id), ("direction", "=", "in"),
            ("message_type", "in", ("image", "document")), ("distri_is_receipt", "=", True)]),
            "shown by Cobros › Comprobantes por WhatsApp")
        # the same POST again, and another event with the same photo
        self.assertEqual(self._sync([event])[0]["status"], "duplicate")
        again = dict(self._receipt_event(route, stop), payload=dict(event["payload"]))
        res = self._sync([again])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(len(self._photos(stop)), 1, "the same photo is never created twice")
        # the sync log never keeps the photo itself
        log = self.env["distri.sync.event"].search([("uuid", "=", event["uuid"])])
        self.assertNotIn(event["payload"]["photo"][30:90], log.payload)
        self.assertFalse(self.env["account.payment"].search([
            ("partner_id", "=", self.customer.id)]), "a photo never creates a payment")

    def test_two_photos_in_one_batch_and_a_bad_one(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self._deliver(route, stop)
        photo_uuid = _uuid()
        first = self._receipt_event(route, stop, photo_uuid=photo_uuid)
        second = self._receipt_event(route, stop, photo_uuid=photo_uuid)
        bad = dict(self._receipt_event(route, stop))
        bad["payload"] = dict(bad["payload"], photo="data:image/png;base64,QUJD")
        res = self._sync([first, second, bad])
        self.assertEqual([r["status"] for r in res], ["ok", "ok", "error"], res)
        self.assertFalse(res[2]["retry"])
        self.assertEqual(len(self._photos(stop)), 1)

    def test_photo_of_another_order_or_driver_is_rejected(self):
        first = self._order(self.customer, self.product_a)
        second = self._order(self.customer, self.product_a)
        route = self._route(first | second)
        stop_a, stop_b = route.stop_ids
        photo_uuid = _uuid()
        self.assertEqual(self._sync([self._receipt_event(route, stop_a, photo_uuid)])[0]["status"],
                         "ok")
        res = self._sync([self._receipt_event(route, stop_b, photo_uuid)])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("otro pedido", res[0]["error"])
        res = self._sync([self._receipt_event(route, stop_b)], user=self.other_driver)
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual(len(self._photos()), 1)

    # -------------------------------------------- the pending receipt

    def test_photo_resolves_the_pending_receipt_of_its_visit(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        promise = self._deliver(route, stop)
        self.assertEqual(promise.state, "open")
        self.assertEqual(promise.receipt_answer, "photo")
        self._sync([self._receipt_event(route, stop)])
        photo = self._photos(stop)
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.resolved_via, "app")
        self.assertEqual(promise.receipt_id, photo)
        self.assertEqual(promise.resolved_by_id, self.driver)
        self.assertEqual(promise.resolved_by_label, "Repartidor Test (repartidor)")
        # «No es comprobante»: the pending receipt opens again
        photo.action_distri_dismiss_receipt()
        self.assertEqual(promise.state, "open")
        self.assertFalse(stop.has_receipt_photo)

    def test_photo_before_its_visit_resolves_it_and_nothing_else(self):
        older = self._order(self.customer, self.product_a)
        order = self._order(self.customer, self.product_a)
        route = self._route(older | order)
        stop_old, stop = route.stop_ids
        # another transfer of the same customer and amount, still open
        other = self._deliver(route, stop_old, receipt_answer="not_yet")
        self._sync([self._receipt_event(route, stop, captured="2026-09-26T15:05:00.000Z")])
        self.assertEqual(other.state, "open", "never resolves another visit by amount")
        promise = self._deliver(route, stop, captured="2026-09-26T15:10:00.000Z")
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.receipt_id, self._photos(stop))
        self.assertEqual(other.state, "open")

    def test_answers_without_photo_still_work(self):
        orders = self._order(self.customer, self.product_a) | \
            self._order(self.customer, self.product_a) | self._order(self.customer, self.product_a)
        route = self._route(orders)
        # «forwarded»: an old phone that still answers «Ya lo reenvié»
        for stop, answer in zip(route.stop_ids, ("forwarded", "customer_wa", "not_yet")):
            promise = self._deliver(route, stop, receipt_answer=answer)
            self.assertEqual(stop.receipt_answer, answer)
            self.assertEqual(promise.state, "open")
            self.assertFalse(stop.has_receipt_photo)
        self.assertFalse(self._photos())
        res = self._sync([self._event(route, "visit", self._visit(
            route.stop_ids[0], transfer="yes", transfer_amount=1000, receipt_answer="maybe"))])
        self.assertEqual(res[0]["status"], "error")
        # the depot (no photo there) keeps its two answers
        depot = dict(self.env["distri.depot.delivery"]._fields["receipt_answer"].selection)
        self.assertEqual(set(depot), {"forwarded", "not_yet"})

    # ------------------------------------------------- links and views

    def test_links_to_the_order_and_the_stop(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self._deliver(route, stop)
        self._sync([self._receipt_event(route, stop)])
        photo = self._photos(stop)
        self.assertEqual(photo.distri_app_stop_id, stop)
        self.assertEqual(photo.distri_app_order_id, order)
        self.assertEqual(photo.distri_app_route_id, route)
        self.assertTrue(stop.has_receipt_photo)
        self.assertEqual(stop.receipt_photo_count, 1)
        self.assertEqual(order.distri_receipt_photo_count, 1)
        action = order.action_distri_open_receipt_photos()
        self.assertEqual(action["res_model"], "distri.whatsapp.message")
        self.assertEqual(action["res_id"], photo.id)
        self.assertEqual(stop.action_open_receipt_photos()["res_id"], photo.id)
        # a phone that lost the visit learns the photo got here
        visit = route._app_payload()["stops"][0]["visit"]
        self.assertTrue(visit["receipt_photo"])
        self.assertEqual(visit["receipt_answer"], "photo")

    def test_order_without_customer_keeps_the_photo(self):
        generic = self.env["res.partner"].create({
            "name": "Socio sin asignar prueba foto", "distri_generic_partner": True,
            "distri_generic_kind": "retail"})
        order = self._order(generic, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        promise = self._deliver(route, stop)
        self._sync([self._receipt_event(route, stop)])
        photo = self._photos(stop)
        self.assertEqual(len(photo), 1)
        self.assertFalse(photo.distri_partner_id)
        self.assertEqual(photo.distri_app_order_id, order)
        self.assertEqual(promise.state, "fulfilled", "its own visit, customer or not")
        # «Asignar cliente» (asignar_cliente.py): the photo takes the customer
        order.sudo().with_context(distri_asignar_cliente=True).write(
            {"partner_id": self.customer.id})
        self.assertEqual(photo.distri_partner_id, self.customer)

    def test_photo_after_the_settlement_is_still_taken(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        promise = self._deliver(route, stop)
        route.sudo().write({"state": "settled"})
        res = self._sync([self._receipt_event(route, stop)])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(promise.state, "fulfilled")
        # anything else is still refused
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=1000),
                                      captured="2026-09-26T16:00:00.000Z")])
        self.assertEqual(res[0]["status"], "error")

    # ------------------------------------------------------ rendición

    def test_settlement_shows_transfers_with_and_without_photo(self):
        orders = self._order(self.customer, self.product_a) | \
            self._order(self.customer, self.product_a) | self._order(self.customer, self.product_a)
        route = self._route(orders)
        with_photo, without_photo, cash = route.stop_ids
        self._deliver(route, with_photo)
        self._deliver(route, without_photo, receipt_answer="not_yet", transfer_amount=600)
        self._sync([self._event(route, "visit", self._visit(cash, cash=1000))])
        self._sync([self._receipt_event(route, with_photo)])
        self.assertEqual(route.transfer_stop_ids, with_photo | without_photo)
        self.assertEqual(route.transfer_total, 1600)
        self.assertEqual(route.transfer_photo_count, 1)
        self.assertEqual(route.transfer_no_photo_count, 1)
        self.assertTrue(with_photo.has_receipt_photo)
        self.assertFalse(without_photo.has_receipt_photo)
        self.assertEqual(with_photo.receipt_status, "Foto en la app")
        self.assertTrue(without_photo.receipt_status.startswith("Sin foto"))
        self.assertFalse(cash.receipt_status)

    # ------------------------------------------------- «Cliente en rojo»

    def test_photo_counts_as_receipt_sent_for_customer_in_red(self):
        first = self._order(self.customer, self.product_a)
        old_route = self._route(first)
        old_stop = old_route.stop_ids
        old = self._deliver(old_route, old_stop)  # 26/09, the photo still on the phone
        self.env["ir.config_parameter"].sudo().set_param(PARAM_RED, "1")
        second = self._order(self.customer, self.product_a)
        route = self._route(second, driver=self.other_driver)
        stop = route.stop_ids
        self.assertTrue(stop.red)
        self.assertEqual(stop.red_receipts, 1)
        # the photo arrives when the phone gets signal: no longer in red
        self._sync([self._receipt_event(old_route, old_stop)])
        self.assertEqual(old.state, "fulfilled")
        stop._refresh_amounts()
        self.assertFalse(stop.red)
        self.assertFalse(second._distri_red_notice())

    # ----------------------------------------------------------- OCR

    def test_the_route_photos_cron_reads_the_photo(self):
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self._deliver(route, stop)
        data = receipt_images.sample(7)
        data["amount"] = 1000.0
        png, _expected = receipt_images.render("Mercado Pago", data)
        self._sync([self._receipt_event(route, stop, raw=png)])
        photo = self._photos(stop)
        self.assertFalse(photo.media_state, "read in the background")
        self.env["distri.route.expense"]._cron_read_photos()
        self.assertEqual(photo.media_state, "done")
        self.assertTrue(photo.distri_amount_confidence)
        if not ocr_available():
            raise unittest.SkipTest("tesseract is not installed")
        self.assertEqual(photo.distri_read_amount, 1000)
        self.assertEqual(photo.distri_amount_confidence, "high")
        self.assertFalse(photo.distri_amount_review)
