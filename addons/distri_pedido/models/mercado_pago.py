"""Mercado Pago Checkout Pro integration (own integration, no payment provider).

The customer pays the ORDER, not a partner: web orders sit on generic
partners until an admin assigns the real customer. Each order gets an
unguessable `distri_mp_reference` that travels as `external_reference`, and
every payment reported by Mercado Pago is re-fetched from its API before
anything is recorded (the notification body is never trusted).
"""
import logging
import secrets
from datetime import datetime, timezone

import requests
from psycopg2 import IntegrityError

from odoo import api, fields, models

_logger = logging.getLogger(__name__)

# Fixed API host: never build a URL from anything a notification sends (SSRF).
MP_API = "https://api.mercadopago.com"
MP_TIMEOUT = 10
MP_CURRENCY = "ARS"
STATEMENT_DESCRIPTOR = "DISTRINEUQUEN"

PARAM_TOKEN = "distri_pedido.mp_access_token"
# distri_pedido.mp_public_key is not used: Checkout Pro is created on the
# server with the access token (19.0.1.37.0, T9). The parameter can be deleted.
PARAM_WEBHOOK_SECRET = "distri_pedido.mp_webhook_secret"
PARAM_ENABLED = "distri_pedido.mp_enabled"
# The Checkout Pro link charges a commission, so it is off unless this is set
# (decision of 25/09/2026): customers pay by transfer to the alias. The
# webhook and the payment log still follow PARAM_ENABLED.
PARAM_LINK_ENABLED = "distri_pedido.mp_link_habilitado"
# Lab only (controllers/mp_oauth.py): OAuth test of a read-only token. The
# token it obtains lives apart and is never used to charge.
PARAM_CLIENT_SECRET = "distri_pedido.mp_client_secret"
PARAM_OAUTH_PENDING = "distri_pedido.mp_oauth_pending"
PARAM_OAUTH_READ_TOKEN = "distri_pedido.mp_oauth_read_token"
PARAM_OAUTH_READ_SCOPE = "distri_pedido.mp_oauth_read_scope"
PARAM_OAUTH_READ_USER = "distri_pedido.mp_oauth_read_user_id"

# Statuses that deserve a chatter message on the order when reached.
NOTIFY_STATUSES = {
    "approved": "aprobado",
    "rejected": "rechazado",
    "refunded": "devuelto",
    "charged_back": "con contracargo",
    "cancelled": "cancelado",
}


class MercadoPagoError(Exception):
    """Any failure talking to Mercado Pago. Message never contains the token."""


def _money(amount):
    """Argentine formatting: $ 12.345,67"""
    text = "{:,.2f}".format(amount or 0.0)
    return "$ " + text.replace(",", "X").replace(".", ",").replace("X", ".")


def _parse_mp_datetime(value):
    """MP sends ISO 8601 with offset; Odoo stores naive UTC."""
    if not value:
        return False
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return False
    if parsed.tzinfo:
        parsed = parsed.astimezone(timezone.utc).replace(tzinfo=None)
    return parsed


class MercadoPagoPayment(models.Model):
    _name = "distri.mp.payment"
    _description = "Pago Mercado Pago"
    _order = "create_date desc, id desc"
    _rec_name = "payment_id"

    order_id = fields.Many2one(
        "sale.order", string="Pedido", index=True, ondelete="set null", readonly=True,
    )
    payment_id = fields.Char(string="ID de pago", required=True, readonly=True)
    preference_id = fields.Char(string="Preferencia", readonly=True)
    external_reference = fields.Char(string="Referencia externa", index=True, readonly=True)
    status = fields.Char(string="Estado", readonly=True)
    status_detail = fields.Char(string="Detalle del estado", readonly=True)
    amount = fields.Float(string="Importe", digits=(16, 2), readonly=True)
    expected_amount = fields.Float(
        string="Importe esperado", digits=(16, 2), readonly=True,
        help="Importe de la preferencia al momento de crearla.",
    )
    amount_ok = fields.Boolean(string="Importe y moneda correctos", readonly=True)
    currency = fields.Char(string="Moneda", readonly=True)
    payment_method = fields.Char(string="Medio de pago", readonly=True)
    payment_type = fields.Char(string="Tipo de pago", readonly=True)
    installments = fields.Integer(string="Cuotas", readonly=True)
    payer_email = fields.Char(string="Email del pagador", readonly=True)
    payer_name = fields.Char(string="Nombre del pagador", readonly=True)
    payer_identification = fields.Char(string="Documento del pagador", readonly=True)
    date_created_mp = fields.Datetime(string="Creado en MP", readonly=True)
    date_approved = fields.Datetime(string="Fecha de aprobación", readonly=True)
    source = fields.Selection(
        [("webhook", "Webhook"), ("return", "Regreso del cliente")],
        string="Origen", readonly=True,
    )
    last_source = fields.Selection(
        [("webhook", "Webhook"), ("return", "Regreso del cliente")],
        string="Última actualización por", readonly=True,
    )

    _payment_id_unique = models.Constraint(
        "UNIQUE(payment_id)", "Ese pago de Mercado Pago ya está registrado.",
    )

    # ------------------------------------------------------------ config

    @api.model
    def _mp_param(self, key):
        return self.env["ir.config_parameter"].sudo().get_param(key) or ""

    @api.model
    def _mp_enabled(self):
        enabled = self._mp_param(PARAM_ENABLED).strip().lower()
        return enabled in ("1", "true", "yes") and bool(self._mp_param(PARAM_TOKEN))

    @api.model
    def _mp_link_enabled(self):
        """The "Pagar con Mercado Pago" button and /pagar. Off by default."""
        link = self._mp_param(PARAM_LINK_ENABLED).strip().lower()
        return link in ("1", "true", "yes") and self._mp_enabled()

    @api.model
    def _mp_webhook_secret(self):
        return self._mp_param(PARAM_WEBHOOK_SECRET).strip()

    # --------------------------------------------------------------- HTTP

    @api.model
    def _mp_call(self, method, path, payload=None, idempotency_key=None):
        """Call the fixed MP host. `path` is built only from constants and
        validated ids. Errors are logged without the token."""
        token = self._mp_param(PARAM_TOKEN).strip()
        if not token:
            raise MercadoPagoError("Mercado Pago access token is not configured")
        headers = {
            "Authorization": "Bearer %s" % token,
            "Accept": "application/json",
        }
        if idempotency_key:
            headers["X-Idempotency-Key"] = idempotency_key
        try:
            response = requests.request(
                method, MP_API + path, json=payload, headers=headers,
                timeout=MP_TIMEOUT, allow_redirects=False,
            )
        except requests.RequestException as exc:
            _logger.warning("Mercado Pago %s %s failed: %s", method, path, type(exc).__name__)
            raise MercadoPagoError("network error") from None
        if response.status_code == 404:
            return None
        if not 200 <= response.status_code < 300:
            _logger.warning(
                "Mercado Pago %s %s answered HTTP %s: %s",
                method, path, response.status_code, response.text[:500],
            )
            raise MercadoPagoError("HTTP %s" % response.status_code)
        try:
            return response.json()
        except ValueError:
            _logger.warning("Mercado Pago %s %s returned non-JSON", method, path)
            raise MercadoPagoError("invalid response") from None

    # ------------------------------------------------------------ syncing

    @api.model
    def _mp_sync_payment(self, payment_id, source):
        """Fetch a payment from MP by id and record it against its order.

        Returns the distri.mp.payment record, or an empty recordset when the
        payment does not exist or does not belong to any of our orders.
        Raises MercadoPagoError on transport/API failures (caller decides).
        """
        payment_id = str(payment_id)
        if not payment_id.isdigit() or len(payment_id) > 20:
            return self.browse()
        data = self._mp_call("GET", "/v1/payments/%s" % payment_id)
        if not data or str(data.get("id")) != payment_id:
            _logger.info("Mercado Pago payment %s not found, ignored", payment_id)
            return self.browse()

        reference = data.get("external_reference") or ""
        order = self.env["sale.order"].sudo().search(
            [("distri_mp_reference", "=", reference)], limit=1,
        ) if reference else self.env["sale.order"]
        if not order:
            _logger.info(
                "Mercado Pago payment %s has an unknown external_reference, ignored",
                payment_id,
            )
            return self.browse()

        currency = data.get("currency_id") or ""
        amount = float(data.get("transaction_amount") or 0.0)
        expected = order.distri_mp_amount
        amount_ok = currency == MP_CURRENCY and abs(amount - expected) < 0.005

        payer = data.get("payer") or {}
        ident = payer.get("identification") or {}
        name = " ".join(filter(None, [payer.get("first_name"), payer.get("last_name")]))
        values = {
            "status": data.get("status") or "",
            "status_detail": data.get("status_detail") or "",
            "amount": amount,
            "currency": currency,
            "payment_method": data.get("payment_method_id") or "",
            "payment_type": data.get("payment_type_id") or "",
            "installments": int(data.get("installments") or 0),
            "payer_email": payer.get("email") or "",
            "payer_name": name,
            "payer_identification": " ".join(
                filter(None, [ident.get("type"), ident.get("number")])
            ),
            "date_created_mp": _parse_mp_datetime(data.get("date_created")),
            "date_approved": _parse_mp_datetime(data.get("date_approved")),
            "last_source": source,
        }

        Payment = self.sudo()
        record = Payment.search([("payment_id", "=", payment_id)], limit=1)
        previous_status = record.status if record else None
        if record:
            record.write(values)
        else:
            values.update({
                "payment_id": payment_id,
                "order_id": order.id,
                "preference_id": order.distri_mp_preference_id or "",
                "external_reference": reference,
                "expected_amount": expected,
                "amount_ok": amount_ok,
                "source": source,
            })
            try:
                with self.env.cr.savepoint():
                    record = Payment.create(values)
            except IntegrityError:
                # webhook and customer return raced; the other one won
                record = Payment.search([("payment_id", "=", payment_id)], limit=1)
                previous_status = record.status
                record.write({k: v for k, v in values.items()
                              if k not in ("source", "order_id", "expected_amount")})

        if record.status != previous_status:
            record._mp_notify_order()
        return record

    def _mp_notify_order(self):
        """Chatter message on the order when the payment changes status."""
        for payment in self:
            label = NOTIFY_STATUSES.get(payment.status)
            if not label or not payment.order_id:
                continue
            body = "Pago Mercado Pago #%s %s por %s" % (
                payment.payment_id, label, _money(payment.amount),
            )
            if payment.status == "approved" and not payment.amount_ok:
                body += (
                    " - ATENCION: el importe o la moneda no coinciden con el pedido "
                    "(esperado %s %s). Revisar antes de darlo por cobrado."
                    % (_money(payment.expected_amount), MP_CURRENCY)
                )
            if payment.status in ("refunded", "charged_back"):
                body += " - solo registrado, no se hizo ningún movimiento automático."
            payment.order_id.sudo().message_post(body=body)
            # TODO(reconciliation): once orders are invoiced to the real
            # customer, create the account.payment for approved payments here
            # (and reverse it on refunded/charged_back), then reconcile it
            # against the invoice. Out of scope for this iteration.


class SaleOrder(models.Model):
    _inherit = "sale.order"

    distri_mp_reference = fields.Char(
        string="Referencia Mercado Pago", copy=False, readonly=True, index=True,
        groups="base.group_system",
    )
    distri_mp_preference_id = fields.Char(
        string="Preferencia Mercado Pago", copy=False, readonly=True,
        groups="base.group_system",
    )
    distri_mp_init_point = fields.Char(
        string="Link de pago Mercado Pago", copy=False, readonly=True,
        groups="base.group_system",
    )
    distri_mp_amount = fields.Float(
        string="Importe de la preferencia", copy=False, readonly=True, digits=(16, 2),
        groups="base.group_system",
    )
    distri_mp_payment_ids = fields.One2many(
        "distri.mp.payment", "order_id", string="Pagos Mercado Pago",
        groups="base.group_system,sales_team.group_sale_manager",
    )

    def _distri_mp_approved_payments(self):
        self.ensure_one()
        return self.sudo().distri_mp_payment_ids.filtered(
            lambda p: p.status == "approved" and p.amount_ok
        )

    def _distri_mp_payable(self):
        """Amount is final and can be charged. Editability (cutoff) is decided
        by the controller, which owns that rule."""
        self.ensure_one()
        order = self.sudo()
        if not self.env["distri.mp.payment"]._mp_link_enabled():
            return False
        if order.state == "cancel" or order.amount_total <= 0:
            return False
        if order.currency_id.name != MP_CURRENCY:
            return False
        # lines still "a confirmar" (priced when the customer is assigned):
        # the total is not final yet
        product_lines = order.order_line.filtered(lambda l: not l.display_type)
        if any(not line.price_unit for line in product_lines):
            return False
        return not order._distri_mp_approved_payments()

    def _distri_mp_checkout_url(self, base_url):
        """Return a Checkout Pro init_point for the current order amount,
        reusing the open preference when the amount did not change."""
        self.ensure_one()
        order = self.sudo()
        amount = round(order.amount_total, 2)
        if (order.distri_mp_preference_id and order.distri_mp_init_point
                and abs(order.distri_mp_amount - amount) < 0.005):
            return order.distri_mp_init_point

        if not order.distri_mp_reference:
            order.distri_mp_reference = "DN-%s" % secrets.token_hex(16)
        order._portal_ensure_token()
        back = "%s/pedido/%s" % (base_url, order.access_token)
        payload = {
            "items": [{
                "id": order.name,
                "title": "Pedido %s - Distri Neuquén" % order.name,
                "quantity": 1,
                "unit_price": amount,
                "currency_id": MP_CURRENCY,
            }],
            "external_reference": order.distri_mp_reference,
            "back_urls": {
                "success": back + "?mp=vuelta",
                "failure": back + "?mp=vuelta",
                "pending": back + "?mp=vuelta",
            },
            "auto_return": "approved",
            # source_news=webhooks: only Webhooks notifications, not legacy IPN
            "notification_url": "%s/pedido/mp/webhook?source_news=webhooks" % base_url,
            "statement_descriptor": STATEMENT_DESCRIPTOR,
        }
        # same reference + same amount => same key, so a retried request never
        # creates a second preference
        key = "%s-%d" % (order.distri_mp_reference, int(round(amount * 100)))
        data = self.env["distri.mp.payment"]._mp_call(
            "POST", "/checkout/preferences", payload=payload, idempotency_key=key,
        )
        init_point = (data or {}).get("init_point")
        if not init_point or not init_point.startswith("https://"):
            raise MercadoPagoError("preference without init_point")
        order.write({
            "distri_mp_preference_id": data.get("id") or "",
            "distri_mp_init_point": init_point,
            "distri_mp_amount": amount,
        })
        return init_point
