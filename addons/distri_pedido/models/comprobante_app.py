"""Transfer receipt photographed in the driver's app (19.0.1.86.0).

When the customer pays by transfer, the driver takes (or picks from the
gallery) a photo of the receipt inside the app: «Sacar foto del
comprobante». It becomes the same record a receipt photo received by
WhatsApp becomes, a `distri.whatsapp.message` with the photo attached, so
administración reviews it where it always does (Cobros › Comprobantes por
WhatsApp and the collections board) and it follows the same pipeline:

- the local OCR reads it in the route photos cron (as the WhatsApp media
  cron does for a downloaded photo), the amount the driver typed playing the
  role of the caption: a reading that differs is «Revisar importe»;
- it is marked «Reenviado por» the driver, the rule of a photo forwarded by
  a seller or a driver: its card always goes to «Para revisar» and the
  customer's phone is never learned from it;
- the customer comes from the order, never from a phone number. An order
  still without a customer (generic partner) keeps the photo linked to the
  order; the customer is filled in when the order gets one;
- it resolves the «Comprobante pendiente» of its visit at once («Vía: Foto
  en la app del repartidor»): a photo taken in the app counts as the receipt
  sent, also for «Cliente en rojo». Dismissing it («No es comprobante»)
  opens the pending receipt again, as for WhatsApp.

The photo never creates a payment (docs cobranza/README.md, decision 10):
the transfer is confirmed from the Mercado Pago report by administración.

The photo travels in its own «receipt» sync event with an id generated on
the phone (`photo_uuid`): the message's `wa_message_id` is «app:<photo_uuid>»
(unique), so a retried POST or a second event with the same photo finds it
there and never creates a second one.
"""
import base64
import logging

from psycopg2 import IntegrityError

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

APP_KEY = "app:%s"
APP_PROFILE = "App de reparto"
APP_VIA = "app"
# what the reading cron takes per run
APP_READ_BATCH = 30
_MIMES = ((b"\xff\xd8\xff", "image/jpeg", "jpg"), (b"\x89PNG", "image/png", "png"),
          (b"RIFF", "image/webp", "webp"))


def photo_mime(raw):
    """(mimetype, extension) of a photo already checked by decode_photo."""
    for magic, mime, ext in _MIMES:
        if raw.startswith(magic):
            return mime, ext
    return "image/jpeg", "jpg"


def app_partner(order):
    """The customer an app receipt is for: the order's, unless the order is
    still on the generic partner (no customer yet)."""
    partner = order.partner_id
    if not partner or partner.distri_generic_partner:
        return partner.browse()
    return partner.commercial_partner_id


class WhatsappMessageAppReceipt(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_from_app = fields.Boolean(
        string="Foto de la app de reparto", readonly=True, index=True, copy=False,
        help="El repartidor sacó la foto del comprobante en la app, en la entrega.")
    distri_app_stop_id = fields.Many2one(
        "distri.route.stop", string="Visita del reparto", readonly=True, index=True,
        ondelete="set null", copy=False)
    distri_app_order_id = fields.Many2one(
        "sale.order", string="Pedido de la visita", readonly=True, index=True,
        ondelete="set null", copy=False)
    distri_app_route_id = fields.Many2one(related="distri_app_stop_id.route_id",
                                          string="Ruta del reparto")

    @api.model
    def _distri_app_receipt_by_uuid(self, photo_uuid):
        return self.sudo().search([("wa_message_id", "=", APP_KEY % photo_uuid)], limit=1)

    @api.model
    def _distri_app_receipt_create(self, stop, user, photo, photo_uuid, captured, amount):
        """The receipt of a photo taken in the app (`photo`: base64 bytes as
        decode_photo returns them). Returns the new message."""
        order = stop.order_id
        partner = app_partner(order)
        if stop.transfer == "yes" and stop.transfer_amount > 0:
            # the visit (synced first) is what the driver said last
            amount = stop.transfer_amount
        raw = base64.b64decode(photo)
        mime, ext = photo_mime(raw)
        when = captured or fields.Datetime.now()
        vals = {
            "wa_message_id": APP_KEY % photo_uuid,
            "direction": "in",
            "message_type": "image",
            "profile_name": APP_PROFILE,
            # the text «Reenviar a la empresa» writes: names the order
            "body": "Comprobante del pedido %s · foto de %s en la app de reparto" % (
                order.name, user.name),
            "timestamp": when,
            "media_mime_type": mime,
            "media_filename": "comprobante-%s-%s.%s" % (
                order.name, when.strftime("%Y%m%d-%H%M%S"), ext),
            "distri_forwarded_by_id": user.id,
            "distri_from_app": True,
            "distri_app_stop_id": stop.id,
            "distri_app_order_id": order.id,
            "distri_order_ref": order.name,
            "distri_partner_id": partner.id or False,
            "distri_partner_source": ("Pedido %s · foto del repartidor en la app" % order.name)
            if partner else False,
            "distri_amount": amount or 0.0,
            "distri_is_receipt": True,
            "distri_receipt_state": "pending",
        }
        Message = self.sudo().with_context(distri_defer_receipt_read=True)
        try:
            with self.env.cr.savepoint():
                msg = Message.create(vals)
        except IntegrityError:
            # the same photo committed by a parallel request: the phone tries
            # again and finds it then («ya estaba»)
            raise RuntimeError("receipt photo %s saved by a parallel request" % photo_uuid)
        attachment = self.env["ir.attachment"].sudo().create({
            "name": vals["media_filename"], "raw": raw, "mimetype": mime,
            "res_model": msg._name, "res_id": msg.id,
        })
        msg = msg.with_context(distri_defer_receipt_read=False)
        msg.with_context(distri_receipt_matching=True).write({"attachment_id": attachment.id})
        # its own visit first: the receipt is known to be that one
        self.env["distri.promised.transfer"].sudo()._distri_resolve_from_app_photos(stop)
        # then what a WhatsApp photo goes through: copies, the transfer of
        # the report, the card on the board
        msg._distri_read_receipt()
        # the photo is read (OCR) in the background, as a downloaded one
        self.env["distri.route.expense"]._trigger_read()
        _logger.info("Receipt photo %s of %s from the app saved as message %s",
                     photo_uuid, order.name, msg.id)
        return msg

    @api.model
    def _distri_read_app_receipts(self, limit=APP_READ_BATCH):
        """The photos from the app not read yet: «done» runs the reading of
        the file (OCR) and the matching (cobros.py, write())."""
        todo = self.sudo().search([("distri_from_app", "=", True), ("media_state", "=", False),
                                   ("attachment_id", "!=", False)], order="id", limit=limit)
        for msg in todo:
            try:
                with self.env.cr.savepoint():
                    msg.write({"media_state": "done"})
            except Exception:
                # never retried for ever: «Leer de nuevo» reads it by hand
                _logger.exception("Receipt photo %s from the app: reading failed", msg.id)
                msg.invalidate_recordset()
                msg.write({"media_state": "error",
                           "media_error": "No se pudo leer la foto (ver el log del servidor)"})
        return len(todo)

    def _distri_app_receipts_action(self):
        """The receipts list (Cobros › Comprobantes por WhatsApp) with these
        photos; the form when there is one."""
        action = self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_receipts")
        action.update({"domain": [("id", "in", self.ids)], "context": {},
                       "name": "Fotos del comprobante"})
        if len(self) == 1:
            action.update({"view_mode": "form", "res_id": self.id,
                           "views": [(self.env.ref("distri_pedido.view_receipt_form").id,
                                      "form")]})
        return action


class DistriRouteExpenseAppReceipt(models.Model):
    _inherit = "distri.route.expense"

    @api.model
    def _cron_read_photos(self, limit=30):
        """Also the transfer receipts photographed in the app."""
        result = super()._cron_read_photos(limit=limit)
        self.env["distri.whatsapp.message"]._distri_read_app_receipts(limit=limit)
        return result


class DistriPromisedTransferAppReceipt(models.Model):
    _inherit = "distri.promised.transfer"

    resolved_via = fields.Selection(selection_add=[(APP_VIA, "Foto en la app del repartidor")],
                                    ondelete={APP_VIA: "set null"})

    @api.model
    def _distri_resolve_from_app_photos(self, stops):
        """The open pending receipt of a transfer of these visits, with the
        first photo taken in the app for the visit that resolved nothing yet
        (and was not dismissed or found a copy). Returns the resolved ones."""
        from .comprobante_pendiente import CLOSED_RECEIPT_STATES, OPEN_STATES
        Message = self.env["distri.whatsapp.message"].sudo()
        resolved = self.browse()
        promises = self.sudo().search([("stop_id", "in", stops.ids), ("kind", "=", "done"),
                                       ("state", "in", OPEN_STATES)])
        for promise in promises:
            photos = Message.search([
                ("distri_app_stop_id", "=", promise.stop_id.id),
                ("distri_is_receipt", "=", True),
                ("distri_receipt_state", "not in", CLOSED_RECEIPT_STATES),
            ], order="timestamp, id")
            used = set(self.sudo().search([("receipt_id", "in", photos.ids)]).receipt_id.ids)
            photo = photos.filtered(lambda m: m.id not in used)[:1]
            if photo:
                promise._distri_resolve(APP_VIA, user=photo.distri_forwarded_by_id or None,
                                        receipt=photo)
                resolved |= promise
        return resolved

    @api.model
    def _distri_resolve_from_receipt(self, receipt):
        """A photo from the app resolves only the pending receipt of its own
        visit, never another one of the customer by amount: the visit may
        reach the server after the photo."""
        if receipt.sudo().distri_from_app:
            return self._distri_resolve_from_app_photos(receipt.sudo().distri_app_stop_id)
        return super()._distri_resolve_from_receipt(receipt)

    def _distri_resolve_from_waiting_receipts(self):
        """A visit synced after its photo (or edited): the photo of its own
        visit first, also for an order without a customer yet."""
        self._distri_resolve_from_app_photos(self.sudo().stop_id)
        return super()._distri_resolve_from_waiting_receipts()


class DistriRouteStopAppReceipt(models.Model):
    _inherit = "distri.route.stop"

    receipt_photo_ids = fields.One2many("distri.whatsapp.message", "distri_app_stop_id",
                                        string="Fotos del comprobante", readonly=True)
    receipt_photo_count = fields.Integer(string="Fotos del comprobante",
                                         compute="_compute_receipt_photo", store=True)
    has_receipt_photo = fields.Boolean(
        string="Foto del comprobante", compute="_compute_receipt_photo", store=True,
        help="El repartidor sacó la foto del comprobante de la transferencia en la app.")
    receipt_status = fields.Char(string="Comprobante recibido", compute="_compute_receipt_status",
                                 compute_sudo=True)

    @api.depends("receipt_photo_ids.distri_receipt_state")
    def _compute_receipt_photo(self):
        for stop in self:
            # «No es comprobante» does not count as a photo of the receipt
            count = len(stop.receipt_photo_ids.filtered(
                lambda m: m.distri_receipt_state != "dismissed"))
            stop.receipt_photo_count = count
            stop.has_receipt_photo = bool(count)

    @api.depends("transfer", "has_receipt_photo")
    def _compute_receipt_status(self):
        promises = self.env["distri.promised.transfer"].sudo().search(
            [("stop_id", "in", self.ids), ("kind", "=", "done")])
        by_stop = {p.stop_id.id: p for p in promises}
        for stop in self:
            promise = by_stop.get(stop.id)
            if stop.transfer != "yes":
                stop.receipt_status = False
            elif stop.has_receipt_photo:
                stop.receipt_status = "Foto en la app"
            elif promise and promise.state == "fulfilled":
                stop.receipt_status = "Sin foto · %s" % (promise.status_label or "Resuelta")
            elif promise:
                stop.receipt_status = "Sin foto · %s" % (promise.status_label or "")
            else:
                stop.receipt_status = "Sin foto"

    def _app_visit(self):
        visit = super()._app_visit()
        if visit and self.state != "dispatched":
            # a phone that lost the visit still knows the photo got here
            visit["receipt_photo"] = self.has_receipt_photo
        return visit

    def action_open_receipt_photos(self):
        return self.receipt_photo_ids._distri_app_receipts_action()


class DistriRouteAppReceipt(models.Model):
    _inherit = "distri.route"

    transfer_stop_ids = fields.Many2many("distri.route.stop", string="Transferencias",
                                         compute="_compute_transfer_receipts")
    transfer_total = fields.Monetary(string="Transferido", compute="_compute_transfer_receipts")
    transfer_photo_count = fields.Integer(string="Transferencias con foto",
                                          compute="_compute_transfer_receipts")
    transfer_no_photo_count = fields.Integer(string="Transferencias sin foto",
                                             compute="_compute_transfer_receipts")

    @api.depends("stop_ids.transfer", "stop_ids.transfer_amount", "stop_ids.has_receipt_photo")
    def _compute_transfer_receipts(self):
        for route in self:
            stops = route.stop_ids.filtered(lambda s: s.transfer == "yes")
            route.transfer_stop_ids = stops
            route.transfer_total = sum(stops.mapped("transfer_amount"))
            route.transfer_photo_count = len(stops.filtered("has_receipt_photo"))
            route.transfer_no_photo_count = len(stops) - route.transfer_photo_count


class SaleOrderAppReceipt(models.Model):
    _inherit = "sale.order"

    distri_receipt_photo_count = fields.Integer(
        string="Fotos del comprobante", compute="_compute_distri_receipt_photo_count",
        compute_sudo=True)

    def _distri_app_receipts(self):
        return self.env["distri.whatsapp.message"].sudo().search([
            ("distri_app_order_id", "in", self.ids), ("distri_receipt_state", "!=", "dismissed")])

    def _compute_distri_receipt_photo_count(self):
        counts = {}
        for msg in self._distri_app_receipts():
            counts[msg.distri_app_order_id.id] = counts.get(msg.distri_app_order_id.id, 0) + 1
        for order in self:
            order.distri_receipt_photo_count = counts.get(order.id, 0)

    def action_distri_open_receipt_photos(self):
        self.ensure_one()
        photos = self.env["distri.whatsapp.message"].search([("distri_app_order_id", "=", self.id)])
        return photos._distri_app_receipts_action()

    def write(self, vals):
        result = super().write(vals)
        if "partner_id" in vals:
            self._distri_app_receipts_take_customer()
        return result

    def _distri_app_receipts_take_customer(self):
        """An order that gets its customer after the photo: its open app
        receipts without a customer take it, as if written next to the photo
        (cobros.py and comprobante_pendiente.py match it then)."""
        for order in self:
            partner = app_partner(order.sudo())
            if not partner:
                continue
            photos = order._distri_app_receipts().filtered(
                lambda m: not m.distri_partner_id and m._distri_is_open())
            if photos:
                photos.write({"distri_partner_id": partner.id,
                              "distri_partner_source": "Pedido %s · foto del repartidor en "
                                                       "la app" % order.name})
