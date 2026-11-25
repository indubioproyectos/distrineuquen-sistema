"""«Reparto v2» (19.0.1.129.28): the owner-approved «cobro del repartidor»
proposal (24/11/2026), stages 1 and 2.

Everything here sits behind the ir.config_parameter PARAM_REPARTO_V2
(distri_pedido.reparto_v2), OFF by default. With it off nothing below runs:
the route payload, the visit rules, the charges and the API answer exactly
as 19.0.1.129.27 (tests/test_v129_28_reparto_v2.py proves it). Production
gets the code with the flag off; the practice environment turns it on
(scripts/correcciones/v129_28_activar_reparto_v2.sh).

With the flag on:

1. The route payload says so (`reparto_v2`) and each stop carries what the
   visit has to collect now (`v2_due`: the order's invoiced residual, never
   above the order) and whether a partial payment is charged to the driver
   (`v2_exempt`, decision C below). static/src/reparto/app5.js draws the new
   one-screen visit only when it sees `reparto_v2`; otherwise the old screen.
   The visit travels in the same payload as before.
2. «Ya estaba pagado» while the system still sees debt is ACCEPTED (owner):
   the stop is `already_paid`, flagged «A revisar» (`paid_confirm_state`
   to_review) and sent to administración (a To-Do per administración user
   on the order and a note in the route's chatter). Nothing at the driver's name.
   The hourly cron marks it «Confirmado» once the order has nothing left to
   pay.
3. A partial payment (cash, transfer or both short of `v2_due`) is charged
   to the driver at once (a «short» case, as a short transfer already was)
   and the order's salesperson gets a To-Do (owner: «al repartidor y al
   vendedor»).
   Decision C (RECOMMENDED DEFAULT, PENDING OWNER CONFIRMATION): a customer
   with a current account (`distri_cuenta_corriente`, new, set by
   administración) or authorized for the hidden list (`_distri_es_distribuidora`:
   lista oculta, «Es mayorista», a hidden price list) is exempt: the
   difference stays on his account and only the salesperson is told. Its
   own parameter PARAM_EXEMPT (distri_pedido.reparto_v2_exento_cc, ON unless
   set to 0) turns the exemption off.
4. A receipt that arrives after the deadline closes the driver's charge by
   itself (owner), instead of waiting for administración. The deadline
   itself does not change (owner).
5. GET /reparto/api/home (controllers/reparto.py, `_distri_v2_home`): «Mis
   comprobantes a reclamar» (the driver's own pending receipts, unpaid
   orders and open charges, with or without an open route) and the routes of
   earlier days still open, for the gentle reminder (never a block).
6. #4: POST /reparto/api/order, the driver's own visit order (`sequence`),
   so another phone downloads it too.

Owner's rule (until the accounts audit closes): nothing here credits,
creates, cancels or reconciles a payment, an invoice or a journal entry.
Only stops, pending receipts, charges (distri.route.case), activities and
chatter notes are written: no customer's receivable balance moves.
"""
import logging

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError

from .cobros import AMOUNT_TOLERANCE, local_when
from .comprobante_pendiente import role_label
from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

PARAM_REPARTO_V2 = "distri_pedido.reparto_v2"
PARAM_EXEMPT = "distri_pedido.reparto_v2_exento_cc"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")
ADMIN_GROUP = "distri_pedido.grupo_administracion"
REVIEW_SUMMARY = "«Ya estaba pagado» a revisar"
PARTIAL_SUMMARY = "Pago parcial en el reparto"
# the charges shown in «Mis comprobantes a reclamar» (controllers/rendiciones.py)
CHARGE_KINDS = ("receipt", "short", "unpaid")
LATE_MARK = "el cargo se cerró solo (decisión del dueño, 24/11/2026)"
LATE_CLOSED_CASE = "Llegó el comprobante el %s (%s), después del vencimiento: " + LATE_MARK + "."
ORDER_MAX = 300


def reparto_v2_on(env):
    value = (env["ir.config_parameter"].sudo().get_param(PARAM_REPARTO_V2) or "0")
    return value.strip().lower() in ON_VALUES


def exempt_on(env):
    """Decision C: ON unless the parameter says 0 (pending owner confirmation)."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_EXEMPT)
    if value is None or value is False or not str(value).strip():
        return True
    return str(value).strip().lower() in ON_VALUES


def _admin_users(env):
    group = env.ref(ADMIN_GROUP, raise_if_not_found=False)
    if not group:
        return env["res.users"]
    root = env.ref("base.user_root", raise_if_not_found=False)
    return group.sudo().all_user_ids.filtered(
        lambda u: u.active and not u.share and u != root).sorted("id")


def _todo(record, users, summary, note, key):
    """One To-Do per user on `record`, never a second one while one about the
    same `key` (the order's name) is open."""
    todo = record.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
    if not todo or not users:
        return
    quiet = record.with_context(mail_activity_quick_update=True, mail_notrack=True).sudo()
    open_users = record.sudo().activity_ids.filtered(
        lambda a: a.summary == summary and key in (a.note or "")).user_id
    for user in users - open_users:
        quiet.activity_schedule(activity_type_id=todo.id, summary=summary, user_id=user.id,
                                note=note)


class ResPartnerRepartoV2(models.Model):
    _inherit = "res.partner"

    distri_cuenta_corriente = fields.Boolean(
        string="Cuenta corriente (pago parcial a cuenta)", copy=False, tracking=True,
        groups="distri_pedido.grupo_administracion,base.group_system",
        help="19.0.1.129.28, decisión C (pendiente de confirmar por el dueño): con la marca, "
             "un pago parcial en el reparto queda a cuenta del cliente y no se carga al "
             "repartidor; se avisa al vendedor. Solo con «Reparto v2» activado.")

    def _distri_v2_exempt(self):
        """Decision C: a partial payment of this customer is not the driver's."""
        self.ensure_one()
        partner = self.sudo().commercial_partner_id
        if not exempt_on(self.env) or not partner or partner.distri_generic_partner:
            return False
        return bool(partner.distri_cuenta_corriente or partner._distri_es_distribuidora())


class DistriRouteStopRepartoV2(models.Model):
    _inherit = "distri.route.stop"

    paid_confirm_state = fields.Selection(
        selection_add=[("to_review", "A revisar")], ondelete={"to_review": "set null"})
    v2_due_at_download = fields.Monetary(
        string="A cobrar al descargar la ruta", readonly=True, copy=False,
        help="19.0.1.129.28: lo que el celular mostró «Le toca pagar» antes de la visita.")
    v2_due_known = fields.Boolean(readonly=True, copy=False)
    partial_notice_amount = fields.Monetary(
        string="Pago parcial avisado al vendedor", readonly=True, copy=False,
        help="19.0.1.129.28: la diferencia de un pago parcial de la que ya se avisó al vendedor.")

    def _distri_v2_due(self):
        """What the visit had to collect: what was due when the phone
        downloaded the route (`v2_due_at_download`: the order or, when less,
        its invoiced residual) minus what the customer returned. Not the
        residual now: the visit's own cash payment «a rendir» and its
        credited receipt already lowered it. Without a snapshot (the route
        was downloaded with the flag off), today's order as before."""
        self.ensure_one()
        stop = self.sudo()
        if not stop.v2_due_known:
            return stop._distri_due_today()
        base = min(stop.order_amount or 0.0, stop.v2_due_at_download or 0.0)
        return max(base - (stop.returns_amount or 0.0), 0.0)

    def _app_payload(self):
        data = super()._app_payload()
        if reparto_v2_on(self.env):
            due = 0.0 if data.get("payments_paid") else min(
                max(self.order_amount or 0.0, 0.0),
                data.get("payments_due", self.order_amount) or 0.0)
            due = round(max(due, 0.0), 2)
            data.update({"v2_due": due, "v2_exempt": self.partner_id._distri_v2_exempt()})
            # what is due before any visit: never rewritten once visited
            stop = self.sudo()
            if stop.state == "pending" and (not stop.v2_due_known
                                            or abs(stop.v2_due_at_download - due) > 0.005):
                stop.write({"v2_due_at_download": due, "v2_due_known": True})
        return data

    @api.model
    def _distri_cron_paid_to_confirm(self, now=None):
        """19.0.1.129.28: also the visits «Ya estaba pagado · a revisar»:
        «Confirmado» once the order has nothing left to pay."""
        result = super()._distri_cron_paid_to_confirm(now=now)
        now = now or fields.Datetime.now()
        for stop in self.sudo().search([("paid_confirm_state", "=", "to_review"),
                                        ("state", "=", "already_paid")], order="id"):
            if stop.sudo()._app_payments()["paid"]:
                text = "Pago confirmado: %s · %s ya no está a revisar." % (
                    stop.partner_id._distri_app_name(), stop.order_id.name)
                stop.write({"paid_confirm_state": "confirmed", "paid_confirm_done_at": now})
                stop.route_id.message_post(body=text)
                todos = stop.order_id.sudo().activity_ids.filtered(
                    lambda a: a.summary == REVIEW_SUMMARY)
                if todos:
                    todos.action_feedback(feedback=text)
        return result

    def _distri_v2_review_notice(self):
        """«Ya estaba pagado» with debt: administración reviews it."""
        self.ensure_one()
        due = self.sudo()._app_payments()["due"]
        text = ("%s · %s: el repartidor %s lo entregó como «Ya estaba pagado» y el sistema todavía "
                "ve %s sin pagar. Se aceptó (decisión del dueño) y queda a revisar: no se cargó nada "
                "al repartidor ni cambió el saldo del cliente." % (
                    self.partner_id._distri_app_name(), self.order_id.name,
                    self.route_id.driver_id.name or "", _money(due)))
        self.route_id.message_post(body=Markup("<b>%s</b>: %s") % (REVIEW_SUMMARY, text))
        # the route has no activities: the To-Dos go on the order
        _todo(self.order_id.sudo(), _admin_users(self.env), REVIEW_SUMMARY, text,
              self.order_id.name)

    def _distri_v2_partial_notice(self, short, exempt):
        """The salesperson hears of a partial payment once per amount."""
        self.ensure_one()
        if abs((self.partial_notice_amount or 0.0) - short) <= AMOUNT_TOLERANCE:
            return
        self.partial_notice_amount = short
        if short <= AMOUNT_TOLERANCE:
            return
        order = self.order_id.sudo()
        text = ("%s · %s: pagó %s de %s en la entrega. Faltan %s, que %s." % (
            self.partner_id._distri_app_name(), order.name,
            _money(self.cash_amount + self.transfer_amount), _money(short + self.cash_amount
                                                                   + self.transfer_amount),
            _money(short),
            "quedan a cuenta del cliente (cuenta corriente)" if exempt
            else "quedan a cargo del repartidor %s" % (self.route_id.driver_id.name or "")))
        order.message_post(body=Markup("<b>%s</b>: %s") % (PARTIAL_SUMMARY, text))
        seller = order.user_id
        if seller and seller.active and not seller.share and seller != self.route_id.driver_id:
            _todo(order, seller, PARTIAL_SUMMARY, text, order.name)


class DistriRouteRepartoV2(models.Model):
    _inherit = "distri.route"

    def _app_payload(self):
        data = super()._app_payload()
        if reparto_v2_on(self.env):
            data["reparto_v2"] = True
        return data

    def _distri_v2_set_order(self, stop_ids):
        """#4: the driver's own visit order. `stop_ids` lists stops of this
        route, the first to visit first; the others keep theirs after them."""
        self.ensure_one()
        if self.state not in ("draft", "on_route"):
            raise UserError("La ruta ya está cerrada: el orden no se cambia.")
        wanted = []
        for value in (stop_ids or [])[:ORDER_MAX]:
            try:
                stop_id = int(value)
            except (TypeError, ValueError):
                raise UserError("Orden inválido.")
            if stop_id not in wanted:
                wanted.append(stop_id)
        stops = self.stop_ids
        by_id = {s.id: s for s in stops}
        if any(i not in by_id for i in wanted):
            raise UserError("Ese pedido no está en la ruta.")
        ordered = [by_id[i] for i in wanted] + [s for s in stops if s.id not in wanted]
        for index, stop in enumerate(ordered, start=1):
            if stop.sequence != index * 10:
                stop.sequence = index * 10
        self.invalidate_recordset(["stop_ids"])
        return True

    @api.model
    def _distri_v2_home(self, user):
        """«Mis comprobantes a reclamar» and the open routes of earlier days."""
        Promise = self.env["distri.promised.transfer"].sudo()
        items = Promise._distri_driver_payload(user)
        promises = Promise.browse()
        if items:
            promises = Promise.search([("stop_id", "in", [i["stop_id"] for i in items])])
        route_of = {p.stop_id.id: p.route_id.id for p in promises}
        promise_of = {p.stop_id.id: p.id for p in promises}
        for item in items:
            item["route_id"] = route_of.get(item["stop_id"], 0)
            item["promise_id"] = promise_of.get(item["stop_id"], 0)
        cases = self.env["distri.route.case"].sudo().search([
            ("driver_id", "=", user.id), ("state", "=", "open"), ("kind", "in", CHARGE_KINDS),
            ("route_id", "!=", False), ("replenished", "=", False),
        ], order="id")
        labels = dict(cases._fields["kind"].selection)
        charges = []
        for case in cases:
            if case.promise_id and case.promise_id in promises:
                continue  # shown with its receipt («Vencida»)
            stop = case.stop_id
            charges.append({
                "id": case.id, "kind": case.kind, "label": labels.get(case.kind, ""),
                "amount": round(-(case.amount or 0.0), 2),
                "name": stop.partner_id._distri_app_name() if stop else "",
                "order": stop.order_id.name if stop else "",
                "date": case.date.strftime("%d/%m") if case.date else "",
                "text": case.candidates or "",
            })
        stale = self.sudo().search([
            ("driver_id", "=", user.id), ("kind", "=", "route"), ("state", "=", "on_route"),
            ("date", "<", today_ar()),
        ], order="date, trip, id")
        return {
            "receipts": items,
            "charges": charges,
            "stale_routes": [{"id": r.id, "name": r.name,
                              "date": r.date.strftime("%d/%m") if r.date else ""} for r in stale],
        }


class DistriSyncEventRepartoV2(models.Model):
    _inherit = "distri.sync.event"

    def _visit_values(self, stop, payload):
        """«Ya estaba pagado» while the order still owes and no uncredited
        receipt covers it (129.25 «a confirmar»): accepted, «a revisar»."""
        vals = super()._visit_values(stop, payload)
        if reparto_v2_on(self.env) and payload.get("already_paid") \
                and vals.get("state") == "not_paid":
            keep = stop.paid_confirm_state == "to_review" and stop.state == "already_paid"
            vals.update({
                "state": "already_paid",
                "paid_confirm_state": "to_review",
                "paid_confirm_card_id": False,
                "paid_confirm_since": stop.paid_confirm_since if keep else fields.Datetime.now(),
                "paid_confirm_done_at": False,
            })
        return vals

    def _apply_visit(self, user, route, payload, captured):
        reviewed = {}
        if reparto_v2_on(self.env) and isinstance(payload, dict):
            try:
                stop_id = int(payload.get("stop_id") or 0)
            except (TypeError, ValueError):
                stop_id = 0
            stop = route.stop_ids.filtered(lambda s: s.id == stop_id)
            reviewed = {"before": stop.paid_confirm_state == "to_review" if stop else False,
                        "stop": stop}
        result = super()._apply_visit(user, route, payload, captured)
        stop = reviewed.get("stop")
        if stop and stop.paid_confirm_state == "to_review" and not reviewed["before"]:
            stop._distri_v2_review_notice()
        return result


class DistriPromisedTransferRepartoV2(models.Model):
    _inherit = "distri.promised.transfer"

    @api.model
    def _distri_charge_short(self, stop):
        """19.0.1.129.28: a partial payment (cash, transfer or both) short
        of what was due is the driver's at once, unless decision C exempts
        the customer; the salesperson is told either way."""
        if not reparto_v2_on(self.env):
            return super()._distri_charge_short(stop)
        Case = self.env["distri.route.case"].sudo()
        case = Case.search([("stop_id", "=", stop.id), ("kind", "=", "short"),
                            ("state", "=", "open")], limit=1)
        short, due = 0.0, 0.0
        if stop.route_id.kind == "route" and stop.state == "paid" \
                and (stop.cash_amount > 0 or stop.transfer == "yes"):
            due = stop._distri_v2_due()
            short = due - stop.cash_amount - stop.transfer_amount
        exempt = short > AMOUNT_TOLERANCE and stop.partner_id._distri_v2_exempt()
        stop._distri_v2_partial_notice(max(short, 0.0) if short > AMOUNT_TOLERANCE else 0.0,
                                       exempt)
        if short <= AMOUNT_TOLERANCE or exempt:
            if case and not case.replenished:
                case.unlink()
            return Case
        paid = []
        if stop.transfer == "yes":
            paid.append("transfirió %s" % _money(stop.transfer_amount))
        if stop.cash_amount:
            paid.append("pagó %s en efectivo" % _money(stop.cash_amount))
        text = "%s · %s: %s y tenía que pagar %s. La diferencia de %s queda a cargo del " \
               "repartidor." % (stop.partner_id._distri_app_name(), stop.order_id.name,
                                " y ".join(paid), _money(due), _money(short))
        vals = {"amount": -short, "candidates": text}
        if case:
            case.write(vals)
        else:
            case = Case.create(dict(vals, route_id=stop.route_id.id, kind="short",
                                    stop_id=stop.id))
        return case

    def _distri_resolve(self, via, user=None, receipt=None, collection=None):
        """19.0.1.129.28: a receipt that arrives after the deadline closes the
        driver's charge by itself (owner), before the 129.24 rules would
        leave it to administración. The Mercado Pago report keeps its own
        rule (`_distri_close_charge_mp`)."""
        if reparto_v2_on(self.env) and via != "mp":
            now = fields.Datetime.now()
            if via == "payment":
                label = "Pago registrado"
            elif receipt and not user:
                label = "Cliente"
            elif user:
                role = role_label(user)
                label = "%s (%s)" % (user.name, role) if role else user.name
            else:
                label = ""
            for promise in self.sudo():
                case = promise.charge_case_id
                if promise.state == "overdue" and case and case.state == "open":
                    case.write({"state": "closed",
                                "resolution": LATE_CLOSED_CASE % (local_when(now), label)})
        return super()._distri_resolve(via, user=user, receipt=receipt, collection=collection)

    def _distri_reopen(self):
        """A receipt found to be a copy after it resolved an item: the charge
        this module closed for it opens again (only those: LATE_MARK)."""
        result = super()._distri_reopen()
        for promise in self.sudo():
            case = promise.charge_case_id
            if promise.state == "overdue" and case and case.state == "closed" \
                    and LATE_MARK in (case.resolution or ""):
                case.write({"state": "open", "resolution": False})
        return result
