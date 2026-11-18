"""Customer and order data used by the delivery ("reparto") app.

- Delivery data on the contact: exact location, days and time ranges in
  which the shop receives, a note for drivers and the confirmed WhatsApp.
- The 4-digit receipt code of an order. The driver's phone validates it
  offline with a salted SHA-256 hash. Since 19.0.1.99.0 (audit F7) the code
  is created when the customer places the order and shown to him on the
  order confirmation and on his order page, so the plain code is kept too
  (administración only; the customer page reads it with the order token).
  Wrong tries are limited (audit F12): after PARAM_CODE_ATTEMPTS wrong codes
  the order's code locks until administración unlocks it.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import UserError

from .cuenta import clave_documento

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

# Contact name prefixes mark the town. No prefix means Neuquén capital.
TOWN_PREFIXES = {
    "ZC": "Cutral Có",
    "X": "Centenario",
    "Y": "Plottier",
    "Z": "Zapala",
    "W": "Catriel",
}
CAPITAL = "Neuquén"
_PREFIX_RE = re.compile(r"^\s*(ZC|X|Y|Z|W)\s*-\s*", re.IGNORECASE)

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
_HOURS_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)-([01]?\d|2[0-3]):([0-5]\d)$")

RECEIPT_CODE_DIGITS = 4
PARAM_CODE_ATTEMPTS = "distri_pedido.codigo_recibido_intentos"
DEFAULT_CODE_ATTEMPTS = 5


def receipt_code_attempt_limit(env):
    """Wrong codes allowed per order before it locks (>= 1)."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_CODE_ATTEMPTS)
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return DEFAULT_CODE_ATTEMPTS


def today_ar():
    return datetime.now(AR_TZ).date()


def strip_town_prefix(name):
    return _PREFIX_RE.sub("", name or "").strip()


def town_from_name(name):
    match = _PREFIX_RE.match(name or "")
    return TOWN_PREFIXES[match.group(1).upper()] if match else CAPITAL


def friendly_case(text):
    """Most contacts were typed in capitals: "KIOSCO DIMAR" -> "Kiosco Dimar"."""
    text = " ".join((text or "").split())
    if text != text.upper():
        return text
    return " ".join(word[:1].upper() + word[1:].lower() for word in text.split(" "))


def receipt_code_hash(salt, code):
    return hashlib.sha256(("%s:%s" % (salt, code)).encode()).hexdigest()


def normalize_days(value):
    """"0,1,2" (Monday = 0). Unknown values are dropped."""
    days = sorted({int(d) for d in re.findall(r"\d", str(value or "")) if int(d) < 7})
    return ",".join(str(d) for d in days)


def normalize_hours(value):
    """"08:00-12:30;17:00-20:30". Invalid ranges are dropped."""
    ranges = []
    for part in re.split(r"[;,\s]+", str(value or "")):
        match = _HOURS_RE.match(part)
        if not match:
            continue
        h1, m1, h2, m2 = (int(g) for g in match.groups())
        if (h2, m2) <= (h1, m1):
            continue
        ranges.append("%02d:%02d-%02d:%02d" % (h1, m1, h2, m2))
    return ";".join(ranges[:4])


def days_text(days):
    days = [int(d) for d in (days or "").split(",") if d != ""]
    if not days:
        return ""
    if len(days) > 2 and days == list(range(days[0], days[-1] + 1)):
        return "%s a %s" % (WEEKDAYS[days[0]], WEEKDAYS[days[-1]])
    return ", ".join(WEEKDAYS[d] for d in days)


def hours_text(hours):
    parts = []
    for rng in (hours or "").split(";"):
        if "-" in rng:
            start, end = rng.split("-")
            parts.append("%s a %s" % (start.lstrip("0") or "0", end.lstrip("0") or "0"))
    return " y ".join(parts)


class ResPartnerDelivery(models.Model):
    _inherit = "res.partner"

    # partner_latitude / partner_longitude come from base
    distri_location_user_id = fields.Many2one(
        "res.users", string="Ubicación guardada por", copy=False, readonly=True)
    distri_location_date = fields.Datetime(
        string="Ubicación guardada el", copy=False, readonly=True)
    distri_delivery_days = fields.Char(
        string="Días en que recibe", copy=False,
        help='Lunes = 0 ... domingo = 6, separados por coma. Ej.: "0,1,2,3,4,5".')
    distri_delivery_hours = fields.Char(
        string="Horarios en que recibe", copy=False,
        help='Franjas separadas por punto y coma. Ej.: "08:00-12:30;17:00-20:30".')
    distri_delivery_text = fields.Char(
        string="Recibe", compute="_compute_distri_delivery_text")
    distri_driver_note = fields.Text(
        string="Comentario para los repartidores", copy=False)
    distri_whatsapp = fields.Char(
        string="WhatsApp", copy=False,
        help="+54 9, el código de área sin el 0 y el número sin el 15 (10 dígitos después "
             "del +54 9). Ej.: +54 9 299 4222444. Si lo escribís con el 0 o el 15, se "
             "guarda corregido: +5492994222444.")
    distri_whatsapp_confirmed = fields.Boolean(
        string="WhatsApp confirmado", copy=False, readonly=True)
    distri_whatsapp_date = fields.Datetime(
        string="WhatsApp confirmado el", copy=False, readonly=True)
    distri_whatsapp_user_id = fields.Many2one(
        "res.users", string="WhatsApp confirmado por", copy=False, readonly=True)

    @api.depends("distri_delivery_days", "distri_delivery_hours")
    def _compute_distri_delivery_text(self):
        for partner in self:
            days = days_text(partner.distri_delivery_days)
            hours = hours_text(partner.distri_delivery_hours)
            text = ", ".join(p for p in (days, hours) if p)
            partner.distri_delivery_text = ("Recibe %s" % text) if text else ""

    def _distri_app_name(self):
        """The name shown to the driver: without the town prefix."""
        self.ensure_one()
        return friendly_case(strip_town_prefix(self.name))

    def _distri_town(self):
        self.ensure_one()
        return town_from_name(self.name)

    def _distri_whatsapp_shown(self):
        self.ensure_one()
        return self.distri_whatsapp or self.phone or ""

    def _distri_account_balance(self):
        """Receivable balance: Odoo accounting plus the imported history.

        Same rule as the account statement (models/estado_cuenta.py): a
        document present in both sources (same number and same amount,
        models/cuenta.py clave_documento) counts once, and the live one from
        Odoo wins. Returns reported by the driver and not settled yet
        («Devolución informada») are already subtracted.
        """
        self.ensure_one()
        partner = self.commercial_partner_id

        lines = self.env["account.move.line"].sudo().search([
            ("partner_id", "=", partner.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
        ])
        seen = {clave_documento(line.move_id.name, line.debit, line.credit) for line in lines}
        seen.discard(None)
        balance = sum(lines.mapped("debit")) - sum(lines.mapped("credit"))
        for mov in self.env["distri.cuenta.movimiento"].sudo().with_context(active_test=True).search([
                ("partner_id", "=", partner.id)]):
            if clave_documento(mov.comprobante, mov.debe, mov.haber) not in seen:
                balance += (mov.debe or 0.0) - (mov.haber or 0.0)
        provisional = self.env["distri.route.return"]._distri_provisional_for(partner)
        return balance - sum(provisional.mapped("amount"))


class SaleOrderDelivery(models.Model):
    _inherit = "sale.order"

    distri_receipt_salt = fields.Char(
        copy=False, readonly=True, groups="distri_pedido.grupo_administracion")
    distri_receipt_hash = fields.Char(
        copy=False, readonly=True, groups="distri_pedido.grupo_administracion")
    distri_receipt_date = fields.Datetime(
        string="Código de recibido generado", copy=False, readonly=True)
    distri_receipt_code = fields.Char(
        string="Código de recibido", copy=False, readonly=True,
        groups="distri_pedido.grupo_administracion",
        help="Lo ve el cliente en la página de su pedido y se lo dicta al repartidor.")
    distri_receipt_attempts = fields.Integer(
        string="Intentos fallidos del código", copy=False, readonly=True,
        help="Códigos equivocados cargados en la entrega. Al llegar al límite el código se "
             "bloquea hasta que administración lo desbloquee.")
    distri_receipt_locked = fields.Boolean(
        string="Código bloqueado", compute="_compute_distri_receipt_locked")
    distri_stop_ids = fields.One2many(
        "distri.route.stop", "order_id", string="Visitas de reparto", readonly=True)
    distri_delivery_state = fields.Selection([
        ("unassigned", "Sin asignar a una ruta"),
        ("on_route", "En ruta"),
        ("delivered", "Entregado"),
        ("pending", "Pendiente de entrega"),
        ("dispatched", "Despachado por encomienda"),
    ], string="Reparto", compute="_compute_distri_delivery_state")

    @api.depends("distri_stop_ids.delivery_state", "distri_stop_ids.unloaded")
    def _compute_distri_delivery_state(self):
        for order in self:
            stops = order.distri_stop_ids.sorted("id")
            last = stops[-1:]
            if not last:
                order.distri_delivery_state = "unassigned"
            elif last.delivery_state in ("delivered_code", "delivered_no_code"):
                order.distri_delivery_state = "delivered"
            elif last.delivery_state == "dispatched":
                order.distri_delivery_state = "dispatched"
            elif last.delivery_state == "not_delivered":
                order.distri_delivery_state = "pending"
            else:
                order.distri_delivery_state = "on_route"

    @api.depends("distri_receipt_attempts")
    def _compute_distri_receipt_locked(self):
        limit = receipt_code_attempt_limit(self.env)
        for order in self:
            order.distri_receipt_locked = order.distri_receipt_attempts >= limit

    # ---------------------------------------------------- receipt code

    def _distri_ensure_receipt_code(self):
        """The order's code, created once (when the customer places the
        order, or at the end of packing for an order without one). Returns
        the plain code, or "" for an order that only has the hash of a code
        generated before 19.0.1.99.0."""
        self.ensure_one()
        order = self.sudo()
        if order.distri_receipt_hash:
            return order.distri_receipt_code or ""
        return self._distri_generate_receipt_code(
            note="Se generó el código de recibido: el cliente lo ve en la página de su pedido.")

    def _distri_generate_receipt_code(self, note=None):
        """New 4-digit code (the previous one stops working). Returns it.

        `note` replaces the default chatter note (models/pedido_link.py:
        «Enviar código por WhatsApp» says who prepared it)."""
        self.ensure_one()
        code = "%0*d" % (RECEIPT_CODE_DIGITS, secrets.randbelow(10 ** RECEIPT_CODE_DIGITS))
        salt = secrets.token_hex(8)
        self.sudo().write({
            "distri_receipt_salt": salt,
            "distri_receipt_hash": receipt_code_hash(salt, code),
            "distri_receipt_code": code,
            "distri_receipt_date": fields.Datetime.now(),
            "distri_receipt_attempts": 0,
        })
        if note:
            self.sudo().message_post(body=note)
            return code
        sent = self._distri_send_receipt_code(code)
        self.sudo().message_post(body=(
            "Se generó el código de recibido." if sent else
            "Se generó el código de recibido: el cliente lo ve en la página de su pedido."))
        return code

    def _distri_send_receipt_code(self, code):
        """Send the receipt code to the customer by WhatsApp.

        TODO(reparto-whatsapp): Odoo only receives WhatsApp messages
        (decision 9 in docs/distrineuquen-modulo/cobranza/README.md): there is
        no outbound path in models/whatsapp.py and a template message sent
        through the Cloud API is billed by Meta. Until that is decided the
        code is not sent and the administrator regenerates it from the order
        to send it by hand. Return True once a message was actually sent.
        """
        return False

    def _distri_check_receipt_code(self, code):
        """True for the right code. A wrong one counts as a failed try; a
        locked code (audit F12) is never accepted, not even the right one."""
        self.ensure_one()
        order = self.sudo()
        code = str(code or "").strip()
        if not order.distri_receipt_hash or not re.fullmatch(r"\d{%d}" % RECEIPT_CODE_DIGITS, code):
            return False
        if order.distri_receipt_locked:
            return False
        if hmac.compare_digest(
                receipt_code_hash(order.distri_receipt_salt, code), order.distri_receipt_hash):
            return True
        order._distri_add_receipt_attempts(1)
        return False

    def _distri_add_receipt_attempts(self, count):
        """Adds failed tries (also the ones the driver's phone counted
        offline) and notes the lock once it is reached."""
        self.ensure_one()
        order = self.sudo()
        if count <= 0 or not order.distri_receipt_hash:
            return
        was_locked = order.distri_receipt_locked
        order.distri_receipt_attempts += count
        if order.distri_receipt_locked and not was_locked:
            order.message_post(body=(
                "El código de recibido se bloqueó: %d intentos equivocados. Administración "
                "lo desbloquea con «Desbloquear código»." % order.distri_receipt_attempts))

    def action_distri_unlock_receipt_code(self):
        """Administración: the same code works again (the counter restarts)."""
        self.ensure_one()
        if not self.env.user.has_group("distri_pedido.grupo_administracion"):
            raise UserError("Solo administración desbloquea el código de recibido.")
        self.sudo().distri_receipt_attempts = 0
        self.message_post(body="%s desbloqueó el código de recibido." % self.env.user.name)
        return True

    def action_distri_new_receipt_code(self):
        """Shows a new code once, like the web access password."""
        self.ensure_one()
        if not self.env.user.has_group("distri_pedido.grupo_administracion"):
            raise UserError("Solo administración genera el código de recibido.")
        code = self._distri_generate_receipt_code()
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": "Código de recibido de %s" % self.name,
                "message": ("Código: %s\n\nMandáselo al cliente por WhatsApp. No queda "
                            "guardado: si se pierde, generá otro. Si el pedido ya está en "
                            "el celular del repartidor, que actualice la ruta." % code),
                "sticky": True,
                "type": "warning",
            },
        }

    # ------------------------------------------------------- amounts

    def _distri_amount_to_collect(self):
        """The order's part of the balance: invoiced residual, or the total."""
        self.ensure_one()
        if self._distri_facturas_emitidas():
            return self._distri_saldo_facturado()
        return self.amount_total

    def _distri_previous_debt(self):
        """Customer balance without this order's invoices."""
        self.ensure_one()
        if self.partner_id.distri_generic_partner:
            return 0.0
        balance = self.partner_id._distri_account_balance()
        # the order's part of the balance: its invoices minus its returns
        # still to settle (those are already out of the balance)
        return balance - self._distri_saldo_facturado()

    def _distri_route_zone(self):
        """Zone by the customer's city, else by the name prefix."""
        self.ensure_one()
        if self.distri_zona_id:
            return self.distri_zona_id
        return self.env["distri.zona"].sudo().zona_de_ciudad(
            town_from_name(self.partner_id.name))
