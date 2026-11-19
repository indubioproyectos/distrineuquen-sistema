"""Idempotent sync of what the driver's phone recorded offline.

Every change on the phone is an event with a UUID generated on the phone.
The server keeps a log with a unique constraint on that UUID: a retried POST
finds the event already there and answers "duplicate" without applying it
again. Visits are upserted by their own UUID, and a later edit of the same
visit wins only if it was captured later on the phone.
"""
import json
import logging
import math
import re
from datetime import datetime, timedelta, timezone

from psycopg2 import IntegrityError, errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import UserError

from .despacho import LOAD_LOCKED
from .ar_phone import normalize_whatsapp
from .reparto_cliente import normalize_days, normalize_hours

_logger = logging.getLogger(__name__)

EVENT_TYPES = [
    ("load", "Carga de un pedido"),
    ("depart", "Salió a repartir"),
    ("visit", "Visita"),
    ("customer", "Ubicación y horarios"),
    ("whatsapp", "WhatsApp del cliente"),
    ("close", "Cierre de la ruta"),
    ("expense", "Gasto"),
    ("parcel", "Encomienda despachada"),
    # 19.0.1.35.0 (reparto_gps.py): «Llegué» and the location notice
    ("arrive", "Llegada al cliente"),
    ("gps_notice", "Aviso de ubicación aceptado"),
    # 19.0.1.86.0 (comprobante_app.py): the transfer receipt photographed in the app
    ("receipt", "Foto del comprobante"),
]

# events that need the route on the road (state on_route, or already closed
# where each one keeps its own rule below)
ON_ROAD_EVENTS = ("visit", "customer", "expense", "receipt", "parcel", "close")
NOT_DEPARTED = ("La ruta todavía no salió a repartir: cargá los pedidos y tocá «Salir a "
                "repartir» primero.")

UUID_RE = re.compile(r"^[0-9a-fA-F-]{16,64}$")
MAX_EVENTS = 200
MAX_CASH = 1e9
# 19.0.1.115.0: a visit never collects more than this many times the order
# plus the previous debt, above this floor (_check_visit_amounts)
PAID_FACTOR = 10
PAID_FLOOR = 200000.0
PARAM_PAID_FLOOR = "distri_pedido.reparto_tope_minimo"

# 19.0.1.93.0 (E2E-C C14): an edit older than the one the server has is not
# applied, and the phone is told so (status «discarded»), not «ok»
OLDER_EDIT = ("Esta edición es más vieja que la que ya tiene el servidor (otro celular o la hora "
              "del celular atrasada): no se aplicó. Abrí la visita, revisala y guardala de nuevo.")
# 19.0.1.93.0 (C15): concurrency errors go up to Odoo, which retries the
# whole request (odoo.service.model.retrying) instead of answering «error»
CONCURRENCY_ERRORS = (pg_errors.SerializationFailure, pg_errors.LockNotAvailable,
                      pg_errors.DeadlockDetected)
# 19.0.1.93.0 (C4)
RETAINED = ("Pedido retenido por administración: no lo entregues ni lo cobres. Marcalo «No se "
            "entregó» y volvelo al depósito. Motivo: %s")


class DiscardedEvent(Exception):
    """An event logged but not applied: the phone gets status «discarded»."""


def parse_phone_time(value):
    """ISO 8601 from the phone (Date.toISOString) -> naive UTC, or None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo:
        moment = moment.astimezone(timezone.utc).replace(tzinfo=None)
    return moment


def _text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def _number(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        raise UserError("Importe inválido.")
    if not math.isfinite(number) or number < 0 or number > MAX_CASH:
        raise UserError("Importe inválido.")
    return number


class DistriSyncEvent(models.Model):
    _name = "distri.sync.event"
    _description = "Evento sincronizado desde la app de reparto"
    _order = "received_at desc, id desc"
    _rec_name = "uuid"

    uuid = fields.Char(required=True, readonly=True, index=True)
    event_type = fields.Selection(EVENT_TYPES, string="Tipo", required=True, readonly=True)
    route_id = fields.Many2one("distri.route", string="Ruta", readonly=True,
                               ondelete="cascade", index=True)
    user_id = fields.Many2one("res.users", string="Usuario", readonly=True)
    payload = fields.Text(string="Datos", readonly=True)
    captured_at = fields.Datetime(string="Hora en el celular", readonly=True)
    received_at = fields.Datetime(string="Recibido", readonly=True,
                                  default=fields.Datetime.now)
    note = fields.Char(string="Nota", readonly=True)

    _uuid_unique = models.Constraint("unique(uuid)", "Ese evento ya se recibió.")

    # ----------------------------------------------------------- batch

    @api.model
    def process_batch(self, user, events):
        """Applies the events in order. Returns one result per event.

        status: "ok" (applied now), "duplicate" (already had it),
        "discarded" (logged but not applied, with the reason in «error»: an
        edit older than the one the server has, 19.0.1.93.0) or "error" with
        retry=False (the phone must drop it and show the error) or retry=True
        (temporary: try again later). A serialization failure is not caught:
        Odoo retries the whole request.
        """
        results = []
        if not isinstance(events, list):
            raise UserError("Formato inválido.")
        for event in events[:MAX_EVENTS]:
            results.append(self._process_one(user, event))
        return results

    def _process_one(self, user, event):
        result = self._process_one_inner(user, event)
        # 19.0.1.29.0: a rejection is never lost (the phone keeps it too)
        # and a successful resend closes the original one
        Rejection = self.env["distri.sync.rejection"].sudo()
        if result.get("status") == "error" and not result.get("retry"):
            Rejection._distri_record(user, event, result.get("error") or "")
        elif result.get("status") in ("ok", "duplicate") and isinstance(event, dict):
            Rejection._distri_resent(event, result["status"])
        return result

    def _process_one_inner(self, user, event):
        event_uuid = str((event or {}).get("uuid") or "") if isinstance(event, dict) else ""
        if not UUID_RE.match(event_uuid):
            return {"uuid": event_uuid, "status": "error", "retry": False,
                    "error": "Evento sin identificador."}
        if self.search_count([("uuid", "=", event_uuid)]):
            return {"uuid": event_uuid, "status": "duplicate"}
        discarded = False
        try:
            with self.env.cr.savepoint():
                try:
                    note = self._apply(user, event)
                except DiscardedEvent as exc:
                    note, discarded = str(exc), True
                self.create({
                    "uuid": event_uuid,
                    "event_type": event.get("type"),
                    "route_id": event.get("route_id"),
                    "user_id": user.id,
                    "payload": json.dumps(_without_photo(event.get("payload")),
                                          ensure_ascii=False)[:20000],
                    "captured_at": parse_phone_time(event.get("captured_at")),
                    "note": (note or "")[:250] or False,
                })
                self.env.flush_all()
        except CONCURRENCY_ERRORS:
            raise
        except IntegrityError:
            # the same uuid committed by a parallel request
            self.env.invalidate_all()
            if self.search_count([("uuid", "=", event_uuid)]):
                return {"uuid": event_uuid, "status": "duplicate"}
            _logger.exception("reparto: integrity error on event %s", event_uuid)
            return {"uuid": event_uuid, "status": "error", "retry": False,
                    "error": "Dato repetido o inválido."}
        except UserError as exc:
            self.env.invalidate_all()
            return {"uuid": event_uuid, "status": "error", "retry": False,
                    "error": str(exc.args[0] if exc.args else exc)}
        except Exception:
            self.env.invalidate_all()
            _logger.exception("reparto: could not apply event %s", event_uuid)
            return {"uuid": event_uuid, "status": "error", "retry": True,
                    "error": "Error del servidor. Se reintenta."}
        if discarded:
            return {"uuid": event_uuid, "status": "discarded", "error": OLDER_EDIT}
        return {"uuid": event_uuid, "status": "ok"}

    # ----------------------------------------------------------- apply

    def _route_for(self, user, event):
        try:
            route_id = int(event.get("route_id") or 0)
        except (TypeError, ValueError):
            route_id = 0
        route = self.env["distri.route"].browse(route_id).exists()
        if not route or route.driver_id != user or route.kind != "route":
            raise UserError("Esa ruta no es tuya.")
        return route

    def _stop_for(self, route, payload):
        try:
            stop_id = int(payload.get("stop_id") or 0)
        except (TypeError, ValueError):
            stop_id = 0
        stop = route.stop_ids.filtered(lambda s: s.id == stop_id)
        if not stop:
            raise UserError("Ese pedido no está en la ruta.")
        return stop

    def _apply(self, user, event):
        kind = event.get("type")
        payload = event.get("payload") or {}
        if kind not in dict(EVENT_TYPES) or not isinstance(payload, dict):
            raise UserError("Tipo de evento desconocido.")
        route = self._route_for(user, event)
        captured = parse_phone_time(event.get("captured_at")) or fields.Datetime.now()
        # a receipt photo is still welcome after the settlement: it resolves the
        # pending receipt that would otherwise be charged to the driver
        if route.state == "settled" and kind != "receipt":
            raise UserError("La ruta ya se rindió.")
        if kind in ("load", "depart", "visit", "expense", "parcel", "arrive") \
                and route.state == "closed":
            raise UserError("La ruta ya está cerrada.")
        # 19.0.1.91.0 (E2E-A A5): nothing is delivered, collected or closed
        # before «Salir a repartir», which needs the double check of the load
        # (despacho.py). An offline batch still works: its depart comes first
        # and the events after it see the route on the road.
        if kind in ON_ROAD_EVENTS and route.state == "draft":
            raise UserError(NOT_DEPARTED)
        return getattr(self, "_apply_%s" % kind)(user, route, payload, captured)

    def _apply_load(self, user, route, payload, captured):
        try:
            stop_id = int(payload.get("stop_id") or 0)
        except (TypeError, ValueError):
            stop_id = 0
        # a tick sent before the phone learned that the order stays at the
        # depot (despacho.py): nothing to do, and not an error to show
        if stop_id and route.left_order_ids.filtered(lambda l: l.stop_ref == stop_id):
            return "quedó en el depósito"
        stop = self._stop_for(route, payload)
        # «Controlado» closed the dispatch (despacho.py): the load is what
        # both people checked, the phone can't change it afterwards
        if route.departure_controlled_at and bool(payload.get("loaded")) != stop.loaded:
            raise UserError(LOAD_LOCKED)
        stop._set_loaded(bool(payload.get("loaded")), captured)

    def _apply_depart(self, user, route, payload, captured):
        """«Salir a repartir». Only with every order ticked by the driver and
        by the depot (despacho.py): otherwise rejected and the route stays
        «Asignada». The app sends it online, right after its load ticks.
        With reverse_load_order the driver accepted visiting in the reverse
        order of the loading."""
        if route.state != "draft":
            return "ya había salido"
        route._check_ready_to_depart()
        if payload.get("reverse_load_order"):
            route._sequence_by_reverse_load()
        route.write({"state": "on_route", "departed_at": captured})

    def _apply_close(self, user, route, payload, captured):
        if route.state == "closed":
            return "ya estaba cerrada"
        pending = route.stop_ids.filtered(lambda s: s.state == "pending")
        if pending:
            raise UserError("Quedan %s visitas sin estado." % len(pending))
        no_reason = route.stop_ids.filtered(
            lambda s: s.state == "not_delivered" and not s.not_delivered_reason)
        if no_reason:
            raise UserError("Falta el motivo de %s." % ", ".join(no_reason.order_id.mapped("name")))
        # parcel receipts must never get lost: no close without the guide photo
        no_guide = route.stop_ids.filtered(
            lambda s: s.state == "dispatched" and not (s.parcel_id and s.parcel_id.photo))
        if no_guide:
            raise UserError("Falta la foto de la guía de %s." % ", ".join(
                no_guide.order_id.mapped("name")))
        route.write({"state": "closed", "closed_at": captured})

    def _apply_customer(self, user, route, payload, captured):
        partner = self._route_partner(route, payload, "shipping_partner_id")
        vals = {}
        if payload.get("location") in ("confirm", "gps"):
            try:
                lat, lng = float(payload.get("lat")), float(payload.get("lng"))
            except (TypeError, ValueError):
                raise UserError("Ubicación inválida.")
            if not (-90 <= lat <= 90 and -180 <= lng <= 180):
                raise UserError("Ubicación inválida.")
            vals.update({
                "partner_latitude": lat,
                "partner_longitude": lng,
                "distri_location_user_id": user.id,
                "distri_location_date": captured,
            })
        if "days" in payload:
            vals["distri_delivery_days"] = normalize_days(payload.get("days")) or False
        if "hours" in payload:
            vals["distri_delivery_hours"] = normalize_hours(payload.get("hours")) or False
        if "note" in payload:
            vals["distri_driver_note"] = str(payload.get("note") or "")[:1000] or False
        if vals:
            partner.write(vals)

    def _apply_whatsapp(self, user, route, payload, captured):
        partner = self._route_partner(route, payload, "partner_id")
        number = _text(payload.get("number"), 32)
        if len(re.sub(r"\D", "", number)) < 8:
            raise UserError("El número de WhatsApp es muy corto.")
        try:
            number = normalize_whatsapp(number)
        except ValueError as error:
            raise UserError(str(error)) from None
        partner.write({
            "distri_whatsapp": number,
            "distri_whatsapp_confirmed": True,
            "distri_whatsapp_date": captured,
            "distri_whatsapp_user_id": user.id,
        })

    def _route_partner(self, route, payload, field):
        try:
            partner_id = int(payload.get("partner_id") or 0)
        except (TypeError, ValueError):
            partner_id = 0
        partners = route.stop_ids.mapped(field)
        partner = partners.filtered(lambda p: p.id == partner_id)
        if not partner:
            raise UserError("Ese cliente no está en la ruta.")
        return partner

    # ----------------------------------------------------------- visit

    def _apply_expense(self, user, route, payload, captured):
        return self.env["distri.route.expense"]._distri_apply(route, payload, captured)

    def _apply_receipt(self, user, route, payload, captured):
        """«Sacar foto del comprobante» (comprobante_app.py): the photo of a
        transfer receipt becomes a receipt to review in Cobros, the same
        record a photo received by WhatsApp becomes. The photo carries its
        own id, so a retry or a second event with the same photo never
        creates it twice."""
        stop = self._stop_for(route, payload)
        if stop.order_id.distri_encomienda:
            raise UserError("Ese pedido es una encomienda: no lleva comprobante de transferencia.")
        photo_uuid = str(payload.get("photo_uuid") or "")
        if not UUID_RE.match(photo_uuid):
            raise UserError("Foto sin identificador.")
        Message = self.env["distri.whatsapp.message"].sudo()
        existing = Message._distri_app_receipt_by_uuid(photo_uuid)
        if existing:
            if existing.distri_app_stop_id != stop:
                raise UserError("Esa foto es de otro pedido.")
            return "ya estaba"
        from .gasto import decode_receipt_file
        photo = decode_receipt_file(payload.get("photo"))
        amount = _number(payload.get("transfer_amount"))
        Message._distri_app_receipt_create(stop, user, photo, photo_uuid, captured, amount)
        return False

    def _apply_parcel(self, user, route, payload, captured):
        """«Despachar encomienda»: guide photo, guide number, shipping cost
        (only when Distrineuquén pays) and packages; or «No se pudo
        despachar» with the reason. Same visit upsert rules as a visit."""
        stop = self._stop_for(route, payload)
        if not stop.order_id.distri_encomienda:
            raise UserError("Ese pedido no es una encomienda.")
        visit_uuid = str(payload.get("visit_uuid") or "")
        if not UUID_RE.match(visit_uuid):
            raise UserError("Visita sin identificador.")
        other = self.env["distri.route.stop"].search(
            [("visit_uuid", "=", visit_uuid), ("id", "!=", stop.id)], limit=1)
        if other:
            raise UserError("Esa visita es de otro pedido.")
        if stop.visit_captured_at and captured < stop.visit_captured_at:
            raise DiscardedEvent("más vieja que la registrada: no se aplicó")
        if stop.order_id.distri_retenido and payload.get("outcome") != "not_delivered":
            raise UserError(RETAINED % (stop.order_id.distri_retenido_motivo or "-"))
        common = {
            "visit_uuid": visit_uuid, "visit_captured_at": captured,
            "visit_received_at": fields.Datetime.now(),
            "returns_answer": False, "code_mismatch": False, "cash_amount": 0.0,
            "transfer": False, "transfer_amount": 0.0, "receipt_answer": False,
            "sent_to_vendor": False,
            "comment": _text(payload.get("comment"), 250) or False,
        }
        if payload.get("outcome") == "not_delivered":
            if stop.parcel_id:
                raise UserError("La encomienda ya se despachó.")
            stop.write(dict(common, state="not_delivered", delivery_state="not_delivered",
                            not_delivered_reason=_text(payload.get("reason"), 250) or False))
            return False
        if payload.get("outcome") != "dispatched":
            raise UserError("Falta indicar si se despachó.")
        from .gasto import decode_photo
        photo = payload.get("photo")
        if photo:
            photo = decode_photo(photo)
        elif not stop.parcel_id.photo:
            raise UserError("Falta la foto de la guía.")
        payer = stop.order_id.distri_envio_paga or "distri"
        cost = _number(payload.get("cost"))
        if payer == "distri" and cost <= 0:
            raise UserError("Poné cuánto pagaste el envío.")
        if payer != "distri":
            cost = 0.0
        try:
            packages = int(payload.get("packages") or stop.parcels or 0)
        except (TypeError, ValueError):
            raise UserError("Bultos inválidos.")
        if packages < 1 or packages > 999:
            raise UserError("Poné cuántos bultos despachaste.")
        stop.write(dict(common, state="dispatched", delivery_state="dispatched",
                        not_delivered_reason=False))
        self.env["distri.parcel"]._distri_record(stop, {
            "guide_number": _text(payload.get("guide_number"), 40),
            "cost": cost, "packages": packages,
            "comment": _text(payload.get("comment"), 250),
        }, photo, captured)
        self.env["distri.route.expense"]._trigger_read()
        return False

    def _apply_visit(self, user, route, payload, captured):
        stop = self._stop_for(route, payload)
        if stop.order_id.distri_encomienda:
            raise UserError("Ese pedido es una encomienda: se carga con «Despachar encomienda».")
        visit_uuid = str(payload.get("visit_uuid") or "")
        if not UUID_RE.match(visit_uuid):
            raise UserError("Visita sin identificador.")
        other = self.env["distri.route.stop"].search(
            [("visit_uuid", "=", visit_uuid), ("id", "!=", stop.id)], limit=1)
        if other:
            raise UserError("Esa visita es de otro pedido.")
        if stop.visit_captured_at and captured < stop.visit_captured_at:
            raise DiscardedEvent("más vieja que la registrada: no se aplicó")
        if stop.order_id.distri_retenido and payload.get("outcome") != "not_delivered":
            raise UserError(RETAINED % (stop.order_id.distri_retenido_motivo or "-"))
        self._record_visit(stop, visit_uuid, payload, captured)
        return False

    def _record_visit(self, stop, visit_uuid, payload, captured):
        """Writes a visit on its stop: states, returns and pending receipt.

        Shared by the phone (_apply_visit) and the delivery at the depot
        (deposito.py), so both follow the same rules.
        """
        vals = self._visit_values(stop, payload)
        vals.update({
            "visit_uuid": visit_uuid,
            "visit_captured_at": captured,
            "visit_received_at": fields.Datetime.now(),
        })
        returns = vals.pop("_returns")
        stop.write(vals)
        self._sync_returns(stop, returns)
        self._sync_promise(stop, captured)
        return stop

    def _visit_values(self, stop, payload):
        outcome = payload.get("outcome")
        if outcome == "not_delivered":
            return {
                "state": "not_delivered",
                "delivery_state": "not_delivered",
                "not_delivered_reason": _text(payload.get("reason"), 250) or False,
                "returns_answer": False,
                "code_mismatch": False,
                "cash_amount": 0.0,
                "transfer": False,
                "transfer_amount": 0.0,
                "receipt_answer": False,
                "sent_to_vendor": False,
                "red_breach": False,
                "comment": _text(payload.get("comment"), 250) or False,
                "_returns": [],
            }
        if outcome != "delivered":
            raise UserError("Falta indicar si se entregó.")

        answer = payload.get("returns_answer")
        if answer not in ("none", "yes"):
            raise UserError("Falta responder si devolvió algo.")
        returns = self._clean_returns(stop, payload.get("returns") if answer == "yes" else [])
        if answer == "yes" and not returns:
            raise UserError("Cargá lo que devolvió.")

        # 19.0.1.99.0 (audit F12): the wrong codes the phone counted offline
        # add to the order's tries once (a later edit of the visit sends the
        # same running count)
        try:
            tries = min(max(int(payload.get("code_attempts") or 0), 0), 99)
        except (TypeError, ValueError):
            tries = 0
        if tries > (stop.code_attempts or 0):
            stop.order_id._distri_add_receipt_attempts(tries - (stop.code_attempts or 0))
            stop.code_attempts = tries

        code = str(payload.get("code") or "").strip()
        if code:
            code_ok = stop.order_id._distri_check_receipt_code(code)
            delivery = "delivered_code" if code_ok else "delivered_no_code"
            mismatch = not code_ok
        elif payload.get("code_already_ok") and stop.delivery_state == "delivered_code":
            # an edit of a visit whose code the server already validated
            delivery, mismatch = "delivered_code", False
        elif payload.get("without_code"):
            delivery, mismatch = "delivered_no_code", False
        else:
            raise UserError("Falta el código de recibido o «Entregar igual».")

        cash = _number(payload.get("cash"))
        transfer, transfer_amount, answer_receipt = self._transfer_values(
            stop, payload, cash, returns)
        self._check_visit_amounts(stop, cash + transfer_amount)

        if payload.get("no_payment"):
            if cash or transfer != "no":
                raise UserError("«No pagó nada» no lleva efectivo ni transferencia.")
            state = "not_paid"
        elif cash > 0 or transfer == "yes":
            state = "paid"
        else:
            state = "not_paid"

        return {
            "state": state,
            "delivery_state": delivery,
            "code_mismatch": mismatch,
            "returns_answer": answer,
            "not_delivered_reason": False,
            "cash_amount": cash,
            "transfer": transfer,
            "transfer_amount": transfer_amount,
            "receipt_answer": answer_receipt,
            "sent_to_vendor": False,
            "comment": _text(payload.get("comment"), 250) or False,
            "_returns": returns,
        }

    def _check_visit_amounts(self, stop, paid):
        """19.0.1.115.0: a sanity bound on what a visit collects (cash plus
        transfer). An amount read wrong on the phone («14.307.60» as
        1.430.760) is refused instead of reaching the settlement: more than
        PAID_FACTOR times the order plus the previous debt, and more than the
        floor (parameter distri_pedido.reparto_tope_minimo)."""
        if not paid:
            return
        order = stop.order_id
        try:
            floor = float(self.env["ir.config_parameter"].sudo().get_param(
                PARAM_PAID_FLOOR) or PAID_FLOOR)
        except ValueError:
            floor = PAID_FLOOR
        reference = stop.order_amount or 0.0
        if paid <= max(PAID_FACTOR * reference, floor):
            return
        # the previous debt (slower) only when the order alone is exceeded
        reference += max(order._distri_previous_debt() or 0.0, 0.0)
        if paid > max(PAID_FACTOR * reference, floor):
            raise UserError(
                "El cobro de %s (%s) es demasiado: el pedido y la deuda suman %s. Revisá el "
                "importe: los miles van con punto y los centavos con coma (ej.: 14.307,60)." % (
                    order.name, _money_text(paid), _money_text(reference)))

    def _transfer_values(self, stop, payload, cash, returns):
        """«¿Paga con transferencia?»: (transfer, amount, receipt answer).

        «Sí» needs the amount transferred and the driver's answer about the
        receipt: «Sacar foto del comprobante» (the photo travels in its own
        «receipt» event), «El cliente lo manda por WhatsApp» or «Lo manda
        después»; «Ya lo reenvié» from the app of before 19.0.1.86.0. «Transfiere
        después» no longer exists. A phone with the app of before
        19.0.1.25.0 may still send «Ya transfirió» ("done"): it counts as a
        transfer of what was left to collect, with the receipt still to come.
        """
        transfer = payload.get("transfer") or "no"
        if transfer == "later":
            raise UserError("«Transfiere después» ya no existe: el cliente transfiere en el "
                            "momento. Abrí la app con señal para actualizarla.")
        if transfer not in ("no", "yes", "done"):
            raise UserError("Transferencia inválida.")
        if transfer == "no":
            return "no", 0.0, False
        if transfer == "done":
            returned = sum(item["qty"] * ((item["line"].price_total / item["line"].product_uom_qty)
                                          if item["line"].product_uom_qty else 0.0)
                           for item in returns)
            due = stop._distri_due_today(returned)
            return "yes", max(due - cash, 0.0), "not_yet"
        amount = _number(payload.get("transfer_amount"))
        if amount <= 0:
            raise UserError("Poné el importe transferido.")
        answer = payload.get("receipt_answer")
        # «forwarded»: the app of before 19.0.1.86.0 (no photo in the app)
        if answer not in ("photo", "customer_wa", "not_yet", "forwarded"):
            raise UserError("Elegí cómo llega el comprobante: foto, WhatsApp del cliente o "
                            "lo manda después.")
        return "yes", amount, answer

    def _clean_returns(self, stop, items):
        if not isinstance(items, list):
            raise UserError("Devoluciones inválidas.")
        lines = stop.order_id.order_line.filtered(lambda l: not l.display_type)
        total_by_line = {}
        out = []
        for item in items[:100]:
            if not isinstance(item, dict):
                raise UserError("Devoluciones inválidas.")
            ret_uuid = str(item.get("uuid") or "")
            if not UUID_RE.match(ret_uuid):
                raise UserError("Devolución sin identificador.")
            try:
                line_id = int(item.get("line_id") or 0)
                qty = float(item.get("qty") or 0)
            except (TypeError, ValueError):
                raise UserError("Devolución inválida.")
            line = lines.filtered(lambda l: l.id == line_id)
            if not line:
                raise UserError("Ese producto no es del pedido.")
            if not math.isfinite(qty) or qty <= 0:
                raise UserError("La cantidad devuelta tiene que ser mayor que cero.")
            total_by_line[line_id] = total_by_line.get(line_id, 0) + qty
            if total_by_line[line_id] > line.product_uom_qty + 1e-6:
                raise UserError("Devolvió más de lo que se entregó de %s." % line.product_id.name)
            reason = _text(item.get("reason"), 200)
            if not reason:
                raise UserError("Falta el motivo de la devolución.")
            out.append({"uuid": ret_uuid, "line": line, "qty": qty, "reason": reason})
        return out

    def _sync_returns(self, stop, returns):
        Return = self.env["distri.route.return"]
        keep = Return
        for item in returns:
            line = item["line"]
            unit = (line.price_total / line.product_uom_qty) if line.product_uom_qty else 0.0
            existing = Return.search([("uuid", "=", item["uuid"])], limit=1)
            if existing and existing.stop_id != stop:
                raise UserError("Esa devolución es de otro pedido.")
            vals = {"order_line_id": line.id, "quantity": item["qty"],
                    "reason": item["reason"], "unit_price": unit}
            if existing:
                existing.write(vals)
                keep |= existing
            else:
                keep |= Return.create(dict(vals, uuid=item["uuid"], stop_id=stop.id))
        gone = stop.return_ids - keep
        if gone.filtered(lambda r: r.settlement_state != "pending"):
            raise UserError("Una devolución ya se controló en el depósito.")
        gone.unlink()

    def _sync_promise(self, stop, captured):
        """The visit's «Comprobante pendiente», its charges to the driver and
        the «Cliente en rojo» check (comprobante_pendiente.py)."""
        self.env["distri.promised.transfer"]._distri_sync_from_stop(stop, captured)


REJECTION_STATES = [
    ("open", "Pendiente"),
    ("resent", "Reenviado"),
    ("resolved", "Resuelto por administración"),
]


class DistriSyncRejection(models.Model):
    """«Cambios rechazados de la app» (19.0.1.29.0).

    A change the server rejects for good (retry=False) used to leave the
    phone with only its error text: dismissing the banner lost it, and a
    rejected visit could hide a cash collection from the settlement. Now
    each rejection is kept here with its payload until the driver resends
    it corrected (a new event with a new uuid that names the original in
    «resends»: never counted twice) or administración marks it resolved.
    The settlement of its route is blocked while it is open.
    """
    _name = "distri.sync.rejection"
    _description = "Cambio rechazado de la app de reparto"
    _inherit = ["mail.thread"]
    _order = "state, rejected_at desc, id desc"
    _rec_name = "summary"

    uuid = fields.Char(string="Id del cambio original", required=True, readonly=True, index=True,
                       copy=False)
    last_uuid = fields.Char(string="Id del último envío", readonly=True, copy=False)
    event_type = fields.Selection(EVENT_TYPES, string="Tipo", readonly=True)
    user_id = fields.Many2one("res.users", string="Repartidor", readonly=True, index=True)
    route_id = fields.Many2one("distri.route", string="Ruta", readonly=True, index=True,
                               ondelete="set null")
    stop_id = fields.Many2one("distri.route.stop", string="Visita", readonly=True,
                              ondelete="set null")
    partner_id = fields.Many2one(related="stop_id.partner_id", string="Cliente")
    order_id = fields.Many2one(related="stop_id.order_id", string="Pedido")
    currency_id = fields.Many2one(related="route_id.currency_id")
    cash = fields.Monetary(string="Efectivo del cambio", readonly=True,
                           help="Efectivo que traía el cambio rechazado: no está en la rendición "
                                "hasta que se reenvíe.")
    amount = fields.Monetary(string="Importe del cambio", readonly=True,
                             help="Transferencia o gasto que traía el cambio rechazado.")
    error = fields.Char(string="Motivo del rechazo", readonly=True)
    payload = fields.Text(string="Datos enviados", readonly=True)
    captured_at = fields.Datetime(string="Hora en el celular", readonly=True)
    rejected_at = fields.Datetime(string="Rechazado", readonly=True)
    attempts = fields.Integer(string="Intentos", readonly=True, default=1)
    summary = fields.Char(string="Cambio", compute="_compute_summary", store=True)
    state = fields.Selection(REJECTION_STATES, string="Estado", default="open", required=True,
                             readonly=True, index=True, tracking=True)
    resolution = fields.Text(string="Resolución")
    resolved_by_id = fields.Many2one("res.users", string="Resolvió", readonly=True)
    resolved_at = fields.Datetime(string="Resuelto el", readonly=True)

    _uuid_unique = models.Constraint("unique(uuid)", "Ese rechazo ya está registrado.")

    @api.depends("event_type", "stop_id", "route_id", "cash", "amount")
    def _compute_summary(self):
        labels = dict(EVENT_TYPES)
        for rec in self:
            parts = [labels.get(rec.event_type, rec.event_type or "Cambio")]
            if rec.stop_id:
                parts.append("%s · %s" % (rec.stop_id.order_id.name or "",
                                          rec.stop_id.partner_id.display_name or ""))
            if rec.cash:
                parts.append("efectivo %s" % _money_text(rec.cash))
            elif rec.amount:
                parts.append(_money_text(rec.amount))
            rec.summary = " · ".join(p for p in parts if p)

    @api.model
    def _distri_record(self, user, event, error):
        """A rejection (or a rejected resend: the same record, updated)."""
        if not isinstance(event, dict):
            return self.browse()
        event_uuid = str(event.get("uuid") or "")
        root = str(event.get("resends") or "")
        root = root if UUID_RE.match(root) else event_uuid
        if not UUID_RE.match(root or ""):
            return self.browse()
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        try:
            route_id = int(event.get("route_id") or 0)
        except (TypeError, ValueError):
            route_id = 0
        route = self.env["distri.route"].sudo().browse(route_id).exists()
        if route and route.driver_id != user:
            route = self.env["distri.route"]
        try:
            stop_id = int(payload.get("stop_id") or 0)
        except (TypeError, ValueError):
            stop_id = 0
        stop = route.stop_ids.filtered(lambda s: s.id == stop_id) if route else False
        vals = {
            "last_uuid": event_uuid,
            "event_type": event.get("type") if event.get("type") in dict(EVENT_TYPES) else False,
            "user_id": user.id,
            "route_id": route.id if route else False,
            "stop_id": stop.id if stop else False,
            "cash": _safe_number(payload.get("cash")),
            "amount": _safe_number(payload.get("transfer_amount") or payload.get("amount")
                                   or payload.get("cost")),
            "error": str(error)[:500],
            "payload": json.dumps(_without_photo(payload), ensure_ascii=False)[:20000],
            "captured_at": parse_phone_time(event.get("captured_at")),
            "rejected_at": fields.Datetime.now(),
            "state": "open",
        }
        rec = self.search([("uuid", "=", root)], limit=1)
        if rec:
            if rec.last_uuid == event_uuid:
                return rec  # the same POST again
            rec.write(dict(vals, attempts=rec.attempts + 1))
            return rec
        rec = self.create(dict(vals, uuid=root))
        if rec.route_id:
            rec.route_id.message_post(body="La app rechazó un cambio: %s. Motivo: %s" % (
                rec.summary, rec.error))
        return rec

    @api.model
    def _distri_resent(self, event, status):
        """A resend went through: its rejection is closed. A close, a
        departure or a load that goes through also closes the rejected ones
        of the same kind on that route (they carry no money and the app
        redoes them from its screen)."""
        if status == "ok" and event.get("type") in ("close", "depart", "load"):
            try:
                route_id = int(event.get("route_id") or 0)
            except (TypeError, ValueError):
                route_id = 0
            same = self.search([("route_id", "=", route_id), ("state", "=", "open"),
                                ("event_type", "=", event.get("type"))])
            if event.get("type") == "load":
                payload = event.get("payload") or {}
                same = same.filtered(lambda r: r.stop_id.id == payload.get("stop_id"))
            same.write({"state": "resent", "last_uuid": str(event.get("uuid") or ""),
                        "resolved_at": fields.Datetime.now()})
        root = str(event.get("resends") or "")
        if not UUID_RE.match(root):
            return self.browse()
        rec = self.search([("uuid", "=", root), ("state", "=", "open")], limit=1)
        if rec:
            rec.write({"state": "resent", "last_uuid": str(event.get("uuid") or ""),
                       "resolved_at": fields.Datetime.now()})
        return rec

    def action_resolve(self):
        """«Marcar resuelto» (administración), with the resolution written."""
        if not (self.env.su or self.env.user.has_group("distri_pedido.grupo_administracion")):
            raise UserError("Solo administración marca un cambio rechazado como resuelto.")
        for rec in self:
            if rec.state != "open":
                raise UserError("Ese cambio ya no está pendiente.")
            if not (rec.resolution or "").strip():
                raise UserError("Escribí la resolución antes de marcarlo resuelto.")
            rec.write({"state": "resolved", "resolved_by_id": self.env.user.id,
                       "resolved_at": fields.Datetime.now()})
        return True

    @api.model
    def _distri_app_payload(self, user):
        """For the phone: which of its rejections are still open on the
        server (the others it can drop)."""
        recs = self.sudo().search([("user_id", "=", user.id),
                                   ("rejected_at", ">=", fields.Datetime.now()
                                    - timedelta(days=30))])
        return [{"uuid": r.uuid, "state": r.state, "resolution": r.resolution or ""}
                for r in recs]


def _safe_number(value):
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return number if math.isfinite(number) and abs(number) <= MAX_CASH else 0.0


def _money_text(amount):
    from .reparto import _money
    return _money(amount)


def _without_photo(payload):
    """The event as logged: the photo lives in its record, not in the log."""
    payload = dict(payload or {}) if isinstance(payload, dict) else {}
    if payload.get("photo"):
        payload["photo"] = "(foto de %s caracteres)" % len(str(payload["photo"]))
    # the coordinates live only in distri.route.gps, purged after the
    # retention period (reparto_gps.py); the log never keeps them
    if "gps" in payload:
        payload["gps"] = "(ubicación)"
    return payload


class DistriRouteRejections(models.Model):
    _inherit = "distri.route"

    rejection_ids = fields.One2many("distri.sync.rejection", "route_id",
                                    string="Cambios rechazados de la app")
    rejection_open_count = fields.Integer(string="Cambios rechazados sin resolver",
                                          compute="_compute_rejection_open_count")

    @api.depends("rejection_ids.state")
    def _compute_rejection_open_count(self):
        for route in self:
            route.rejection_open_count = len(route.rejection_ids.filtered(
                lambda r: r.state == "open"))

    def action_settle(self):
        """Nothing is settled without seeing what the app could not send."""
        for route in self:
            pending = route.rejection_ids.filtered(lambda r: r.state == "open")
            if pending:
                raise UserError(
                    "La app rechazó %s %s de esta ruta y siguen sin resolver: %s. El repartidor "
                    "tiene que reenviarlos corregidos, o administración marcarlos resueltos en "
                    "Reparto › Cambios rechazados de la app." % (
                        len(pending), "cambio" if len(pending) == 1 else "cambios",
                        "; ".join(pending.mapped("summary"))))
        return super().action_settle()

    def action_open_rejections(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Cambios rechazados de la app",
            "res_model": "distri.sync.rejection",
            "view_mode": "list,form",
            "domain": [("route_id", "=", self.id)],
        }

    def _app_payload(self):
        payload = super()._app_payload()
        # the phone drops the rejections the server already closed
        payload["rejections"] = self.env["distri.sync.rejection"].sudo()._distri_app_payload(
            self.driver_id)
        return payload
