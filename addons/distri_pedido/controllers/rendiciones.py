"""«Rendiciones y casos» on the phone (19.0.1.127.1).

The home tile «Rendiciones y casos» used to open the backend menu «Rendición
de caja», which reads badly on a phone. It now opens this small kit page:
big rows to the screens of settling routes and following cases, each with a
count when it is cheap. Levels (back goes UP one level, dn.js):

    /tablero                    staff home
      /rendiciones              Rendiciones y casos
        /rendiciones/rutas      closed routes waiting to be settled
          (each one opens the backend route form)
        /caja/rendicion         the cashier's mobile «Recibir rendiciones»

Same access as the tile: whoever sees the menu «Reparto › Rendición de caja»
(administración, gerencia, system administrators); anybody else goes to
/tablero, which sends him to his own screen. Read only: the counts and the
list use sudo with the same domains as the backend actions and /caja.
The look is /deposito's (static/src/deposito/deposito.css, dp-* classes).

19.0.1.129.25 (#105): each route of «Rutas para rendir» has «Reabrir para
el repartidor» (POST /rendiciones/rutas/<id>/reabrir, models/reparto.py
`_distri_reopen`, which checks the role and the cash-count lock).
"""
from urllib.parse import urlencode

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..models.comprobante_pendiente import OPEN_STATES
from ..models.reparto import _money
from .caja import _dia
from .kit import kit_context
from .tablero import _menu_url, wants_tablero

TEMPLATE = "distri_pedido.rendiciones_page"
VISIBLE_MENU = "distri_pedido.menu_reparto_settle"
# the record URL of a route keeps the «Rendición de caja» breadcrumb
ROUTE_URL = "/odoo/action-distri_pedido.action_route_settle/%s"
# the kinds of «Cargos al repartidor» (views/reparto.xml action_driver_charges)
CHARGE_KINDS = ("receipt", "short", "unpaid")


def installed(env):
    """The page's templates of this version are loaded (module updated)."""
    return bool(env.ref(TEMPLATE, raise_if_not_found=False))


def can_open(env):
    """The tile's rule: the user sees «Reparto › Rendición de caja»."""
    menu = env.ref(VISIBLE_MENU, raise_if_not_found=False)
    return bool(menu) and not env.user.share and menu.id in env["ir.ui.menu"]._visible_menu_ids()


def routes_to_settle(env):
    """Driver routes closed on the phone and not settled yet: the
    «Para rendir» filter of the backend, the same search /caja uses."""
    routes, _boxes = env["distri.caja"].sudo().pending_receptions()
    return routes


def counts(env):
    """The badges of the page: {key: n}."""
    routes, boxes = env["distri.caja"].sudo().pending_receptions()
    Case = env["distri.route.case"].sudo()
    return {
        "recibir": len(routes) + len(boxes),
        "rutas": len(routes),
        "casos": Case.search_count([("state", "=", "open")]),
        "cargos": Case.search_count([("state", "=", "open"), ("kind", "in", CHARGE_KINDS)]),
        "comprobantes": env["distri.promised.transfer"].sudo().search_count(
            [("state", "in", OPEN_STATES)]),
    }


def _backend(env, xmlid):
    """URL of a backend menu the user can see (tablero's _menu_url), or ''."""
    menu = env.ref(xmlid, raise_if_not_found=False)
    if not menu or menu.id not in env["ir.ui.menu"]._visible_menu_ids():
        return ""
    return _menu_url(menu)


def rows(env):
    """The rows of the page, in order: dicts with key, title, subtitle,
    icon, url, how (kit «down» or a plain backend link) and badge."""
    n = counts(env)
    items = [
        ("recibir", "Recibir rendiciones", "Rutas y cajas para pasar a tu caja", "register",
         "/caja/rendicion", "down"),
        ("rutas", "Rutas para rendir", "Cerradas por el repartidor, sin rendir", "truck",
         "/rendiciones/rutas", "down"),
        ("casos", "Casos", "Diferencias, devoluciones y gastos abiertos", "alert",
         _backend(env, "distri_pedido.menu_reparto_cases"), ""),
        ("cargos", "Cargos al repartidor", "Comprobantes no rendidos y pedidos no cobrados",
         "user", _backend(env, "distri_pedido.menu_reparto_charges"), ""),
        ("comprobantes", "Comprobantes pendientes", "Transferencias sin comprobante",
         "receipt", _backend(env, "distri_pedido.menu_reparto_promises"), ""),
    ]
    return [{"key": key, "title": title, "subtitle": subtitle, "icon": icon, "url": url,
             "how": how, "badge": n.get(key) or 0}
            for key, title, subtitle, icon, url, how in items if url]


class Rendiciones(http.Controller):

    def _render(self, template, values):
        user = request.env.user
        base = {
            "dn": kit_context("deposito.css"),
            "user": user,
            "money": _money,
            "dia": _dia,
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            "ok": "",
            "error": "",
            "csrf_token": request.csrf_token(),
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _refused(self):
        """None when the user may open the page, else where he goes."""
        env = request.env
        if not can_open(env):
            return request.redirect("/tablero", 303)
        if not installed(env):
            # code newer than the database: the backend menu, as before
            return request.redirect(_backend(env, VISIBLE_MENU) or "/tablero", 303)
        return None

    @http.route("/rendiciones", type="http", auth="user", methods=["GET"], sitemap=False)
    def home(self, **kw):
        refused = self._refused()
        if refused:
            return refused
        env = request.env
        return self._render(TEMPLATE, {
            "rows": rows(env),
            # 19.0.1.127.1: zonas, gastos, transportistas, tiempos: the whole
            # «Reparto» menu bar of the backend, opened on «Zonas»
            "ajustes": _backend(env, "distri_pedido.menu_reparto_zones"),
        })

    @http.route("/rendiciones/rutas", type="http", auth="user", methods=["GET"], sitemap=False)
    def routes(self, ok="", **kw):
        refused = self._refused()
        if refused:
            return refused
        return self._routes_page(ok=ok)

    def _routes_page(self, ok="", error="", status=200):
        response = self._render("distri_pedido.rendiciones_rutas", {
            "routes": routes_to_settle(request.env),
            "route_url": ROUTE_URL,
            "ok": ok,
            "error": error,
        })
        response.status_code = status
        return response

    @http.route("/rendiciones/rutas/<int:route_id>/reabrir", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def reopen(self, route_id, **kw):
        """19.0.1.129.25 (#105): «Reabrir para el repartidor»."""
        refused = self._refused()
        if refused:
            return refused
        route = request.env["distri.route"].sudo().browse(route_id).exists()
        if not route:
            return self._routes_page(error="Esa ruta ya no existe.", status=404)
        try:
            route._distri_reopen(request.env.user, via="rendiciones")
        except UserError as exc:
            return self._routes_page(error=exc.args[0] if exc.args else str(exc), status=400)
        return request.redirect("/rendiciones/rutas?" + urlencode(
            {"ok": "%s: reabierta para el repartidor." % route.name}), 303)
