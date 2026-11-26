"""WhatsApp Cloud API inbound webhook (Meta), own integration.

Meta signs every POST with the app secret (X-Hub-Signature-256); the
controller checks that signature before calling `_wa_store_webhook`, so the
payload handled here is already authentic. Nothing is sent from here yet.

Images and documents (payment receipts) are NOT downloaded inside the webhook
request: Meta wants the 200 in under 5 s. The message is stored with the
media id as `pending` and a cron fetches the file later from the Graph API
(first the media id -> short-lived URL, then the file with the same token).

Several numbers (19.0.1.88.0): every number connected to the same Meta app
posts to the same webhook; `metadata.phone_number_id` says which number
received the message. `distri.whatsapp.numero` gives each one a role
(models/wa_payload.py). The media download never depends on the number: the
media id alone, with the single app access token.
"""
import base64
import hashlib
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

import requests
from markupsafe import Markup, escape
from psycopg2 import IntegrityError

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

from .wa_payload import (
    DEFAULT_ROLE, MEDIA_ALERT_EXPIRING_DAYS, MEDIA_ALERT_LATE_HOURS, MEDIA_EXPIRY_DAYS,
    ROLES, USER_ID_MAX, sender_identity, status_recipient,
)

_logger = logging.getLogger(__name__)

PARAM_VERIFY_TOKEN = "distri_pedido.wa_verify_token"
PARAM_APP_SECRET = "distri_pedido.wa_app_secret"
PARAM_ACCESS_TOKEN = "distri_pedido.wa_access_token"
# Legacy single number (19.0.1.37.0). Since 19.0.1.88.0 the numbers and their
# roles live in `distri.whatsapp.numero`; the parameter is only read once, by
# the 19.0.1.88.0 migration (`_wa_migrate_phone_param`).
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

# Media alerts (`_wa_cron_media_alerts`): one To-Do for administración,
# anchored on the receiving number, updated while it is open.
MEDIA_ALERT_LATE = "late"
MEDIA_ALERT_EXPIRING = "expiring"
MEDIA_ALERT_SUMMARY = {
    MEDIA_ALERT_LATE: "WhatsApp: comprobantes sin descargar",
    MEDIA_ALERT_EXPIRING: "WhatsApp: comprobantes por vencer en Meta",
}
MEDIA_ALERT_LIST_MAX = 40
ADMIN_GROUP = "distri_pedido.grupo_administracion"
AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")


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


class WhatsappNumero(models.Model):
    """A WhatsApp number connected to the Meta app, with its role.

    A model and not parameters: several numbers, each with a role and an
    on/off switch, editable by an administrator without restarting Odoo, and
    a record to hang the media alerts on (activities need mail.thread)."""
    _name = "distri.whatsapp.numero"
    _description = "Número de WhatsApp conectado"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "sequence, id"

    name = fields.Char(string="Número", required=True, tracking=True,
                       help="El número como se muestra, con código de país. "
                            "Ej.: +54 9 299 000-0000.")
    phone_number_id = fields.Char(
        string="Phone Number ID", required=True, tracking=True,
        help="Identificador del número en Meta (WhatsApp Manager › Configuración de la API). "
             "Llega en cada aviso como metadata.phone_number_id.")
    rol = fields.Selection(ROLES, string="Uso", required=True, default=DEFAULT_ROLE,
                           tracking=True,
                           help="Pedidos y comprobantes: confirma pedidos por código y lee "
                                "comprobantes (como siempre). Solo comprobantes: el número de "
                                "repartidores y vendedores; nunca confirma pedidos ni verifica "
                                "teléfonos, y toda foto o PDF es un comprobante. Solo pedidos: "
                                "las fotos no se leen como comprobantes.")
    active = fields.Boolean(string="Activo", default=True, tracking=True,
                            help="Un número archivado se trata como no configurado: sus "
                                 "mensajes se guardan igual, marcados.")
    sequence = fields.Integer(default=10)
    whatsapp_count = fields.Integer(string="Mensajes", compute="_compute_whatsapp_count")

    _phone_number_id_unique = models.Constraint(
        "UNIQUE(phone_number_id)", "Ese Phone Number ID ya está cargado.",
    )

    @api.constrains("phone_number_id")
    def _check_phone_number_id(self):
        for record in self:
            if not (record.phone_number_id or "").isdigit():
                raise ValidationError(_("El Phone Number ID son solo números."))

    def _compute_whatsapp_count(self):
        groups = self.env["distri.whatsapp.message"].sudo()._read_group(
            [("numero_id", "in", self.ids)], ["numero_id"], ["__count"])
        counts = {numero.id: count for numero, count in groups}
        for record in self:
            record.whatsapp_count = counts.get(record.id, 0)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get("phone_number_id"):
                vals["phone_number_id"] = str(vals["phone_number_id"]).strip()
        return super().create(vals_list)

    def write(self, vals):
        if vals.get("phone_number_id"):
            vals = dict(vals, phone_number_id=str(vals["phone_number_id"]).strip())
        return super().write(vals)

    @api.model
    def _wa_route(self, phone_number_id):
        """(numero, role, not_configured) for the number that received a
        message. Empty phone_number_id (app photos, chat imports): no number,
        default role, not flagged. Unknown or archived: default role, flagged."""
        phone_number_id = (phone_number_id or "").strip()
        if not phone_number_id:
            return self.browse(), False, False
        numero = self.sudo().search([("phone_number_id", "=", phone_number_id)], limit=1)
        if numero:
            return numero, numero.rol, False
        return self.browse(), DEFAULT_ROLE, True

    @api.model
    def _wa_default(self):
        """The number that takes the alerts of messages without a known one."""
        Numero = self.sudo()
        return (Numero.search([("rol", "=", DEFAULT_ROLE)], limit=1)
                or Numero.search([], limit=1))

    @api.model
    def _wa_migrate_phone_param(self):
        """19.0.1.88.0: the single `distri_pedido.wa_phone_number_id` becomes a
        number with role «Pedidos y comprobantes» (same behaviour as before),
        and its messages get the number. Idempotent. Returns the number."""
        value = (self.env["ir.config_parameter"].sudo().get_param(PARAM_PHONE_NUMBER_ID)
                 or "").strip()
        if not value.isdigit():
            return self.browse()
        Numero = self.sudo().with_context(active_test=False)
        numero = Numero.search([("phone_number_id", "=", value)], limit=1)
        if not numero:
            numero = Numero.create({
                "name": "Número principal (%s)" % value,
                "phone_number_id": value,
                "rol": DEFAULT_ROLE,
            })
            _logger.info("WhatsApp number %s migrated from the parameter", value)
        self.env.cr.execute("""
            UPDATE distri_whatsapp_message
               SET numero_id = %s, wa_rol = %s
             WHERE phone_number_id = %s AND numero_id IS NULL
        """, [numero.id, numero.rol, value])
        self.env["distri.whatsapp.message"].invalidate_model(["numero_id", "wa_rol"])
        return numero

    def action_view_messages(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": _("Mensajes de %s", self.name),
            "res_model": "distri.whatsapp.message",
            "view_mode": "list,form",
            "domain": [("numero_id", "=", self.id)],
        }


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
    phone_number_id = fields.Char(string="Phone Number ID", readonly=True, index=True)
    # 19.0.1.88.0: the receiving number and its role, fixed when the message
    # arrives (changing a number's role later does not re-route old messages)
    numero_id = fields.Many2one("distri.whatsapp.numero", string="Número que recibió",
                                readonly=True, index=True, ondelete="set null")
    wa_rol = fields.Selection(ROLES, string="Uso del número", readonly=True,
                              help="El uso del número que recibió el mensaje, al llegar. "
                                   "Vacío: mensajes de la app de reparto o de un chat importado.")
    wa_numero_no_configurado = fields.Boolean(
        string="Número no configurado", readonly=True, index=True,
        help="Llegó a un Phone Number ID que no está en «Números de WhatsApp» (o está "
             "archivado). Se guardó y se trató como «Pedidos y comprobantes».")
    numero_recibido = fields.Char(string="Recibido en", compute="_compute_numero_recibido")
    # senders without a phone (WhatsApp usernames, 2026)
    wa_user_id = fields.Char(string="Usuario de WhatsApp (BSUID)", readonly=True, index=True,
                             help="Identificador de usuario que manda Meta (user_id). Llega "
                                  "aunque el remitente oculte su teléfono con un nombre de "
                                  "usuario.")
    wa_username = fields.Char(string="Nombre de usuario", readonly=True)
    wa_sin_telefono = fields.Boolean(
        string="Remitente sin teléfono", readonly=True, index=True,
        help="El aviso de Meta no trajo el número del remitente (usa nombre de usuario). "
             "Un pedido con código no verifica teléfono y un comprobante va a «Revisar».")
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
    media_alert = fields.Selection(
        [(MEDIA_ALERT_LATE, "Avisado: sin descargar"),
         (MEDIA_ALERT_EXPIRING, "Avisado: por vencer")],
        string="Alerta de descarga", readonly=True, copy=False,
        help="La última alerta a administración por este archivo sin descargar.")
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

    @api.depends("numero_id", "phone_number_id")
    def _compute_numero_recibido(self):
        for record in self:
            record.numero_recibido = record.numero_id.name or record.phone_number_id or False

    @api.model_create_multi
    def create(self, vals_list):
        Numero = self.env["distri.whatsapp.numero"]
        for vals in vals_list:
            if vals.get("phone_number_id") and "wa_rol" not in vals:
                numero, role, unknown = Numero._wa_route(vals["phone_number_id"])
                vals.update(numero_id=numero.id or False, wa_rol=role,
                            wa_numero_no_configurado=unknown)
        return super().create(vals_list)

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
                metadata = value.get("metadata") if isinstance(value.get("metadata"), dict) else {}
                phone_number_id = _text(metadata.get("phone_number_id"), 32)
                contacts = value.get("contacts") if isinstance(value.get("contacts"), list) else []
                for message in value.get("messages") or []:
                    if isinstance(message, dict) and self._wa_store_message(
                            message, contacts, phone_number_id):
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
    def _wa_store_message(self, message, contacts, phone_number_id):
        """One incoming message. The sender may come without a phone (only
        a BSUID): it is stored all the same, flagged «Remitente sin teléfono»."""
        message_id = _text(message.get("id"), 256)
        if not message_id:
            return False
        sender = sender_identity(message, contacts)
        kind = _text(message.get("type"), 32)
        body = ""
        if kind == "text" and isinstance(message.get("text"), dict):
            body = _text(message["text"].get("body"), 8192)
        values = {
            "wa_message_id": message_id,
            "direction": "in",
            "wa_id": sender["phone"] or False,
            "wa_user_id": sender["user_id"] or False,
            "wa_username": sender["username"] or False,
            "wa_sin_telefono": not sender["phone"],
            "profile_name": sender["name"] or sender["username"] or "",
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
            phone, user_id = status_recipient(status)
            record = self._wa_create_once(dict(values, **{
                "wa_message_id": message_id,
                "direction": "out",
                "wa_id": phone or False,
                "wa_user_id": user_id[:USER_ID_MAX] or False,
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

        Every call (so every attempt of the cron or the button) asks the
        Graph API for a fresh URL first: the URL lives about 5 minutes, the
        media id about 7 days (webhook media). Only the media id and the
        single app token are used: no phone_number_id in the path, so it
        works the same for every connected number."""
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
        # without a media id there is nothing to ask Meta (a chat import cut
        # halfway leaves files «pending» with their attachment): never let
        # those fill the batch and block the real ones
        pending = self.sudo().search(
            [("media_state", "=", "pending"), ("direction", "=", "in"),
             ("media_id", "!=", False), ("attachment_id", "=", False)],
            order="id", limit=MEDIA_CRON_BATCH,
        )
        if not pending:
            return
        if not token:
            _logger.warning("WhatsApp media: %s pending files, access token not configured",
                            len(pending))
            return
        Cron = self.env["ir.cron"]
        # 26/11/2026: the receipt reading inside a download gets the time this
        # run has left (minus a margin), so it gives up before Odoo kills the
        # worker past limit_time_real
        from .ocr_comprobante import ocr_deadline
        left = Cron._commit_progress(0)
        for record in pending:
            budget = min(left, 3600) - WA_TIMEOUT * 3
            if budget < 5:
                break  # the next run picks up the rest
            try:
                with self.env.cr.savepoint(), ocr_deadline(budget):
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
            left = Cron._commit_progress(1)
            if left <= WA_TIMEOUT * 2:
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

    # ------------------------------------------------------------ media alerts

    @api.model
    def _wa_missing_media_domain(self):
        """Incoming photos and PDFs Meta announced whose file is not saved."""
        return [("direction", "=", "in"), ("message_type", "in", list(MEDIA_TYPES)),
                ("media_mime_type", "in", list(MEDIA_ALLOWED)),
                ("media_state", "in", ("pending", "error")),
                ("attachment_id", "=", False)]

    @api.model
    def _wa_admin_user(self):
        """The administración user who gets the media alerts."""
        root = self.env.ref("base.user_root")
        group = self.env.ref(ADMIN_GROUP, raise_if_not_found=False)
        users = group.sudo().user_ids.filtered(
            lambda u: u.active and not u.share and u != root) if group else self.env["res.users"]
        return users.sorted("id")[:1]

    @api.model
    def _wa_cron_media_alerts(self, now=None):
        """Hourly: one To-Do for administración (activities only, no email)
        when a photo or PDF is still not downloaded more than
        MEDIA_ALERT_LATE_HOURS after it arrived, and again when it is more
        than MEDIA_ALERT_EXPIRING_DAYS old (Meta drops it after about
        MEDIA_EXPIRY_DAYS). Each message is reported once per level. The
        activity hangs on the receiving number (the default one for app or
        unknown numbers); while it is open new messages are appended to it
        instead of opening another. Returns {level: messages alerted}."""
        now = now or fields.Datetime.now()
        Message = self.sudo()
        # older than the expiry plus a day: the file is gone, alerting helps
        # nobody (it stays in «Comprobantes sin archivo»)
        horizon = now - timedelta(days=MEDIA_EXPIRY_DAYS + 1)
        base = self._wa_missing_media_domain() + [("create_date", ">=", horizon)]
        levels = (
            (MEDIA_ALERT_EXPIRING, [("create_date", "<=",
                                     now - timedelta(days=MEDIA_ALERT_EXPIRING_DAYS)),
                                    ("media_alert", "!=", MEDIA_ALERT_EXPIRING)]),
            (MEDIA_ALERT_LATE, [("create_date", "<=",
                                 now - timedelta(hours=MEDIA_ALERT_LATE_HOURS)),
                                ("media_alert", "=", False)]),
        )
        result = {}
        user = self._wa_admin_user()
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        Numero = self.env["distri.whatsapp.numero"]
        for level, extra in levels:
            found = Message.search(base + extra, order="create_date, id")
            if not found:
                continue
            if not (user and todo):
                _logger.warning("WhatsApp media alert (%s): %s messages, no administración "
                                "user to warn", level, len(found))
                continue
            default = Numero._wa_default()
            by_number = {}
            for msg in found:
                anchor = msg.numero_id if msg.numero_id.active else default
                by_number.setdefault(anchor, Message.browse())
                by_number[anchor] |= msg
            alerted = Message.browse()
            for anchor, msgs in by_number.items():
                if not anchor:
                    _logger.warning("WhatsApp media alert (%s): %s messages, no WhatsApp "
                                    "number to attach the activity to", level, len(msgs))
                    continue
                self._wa_alert_activity(anchor, level, msgs, user, todo)
                alerted |= msgs
            # the 2 h alert is not needed any more once it is about to expire
            alerted.write({"media_alert": level})
            result[level] = alerted
            _logger.info("WhatsApp media alert (%s): %s messages", level, len(alerted))
        return result

    @api.model
    def _wa_alert_line(self, msg):
        sender = msg.distri_forwarded_by_id.name if "distri_forwarded_by_id" in msg else ""
        sender = sender or msg.profile_name or (("+" + msg.wa_id) if msg.wa_id else "") \
            or msg.wa_username or "remitente sin teléfono"
        when = msg.create_date.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime(
            "%d/%m %H:%M")
        state = dict(msg._fields["media_state"].selection).get(msg.media_state, "")
        line = "Mensaje %s · %s · %s · %s" % (msg.id, when, sender, state)
        if msg.media_error:
            line += " (%s)" % msg.media_error
        return line

    @api.model
    def _wa_alert_activity(self, anchor, level, msgs, user, todo):
        summary = MEDIA_ALERT_SUMMARY[level]
        if level == MEDIA_ALERT_LATE:
            intro = ("Fotos o PDF recibidos por WhatsApp que siguen sin descargar después de "
                     "%s h. Revisá «Comprobantes sin archivo» y probá «Descargar ahora»; si "
                     "Meta ya no lo entrega, pedile al remitente que lo mande de nuevo."
                     % MEDIA_ALERT_LATE_HOURS)
        else:
            intro = ("Fotos o PDF de más de %s días sin descargar: Meta los borra a los %s "
                     "días. Descargalos ahora o pedile al remitente que los mande de nuevo."
                     % (MEDIA_ALERT_EXPIRING_DAYS, MEDIA_EXPIRY_DAYS))
        lines = [escape(self._wa_alert_line(m)) for m in msgs[:MEDIA_ALERT_LIST_MAX]]
        if len(msgs) > MEDIA_ALERT_LIST_MAX:
            lines.append(escape("y %s más" % (len(msgs) - MEDIA_ALERT_LIST_MAX)))
        listed = Markup("<br/>").join(lines)
        anchor = anchor.sudo().with_context(mail_activity_quick_update=True, mail_notrack=True)
        open_activity = anchor.activity_ids.filtered(lambda a: a.summary == summary)[:1]
        if open_activity:
            # the same activity, never a second one while it is open
            open_activity.write({"note": Markup("%s<br/>%s") % (
                Markup(open_activity.note or ""), listed)})
            return open_activity
        return anchor.activity_schedule(
            activity_type_id=todo.id, summary=summary, user_id=user.id,
            note=Markup("%s<br/>%s") % (intro, listed))

    @api.model
    def action_wa_missing_files(self):
        """«Comprobantes sin archivo»: the daily reconciliation. Photos and
        PDFs Meta announced whose file is not saved (pending or error), the
        last day by default (the filter can be removed)."""
        return {
            "type": "ir.actions.act_window",
            "name": _("Comprobantes sin archivo"),
            "res_model": "distri.whatsapp.message",
            "view_mode": "list,form",
            "views": [(self.env.ref("distri_pedido.view_whatsapp_missing_files_list").id, "list"),
                      (False, "form")],
            "domain": self._wa_missing_media_domain(),
            "context": {"search_default_ultimo_dia": 1, "create": False},
            "help": _("<p>No hay fotos ni PDF sin archivo.</p>"),
        }
