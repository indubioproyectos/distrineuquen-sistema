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

Amount typed by the uploader (19.0.1.100.0): each file may carry an
«Importe» typed before «Subir» (Argentine format, the /cobro rules of
`_cash_amount`). It is kept apart in `distri_amount_typed`, which nothing
read later writes. When the file is read (`_distri_file_values`):

- no amount read: the typed one becomes the receipt amount («Revisar
  importe»: only one source, a person confirms it looking at the photo);
- the same amount (AMOUNT_TOLERANCE): the receipt amount, «Leído»;
- a different amount: the read one stays as the receipt amount, never
  silently replaced, and the receipt is flagged for administración
  («Cargado: $ X · leído: $ Y» on the receipt list/form and on the card of
  the collections board) until somebody types the amount («Cargado a mano»).

An amount typed by administración on the receipt («Cargado a mano») still
wins over both. «Subidos hoy por vos» shows the typed amount at once, before
the reading is done.

Upright photos (19.0.1.100.0): a phone camera may hand over a landscape
photo without any EXIF orientation. The stored photo gets the EXIF
orientation applied when it has one, then the turn the user chose with
«Girar» on the page (90° steps, clockwise), before it is read. The duplicate
check and `wa_message_id` keep using the ORIGINAL bytes, so the same file
uploaded again is still «Ya estaba cargado». Administración can turn a
stored photo from the receipt form (and the card of the board); the file is
read again after it. Whenever the stored photo is turned, the sha1 of the
file as it arrived (the ir.attachment checksum of those bytes) is kept in
`distri_original_checksum`, the first one only: the checksum comparisons
(the upload duplicate check, the chat viewer in models/visor_chat.py) match
either checksum, so a turned photo still pairs with the same file arriving
by another channel.

«Ver comprobante» (19.0.1.100.0): every uploaded receipt opens in the kit
(/comprobantes/<id>) for whoever uploaded it, and for administración and
gerencia; «Mis comprobantes» lists the uploader's last LIST_DAYS days by
state (`_distri_upload_bucket`). Read only: nothing is approved or deleted
from there.

Refused duplicates (19.0.1.101.0): every «Ya estaba cargado» is kept as a
`distri.receipt.upload.attempt` (who, when, the file name, the channel, the
typed amount and the receipt it matched; not the file again: the receipt
already has it). «Subidos hoy por vos» and «Mis comprobantes» list them as
their own rows («No se cargó · ya estaba en el sistema», when and how the
existing receipt arrived, its state) and the row opens the existing receipt
read only for the user who tried (/comprobantes/intento/<id>): only the
file, amount, customer, date, channel and state, never the notes of
administración. Administración sees them in Cobros › Intentos repetidos and
as «Intentaron subirlo de nuevo: N» on the receipt form. The JSON keeps
«uploads» as before and adds «attempts», drawn by comprobantes.js only on a
data-comp-api="101" page.

The receipt never creates a payment (docs cobranza/README.md, decision 10).
"""
import base64
import hashlib
import io
import logging
import secrets
from datetime import datetime, time, timedelta, timezone

from PIL import Image, ImageOps
from psycopg2 import IntegrityError

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .cobros import AMOUNT_TOLERANCE, CONFIDENCE_HIGH, CONFIDENCE_LOW
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


# see any uploaded receipt (not only their own) in «Ver comprobante»
SEE_ALL_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")
# turn a stored photo from the back office
ROTATE_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_cobros",
                 "base.group_system")
LIST_DAYS = 30
ROTATIONS = (0, 90, 180, 270)
# clockwise degrees -> PIL transpose (PIL's ROTATE_90 is counter-clockwise)
_TURN = {90: Image.Transpose.ROTATE_270, 180: Image.Transpose.ROTATE_180,
         270: Image.Transpose.ROTATE_90}
_PIL_FORMAT = {"image/jpeg": "JPEG", "image/png": "PNG", "image/webp": "WEBP"}
EXIF_ORIENTATION = 0x0112
# what the uploader sees of the receipt's state (`_distri_upload_bucket`)
BUCKETS = {
    "pendiente": "Pendiente",
    "aprobado": "Aprobado",
    "rechazado": "Rechazado",
    "cerrado": "Cerrado",
}
# «Mis comprobantes» filter of the refused duplicates (19.0.1.101.0)
ATTEMPT_FILTER = "repetido"
ATTEMPT_FLAG = "No se cargó · ya estaba en el sistema"
VIAS = [("pantalla", "Pantalla «Subir comprobante»"), ("compartido", "Compartir con Distri"),
        # 19.0.1.103.0: the customer, from his order page
        ("cliente", "Página del pedido (cliente)")]


def _ar(dt, fmt):
    """A stored UTC datetime as Argentine text."""
    return dt.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime(fmt) if dt else ""


def _since(days):
    """UTC start of the Argentine day `days - 1` days ago (1: today)."""
    return datetime.combine(today_ar() - timedelta(days=days - 1), time()).replace(
        tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def merge_rows(*lists):
    """Rows of uploads and attempts, newest first (their «ts»)."""
    return sorted((row for rows in lists for row in rows), key=lambda r: r["ts"], reverse=True)


def rotation(value):
    """A «Girar» value of the page: 0, 90, 180 or 270; anything else 0."""
    try:
        degrees = int(str(value if value is not None else "").strip() or 0)
    except (TypeError, ValueError):
        return 0
    return degrees if degrees in ROTATIONS else 0


def upright_image(raw, mime, degrees=0):
    """The photo with its EXIF orientation applied (when it has one) and
    then turned `degrees` clockwise. The same bytes when there is nothing
    to do, for a PDF, or when the photo cannot be decoded."""
    fmt = _PIL_FORMAT.get(mime or "")
    if not raw or not fmt:
        return raw
    try:
        image = Image.open(io.BytesIO(raw))
        orientation = image.getexif().get(EXIF_ORIENTATION, 1)
        if orientation in (0, 1) and not degrees:
            return raw
        image = ImageOps.exif_transpose(image)
        if degrees in _TURN:
            image = image.transpose(_TURN[degrees])
        options = {}
        if fmt == "JPEG":
            if image.mode not in ("RGB", "L"):
                image = image.convert("RGB")
            options = {"quality": 90}
        out = io.BytesIO()
        image.save(out, format=fmt, **options)
        return out.getvalue()
    except Exception:
        _logger.warning("Receipt photo could not be turned upright", exc_info=True)
        return raw


class WhatsappMessageUpload(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_upload = fields.Boolean(
        string="Subido desde «Subir comprobante»", readonly=True, index=True, copy=False,
        help="Lo subió el personal desde la pantalla «Subir comprobante» o con «Compartir con "
             "Distri».")
    distri_amount_typed = fields.Float(
        string="Importe cargado por quien lo subió", digits=(16, 2), readonly=True, copy=False,
        help="El importe que escribió quien subió el comprobante. Si la lectura no trae importe, "
             "es el importe del comprobante; si trae otro, queda el leído y el comprobante se "
             "marca para revisar.")
    distri_amount_typed_differs = fields.Boolean(
        string="Importe cargado distinto del leído", compute="_compute_distri_amount_typed_differs",
        store=True, help="Quien lo subió cargó un importe y la lectura del comprobante dice otro: "
                         "confirmá el importe mirando la foto.")
    distri_amount_typed_note = fields.Char(
        string="Importe cargado y leído", compute="_compute_distri_amount_typed_differs")
    distri_upload_attempt_count = fields.Integer(
        string="Intentaron subirlo de nuevo", compute="_compute_distri_upload_attempt_count",
        help="Veces que alguien quiso subir este mismo archivo desde «Subir comprobante» y no "
             "se cargó porque ya estaba.")
    distri_original_checksum = fields.Char(
        string="Checksum del archivo original", readonly=True, index=True, copy=False,
        help="SHA1 del archivo tal como llegó, antes de girarlo: el mismo archivo que llegue "
             "por otro medio se reconoce igual.")

    def _compute_distri_upload_attempt_count(self):
        counts = {}
        if self.ids:
            groups = self.env["distri.receipt.upload.attempt"].sudo()._read_group(
                [("message_id", "in", self.ids)], ["message_id"], ["__count"])
            counts = {message.id: count for message, count in groups}
        for msg in self:
            msg.distri_upload_attempt_count = counts.get(msg.id, 0)

    def action_distri_view_upload_attempts(self):
        """«Ver» next to «Intentaron subirlo de nuevo» on the receipt form."""
        self.ensure_one()
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_receipt_upload_attempts")
        action["domain"] = [("message_id", "=", self.id)]
        action["context"] = {}
        return action

    @api.depends("distri_amount_typed", "distri_read_amount", "distri_amount_confidence")
    def _compute_distri_amount_typed_differs(self):
        from .reparto import _money
        for msg in self:
            typed, read = msg.distri_amount_typed, msg.distri_read_amount
            differs = bool(typed and read and abs(typed - read) > AMOUNT_TOLERANCE)
            # settled once a person typed the amount on the receipt
            msg.distri_amount_typed_differs = differs and msg.distri_amount_confidence != "manual"
            msg.distri_amount_typed_note = (
                "Cargado: %s · leído: %s" % (_money(typed), _money(read)) if differs else False)

    # ------------------------------------------------------------ amounts

    @api.model
    def _distri_typed_amount(self, text, name):
        """The amount typed for file `name`: 0.0 when left empty; a polite
        UserError when it cannot be understood."""
        text = str(text if text is not None else "").strip()
        if not text:
            return 0.0
        if len(text) > 20:
            raise UserError("Revisá el importe de «%s»: es demasiado largo." % name)
        try:
            return self.env["distri.cash.collection"]._cash_amount(text)
        except UserError as exc:
            raise UserError("Revisá el importe de «%s»: %s" % (
                name, exc.args[0] if exc.args else "no se entiende.")) from None

    def _distri_file_values(self, reading, caption_amount):
        """An uploaded receipt with a typed amount: the typed amount fills a
        reading without amount, and a reading that differs keeps the read
        amount and asks a person (see the module docstring). The typed
        field itself is never written here."""
        vals = super()._distri_file_values(reading, caption_amount)
        typed = self.distri_amount_typed
        if not typed or self.distri_amount_confidence == "manual":
            return vals
        read = reading.get("amount") or 0.0
        if read and abs(read - typed) <= AMOUNT_TOLERANCE:
            sure = reading.get("amount_confidence") == CONFIDENCE_HIGH
            vals.update(distri_amount=read if sure else typed,
                        distri_amount_confidence=CONFIDENCE_HIGH)
        elif read:
            vals.update(distri_amount=read, distri_amount_confidence=CONFIDENCE_LOW)
        else:
            vals.update(distri_amount=typed, distri_amount_confidence=CONFIDENCE_LOW)
        return vals

    def _distri_use_typed_amount(self):
        """The file could not be read: the typed amount is the receipt
        amount, to review («Revisar importe»)."""
        for msg in self.sudo():
            if not msg.distri_amount_typed or msg.distri_amount \
                    or msg.distri_amount_confidence == "manual":
                continue
            msg.with_context(distri_receipt_matching=True).write({
                "distri_amount": msg.distri_amount_typed,
                "distri_amount_confidence": CONFIDENCE_LOW})
            if msg.distri_receipt_state in ("pending", "no_money"):
                msg._distri_classify()
                msg._distri_match_income()
            msg._distri_sync_verify_card()

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
        found = Message.browse(attachment.res_id).exists() if attachment else Message.browse()
        # a photo turned after it arrived keeps the checksum of the original
        return found or Message.search([("distri_original_checksum", "=", checksum)], limit=1)

    # ------------------------------------------------------------ upload

    @api.model
    def _distri_upload_receipts(self, files, partner=None, via="pantalla", amounts=None,
                                rotations=None):
        """Creates one pending receipt per new file.

        `files`: [(filename, raw bytes)]. `partner`: the customer the user
        picked (optional). `amounts`: the «Importe» typed for each file, in
        the same order (text, optional; a missing or empty one is no
        amount). An amount that cannot be understood refuses the whole
        upload before anything is created. `rotations`: the «Girar» of each
        photo, same order (degrees clockwise; anything else is 0). Returns a list of {"name",
        "status", "message", "id"}: status «nuevo», «repetido» or «error»
        (a file that is not a photo or PDF, too big). Caller checks
        can_upload()."""
        user = self.env.user
        if not can_upload(user):
            raise AccessError("Tu usuario no sube comprobantes.")
        if not files:
            raise UserError("Elegí al menos una foto o un PDF del comprobante.")
        if len(files) > MAX_FILES:
            raise UserError("Subí hasta %d archivos por vez." % MAX_FILES)
        customer = partner.commercial_partner_id if partner else self.env["res.partner"]
        amounts = list(amounts or [])
        typed_amounts = [
            self._distri_typed_amount(amounts[n] if n < len(amounts) else "",
                                      (name or "comprobante")[:120])
            for n, (name, _raw) in enumerate(files)]
        results = []
        created = self.browse()
        rotations = list(rotations or [])
        for n, ((name, raw), typed) in enumerate(zip(files, typed_amounts)):
            name = (name or "comprobante")[:120]
            degrees = rotation(rotations[n] if n < len(rotations) else 0)
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
                # 19.0.1.101.0: kept so the user can review it later
                attempt = self.env["distri.receipt.upload.attempt"].sudo().create({
                    "user_id": user.id, "file_name": name, "message_id": existing.id,
                    "via": via if via in dict(VIAS) else "pantalla",
                    "amount_typed": typed or 0.0})
                results.append({"name": name, "status": "repetido", "id": existing.id,
                                "attempt_id": attempt.id,
                                "message": "Ya estaba cargado (%s)." % existing._distri_upload_label()})
                continue
            msg = self._distri_upload_create(name, raw, kind, user, customer, via, typed,
                                             degrees)
            created |= msg
            results.append({"name": name, "status": "nuevo", "id": msg.id,
                            "message": "Subido. Lo revisa administración."})
        if created:
            # the file (OCR / PDF text) is read in the background
            self.env["distri.route.expense"]._trigger_read()
        return results

    @api.model
    def _distri_upload_create(self, name, raw, kind, user, customer, via, typed=0.0,
                              degrees=0, extra=None):
        """`extra` (19.0.1.103.0): values that replace the staff ones (the
        customer's upload from his order page, models/comprobante_cliente.py)."""
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
            # apart from distri_amount until the file is read (_distri_file_values)
            "distri_amount_typed": typed or 0.0,
        }
        vals.update(extra or {})
        # stored upright; the key above stays the one of the original bytes,
        # and so does the checksum the other channels compare
        stored = upright_image(raw, mime, degrees)
        if stored != raw:
            vals["distri_original_checksum"] = hashlib.sha1(raw).hexdigest()
        Message = self.sudo().with_context(distri_defer_receipt_read=True)
        try:
            with self.env.cr.savepoint():
                msg = Message.create(vals)
        except IntegrityError:
            raise UserError("Ese comprobante se está subiendo en otra pestaña: probá de nuevo.")
        attachment = self.env["ir.attachment"].sudo().create({
            "name": vals["media_filename"], "raw": stored,
            "mimetype": mime, "res_model": msg._name, "res_id": msg.id,
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
        if msg.distri_amount or msg.distri_amount_typed:
            parts.append(_money(msg.distri_amount or msg.distri_amount_typed))
        parts.append(msg.distri_partner_id.name or "sin cliente")
        state = dict(self._fields["distri_receipt_state"].selection).get(msg.distri_receipt_state)
        if state:
            parts.append(state)
        return " · ".join(parts)

    @api.model
    def _distri_uploads_today(self, user=None):
        """The receipts the user uploaded today (Argentine day), newest first."""
        user = user or self.env.user
        return self.sudo().search([
            ("distri_upload", "=", True), ("distri_forwarded_by_id", "=", user.id),
            ("create_date", ">=", _since(1))], order="id desc", limit=50)

    def _distri_upload_row(self):
        self.ensure_one()
        msg = self.sudo()
        return {
            "id": msg.id,
            "amount": msg.distri_amount or 0.0,
            # 19.0.1.100.0: shown at once, before the reading is done
            "amount_typed": msg.distri_amount_typed or 0.0,
            "amount_differs": bool(msg.distri_amount_typed_note),
            "partner": msg.distri_partner_id.name or "",
            "reading": not msg.media_state,
            "state": msg.distri_receipt_state or "",
            "state_label": dict(self._fields["distri_receipt_state"].selection).get(
                msg.distri_receipt_state, ""),
            "pdf": msg.media_mime_type == "application/pdf",
            "name": msg.media_filename or "",
            # 19.0.1.100.0: the row opens «Ver comprobante»
            "url": "/comprobantes/%d" % msg.id,
            "date": _ar(msg.create_date, "%d/%m %H:%M"),
            # 19.0.1.101.0: merged with the refused attempts (merge_rows)
            "ts": msg.create_date.isoformat() if msg.create_date else "",
            "bucket": msg._distri_upload_bucket(),
            "bucket_label": BUCKETS[msg._distri_upload_bucket()],
        }

    # ------------------------------------------------------------ «Ver comprobante»

    def _distri_upload_bucket(self):
        """«pendiente», «aprobado», «rechazado» or «cerrado» (already
        received, a refund) for the uploader."""
        self.ensure_one()
        msg = self.sudo()
        card = msg.distri_collection_id
        state = msg.distri_receipt_state
        if state in ("duplicate", "outgoing"):
            # its card is discarded with it: not a rejection for the uploader
            return "cerrado"
        if state == "dismissed" or card.state == "ignored":
            return "rechazado"
        if state == "registered" or card.state == "confirmed":
            return "aprobado"
        return "pendiente"

    def _distri_upload_visible_to(self, user):
        """An uploaded receipt is seen by whoever uploaded it, and by
        administración and gerencia."""
        self.ensure_one()
        msg = self.sudo()
        if not msg.distri_upload or not can_upload(user):
            return False
        return msg.distri_forwarded_by_id == user or any(user.has_group(g) for g in SEE_ALL_GROUPS)

    def _distri_upload_detail(self):
        """What «Ver comprobante» shows (read only)."""
        self.ensure_one()
        msg = self.sudo()
        row = msg._distri_upload_row()
        state = msg.distri_receipt_state
        row.update({
            "image": (msg.media_mime_type or "").startswith("image/") and bool(msg.attachment_id),
            "file_url": "/comprobantes/%d/archivo" % msg.id if msg.attachment_id else "",
            "note": msg.distri_receipt_note or "",
            "typed_note": msg.distri_amount_typed_note or "",
            "uploader": msg.distri_forwarded_by_id.name or "",
            "when": _ar(msg.create_date, "%d/%m/%Y %H:%M"),
            "dismissed": state == "dismissed",
        })
        return row

    @api.model
    def _distri_uploads_recent(self, user=None, bucket=None, days=LIST_DAYS):
        """«Mis comprobantes»: the user's uploads of the last `days` days,
        newest first, optionally of one bucket."""
        user = user or self.env.user
        found = self.sudo().search([
            ("distri_upload", "=", True), ("distri_forwarded_by_id", "=", user.id),
            ("create_date", ">=", _since(days))], order="id desc", limit=300)
        if bucket in BUCKETS:
            found = found.filtered(lambda m: m._distri_upload_bucket() == bucket)
        return found

    def _distri_upload_channel(self, user=None):
        """How the receipt arrived, for the user who tried to upload it again:
        «Por WhatsApp», «Exportación del chat de WhatsApp», «App del
        repartidor», «Subido por X» («Subido por vos»)."""
        self.ensure_one()
        msg = self.sudo()
        if msg.distri_upload:
            who = msg.distri_forwarded_by_id
            if user and who == user:
                return "Subido por vos"
            return "Subido por %s" % (who.name or "el personal")
        if msg.distri_from_app:
            return "App del repartidor"
        if (msg.wa_message_id or "").startswith(CHAT_KEY % ""):
            return "Exportación del chat de WhatsApp"
        return "Por WhatsApp"

    def _distri_arrived_at(self):
        """When the receipt arrived (the WhatsApp time when there is one)."""
        self.ensure_one()
        msg = self.sudo()
        return msg.timestamp or msg.create_date

    # ------------------------------------------------------------ turn a stored photo

    def _distri_rotate_photo(self, degrees):
        """Administración turns the stored photo (clockwise) and the file is
        read again."""
        user = self.env.user
        if not any(user.has_group(g) for g in ROTATE_GROUPS):
            raise AccessError("Solo administración gira los comprobantes.")
        for msg in self.sudo():
            attachment = msg.attachment_id
            if not attachment or (attachment.mimetype or "") not in _PIL_FORMAT:
                raise UserError("Solo se pueden girar las fotos (JPG, PNG o WEBP).")
            turned = upright_image(attachment.raw, attachment.mimetype, degrees)
            if turned == attachment.raw:
                raise UserError("No se pudo girar la foto.")
            # the first original wins: a second turn keeps it
            original = msg.distri_original_checksum or attachment.checksum \
                or hashlib.sha1(attachment.raw).hexdigest()
            attachment.write({"raw": turned})
            _logger.info("Receipt %s turned %s° by %s", msg.id, degrees, user.login)
            # «done» reads the file again (write())
            msg.with_context(distri_receipt_matching=True).write(
                {"media_state": "done", "media_error": False,
                 "distri_original_checksum": original})
        # the form keeps the same photo URL: a reload shows the turned one
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_distri_rotate_right(self):
        """«Girar a la derecha» on the receipt form."""
        return self._distri_rotate_photo(90)

    def action_distri_rotate_left(self):
        """«Girar a la izquierda» on the receipt form."""
        return self._distri_rotate_photo(270)

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
                msg._distri_use_typed_amount()
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


class DistriReceiptUploadAttempt(models.Model):
    """A file refused at «Subir comprobante» because it was already loaded
    (19.0.1.101.0). The file itself is not kept again: `message_id` has it."""
    _name = "distri.receipt.upload.attempt"
    _description = "Intento de subir un comprobante que ya estaba"
    _order = "id desc"
    _rec_name = "file_name"

    user_id = fields.Many2one("res.users", string="Quién lo intentó", required=True,
                              readonly=True, index=True, ondelete="cascade")
    attempted_at = fields.Datetime(string="Cuándo", required=True, readonly=True, index=True,
                                   default=fields.Datetime.now)
    file_name = fields.Char(string="Archivo", readonly=True)
    via = fields.Selection(VIAS, string="Desde", required=True, readonly=True,
                           default="pantalla")
    message_id = fields.Many2one("distri.whatsapp.message", string="Comprobante que ya estaba",
                                 required=True, readonly=True, index=True, ondelete="cascade")
    amount_typed = fields.Float(string="Importe que cargó", digits=(16, 2), readonly=True)
    message_timestamp = fields.Datetime(related="message_id.timestamp", string="Había llegado")
    partner_id = fields.Many2one(related="message_id.distri_partner_id", string="Cliente")
    receipt_state = fields.Selection(related="message_id.distri_receipt_state",
                                     string="Estado del comprobante")
    channel = fields.Char(string="Cómo había llegado", compute="_compute_channel")

    def _compute_channel(self):
        for attempt in self:
            attempt.channel = attempt.message_id._distri_upload_channel() \
                if attempt.message_id else ""

    @api.model
    def _distri_for(self, user, days):
        """The user's attempts of the last `days` Argentine days, newest first."""
        return self.sudo().search([("user_id", "=", user.id),
                                   ("attempted_at", ">=", _since(days))],
                                  order="id desc", limit=300)

    def _distri_visible_to(self, user):
        """Only the user who tried sees an attempt (and through it the
        existing receipt, with the few facts of `_distri_detail`)."""
        self.ensure_one()
        return can_upload(user) and self.sudo().user_id == user and bool(self.sudo().message_id)

    def _distri_row(self, user=None):
        """A row of «Subidos hoy por vos» / «Mis comprobantes»."""
        self.ensure_one()
        attempt = self.sudo()
        msg = attempt.message_id
        bucket = msg._distri_upload_bucket()
        return {
            "attempt": True,
            "id": attempt.id,
            "url": "/comprobantes/intento/%d" % attempt.id,
            "name": attempt.file_name or "",
            "flag": ATTEMPT_FLAG,
            "amount": msg.distri_amount or msg.distri_amount_typed or 0.0,
            "amount_typed": attempt.amount_typed or 0.0,
            "partner": msg.distri_partner_id.name or "",
            "date": _ar(attempt.attempted_at, "%d/%m %H:%M"),
            "ts": attempt.attempted_at.isoformat() if attempt.attempted_at else "",
            "existing_when": _ar(msg._distri_arrived_at(), "%d/%m/%Y"),
            "existing_channel": msg._distri_upload_channel(user or attempt.user_id),
            "bucket": bucket,
            "bucket_label": BUCKETS[bucket],
        }

    def _distri_detail(self):
        """The attempt page: the existing receipt, only what the user needs
        to recognise it (file, amount, customer, date, channel, state)."""
        self.ensure_one()
        attempt = self.sudo()
        msg = attempt.message_id
        row = attempt._distri_row()
        row.update({
            "image": (msg.media_mime_type or "").startswith("image/") and bool(msg.attachment_id),
            "file_url": "/comprobantes/intento/%d/archivo" % attempt.id
            if msg.attachment_id else "",
            "existing_when": _ar(msg._distri_arrived_at(), "%d/%m/%Y %H:%M"),
            "when": _ar(attempt.attempted_at, "%d/%m/%Y %H:%M"),
            "via_label": dict(VIAS).get(attempt.via, ""),
        })
        return row


class DistriMpCollectionUpload(models.Model):
    """The card of the collections board: the typed-vs-read amount note and
    turning the photo (19.0.1.100.0)."""
    _inherit = "distri.mp.collection"

    receipt_amount_typed_note = fields.Char(
        related="receipt_id.distri_amount_typed_note", string="Importe cargado y leído")

    def action_receipt_rotate_right(self):
        self.ensure_one()
        return self.receipt_id._distri_rotate_photo(90)

    def action_receipt_rotate_left(self):
        self.ensure_one()
        return self.receipt_id._distri_rotate_photo(270)
