"""«Planificación de rutas» (19.0.1.110.0, owner-approved mockup
«pc-reparto-planificacion»): the dispatcher's desktop board at /rutas
(controllers/rutas.py, views/rutas_app.xml, static/src/rutas/).

Administración or gerencia (Denis, Gonzalo) plan the day: on the left every
packed order that is on no route («Sin ruta · depósito»), on the right one
column per route of the day. Orders move one by one between the depot
column and the routes, and between routes, until the route leaves.

Every move goes through the route's own methods: `_add_orders` (the stop and
its amounts snapshot), `distri.route.stop.unlink` (only a pending stop) and
the C3 rule «one order on one active route» (_check_one_active_route), so
the backend form, «Asignar pedidos armados de las zonas» and /despacho keep
their rules.

When an order may move (decided with the driver's app in mind, see
controllers/reparto.py and static/src/reparto/app.js):

- Only on a route still at the depot («Asignada») whose load was not
  «Controlado» (models/despacho.py): after «Controlado» the load is what two
  people checked, and after «Salir a repartir» the stops belong to the
  driver's phone.
- Not an order already ticked on the truck by the driver or by the depot:
  it is physically loaded. Untick it first (Control de carga / Despacho).
- The driver's phone needs nothing else: on «Control de carga» it polls
  /reparto/api/load every 5 seconds and downloads the route again as soon as
  the set of stops changes, and «Salir a repartir» is checked by the server
  against the stops it has (every one ticked by both), so an order added at
  the last minute blocks the departure until it is loaded and checked, and
  a removed one simply disappears from the phone. The visit order is
  downloaded again right after the departure.
- The driver of a route can be picked while nobody ticked any of its orders
  (an empty route waits for its driver).
- 19.0.1.113.0: the same driver on two routes of the same date and trip is
  allowed (a second route of the day) but warned: in the toast of the change,
  in the column («Ojo: …») and in the backend form (onchange). Before, «Nueva
  ruta» refused it.
- 19.0.1.119.0: orders of the same customer travel together, each with its
  own invoice (orders are invoiced when packing ends, so they are never
  merged). The depot column shows them side by side; moving one moves the
  customer's other orders that may move on their own (_planning_move_group);
  a route whose customer has another open order that is not on it yet (not
  packed, held, or packed on no route) is warned, never blocked
  (_planning_sibling_notes), on the board and before «Controlado».
"""
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .despacho import ar_time, can_self_dispatch, first_name
from .reparto import TRIPS, _money
from .reparto_cliente import today_ar

PLANNING_GROUP = "distri_pedido.grupo_administracion"
WEEKDAYS = ["Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo"]

STATE_CHIPS = {
    "draft": ("Asignada", "warn"),
    "on_route": ("En reparto", "ok"),
    "closed": ("Para rendir", ""),
    "settled": ("Rendida", "neutral"),
}
STOP_CHIPS = {
    "pending": ("Pendiente", ""),
    "paid": ("Pagó", "ok"),
    "not_paid": ("No pagó", "err"),
    "already_paid": ("Ya estaba pagado", "ok"),
    "not_delivered": ("No se entregó", "warn"),
    "dispatched": ("Encomienda", "ok"),
}
LOAD_CHIPS = {
    "both": ("Doble control", "ok"),
    "driver": ("Lo tildó el repartidor", "warn"),
    "dispatch": ("Lo tildó el depósito", "warn"),
    "none": ("", ""),
}
LOADED_TEXT = ("%s ya está tildado en la carga de %s: está arriba de la camioneta. Para "
               "moverlo, que lo destilden primero en Control de carga y en Despacho.")


def day_text(day):
    """«Jueves 19/11/2026»."""
    return "%s %s" % (WEEKDAYS[day.weekday()], day.strftime("%d/%m/%Y")) if day else ""


def parcels_text(n):
    return "%s %s" % (n, "bulto" if n == 1 else "bultos")


def check_planner(env):
    if not (env.su or env.user.has_group(PLANNING_GROUP)):
        raise UserError("Solo administración y gerencia planifican las rutas.")


class DistriRoutePlanning(models.Model):
    _inherit = "distri.route"

    # 19.0.1.110.0: a route of the day can be created empty and wait for its
    # driver on the planning board («Nueva ruta»). Only the column changes
    # (NOT NULL is dropped on the update); nothing leaves without a driver:
    # the departure comes from the driver's phone.
    driver_id = fields.Many2one(required=False)

    def action_mark_controlled(self):
        """A route waiting for its driver is not «Controlado» (nobody would
        take it out)."""
        if any(r.kind == "route" and not r.driver_id for r in self):
            raise UserError("Elegí el repartidor de la ruta antes de dar la carga por controlada.")
        return super().action_mark_controlled()

    # ------------------------------------------------------------ helpers

    def _planning_number(self):
        """«Ruta N» of the day: the position among the routes of the same
        date and trip, oldest first (computed, never stored)."""
        self.ensure_one()
        if self.kind != "route" or not self.date:
            return 0
        same = self.sudo().search([("kind", "=", "route"), ("date", "=", self.date),
                                   ("trip", "=", self.trip),
                                   ("company_id", "=", self.company_id.id)], order="id")
        return same.ids.index(self.id) + 1 if self.id in same.ids else 0

    def _planning_title(self):
        self.ensure_one()
        return "Ruta %s · Viaje %s" % (self._planning_number(), self.trip)

    def _planning_editable(self):
        """Stops still move: at the depot and the load not «Controlado»."""
        self.ensure_one()
        return (self.kind == "route" and self.state == "draft"
                and not self.departure_controlled_at)

    def _planning_check_editable(self):
        for route in self:
            if route.kind != "route":
                raise UserError("Solo para las rutas de reparto.")
            if route.state != "draft":
                raise UserError("La %s ya salió: sus pedidos quedan fijos. Los cambios de "
                                "paradas los hace el repartidor." % route._planning_title())
            if route.departure_controlled_at:
                raise UserError("La carga de la %s ya se controló: sus pedidos quedan fijos."
                                % route._planning_title())

    def _planning_twins(self):
        """The driver's other routes of the same date and trip."""
        self.ensure_one()
        if self.kind != "route" or not self.driver_id or not self.date:
            return self.browse()
        return self.sudo().search([
            ("kind", "=", "route"), ("driver_id", "=", self.driver_id.id),
            ("date", "=", self.date), ("trip", "=", self.trip),
            ("id", "!=", self._origin.id or 0)], order="id")

    def _planning_twin_warning(self):
        """«Ojo: …» when the driver has another route of the same date and
        trip, else "". Never blocks (19.0.1.113.0)."""
        self.ensure_one()
        twins = self._planning_twins()
        if not twins:
            return ""
        names = ", ".join(t._planning_title() for t in twins)
        return ("Ojo: %s ya tiene %s del mismo día y viaje (%s). Revisá que no sea un error." % (
            first_name(self.driver_id) or self.driver_id.name,
            "otra ruta" if len(twins) == 1 else "otras %s rutas" % len(twins), names))

    @api.onchange("driver_id", "date", "trip")
    def _onchange_planning_twin(self):
        if self.kind == "route" and self.driver_id and self.date:
            text = self._planning_twin_warning()
            if text:
                return {"warning": {"title": "Repartidor con dos rutas", "message": text}}
        return None

    def _planning_pending_siblings(self):
        """[(order, kind)]: the other open orders of this route's customers
        that are not on it and not delivered (19.0.1.119.0). kind:
        «unpacked» (not packed yet), «held» (packed but held) or «free»
        (packed, on no active route). Never the cancelled, delivered or
        dispatched ones, pickups («retira» or a pickup zone), the ones
        waiting for the customer's WhatsApp, the generic web customer, an
        order packed and travelling on another route, or an order the
        dispatcher left at the depot from this route (a decision with its
        reason, models/despacho.py). Only while the route is at the depot."""
        self.ensure_one()
        if self.kind != "route" or self.state != "draft":
            return []
        route = self.sudo()
        stops = route.stop_ids
        partners = stops.partner_id.filtered(lambda p: not p.distri_generic_partner)
        if not partners:
            return []
        skip = stops.order_id | route.left_order_ids.order_id
        orders = self.env["sale.order"].sudo().search([
            ("partner_id", "in", partners.ids), ("state", "=", "sale"),
            ("id", "not in", skip.ids), ("company_id", "=", route.company_id.id),
            ("distri_entrega", "!=", "retira"), ("distri_wa_state", "!=", "waiting"),
        ], order="date_order, id")
        out = []
        for order in orders:
            if order.distri_delivery_state in ("delivered", "dispatched") \
                    or order._distri_pickup_zone():
                continue
            if order.distri_armado != "armado":
                out.append((order, "unpacked"))
                continue
            active = order.distri_stop_ids.filtered(
                lambda s: s.route_id.state != "settled"
                and not (s.state == "not_delivered" and s.unloaded))
            if not active:
                out.append((order, "held" if order.distri_retenido else "free"))
        return out

    def _planning_sibling_notes(self):
        """One «Ojo: …» sentence per customer and situation (see
        _planning_pending_siblings), else []. Never blocks."""
        self.ensure_one()
        groups, index = [], {}
        for order, kind in self._planning_pending_siblings():
            key = (order.partner_id.id, kind)
            if key not in index:
                index[key] = (order.partner_id, kind, [])
                groups.append(index[key])
            index[key][2].append(order.name)
        notes = []
        for partner, kind, names in groups:
            many = len(names) > 1
            what = "otros pedidos (%s)" % ", ".join(names) if many else \
                "otro pedido (%s)" % names[0]
            if kind == "unpacked":
                tail = ("sin armar. ¿Los terminás de armar para cargarlos?" if many
                        else "sin armar. ¿Lo terminás de armar para cargarlo?")
            elif kind == "held":
                tail = ("armados pero retenidos. ¿Los liberás para cargarlos?" if many
                        else "armado pero retenido. ¿Lo liberás para cargarlo?")
            else:
                tail = ("armados que no están en ninguna ruta. ¿Los sumás a esta ruta?" if many
                        else "armado que no está en ninguna ruta. ¿Lo sumás a esta ruta?")
            notes.append("Ojo: %s tiene %s %s" % (partner._distri_app_name(), what, tail))
        return notes

    def _planning_sibling_warning(self):
        return " ".join(self._planning_sibling_notes())

    def _planning_zones(self):
        """The zones of its stops, else the zones chosen for the route."""
        self.ensure_one()
        zones = self.stop_ids.zone_id
        return zones.sorted(lambda z: (z.viaje, z.sequence, z.name)) or self.zone_ids

    def _planning_dispatcher(self):
        """Who controlled the departure, else who ticked every order at the
        depot when it was one person: the «Despachó» of the route sheet."""
        self.ensure_one()
        if self.departure_controlled_by_id:
            return self.departure_controlled_by_id
        checkers = self.stop_ids.dispatch_checked_by_id
        return checkers if len(checkers) == 1 and all(self.stop_ids.mapped("dispatch_checked")) \
            else self.env["res.users"]

    @api.model
    def _planning_free_domain(self, company=None):
        """The orders a route may take (distri.route._free_orders_domain, so
        the same rules as «Asignar pedidos armados de las zonas»), searched
        only among those with no active stop."""
        company = company or self.env.company
        domain = self.new({"company_id": company.id})._free_orders_domain()
        return domain + [("distri_stop_ids", "not any", [
            "|", ("state", "!=", "not_delivered"), ("unloaded", "=", False)])]

    @api.model
    def _planning_drivers(self):
        """Who can drive: the reparto group (administración and gerencia
        imply it, so Denis and Gonzalo appear after the drivers)."""
        group = self.env.ref("distri_pedido.grupo_reparto", raise_if_not_found=False)
        admin = self.env.ref(PLANNING_GROUP, raise_if_not_found=False)
        if not group:
            return self.env["res.users"]
        users = self.env["res.users"].sudo().search([
            ("all_group_ids", "in", group.ids), ("share", "=", False),
            ("company_ids", "in", self.env.company.ids)], order="name")
        return users.sorted(lambda u: (bool(admin) and admin in u.all_group_ids, u.name or ""))

    # ------------------------------------------------------------ actions

    @api.model
    def planning_create(self, day=None, trip="1", driver=None):
        """«Nueva ruta»: an empty route of the day (today or later), with or
        without its driver."""
        check_planner(self.env)
        day = fields.Date.to_date(day) if day else today_ar()
        if day < today_ar():
            raise UserError("Las rutas nuevas son de hoy en adelante.")
        if trip not in dict(TRIPS):
            raise UserError("Elegí el viaje.")
        driver = driver or self.env["res.users"]
        if driver:
            self._planning_check_driver(driver)
        # 19.0.1.113.0: a second route of the same day and trip is allowed,
        # with a warning (_planning_twin_warning) instead of an error
        route = self.create({"kind": "route", "date": day, "trip": trip,
                             "driver_id": driver.id or False})
        route.message_post(body="Ruta creada en Planificación por %s." % self.env.user.name)
        return route

    @api.model
    def _planning_check_driver(self, driver):
        if driver not in self._planning_drivers():
            raise UserError("%s no tiene permiso de reparto." % (driver.name or "Ese usuario"))

    def planning_set_driver(self, driver):
        """The driver of the column header. Only while nobody ticked any of
        its orders: an old driver's phone may hold the route until then."""
        check_planner(self.env)
        self.ensure_one()
        self._planning_check_editable()
        driver = driver or self.env["res.users"]
        if driver == self.driver_id:
            return True
        if driver:
            self._planning_check_driver(driver)
        if self.driver_id and self.stop_ids.filtered("loaded"):
            raise UserError("%s ya empezó a cargar la %s: el repartidor no se cambia. Pasá los "
                            "pedidos a otra ruta." % (first_name(self.driver_id),
                                                      self._planning_title()))
        if driver and not can_self_dispatch(driver) \
                and self.stop_ids.filtered(lambda s: s.dispatch_checked_by_id == driver):
            raise UserError("%s controló pedidos de esta ruta en el depósito: la carga la "
                            "controla otra persona, no el repartidor." % driver.name)
        old = self.driver_id
        self.driver_id = driver.id or False
        self.message_post(body="Repartidor: %s → %s (lo cambió %s en Planificación)." % (
            old.name or "sin repartidor", driver.name or "sin repartidor", self.env.user.name))
        return True

    def planning_set_zones(self, zones):
        check_planner(self.env)
        self.ensure_one()
        self._planning_check_editable()
        self.zone_ids = [(6, 0, zones.ids)]
        return True

    def planning_assign_zones(self):
        """«Asignar pedidos armados de las zonas» (kept for the sub-zones of
        Neuquén): the route's own method."""
        check_planner(self.env)
        self._planning_check_editable()
        return self.action_assign_orders()

    def planning_delete(self):
        """An empty route created by mistake."""
        check_planner(self.env)
        for route in self:
            route._planning_check_editable()
            if route.stop_ids or route.left_order_ids:
                raise UserError("La %s tiene pedidos: pasalos a otra ruta o al depósito antes "
                                "de borrarla." % route._planning_title())
            if self.env["distri.sync.event"].sudo().search_count([("route_id", "=", route.id)]):
                raise UserError("El celular del repartidor ya usó esta ruta: no se borra.")
        self.sudo().unlink()
        return True

    def planning_reorder(self, stop_ids):
        """The visit order of the column (drag inside a route)."""
        check_planner(self.env)
        self.ensure_one()
        if self.kind != "route" or self.state != "draft":
            raise UserError("La %s ya salió: el orden de las visitas lo maneja el repartidor."
                            % self._planning_title())
        current = self.env["distri.route.stop"].search([("route_id", "=", self.id)])
        wanted = [int(x) for x in stop_ids if int(x) in current.ids]
        rest = [s.id for s in current if s.id not in wanted]
        stops = self.env["distri.route.stop"].browse(wanted + rest)
        for index, stop in enumerate(stops, start=1):
            if stop.sequence != index * 10:
                stop.sequence = index * 10
        # the one2many keeps its fetch order in the cache: the board drawn
        # right after (same request) must see the new one
        self.invalidate_recordset(["stop_ids"])
        return True

    @api.model
    def planning_move(self, order, target=None, before=None):
        """One order to a route (`target`), or back to the depot column
        (`target` empty). `before`: the stop of the target it goes in front
        of (else at the end). Returns the new stop (or an empty recordset).
        19.0.1.119.0: the customer's other orders go with it
        (_planning_move_group)."""
        return self._planning_move_group(order, target, before)[0]

    @api.model
    def _planning_active_stops(self, order):
        """The stops that hold the order (the C3 rule of
        _check_one_active_route)."""
        return order.sudo().distri_stop_ids.filtered(
            lambda s: s.route_id.state != "settled"
            and not (s.state == "not_delivered" and s.unloaded))

    @api.model
    def _planning_move_group(self, order, target=None, before=None):
        """planning_move, and then the same customer's other orders follow
        it, each through the same checks as a move of its own: into a
        route, the customer's free orders (the depot column's rules, not
        held, not left at the depot from that route) and the ones still
        movable on the route it came from, right after it; back to the
        depot, the ones still movable on that route. An order that does not
        pass stays where it is. Returns (new stop, the other orders moved)."""
        check_planner(self.env)
        order.ensure_one()
        target = target or self.browse()
        source = self._planning_active_stops(order)[:1].route_id
        new = self._planning_move_one(order, target, before)
        Order = self.env["sale.order"]
        if source == target or not (source or target):
            return new, Order
        moved = Order
        for other in self._planning_group_candidates(order, source, target):
            try:
                with self.env.cr.savepoint():
                    self._planning_move_one(other, target)
            except (UserError, ValidationError):
                continue
            moved |= other
        if target and new and moved:
            target.invalidate_recordset(["stop_ids"])
            followers = target.stop_ids.filtered(lambda s: s.order_id in moved)
            ids = [s.id for s in target.stop_ids if s not in followers]
            at = ids.index(new.id) + 1 if new.id in ids else len(ids)
            ids[at:at] = followers.ids
            target.planning_reorder(ids)
        return new, moved

    @api.model
    def _planning_group_candidates(self, order, source, target):
        """The customer's other orders that may follow `order` (each one is
        still checked by _planning_move_one)."""
        partner = order.sudo().partner_id
        Order = self.env["sale.order"]
        if not partner or partner.distri_generic_partner:
            return Order
        found = Order.sudo()
        if source:
            found |= source.sudo().stop_ids.filtered(
                lambda s: s.partner_id == partner and s.order_id != order
                and s.state == "pending" and not (s.loaded or s.dispatch_checked)).order_id
        if target:
            left = target.sudo().left_order_ids.order_id
            free = Order.sudo().search(
                self._planning_free_domain(target.company_id)
                + [("partner_id", "=", partner.id), ("id", "!=", order.id)],
                order="distri_armado_fin, id")
            found |= free.filtered(lambda o: o not in left and target._order_is_free(o))
        return found.with_env(self.env)

    @api.model
    def _planning_move_one(self, order, target=None, before=None):
        """The move of one order (planning_move before 19.0.1.119.0)."""
        check_planner(self.env)
        order.ensure_one()
        target = target or self.browse()
        Stop = self.env["distri.route.stop"]
        active = self._planning_active_stops(order)
        current = active[:1]
        if len(active) > 1:
            raise UserError("El pedido %s figura en más de una ruta: revisalo en el backend."
                            % order.name)
        if current:
            route = current.route_id
            if route.kind != "route":
                raise UserError("El pedido %s ya se entregó en el depósito." % order.name)
            route._planning_check_editable()
            if current.state != "pending":
                raise UserError("El pedido %s ya tiene la visita." % order.name)
        if target:
            target._planning_check_editable()
        if current and target and current.route_id == target:
            if before:
                ids = [s.id for s in target.stop_ids if s != current]
                ids.insert(ids.index(before.id) if before.id in ids else len(ids), current.id)
                target.planning_reorder(ids)
            return current
        if not current and not target:
            return Stop
        if current and (current.loaded or current.dispatch_checked):
            raise UserError(LOADED_TEXT % (order.name, current.route_id._planning_title()))
        user = self.env.user.name
        new = Stop
        with self.env.cr.savepoint():
            if current:
                source = current.route_id
                current.unlink()
                source.message_post(body="%s sale de la %s%s (lo movió %s en Planificación)." % (
                    order.name, source._planning_title(),
                    " y pasa a la %s" % target._planning_title() if target
                    else " y vuelve al depósito, sin ruta", user))
            if target:
                if not target._planning_can_take(order):
                    if order.sudo().partner_id.distri_generic_partner:
                        # 19.0.1.116.0: the usual cause, said plainly
                        raise UserError(
                            "El pedido %s no tiene cliente: sigue en el contacto genérico de "
                            "los pedidos web. Asignale el cliente en Pedidos para revisar "
                            "antes de subirlo a una ruta." % order.name)
                    raise UserError("El pedido %s no puede subir a una ruta: tiene que estar "
                                    "confirmado y armado, sin retener, y no ser para retirar en "
                                    "el depósito." % order.name)
                new = target._add_orders(order)
                zone = new.zone_id
                if zone and zone not in target.zone_ids:
                    target.zone_ids = [(4, zone.id)]
                # it was left at the depot by the dispatcher of this route
                # (models/despacho.py): it is back on it
                target.left_order_ids.filtered(lambda l: l.order_id == order).unlink()
                target.invalidate_recordset(["stop_ids"])
                if before and before in target.stop_ids:
                    ids = [s.id for s in target.stop_ids if s != new]
                    ids.insert(ids.index(before.id), new.id)
                    target.planning_reorder(ids)
                target.message_post(body="%s sube a la %s (lo movió %s en Planificación)." % (
                    order.name, target._planning_title(), user))
            order.message_post(body="Planificación: %s (lo movió %s)." % (
                "va en la %s · %s" % (target._planning_title(), target.driver_id.name
                                       or "sin repartidor") if target
                else "vuelve al depósito, sin ruta", user))
        return new

    def _planning_can_take(self, order):
        """The route's own rules for a free order (same as «Asignar»)."""
        self.ensure_one()
        found = self.env["sale.order"].sudo().search_count(
            self._free_orders_domain() + [("id", "=", order.id)])
        return bool(found) and self._order_is_free(order)

    # -------------------------------------------------------------- board

    @api.model
    def _planning_routes(self, day):
        """The routes of the day; today also the ones of the days before
        that never left (they are still at the depot)."""
        base = [("kind", "=", "route"), ("company_id", "in", self.env.companies.ids)]
        routes = self.sudo().search(base + [("date", "=", day)], order="trip, id")
        if day == today_ar():
            routes = self.sudo().search(base + [("date", "<", day), ("state", "=", "draft")],
                                        order="date, trip, id") | routes
        return routes

    @api.model
    def _planning_board(self, day=None):
        """Everything the board shows (views/rutas_app.xml)."""
        check_planner(self.env)
        day = day or today_ar()
        routes = self._planning_routes(day)
        drivers = self._planning_drivers()
        zones_all = self.env["distri.zona"].sudo().search([])
        columns = [route.with_context(planning_drivers=drivers)._planning_column(zones_all)
                   for route in routes]
        targets = [{"id": c["id"], "label": c["title"] + (" · " + c["driver_first"]
                                                          if c["driver_first"] else "")}
                   for c in columns if c["editable"]]
        free, held = self._planning_depot_orders()
        groups, index = [], {}
        for order in free | held:
            zone = order._distri_route_zone()
            key = zone.id or 0
            if key not in index:
                index[key] = {"id": key, "name": zone.name or "Sin zona", "orders": [],
                              "parcels": 0, "sort": (zone.viaje or "9", zone.sequence,
                                                     zone.name or "~")}
                groups.append(index[key])
            index[key]["orders"].append(self._planning_card(order, held=order in held))
            index[key]["parcels"] += order.distri_bultos
        groups.sort(key=lambda g: g["sort"])
        for g in groups:
            g["orders"] = self._planning_group_customers(g["orders"])
        # «Nuevo viaje con …» only when that driver has no route for it yet
        taken = {(c["driver_id"], c["trip"]) for c in columns if c["driver_id"]}
        for c in columns:
            if c["next_trip"] and (c["driver_id"], c["next_trip"]) in taken:
                c["next_trip"] = None
        pickup = [d for d in self._planning_free_domain() if d != ("distri_entrega", "!=", "retira")]
        return {
            "day": day,
            "day_text": day_text(day),
            "is_today": day == today_ar(),
            "can_create": day >= today_ar(),
            "columns": columns,
            "targets": targets,
            "groups": groups,
            "free_count": len(free),
            "free_parcels": sum(free.mapped("distri_bultos")),
            "held_count": len(held),
            # «Lo voy a retirar»: they never go on a route (/deposito)
            "pickup_count": self.env["sale.order"].sudo().search_count(
                pickup + [("distri_entrega", "=", "retira")]),
            "drivers": [{"id": u.id, "name": u.name} for u in drivers],
            "trips": TRIPS,
        }

    @api.model
    def _planning_depot_orders(self):
        """(free, held): packed orders on no route; the held ones are shown
        locked (models/revisar.py keeps them off every route)."""
        Order = self.env["sale.order"].sudo()
        domain = self._planning_free_domain()
        free = Order.search(domain, order="distri_armado_fin, id")
        probe = self.new({"company_id": self.env.company.id})
        free = free.filtered(lambda o: probe._order_is_free(o))
        held_domain = [d for d in domain if d != ("distri_retenido", "=", False)]
        held = Order.search(held_domain + [("distri_retenido", "=", True)],
                            order="distri_armado_fin, id")
        return free, held

    @api.model
    def _planning_group_customers(self, cards):
        """The cards of a zone, oldest packed first, with the orders of the
        same customer side by side (where the customer's first one is) and
        the chip «N pedidos de este cliente» (19.0.1.119.0)."""
        def own(c):
            return (c["held"], c["packed_at"] or "", c["id"])
        first, count = {}, {}
        for c in cards:
            key = c["partner_id"]
            count[key] = count.get(key, 0) + 1
            first[key] = min(first.get(key, own(c)), own(c))
        for c in cards:
            n = count[c["partner_id"]]
            c["siblings"] = n - 1
            if n > 1:
                c["chips"] = [("%s pedidos de este cliente" % n, "neutral")] + c["chips"]
        return sorted(cards, key=lambda c: (first[c["partner_id"]], c["partner_id"], own(c)))

    @api.model
    def _planning_address(self, partner):
        """«Calle 123, Ciudad»: where the order goes, short."""
        street = " ".join((partner.street or "").split())
        city = " ".join((partner.city or "").split())
        return ", ".join(p for p in (street, city) if p)

    @api.model
    def _planning_card(self, order, held=False):
        o = order.sudo()
        bits = [o.name, parcels_text(o.distri_bultos)]
        when = ""
        if o.distri_armado_fin:
            day = fields.Datetime.context_timestamp(self.with_context(
                tz="America/Argentina/Buenos_Aires"), o.distri_armado_fin).date()
            when = ("armado %s" % ar_time(o.distri_armado_fin)) if day == today_ar() else \
                ("armado ayer" if (today_ar() - day).days == 1 else
                 "armado el %s" % day.strftime("%d/%m"))
            bits.append(when)
        chips = []
        if o.distri_encomienda:
            chips.append(("Encomienda", ""))
        if o.distri_stop_ids:
            chips.append(("Volvió de una ruta", "warn"))
        notice = o.distri_customer_notice_text or ""
        return {
            "id": o.id,
            "name": o.name,
            "client": o.partner_id._distri_app_name(),
            "partner_id": o.partner_id.id,
            "address": self._planning_address(o.partner_shipping_id or o.partner_id),
            "siblings": 0,
            "meta": " · ".join(b for b in bits if b),
            "amount": _money(o._distri_amount_to_collect()),
            "parcels": o.distri_bultos,
            "notice": notice,
            "chips": chips,
            "held": held,
            "held_reason": (o.distri_retenido_motivo or "") if held else "",
            "packed_at": fields.Datetime.to_string(o.distri_armado_fin) if o.distri_armado_fin
            else "",
            "search": ("%s %s" % (o.name, o.partner_id.name or "")).lower(),
        }

    def _planning_column(self, zones_all):
        self.ensure_one()
        drivers = self.env.context.get("planning_drivers") or self._planning_drivers()
        label, chip = STATE_CHIPS.get(self.state, ("", ""))
        editable = self._planning_editable()
        stops = []
        for index, stop in enumerate(self.stop_ids, start=1):
            if self.state == "draft":
                text, kind = LOAD_CHIPS.get(stop.load_state or "none", ("", ""))
            else:
                text, kind = STOP_CHIPS.get(stop.state, ("", ""))
            stops.append({
                "id": stop.id,
                "n": index,
                "order_id": stop.order_id.id,
                "order": stop.order_id.name,
                "client": stop.partner_id._distri_app_name(),
                "meta": " · ".join(b for b in (
                    stop.order_id.name, parcels_text(stop.parcels),
                    stop.customer_notice or "") if b),
                "amount": _money(stop.order_amount),
                "chip": (text, kind),
                "movable": editable and not (stop.loaded or stop.dispatch_checked),
                "search": ("%s %s" % (stop.order_id.name, stop.partner_id.name or "")).lower(),
            })
        locked = ""
        if self.state != "draft":
            locked = "Ya salió: los cambios de paradas los hace el repartidor."
        elif self.departure_controlled_at:
            locked = "La carga ya se controló (%s, %s): los pedidos quedan fijos." % (
                first_name(self.departure_controlled_by_id), ar_time(self.departure_controlled_at))
        driver_locked = (not editable) or bool(self.driver_id and self.stop_ids.filtered("loaded"))
        loaded = self.stop_ids.filtered("loaded")
        return {
            "id": self.id,
            "title": self._planning_title(),
            "trip": self.trip,
            "day": self.date,
            "old": self.date < today_ar() if self.date else False,
            "day_short": self.date.strftime("%d/%m") if self.date else "",
            "driver_id": self.driver_id.id,
            "driver": self.driver_id.name or "",
            "driver_first": first_name(self.driver_id),
            "drivers": [{"id": u.id, "name": u.name} for u in drivers],
            "driver_locked": driver_locked,
            "state": self.state,
            "state_label": label,
            "state_chip": chip,
            "editable": editable,
            "draft": self.state == "draft",
            "locked": locked,
            "zones": ", ".join(self._planning_zones().mapped("name")),
            "zone_ids": self.zone_ids.ids,
            "zones_all": [{"id": z.id, "name": z.name} for z in zones_all],
            "stop_count": len(self.stop_ids),
            "parcel_count": self.parcel_count,
            "loaded_count": len(loaded),
            "checked_count": self.checked_stop_count,
            "departed": ar_time(self.departed_at) if self.departed_at else "",
            "stops": stops,
            "left": [{"order": l.order_id.name, "client": l.partner_id._distri_app_name(),
                      "reason": l.reason} for l in self.left_order_ids],
            "can_delete": editable and not self.stop_ids and not self.left_order_ids,
            # 19.0.1.113.0: the same driver, date and trip on another route
            "twin": self._planning_twin_warning(),
            # 19.0.1.119.0: a customer of the route with another open order
            "siblings": self._planning_sibling_notes(),
            "next_trip": next((t for t, _ in TRIPS if t > (self.trip or "1")), None)
            if self.state != "draft" and self.driver_id and self.date == today_ar() else None,
            "sheet_url": "/report/pdf/distri_pedido.hoja_ruta_documento/%s" % self.id,
            "remitos_url": "/report/pdf/distri_pedido.remitos_ruta_documento/%s" % self.id,
        }
