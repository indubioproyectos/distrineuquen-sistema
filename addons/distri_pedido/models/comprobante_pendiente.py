"""«Comprobantes pendientes»: the driver answers for the receipts of the
transfers he collects (design approved by management on 22/10/2026, docs
cobranza/README.md «Comprobantes en la entrega»).

1. At each visit the driver first claims what the customer owes from the
   days before («Cliente en rojo», behind a setting), then delivers and
   claims the receipt of the transfer on the spot: the customer sends it to
   his WhatsApp and he forwards it to the company.
2. Every transfer of a visit leaves a «Comprobante pendiente» (technical
   model distri.promised.transfer) at the driver's name. It is «Resuelta»
   when the receipt reaches the company by WhatsApp (whoever sends it: the
   driver, the seller or the customer), when the seller, administración or
   gerencia mark it, or when the Mercado Pago report brings the transfer.
3. The driver collects the full amount: a transfer short of what was left
   to collect is charged to him at once (case «Transferencia menor al
   total»). The receipts of a day that did not arrive by the settlement of
   the next day expire («Vencida») and are charged to him (case
   «Comprobante de transferencia no rendido»), unless administración
   verifies the payment in Mercado Pago: confirming the transfer on the
   collections board closes that charge.
4. The same receipt is counted once: a copy (same operation number, or same
   amount, date and time) never resolves a second pending receipt, and a
   copy detected late gives back what it had resolved.
5. A photo forwarded together with a text from the same number («Comprobante
   del pedido S00231», before or after the photo) belongs to that order.

The charges are cases against the route (distri.route.case): the existing
«Casos» of the settlement, so they show up with the cash differences.
"""
import json
import logging
import os
import re
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError

from .cobros import (
    AMOUNT_TOLERANCE, PROMISE_BEFORE, RECEIPT_TEXT_WINDOW, SELLER_NOTE_WINDOW, _ORDER_RE,
    local_when,
)
from .reparto import _money
from .reparto_cliente import AR_TZ, today_ar

_logger = logging.getLogger(__name__)

ADMIN_GROUP = "distri_pedido.grupo_administracion"
SELLER_GROUP = "distri_pedido.grupo_vendedor"

# «Cliente en rojo»: off until the current accounts are brought up to date
# (today the system shows about $104 M of debt that was already paid).
PARAM_RED = "distri_pedido.cliente_en_rojo"

OPEN_STATES = ("open", "overdue")
CLOSED_RECEIPT_STATES = ("dismissed", "duplicate", "outgoing")

RESOLVED_VIA = [
    ("whatsapp", "WhatsApp"),
    ("manual", "Marcado a mano"),
    ("mp", "Reporte Mercado Pago · automático"),
    ("payment", "Pago registrado"),
]

# the charge of each kind of pending item (distri.route.case kind)
CASE_KIND = {"done": "receipt", "unpaid": "unpaid"}

MP_CLOSED_CASE = "Pago verificado en Mercado Pago (%s): no se carga al repartidor."
PAID_CLOSED_CASE = "El pago del pedido %s quedó registrado: no se carga al repartidor."
RECEIPT_CLOSED_CASE = "Llegó el comprobante el %s (%s): no se carga al repartidor."
# 19.0.1.129.52 (owner, 26/11/2026): a pending receipt whose order is already
# paid (e.g. the Mercado Pago transfer was confirmed before the visit synced)
DONE_PAID_CLOSED_CASE = "Factura %s ya pagada: no es un faltante del repartidor."


def red_rule_on(env):
    value = (env["ir.config_parameter"].sudo().get_param(PARAM_RED) or "0").strip().lower()
    return value in ("1", "true", "yes", "si", "sí", "on")


def role_label(user):
    """«vendedor», «administración»… for «Resuelta por Martín (vendedor)»."""
    if not user:
        return ""
    for group, label in (("distri_pedido.grupo_gerencia", "gerencia"),
                         (ADMIN_GROUP, "administración"),
                         (SELLER_GROUP, "vendedor"),
                         ("distri_pedido.grupo_reparto", "repartidor")):
        if user.has_group(group):
            return label
    return ""


def day_label(day):
    return day.strftime("%d/%m") if day else ""


# The wording and rules of the claim messages, shared with the driver's app
# (static/src/reparto/claim.js reads the same file from the route payload).
CLAIM_FILE = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                          "static", "src", "reparto", "claim_texts.json")
with open(CLAIM_FILE, encoding="utf-8") as _handle:
    CLAIM = json.load(_handle)
CLAIM_VOICES = ("driver", "company")


def to_local(moment):
    """Naive UTC datetime -> naive Argentine datetime."""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).replace(tzinfo=None)


def _fill(template, **values):
    return re.sub(r"\{(\w+)\}", lambda m: str(values.get(m.group(1), m.group(0))), template)


def relative_when(moment, now=None):
    """When the goods were delivered, as it is said (Argentina): «hace un
    rato», «hoy a la mañana», «hoy a la tarde», «ayer», «el martes 20/10»
    (within the last 6 days) or «el 14/10». Naive UTC datetimes."""
    if not moment:
        return ""
    now = now or fields.Datetime.now()
    words = CLAIM["when"]
    local, local_now = to_local(moment), to_local(now)
    days = (local_now.date() - local.date()).days
    if days <= 0:
        if now - moment < timedelta(hours=CLAIM["recent_hours"]):
            return words["recent"]
        return words["morning"] if local.hour < CLAIM["afternoon_hour"] else words["afternoon"]
    if days == 1:
        return words["yesterday"]
    if days <= 6:
        return _fill(words["weekday"], weekday=CLAIM["weekdays"][local.weekday()],
                     date=local.strftime("%d/%m"))
    return _fill(words["date"], date=local.strftime("%d/%m"))


def document_of(order):
    """(«Factura 0001-00006281», «el pedido de la factura 0001-00006281»):
    the invoice number, else the remito, else the order."""
    order = order.sudo()
    docs = CLAIM["documents"]
    invoices = order._distri_facturas_emitidas().filtered(lambda i: i.name and i.name != "/")
    if invoices:
        kind, number = "invoice", ", ".join(invoices.mapped("name"))
    else:
        remitos = order._distri_remitos_texto() if hasattr(order, "_distri_remitos_texto") else ""
        kind, number = ("remito", remitos) if remitos else ("order", order.name)
    return _fill(docs[kind][0], number=number), _fill(docs[kind][1], number=number)


def claim_text(items, voice="driver", now=None, company=""):
    """The claim to one customer, from plain items {kind, amount, document,
    phrase, delivered_at (naive UTC), id}: every item, oldest first, with the
    total when there are several. claim.js builds the same text."""
    texts = CLAIM["voices"][voice]
    items = sorted(items, key=lambda i: (i["delivered_at"] or datetime.min, i.get("id") or 0))
    if not items:
        return ""
    if len(items) == 1:
        item = items[0]
        text = _fill(texts["single_unpaid" if item["kind"] == "unpaid" else "single_done"],
                     phrase=item["phrase"], when=relative_when(item["delivered_at"], now),
                     amount=_money(item["amount"]))
    else:
        lines = [_fill(texts["line_unpaid" if i["kind"] == "unpaid" else "line_done"],
                       document=i["document"], when=relative_when(i["delivered_at"], now),
                       amount=_money(i["amount"])) for i in items]
        total = round(sum(round(i["amount"] * 100) for i in items)) / 100.0
        text = "\n".join([texts["multi_head"]] + lines
                         + [_fill(texts["multi_tail"], total=_money(total))])
    return text + _fill(texts.get("signature") or "", company=company or "")


KIND_LABELS = CLAIM["kind_labels"]


def local_day(moment):
    """Naive UTC datetime -> date in Argentina."""
    if not moment:
        return False
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).date()


class DistriComprobantesConfig(models.TransientModel):
    """«Ajustes de comprobantes»: the «Cliente en rojo» switch."""
    _name = "distri.comprobantes.config"
    _description = "Ajustes de comprobantes"

    cliente_en_rojo = fields.Boolean(
        string="Aplicar «Cliente en rojo»",
        default=lambda self: red_rule_on(self.env),
        help="Activado: la visita de un cliente que debe plata o comprobantes de días "
             "anteriores sale en rojo en la app del repartidor, y el cliente ve el aviso en su "
             "pedido. Activarlo recién con las cuentas corrientes al día.")

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP):
            raise UserError("Solo administración o gerencia cambian este ajuste.")
        self.env["ir.config_parameter"].sudo().set_param(
            PARAM_RED, "1" if self.cliente_en_rojo else "0")
        return {"type": "ir.actions.act_window_close"}


class DistriRouteStopComprobante(models.Model):
    _inherit = "distri.route.stop"

    def _distri_due_today(self, returned=None):
        """What the visit has to collect for today's order: the order minus
        what the customer returned."""
        self.ensure_one()
        if returned is None:
            returned = self.returns_amount
        return max((self.order_amount or 0.0) - (returned or 0.0), 0.0)

    def _distri_red_values(self):
        """Snapshot of «Cliente en rojo» for the phone (with the amounts)."""
        self.ensure_one()
        empty = {"red": False, "red_debt": 0.0, "red_receipts": 0}
        if not red_rule_on(self.env) or self.route_id.kind != "route":
            return empty
        order = self.order_id
        partner = order.partner_id.commercial_partner_id
        if not partner or order.partner_id.distri_generic_partner:
            return empty
        Promise = self.env["distri.promised.transfer"].sudo()
        receipts = Promise._distri_owed_receipts(partner, exclude_stop=self)
        debt = max(order._distri_previous_debt()
                   - Promise._distri_waiting_money(partner), 0.0)
        if debt <= AMOUNT_TOLERANCE:
            debt = 0.0
        return {"red": bool(debt or receipts), "red_debt": debt, "red_receipts": len(receipts)}

    def _distri_document_label(self):
        self.ensure_one()
        return document_of(self.order_id)[0]

    def _distri_red_payload(self):
        self.ensure_one()
        if not self.red:
            return None
        partner = self.partner_id.commercial_partner_id
        receipts = self.env["distri.promised.transfer"].sudo()._distri_owed_receipts(
            partner, exclude_stop=self)
        return {
            "debt": self.red_debt,
            "receipts": [{"order": p.order_id.name, "amount": p.amount, "kind": p.kind or "done",
                          "document": document_of(p.order_id)[0],
                          "delivered_at": fields.Datetime.to_string(p.captured_at)
                          if p.captured_at else "",
                          "line": p._distri_item_line(),
                          "date": day_label(p.delivered_day)} for p in receipts],
        }


class DistriRouteComprobante(models.Model):
    _inherit = "distri.route"

    def _app_payload(self):
        payload = super()._app_payload()
        Promise = self.env["distri.promised.transfer"].sudo()
        payload.update({
            "red_rule": red_rule_on(self.env),
            # «Reenviar a la empresa»: the company's WhatsApp
            "company_whatsapp": self.env["distri.pedido.whatsapp.settings"].sudo()
            .company_number(),
            # «Transferencias sin comprobante · a tu cargo» on «Cerrar la ruta»
            "receipts": Promise._distri_driver_payload(self.driver_id),
            # the wording of the claims, for claim.js
            "claim_texts": CLAIM,
        })
        return payload


class DistriPromisedTransferComprobante(models.Model):
    _inherit = "distri.promised.transfer"

    state = fields.Selection(selection_add=[
        ("overdue", "Vencida"),
        ("fulfilled",),
    ], ondelete={"overdue": "set default"})
    route_kind = fields.Selection(related="route_id.kind", string="Tipo de entrega")
    delivered_day = fields.Date(string="Día de la entrega", compute="_compute_delivered_day",
                                store=True, index=True)
    overdue_since = fields.Date(string="Vencida desde", readonly=True, copy=False)
    status_label = fields.Char(string="Situación", compute="_compute_status_label")
    resolved_at = fields.Datetime(string="Resuelta el", readonly=True, copy=False)
    resolved_by_id = fields.Many2one("res.users", string="Resuelta por (usuario)", readonly=True,
                                     copy=False)
    resolved_by_label = fields.Char(string="Resuelta por", readonly=True, copy=False)
    resolved_via = fields.Selection(RESOLVED_VIA, string="Vía", readonly=True, copy=False)
    receipt_id = fields.Many2one("distri.whatsapp.message", string="Comprobante recibido",
                                 readonly=True, copy=False, ondelete="set null", index=True)
    fulfilled_by_id = fields.Many2one("distri.mp.collection", string="Transferencia confirmada",
                                      readonly=True, copy=False, ondelete="set null")
    charge_case_id = fields.Many2one("distri.route.case", string="Cargo al repartidor",
                                     readonly=True, copy=False, ondelete="set null")
    # «Pedido sin cobrar» (19.0.1.27.0): the amount follows what is still
    # unpaid on the order's invoice, never above what was left unpaid
    original_amount = fields.Monetary(string="Importe original", readonly=True, copy=False)
    amount_history_ids = fields.One2many("distri.promised.transfer.history", "promise_id",
                                         string="Historial del importe", readonly=True)
    partial_note = fields.Char(
        string="Pago parcial", readonly=True,
        help="Llegó un comprobante o una transferencia del cliente que no cubre este importe.")
    # kept from the promised transfers: the reminder's «CUIT conocido» column
    state_changed_at = fields.Datetime(string="Cambió de estado", readonly=True)

    @api.depends("captured_at")
    def _compute_delivered_day(self):
        for promise in self:
            promise.delivered_day = local_day(promise.captured_at)

    @api.depends("state", "overdue_since", "resolved_by_label", "resolved_at")
    def _compute_status_label(self):
        for promise in self:
            if promise.state == "overdue":
                promise.status_label = "Vencida desde %s" % day_label(promise.overdue_since)
            elif promise.state == "fulfilled":
                promise.status_label = "Resuelta · %s" % (promise.resolved_by_label or "")
            else:
                promise.status_label = dict(self._fields["state"].selection).get(
                    promise.state, "")

    # ------------------------------------------------------------ the visit

    @api.model
    def _distri_sync_from_stop(self, stop, captured):
        """After a visit is recorded: its pending receipt, the charge for a
        short transfer and the «Cliente en rojo» check."""
        promise = self.sudo().search([("stop_id", "=", stop.id)], limit=1)
        vals = False
        if stop.transfer == "yes" and stop.transfer_amount > 0:
            vals = {"kind": "done", "receipt_answer": stop.receipt_answer,
                    "amount": stop.transfer_amount}
        elif stop.state == "not_paid" and stop.route_id.kind == "route" \
                and stop._distri_due_today() > AMOUNT_TOLERANCE:
            # «No pagó nada»: the order's unpaid total is at the driver's name
            # (decision of ale, 22/10/2026). Not at the depot.
            vals = {"kind": "unpaid", "receipt_answer": False,
                    "amount": stop._distri_due_today(),
                    "original_amount": stop._distri_due_today()}
        if vals:
            vals.update(comment=stop.comment, captured_at=captured)
            if promise and promise.kind != vals["kind"] and promise.state != "open":
                vals.pop("kind")
            if promise:
                promise.write(vals)
            else:
                promise = self.sudo().create(dict(vals, stop_id=stop.id))
            if promise.state == "open":
                # a receipt that arrived before the visit was synced
                promise._distri_resolve_from_waiting_receipts()
                promise._distri_check_paid_items()
                promise._distri_resolve_done_paid()
        elif promise and promise.state == "open" and not promise.charge_case_id:
            promise.unlink()
        self._distri_charge_short(stop)
        self._distri_check_red(stop)

    @api.model
    def _distri_charge_short(self, stop):
        """«Exige el total»: a transfer that leaves part of today's order
        unpaid is charged to the driver at once."""
        Case = self.env["distri.route.case"].sudo()
        case = Case.search([("stop_id", "=", stop.id), ("kind", "=", "short"),
                            ("state", "=", "open")], limit=1)
        short = 0.0
        if stop.route_id.kind == "route" and stop.transfer == "yes":
            short = stop._distri_due_today() - stop.cash_amount - stop.transfer_amount
        if short <= AMOUNT_TOLERANCE:
            if case and not case.replenished:
                case.unlink()
            return Case
        text = "%s · %s: transfirió %s%s y el pedido de hoy era de %s. La diferencia de %s " \
               "queda a cargo del repartidor." % (
                   stop.partner_id._distri_app_name(), stop.order_id.name,
                   _money(stop.transfer_amount),
                   " más %s en efectivo" % _money(stop.cash_amount) if stop.cash_amount else "",
                   _money(stop._distri_due_today()), _money(short))
        vals = {"amount": -short, "candidates": text}
        if case:
            case.write(vals)
        else:
            case = Case.create(dict(vals, route_id=stop.route_id.id, kind="short",
                                    stop_id=stop.id))
        return case

    @api.model
    def _distri_check_red(self, stop):
        """Delivering to a customer in red without collecting what he owed
        from the days before is a serious fault of the driver."""
        # 19.0.1.129.22: «Ya estaba pagado» owes nothing for today's order,
        # only what was owed from the days before
        today = 0.0 if stop.state == "already_paid" else stop._distri_due_today()
        breach = bool(
            stop.red and stop.state in ("paid", "not_paid", "already_paid")
            and stop.delivery_state in ("delivered_code", "delivered_no_code")
            and stop.cash_amount + stop.transfer_amount + AMOUNT_TOLERANCE
            < today + stop.red_debt)
        if breach and not stop.red_breach:
            stop.route_id.message_post(body=(
                "Falta grave: se entregó a %s (%s) en rojo sin cobrar lo que debía de días "
                "anteriores (%s)." % (stop.partner_id._distri_app_name(), stop.order_id.name,
                                     _money(stop.red_debt))))
        if breach != stop.red_breach:
            stop.red_breach = breach

    # ------------------------------------------------------------ owed

    @api.model
    def _distri_owed_receipts(self, partner, exclude_stop=None):
        """Receipts of the customer still missing from the days before."""
        domain = [("partner_id", "child_of", partner.id), ("state", "in", OPEN_STATES),
                  ("delivered_day", "<", today_ar())]
        if exclude_stop:
            domain.append(("stop_id", "!=", exclude_stop.id))
        return self.sudo().search(domain, order="captured_at, id")

    @api.model
    def _distri_waiting_money(self, partner):
        """Receipts that already arrived and whose transfer the Mercado Pago
        report has not brought yet: the account still shows them as debt."""
        return sum(self.sudo().search([
            ("partner_id", "child_of", partner.id), ("state", "=", "fulfilled"),
            ("fulfilled_by_id", "=", False), ("resolved_via", "!=", "mp"),
        ]).mapped("amount"))

    @api.model
    def _distri_driver_payload(self, driver):
        """The driver's receipts for «Cerrar la ruta»: pending and expired
        ones, and the ones resolved today."""
        today = today_ar()
        promises = self.sudo().search([
            ("driver_id", "=", driver.id), ("route_kind", "=", "route"),
            "|", ("state", "in", OPEN_STATES),
            "&", ("state", "=", "fulfilled"), ("delivered_day", "=", today),
        ], order="captured_at, id")
        out = []
        for promise in promises:
            delivered = promise.captured_at
            local = delivered.replace(tzinfo=timezone.utc).astimezone(AR_TZ) if delivered else None
            document, _phrase = document_of(promise.order_id)
            out.append({
                "stop_id": promise.stop_id.id,
                "partner_id": promise.partner_id.commercial_partner_id.id,
                "document": document,
                "delivered_at": (fields.Datetime.to_string(promise.captured_at)
                                 if promise.captured_at else ""),
                "line": promise._distri_item_line(),
                "order": promise.order_id.name,
                "name": promise.partner_id._distri_app_name(),
                "amount": promise.amount,
                "delivered": ("Entregado hoy %s" % local.strftime("%H:%M")
                              if local and local.date() == today
                              else "Entregado el %s" % day_label(promise.delivered_day)),
                "state": promise.state,
                "kind": promise.kind or "done",
                "overdue_since": day_label(promise.overdue_since),
                "resolved_by": (promise.resolved_by_label or "").split(" (")[0],
                "whatsapp": promise._whatsapp_digits(),
                "claim": self._distri_customer_claim(promises.filtered(
                    lambda p: p.state in OPEN_STATES and p.partner_id.commercial_partner_id
                    == promise.partner_id.commercial_partner_id)) or
                promise._distri_driver_claim_text(),
            })
        return out

    def _distri_item_line(self, now=None):
        """«Faltan $ 40.000 · Factura 0001-00006281 · entregado ayer»."""
        self.ensure_one()
        document, _phrase = document_of(self.order_id)
        return _fill(CLAIM["item_line"], amount=_money(self.amount), document=document,
                     when=relative_when(self.captured_at, now))

    def _distri_claim_items(self):
        """The plain items claim_text() and claim.js take."""
        out = []
        for item in self.sudo():
            document, phrase = document_of(item.order_id)
            out.append({"id": item.id, "kind": item.kind or "done", "amount": item.amount,
                        "document": document, "phrase": phrase,
                        "delivered_at": item.captured_at, "state": item.state})
        return out

    @api.model
    def _distri_customer_claim(self, items, now=None, voice="driver"):
        """The WhatsApp to one customer: every open item, oldest first, with
        the total when there are several. «driver»: the driver writes
        («te entregué»); «company»: vendedor, administración or gerencia from
        Odoo («te entregamos», signed with the company)."""
        items = items.filtered(lambda p: p.state in OPEN_STATES)
        company = self.env.company.name if voice == "company" else ""
        return claim_text(items._distri_claim_items(), voice, now, company)

    def _distri_driver_claim_text(self):
        self.ensure_one()
        return self._distri_customer_claim(self)

    # ------------------------------------------------------------ expiry

    @api.model
    def _distri_expire_for_driver(self, driver):
        """At the settlement: the driver's receipts of the days before today
        that never arrived."""
        expired = self.sudo().search([
            ("driver_id", "=", driver.id), ("route_kind", "=", "route"),
            ("state", "=", "open"), ("delivered_day", "<", today_ar()),
        ])
        expired = expired - expired._distri_check_paid_items() - expired._distri_resolve_done_paid()
        expired._distri_expire()
        return expired

    @api.model
    def _distri_cron_expire(self):
        """Every hour, in case there is no settlement the next day: a
        receipt of the day before yesterday (or older) is overdue anyway."""
        # an unpaid order paid in the meantime closes first
        self.sudo().search([("kind", "=", "unpaid"),
                            ("state", "in", OPEN_STATES)])._distri_check_paid_items()
        # 19.0.1.129.52: a pending receipt whose order is already paid in full
        self.sudo().search([("kind", "in", ("done", False)),
                            ("state", "in", OPEN_STATES)])._distri_resolve_done_paid()
        expired = self.sudo().search([
            ("route_kind", "=", "route"), ("state", "=", "open"),
            ("delivered_day", "<", today_ar() - timedelta(days=1)),
        ])
        expired._distri_expire()
        return len(expired)

    def _distri_expire(self):
        """«Vencida»: charged to the driver."""
        today = today_ar()
        Case = self.env["distri.route.case"].sudo()
        for promise in self.sudo():
            vals = {"state": "overdue", "overdue_since": today,
                    "state_changed_at": fields.Datetime.now()}
            if not promise.charge_case_id:
                who = "%s · %s" % (promise.partner_id._distri_app_name(), promise.order_id.name)
                if promise.kind == "unpaid":
                    text = ("%s: el pedido del %s se entregó y no se cobró (%s). Se carga a la "
                            "cuenta del repartidor; se cierra solo si el pago se registra o llega "
                            "el comprobante." % (who, day_label(promise.delivered_day),
                                                 _money(promise.amount)))
                else:
                    text = ("%s: el comprobante de la transferencia de %s del %s no llegó a la "
                            "empresa. Se carga a la cuenta del repartidor salvo que "
                            "administración verifique el pago en Mercado Pago." % (
                                who, _money(promise.amount), day_label(promise.delivered_day)))
                vals["charge_case_id"] = Case.create({
                    "route_id": promise.route_id.id,
                    "kind": CASE_KIND.get(promise.kind or "done", "receipt"),
                    "amount": -promise.amount,
                    "stop_id": promise.stop_id.id,
                    "promise_id": promise.id,
                    "candidates": text,
                }).id
            promise.write(vals)

    # ------------------------------------------------------------ resolution

    def _distri_resolve(self, via, user=None, receipt=None, collection=None):
        """«Resuelta»: who, when and how the receipt arrived."""
        now = fields.Datetime.now()
        for promise in self.sudo():
            if via == "mp":
                label = "Reporte Mercado Pago"
            elif via == "payment":
                label = "Pago registrado"
            elif receipt and not user:
                label = "Cliente"
            else:
                role = role_label(user)
                label = "%s (%s)" % (user.name, role) if role else user.name
            was_overdue = promise.state == "overdue"
            vals = {"state": "fulfilled", "resolved_at": now, "resolved_via": via,
                    "resolved_by_id": user.id if user else False, "resolved_by_label": label,
                    "partial_note": False, "state_changed_at": now}
            if receipt:
                vals["receipt_id"] = receipt.id
            if collection:
                vals["fulfilled_by_id"] = collection.id
            promise.write(vals)
            case = promise.charge_case_id
            if not (was_overdue and case and case.state == "open"):
                continue
            if via == "mp":
                promise._distri_close_charge_mp(collection)
            elif promise.kind == "unpaid" and via in ("payment", "whatsapp"):
                # the customer paid: nothing to charge (decision of ale, 22/10/2026)
                case.write({"state": "closed", "resolution": (
                    PAID_CLOSED_CASE % promise.order_id.name if via == "payment"
                    else RECEIPT_CLOSED_CASE % (local_when(now), label))})
            else:
                case.message_post(body=(
                    "El comprobante llegó el %s, después del vencimiento (%s). Administración "
                    "decide si se descarga el cargo; se descarga solo cuando el reporte de "
                    "Mercado Pago trae la transferencia." % (local_when(now), label)))

    def _distri_reopen(self):
        """Back to «Pendiente» (or «Vencida» if it had expired)."""
        for promise in self.sudo():
            promise.write({
                "state": "overdue" if promise.overdue_since else "open",
                "resolved_at": False, "resolved_via": False, "resolved_by_id": False,
                "resolved_by_label": False, "receipt_id": False, "fulfilled_by_id": False,
            })

    def _check_can_resolve(self):
        user = self.env.user
        if not (self.env.su or user.has_group(SELLER_GROUP) or user.has_group(ADMIN_GROUP)):
            raise UserError("Solo vendedores, administración y gerencia marcan comprobantes.")

    def _distri_backend_claim_items(self):
        """Every open item of this customer the user can see: a seller only
        sees (and claims) the customers of his portfolio (security/reglas.xml)."""
        self.ensure_one()
        self.check_access("read")
        partner = self.sudo().partner_id.commercial_partner_id
        visible = self.search([("partner_id", "child_of", partner.id),
                               ("state", "in", OPEN_STATES)])
        return visible.sudo()

    def _reminder_text(self):
        """«Reclamar por WhatsApp» from Odoo: the same message as the driver's
        app, in the company's voice, with every open item of the customer."""
        self.ensure_one()
        return self._distri_customer_claim(self._distri_backend_claim_items(), voice="company")

    def action_mark_arrived(self):
        """«Marcar llegó el comprobante» (seller, administración, gerencia)."""
        self._check_can_resolve()
        self.check_access("read")
        todo = self.sudo().filtered(lambda p: p.state in OPEN_STATES)
        if not todo:
            raise UserError("El comprobante ya está resuelto o anulado.")
        todo._distri_resolve("manual", user=self.env.user)
        return True

    def action_fulfilled(self):
        return self.action_mark_arrived()

    def action_cancel(self):
        if not (self.env.su or self.env.user.has_group(ADMIN_GROUP)):
            raise UserError("Solo administración o gerencia anulan un comprobante pendiente.")
        self.sudo().write({"state": "cancelled", "state_changed_at": fields.Datetime.now()})
        return True

    # ------------------------------------------------------------ WhatsApp

    @api.model
    def _distri_resolve_from_receipt(self, receipt):
        """A receipt that reached the company resolves at most one pending
        receipt: the one of the order written next to it, else the one of
        the same amount, else the only one of the customer."""
        receipt = receipt.sudo()
        if not receipt.distri_is_receipt or \
                receipt.distri_receipt_state in CLOSED_RECEIPT_STATES:
            return self.browse()
        partner = receipt.distri_partner_id.commercial_partner_id
        if not partner or self.search_count([("receipt_id", "=", receipt.id)]):
            return self.browse()
        domain = [("partner_id", "child_of", partner.id)]
        if receipt.timestamp:
            domain.append(("captured_at", "<=", receipt.timestamp + PROMISE_BEFORE))
        candidates = self.sudo().search(domain + [("state", "in", OPEN_STATES)],
                                        order="captured_at, id")
        if receipt.timestamp:
            candidates = candidates.filtered(
                lambda p: not p.captured_at or p.captured_at >= receipt.timestamp - PROMISE_BEFORE
                or p.state == "overdue")
        amount = receipt.distri_amount
        target = self.browse()
        if receipt.distri_order_ref:
            mine = self.sudo().search(domain + [("order_id.name", "=", receipt.distri_order_ref)])
            if mine:
                # its order's receipt (already resolved: nothing else)
                target = mine.filtered(lambda p: p.state in OPEN_STATES)[:1]
                if not target:
                    return self.browse()
        if not target and amount:
            target = candidates.filtered(
                lambda p: abs(p.amount - amount) <= AMOUNT_TOLERANCE)[:1]
        if not target and len(candidates) == 1:
            target = candidates
        if not target:
            return self.browse()
        if amount and amount < target.amount - AMOUNT_TOLERANCE:
            target.partial_note = "Llegó un comprobante de %s (%s): no cubre los %s" % (
                _money(amount), local_when(receipt.timestamp), _money(target.amount))
            return self.browse()
        target._distri_resolve("whatsapp", user=receipt.distri_forwarded_by_id or None,
                               receipt=receipt)
        return target

    def _distri_resolve_from_waiting_receipts(self):
        """A visit synced after its receipt arrived: the receipt of the
        customer that resolved nothing yet."""
        Message = self.env["distri.whatsapp.message"].sudo()
        for promise in self.sudo().filtered(lambda p: p.state in OPEN_STATES):
            partner = promise.partner_id.commercial_partner_id
            since = (promise.captured_at or fields.Datetime.now()) - PROMISE_BEFORE
            receipts = Message.search([
                ("distri_is_receipt", "=", True),
                ("distri_receipt_state", "not in", CLOSED_RECEIPT_STATES),
                ("distri_partner_id", "child_of", partner.id),
                ("timestamp", ">=", since),
            ], order="timestamp, id")
            used = set(self.sudo().search([("receipt_id", "in", receipts.ids)]).receipt_id.ids)
            for receipt in receipts.filtered(lambda r: r.id not in used):
                if self._distri_resolve_from_receipt(receipt) == promise:
                    break

    @api.model
    def _distri_unresolve_receipt(self, receipt):
        """A copy detected late (after the photo was read): what it resolved
        opens again."""
        promises = self.sudo().search([("receipt_id", "=", receipt.id),
                                       ("state", "=", "fulfilled"),
                                       ("fulfilled_by_id", "=", False)])
        promises._distri_reopen()
        return promises

    # ------------------------------------------------------------ payments

    def _distri_check_paid_items(self, cause=None):
        """«Pedido sin cobrar» follows what is still unpaid on the order's
        invoice (decision of ale, 22/10/2026): the invoice residual, never
        above the amount left unpaid at the visit. A partial payment lowers
        the item and its open charge; a payment undone raises them again; at
        zero the item is resolved and the charge closes. Every change is kept
        in the item's history with the payment that caused it. An order
        without an invoice is left alone, and so is a charge administración
        closed or decided by hand. Returns the items paid in full now."""
        paid = self.browse()
        for promise in self.sudo().filtered(lambda p: p.kind == "unpaid" and (
                p.state in OPEN_STATES
                or (p.state == "fulfilled" and p.resolved_via == "payment"))):
            invoices = promise.order_id._distri_facturas_emitidas()
            if not invoices:
                continue
            currency = promise.currency_id or invoices[:1].currency_id
            original = promise.original_amount or promise.amount
            residual = max(sum(invoices.mapped("amount_residual")), 0.0)
            unpaid = currency.round(min(residual, original))
            if currency.compare_amounts(unpaid, promise.amount) == 0:
                continue
            case = promise.charge_case_id
            if case and not promise._distri_case_is_automatic(case):
                # administración decided this charge: nothing changes by itself
                continue
            promise._distri_log_amount(unpaid, cause)
            if case and case.state == "open":
                case.amount = -unpaid
            if currency.is_zero(unpaid):
                if promise.state in OPEN_STATES:
                    promise._distri_resolve("payment")
                paid |= promise
            elif promise.state == "fulfilled":
                # a payment was undone: pending (or overdue) again
                promise._distri_reopen()
                if case and case.state == "closed":
                    case.write({"state": "open", "resolution": False, "amount": -unpaid})
        return paid

    def _distri_resolve_done_paid(self):
        """19.0.1.129.52 (owner, 26/11/2026): a pending receipt of a delivery
        paid by transfer («done») whose order's issued invoices are already
        paid in full (residual 0 on every one; e.g. the customer's Mercado
        Pago transfer was confirmed before the driver's visit synced, so the
        income never met the promise) is resolved as «Pago registrado», and
        its open charge to the driver is closed. A partial payment or an
        order without an issued invoice is left alone, and so is a charge
        the driver already replenished. Nothing touches the ledger. Returns
        the items resolved now."""
        resolved = self.browse()
        for promise in self.sudo().filtered(
                lambda p: (p.kind or "done") == "done" and p.state in OPEN_STATES
                and p.order_id):
            invoices = promise.order_id._distri_facturas_emitidas()
            if not invoices or not all(inv.currency_id.is_zero(inv.amount_residual)
                                       for inv in invoices):
                continue
            case = promise.charge_case_id
            promise._distri_resolve("payment")
            if case and case.state == "open" and not case.replenished:
                case.write({"state": "closed", "resolution": DONE_PAID_CLOSED_CASE
                            % ", ".join(invoices.mapped("name"))})
            resolved |= promise
        return resolved

    def _distri_case_is_automatic(self, case):
        """An open charge nobody marked as replenished, or one this module
        closed because the order was paid."""
        if case.replenished:
            return False
        if case.state == "open":
            return True
        return (case.resolution or "").startswith(PAID_CLOSED_CASE.split(" %s")[0])

    def _distri_log_amount(self, new, cause=None):
        self.ensure_one()
        self.env["distri.promised.transfer.history"].sudo().create({
            "promise_id": self.id,
            "old_amount": self.amount,
            "new_amount": new,
            "move_id": cause.id if cause else False,
            "cause": cause.display_name if cause else "Saldo de la factura recalculado",
        })
        self.amount = new

    @api.model
    def _distri_check_paid_orders(self, orders, cause=None):
        if not orders:
            return self.browse()
        return self.sudo().search([("kind", "=", "unpaid"),
                                   ("order_id", "in", orders.ids)])._distri_check_paid_items(cause)

    # ------------------------------------------------------------ Mercado Pago

    @api.model
    def _distri_fulfil_for(self, partner, when, amount, collection=None):
        """A transfer of the customer confirmed on the collections board.

        The pending receipt it was proposed by goes first, then the one whose
        receipt is the transfer's, then the oldest pending ones while the
        amount covers them; the first one it does not cover keeps a note (a
        partial payment) and stops there. A pending receipt that expired
        gets its charge to the driver closed: the payment is verified."""
        domain = [("partner_id", "child_of", partner.id)]
        if when:
            domain.append(("captured_at", "<=", when + PROMISE_BEFORE))
        candidates = self.sudo().search(domain + [
            "|", ("state", "in", OPEN_STATES),
            "&", ("state", "=", "fulfilled"), ("fulfilled_by_id", "=", False),
        ]).sorted(lambda p: (p.captured_at or datetime.min, p.id))
        own = collection.promise_id if collection else self.browse()
        receipt = collection.receipt_id if collection else False
        first = own | candidates.filtered(lambda p: receipt and p.receipt_id == receipt)
        rest = candidates.filtered(lambda p: p.state in OPEN_STATES) - first
        remaining = amount or 0.0
        touched = self.browse()
        for promise in (first & candidates) | rest:
            if promise.amount > remaining + AMOUNT_TOLERANCE:
                if remaining > AMOUNT_TOLERANCE and promise.state in OPEN_STATES:
                    promise.partial_note = "Transferencia de %s%s: no cubre los %s" % (
                        _money(remaining), " (%s)" % collection.name if collection else "",
                        _money(promise.amount))
                break
            remaining -= promise.amount
            touched |= promise
        for promise in touched:
            if promise.state == "fulfilled" and promise.resolved_via == "payment" and collection:
                # the transfer's own payment closed it a moment ago: it is Mercado Pago's
                promise.write({"fulfilled_by_id": collection.id, "resolved_via": "mp",
                               "resolved_by_label": "Reporte Mercado Pago"})
                case = promise.charge_case_id
                if case and (case.resolution or "").startswith("El pago del pedido"):
                    case.resolution = MP_CLOSED_CASE % collection.name
            elif promise.state == "fulfilled":
                promise.fulfilled_by_id = collection.id if collection else False
                # 19.0.1.129.24: the receipt arrived late by WhatsApp (the
                # charge stayed open for administración) and now the
                # transfer is verified: the charge closes, as designed
                promise._distri_close_charge_mp(collection)
            else:
                promise._distri_resolve("mp", collection=collection)
        return touched

    def _distri_close_charge_mp(self, collection=None):
        """19.0.1.129.24: the open charge to the driver closes, the payment is
        verified in Mercado Pago. Before, only a promise still open or
        overdue at the confirmation closed it (`_distri_resolve`); one
        already resolved by a late WhatsApp receipt kept the charge open
        forever (15 open charges in production on 24/11)."""
        for promise in self.sudo():
            case = promise.charge_case_id
            if case and case.state == "open":
                case.write({"state": "closed", "resolution": MP_CLOSED_CASE % (
                    collection.name if collection else "")})

    @api.model
    def _distri_close_settled_charges(self, apply=False):
        """19.0.1.129.24, one-off (scripts/correcciones/v129_24_cerrar_cargos.sh,
        never run by itself): the open charges whose pending item is already
        settled, closed by the same rules and with the same texts as when it
        settles now. Idempotent: a closed charge is never touched again.

        - verified in Mercado Pago (a confirmed transfer): MP_CLOSED_CASE;
        - «Pedido sin cobrar» paid, or its receipt arrived: PAID_CLOSED_CASE
          or RECEIPT_CLOSED_CASE (decision of ale, 22/10/2026).
        A transfer receipt that only arrived late by WhatsApp (or was marked
        by hand) stays open: administración decides it, as designed.

        Owner's rule (24/11, until the accounts audit closes): no customer
        balance changes. Only the internal charge is written (state,
        resolution, a chatter note); never a payment, an invoice, a journal
        entry or a reconciliation. Checked: the receivable total and the
        number of entries, payments and reconciliations are the same before
        and after, or nothing is written (UserError, the transaction rolls
        back), in the dry run too.

        Returns one dict per charge: what was (or would be, `apply=False`)
        closed, and those left for administración.
        """
        before = self._distri_ledger_fingerprint()
        Case = self.env["distri.route.case"].sudo()
        cases = Case.search([("state", "=", "open"), ("promise_id", "!=", False),
                             ("promise_id.state", "=", "fulfilled")], order="id")
        rows = []
        for case in cases:
            promise = case.promise_id
            collection = promise.fulfilled_by_id
            if collection and collection.state == "confirmed":
                resolution = MP_CLOSED_CASE % collection.name
            elif promise.kind == "unpaid" and promise.resolved_via == "payment":
                resolution = PAID_CLOSED_CASE % promise.order_id.name
            elif promise.kind == "unpaid" and promise.resolved_via == "whatsapp":
                resolution = RECEIPT_CLOSED_CASE % (local_when(promise.resolved_at),
                                                    promise.resolved_by_label or "")
            else:
                resolution = False
            rows.append({"case": case.display_name, "case_id": case.id,
                         "promise_id": promise.id, "order": promise.order_id.name or "",
                         "driver": case.driver_id.name or "", "amount": case.amount,
                         "close": bool(resolution), "resolution": resolution or
                         "Queda abierto: el comprobante llegó tarde y no está verificado "
                         "en Mercado Pago (decide administración)."})
            if apply and resolution:
                case.write({"state": "closed", "resolution": resolution})
                case.message_post(body="Cerrado por la corrección 19.0.1.129.24: el pendiente "
                                       "ya estaba resuelto (%s)." % (promise.resolved_by_label or ""))
        self.env.flush_all()
        after = self._distri_ledger_fingerprint()
        if after != before:
            raise UserError("La corrección cambiaría saldos de clientes (%s -> %s): no se cierra "
                            "nada." % (before, after))
        if apply:
            _logger.info("19.0.1.129.24: closed %s settled driver charges",
                         sum(1 for r in rows if r["close"]))
        return rows

    @api.model
    def _distri_ledger_fingerprint(self):
        """The customers' receivable total (posted entries) and how many
        entries, payments and reconciliations exist: what a correction that
        must not touch any balance has to leave as it found it."""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("""
            SELECT COALESCE(SUM(l.balance), 0)
              FROM account_move_line l
              JOIN account_account a ON a.id = l.account_id
             WHERE a.account_type = 'asset_receivable' AND l.parent_state = 'posted'
        """)
        receivable = round(float(cr.fetchone()[0]), 2)
        counts = []
        for table in ("account_move", "account_payment", "account_partial_reconcile"):
            cr.execute("SELECT COUNT(*) FROM %s" % table)  # fixed names, no input
            counts.append(cr.fetchone()[0])
        return (receivable, *counts)

    def _distri_undo_mp(self, collection):
        """The confirmation of `collection` was undone."""
        for promise in self.sudo().filtered(lambda p: p.fulfilled_by_id == collection):
            if promise.resolved_via == "mp":
                promise._distri_reopen()
                case = promise.charge_case_id
                if case and case.state == "closed" and (case.resolution or "").startswith(
                        "Pago verificado en Mercado Pago"):
                    case.write({"state": "open", "resolution": False})
            else:
                promise.fulfilled_by_id = False
                # 19.0.1.129.24: a late receipt alone does not close the
                # charge: open again what the verification closed
                case = promise.charge_case_id
                if case and case.state == "closed" and (case.resolution or "").startswith(
                        "Pago verificado en Mercado Pago"):
                    case.write({"state": "open", "resolution": False})
        # its payment was cancelled: what is unpaid on the invoice again
        self.sudo().filtered(lambda p: p.kind == "unpaid")._distri_check_paid_items(
            collection.payment_id.move_id if collection.payment_id else None)


class DistriPromisedTransferHistory(models.Model):
    """«Historial del importe» of a «Pedido sin cobrar»: why its amount (and
    the driver's charge) went down or up."""
    _name = "distri.promised.transfer.history"
    _description = "Cambio de importe de un pendiente del repartidor"
    _order = "create_date desc, id desc"

    promise_id = fields.Many2one("distri.promised.transfer", required=True, ondelete="cascade",
                                 index=True, string="Pendiente")
    currency_id = fields.Many2one(related="promise_id.currency_id")
    date = fields.Datetime(string="Fecha", default=fields.Datetime.now, readonly=True)
    old_amount = fields.Monetary(string="Importe anterior", readonly=True)
    new_amount = fields.Monetary(string="Importe nuevo", readonly=True)
    move_id = fields.Many2one("account.move", string="Pago", readonly=True, ondelete="set null")
    cause = fields.Char(string="Motivo", readonly=True)


class DistriRouteCaseHistory(models.Model):
    _inherit = "distri.route.case"

    amount_history_ids = fields.One2many(related="promise_id.amount_history_ids",
                                         string="Historial del importe")


class WhatsappMessageComprobantePendiente(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_promise_ids = fields.One2many("distri.promised.transfer", "receipt_id",
                                         string="Comprobante pendiente resuelto")

    def _distri_read_receipt(self, read_file=False):
        result = super()._distri_read_receipt(read_file=read_file)
        Promise = self.env["distri.promised.transfer"].sudo()
        for msg in self.sudo().filtered(lambda m: m.distri_is_receipt):
            Promise._distri_resolve_from_receipt(msg)
        return result

    def write(self, vals):
        result = super().write(vals)
        if not self.env.context.get("distri_receipt_matching") and (
                {"distri_partner_id", "distri_amount", "distri_is_receipt"} & set(vals)):
            Promise = self.env["distri.promised.transfer"].sudo()
            for msg in self.sudo().filtered(lambda m: m.distri_is_receipt):
                Promise._distri_resolve_from_receipt(msg)
        return result

    def _distri_close(self, state, note):
        result = super()._distri_close(state, note)
        if state in CLOSED_RECEIPT_STATES:
            self.env["distri.promised.transfer"].sudo()._distri_unresolve_receipt(self)
        return result

    def action_distri_dismiss_receipt(self):
        result = super().action_distri_dismiss_receipt()
        Promise = self.env["distri.promised.transfer"].sudo()
        for msg in self:
            Promise._distri_unresolve_receipt(msg)
        return result

    def _distri_apply_late_note(self):
        """Also a text with the order number written after the photo
        («Comprobante del pedido S00231», as «Reenviar a la empresa» writes
        it): it names the order of the last photo or PDF the same number
        sent just before, while that photo has none."""
        super()._distri_apply_late_note()
        Order = self.env["sale.order"].sudo()
        for note in self.sudo():
            found = _ORDER_RE.search(note.body or "")
            if not (found and note.wa_id and note.timestamp):
                continue
            window = SELLER_NOTE_WINDOW if self.env["res.users"]._distri_seller_by_phone(
                note.wa_id) else RECEIPT_TEXT_WINDOW
            photo = note.search([
                ("wa_id", "=", note.wa_id), ("direction", "=", "in"), ("id", "!=", note.id),
                ("message_type", "in", ("image", "document")),
                ("distri_order_ref", "=", False),
                ("timestamp", ">=", note.timestamp - window),
                ("timestamp", "<=", note.timestamp),
            ], order="timestamp desc, id desc", limit=1)
            if not photo or (photo.distri_receipt_state
                             and photo.distri_receipt_state not in ("pending", "no_money")):
                continue
            order = Order.search([("name", "=", found.group(0))], limit=1)
            vals = {"distri_order_ref": found.group(0)}
            if not photo.distri_is_receipt:
                vals.update(distri_is_receipt=True, distri_receipt_state="pending")
            if order and not order.partner_id.distri_generic_partner and \
                    not photo.distri_partner_id:
                vals.update(distri_partner_id=order.partner_id.commercial_partner_id.id,
                            distri_partner_source="Pedido %s escrito después de la foto" %
                            order.name)
            photo.with_context(distri_receipt_matching=True).write(vals)
            photo._distri_classify()
            photo._distri_match_income()
            photo._distri_sync_verify_card()
            self.env["distri.promised.transfer"].sudo()._distri_resolve_from_receipt(photo)


class AccountMoveLineComprobante(models.Model):
    _inherit = "account.move.line"

    def reconcile(self):
        """A payment applied to an invoice: the «No pagó nada» of its order
        may be paid now (comprobante_pendiente.py)."""
        result = super().reconcile()
        _distri_after_reconcile_change(self.env, self.move_id)
        return result


def _distri_after_reconcile_change(env, moves):
    """The «No pagó nada» of the orders of these invoices follows their
    residual; the cause is the other entry (the payment)."""
    invoices = moves.filtered(lambda m: m.move_type == "out_invoice")
    orders = invoices.sudo().line_ids.sale_line_ids.order_id
    if not orders or not env["distri.promised.transfer"].sudo().search_count(
            [("kind", "=", "unpaid"), ("order_id", "in", orders.ids)]):
        return
    cause = (moves - invoices)[:1]
    env["distri.promised.transfer"].sudo()._distri_check_paid_orders(orders, cause or None)


class AccountMoveComprobante(models.Model):
    _inherit = "account.move"

    def write(self, vals):
        """A payment reset to draft, cancelled or posted again changes what is
        unpaid on the invoices it was matched with (Odoo 19 keeps the matching
        and leaves the draft entry out of the residual)."""
        result = super().write(vals)
        if "state" in vals:
            lines = self.sudo().line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable")
            partials = lines.matched_debit_ids | lines.matched_credit_ids
            related = (partials.debit_move_id | partials.credit_move_id).move_id
            _distri_after_reconcile_change(self.env, self.sudo() | related)
        return result


class AccountPartialReconcileComprobante(models.Model):
    _inherit = "account.partial.reconcile"

    def unlink(self):
        """A payment taken off an invoice: what is unpaid goes up again."""
        moves = (self.debit_move_id | self.credit_move_id).move_id
        result = super().unlink()
        _distri_after_reconcile_change(self.env, moves.exists())
        return result


class SaleOrderComprobante(models.Model):
    _inherit = "sale.order"

    def _distri_red_notice(self):
        """«Cliente en rojo» on the customer's order page: what he owes from
        the days before, so he can pay it or send the receipts. Only while
        the setting is on."""
        self.ensure_one()
        order = self.sudo()
        if not red_rule_on(self.env) or order.state == "cancel" or \
                order.partner_id.distri_generic_partner:
            return False
        partner = order.partner_id.commercial_partner_id
        Promise = self.env["distri.promised.transfer"].sudo()
        receipts = Promise.search([("partner_id", "child_of", partner.id),
                                   ("state", "in", OPEN_STATES), ("kind", "=", "done"),
                                   ("order_id", "!=", order.id)], order="captured_at, id")
        debt = max(order._distri_previous_debt() - Promise._distri_waiting_money(partner), 0.0)
        if debt <= AMOUNT_TOLERANCE and not receipts:
            return False
        return {
            "debe": _money(debt) if debt > AMOUNT_TOLERANCE else "",
            "comprobantes": [{"pedido": p.order_id.name, "fecha": day_label(p.delivered_day),
                              "importe": _money(p.amount)} for p in receipts],
        }
