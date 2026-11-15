"""Several WhatsApp numbers with roles, senders without a phone (BSUID) and
media reliability (19.0.1.88.0: models/whatsapp.py, models/wa_payload.py)."""
import hashlib
from datetime import timedelta
from unittest.mock import MagicMock, patch

from odoo import fields
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models import wa_payload
from odoo.addons.distri_pedido.models import whatsapp as wa
from odoo.addons.distri_pedido.tests.test_pedido_whatsapp import PedidoWhatsappMixin

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
PDF = b"%PDF-1.4\n%test\n"
PHONE_ID_BOTH = "900000000000101"
PHONE_ID_RECEIPTS = "900000000000102"
PHONE_ID_ORDERS = "900000000000103"
PHONE_ID_UNKNOWN = "900000000000199"
BSUID = "AR.13491208655302741918"


def payload(phone_number_id, messages, contacts=None, statuses=None):
    value = {"messaging_product": "whatsapp",
             "metadata": {"display_phone_number": "5492990000000",
                          "phone_number_id": phone_number_id},
             "messages": messages}
    if contacts is not None:
        value["contacts"] = contacts
    if statuses is not None:
        value["statuses"] = statuses
    return {"object": "whatsapp_business_account",
            "entry": [{"id": "900000000000001",
                       "changes": [{"field": "messages", "value": value}]}]}


class NumerosMixin(PedidoWhatsappMixin):

    @classmethod
    def _setup_numeros(cls):
        cls._setup_pedido()
        Numero = cls.env["distri.whatsapp.numero"].sudo()
        cls.numero_both = Numero.create({"name": "+54 9 299 000-0101",
                                         "phone_number_id": PHONE_ID_BOTH, "rol": "both"})
        cls.numero_receipts = Numero.create({"name": "+54 9 299 000-0102",
                                             "phone_number_id": PHONE_ID_RECEIPTS,
                                             "rol": "receipts"})
        cls.numero_orders = Numero.create({"name": "+54 9 299 000-0103",
                                           "phone_number_id": PHONE_ID_ORDERS, "rol": "orders"})
        cls.Message = cls.env["distri.whatsapp.message"].sudo()

    _wamid = 0

    def _store(self, phone_number_id, message, contacts=None, statuses=None):
        """Store one webhook message; returns the record."""
        NumerosMixin._wamid += 1
        message = dict(message, id=message.get("id") or "wamid.numeros.%s" % NumerosMixin._wamid)
        message.setdefault("timestamp", str(int(fields.Datetime.now().timestamp())))
        self.Message._wa_store_webhook(payload(phone_number_id, [message], contacts, statuses))
        return self.Message.search([("wa_message_id", "=", message["id"])])

    def _text(self, phone_number_id, body, sender="5492995551234", **extra):
        message = {"type": "text", "text": {"body": body}}
        if sender:
            message["from"] = sender
        message.update(extra)
        contacts = [{"profile": {"name": "Cliente Numeros"}, "wa_id": sender}] if sender else []
        return self._store(phone_number_id, message, contacts)

    def _media(self, phone_number_id, sender="5492995551234", mime="image/jpeg",
               caption="", kind="image", **extra):
        media = {"id": "7%014d" % NumerosMixin._wamid, "mime_type": mime,
                 "sha256": "x", "caption": caption}
        message = {"type": kind, kind: media}
        if sender:
            message["from"] = sender
        message.update(extra)
        return self._store(phone_number_id, message, [])

    def _code_line(self, order):
        return "Pedido %s · código %s" % (order.name, order.distri_wa_code)


@tagged("post_install", "-at_install", "distri_whatsapp_numeros")
class TestPayloadHelpers(TransactionCase):

    def test_sender_with_phone(self):
        got = wa_payload.sender_identity(
            {"from": "5492995551234"},
            [{"wa_id": "5492995551234", "profile": {"name": "Ana"}, "user_id": BSUID}])
        self.assertEqual(got, {"phone": "5492995551234", "user_id": BSUID, "username": "",
                               "name": "Ana"})

    def test_sender_without_phone_shapes(self):
        # messages[].from missing, from_user_id present, contact without wa_id
        got = wa_payload.sender_identity(
            {"from_user_id": BSUID},
            [{"user_id": BSUID, "profile": {"name": "Ana", "username": "@ana.kiosco"}}])
        self.assertEqual((got["phone"], got["user_id"], got["username"], got["name"]),
                         ("", BSUID, "@ana.kiosco", "Ana"))
        # from null, wa_id null, only contacts[].user_id
        got = wa_payload.sender_identity({"from": None},
                                         [{"wa_id": None, "user_id": BSUID, "profile": {}}])
        self.assertEqual((got["phone"], got["user_id"]), ("", BSUID))
        # a BSUID in `from` is never taken as a phone
        got = wa_payload.sender_identity({"from": BSUID}, [])
        self.assertEqual((got["phone"], got["user_id"]), ("", BSUID))
        # garbage never raises
        for message, contacts in (({}, None), ({"from": {"x": 1}}, "nope"),
                                  ({"from": ""}, [None, 3, {"profile": "x"}]),
                                  (None, [{"user_id": ["x"]}])):
            got = wa_payload.sender_identity(message, contacts)
            self.assertEqual(got["phone"], "")
        # the contact of the phone wins when there are several
        got = wa_payload.sender_identity(
            {"from": "5492990000002"},
            [{"wa_id": "5492990000001", "profile": {"name": "Uno"}},
             {"wa_id": "5492990000002", "profile": {"name": "Dos"}}])
        self.assertEqual(got["name"], "Dos")

    def test_status_recipient(self):
        self.assertEqual(wa_payload.status_recipient({"recipient_id": "5492995551234"}),
                         ("5492995551234", ""))
        self.assertEqual(wa_payload.status_recipient({"recipient_user_id": BSUID}), ("", BSUID))
        self.assertEqual(wa_payload.status_recipient({"recipient_id": None}), ("", ""))


@tagged("post_install", "-at_install", "distri_whatsapp_numeros")
class TestWhatsappNumeros(NumerosMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_numeros()

    # ------------------------------------------------------------ routing

    def test_message_keeps_the_receiving_number(self):
        msg = self._text(PHONE_ID_BOTH, "hola")
        self.assertEqual(msg.numero_id, self.numero_both)
        self.assertEqual(msg.wa_rol, "both")
        self.assertFalse(msg.wa_numero_no_configurado)
        self.assertEqual(msg.numero_recibido, "+54 9 299 000-0101")
        self.assertEqual(self.numero_both.whatsapp_count, 1)
        self.assertIn(msg, self.Message.search([("numero_id", "=", self.numero_both.id)]))

    def test_unknown_number_is_stored_flagged_with_the_default_role(self):
        order = self._web_order()
        msg = self._text(PHONE_ID_UNKNOWN, self._code_line(order))
        self.assertTrue(msg)
        self.assertFalse(msg.numero_id)
        self.assertTrue(msg.wa_numero_no_configurado)
        self.assertEqual(msg.wa_rol, "both")
        self.assertEqual(msg.numero_recibido, PHONE_ID_UNKNOWN)
        # treated as «Pedidos y comprobantes»: the code confirms the order
        self.assertEqual(order.distri_wa_state, "received")
        self.assertEqual(order.distri_wa_phone, "+5492995551234")
        self.assertIn(msg, self.Message.search([("wa_numero_no_configurado", "=", True)]))

    def test_archived_number_counts_as_not_configured(self):
        self.numero_orders.active = False
        msg = self._text(PHONE_ID_ORDERS, "hola")
        self.assertTrue(msg.wa_numero_no_configurado)
        self.assertEqual(msg.wa_rol, "both")

    def test_messages_without_number_are_not_flagged(self):
        # app photos and chat imports carry no phone_number_id
        msg = self._message("hola")
        self.assertFalse(msg.wa_numero_no_configurado)
        self.assertFalse(msg.wa_rol)

    def test_receipts_number_ignores_order_codes(self):
        order = self._web_order()
        msg = self._text(PHONE_ID_RECEIPTS, self._code_line(order))
        self.assertEqual(msg.wa_rol, "receipts")
        self.assertEqual(order.distri_wa_state, "waiting")
        self.assertFalse(order.distri_wa_phone)
        self.assertFalse(order.distri_telefono_verificado)
        self.assertFalse(msg.distri_order_id)
        # nor a phone-only proposal for the waiting order
        self.assertFalse(order.distri_wa_proposal_id)

    def test_receipts_number_creates_receipts_without_using_the_phone(self):
        customer = self.env["res.partner"].create({
            "name": "Kiosco Teléfono Numeros", "phone": "299 5557777",
            "company_type": "company"})
        # a plain photo, no caption, from a number that is a customer's phone
        msg = self._media(PHONE_ID_RECEIPTS, sender="5492995557777")
        self.assertTrue(msg.distri_is_receipt)
        self.assertEqual(msg.distri_receipt_state, "pending")
        self.assertNotEqual(msg.distri_partner_id, customer)
        self.assertFalse(msg.distri_partner_id)
        # the same photo to the default number is not a receipt by itself
        # but proposes the customer of the phone
        other = self._media(PHONE_ID_BOTH, sender="5492995557777", caption="comprobante")
        self.assertEqual(other.distri_partner_id, customer)
        # a PDF too
        pdf = self._media(PHONE_ID_RECEIPTS, sender="5492995557777",
                          mime="application/pdf", kind="document")
        self.assertTrue(pdf.distri_is_receipt)

    def test_receipts_number_identifies_driver_or_seller(self):
        driver = new_test_user(self.env, login="repartidor_numeros", name="Repartidor Numeros",
                               groups="base.group_user")
        driver.sudo().distri_whatsapp_vendedor = "299 4440000"
        msg = self._media(PHONE_ID_RECEIPTS, sender="5492994440000")
        self.assertEqual(msg.distri_forwarded_by_id, driver)
        self.assertTrue(msg.distri_is_receipt)

    def test_receipts_number_never_learns_a_customer_phone(self):
        customer = self.env["res.partner"].create({"name": "Almacén Aprende Numeros",
                                                   "company_type": "company"})
        msg = self._media(PHONE_ID_RECEIPTS, sender="5492996660000")
        self.assertFalse(msg.distri_partner_id)
        # learning that phone for a customer does not reach the receipt
        self.env["distri.payer.identity"]._learn("phone", "5492996660000", customer)
        self.assertFalse(msg.distri_partner_id)
        # it does reach one sent to the default number
        other = self._media(PHONE_ID_BOTH, sender="5492996660001", caption="comprobante")
        self.env["distri.payer.identity"]._learn("phone", "5492996660001", customer)
        self.assertEqual(other.distri_partner_id, customer)

    def test_orders_number_photos_are_not_receipts(self):
        msg = self._media(PHONE_ID_ORDERS, caption="comprobante", mime="application/pdf",
                          kind="document")
        self.assertFalse(msg.distri_is_receipt)
        self.assertFalse(msg.distri_receipt_state)
        order = self._web_order(phone="299 555 2222")
        self._text(PHONE_ID_ORDERS, self._code_line(order), sender="5492995552222")
        self.assertEqual(order.distri_wa_state, "received")

    # ------------------------------------------------------------ no phone

    def test_message_without_phone_is_stored(self):
        msg = self._store(PHONE_ID_BOTH, {"from_user_id": BSUID, "type": "text",
                                          "text": {"body": "hola"}},
                          contacts=[{"user_id": BSUID,
                                     "profile": {"name": "Ana", "username": "@ana"}}])
        self.assertTrue(msg)
        self.assertFalse(msg.wa_id)
        self.assertEqual(msg.wa_user_id, BSUID)
        self.assertEqual(msg.wa_username, "@ana")
        self.assertEqual(msg.profile_name, "Ana")
        self.assertTrue(msg.wa_sin_telefono)
        # `from` null and the BSUID only in the contact
        msg = self._store(PHONE_ID_BOTH, {"from": None, "type": "text", "text": {"body": "x"}},
                          contacts=[{"wa_id": None, "user_id": BSUID}])
        self.assertEqual((msg.wa_id, msg.wa_user_id, msg.wa_sin_telefono), (False, BSUID, True))
        # a status of a message we sent to a username
        created, updated = self.Message._wa_store_webhook(payload(PHONE_ID_BOTH, [], statuses=[
            {"id": "wamid.numeros.status.1", "status": "sent", "timestamp": "1790000000",
             "recipient_user_id": BSUID}]))
        self.assertEqual(updated, 1)
        out = self.Message.search([("wa_message_id", "=", "wamid.numeros.status.1")])
        self.assertEqual((out.direction, out.wa_id, out.wa_user_id), ("out", False, BSUID))

    def test_with_phone_is_not_flagged(self):
        msg = self._text(PHONE_ID_BOTH, "hola")
        self.assertFalse(msg.wa_sin_telefono)
        self.assertEqual(msg.wa_id, "5492995551234")

    def test_order_code_without_phone(self):
        order = self._web_order()
        msg = self._store(PHONE_ID_BOTH, {"from_user_id": BSUID, "type": "text",
                                          "text": {"body": self._code_line(order)}},
                          contacts=[{"user_id": BSUID, "profile": {"username": "@kiosco"}}])
        self.assertEqual(msg.distri_order_id, order)
        self.assertEqual(order.distri_wa_state, "received")
        self.assertTrue(order.distri_wa_sin_telefono)
        self.assertFalse(order.distri_wa_phone)
        self.assertFalse(order.distri_telefono_verificado)
        self.assertIn("WhatsApp sin teléfono", order.distri_semaforo_motivos)
        self.assertTrue(order.message_ids.filtered(
            lambda m: "No trae teléfono" in (m.body or "")))

    def test_order_code_without_phone_does_not_touch_the_customer(self):
        customer = self.env["res.partner"].create({"name": "Almacén Sin Teléfono WA",
                                                   "company_type": "company"})
        order = self._web_order(partner=customer)
        self._store(PHONE_ID_BOTH, {"from_user_id": BSUID, "type": "text",
                                    "text": {"body": self._code_line(order)}})
        self.assertEqual(order.distri_wa_state, "received")
        self.assertFalse(customer.distri_whatsapp)
        self.assertFalse(customer.distri_whatsapp_confirmed)
        self.assertIn("WhatsApp sin teléfono", order.distri_semaforo_motivos)

    def test_receipt_without_phone_goes_to_review(self):
        msg = self._media(PHONE_ID_BOTH, sender=None, mime="application/pdf",
                          kind="document", from_user_id=BSUID)
        self.assertTrue(msg.wa_sin_telefono)
        self.assertTrue(msg.distri_is_receipt)
        self.assertEqual(msg.distri_receipt_state, "pending")
        card = self.env["distri.mp.collection"].sudo().search([("receipt_id", "=", msg.id)])
        self.assertEqual(card.kind, "verify")
        self.assertEqual(card.column, "review")
        self.assertTrue(card.review_reason.startswith("Remitente sin teléfono"),
                        card.review_reason)
        # the attachment name does not need a phone
        self.assertTrue(msg._wa_media_attachment_name("pdf").startswith("comprobante-sin-numero"))

    # ------------------------------------------------------------ migration

    def test_migration_of_the_phone_param(self):
        Numero = self.env["distri.whatsapp.numero"]
        old = self.Message.create({"wa_message_id": "wamid.numeros.migra", "direction": "in",
                                   "wa_id": "5492995551234", "message_type": "text",
                                   "body": "viejo", "phone_number_id": "900000000000555",
                                   "wa_rol": False})
        self.assertFalse(old.numero_id)
        self.env["ir.config_parameter"].sudo().set_param(wa.PARAM_PHONE_NUMBER_ID,
                                                         "900000000000555")
        numero = Numero._wa_migrate_phone_param()
        self.assertEqual((numero.phone_number_id, numero.rol, numero.active),
                         ("900000000000555", "both", True))
        old.invalidate_recordset()
        self.assertEqual(old.numero_id, numero)
        self.assertEqual(old.wa_rol, "both")
        # idempotent
        self.assertEqual(Numero._wa_migrate_phone_param(), numero)
        self.assertEqual(Numero.search_count([("phone_number_id", "=", "900000000000555")]), 1)
        # a new message on it routes like before
        msg = self._text("900000000000555", "hola")
        self.assertEqual((msg.numero_id, msg.wa_rol, msg.wa_numero_no_configurado),
                         (numero, "both", False))
        # nothing to migrate
        self.env["ir.config_parameter"].sudo().set_param(wa.PARAM_PHONE_NUMBER_ID, "")
        self.assertFalse(Numero._wa_migrate_phone_param())

    def test_phone_number_id_is_unique_and_numeric(self):
        from odoo.exceptions import ValidationError
        with self.assertRaises(ValidationError):
            self.env["distri.whatsapp.numero"].create({"name": "x", "phone_number_id": "abc"})


@tagged("post_install", "-at_install", "distri_whatsapp_numeros")
class TestWhatsappMedia(NumerosMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_numeros()
        cls.admin_user = new_test_user(
            cls.env, login="admin_media_numeros", name="Administración Media",
            groups="base.group_user,distri_pedido.grupo_administracion")

    def _pending(self, hours_old, phone_number_id=PHONE_ID_BOTH, **values):
        NumerosMixin._wamid += 1
        vals = {"wa_message_id": "wamid.media.%s" % NumerosMixin._wamid, "direction": "in",
                "wa_id": "5492990000000", "message_type": "image",
                "media_id": "8%014d" % NumerosMixin._wamid, "media_mime_type": "image/png",
                "media_state": "pending", "phone_number_id": phone_number_id}
        vals.update(values)
        msg = self.Message.create(vals)
        created = fields.Datetime.now() - timedelta(hours=hours_old)
        self.env.cr.execute("UPDATE distri_whatsapp_message SET create_date = %s WHERE id = %s",
                            [created, msg.id])
        msg.invalidate_recordset(["create_date"])
        return msg

    def _alerts(self, numero, level):
        return numero.activity_ids.filtered(lambda a: a.summary == wa.MEDIA_ALERT_SUMMARY[level])

    # ------------------------------------------------------------ fresh URL

    def test_every_attempt_asks_for_a_fresh_url(self):
        msg = self._pending(0)

        def responses(download_status):
            info = MagicMock(status_code=200)
            info.json.return_value = {"url": "https://lookaside.fbsbx.com/whatsapp/media/1",
                                      "file_size": len(PNG),
                                      "sha256": hashlib.sha256(PNG).hexdigest()}
            download = MagicMock(status_code=download_status,
                                 headers={"Content-Type": "image/png",
                                          "Content-Length": str(len(PNG))})
            download.__enter__.return_value = download
            download.iter_content.return_value = [PNG]
            return [info, download]

        graph = "%s/%s" % (wa.WA_GRAPH_API, msg.media_id)
        with patch.object(wa.requests, "get", side_effect=responses(403)) as get:
            msg._wa_download_media("token")
        self.assertEqual((msg.media_state, msg.media_attempts), ("pending", 1))
        self.assertEqual(get.call_args_list[0].args[0], graph)
        with patch.object(wa.requests, "get", side_effect=responses(200)) as get:
            msg._wa_download_media("token")
        # the second attempt asked the Graph API again before downloading
        self.assertEqual(get.call_args_list[0].args[0], graph)
        self.assertEqual(get.call_count, 2)
        self.assertEqual(msg.media_state, "done")
        # same single token whatever number received it
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer token")

    def test_cron_skips_pending_without_media_id(self):
        # a chat import cut halfway: pending with its file already attached
        attachment = self.env["ir.attachment"].sudo().create({"name": "x.png", "raw": PNG})
        stuck = self._pending(0, media_id=False, attachment_id=attachment.id,
                              phone_number_id=False)
        self.env["ir.config_parameter"].sudo().set_param(wa.PARAM_ACCESS_TOKEN, "token")
        with patch.object(wa.WhatsappMessage, "_wa_download_media", autospec=True,
                          return_value=False) as download, \
                patch.object(self.registry["ir.cron"], "_commit_progress", return_value=1000.0):
            self.Message._wa_cron_download_media()
        self.assertNotIn(stuck.id, [call.args[0].id for call in download.call_args_list])

    # ------------------------------------------------------------ alerts

    def test_late_media_alert_threshold_and_single_activity(self):
        young = self._pending(1.9)
        late = self._pending(2.1)
        late_error = self._pending(3, media_state="error", media_error="Meta respondió HTTP 404")
        done = self._pending(3, media_state="done")
        attached = self._pending(3, attachment_id=self.env["ir.attachment"].sudo().create(
            {"name": "y.png", "raw": PNG}).id)
        sticker = self._pending(3, media_mime_type="audio/ogg", media_state="error")
        result = self.Message._wa_cron_media_alerts()
        self.assertEqual(result["late"], late | late_error)
        activity = self._alerts(self.numero_both, "late")
        self.assertEqual(len(activity), 1)
        self.assertTrue(activity.user_id.has_group("distri_pedido.grupo_administracion"))
        self.assertIn("Mensaje %s" % late.id, activity.note)
        self.assertIn("HTTP 404", activity.note)
        for msg in (young, done, attached, sticker):
            self.assertFalse(msg.media_alert, msg)
        # the next run alerts nothing again
        self.assertFalse(self.Message._wa_cron_media_alerts())
        # a new late one joins the same open activity
        newer = self._pending(2.5)
        self.Message._wa_cron_media_alerts()
        activity = self._alerts(self.numero_both, "late")
        self.assertEqual(len(activity), 1)
        self.assertIn("Mensaje %s" % newer.id, activity.note)
        self.assertIn("Mensaje %s" % late.id, activity.note)

    def test_media_about_to_expire_alert(self):
        late = self._pending(3)
        self.Message._wa_cron_media_alerts()
        self.assertEqual(late.media_alert, "late")
        # five days later it is still not there: a second, urgent alert
        now = fields.Datetime.now() + timedelta(days=5)
        result = self.Message._wa_cron_media_alerts(now=now)
        self.assertEqual(result["expiring"], late)
        self.assertNotIn("late", result)
        self.assertEqual(late.media_alert, "expiring")
        self.assertEqual(len(self._alerts(self.numero_both, "expiring")), 1)
        self.assertFalse(self.Message._wa_cron_media_alerts(now=now))
        # older than the expiry plus a day: nothing to do, no alert
        old = self._pending(24 * 9)
        self.Message._wa_cron_media_alerts()
        self.assertFalse(old.media_alert)
        # never alerted before and already 6 days old: straight to «por vencer»
        six = self._pending(24 * 6, phone_number_id=PHONE_ID_RECEIPTS)
        result = self.Message._wa_cron_media_alerts()
        self.assertEqual(result["expiring"], six)
        self.assertEqual(len(self._alerts(self.numero_receipts, "expiring")), 1)

    def test_alert_of_unknown_number_goes_to_the_default_number(self):
        msg = self._pending(3, phone_number_id=PHONE_ID_UNKNOWN)
        self.assertTrue(msg.wa_numero_no_configurado)
        self.Message._wa_cron_media_alerts()
        anchor = self.env["distri.whatsapp.numero"]._wa_default()
        self.assertTrue(anchor)
        self.assertIn("Mensaje %s" % msg.id, self._alerts(anchor, "late").note)

    def test_alert_sends_no_email(self):
        self._pending(3)
        before = self.env["mail.mail"].sudo().search_count([])
        self.Message._wa_cron_media_alerts()
        self.assertEqual(self.env["mail.mail"].sudo().search_count([]), before)

    # ------------------------------------------------------------ reconciliation

    def test_missing_files_action(self):
        pending = self._pending(0)
        error = self._pending(30, media_state="error")
        done = self._pending(0, media_state="done")
        chat = self._pending(0, media_id=False, phone_number_id=False,
                             attachment_id=self.env["ir.attachment"].sudo().create(
                                 {"name": "z.png", "raw": PNG}).id)
        text = self.Message.create({"wa_message_id": "wamid.media.text", "direction": "in",
                                    "message_type": "text", "body": "hola"})
        action = self.env.ref("distri_pedido.action_whatsapp_missing_files").sudo().run()
        self.assertEqual(action["res_model"], "distri.whatsapp.message")
        self.assertEqual(action["context"].get("search_default_ultimo_dia"), 1)
        found = self.Message.search(action["domain"])
        self.assertIn(pending, found)
        self.assertIn(error, found)
        for msg in (done, chat, text):
            self.assertNotIn(msg, found)
