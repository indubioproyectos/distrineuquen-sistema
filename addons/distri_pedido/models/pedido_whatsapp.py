"""Web orders confirmed by the customer's WhatsApp, and the seller's tools.

Decision of ale (27/09/2026): every order arrives through WhatsApp, the
reliable contact channel with the customer. An order from the public page
waits in «Esperando el WhatsApp» until the customer's message with the
order code reaches the company number (models/whatsapp.py webhook). While it
waits it is not packed, not routed and not invoiced. Nothing cancels it by
itself: the administrator confirms it by hand (with a reason) or cancels it.

The switch `distri_pedido.pedido_requiere_whatsapp` ("1" by default) turns
the waiting step off while the company number is not connected to Odoo:
web orders then enter directly, and a message with the code still verifies
the customer's phone.

Exceptions: orders taken on /pedido by staff (vendedor, administración,
gerencia) and «Venta en mostrador». The customer of those must have a
WhatsApp on file before the order is confirmed.

Sellers: their phone is stored on the user. A receipt photo sent from a
seller's number is marked «reenviado por» and goes to «Revisar».
"""
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

PARAM_REQUIRE = "distri_pedido.pedido_requiere_whatsapp"
PARAM_COMPANY_NUMBER = "distri_pedido.whatsapp_empresa"
# Number the wa.me links open: only in the setting (distri_pedido.whatsapp_empresa,
# loaded by /srv/odoo-lab/scripts/cargar-datos-empresa.sh or typed in «WhatsApp de
# pedidos»). No real number lives in the code (19.0.1.32.0).
DEFAULT_COMPANY_NUMBER = ""

# No 0/O, 1/I/L: the customer may retype the code.
CODE_ALPHABET = "23456789ABCDEFGHJKMNPQRSTUVWXYZ"
CODE_LENGTH = 4
_CODE_TOKEN_RE = re.compile(r"(?<![A-Za-z0-9])([A-Za-z0-9]{%d})(?![A-Za-z0-9])" % CODE_LENGTH)

WA_STATES = [
    ("waiting", "Esperando el WhatsApp"),
    ("received", "Recibido por WhatsApp"),
    ("manual", "Confirmado a mano"),
    ("direct", "Recibido sin esperar el WhatsApp"),
    ("staff", "Cargado por el personal"),
    ("counter", "Venta en mostrador"),
]

AGE_AMBER = timedelta(hours=2)
AGE_RED = timedelta(hours=24)

STAFF_GROUPS = (
    "distri_pedido.grupo_vendedor",
    "distri_pedido.grupo_administracion",
    "distri_pedido.grupo_gerencia",
)


def only_digits(value):
    return re.sub(r"\D", "", str(value or ""))


def phone_key(value):
    """Last 10 digits: the wa_id carries 54 and the 9 of mobiles, the
    typed phone usually does not."""
    number = only_digits(value)
    return number[-10:] if len(number) >= 8 else ""


def wa_digits(value):
    """Number for a wa.me link (Argentina when no country code)."""
    number = only_digits(value).lstrip("0")
    if len(number) < 8:
        return ""
    if not number.startswith("54"):
        number = "549" + number
    elif len(number) == 12:
        # 54 + area + number: WhatsApp needs the 9 of mobiles
        number = "549" + number[2:]
    return number


def valid_code(token):
    token = token.upper()
    return (len(token) == CODE_LENGTH and all(c in CODE_ALPHABET for c in token)
            and any(c.isdigit() for c in token) and any(c.isalpha() for c in token))


# The page's own line: «Pedido S00164 · código 7K3P» (order name, then code).
_WEB_CODE_RE = re.compile(
    r"(?<![A-Za-z0-9])(S\d{3,})\W{0,6}c[oó]digo\W{0,3}([A-Za-z0-9]{%d})(?![A-Za-z0-9])"
    % CODE_LENGTH, re.IGNORECASE)


def find_web_codes(text):
    """(order name, code) pairs written as the page writes them."""
    found = []
    for name, token in _WEB_CODE_RE.findall(text or ""):
        pair = (name.upper(), token.upper())
        if valid_code(pair[1]) and pair not in found:
            found.append(pair)
    return found


def find_codes(text):
    """Order codes written anywhere in a message, case-insensitive."""
    found = []
    for token in _CODE_TOKEN_RE.findall(text or ""):
        token = token.upper()
        if valid_code(token) and token not in found:
            found.append(token)
    return found


def age_text(delta):
    minutes = int(delta.total_seconds() // 60)
    if minutes < 60:
        return "hace %s min" % max(minutes, 1)
    hours = minutes // 60
    if hours < 48:
        return "hace %s h" % hours
    return "hace %s días" % (hours // 24)


def is_staff(user):
    """Staff taking orders on /pedido: internal user of the order roles."""
    if not user or user._is_public() or user.share:
        return False
    return any(user.has_group(g) for g in STAFF_GROUPS) or user.has_group("base.group_system")


class DistriWhatsappSettings(models.AbstractModel):
    _name = "distri.pedido.whatsapp.settings"
    _description = "Pedidos por WhatsApp: ajustes"

    @api.model
    def requires_whatsapp(self):
        value = (self.env["ir.config_parameter"].sudo().get_param(PARAM_REQUIRE) or "1").strip()
        return value.lower() not in ("0", "false", "no", "off")

    @api.model
    def company_number(self):
        """Digits with the country code, as typed in the setting."""
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_COMPANY_NUMBER)
        number = only_digits(value).lstrip("0")
        return number if len(number) >= 8 else DEFAULT_COMPANY_NUMBER

    @api.model
    def company_url(self, text=""):
        url = "https://wa.me/%s" % self.company_number()
        return url + ("?text=%s" % quote(text) if text else "")


class DistriWhatsappConfig(models.TransientModel):
    """«WhatsApp de pedidos»: the switch and the company number, for
    administración and gerencia (menu Transición AgilPedido)."""
    _name = "distri.pedido.whatsapp.config"
    _description = "WhatsApp de pedidos"

    requiere = fields.Boolean(
        string="Confirmar pedidos por WhatsApp",
        default=lambda self: self.env["distri.pedido.whatsapp.settings"].requires_whatsapp(),
        help="Activado: un pedido de la página espera el WhatsApp del cliente con el código "
             "antes de entrar a la operación. Desactivado: entra directo; el WhatsApp con el "
             "código igual verifica el teléfono.")
    numero = fields.Char(
        string="Número de WhatsApp de la empresa",
        default=lambda self: self.env["distri.pedido.whatsapp.settings"].company_number(),
        help="Con código de país, sin espacios. Ej.: 5492990000000. Es el número que abren "
             "los botones de la página.")

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group("distri_pedido.grupo_administracion"):
            raise UserError("Solo administración o gerencia cambian este ajuste.")
        numero = only_digits(self.numero).lstrip("0")
        if len(numero) < 10:
            raise UserError("El número de WhatsApp de la empresa es muy corto.")
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_REQUIRE, "1" if self.requiere else "0")
        params.set_param(PARAM_COMPANY_NUMBER, numero)
        return {"type": "ir.actions.act_window_close"}


class ResUsersSeller(models.Model):
    _inherit = "res.users"

    distri_whatsapp_vendedor = fields.Char(
        string="WhatsApp de trabajo",
        help="El número del vendedor o del repartidor. Una foto de comprobante que llega desde "
             "este número se marca «reenviado por» esa persona y resuelve el comprobante "
             "pendiente del pedido escrito junto a la foto.")

    @api.model
    def _distri_seller_by_phone(self, number):
        key = phone_key(number)
        if not key:
            return self.browse()
        users = self.sudo().search([("distri_whatsapp_vendedor", "!=", False), ("share", "=", False)])
        return users.filtered(lambda u: phone_key(u.distri_whatsapp_vendedor) == key)[:1]


class SaleOrderWhatsapp(models.Model):
    _inherit = "sale.order"

    distri_wa_state = fields.Selection(
        WA_STATES, string="WhatsApp del pedido", copy=False, index=True, tracking=True)
    distri_wa_code = fields.Char(string="Código del pedido", copy=False, readonly=True,
                                 index=True)
    distri_wa_phone = fields.Char(string="WhatsApp verificado", copy=False, readonly=True,
                                  help="Número desde el que llegó el WhatsApp con el código.")
    distri_telefono_verificado = fields.Boolean(string="Teléfono verificado por WhatsApp",
                                                copy=False, readonly=True)
    distri_wa_message_id = fields.Many2one("distri.whatsapp.message", string="Mensaje recibido",
                                           copy=False, readonly=True, ondelete="set null")
    distri_wa_proposal_id = fields.Many2one(
        "distri.whatsapp.message", string="Mensaje propuesto", copy=False, readonly=True,
        ondelete="set null",
        help="Llegó un WhatsApp sin código desde el teléfono del pedido. El administrador "
             "decide si es la confirmación.")
    distri_wa_proposal_text = fields.Text(related="distri_wa_proposal_id.body",
                                          string="Texto del mensaje propuesto")
    distri_wa_confirmed_at = fields.Datetime(string="Confirmado el", copy=False, readonly=True)
    distri_wa_confirmed_by_id = fields.Many2one("res.users", string="Confirmado por",
                                                copy=False, readonly=True)
    distri_wa_reason = fields.Char(string="Motivo de la confirmación a mano", copy=False,
                                   readonly=True)
    distri_wa_cancel_reason = fields.Char(string="Motivo de la cancelación", copy=False,
                                          readonly=True)
    distri_wa_cancelled_by_id = fields.Many2one("res.users", string="Cancelado por", copy=False,
                                                readonly=True)
    distri_wa_reminded_at = fields.Datetime(string="Último recordatorio", copy=False,
                                            readonly=True)
    distri_wa_reminded_by_id = fields.Many2one("res.users", string="Recordó", copy=False,
                                               readonly=True)
    distri_wa_age = fields.Char(string="Espera", compute="_compute_distri_wa_age")
    distri_wa_age_level = fields.Selection(
        [("normal", "Menos de 2 h"), ("amber", "De 2 a 24 h"), ("red", "Más de 24 h")],
        string="Nivel de espera", compute="_compute_distri_wa_age")

    _distri_wa_code_unique = models.Constraint(
        "UNIQUE(distri_wa_code)", "Ese código de pedido ya existe.")

    @api.depends("create_date", "distri_wa_state")
    def _compute_distri_wa_age(self):
        now = fields.Datetime.now()
        for order in self:
            start = order.create_date or now
            delta = now - start
            order.distri_wa_age = age_text(delta)
            order.distri_wa_age_level = (
                "red" if delta > AGE_RED else "amber" if delta > AGE_AMBER else "normal")

    # ------------------------------------------------------------ rules

    def _distri_wa_waiting(self):
        self.ensure_one()
        return self.distri_wa_state == "waiting" and self.state != "cancel"

    def _distri_armable(self):
        if self.distri_wa_state == "waiting":
            return False
        return super()._distri_armable()

    def _create_invoices(self, grouped=False, final=False, date=None):
        if any(o.distri_wa_state == "waiting" for o in self):
            raise UserError("El pedido espera el WhatsApp del cliente. Confirmalo desde "
                            "«Pedidos sin WhatsApp» antes de facturarlo.")
        return super()._create_invoices(grouped=grouped, final=final, date=date)

    def action_confirm(self):
        for order in self:
            if order.distri_wa_state == "waiting":
                raise UserError(
                    "El pedido %s espera el WhatsApp del cliente. Si el cliente ya avisó por "
                    "otro medio, usá «Confirmar a mano» en «Pedidos sin WhatsApp»." % order.name)
            if order.distri_wa_state in ("staff", "counter") and not order._distri_wa_customer_number():
                raise UserError(
                    "El cliente de %s no tiene un WhatsApp cargado. Cargalo en el contacto "
                    "(campo WhatsApp) antes de confirmar: es el canal de contacto con el cliente."
                    % order.name)
        return super().action_confirm()

    def action_distri_counter_sale(self):
        """«Venta en mostrador» is an exception: the customer is present."""
        for order in self:
            if order.distri_wa_state in (False, "waiting", "direct"):
                order.distri_wa_state = "counter"
        return super().action_distri_counter_sale()

    def _distri_wa_customer_number(self):
        """The customer's WhatsApp on file (typed phone for a generic)."""
        self.ensure_one()
        order = self.sudo()
        if order.partner_id.distri_generic_partner:
            return wa_digits(order.distri_wa_phone or order.distri_telefono)
        partner = order.partner_id
        for value in (partner.distri_whatsapp, partner.phone,
                      partner.commercial_partner_id.distri_whatsapp,
                      partner.commercial_partner_id.phone):
            digits = wa_digits(value)
            if digits:
                return digits
        return ""

    # ------------------------------------------------------------ start

    @api.model
    def _distri_wa_new_code(self):
        for _attempt in range(50):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
            if not valid_code(code):
                continue
            if not self.sudo().with_context(active_test=False).search_count(
                    [("distri_wa_code", "=", code)], limit=1):
                return code
        raise UserError("No se pudo generar el código del pedido. Probá de nuevo.")

    def _distri_wa_start(self, staff=False):
        """Called when /pedido saves a new order."""
        Settings = self.env["distri.pedido.whatsapp.settings"]
        for order in self.sudo():
            if staff:
                order.distri_wa_state = "staff"
                continue
            vals = {"distri_wa_state": "waiting" if Settings.requires_whatsapp() else "direct"}
            if not order.distri_wa_code:
                vals["distri_wa_code"] = self._distri_wa_new_code()
            order.write(vals)

    def _distri_wa_code_line(self):
        self.ensure_one()
        return "Pedido %s · código %s" % (self.name, self.distri_wa_code)

    # ------------------------------------------------------------ incoming

    @api.model
    def _distri_wa_match_message(self, message):
        """A text that reached the company number: confirm the order of the
        code in it, or propose the match when it only comes from the phone
        of a waiting order. Returns the orders touched."""
        message = message.sudo()
        seller = self.env["res.users"]._distri_seller_by_phone(message.wa_id)
        Order = self.sudo()
        codes = find_codes(message.body)
        pairs = []
        if codes and self._distri_agil_forma_de_pedido(message.body):
            # an AgilPedido order («- 2 x Maní 85GR ($ ...)») is full of
            # 4-character tokens: only the page's «Pedido S00164 · código
            # 7K3P» confirms a web order there, never a bare token
            pairs = find_web_codes(message.body)
            codes = [code for _name, code in pairs]
        if codes:
            orders = Order.search([("distri_wa_code", "in", codes)])
            if pairs:
                orders = orders.filtered(lambda o: (o.name.upper(), o.distri_wa_code) in pairs)
            for order in orders:
                order._distri_wa_receive(message, seller)
            if orders:
                return orders
        # an AgilPedido order (agilpedido_wa.py) is a new order, never the
        # confirmation of a waiting one: it does not become a phone proposal
        agil = Order._distri_agil_desde_mensaje(message)
        if agil:
            return agil
        if seller or not message.wa_id:
            return Order.browse()
        key = phone_key(message.wa_id)
        waiting = Order.search([("distri_wa_state", "=", "waiting"), ("state", "!=", "cancel")])
        candidates = waiting.filtered(lambda o: key and key in (
            phone_key(o.distri_telefono),
            "" if o.partner_id.distri_generic_partner
            else phone_key(o.partner_id._distri_whatsapp_shown())))
        if len(candidates) == 1 and candidates.distri_wa_proposal_id != message:
            candidates.distri_wa_proposal_id = message
            candidates.message_post(body=(
                "Llegó un WhatsApp sin el código desde +%s: «%s». Si es la confirmación, "
                "tocá «Confirmar con este mensaje» en «Pedidos sin WhatsApp»."
                % (message.wa_id, (message.body or "")[:200])))
            return candidates
        return Order.browse()

    def _distri_wa_receive(self, message, seller=None):
        self.ensure_one()
        order = self.sudo()
        number = "+%s" % message.wa_id if message.wa_id else ""
        message.distri_order_id = order
        if order.state == "cancel":
            order.message_post(body="Llegó el WhatsApp con el código desde %s, pero el pedido "
                                    "está cancelado." % (number or "un número sin dato"))
            return
        vals = {"distri_wa_message_id": message.id}
        if order.distri_wa_state in ("waiting", "direct"):
            vals.update({"distri_wa_state": "received",
                         "distri_wa_confirmed_at": fields.Datetime.now(),
                         "distri_wa_proposal_id": False})
            body = "Pedido recibido por WhatsApp desde %s." % number
        else:
            body = "Llegó otro WhatsApp con el código del pedido desde %s." % number
        if seller:
            body += " El número es del vendedor %s: no se guarda como WhatsApp del cliente." % (
                seller.name)
        elif number:
            vals["distri_wa_phone"] = number
        order.write(vals)
        order.message_post(body=body)
        if not seller and number:
            order._distri_wa_store_phone()

    def _distri_wa_store_phone(self):
        """The verified number goes to the customer (or to the typed data of
        an order still under a generic contact)."""
        for order in self.sudo().filtered("distri_wa_phone"):
            number = order.distri_wa_phone
            partner = order.partner_id.commercial_partner_id
            if order.partner_id.distri_generic_partner:
                vals = {"distri_telefono_verificado": True}
                if phone_key(order.distri_telefono) != phone_key(number):
                    if order.distri_telefono:
                        order.message_post(body="Teléfono escrito en la página: %s. Queda el "
                                                "verificado por WhatsApp: %s." % (
                                                    order.distri_telefono, number))
                    vals["distri_telefono"] = number
                order.write(vals)
                continue
            if not partner.distri_whatsapp_confirmed or not partner.distri_whatsapp:
                partner.write({
                    "distri_whatsapp": number,
                    "distri_whatsapp_confirmed": True,
                    "distri_whatsapp_date": fields.Datetime.now(),
                    "distri_whatsapp_user_id": False,
                })
                partner.message_post(body="WhatsApp verificado por el pedido %s: %s." % (
                    order.name, number))
            self.env["distri.payer.identity"]._learn(
                "phone", number, partner,
                order.distri_wa_message_id.profile_name or partner.name)

    def write(self, vals):
        result = super().write(vals)
        if "partner_id" in vals:
            # a customer assigned after the WhatsApp arrived gets the number
            self.filtered(lambda o: o.distri_wa_phone and not o.partner_id.distri_generic_partner
                          and o.distri_wa_state in ("received",))._distri_wa_store_phone()
        return result

    @api.model
    def _distri_ensure_presupuestos_menu(self):
        """«Pedidos sin WhatsApp» inside the PRESUPUESTOS app (distri_tema)."""
        root = self.env.ref("distri_tema.menu_presupuestos_raiz", raise_if_not_found=False)
        action = self.env.ref("distri_pedido.action_pedidos_sin_whatsapp")
        if not root:
            return False
        Data = self.env["ir.model.data"].sudo()
        xmlid = "distri_pedido.menu_presupuestos_sin_whatsapp"
        menu = self.env.ref(xmlid, raise_if_not_found=False)
        values = {
            "name": "Pedidos sin WhatsApp",
            "parent_id": root.id,
            "action": "ir.actions.act_window,%s" % action.id,
            "sequence": 2,
            "group_ids": [(6, 0, [self.env.ref("distri_pedido.grupo_administracion").id])],
        }
        if menu:
            menu.sudo().write(values)
            return menu
        menu = self.env["ir.ui.menu"].sudo().create(values)
        # noupdate: a record created by code is not dropped at the next update
        Data.create({"module": "distri_pedido", "name": "menu_presupuestos_sin_whatsapp",
                     "model": "ir.ui.menu", "res_id": menu.id, "noupdate": True})
        return menu

    # ------------------------------------------------------------ admin

    def _distri_wa_check_admin(self):
        if not self.env.user.has_group("distri_pedido.grupo_administracion"):
            raise UserError("Solo administración o gerencia confirman o cancelan estos pedidos.")

    def _distri_wa_reminder_text(self):
        self.ensure_one()
        name = (self.distri_nombre or "").strip()
        return ("Hola%s, recibimos tu pedido %s desde la página. Para confirmarlo, respondé "
                "este mensaje con el código %s. ¡Gracias!" % (
                    " " + name if name else "", self.name, self.distri_wa_code or ""))

    def action_distri_wa_open_chat(self):
        """«Llamar / abrir WhatsApp»: the typed phone, with a reminder."""
        self.ensure_one()
        self._distri_wa_check_admin()
        digits = wa_digits(self.distri_telefono) or self._distri_wa_customer_number()
        if not digits:
            raise UserError("El pedido no tiene un teléfono para escribirle.")
        self.sudo().write({"distri_wa_reminded_at": fields.Datetime.now(),
                           "distri_wa_reminded_by_id": self.env.user.id})
        self.message_post(body="%s abrió el WhatsApp del cliente para recordarle el pedido." %
                               self.env.user.name)
        return {
            "type": "ir.actions.act_url",
            "url": "https://wa.me/%s?text=%s" % (digits, quote(self._distri_wa_reminder_text())),
            "target": "new",
        }

    def _distri_wa_wizard(self, mode):
        self.ensure_one()
        self._distri_wa_check_admin()
        return {
            "type": "ir.actions.act_window",
            "name": "Confirmar a mano" if mode == "confirm" else "Cancelar el pedido",
            "res_model": "distri.pedido.whatsapp.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_order_id": self.id, "default_mode": mode},
        }

    def action_distri_wa_confirm_manual(self):
        return self._distri_wa_wizard("confirm")

    def action_distri_wa_cancel(self):
        return self._distri_wa_wizard("cancel")

    def action_distri_wa_accept_proposal(self):
        """«Confirmar con este mensaje»: the phone-only match, checked."""
        self.ensure_one()
        self._distri_wa_check_admin()
        message = self.distri_wa_proposal_id
        if not message or not self._distri_wa_waiting():
            raise UserError("No hay un mensaje propuesto para este pedido.")
        self._distri_wa_receive(message)
        self.sudo().write({"distri_wa_confirmed_by_id": self.env.user.id,
                           "distri_wa_reason": "Mensaje sin código desde el teléfono del pedido"})
        self.message_post(body="%s confirmó el pedido con el mensaje sin código." %
                               self.env.user.name)
        return True

    def _distri_wa_confirm_manual(self, reason):
        self.ensure_one()
        self._distri_wa_check_admin()
        if not self._distri_wa_waiting():
            raise UserError("El pedido no está esperando el WhatsApp.")
        self.sudo().write({
            "distri_wa_state": "manual",
            "distri_wa_confirmed_at": fields.Datetime.now(),
            "distri_wa_confirmed_by_id": self.env.user.id,
            "distri_wa_reason": reason,
            "distri_wa_proposal_id": False,
        })
        self.message_post(body="Confirmado a mano por %s. Motivo: %s" % (self.env.user.name, reason))

    def _distri_wa_cancel(self, reason):
        self.ensure_one()
        self._distri_wa_check_admin()
        if self.state == "cancel":
            raise UserError("El pedido ya está cancelado.")
        self.sudo().write({"distri_wa_cancel_reason": reason,
                           "distri_wa_cancelled_by_id": self.env.user.id})
        self.action_cancel()
        self.message_post(body="Cancelado por %s. Motivo: %s" % (self.env.user.name, reason))


class DistriPedidoWhatsappWizard(models.TransientModel):
    _name = "distri.pedido.whatsapp.wizard"
    _description = "Confirmar o cancelar un pedido sin WhatsApp"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, readonly=True)
    mode = fields.Selection([("confirm", "Confirmar a mano"), ("cancel", "Cancelar")],
                            required=True, readonly=True)
    reason = fields.Char(string="Motivo", required=True,
                         help="Ej.: el cliente llamó por teléfono, lo pidió en el mostrador, "
                              "pedido repetido.")

    def action_apply(self):
        self.ensure_one()
        reason = (self.reason or "").strip()
        if not reason:
            raise UserError("Escribí el motivo.")
        if self.mode == "confirm":
            self.order_id._distri_wa_confirm_manual(reason)
        else:
            self.order_id._distri_wa_cancel(reason)
        return {"type": "ir.actions.act_window_close"}


class WhatsappMessageOrder(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_order_id = fields.Many2one("sale.order", string="Pedido del código", readonly=True,
                                      index=True, ondelete="set null")
    distri_forwarded_by_id = fields.Many2one(
        "res.users", string="Reenviado por", readonly=True, index=True,
        help="La foto llegó desde el número de un vendedor.")

    @api.model_create_multi
    def create(self, vals_list):
        Users = self.env["res.users"]
        for vals in vals_list:
            if vals.get("direction") == "in" and vals.get("wa_id") and \
                    vals.get("message_type") in ("image", "document"):
                seller = Users._distri_seller_by_phone(vals["wa_id"])
                if seller:
                    vals["distri_forwarded_by_id"] = seller.id
        records = super().create(vals_list)
        for msg in records.filtered(lambda m: m.direction == "in" and m.message_type == "text"):
            try:
                with self.env.cr.savepoint():
                    self.env["sale.order"]._distri_wa_match_message(msg)
            except Exception:
                # never lose the message nor make Meta retry for a bug here
                _logger.exception("WhatsApp message %s: order match failed", msg.id)
        return records
