"""The customer uploads his transfer receipt from his order page
(19.0.1.103.0, audit proposal 3a step 3).

/pedido/<token> (controllers/main.py `subir_comprobante`) shows «Subir
comprobante de transferencia» while paying the order makes sense
(`sale.order._distri_customer_upload_open`): not cancelled, not still
waiting for the customer's WhatsApp, not already paid. One photo (JPG, PNG,
WEBP) or PDF of at most CUSTOMER_MAX_BYTES per post, an optional amount.

The file becomes the same pending receipt «Subir comprobante» creates
(models/comprobante_subir.py `_distri_upload_create`, background reading,
duplicate check by the original bytes and the refused attempt), with these
differences:

- nobody «forwarded» it: `distri_forwarded_by_id` stays empty, the profile is
  «Subido por el cliente» and `distri_customer_order_id` is the order;
- the customer is the order's customer (source «Subido por el cliente desde
  su pedido Sxxxxx»); an order still under the generic contact names no
  customer and the receipt's text carries what the buyer typed (name and
  phone), so administración picks the customer as with any other receipt;
- a file already loaded by any channel is refused as «Ya lo teníamos» and
  kept as an attempt of that order (via «cliente»), never showing anything
  of the existing receipt;
- at most `distri_pedido.comprobante_cliente_max_dia` (CUSTOMER_MAX_PER_DAY)
  receipts and attempts per order and Argentine day, and a rate limit per IP
  and session (models/limites.py, CUSTOMER_UPLOAD_LIMITS).

The page lists only the receipts and attempts of THAT order uploaded from
that page, with the state in the customer's words (`_distri_customer_rows`).
The receipt never creates a payment (docs cobranza/README.md, decision 10).
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .comprobante_subir import _ar, _since, receipt_kind
from .reparto import _money

_logger = logging.getLogger(__name__)

CUSTOMER_PROFILE = "Subido por el cliente"
CUSTOMER_VIA = "cliente"
CUSTOMER_MAX_BYTES = 10 * 1024 * 1024
CUSTOMER_MAX_MB = 10
PARAM_MAX_PER_DAY = "distri_pedido.comprobante_cliente_max_dia"
CUSTOMER_MAX_PER_DAY = 20
# /pedido/<token>/comprobante (models/limites.py)
CUSTOMER_UPLOAD_LIMITS = {
    "ip_10min": ("distri_pedido.comprobante_cliente_limite_ip_10min", 20, None),
    "ip_day": ("distri_pedido.comprobante_cliente_limite_ip_dia", 60, None),
    "session_10min": ("distri_pedido.comprobante_cliente_limite_sesion_10min", 8, None),
}
CUSTOMER_LIST_MAX = 30
# what the customer reads of each receipt (_distri_upload_bucket)
CUSTOMER_STATES = {
    "pendiente": "Lo revisa administración",
    "aprobado": "Recibido",
    "rechazado": "No lo pudimos tomar: escribinos",
    "cerrado": "Revisado",
}
CUSTOMER_ATTEMPT_STATE = "Ya lo teníamos"


class CustomerUploadError(UserError):
    """A refused customer upload; `code` is what the page shows
    (controllers/main.py CUSTOMER_UPLOAD_NOTICES)."""

    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def customer_max_per_day(env):
    try:
        value = int(env["ir.config_parameter"].sudo().get_param(PARAM_MAX_PER_DAY)
                    or CUSTOMER_MAX_PER_DAY)
    except ValueError:
        value = CUSTOMER_MAX_PER_DAY
    return max(value, 1)


class SaleOrderCustomerUpload(models.Model):
    _inherit = "sale.order"

    def _distri_customer_upload_open(self):
        """(True, "") while the customer may upload a transfer receipt from
        his order page, else (False, the reason in his words)."""
        self.ensure_one()
        order = self.sudo()
        if order.state == "cancel":
            return False, "El pedido está cancelado."
        if order.state not in ("draft", "sent", "sale"):
            return False, "Este pedido ya no recibe comprobantes. Escribinos por WhatsApp."
        if order.distri_wa_state == "waiting":
            return False, "Primero mandanos el pedido por WhatsApp; después podés subir el comprobante."
        if order._distri_facturas_emitidas():
            currency = order.currency_id or order.company_id.currency_id
            if currency.compare_amounts(order._distri_importe_a_pagar(), 0) <= 0:
                return False, "Este pedido ya está pagado."
        return True, ""


class WhatsappMessageCustomerUpload(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_customer_order_id = fields.Many2one(
        "sale.order", string="Subido por el cliente en el pedido", readonly=True, index=True,
        copy=False, ondelete="set null",
        help="El cliente lo subió desde la página de su pedido.")
    distri_upload_source = fields.Char(
        string="Origen", compute="_compute_distri_upload_source",
        help="Subido por el cliente desde la página de su pedido, o por el personal desde "
             "«Subir comprobante».")

    @api.depends("distri_customer_order_id", "distri_upload")
    def _compute_distri_upload_source(self):
        for msg in self:
            if msg.distri_customer_order_id:
                msg.distri_upload_source = CUSTOMER_PROFILE
            elif msg.distri_upload:
                msg.distri_upload_source = "Subido por el personal"
            else:
                msg.distri_upload_source = False

    def _distri_upload_channel(self, user=None):
        if self.sudo().distri_customer_order_id:
            return CUSTOMER_PROFILE
        return super()._distri_upload_channel(user=user)

    # ------------------------------------------------------------ upload

    @api.model
    def _distri_customer_today(self, order):
        """Receipts and refused attempts of `order` from its page today."""
        since = _since(1)
        msgs = self.sudo().with_context(active_test=False).search_count([
            ("distri_customer_order_id", "=", order.id), ("create_date", ">=", since)])
        attempts = self.env["distri.receipt.upload.attempt"].sudo().search_count([
            ("order_id", "=", order.id), ("attempted_at", ">=", since)])
        return msgs + attempts

    @api.model
    def _distri_customer_upload(self, order, name, raw, amount_text=""):
        """One receipt the customer uploads from his order page. Returns
        {"status": "nuevo" | "repetido", "id"}; raises CustomerUploadError
        (with its `code`) when refused. The caller found the order by its
        token and counted the rate limit."""
        order = order.sudo()
        is_open, reason = order._distri_customer_upload_open()
        if not is_open:
            raise CustomerUploadError("estado", reason)
        name = (name or "comprobante").replace("/", "_").replace("\\", "_")[:120]
        if not raw:
            raise CustomerUploadError("falta", "Elegí la foto o el PDF del comprobante.")
        if len(raw) > CUSTOMER_MAX_BYTES:
            raise CustomerUploadError("grande", "Es demasiado grande (más de %d MB)." %
                                      CUSTOMER_MAX_MB)
        kind = receipt_kind(raw)
        if not kind:
            raise CustomerUploadError("tipo", "No es una foto (JPG, PNG o WEBP) ni un PDF.")
        if self._distri_customer_today(order) >= customer_max_per_day(self.env):
            raise CustomerUploadError("muchos", "Ya subiste varios comprobantes hoy.")
        try:
            typed = self._distri_typed_amount(amount_text, name)
        except UserError as exc:
            raise CustomerUploadError("importe", exc.args[0] if exc.args else "") from None
        user = self.env.user
        existing = self._distri_upload_duplicate(raw)
        if existing:
            attempt = self.env["distri.receipt.upload.attempt"].sudo().create({
                "user_id": user.id, "file_name": name, "message_id": existing.id,
                "via": CUSTOMER_VIA, "order_id": order.id, "amount_typed": typed or 0.0})
            _logger.info("Customer receipt %s for %s refused: already loaded as %s",
                         name, order.name, existing.id)
            return {"status": "repetido", "id": False, "attempt_id": attempt.id}
        partner = order.partner_id
        customer = partner.commercial_partner_id if partner and \
            not partner.distri_generic_partner else self.env["res.partner"]
        body = "Comprobante del pedido %s, subido por el cliente desde la página del pedido" % (
            order.name)
        if not customer:
            typed_data = [v for v in (order._distri_nombre_web(), order.distri_telefono) if v]
            if typed_data:
                body += " · escribió: %s" % " · ".join(typed_data)
        extra = {
            "profile_name": CUSTOMER_PROFILE,
            "distri_forwarded_by_id": False,
            "distri_customer_order_id": order.id,
            "distri_order_ref": order.name,
            "distri_partner_source": ("Subido por el cliente desde su pedido %s" % order.name)
            if customer else False,
            "body": body,
        }
        msg = self._distri_upload_create(name, raw, kind, user, customer, CUSTOMER_VIA, typed,
                                         0, extra=extra)
        self.env["distri.route.expense"].sudo()._trigger_read()
        order.message_post(body="El cliente subió un comprobante de transferencia desde la "
                                "página del pedido (%s)." % name)
        return {"status": "nuevo", "id": msg.id}

    @api.model
    def _distri_customer_rows(self, order):
        """The rows the order page lists: only that order's uploads from its
        page and the refused attempts, newest first; the customer's words,
        nothing read from the file nor of other receipts."""
        Message = self.sudo().with_context(active_test=False)
        rows = []
        for msg in Message.search([("distri_customer_order_id", "=", order.id)],
                                  order="id desc", limit=CUSTOMER_LIST_MAX):
            bucket = msg._distri_upload_bucket()
            rows.append({
                "name": msg.media_filename or "comprobante",
                "date": _ar(msg.create_date, "%d/%m %H:%M"),
                "amount": _money(msg.distri_amount_typed) if msg.distri_amount_typed else "",
                "state": CUSTOMER_STATES[bucket],
                "kind": bucket,
                "ts": msg.create_date.isoformat() if msg.create_date else "",
            })
        for attempt in self.env["distri.receipt.upload.attempt"].sudo().search(
                [("order_id", "=", order.id)], order="id desc", limit=CUSTOMER_LIST_MAX):
            rows.append({
                "name": attempt.file_name or "comprobante",
                "date": _ar(attempt.attempted_at, "%d/%m %H:%M"),
                "amount": _money(attempt.amount_typed) if attempt.amount_typed else "",
                "state": CUSTOMER_ATTEMPT_STATE,
                "kind": "repetido",
                "ts": attempt.attempted_at.isoformat() if attempt.attempted_at else "",
            })
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows[:CUSTOMER_LIST_MAX]


class DistriReceiptUploadAttemptOrder(models.Model):
    _inherit = "distri.receipt.upload.attempt"

    order_id = fields.Many2one("sale.order", string="Pedido del cliente", readonly=True,
                               index=True, ondelete="cascade",
                               help="El cliente quiso subirlo desde la página de este pedido.")
