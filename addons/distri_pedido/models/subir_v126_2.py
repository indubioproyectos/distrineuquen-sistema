"""Cancelled uploads and the uploader's own pending receipt (19.0.1.126.2).

Found in practice with 19.0.1.126.1: a cashier uploaded a receipt with «No
está en el sistema», uploaded the same photo again meaning to pick the
customer and was refused («ya estaba cargado»); he cancelled the first one
and the refused attempt still told him it «ya estaba en el sistema».

- A cancelled upload is never a duplicate, on any path of
  `_distri_upload_duplicate` (models/comprobante_subir.py). `wa_message_id`
  is unique, so a cancelled upload frees its key (CANCEL_KEY), in
  «Cancelar» and lazily when the duplicate check finds a cancelled upload
  still holding it. No data migration: the lazy path covers old ones.
- The same bytes as a receipt the SAME user uploaded, not credited and
  pending assignment (no customer, or «No está en el sistema»), are not
  refused: the result («completar») offers «Ya subiste este comprobante y
  quedó sin acreditar. Completalo:» with that receipt (photo, amount read),
  the suggested customers, the customer picker and «Completar y acreditar»
  (/comprobantes/<id>/completar). Completing sets the customer on the
  EXISTING receipt (who sent it moves to the customer's source note: it is
  no longer an assignment) and credits it through `_distri_upload_credit_now`
  (`_distri_credit`: every defense, the > $ 100.000 warning). No new record.
- Someone else's receipt, or one already credited, keeps the refusal, now
  naming who uploaded it, when and how it is («Ya estaba cargado: lo subió
  Ana hoy, acreditado a Kiosco Sol»).
- A refused attempt whose receipt was cancelled afterwards says so («el que
  estaba se canceló: podés subirlo de nuevo»).

No new column.
"""
import logging
from datetime import timedelta, timezone

from odoo import models
from odoo.exceptions import UserError

from .acreditar import can_credit
from .esquema_v126 import v126_1_ready
from .ocr_comprobante import CONFIDENCE_HIGH
from .reparto import _money
from .reparto_cliente import AR_TZ, today_ar

_logger = logging.getLogger(__name__)

COMPLETE_OFFER = "Ya subiste este comprobante y quedó sin acreditar. Completalo:"
COMPLETE_GONE = ("Este comprobante ya no está pendiente de asignación: abrilo para ver cómo "
                 "quedó.")
COMPLETED_NO_CREDIT = ("Completado: quedó como comprobante de %s, pendiente (tu usuario no "
                       "acredita comprobantes; lo acredita administración).")
ATTEMPT_CANCELLED_FLAG = "No se cargó · el que estaba se canceló: podés subirlo de nuevo"
BUCKET_TEXT = {"rechazado": "rechazado", "cerrado": "cerrado", "cancelado": "cancelado"}


def _amount_text(amount):
    return ("%.2f" % amount).replace(".", ",") if amount else ""


class WhatsappMessageOwnPending(models.Model):
    _inherit = "distri.whatsapp.message"

    # ------------------------------------------------------------ the refusal

    def _distri_duplicate_message(self, user):
        """«Ya estaba cargado: lo subió Ana hoy, acreditado a Kiosco Sol ($ 82.200)»."""
        self.ensure_one()
        msg = self.sudo()
        if msg.distri_upload:
            uploader = msg.distri_forwarded_by_id
            who = "lo subiste vos" if uploader == user else "lo subió %s" % (
                uploader.name or "el personal")
        else:
            who = "llegó %s" % {
                "App del repartidor": "por la app del repartidor",
                "Exportación del chat de WhatsApp": "por la exportación del chat de WhatsApp",
            }.get(msg._distri_upload_channel(user), "por WhatsApp")
        arrived = msg._distri_arrived_at()
        day = arrived.replace(tzinfo=timezone.utc).astimezone(AR_TZ).date() if arrived else None
        when = "hoy" if day == today_ar() else "ayer" if day == today_ar() - timedelta(days=1) \
            else "el %s" % day.strftime("%d/%m/%Y") if day else ""
        amount = msg.distri_amount or msg.distri_amount_typed
        money = " (%s)" % _money(amount) if amount else ""
        partner = msg.distri_partner_id.name
        bucket = msg._distri_upload_bucket()
        if msg._distri_upload_credit_state()[1]:
            state = "acreditado a %s%s" % (partner or "un cliente", money)
        elif bucket == "aprobado":
            state = "aprobado%s%s" % ((" a %s" % partner) if partner else "", money)
        elif bucket == "pendiente":
            state = "pendiente%s%s" % (
                (", de %s" % partner) if partner else " de asignación", money)
        else:
            state = "%s%s" % (msg._distri_upload_status_label().lower()
                              or BUCKET_TEXT.get(bucket, bucket), money)
        return "Ya estaba cargado: %s, %s." % (" ".join(p for p in (who, when) if p), state)

    # ------------------------------------------------------------ own pending receipt

    def _distri_upload_completable(self, user):
        """The receipt `user` uploaded, not credited and pending assignment
        (no customer, or «No está en el sistema»), that he may still change
        (`_distri_upload_manage_problem`): the same bytes complete it."""
        self.ensure_one()
        msg = self.sudo()
        if not v126_1_ready(self.env) or not msg.distri_upload \
                or msg.distri_forwarded_by_id != user or msg.distri_receipt_state != "pending":
            return False
        if msg.distri_partner_id and not msg._distri_sender_note_text():
            return False
        if msg._distri_upload_credit_state()[1]:
            return False
        return not msg._distri_upload_manage_problem(user)

    def _distri_upload_completion(self, name, partner=None):
        """The «completar» result of the upload: the existing receipt, the
        suggestions and where to complete it. `partner`: the customer the
        user chose this time (pre-filled, never credited by itself)."""
        self.ensure_one()
        msg = self.sudo()
        partner = partner or self.env["res.partner"]
        amount = msg._distri_upload_complete_amount()
        return {
            "name": name, "status": "completar", "id": msg.id, "credit": "",
            "message": COMPLETE_OFFER,
            "url": "/comprobantes/%d/completar" % msg.id,
            "file_url": "/comprobantes/%d/archivo" % msg.id if msg.attachment_id else "",
            "image": (msg.media_mime_type or "").startswith("image/") and bool(msg.attachment_id),
            "amount": amount, "amount_text": _amount_text(amount),
            "suggestions": msg._distri_stored_suggestions(),
            "sender_note": msg._distri_sender_note_text(),
            "partner_id": partner.id or "", "partner_name": partner.name or "",
        }

    def _distri_upload_complete_amount(self):
        """The amount offered: the one read with confidence, else typed, else read."""
        msg = self.sudo()
        read = msg.distri_read_amount if msg.distri_amount_confidence == CONFIDENCE_HIGH else 0.0
        return msg.distri_amount_typed or read or msg.distri_amount or 0.0

    def _distri_upload_complete_form(self, user, partner=None):
        """What the «Completar y acreditar» form of «Ver comprobante» shows, or None."""
        self.ensure_one()
        msg = self.sudo()
        if not msg._distri_upload_completable(user):
            return None
        partner = partner or self.env["res.partner"]
        amount = msg._distri_upload_complete_amount()
        return {
            "message": COMPLETE_OFFER,
            "amount_text": _amount_text(amount),
            "read_amount": msg.distri_read_amount or 0.0,
            "typed_amount": msg.distri_amount_typed or 0.0,
            "suggestions": msg._distri_stored_suggestions(),
            "partner_id": partner.id or "",
            "partner_name": partner.name or "",
            "credit": can_credit(user),
        }

    def _distri_upload_complete(self, partner, amount=None, confirmed=False):
        """«Completar y acreditar»: `partner` becomes the customer of this
        existing receipt and it is credited like a new upload with the
        customer (`_distri_upload_credit_now`). Returns the same dict
        ({credit, message, warnings?, amount_text?, partner_id?})."""
        self.ensure_one()
        user = self.env.user
        msg = self.sudo()
        if not msg._distri_upload_completable(user):
            raise UserError(COMPLETE_GONE)
        partner = partner.commercial_partner_id if partner else self.env["res.partner"]
        if not partner:
            raise UserError("Elegí el cliente.")
        if partner.distri_generic_partner:
            raise UserError("Elegí un cliente con cuenta corriente: el genérico no lleva saldo.")
        note = msg._distri_sender_note_text()
        source = "Elegido por %s al completarlo" % user.name
        if note:
            source += " (lo mandó: %s)" % note
        vals = {"distri_partner_id": partner.id, "distri_partner_source": source[:250]}
        if note:
            # no longer an assignment: the uploader's own credit rules apply
            vals["distri_sender_note"] = False
        # not a matching write: classified and its card follows the customer
        msg.write(vals)
        _logger.info("Uploaded receipt %s completed by %s with partner %s", msg.id, user.login,
                     partner.id)
        if not can_credit(user):
            return {"credit": "pendiente", "message": COMPLETED_NO_CREDIT % partner.name}
        return self._distri_upload_credit_now(partner, amount=amount or None,
                                              confirmed=confirmed)

    # ------------------------------------------------------------ rows

    def _distri_upload_row(self):
        row = super()._distri_upload_row()
        if self._distri_upload_completable(self.env.user):
            row["complete_url"] = "/comprobantes/%d/completar" % self.id
        return row


class DistriReceiptUploadAttemptCancelled(models.Model):
    _inherit = "distri.receipt.upload.attempt"

    def _distri_row(self, user=None):
        row = super()._distri_row(user=user)
        if row.get("bucket") == "cancelado":
            row.update(flag=ATTEMPT_CANCELLED_FLAG, cancelled=True)
        return row

