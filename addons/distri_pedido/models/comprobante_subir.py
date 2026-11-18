"""«Subir comprobante» (19.0.1.99.0, audit proposal 3a, steps 1 and 2).

Staff upload transfer receipts (photos or PDF, several at once) from the
phone or the depot PC, instead of posting them in the WhatsApp group that is
exported by hand once a day. Each file becomes the same record a receipt
received by WhatsApp becomes (`distri.whatsapp.message`, pending review in
Cobros › Comprobantes por WhatsApp and on the collections board):

- it is marked «Reenviado por» the user, the rule of a photo forwarded by a
  seller: its card always goes to «Para revisar» and no phone is learned;
- the customer, when the user picks one, replaces the note written next to
  the photo in the group («Elegido por X al subirlo»); without one the
  reading (CUIT, order number) proposes it, as today;
- the file is read (OCR / PDF text) in the background by the route photos
  cron, like the driver's photos: the page answers at once and the list
  «Subidos hoy por vos» fills the amount when the reading is done;
- a file already loaded by any channel (same bytes: the chat export, the
  driver's app, the webhook, another upload) is not created again and the
  user is told at once («Ya estaba cargado»).

«Compartir con Distri» (Web Share Target, Android): the shared files are
staged in `distri.receipt.share` under a random token and the upload page
opens with them; nothing is created until the user confirms. A share that
arrives with the session expired is staged the same way and claimed after
the login. Stages expire after STAGE_HOURS.

The receipt never creates a payment (docs cobranza/README.md, decision 10).
"""
import base64
import hashlib
import logging
import secrets
from datetime import datetime, time, timedelta, timezone

from psycopg2 import IntegrityError

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .reparto_cliente import AR_TZ, today_ar

_logger = logging.getLogger(__name__)

UPLOAD_GROUPS = (
    "distri_pedido.grupo_vendedor",
    "distri_pedido.grupo_administracion",
    "distri_pedido.grupo_gerencia",
    "distri_pedido.grupo_cajero",
)
UPLOAD_KEY = "upload:%s"
CHAT_KEY = "chat:%s"
UPLOAD_PROFILE = "Subido por el personal"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_FILES = 10
STAGE_HOURS = 24
# (magic, mime, message type, extension)
_KINDS = (
    (b"\xff\xd8\xff", "image/jpeg", "image", "jpg"),
    (b"\x89PNG\r\n\x1a\n", "image/png", "image", "png"),
    (b"%PDF-", "application/pdf", "document", "pdf"),
)


def receipt_kind(raw):
    """(mime, message type, extension) of a receipt file, by its bytes, or
    None. WEBP is checked apart (RIFF....WEBP). HEIC and anything else: None."""
    if not raw:
        return None
    for magic, mime, kind, ext in _KINDS:
        if raw.startswith(magic):
            return mime, kind, ext
    if raw[:4] == b"RIFF" and raw[8:12] == b"WEBP":
        return "image/webp", "image", "webp"
    return None


def file_key(raw):
    """Same digest as the chat import (puesta_al_dia.chat_file_key)."""
    return hashlib.sha256(raw).hexdigest()[:48]


def can_upload(user):
    return bool(user) and not user.share and any(user.has_group(g) for g in UPLOAD_GROUPS)


class WhatsappMessageUpload(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_upload = fields.Boolean(
        string="Subido desde «Subir comprobante»", readonly=True, index=True, copy=False,
        help="Lo subió el personal desde la pantalla «Subir comprobante» o con «Compartir con "
             "Distri».")

    # ------------------------------------------------------------ duplicates

    @api.model
    def _distri_upload_duplicate(self, raw):
        """The receipt already loaded with these same bytes, by any channel."""
        Message = self.sudo().with_context(active_test=False)
        key = file_key(raw)
        found = Message.search([("wa_message_id", "in", (UPLOAD_KEY % key, CHAT_KEY % key))],
                               limit=1)
        if found:
            return found
        checksum = hashlib.sha1(raw).hexdigest()
        attachment = self.env["ir.attachment"].sudo().search([
            ("checksum", "=", checksum), ("res_model", "=", self._name)], limit=1)
        return Message.browse(attachment.res_id).exists() if attachment else Message.browse()

    # ------------------------------------------------------------ upload

    @api.model
    def _distri_upload_receipts(self, files, partner=None, via="pantalla"):
        """Creates one pending receipt per new file.

        `files`: [(filename, raw bytes)]. `partner`: the customer the user
        picked (optional). Returns a list of {"name", "status", "message",
        "id"}: status «nuevo», «repetido» or «error» (a file that is not a
        photo or PDF, too big). Caller checks can_upload()."""
        user = self.env.user
        if not can_upload(user):
            raise AccessError("Tu usuario no sube comprobantes.")
        if not files:
            raise UserError("Elegí al menos una foto o un PDF del comprobante.")
        if len(files) > MAX_FILES:
            raise UserError("Subí hasta %d archivos por vez." % MAX_FILES)
        customer = partner.commercial_partner_id if partner else self.env["res.partner"]
        results = []
        created = self.browse()
        for name, raw in files:
            name = (name or "comprobante")[:120]
            kind = receipt_kind(raw)
            if len(raw or b"") > MAX_FILE_BYTES:
                results.append({"name": name, "status": "error", "id": False,
                                "message": "Es demasiado grande (más de 10 MB)."})
                continue
            if not kind:
                results.append({"name": name, "status": "error", "id": False,
                                "message": "No es una foto (JPG, PNG o WEBP) ni un PDF."})
                continue
            existing = self._distri_upload_duplicate(raw)
            if existing:
                results.append({"name": name, "status": "repetido", "id": existing.id,
                                "message": "Ya estaba cargado (%s)." % existing._distri_upload_label()})
                continue
            msg = self._distri_upload_create(name, raw, kind, user, customer, via)
            created |= msg
            results.append({"name": name, "status": "nuevo", "id": msg.id,
                            "message": "Subido. Lo revisa administración."})
        if created:
            # the file (OCR / PDF text) is read in the background
            self.env["distri.route.expense"]._trigger_read()
        return results

    @api.model
    def _distri_upload_create(self, name, raw, kind, user, customer, via):
        mime, message_type, ext = kind
        now = fields.Datetime.now()
        body = "Comprobante subido por %s (%s)" % (user.name, via)
        if customer:
            body += " · cliente %s" % customer.name
        vals = {
            "wa_message_id": UPLOAD_KEY % file_key(raw),
            "direction": "in",
            "message_type": message_type,
            "profile_name": UPLOAD_PROFILE,
            "body": body,
            "timestamp": now,
            "media_mime_type": mime,
            "media_filename": name if name.lower().endswith("." + ext) else "%s.%s" % (name, ext),
            "distri_forwarded_by_id": user.id,
            "distri_upload": True,
            "distri_partner_id": customer.id or False,
            "distri_partner_source": ("Elegido por %s al subirlo" % user.name) if customer else False,
            "distri_is_receipt": True,
            "distri_receipt_state": "pending",
        }
        Message = self.sudo().with_context(distri_defer_receipt_read=True)
        try:
            with self.env.cr.savepoint():
                msg = Message.create(vals)
        except IntegrityError:
            raise UserError("Ese comprobante se está subiendo en otra pestaña: probá de nuevo.")
        attachment = self.env["ir.attachment"].sudo().create({
            "name": vals["media_filename"], "raw": raw, "mimetype": mime,
            "res_model": msg._name, "res_id": msg.id,
        })
        msg = msg.with_context(distri_defer_receipt_read=False)
        msg.with_context(distri_receipt_matching=True).write({"attachment_id": attachment.id})
        # what a WhatsApp photo goes through before the file is read: copies,
        # the transfer of the report, the card on the board
        msg._distri_read_receipt()
        _logger.info("Receipt %s uploaded by %s (%s) as message %s", name, user.login, via, msg.id)
        return msg

    def _distri_upload_label(self):
        """«$ 82.200 · Kiosco Sol · Esperando el reporte» for the list."""
        self.ensure_one()
        msg = self.sudo()
        from .reparto import _money
        parts = []
        if msg.distri_amount:
            parts.append(_money(msg.distri_amount))
        parts.append(msg.distri_partner_id.name or "sin cliente")
        state = dict(self._fields["distri_receipt_state"].selection).get(msg.distri_receipt_state)
        if state:
            parts.append(state)
        return " · ".join(parts)

    @api.model
    def _distri_uploads_today(self, user=None):
        """The receipts the user uploaded today (Argentine day), newest first."""
        user = user or self.env.user
        start = datetime.combine(today_ar(), time()).replace(tzinfo=AR_TZ).astimezone(
            timezone.utc).replace(tzinfo=None)
        return self.sudo().search([
            ("distri_upload", "=", True), ("distri_forwarded_by_id", "=", user.id),
            ("create_date", ">=", start)], order="id desc", limit=50)

    def _distri_upload_row(self):
        self.ensure_one()
        msg = self.sudo()
        return {
            "id": msg.id,
            "amount": msg.distri_amount or 0.0,
            "partner": msg.distri_partner_id.name or "",
            "reading": not msg.media_state,
            "state": msg.distri_receipt_state or "",
            "state_label": dict(self._fields["distri_receipt_state"].selection).get(
                msg.distri_receipt_state, ""),
            "pdf": msg.media_mime_type == "application/pdf",
            "name": msg.media_filename or "",
        }

    @api.model
    def _distri_read_app_receipts(self, limit=30):
        """Also the uploaded receipts (same background reading)."""
        result = super()._distri_read_app_receipts(limit=limit)
        todo = self.sudo().search([("distri_upload", "=", True), ("media_state", "=", False),
                                   ("attachment_id", "!=", False)], order="id", limit=limit)
        for msg in todo:
            try:
                with self.env.cr.savepoint():
                    msg.write({"media_state": "done"})
            except Exception:
                _logger.exception("Uploaded receipt %s: reading failed", msg.id)
                msg.invalidate_recordset()
                msg.write({"media_state": "error",
                           "media_error": "No se pudo leer el archivo (ver el log del servidor)"})
        return result + len(todo)


class DistriReceiptShare(models.Model):
    """Files shared with «Compartir con Distri», waiting for the user to
    confirm them on the upload page."""
    _name = "distri.receipt.share"
    _description = "Comprobantes compartidos, por confirmar"
    _order = "id desc"

    token = fields.Char(required=True, index=True, readonly=True, copy=False)
    user_id = fields.Many2one("res.users", string="Usuario", readonly=True, index=True,
                              ondelete="cascade")

    _distri_token_unique = models.Constraint("UNIQUE(token)", "Ese envío ya existe.")

    @api.model
    def _distri_stage(self, files, user=None):
        """Keeps the shared files (only photos and PDF, capped) and returns
        the stage. `user` is False when the session had expired."""
        kept = []
        for name, raw in files[:MAX_FILES]:
            if raw and len(raw) <= MAX_FILE_BYTES and receipt_kind(raw):
                kept.append((name, raw))
        if not kept:
            return self.browse()
        stage = self.sudo().create({"token": secrets.token_urlsafe(24),
                                    "user_id": user.id if user else False})
        for name, raw in kept:
            mime = receipt_kind(raw)[0]
            self.env["ir.attachment"].sudo().create({
                "name": (name or "comprobante")[:120], "raw": raw, "mimetype": mime,
                "res_model": self._name, "res_id": stage.id})
        return stage

    @api.model
    def _distri_claim(self, token, user=None):
        """The stage of `token` for this user: his own, or one staged while
        logged out (it becomes his). Empty when unknown, expired or taken."""
        user = user or self.env.user
        if not token or not isinstance(token, str) or len(token) > 64:
            return self.browse()
        stage = self.sudo().search([("token", "=", token)], limit=1)
        if not stage or stage.create_date < fields.Datetime.now() - timedelta(hours=STAGE_HOURS):
            return self.browse()
        if stage.user_id and stage.user_id != user:
            return self.browse()
        if not stage.user_id:
            stage.user_id = user
        return stage

    def _distri_files(self):
        self.ensure_one()
        atts = self.env["ir.attachment"].sudo().search(
            [("res_model", "=", self._name), ("res_id", "=", self.id)], order="id")
        return [(a.name, a.raw) for a in atts]

    def _distri_preview(self):
        """[{index, name, pdf, data_url (photos only)}] for the page."""
        self.ensure_one()
        atts = self.env["ir.attachment"].sudo().search(
            [("res_model", "=", self._name), ("res_id", "=", self.id)], order="id")
        out = []
        for n, att in enumerate(atts):
            pdf = att.mimetype == "application/pdf"
            out.append({"index": n, "name": att.name, "pdf": pdf,
                        "src": "" if pdf else "data:%s;base64,%s" % (
                            att.mimetype, base64.b64encode(att.raw).decode())})
        return out

    def _distri_done(self):
        atts = self.env["ir.attachment"].sudo().search(
            [("res_model", "=", self._name), ("res_id", "in", self.ids)])
        atts.unlink()
        self.sudo().unlink()

    @api.autovacuum
    def _gc_expired(self):
        old = self.sudo().search([
            ("create_date", "<", fields.Datetime.now() - timedelta(hours=STAGE_HOURS))])
        old._distri_done()
