"""The delivery driver's app (PWA) and its sync API.

The app shell has no user data, so the service worker can cache it and the
app opens without signal. Everything the driver needs is downloaded as JSON
and kept in IndexedDB on the phone; changes go back through /reparto/api/sync
as events with a UUID generated on the phone (see models/reparto_sync.py).

Like the packing screen (/armado), everything runs with sudo(): each route
checks the group, and a route is read and written only by its driver.

19.0.1.113.0: the page loads app2.js (app.js stays one version for the
19.0.1.112.0 template: static files are served live). /reparto/api/route
names the user (user_id) and lists the driver's open routes for «Tus rutas
de hoy», the one «En reparto» first; a route asked for that is gone (deleted,
another driver's, settled) is answered with "reset" and the current route,
never a 404, so the phone moves on and keeps its pending changes. The sync
refuses a batch whose user_id is not the session's (another user logged in
on the same phone).

19.0.1.115.0: the page loads app3.js (amounts in the Argentine format,
a reopened route opens again); app2.js stays for the cached older page.

19.0.1.129.16: the page loads app4.js («Pagado / comprobante cargado» in the
visit, models/reparto_pagos.py, and /reparto/api/receipt); app3.js stays for
the cached older page.

19.0.1.129.25 (#105): POST /reparto/api/reopen, «Reabrir la ruta» by its own
driver (models/reparto.py `_distri_reopen`), online only.

19.0.1.129.28 («Reparto v2», models/reparto_v2.py, behind the flag
distri_pedido.reparto_v2): the page loads app5.js (the new visit, «Mis
comprobantes a reclamar», the open-route reminder and the driver's own
order); GET /reparto/api/home and POST /reparto/api/order. With the flag
off /reparto/api/home answers {"v2": false} (app5.js then keeps the old
screens) and /reparto/api/order refuses; nothing else changes. With it on,
#37: a phone that asks without a route id (a new phone, site data cleared)
gets today's route «En reparto» before an older one left open.
"""
import hashlib
import json
import os

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request
from odoo.modules.module import get_module_path

from .main import FICHA
from ..models.reparto_cliente import today_ar
from ..models.reparto_v2 import reparto_v2_on

GROUP_DRIVER = "distri_pedido.grupo_reparto"
WHOLESALE_ACCOUNT = ("Este cliente es mayorista: su cuenta la lleva administración. "
                     "Podés entregarle y cobrarle como a cualquier cliente.")
STATIC = "/distri_pedido/static/src/reparto/"
_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "reparto")
ASSETS = ("claim.js", "app.js", "app2.js", "app3.js", "app4.js", "app5.js", "app.css",
          "icon-192.png", "icon-512.png")
# The look shared with the packing screen (/armado)
SHARED_CSS = "/distri_pedido/static/src/css/distri_ui.css"
# Figtree, self-hosted by the theme; distri_ui.css points at these files
FONTS = (
    "/distri_tema/static/src/fonts/figtree-latin-wght-normal.woff2",
    "/distri_tema/static/src/fonts/figtree-latin-ext-wght-normal.woff2",
)
# a batch carries photos (expense receipts, parcel guides) compressed on the
# phone to under 1 MB each; the app sends about 1,5 MB per request
MAX_BODY = 6 * 1024 * 1024


def _disk(url):
    """/module/static/... to the file on disk, or None without that module."""
    module, *rest = url.strip("/").split("/")
    root = get_module_path(module, display_warning=False)
    return os.path.join(root, *rest) if root else None


def fonts():
    """The font files that exist: without the theme the app still caches."""
    return [url for url in FONTS if _disk(url) and os.path.isfile(_disk(url))]


def asset_version():
    """Changes whenever an app file changes: new service worker, new cache."""
    digest = hashlib.sha256()
    paths = [os.path.join(_DIR, name) for name in ASSETS + ("sw.js",)]
    paths += [_disk(SHARED_CSS)] + [_disk(url) for url in fonts()]
    for path in paths:
        stat = os.stat(path)
        digest.update(("%s:%s:%s;" % (path, stat.st_mtime_ns, stat.st_size)).encode())
    return digest.hexdigest()[:12]


class Reparto(http.Controller):

    def _is_driver(self):
        return request.env.user.has_group(GROUP_DRIVER)

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status, headers=[
            ("Cache-Control", "no-store"),
        ])

    # ------------------------------------------------------------ the shell

    @http.route("/reparto", type="http", auth="user", sitemap=False)
    def app(self, **kw):
        if not self._is_driver():
            raise Forbidden()
        version = asset_version()
        html = request.env["ir.qweb"]._render("distri_pedido.reparto_app", {
            "version": version,
            # whose state the app opens (app2.js); the cached shell keeps the
            # last user online
            "uid": request.env.user.id,
            "static": STATIC,
            "shared_css": SHARED_CSS,
            "empresa": FICHA["nombre"],
        })
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    @http.route("/reparto/manifest.webmanifest", type="http", auth="public", sitemap=False)
    def manifest(self, **kw):
        data = {
            "name": "Reparto · %s" % FICHA["nombre"],
            "short_name": "Reparto",
            "lang": "es-AR",
            "start_url": "/reparto",
            "scope": "/reparto",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f7f8f7",
            "theme_color": "#10243f",
            "icons": [
                {"src": STATIC + "icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": STATIC + "icon-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "any maskable"},
            ],
        }
        return request.make_response(json.dumps(data, ensure_ascii=False), headers=[
            ("Content-Type", "application/manifest+json; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    @http.route("/reparto/sw.js", type="http", auth="public", sitemap=False)
    def service_worker(self, **kw):
        version = asset_version()
        with open(os.path.join(_DIR, "sw.js"), encoding="utf-8") as handle:
            source = handle.read()
        shell = ["/reparto", "/reparto/manifest.webmanifest"] + [
            "%s%s?v=%s" % (STATIC, name, version) for name in ASSETS] + [
            "%s?v=%s" % (SHARED_CSS, version)] + fonts()
        source = source.replace("__VERSION__", version).replace(
            "__SHELL__", json.dumps(shell))
        return request.make_response(source, headers=[
            ("Content-Type", "text/javascript; charset=utf-8"),
            ("Cache-Control", "no-cache"),
            # the worker lives under /reparto/ and controls /reparto itself
            ("Service-Worker-Allowed", "/reparto"),
        ])

    # ------------------------------------------------------------------ API

    def _routes(self):
        """The driver's open routes: the one «En reparto» first, then by date,
        trip and id («Tus rutas de hoy»)."""
        routes = request.env["distri.route"].sudo().search([
            ("driver_id", "=", request.env.user.id),
            ("kind", "=", "route"),
            ("state", "in", ("draft", "on_route")),
        ], order="date, trip, id")
        return routes.sorted(lambda r: r.state != "on_route")

    def _wanted_route(self, id):
        """(route, gone): the route asked for when it is still this driver's to
        show, else an empty recordset and gone=True. A route closed by the
        driver and not settled is still shown («Ruta cerrada», the cash to
        hand in); a settled one, another driver's or a deleted one is gone."""
        Route = request.env["distri.route"].sudo()
        try:
            wanted = int(id)
        except (TypeError, ValueError):
            wanted = 0
        route = Route.browse(wanted).exists() if wanted > 0 else Route
        if (not route or route.driver_id != request.env.user or route.kind != "route"
                or route.state == "settled"):
            return Route, True
        return route, False

    @http.route("/reparto/api/route", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def route(self, id=None, **kw):
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        routes = self._routes()
        chosen, gone = routes[:1], False
        if reparto_v2_on(request.env) and chosen.state == "on_route":
            # 19.0.1.129.28 (#37): a phone without a route of its own (a new
            # phone, data cleared) opens today's route, not an older one left
            # open (the reminder points at that one)
            today = routes.filtered(lambda r: r.state == "on_route" and r.date == today_ar())
            chosen = today[:1] or chosen
        if id:
            route, gone = self._wanted_route(id)
            if not gone:
                chosen = route
        data = {
            "ok": True,
            "user": request.env.user.name,
            "user_id": request.env.user.id,
            "routes": [r._app_pick_payload() for r in routes],
            "route": chosen._app_payload() if chosen else None,
        }
        if gone:
            # the phone drops the route asked for and shows `route`; its
            # pending changes stay and are still sent (sync)
            data.update({"reset": True, "gone_id": int(id) if str(id).isdigit() else 0})
        return self._json(data)

    @http.route("/reparto/api/load", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def load_status(self, id=None, **kw):
        """Both ticks of each order, polled by «Control de carga» while the
        dispatcher checks on /despacho (models/despacho.py)."""
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        try:
            wanted = int(id or 0)
        except ValueError:
            wanted = 0
        route = request.env["distri.route"].sudo().browse(wanted).exists()
        if not route or route.driver_id != request.env.user or route.kind != "route":
            return self._json({"ok": False, "error": "Esa ruta no es tuya."}, 404)
        return self._json({"ok": True, "route": route._load_status_payload()})

    @http.route("/reparto/api/account", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def account(self, stop=None, **kw):
        """«Ver cuenta» of a stop (19.0.1.92.0): the customer's balance and
        newest movements, read only, online. Only for a stop on one of the
        driver's own routes (models/reparto.py _driver_stop)."""
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        Route = request.env["distri.route"]
        found = Route._driver_stop(request.env.user, stop)
        if not found or found.partner_id.commercial_partner_id.distri_generic_partner:
            return self._json({"ok": False, "error": "Ese cliente no está en tus repartos."}, 404)
        # 19.0.1.120.0: a wholesale customer's stop is delivered and collected
        # like any other, but his account stays with administración
        if not found._app_account_visible():
            return self._json({"ok": False, "error": WHOLESALE_ACCOUNT}, 403)
        return self._json({"ok": True, "account": Route._app_partner_statement(found.partner_id)})

    @http.route("/reparto/api/receipt", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def receipt(self, stop=None, receipt=None, **kw):
        """19.0.1.129.16: the receipt of a payment shown in the visit (models/
        reparto_pagos.py), photo or PDF. Same guards as /reparto/api/account:
        a stop of the driver's own routes, never a wholesale customer's."""
        if not self._is_driver():
            raise Forbidden()
        found = request.env["distri.route"]._driver_stop(request.env.user, stop)
        if not found or found.partner_id.commercial_partner_id.distri_generic_partner \
                or not found._app_account_visible():
            raise NotFound()
        result = found._app_receipt_file(receipt)
        if not result:
            raise NotFound()
        data, mimetype, name = result
        inline = mimetype.startswith("image/") and mimetype != "image/svg+xml" \
            or mimetype == "application/pdf"
        return request.make_response(data, headers=[
            ("Content-Type", mimetype if inline else "application/octet-stream"),
            ("Content-Disposition", http.content_disposition(
                name, "inline" if inline else "attachment")),
            ("Cache-Control", "private, max-age=600"),
            ("X-Content-Type-Options", "nosniff"),
        ])

    @http.route("/reparto/api/reopen", type="http", auth="user", methods=["POST"],
                csrf=False, sitemap=False)
    def reopen(self, **kw):
        """19.0.1.129.25 (#105): «Reabrir la ruta» from the app, for the
        route's own driver while the cash was not counted. No CSRF token
        for the same reason as /reparto/api/sync (a JSON body instead)."""
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        httpreq = request.httprequest
        if httpreq.mimetype != "application/json":
            return self._json({"ok": False, "error": "Se espera JSON."}, 415)
        if httpreq.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._json({"ok": False, "error": "Origen no permitido."}, 403)
        try:
            body = json.loads(httpreq.get_data(as_text=True) or "{}")
            wanted = int(body.get("route_id") or 0) if isinstance(body, dict) else 0
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "JSON inválido."}, 400)
        user = request.env.user
        route = request.env["distri.route"].sudo().browse(wanted).exists() if wanted > 0 \
            else request.env["distri.route"]
        if not route or route.driver_id != user or route.kind != "route":
            return self._json({"ok": False, "error": "Esa ruta no es tuya."}, 404)
        try:
            route._distri_reopen(user, via="app", by_driver=True)
        except UserError as exc:
            return self._json({"ok": False, "error": exc.args[0] if exc.args else str(exc),
                               "locked": True}, 409)
        return self._json({"ok": True, "state": route.state})

    @http.route("/reparto/api/home", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def home(self, **kw):
        """19.0.1.129.28: «Mis comprobantes a reclamar» and the open routes of
        earlier days (models/reparto_v2.py `_distri_v2_home`), with or without
        a route on the phone. With the flag off: {"v2": false}."""
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        if not reparto_v2_on(request.env):
            return self._json({"ok": True, "v2": False})
        data = request.env["distri.route"].sudo()._distri_v2_home(request.env.user)
        data.update({"ok": True, "v2": True, "user_id": request.env.user.id})
        return self._json(data)

    @http.route("/reparto/api/order", type="http", auth="user", methods=["POST"],
                csrf=False, sitemap=False)
    def order(self, **kw):
        """19.0.1.129.28 (#4): the driver's own visit order, online and best
        effort (the phone keeps it anyway). No CSRF token for the same
        reason as /reparto/api/sync (a JSON body instead)."""
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        if not reparto_v2_on(request.env):
            return self._json({"ok": False, "error": "No disponible."}, 404)
        httpreq = request.httprequest
        if httpreq.mimetype != "application/json":
            return self._json({"ok": False, "error": "Se espera JSON."}, 415)
        if httpreq.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._json({"ok": False, "error": "Origen no permitido."}, 403)
        try:
            body = json.loads(httpreq.get_data(as_text=True) or "{}")
            wanted = int(body.get("route_id") or 0) if isinstance(body, dict) else 0
            stop_ids = body.get("stop_ids") if isinstance(body, dict) else None
        except (TypeError, ValueError):
            return self._json({"ok": False, "error": "JSON inválido."}, 400)
        if not isinstance(stop_ids, list):
            return self._json({"ok": False, "error": "Falta el orden."}, 400)
        user = request.env.user
        route = request.env["distri.route"].sudo().browse(wanted).exists() if wanted > 0 \
            else request.env["distri.route"]
        if not route or route.driver_id != user or route.kind != "route":
            return self._json({"ok": False, "error": "Esa ruta no es tuya."}, 404)
        try:
            route._distri_v2_set_order(stop_ids)
        except UserError as exc:
            return self._json({"ok": False, "error": exc.args[0] if exc.args else str(exc)}, 409)
        return self._json({"ok": True})

    @http.route("/reparto/api/sync", type="http", auth="user", methods=["POST"],
                csrf=False, sitemap=False)
    def sync(self, **kw):
        """Events from the phone's outbox.

        No CSRF token: the cached app cannot carry a fresh one. A JSON body is
        required instead, which a cross-site form cannot send without a CORS
        preflight that Odoo does not answer.
        """
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        httpreq = request.httprequest
        if httpreq.mimetype != "application/json":
            return self._json({"ok": False, "error": "Se espera JSON."}, 415)
        if httpreq.headers.get("Sec-Fetch-Site") == "cross-site":
            return self._json({"ok": False, "error": "Origen no permitido."}, 403)
        if (httpreq.content_length or 0) > MAX_BODY:
            return self._json({"ok": False, "error": "Demasiados datos."}, 413)
        try:
            body = json.loads(httpreq.get_data(as_text=True) or "{}")
        except ValueError:
            return self._json({"ok": False, "error": "JSON inválido."}, 400)
        events = body.get("events") if isinstance(body, dict) else None
        if not isinstance(events, list):
            return self._json({"ok": False, "error": "Faltan los eventos."}, 400)
        # 19.0.1.113.0: the outbox of another user of this phone is never
        # applied as this one (app2.js sends whose events these are; the
        # older app sends nothing and keeps working as before)
        sender = body.get("user_id")
        try:
            sender = int(sender) if sender is not None else None
        except (TypeError, ValueError):
            sender = 0
        if sender is not None and sender != request.env.user.id:
            return self._json({"ok": False, "user_mismatch": True,
                               "error": "Estos cambios son de otro usuario."}, 409)
        results = request.env["distri.sync.event"].sudo().process_batch(
            request.env.user, events)
        return self._json({"ok": True, "results": results})
