"""«Despacho»: the dispatcher's phone screen to check the truck's load.

Administración and gerencia only. Unlike the driver's app it works online
only (there is a connection at the depot): every tick goes straight to the server and the
screen polls the route every few seconds to show the driver's ticks. The
rules live in models/despacho.py; everything runs as the logged-in user, so
the model checks the group and the two-person rule.

19.0.1.119.0: «Controlado» on a route whose customer has another open order
that is not on it (models/planificacion.py _planning_sibling_notes) answers
{"ok": false, "confirm": [...]} without closing the dispatch; the screen
lists them and sends the action again with "confirmed": true. Every existing
refusal comes first and stays as it was.

19.0.1.129.33 («Etapa 3b»): with distri_pedido.ui_v2 on, the page takes the
kit's look (dn.css + kit/despacho_v2.css instead of distri_ui.css) and the
role's bottom bar (kit.legacy_ui). The data, the buttons, the API and the
permissions do not change; with the flag off the page is the same as before.
"""
import hashlib
import json
import os

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from .kit import legacy_ui
from .main import FICHA
from .reparto import SHARED_CSS, _disk

GROUP_DISPATCH = "distri_pedido.grupo_administracion"
STATIC = "/distri_pedido/static/src/despacho/"
_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "despacho")
ASSETS = ("despacho.js", "despacho.css")
ACTIONS = ("check", "uncheck", "leave", "undo", "controlled")


class _NeedsConfirmation(Exception):
    """«Controlado» passed every check, but the dispatcher has to see the
    customers' other open orders first: the savepoint rolls it back."""

    def __init__(self, notes):
        super().__init__(notes)
        self.notes = notes


def asset_version():
    digest = hashlib.sha256()
    for path in [os.path.join(_DIR, name) for name in ASSETS] + [_disk(SHARED_CSS)]:
        stat = os.stat(path)
        digest.update(("%s:%s:%s;" % (path, stat.st_mtime_ns, stat.st_size)).encode())
    return digest.hexdigest()[:12]


class Despacho(http.Controller):

    def _allowed(self):
        return request.env.user.has_group(GROUP_DISPATCH)

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status, headers=[
            ("Cache-Control", "no-store"),
        ])

    def _route(self, route_id):
        try:
            wanted = int(route_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        route = request.env["distri.route"].browse(wanted).exists()
        if not route or route.kind != "route":
            return None
        return route

    @http.route("/despacho", type="http", auth="user", sitemap=False)
    def app(self, **kw):
        if not self._allowed():
            raise Forbidden()
        version = asset_version()
        html = request.env["ir.qweb"]._render("distri_pedido.despacho_app", {
            "version": version,
            "static": STATIC,
            "shared_css": SHARED_CSS,
            "empresa": FICHA["nombre"],
            # 19.0.1.129.33: the kit's look and the role's bar (ui_v2 only)
            **legacy_ui(request.env, "despacho", "despacho_v2.css"),
        })
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    @http.route("/despacho/api/routes", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def routes(self, **kw):
        if not self._allowed():
            return self._json({"ok": False, "error": "Solo administración y gerencia."}, 403)
        routes = request.env["distri.route"]._despacho_routes()
        return self._json({"ok": True, "user": request.env.user.name,
                           "routes": [r._despacho_card() for r in routes]})

    @http.route("/despacho/api/route", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def route(self, id=None, **kw):
        if not self._allowed():
            return self._json({"ok": False, "error": "Solo administración y gerencia."}, 403)
        route = self._route(id)
        if not route:
            return self._json({"ok": False, "error": "No se encontró la ruta."}, 404)
        return self._json({"ok": True, "route": route._despacho_payload()})

    @http.route("/despacho/api/action", type="http", auth="user", methods=["POST"],
                csrf=False, sitemap=False)
    def action(self, **kw):
        """One dispatcher action. Same protection as /reparto/api/sync: a JSON
        body, which a cross-site form cannot send without a CORS preflight."""
        if not self._allowed():
            return self._json({"ok": False, "error": "Solo administración y gerencia."}, 403)
        httpreq = request.httprequest
        if httpreq.mimetype != "application/json":
            return self._json({"ok": False, "error": "Se espera JSON."}, 415)
        if httpreq.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._json({"ok": False, "error": "Origen no permitido."}, 403)
        try:
            body = json.loads(httpreq.get_data(as_text=True) or "{}")
        except ValueError:
            return self._json({"ok": False, "error": "JSON inválido."}, 400)
        if not isinstance(body, dict) or body.get("action") not in ACTIONS:
            return self._json({"ok": False, "error": "Acción desconocida."}, 400)
        route = self._route(body.get("route_id"))
        if not route:
            return self._json({"ok": False, "error": "No se encontró la ruta."}, 404)
        try:
            with request.env.cr.savepoint():
                self._do(route, body)
        except _NeedsConfirmation as exc:
            request.env.invalidate_all()
            return self._json({"ok": False, "confirm": exc.notes,
                               "route": route._despacho_payload()})
        except UserError as exc:
            request.env.invalidate_all()
            return self._json({"ok": False, "error": str(exc.args[0] if exc.args else exc),
                               "route": route._despacho_payload()})
        return self._json({"ok": True, "route": route._despacho_payload()})

    def _do(self, route, body):
        action = body["action"]
        if action == "controlled":
            notes = route._planning_sibling_notes()
            route.action_mark_controlled()
            if notes and body.get("confirmed") is not True:
                raise _NeedsConfirmation(notes)
            return
        if action == "undo":
            try:
                left_id = int(body.get("left_id") or 0)
            except (TypeError, ValueError):
                left_id = 0
            left = route.left_order_ids.filtered(lambda l: l.id == left_id)
            if not left:
                raise UserError("Ese pedido ya no figura como que queda en el depósito.")
            left.action_undo()
            return
        try:
            stop_id = int(body.get("stop_id") or 0)
        except (TypeError, ValueError):
            stop_id = 0
        stop = route.stop_ids.filtered(lambda s: s.id == stop_id)
        if not stop:
            raise UserError("Ese pedido ya no está en la ruta.")
        if action == "check":
            stop.action_dispatch_check()
        elif action == "uncheck":
            stop.action_dispatch_uncheck()
        elif action == "leave":
            stop.action_leave_at_depot(body.get("reason"))
