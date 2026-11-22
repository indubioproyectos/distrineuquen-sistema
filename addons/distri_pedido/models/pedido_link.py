"""The order's short link for the team, «WhatsApp verificado» and
«Enviar código por WhatsApp» (19.0.1.87.0).

- Every web order gets an unguessable link token (`distri_link_token`). The
  text the customer sends from /pedido ends with «Tu pedido (link personal,
  no lo compartas):» and https://distrineuquen.com/p/<token> on its own line
  (19.0.1.129.5) right below the
  «Pedido S00164 · código 7K3P» line (that line stays untouched: the webhook
  matcher reads it). It is the order's only link (19.0.1.129.3): the manager
  reads the chat in the WhatsApp Business app on his phone and taps it,
  administración/gerencia/sistemas land on the order form
  (controllers/pedido_link.py); anyone else, the customer included, lands on
  the customer's own order page (/pedido/<access_token>).
  The token is letters and digits only, never «-» or «_»: a 4-character
  piece between dashes could be read as an order code by `find_codes`.
- «WhatsApp verificado»: the manager checked the chat and the number the
  customer typed is his. It goes to the customer's WhatsApp, normalized
  (models/ar_phone.py), and the semaphore repaints.
- «Enviar código por WhatsApp»: the order's receipt code
  (models/reparto_cliente.py) in a wa.me link to the customer's chat; a new
  one only when the order has no readable code (19.0.1.99.0).

This file loads last and imports no other model module (sale.order
overrides are built in import order): only the pure helpers of ar_phone.
"""
import hmac
import re
import secrets
import string
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

from .ar_phone import normalize_whatsapp, phone_key

GROUP_ADMIN = "distri_pedido.grupo_administracion"

LINK_TOKEN_ALPHABET = string.ascii_letters + string.digits
LINK_TOKEN_LENGTH = 24  # about 143 bits
_LINK_TOKEN_RE = re.compile(r"^[A-Za-z0-9]{%d}$" % LINK_TOKEN_LENGTH)

# Plain words of an order's status (the /p/ status page until 19.0.1.129.3)
LINK_STATUS = {
    "received": "recibido",
    "packing": "en armado",
    "on_the_way": "en camino",
    "delivered": "entregado",
    "cancelled": "cancelado",
}

RECEIPT_WA_TEXT = ("Hola, te escribimos de Distri Neuquén. Tu código de recibido del pedido %s "
                   "es %s. Dáselo al repartidor cuando recibas el pedido.")
ROUTE_REFRESH_HINT = "El pedido ya está en el celular del repartidor: que actualice la ruta."

LINK_LABEL = "Tu pedido (link personal, no lo compartas):"
LINK_LABEL_WITH_ACCOUNT = "Tu pedido y tu cuenta (link personal, no lo compartas):"


def new_link_token():
    return "".join(secrets.choice(LINK_TOKEN_ALPHABET) for _ in range(LINK_TOKEN_LENGTH))


def valid_link_token(token):
    return bool(_LINK_TOKEN_RE.match(str(token or "")))


def wa_number_digits(value):
    """Digits for wa.me of a number that reads for sure, else ""."""
    try:
        number = normalize_whatsapp(value)
    except ValueError:
        return ""
    return number[1:] if number else ""


def can_manage(env):
    return env.su or env.user.has_group(GROUP_ADMIN)


class SaleOrderLink(models.Model):
    _inherit = "sale.order"

    distri_link_token = fields.Char(
        string="Link del pedido", copy=False, readonly=True, index=True,
        groups=GROUP_ADMIN,
        help="Parte final de /p/... que va en el WhatsApp del cliente: abre el pedido.")
    distri_wa_ya_verificado = fields.Boolean(
        string="WhatsApp ya verificado", compute="_compute_distri_wa_ya_verificado")
    distri_receipt_wa_digits = fields.Char(
        string="WhatsApp para el código", compute="_compute_distri_receipt_wa_digits")

    _distri_link_token_unique = models.Constraint(
        "UNIQUE(distri_link_token)", "Ese link de pedido ya existe.")

    # ------------------------------------------------------------ computes

    @api.depends("partner_id", "partner_id.distri_generic_partner",
                 "partner_id.commercial_partner_id.distri_whatsapp",
                 "partner_id.commercial_partner_id.distri_whatsapp_confirmed",
                 "distri_telefono", "distri_telefono_verificado")
    def _compute_distri_wa_ya_verificado(self):
        for order in self:
            sudo = order.sudo()
            partner = sudo.partner_id
            if not partner:
                done = False
            elif partner.distri_generic_partner:
                done = sudo.distri_telefono_verificado
            else:
                customer = partner.commercial_partner_id
                typed = sudo.distri_telefono
                done = bool(customer.distri_whatsapp_confirmed and customer.distri_whatsapp and (
                    not typed or phone_key(typed) == phone_key(customer.distri_whatsapp)))
            order.distri_wa_ya_verificado = done

    @api.depends("partner_id", "partner_id.distri_generic_partner", "partner_id.distri_whatsapp",
                 "partner_id.commercial_partner_id.distri_whatsapp",
                 "distri_telefono", "distri_wa_phone")
    def _compute_distri_receipt_wa_digits(self):
        for order in self:
            order.distri_receipt_wa_digits = order._distri_receipt_wa_number() or False

    # ------------------------------------------------------------ link token

    def _distri_wa_start(self, staff=False):
        result = super()._distri_wa_start(staff=staff)
        if not staff:
            self._distri_link_token_ensure()
        return result

    def _distri_link_token_ensure(self):
        """The order's token, created the first time it is needed (orders
        older than 19.0.1.87.0 get it when their message is built)."""
        for order in self.sudo().filtered(lambda o: not o.distri_link_token):
            order.distri_link_token = new_link_token()
        return self.sudo().distri_link_token if len(self) == 1 else False

    def _distri_link_url(self):
        self.ensure_one()
        base = self.env["ir.config_parameter"].sudo().get_param("web.base.url") or ""
        return "%s/p/%s" % (base.rstrip("/"), self._distri_link_token_ensure())

    def _distri_link_line(self, label=None):
        """The end of the order's WhatsApp text: the label, then the order's
        only link on its own line (19.0.1.129.5). «Enviar al cliente» passes
        its own label when the customer also sees his account (19.0.1.129.6)."""
        self.ensure_one()
        return "%s\n%s" % (label or LINK_LABEL, self._distri_link_url())

    def _distri_link_customer_url(self):
        """Where /p/ sends anyone outside the team: «Tu pedido»."""
        self.ensure_one()
        order = self.sudo()
        order._portal_ensure_token()
        return "/pedido/%s" % order.access_token

    @api.model
    def _distri_link_find(self, token):
        """The order of a /p/ token, or an empty recordset. The format is
        checked first and the stored value is compared in constant time."""
        Order = self.sudo().with_context(active_test=False)
        if not valid_link_token(token):
            return Order.browse()
        order = Order.search([("distri_link_token", "=", token)], limit=1)
        if not order or not hmac.compare_digest(order.distri_link_token or "", token):
            return Order.browse()
        return order

    def _distri_link_status(self):
        """recibido / en armado / en camino / entregado / cancelado."""
        self.ensure_one()
        order = self.sudo()
        if order.state == "cancel":
            key = "cancelled"
        elif order.distri_delivery_state == "delivered":
            key = "delivered"
        elif order.distri_delivery_state == "dispatched" or any(
                stop.state == "pending" and stop.route_id.state == "on_route"
                for stop in order.distri_stop_ids):
            key = "on_the_way"
        elif order.distri_armado in ("en_armado", "armado"):
            key = "packing"
        else:
            key = "received"
        return LINK_STATUS[key]

    @api.model
    def _distri_link_is_team(self, user):
        """Administración, gerencia or a system administrator (internal)."""
        if not user or user._is_public() or user.share:
            return False
        return user.has_group(GROUP_ADMIN) or user.has_group("base.group_system")

    def _distri_link_backend_url(self):
        self.ensure_one()
        return "/odoo/action-distri_pedido.action_pedidos_revisar/%s" % self.id

    # ------------------------------------------------------------ «WhatsApp verificado»

    def _distri_check_manage(self, message):
        if not can_manage(self.env):
            raise UserError(message)

    def action_distri_wa_verificado(self):
        """The manager checked the chat: the number typed on the order is the
        customer's. Without a typed number, the WhatsApp dialog asks for it."""
        self.ensure_one()
        self._distri_check_manage("Solo administración y gerencia marcan el WhatsApp como "
                                  "verificado.")
        if self.state == "cancel":
            raise UserError("El pedido %s está cancelado." % self.name)
        typed = (self.sudo().distri_telefono or "").strip()
        if not typed:
            return {
                "type": "ir.actions.act_window",
                "name": "WhatsApp verificado",
                "res_model": "distri.wa.confirm.wizard",
                "view_mode": "form",
                "target": "new",
                "context": {"default_order_id": self.id, "default_then": "verify"},
            }
        self._distri_wa_mark_verified(typed)
        return True

    def _distri_wa_mark_verified(self, value):
        """Stores `value` as the customer's checked WhatsApp (normalized; an
        ambiguous number is refused with the usual error)."""
        self.ensure_one()
        self._distri_check_manage("Solo administración y gerencia marcan el WhatsApp como "
                                  "verificado.")
        try:
            number = normalize_whatsapp(value)
        except ValueError as error:
            raise UserError(str(error)) from None
        if not number:
            raise UserError("Falta el número de WhatsApp.")
        user = self.env.user
        order = self.sudo()
        if order.partner_id.distri_generic_partner:
            # no customer yet: the typed data of the order keeps it
            order.write({"distri_telefono": number, "distri_telefono_verificado": True})
            order.message_post(body="%s verificó el WhatsApp del cliente en el chat: %s." % (
                user.name, number))
            return number
        partner = order.partner_id.commercial_partner_id
        previous = partner.distri_whatsapp
        partner.write({
            "distri_whatsapp": number,
            "distri_whatsapp_confirmed": True,
            "distri_whatsapp_date": fields.Datetime.now(),
            "distri_whatsapp_user_id": user.id,
        })
        note = "%s verificó el WhatsApp del cliente en el chat: %s." % (user.name, number)
        if previous and phone_key(previous) != phone_key(number):
            note += " Reemplaza al anterior: %s." % previous
        order.message_post(body=note)
        partner.message_post(body="WhatsApp verificado por %s con el pedido %s: %s." % (
            user.name, order.name, number))
        return number

    # ------------------------------------------------------------ receipt code by WhatsApp

    def _distri_receipt_wa_number(self):
        """wa.me digits for the receipt code: the customer's WhatsApp on
        file, or the typed phone of an order under a generic contact."""
        self.ensure_one()
        order = self.sudo()
        partner = order.partner_id
        if not partner:
            return ""
        if partner.distri_generic_partner:
            return wa_number_digits(order.distri_wa_phone or order.distri_telefono)
        return wa_number_digits(partner.distri_whatsapp
                                or partner.commercial_partner_id.distri_whatsapp)

    def _distri_on_driver_phone(self):
        """A pending stop of a route with a driver that is not closed: the
        driver's phone already holds the order (and the old code hash)."""
        self.ensure_one()
        return any(stop.state == "pending" and stop.route_id.driver_id
                   and stop.route_id.state in ("draft", "on_route")
                   for stop in self.sudo().distri_stop_ids)

    def action_distri_receipt_code_whatsapp(self):
        """«Enviar código por WhatsApp»: a new receipt code in the
        customer's chat, ready to send from the user's WhatsApp."""
        self.ensure_one()
        if not self.env.user.has_group(GROUP_ADMIN):
            raise UserError("Solo administración genera el código de recibido.")
        digits = self._distri_receipt_wa_number()
        if not digits:
            raise UserError("El cliente no tiene un WhatsApp cargado para mandarle el código.")
        # 19.0.1.99.0: the code the customer already sees on his order page
        # is sent as is; a new one only for an order without a readable code
        code = self.sudo().distri_receipt_code
        if code:
            self.sudo().message_post(
                body="Código de recibido preparado para enviar por WhatsApp por %s." % (
                    self.env.user.name))
        else:
            note = "Código de recibido nuevo, preparado para enviar por WhatsApp por %s." % (
                self.env.user.name)
            if self._distri_on_driver_phone():
                note += " " + ROUTE_REFRESH_HINT
            code = self._distri_generate_receipt_code(note=note)
        return {
            "type": "ir.actions.act_url",
            "url": "https://wa.me/%s?text=%s" % (digits, quote(RECEIPT_WA_TEXT % (self.name, code))),
            "target": "new",
        }


class DistriWaConfirmWizardVerify(models.TransientModel):
    """The «WhatsApp del cliente» dialog, also for «WhatsApp verificado»
    when the order has no typed number: it stores it, never confirms."""
    _inherit = "distri.wa.confirm.wizard"

    then = fields.Selection(selection_add=[("verify", "WhatsApp verificado")],
                            ondelete={"verify": "cascade"})

    def action_save_confirm(self):
        if self.then != "verify":
            return super().action_save_confirm()
        self.ensure_one()
        self.order_id._distri_wa_mark_verified(self._canonical())
        return {"type": "ir.actions.act_window_close"}

    def action_skip(self):
        if any(wizard.then == "verify" for wizard in self):
            raise UserError("Escribí el número que verificaste en el chat.")
        return super().action_skip()
