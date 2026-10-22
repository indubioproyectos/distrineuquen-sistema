"""WhatsApp Cloud API inbound webhook (Meta), own integration.

Meta signs every POST with the app secret (X-Hub-Signature-256); the
controller checks that signature before calling `_wa_store_webhook`, so the
payload handled here is already authentic. Nothing is sent from here yet.

Images and documents (payment receipts) are NOT downloaded inside the webhook
request: Meta wants the 200 in under 5 s. The message is stored with the
media id as `pending` and a cron fetches the file later from the Graph API
(first the media id -> short-lived URL, then the file with the same token).
"""
import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests
from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_VERIFY_TOKEN = "distri_pedido.wa_verify_token"
PARAM_APP_SECRET = "distri_pedido.wa_app_secret"
PARAM_ACCESS_TOKEN = "distri_pedido.wa_access_token"
# Reserved, not read today (19.0.1.37.0, T9): the number and the business
# account of the Cloud API. Odoo only receives; they are needed the day Odoo
# sends messages or checks which number a notification is for (the real
# number, cobranza/README.md «Día de conexión del número real»).
PARAM_PHONE_NUMBER_ID = "distri_pedido.wa_phone_number_id"
PARAM_WABA_ID = "distri_pedido.wa_waba_id"

# Delivery statuses in the order they happen; a late retry of an older
# status must not move a message backwards (read -> delivered).
STATUS_RANK = {"sent": 1, "delivered": 2, "read": 3, "failed": 4}

# Fixed API host: the media id from the payload only goes into the path, and
# only after checking it is numeric (SSRF).
WA_GRAPH_API = "https://graph.facebook.com/v25.0"
WA_TIMEOUT = 10
MEDIA_TYPES = ("image", "document")
# Receipts only: photos and PDFs. Extension used for the attachment name.
MEDIA_ALLOWED = {
    "image/jpeg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "application/pdf": "pdf",
}
MEDIA_MAX_BYTES = 20 * 1024 * 1024
MEDIA_MAX_ATTEMPTS = 5
MEDIA_CRON_BATCH = 20
# The short-lived download URL comes from the Graph API answer; the token is
# only ever sent to Meta's own hosts, never to a URL taken from elsewhere.
MEDIA_URL_HOSTS = (".fbsbx.com", ".facebook.com", ".whatsapp.net", ".fbcdn.net")


class WhatsappMediaError(Exception):
    """Failure downloading a media file. Message never contains the token.

    `final` means retrying cannot help (unknown id, wrong type, too big...)."""

    def __init__(self, message, final=False):
        super().__init__(message)
        self.final = final


def _parse_wa_timestamp(value):
    """Meta sends unix seconds as a string; Odoo stores naive UTC."""
    try:
        seconds = int(value)
    except (TypeError, ValueError):
        return False
    return datetime.fromtimestamp(seconds, timezone.utc).replace(tzinfo=None)


def _text(value, limit):
    return str(value or "")[:limit]


def _mime(value):
    """'image/jpeg; charset=x' -> 'image/jpeg'"""
    return str(value or "").split(";", 1)[0].strip().lower()[:128]


def _magic_matches(mime, data):
    """The first bytes must agree with the declared type."""
    if mime == "image/jpeg":
        return data[:3] == b"\xff\xd8\xff"
    if mime == "image/png":
        return data[:8] == b"\x89PNG\r\n\x1a\n"
    if mime == "image/webp":
        return data[:4] == b"RIFF" and data[8:12] == b"WEBP"
    if mime == "application/pdf":
        return data[:5] == b"%PDF-"
    return False


def _sha256_matches(expected, data):
    """Meta documents the hash as sha256 but has sent it both hex and
    base64 encoded; accept either."""
    digest = hashlib.sha256(data).digest()
    expected = expected.strip()
    if expected.lower() == digest.hex():
        return True
    return expected.rstrip("=") in (
        base64.b64encode(digest).decode().rstrip("="),
        base64.urlsafe_b64encode(digest).decode().rstrip("="),
    )


def _safe_filename(name, ext):
    """Keep only the base name, printable, with the extension of the real
    type (a 'recibo.exe' declared as PDF is saved as 'recibo.exe.pdf')."""
    name = re.sub(r"[\x00-\x1f/\\]+", "_", str(name or "")).strip(" ._")[:120]
    if not name:
        return ""
    if not name.lower().endswith("." + ext):
        name = "%s.%s" % (name, ext)
    return name


class WhatsappMessage(models.Model):
    _name = "distri.whatsapp.message"
    _description = "Mensaje WhatsApp"
    _order = "timestamp desc, id desc"
    _rec_name = "wa_message_id"

    wa_message_id = fields.Char(string="ID de mensaje", required=True, readonly=True)
    direction = fields.Selection(
        [("in", "Recibido"), ("out", "Enviado")],
        string="Dirección", required=True, readonly=True,
    )
    wa_id = fields.Char(
        string="Número (wa_id)", index=True, readonly=True,
        help="Número del cliente: quien escribe en los recibidos, "
             "destinatario en los enviados.",
    )
    profile_name = fields.Char(string="Nombre del perfil", readonly=True)
    message_type = fields.Char(string="Tipo", readonly=True)
    body = fields.Text(string="Texto", readonly=True)
    timestamp = fields.Datetime(string="Fecha del mensaje", readonly=True)
    status = fields.Char(string="Estado", readonly=True)
    status_date = fields.Datetime(string="Fecha del estado", readonly=True)
    phone_number_id = fields.Char(string="Phone Number ID", readonly=True)
    payload = fields.Text(string="Aviso recibido (JSON)", readonly=True)

    # media (image / document): downloaded later by the cron
    media_id = fields.Char(string="ID de archivo (Meta)", readonly=True)
    media_mime_type = fields.Char(string="Tipo de archivo", readonly=True)
    media_sha256 = fields.Char(string="SHA-256 (Meta)", readonly=True)
    media_filename = fields.Char(string="Nombre original", readonly=True)
    media_state = fields.Selection(
        [("pending", "Pendiente"), ("done", "Descargado"), ("error", "Error")],
        string="Descarga", index=True, readonly=True, copy=False,
    )
    media_attempts = fields.Integer(string="Intentos de descarga", readonly=True, copy=False)
    media_error = fields.Char(string="Último error de descarga", readonly=True, copy=False)
    attachment_id = fields.Many2one(
        "ir.attachment", string="Archivo", readonly=True, copy=False, ondelete="set null",
    )
    has_attachment = fields.Boolean(
        string="Con archivo", compute="_compute_has_attachment", store=True,
    )
    media_is_image = fields.Boolean(compute="_compute_media_is_image")
    media_data = fields.Binary(string="Comprobante", related="attachment_id.datas")
    media_name = fields.Char(string="Nombre del archivo", related="attachment_id.name")

    _wa_message_id_unique = models.Constraint(
        "UNIQUE(wa_message_id)", "Ese mensaje de WhatsApp ya está registrado.",
    )

    @api.depends("attachment_id", "attachment_id.mimetype")
    def _compute_has_attachment(self):
        for record in self:
            record.has_attachment = bool(record.attachment_id)

    @api.depends("attachment_id.mimetype")
    def _compute_media_is_image(self):
        for record in self:
            record.media_is_image = (record.attachment_id.sudo().mimetype or "").startswith("image/")

    # ------------------------------------------------------------ config

    @api.model
    def _wa_param(self, key):
        return (self.env["ir.config_parameter"].sudo().get_param(key) or "").strip()

    @api.model
    def _wa_verify_token(self):
        return self._wa_param(PARAM_VERIFY_TOKEN)

    @api.model
    def _wa_app_secret(self):
        return self._wa_param(PARAM_APP_SECRET)

    # ------------------------------------------------------------ storing

    @api.model
    def _wa_store_webhook(self, payload):
        """Record every message and status of a verified webhook body.

        Returns (created, updated) counts. Unknown shapes are skipped, never
        raised: Meta retries anything that is not a 200.
        """
        created = updated = 0
        if not isinstance(payload, dict) or payload.get("object") != "whatsapp_business_account":
            return created, updated
        for entry in payload.get("entry") or []:
            if not isinstance(entry, dict):
                continue
            for change in entry.get("changes") or []:
                if not isinstance(change, dict) or change.get("field") != "messages":
                    continue
                value = change.get("value") if isinstance(change.get("value"), dict) else {}
                phone_number_id = _text((value.get("metadata") or {}).get("phone_number_id"), 32)
                names = {}
                for contact in value.get("contacts") or []:
                    if isinstance(contact, dict) and contact.get("wa_id"):
                        names[str(contact["wa_id"])] = _text(
                            (contact.get("profile") or {}).get("name"), 256)
                for message in value.get("messages") or []:
                    if isinstance(message, dict) and self._wa_store_message(
                            message, names, phone_number_id):
                        created += 1
                for status in value.get("statuses") or []:
                    if isinstance(status, dict) and self._wa_store_status(status, phone_number_id):
                        updated += 1
        return created, updated

    @api.model
    def _wa_create_once(self, values):
        """Create unless the id already exists (Meta retries). Returns the
        new record, or an empty recordset when it was already there."""
        Message = self.sudo()
        if Message.search_count([("wa_message_id", "=", values["wa_message_id"])], limit=1):
            return Message.browse()
        try:
            with self.env.cr.savepoint():
                return Message.create(values)
        except IntegrityError:
            # two deliveries of the same retry raced; the other one won
            return Message.browse()

    @api.model
    def _wa_store_message(self, message, names, phone_number_id):
        message_id = _text(message.get("id"), 256)
        if not message_id:
            return False
        sender = _text(message.get("from"), 32)
        kind = _text(message.get("type"), 32)
        body = ""
        if kind == "text":
            body = _text((message.get("text") or {}).get("body"), 8192)
        values = {
            "wa_message_id": message_id,
            "direction": "in",
            "wa_id": sender,
            "profile_name": names.get(sender, ""),
            "message_type": kind,
            "body": body,
            "timestamp": _parse_wa_timestamp(message.get("timestamp")),
            "phone_number_id": phone_number_id,
            "payload": json.dumps(message, ensure_ascii=False, indent=2),
        }
        if kind in MEDIA_TYPES:
            values.update(self._wa_media_values(message.get(kind)))
        record = self._wa_create_once(values)
        return bool(record)

    @api.model
    def _wa_media_values(self, media):
        """Fields of an image/document message. Only stores what the payload
        says; the file itself is fetched by the cron."""
        media = media if isinstance(media, dict) else {}
        mime = _mime(media.get("mime_type"))
        values = {
            "body": _text(media.get("caption"), 8192),
            "media_id": _text(media.get("id"), 64),
            "media_mime_type": mime,
            "media_sha256": _text(media.get("sha256"), 128),
            "media_filename": _text(media.get("filename"), 256),
            "media_state": "pending",
        }
        if not values["media_id"]:
            values.update(media_state="error", media_error="El aviso no trae el ID del archivo")
        elif mime not in MEDIA_ALLOWED:
            # stickers, audio, Word files...: not a receipt, never downloaded
            values.update(media_state="error",
                          media_error="Tipo de archivo no permitido: %s" % (mime or "desconocido"))
        return values

    @api.model
    def _wa_store_status(self, status, phone_number_id):
        """Status updates refer to messages WE sent: keep one record per
        message id and move its status forward."""
        message_id = _text(status.get("id"), 256)
        new_status = _text(status.get("status"), 32)
        if not message_id or not new_status:
            return False
        values = {
            "status": new_status,
            "status_date": _parse_wa_timestamp(status.get("timestamp")),
            "payload": json.dumps(status, ensure_ascii=False, indent=2),
        }
        Message = self.sudo()
        record = Message.search([("wa_message_id", "=", message_id)], limit=1)
        if not record:
            record = self._wa_create_once(dict(values, **{
                "wa_message_id": message_id,
                "direction": "out",
                "wa_id": _text(status.get("recipient_id"), 32),
                "phone_number_id": phone_number_id,
            }))
            if record:
                return True
            record = Message.search([("wa_message_id", "=", message_id)], limit=1)
        if STATUS_RANK.get(new_status, 0) < STATUS_RANK.get(record.status, 0):
            return False
        if record.status == new_status:
            return False
        record.write(values)
        return True

    # ------------------------------------------------------------ media

    @api.model
    def _wa_graph_get(self, token, url, stream=False, allow_redirects=False):
        headers = {"Authorization": "Bearer %s" % token}
        try:
            return requests.get(url, headers=headers, timeout=WA_TIMEOUT,
                                stream=stream, allow_redirects=allow_redirects)
        except requests.RequestException as exc:
            _logger.warning("WhatsApp media GET %s failed: %s",
                            urlsplit(url).netloc, type(exc).__name__)
            raise WhatsappMediaError("Error de red al hablar con Meta (%s)"
                                     % type(exc).__name__) from None

    @api.model
    def _wa_fetch_media(self, token, media_id):
        """Return (bytes, mime, sha256 reported by the Graph API).

        Always asks for a fresh URL: it lives about 5 minutes, the media id
        about 30 days."""
        if not media_id.isdigit() or len(media_id) > 32:
            raise WhatsappMediaError("ID de archivo inválido", final=True)
        response = self._wa_graph_get(token, "%s/%s" % (WA_GRAPH_API, media_id))
        if response.status_code != 200:
            try:
                detail = _text((response.json().get("error") or {}).get("message"), 200)
            except ValueError:
                detail = ""
            _logger.warning("WhatsApp media %s: Graph API answered HTTP %s: %s",
                            media_id, response.status_code, detail)
            # 400/404: the id does not exist or already expired
            raise WhatsappMediaError(
                "Meta respondió HTTP %s al pedir el archivo%s"
                % (response.status_code, (": " + detail) if detail else ""),
                final=response.status_code in (400, 404),
            )
        try:
            info = response.json()
        except ValueError:
            raise WhatsappMediaError("Respuesta inválida de Meta (no es JSON)") from None
        url = str(info.get("url") or "")
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        if parts.scheme != "https" or not host.endswith(MEDIA_URL_HOSTS):
            _logger.warning("WhatsApp media %s: unexpected download host %r", media_id, host)
            raise WhatsappMediaError("Meta devolvió una dirección de descarga inesperada",
                                     final=True)
        try:
            size = int(info.get("file_size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > MEDIA_MAX_BYTES:
            raise WhatsappMediaError("Archivo demasiado grande (%s bytes)" % size, final=True)

        # requests drops the Authorization header if a redirect changes host
        response = self._wa_graph_get(token, url, stream=True, allow_redirects=True)
        with response:
            if response.status_code != 200:
                _logger.warning("WhatsApp media %s: download answered HTTP %s",
                                media_id, response.status_code)
                raise WhatsappMediaError("La descarga respondió HTTP %s" % response.status_code)
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MEDIA_MAX_BYTES:
                raise WhatsappMediaError("Archivo demasiado grande (%s bytes)" % declared,
                                         final=True)
            chunks, total = [], 0
            try:
                for chunk in response.iter_content(64 * 1024):
                    total += len(chunk)
                    if total > MEDIA_MAX_BYTES:
                        raise WhatsappMediaError("Archivo de más de %s MB"
                                                 % (MEDIA_MAX_BYTES // (1024 * 1024)), final=True)
                    chunks.append(chunk)
            except requests.RequestException as exc:
                _logger.warning("WhatsApp media %s: download interrupted: %s",
                                media_id, type(exc).__name__)
                raise WhatsappMediaError("Descarga interrumpida (%s)"
                                         % type(exc).__name__) from None
            mime = _mime(response.headers.get("Content-Type"))
        return b"".join(chunks), mime, _text(info.get("sha256"), 128)

    def _wa_media_attachment_name(self, ext):
        self.ensure_one()
        original = _safe_filename(self.media_filename, ext)
        if original:
            return original
        when = self.timestamp or self.create_date or fields.Datetime.now()
        return "comprobante-%s-%s.%s" % (
            re.sub(r"\D", "", self.wa_id or "") or "sin-numero",
            when.strftime("%Y%m%d-%H%M%S"), ext)

    def _wa_download_media(self, token):
        """Download the file of one pending message and attach it.

        Never raises: failures end in `media_error`, and in state `error`
        once they are final or after MEDIA_MAX_ATTEMPTS. Returns True when
        the file was saved."""
        self.ensure_one()
        record = self.sudo()
        # the cron and the button can meet on the same message: whoever
        # holds the row lock downloads, the other one skips it
        self.env.cr.execute(
            "SELECT id FROM distri_whatsapp_message WHERE id = %s FOR UPDATE SKIP LOCKED",
            [record.id],
        )
        if not self.env.cr.fetchone():
            return False
        record.invalidate_recordset()
        if record.media_state == "done" or not record.media_id:
            return False
        attempts = record.media_attempts + 1
        try:
            mime = record.media_mime_type
            if mime not in MEDIA_ALLOWED:
                raise WhatsappMediaError("Tipo de archivo no permitido: %s"
                                         % (mime or "desconocido"), final=True)
            data, served_mime, graph_sha256 = self._wa_fetch_media(token, record.media_id)
            if served_mime and served_mime != mime and served_mime in MEDIA_ALLOWED:
                mime = served_mime
            if not data:
                raise WhatsappMediaError("Meta entregó un archivo vacío")
            if not _magic_matches(mime, data):
                raise WhatsappMediaError(
                    "El contenido no es un %s válido" % MEDIA_ALLOWED[mime].upper(), final=True)
            expected = record.media_sha256 or graph_sha256
            if expected and not _sha256_matches(expected, data):
                # a truncated/corrupt transfer: worth another try
                raise WhatsappMediaError("El SHA-256 del archivo no coincide con el de Meta")
        except WhatsappMediaError as exc:
            final = exc.final or attempts >= MEDIA_MAX_ATTEMPTS
            record.write({
                "media_attempts": attempts,
                "media_error": _text(exc, 250),
                "media_state": "error" if final else "pending",
            })
            _logger.info("WhatsApp media of message %s not downloaded (attempt %s%s)",
                         record.id, attempts, ", final" if final else "")
            return False

        attachment = self.env["ir.attachment"].sudo().create({
            "name": record._wa_media_attachment_name(MEDIA_ALLOWED[mime]),
            "raw": data,
            "mimetype": mime,
            "res_model": record._name,
            "res_id": record.id,
        })
        record.write({
            "attachment_id": attachment.id,
            "media_attempts": attempts,
            "media_error": False,
            "media_state": "done",
        })
        _logger.info("WhatsApp media of message %s saved as attachment %s (%s bytes)",
                     record.id, attachment.id, len(data))
        return True

    @api.model
    def _wa_cron_download_media(self):
        """Every minute: download the pending images/documents."""
        token = self._wa_param(PARAM_ACCESS_TOKEN)
        pending = self.sudo().search(
            [("media_state", "=", "pending"), ("direction", "=", "in")],
            order="id", limit=MEDIA_CRON_BATCH,
        )
        if not pending:
            return
        if not token:
            _logger.warning("WhatsApp media: %s pending files, access token not configured",
                            len(pending))
            return
        Cron = self.env["ir.cron"]
        for record in pending:
            try:
                with self.env.cr.savepoint():
                    record._wa_download_media(token)
            except Exception:
                # a bug must not block the queue forever: count the attempt
                _logger.exception("WhatsApp media of message %s: unexpected error", record.id)
                record.invalidate_recordset()
                attempts = record.media_attempts + 1
                record.write({
                    "media_attempts": attempts,
                    "media_error": "Error inesperado (ver el log del servidor)",
                    "media_state": "error" if attempts >= MEDIA_MAX_ATTEMPTS else "pending",
                })
            if Cron._commit_progress(1) <= WA_TIMEOUT * 2:
                break  # the next run picks up the rest

    def action_wa_download_media(self):
        """Button "Descargar ahora" (admins): same download as the cron."""
        if not self.env.user.has_group("base.group_system"):
            raise UserError(_("Solo un administrador puede descargar el archivo."))
        token = self._wa_param(PARAM_ACCESS_TOKEN)
        if not token:
            raise UserError(_("Falta la llave de WhatsApp (distri_pedido.wa_access_token)."))
        for record in self:
            if record.media_id and record.media_state != "done":
                record._wa_download_media(token)
        return True
