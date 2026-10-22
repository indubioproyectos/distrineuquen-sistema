"""The delivery driver's app (PWA) and its sync API.

The app shell has no user data, so the service worker can cache it and the
app opens without signal. Everything the driver needs is downloaded as JSON
and kept in IndexedDB on the phone; changes go back through /reparto/api/sync
as events with a UUID generated on the phone (see models/reparto_sync.py).

Like the packing screen (/armado), everything runs with sudo(): each route
checks the group, and a route is read and written only by its driver.
"""
import hashlib
import json
import os

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.http import request
from odoo.modules.module import get_module_path

from .main import FICHA

GROUP_DRIVER = "distri_pedido.grupo_reparto"
STATIC = "/distri_pedido/static/src/reparto/"
_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "reparto")
ASSETS = ("claim.js", "app.js", "app.css", "icon-192.png", "icon-512.png")
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
        return request.env["distri.route"].sudo().search([
            ("driver_id", "=", request.env.user.id),
            ("kind", "=", "route"),
            ("state", "in", ("draft", "on_route")),
        ], order="date, trip, id")

    @http.route("/reparto/api/route", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def route(self, id=None, **kw):
        if not self._is_driver():
            return self._json({"ok": False, "error": "No tenés permiso de reparto."}, 403)
        routes = self._routes()
        chosen = routes[:1]
        if id:
            try:
                wanted = int(id)
            except ValueError:
                wanted = 0
            chosen = request.env["distri.route"].sudo().browse(wanted).exists()
            if not chosen or chosen.driver_id != request.env.user or chosen.kind != "route":
                return self._json({"ok": False, "error": "Esa ruta no es tuya."}, 404)
        return self._json({
            "ok": True,
            "user": request.env.user.name,
            "routes": [{"id": r.id, "name": r.name, "state": r.state} for r in routes],
            "route": chosen._app_payload() if chosen else None,
        })

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
        results = request.env["distri.sync.event"].sudo().process_batch(
            request.env.user, events)
        return self._json({"ok": True, "results": results})
