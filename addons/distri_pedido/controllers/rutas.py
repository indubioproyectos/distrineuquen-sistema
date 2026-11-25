"""«Planificación de rutas» on the PC (19.0.1.110.0, owner-approved mockup
«pc-reparto-planificacion»): /rutas, on the kit (views/rutas_app.xml,
static/src/rutas/).

    /rutas                     GET  the board of a day (?fecha=AAAA-MM-DD,
                                    today by default)
    /rutas/tablero             GET  the board alone (JSON with its HTML):
                                    the page refreshes it every few seconds
    /rutas/accion/<action>     POST one change, answered with the board
                                    again: mover (an order to a route or
                                    back to the depot), orden (the visit
                                    order of a route), nueva (an empty route
                                    of the day), repartidor, zonas, asignar
                                    («Asignar pedidos armados de las
                                    zonas»), borrar (an empty route)

Administración and gerencia only. Every rule is the model's
(models/planificacion.py, on top of distri.route): this file only
translates HTTP. The forms post through DN.api (FormData with the CSRF
token, X-Distri-Fetch: 1).

Until the module is updated the templates do not exist yet (the Python
code is loaded first): /rutas then sends to the backend list of routes.
"""
from datetime import timedelta

from werkzeug.exceptions import Forbidden

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models.planificacion import PLANNING_GROUP
from ..models.reparto_cliente import today_ar
from .kit import kit_context, page_nav
from .tablero import wants_tablero

PAGE = "distri_pedido.rutas_app"
BOARD = "distri_pedido.rutas_tablero"
ACTIONS = ("mover", "orden", "nueva", "repartidor", "zonas", "asignar", "borrar")


def is_planner(user):
    return bool(user) and not user.share and user.has_group(PLANNING_GROUP)


def installed(env):
    return bool(env.ref(PAGE, raise_if_not_found=False))


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _day(text):
    try:
        return fields.Date.to_date(text) if text else today_ar()
    except ValueError:
        return today_ar()


class Rutas(http.Controller):

    def _check(self):
        if not is_planner(request.env.user):
            raise Forbidden()

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _values(self, day):
        board = request.env["distri.route"]._planning_board(day)
        return {
            "b": board,
            "prev_day": fields.Date.to_string(day - timedelta(days=1)),
            "next_day": fields.Date.to_string(day + timedelta(days=1)),
            "day_iso": fields.Date.to_string(day),
            "today_iso": fields.Date.to_string(today_ar()),
        }

    def _board_html(self, day):
        return str(request.env["ir.qweb"]._render(BOARD, self._values(day)))

    @http.route("/rutas", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, fecha="", **kw):
        self._check()
        if not installed(request.env):
            return request.redirect("/odoo/action-distri_pedido.action_route", 303)
        user = request.env.user
        day = _day(fecha)
        values = self._values(day)
        values.update({
            "dn": kit_context("rutas.css", "rutas.js"),
            "user": user,
            "inicio": "/tablero" if wants_tablero(user) else "/odoo",
            # 19.0.1.129.29: the role's bottom bar (ui_v2 only)
            "nav": page_nav(request.env, "rutas"),
            "backend_url": "/odoo/action-distri_pedido.action_route",
        })
        html = request.env["ir.qweb"]._render(PAGE, values)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    @http.route("/rutas/tablero", type="http", auth="user", methods=["GET"], sitemap=False)
    def board(self, fecha="", **kw):
        if not is_planner(request.env.user):
            return self._json({"ok": False, "error": "Solo administración y gerencia."}, 403)
        if not installed(request.env):
            return self._json({"ok": False, "error": "Falta actualizar el módulo."}, 409)
        return self._json({"ok": True, "html": self._board_html(_day(fecha))})

    @http.route("/rutas/accion/<string:action>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def action(self, action, **form):
        if not is_planner(request.env.user):
            return self._json({"ok": False, "error": "Solo administración y gerencia."}, 403)
        if action not in ACTIONS or not installed(request.env):
            return self._json({"ok": False, "error": "Acción desconocida."}, 404)
        day = _day(form.get("fecha"))
        try:
            with request.env.cr.savepoint():
                message = self._do(action, form, day)
        except (UserError, ValidationError, AccessError) as exc:
            request.env.invalidate_all()
            return self._json({"ok": False, "error": str(exc.args[0] if exc.args else exc),
                               "html": self._board_html(day)}, 400)
        return self._json({"ok": True, "message": message or "", "html": self._board_html(day)})

    def _route(self, value, required=True):
        Route = request.env["distri.route"]
        route = Route.browse(_int(value)).exists()
        if route and route.kind == "route" and route.company_id in request.env.companies:
            return route
        if required:
            raise UserError("No se encontró la ruta: actualizá la pantalla.")
        return Route

    def _do(self, action, form, day):
        env = request.env
        Route = env["distri.route"]
        if action == "nueva":
            driver = env["res.users"].browse(_int(form.get("driver_id"))).exists()
            route = Route.planning_create(day, form.get("trip") or "1", driver)
            # 19.0.1.113.0: a second route of the same day and trip is warned
            return " ".join(filter(None, ["Listo: %s." % route._planning_title(),
                                          route._planning_twin_warning()]))
        if action == "mover":
            order = env["sale.order"].browse(_int(form.get("order_id"))).exists()
            if not order:
                raise UserError("No se encontró el pedido: actualizá la pantalla.")
            target = self._route(form.get("route_id"), required=False) \
                if _int(form.get("route_id")) else Route
            before = env["distri.route.stop"].browse(_int(form.get("before_id"))).exists()
            source = Route._planning_active_stops(order)[:1].route_id
            _new, moved = Route._planning_move_group(order, target, before or None)
            text = ("%s → %s." % (order.name, target._planning_title())) if target else \
                "%s vuelve al depósito." % order.name
            # 19.0.1.119.0: the customer's other orders travel with it, and a
            # customer with another open order off the route is warned
            if moved:
                names = ", ".join(moved.mapped("name"))
                text += (" También se movieron %s." if len(moved) > 1
                         else " También se movió %s.") % names
            route = target or (source if source.kind == "route" else Route)
            warning = route._planning_sibling_warning() if route else ""
            return " ".join(filter(None, [text, warning]))
        route = self._route(form.get("route_id"))
        if action == "orden":
            ids = [_int(x) for x in (form.get("stop_ids") or "").split(",") if _int(x)]
            route.planning_reorder(ids)
            return ""
        if action == "repartidor":
            driver = env["res.users"].browse(_int(form.get("driver_id"))).exists()
            route.planning_set_driver(driver)
            return " ".join(filter(None, [
                "Repartidor de la %s: %s." % (route._planning_title(),
                                              driver.name or "sin repartidor"),
                route._planning_twin_warning()]))
        if action == "zonas":
            ids = [_int(x) for x in (form.get("zone_ids") or "").split(",") if _int(x)]
            route.planning_set_zones(env["distri.zona"].sudo().browse(ids).exists())
            return "Zonas guardadas."
        if action == "asignar":
            before = len(route.stop_ids)
            route.planning_assign_zones()
            return "Se sumaron %s pedidos a la %s." % (len(route.stop_ids) - before,
                                                       route._planning_title())
        if action == "borrar":
            title = route._planning_title()
            route.planning_delete()
            return "Se borró la %s." % title
        raise UserError("Acción desconocida.")
