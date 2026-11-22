"""«Despacho»: two people check every order that goes on the truck.

The driver ticks each order on the phone when it goes up («Control de carga»,
stop.loaded, reparto_sync._apply_load) and the dispatcher (administración or
gerencia, never the route's own driver) ticks it on /despacho
(controllers/despacho.py). Per order, not per parcel: later the driver can't
claim that an order was missing.

- The route leaves («Salir a repartir») only with every order ticked by both.
  The phone must be online for that (connection at the depot) and the server
  rejects the «depart» event otherwise.
- An order that does not go «queda en el depósito»: it is taken off the
  route the same way as removing its line before departure (the stop is
  deleted, so the order is free again for any route, the depot delivery and
  the customer's page), and a distri.route.left.order keeps who, when and why.
  It can be undone before the route is controlled.
- «Controlado» (action_mark_controlled) closes the dispatch: only with every
  order double-checked. Routes that left before this check existed (not in
  «Asignada») are not blocked.
"""
from zoneinfo import ZoneInfo

from odoo import api, fields, models
from odoo.exceptions import UserError

from .reparto_cliente import today_ar

GROUP_DISPATCH = "distri_pedido.grupo_administracion"
# 19.0.1.127.0 (owner): who may control the load of a route he drives himself
# (loose or out-of-zone orders): «Administración contable» and gerencia. A
# driver without those groups keeps the double control by someone else.
SELF_DISPATCH_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")
SELF_DISPATCH_NOTE = "Autodespacho: %s controló su propia carga"
AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")

LOAD_STATES = [
    ("both", "Doble control"),
    ("driver", "Solo el repartidor"),
    ("dispatch", "Solo el depósito"),
    ("none", "Sin controlar"),
]

LOAD_LOCKED = "La carga ya fue controlada con Administración."

WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]


# 19.0.1.119.0: «Bajar al depósito» on /despacho asks no reason
LEAVE_DEFAULT_REASON = "Bajado en el despacho"


def first_name(user):
    return (user.name or "").split(" ")[0] if user else ""


def can_self_dispatch(user):
    """19.0.1.127.0: the user may control the load of his own route."""
    return bool(user) and any(user.has_group(group) for group in SELF_DISPATCH_GROUPS)


def ar_time(moment):
    """«14:05» in Argentina for a UTC datetime."""
    if not moment:
        return ""
    return moment.replace(tzinfo=ZoneInfo("UTC")).astimezone(AR_TZ).strftime("%H:%M")


def day_label(day):
    today = today_ar()
    if day == today:
        return "hoy"
    if (day - today).days == 1:
        return "mañana"
    return "%s %s" % (WEEKDAYS[day.weekday()], day.strftime("%d/%m"))


class DistriRouteStopDispatch(models.Model):
    _inherit = "distri.route.stop"

    loaded_at = fields.Datetime(string="Cargado a las", readonly=True, copy=False)
    load_sequence = fields.Integer(
        string="Orden de carga", readonly=True, copy=False,
        help="1 es lo primero que subió a la camioneta: se entrega último.")
    dispatch_checked = fields.Boolean(string="Controlado por el depósito", readonly=True,
                                      copy=False)
    dispatch_checked_by_id = fields.Many2one("res.users", string="Controló en el depósito",
                                             readonly=True, copy=False)
    dispatch_checked_at = fields.Datetime(string="Controlado en el depósito el", readonly=True,
                                          copy=False)
    load_state = fields.Selection(LOAD_STATES, string="Control de carga",
                                  compute="_compute_load_state", store=True)

    @api.depends("loaded", "dispatch_checked")
    def _compute_load_state(self):
        for stop in self:
            if stop.loaded and stop.dispatch_checked:
                stop.load_state = "both"
            elif stop.loaded:
                stop.load_state = "driver"
            elif stop.dispatch_checked:
                stop.load_state = "dispatch"
            else:
                stop.load_state = "none"

    # ------------------------------------------------------------ driver

    def _set_loaded(self, loaded, when):
        """The driver's tick from the phone. The loading order is the order
        in which the ticks arrive (the phone's outbox is first in, first out);
        a repeated tick keeps its place."""
        for stop in self:
            if bool(loaded) == stop.loaded:
                continue
            if loaded:
                last = max(stop.route_id.stop_ids.mapped("load_sequence") or [0])
                stop.write({"loaded": True, "loaded_at": when, "load_sequence": last + 1})
            else:
                stop.write({"loaded": False, "loaded_at": False, "load_sequence": 0})

    # -------------------------------------------------------- dispatcher

    def _check_dispatcher(self):
        self.route_id._check_dispatcher()

    def action_dispatch_check(self):
        self._check_dispatcher()
        self.filtered(lambda s: not s.dispatch_checked).write({
            "dispatch_checked": True,
            "dispatch_checked_by_id": self.env.user.id,
            "dispatch_checked_at": fields.Datetime.now(),
        })
        return True

    def action_dispatch_uncheck(self):
        self._check_dispatcher()
        self.write({"dispatch_checked": False, "dispatch_checked_by_id": False,
                    "dispatch_checked_at": False})
        return True

    def action_leave_at_depot(self, reason=None):
        """«Bajar al depósito» (was «Queda en el depósito»): off the route,
        free for another one. 19.0.1.119.0: one tap on /despacho, so the
        reason is optional (LEAVE_DEFAULT_REASON when none is given)."""
        self._check_dispatcher()
        reason = " ".join(str(reason or "").split()) or LEAVE_DEFAULT_REASON
        return self._distri_leave_at_depot(reason)

    def _distri_leave_at_depot(self, reason):
        """The move itself, without the dispatcher's checks: also used when a
        held order leaves a route already «Controlado» that did not depart
        (models/revisar.py, 19.0.1.93.0)."""
        reason = " ".join(str(reason or "").split())[:250]
        if not reason:
            raise UserError("Escribí por qué queda en el depósito.")
        Left = self.env["distri.route.left.order"]
        for stop in self:
            if stop.state != "pending":
                raise UserError("El pedido %s ya tiene la visita." % stop.order_id.name)
            route, order = stop.route_id, stop.order_id
            left = Left.create({
                "route_id": route.id,
                "order_id": order.id,
                "parcels": stop.parcels,
                "zone_id": stop.zone_id.id,
                "sequence": stop.sequence,
                "stop_ref": stop.id,
                "reason": reason,
                "user_id": self.env.user.id,
                "left_at": fields.Datetime.now(),
            })
            text = "%s (%s, %s bultos) queda en el depósito: %s. Lo marcó %s." % (
                order.name, stop.partner_id._distri_app_name(), stop.parcels, reason,
                self.env.user.name)
            route.message_post(body=text)
            order.message_post(body="Quedó en el depósito, fuera de la ruta %s: %s. Lo marcó %s. "
                                    "El pedido queda libre para otra ruta."
                                    % (route.name, reason, self.env.user.name))
            stop.unlink()
            Left |= left
        return Left


    # ---------------------------------------------------------- payloads

    def _dispatch_app_payload(self):
        self.ensure_one()
        return {
            "id": self.id,
            "loaded": self.loaded,
            "dispatch_checked": self.dispatch_checked,
            "dispatch_by": first_name(self.dispatch_checked_by_id),
            "load_sequence": self.load_sequence,
        }

    def _dispatch_row(self, me, driver):
        self.ensure_one()
        partner = self.partner_id
        ship = self.shipping_partner_id or partner
        mine = self.dispatch_checked_by_id == me
        checker = "Vos" if mine else first_name(self.dispatch_checked_by_id)
        if self.load_state == "both":
            message = "Controlado por %s y %s" % (driver, "vos" if mine else checker)
        elif self.load_state == "driver":
            message = "%s lo tildó, vos todavía no" % driver
        elif self.load_state == "dispatch":
            message = "%s lo tildaste, falta %s" % (checker, driver) if mine else \
                "%s lo tildó, falta %s" % (checker, driver)
        else:
            message = "Sin tildar"
        address = ", ".join(p for p in (ship.street, ship.street2) if p)
        if self.is_parcel:
            address = "Encomienda · %s" % (self.order_id.sudo()._distri_parcel_town() or "")
        return {
            "id": self.id,
            "name": partner._distri_app_name(),
            "address": address,
            "order": self.order_id.name,
            # «Etiquetas» (19.0.1.33.0): the labels of this order
            "order_id": self.order_id.id,
            "packed": bool(self.order_id.distri_armado == "armado" and self.order_id.distri_bultos),
            "parcels": self.parcels,
            "loaded": self.loaded,
            "load_sequence": self.load_sequence if self.loaded else 0,
            "dispatch_checked": self.dispatch_checked,
            "load_state": self.load_state,
            "mismatch": self.load_state in ("driver", "dispatch"),
            "message": message,
        }


class DistriRouteDispatch(models.Model):
    _inherit = "distri.route"

    left_order_ids = fields.One2many("distri.route.left.order", "route_id",
                                     string="Quedaron en el depósito")
    checked_stop_count = fields.Integer(string="Pedidos con doble control",
                                        compute="_compute_dispatch", store=True)
    parcel_checked_count = fields.Integer(string="Bultos con doble control",
                                          compute="_compute_dispatch", store=True)
    left_order_count = fields.Integer(string="Quedan en el depósito",
                                      compute="_compute_dispatch", store=True)
    dispatch_missing = fields.Boolean(string="Falta doble control", compute="_compute_dispatch",
                                      store=True)

    @api.depends("stop_ids.load_state", "stop_ids.order_id.distri_bultos", "left_order_ids")
    def _compute_dispatch(self):
        for route in self:
            both = route.stop_ids.filtered(lambda s: s.load_state == "both")
            route.checked_stop_count = len(both)
            route.parcel_checked_count = sum(both.mapped("parcels"))
            route.left_order_count = len(route.left_order_ids)
            route.dispatch_missing = len(both) < len(route.stop_ids)

    # ------------------------------------------------------------ checks

    def _check_dispatcher(self):
        """Administración or gerencia, and not the route's own driver unless
        he may self-dispatch (19.0.1.127.0, noted on the route)."""
        user = self.env.user
        if not (self.env.su or user.has_group(GROUP_DISPATCH)):
            raise UserError("Solo administración y gerencia controlan la carga.")
        for route in self:
            route._check_own_load(user)
            if route.kind != "route":
                raise UserError("Solo para las rutas de reparto.")
            if route.state != "draft":
                raise UserError("La ruta %s ya salió." % route.name)
            if route.departure_controlled_at:
                raise UserError("La salida de %s ya se controló." % route.name)
        for route in self.filtered(lambda r: r.driver_id == user):
            route._note_self_dispatch(user)

    def _check_own_load(self, user):
        """The route's own driver controls his load only with
        SELF_DISPATCH_GROUPS (19.0.1.127.0)."""
        self.ensure_one()
        if self.driver_id == user and not can_self_dispatch(user):
            raise UserError("La carga la controla otra persona, no el repartidor de la ruta.")

    def _note_self_dispatch(self, user):
        """«Autodespacho: X controló su propia carga», once per route."""
        self.ensure_one()
        body = SELF_DISPATCH_NOTE % user.name
        if self.env["mail.message"].sudo().search_count([
                ("model", "=", self._name), ("res_id", "=", self.id),
                ("body", "ilike", body)], limit=1):
            return
        self.sudo().message_post(body=body)

    def _dispatch_missing_lines(self):
        """One line per order still without both ticks."""
        self.ensure_one()
        driver = first_name(self.driver_id) or "el repartidor"
        what = {"driver": "falta el depósito", "dispatch": "falta %s" % driver,
                "none": "faltan los dos"}
        return ["%s · %s: %s" % (s.order_id.name, s.partner_id._distri_app_name(),
                                 what[s.load_state])
                for s in self.stop_ids if s.load_state != "both"]

    def _check_dispatch_complete(self, action):
        """Every order on the route ticked by the driver and by the depot."""
        for route in self:
            if not route.stop_ids:
                raise UserError("La ruta %s no tiene pedidos." % route.name)
            missing = route._dispatch_missing_lines()
            if missing:
                raise UserError("%s: falta el doble control de %s de %s pedidos.\n%s" % (
                    action, len(missing), len(route.stop_ids), "\n".join(missing)))

    def _dispatch_summary(self):
        self.ensure_one()
        text = "Salida controlada por %s: %s pedidos y %s bultos con doble control." % (
            self.env.user.name, self.checked_stop_count, self.parcel_checked_count)
        if self.left_order_ids:
            text += " Quedaron en el depósito: %s." % "; ".join(
                "%s (%s): %s" % (l.order_id.name, l.partner_id._distri_app_name(), l.reason)
                for l in self.left_order_ids)
        return text

    # ------------------------------------------------------ the phone

    def _sequence_by_reverse_load(self):
        """Visit order = loading order reversed: what went up first is at the
        back of the truck and is delivered last. Only when the driver asks."""
        self.ensure_one()
        stops = self.stop_ids.sorted(lambda s: (s.load_sequence, s.sequence, s.id), reverse=True)
        for index, stop in enumerate(stops, start=1):
            stop.sequence = index * 10
        self.message_post(body="%s ordenó las visitas al revés de la carga: %s." % (
            self.driver_id.name, ", ".join(stops.order_id.mapped("name"))))

    def _check_ready_to_depart(self):
        self._check_dispatch_complete("Todavía no podés salir")

    def _load_status_payload(self):
        """What the phone polls on «Control de carga»."""
        self.ensure_one()
        return {
            "id": self.id,
            "state": self.state,
            "controlled": bool(self.departure_controlled_at),
            "stops": [stop._dispatch_app_payload() for stop in self.stop_ids],
            "left": self._left_payload(),
        }

    def _left_payload(self):
        return [{
            "id": left.id,
            "order": left.order_id.name,
            "name": left.partner_id._distri_app_name(),
            "parcels": left.parcels,
            "reason": left.reason,
            "by": first_name(left.user_id),
        } for left in self.left_order_ids]

    # ---------------------------------------------------- /despacho

    @api.model
    def _despacho_routes(self):
        return self.search([("kind", "=", "route"), ("state", "=", "draft"),
                            ("company_id", "in", self.env.companies.ids)],
                           order="date, trip, id")

    def _despacho_card(self):
        self.ensure_one()
        return {
            "id": self.id,
            "driver": self.driver_id.name or "",
            "trip": self.trip,
            "day": day_label(self.date),
            "zones": ", ".join(self.zone_ids.mapped("name")),
            "orders_total": len(self.stop_ids),
            "orders_checked": self.checked_stop_count,
            "controlled": bool(self.departure_controlled_at),
            "is_driver": self.driver_id == self.env.user,
        }

    def _despacho_payload(self):
        """The dispatcher's screen: stops by zone, both ticks, what is missing."""
        self.ensure_one()
        me = self.env.user
        driver = first_name(self.driver_id) or "El repartidor"
        zones, index = [], {}
        for stop in self.stop_ids:
            key = stop.zone_id.id or 0
            if key not in index:
                index[key] = {"name": stop.zone_id.name or "Sin zona", "stops": []}
                zones.append(index[key])
            index[key]["stops"].append(stop._dispatch_row(me, driver))
        controlled = None
        if self.departure_controlled_at:
            controlled = {"by": self.departure_controlled_by_id.name or "",
                          "at": ar_time(self.departure_controlled_at)}
        return {
            "id": self.id,
            "name": self.name,
            "driver": self.driver_id.name or "",
            "driver_first": driver,
            "trip": self.trip,
            "day": day_label(self.date),
            "zones_text": ", ".join(self.zone_ids.mapped("name")),
            "state": self.state,
            "is_driver": self.driver_id == me,
            "editable": self.state == "draft" and not controlled
            and (self.driver_id != me or can_self_dispatch(me)),
            # 19.0.1.127.0: the driver controls his own load (autodespacho)
            "self_dispatch": self.driver_id == me and can_self_dispatch(me),
            "controlled": controlled,
            "orders_total": len(self.stop_ids),
            "orders_checked": self.checked_stop_count,
            "parcels_total": self.parcel_count,
            "parcels_checked": self.parcel_checked_count,
            "missing": self._dispatch_missing_lines(),
            "zones": zones,
            "left": self._left_payload(),
        }


class DistriRouteLeftOrder(models.Model):
    _name = "distri.route.left.order"
    _description = "Pedido que quedó en el depósito"
    _order = "route_id, id"
    _rec_name = "order_id"

    route_id = fields.Many2one("distri.route", string="Ruta", required=True, ondelete="cascade",
                               index=True, readonly=True)
    order_id = fields.Many2one("sale.order", string="Pedido", required=True,
                               ondelete="restrict", index=True, readonly=True)
    partner_id = fields.Many2one(related="order_id.partner_id", string="Cliente")
    parcels = fields.Integer(string="Bultos", readonly=True)
    zone_id = fields.Many2one("distri.zona", string="Zona", readonly=True)
    sequence = fields.Integer(string="Orden en la ruta", readonly=True)
    stop_ref = fields.Integer(string="Visita original", readonly=True,
                              help="Id de la visita que se sacó: el celular puede mandar "
                                   "todavía un tilde de carga de ella.")
    reason = fields.Char(string="Motivo", required=True, readonly=True)
    user_id = fields.Many2one("res.users", string="Lo marcó", readonly=True)
    left_at = fields.Datetime(string="Marcado el", readonly=True)

    def action_undo(self):
        """Back on the route, when it was marked by mistake. Only while the
        dispatch is open and the order is not on another route already."""
        self.route_id._check_dispatcher()
        for left in self:
            route, order = left.route_id, left.order_id
            if order.state != "sale" or not route._order_is_free(order):
                other = order.distri_stop_ids.filtered(
                    lambda s: not (s.state == "not_delivered" and s.unloaded))[:1]
                raise UserError("No se puede volver a subir %s: ya está en %s." % (
                    order.name, other.route_id.name or "otra ruta"))
            self.env["distri.route.stop"].create({
                "route_id": route.id,
                "order_id": order.id,
                "sequence": left.sequence,
                "zone_id": left.zone_id.id,
            })
            route.message_post(body="%s vuelve a la ruta (se deshizo «Queda en el depósito»). "
                                    "Lo marcó %s." % (order.name, self.env.user.name))
            order.message_post(body="Vuelve a la ruta %s. Lo marcó %s." % (
                route.name, self.env.user.name))
            left.unlink()
        return True
