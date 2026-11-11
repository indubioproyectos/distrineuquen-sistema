"""«Cuentas (celular)»: the phone app to review customer accounts
(19.0.1.74.0; cash sheets 19.0.1.75.0). Same shape as /despacho: a standalone page and a small JSON
API that runs as the logged-in user. The rules and the seller scope live in
models/cuentas_app.py (distri.account.app); this file only translates HTTP.

Read-only: the only writes are adding and deleting a comment
(distri.account.comment). Nothing here registers, confirms or adjusts money.
"""
import hashlib
import json
import os

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.cuentas_app import app_scope
from .main import FICHA
from .reparto import SHARED_CSS, _disk

STATIC = "/distri_pedido/static/src/cuentas/"
ICONS = "/distri_pedido/static/src/reparto/"
_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "cuentas")
ASSETS = ("cuentas.js", "cuentas.css")


def asset_version():
    digest = hashlib.sha256()
    for path in [os.path.join(_DIR, name) for name in ASSETS] + [_disk(SHARED_CSS)]:
        stat = os.stat(path)
        digest.update(("%s:%s:%s;" % (path, stat.st_mtime_ns, stat.st_size)).encode())
    return digest.hexdigest()[:12]


def _message(exc):
    return str(exc.args[0] if exc.args else exc)


class Cuentas(http.Controller):

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status, headers=[
            ("Cache-Control", "no-store"),
        ])

    def _app(self):
        return request.env["distri.account.app"]

    def _call(self, method, *args, **kwargs):
        """Run one read of the app; access and business errors become JSON."""
        if not app_scope(request.env):
            return self._json({"ok": False, "error": "Sin acceso a esta pantalla."}, 403)
        try:
            data = getattr(self._app(), method)(*args, **kwargs)
        except AccessError as exc:
            return self._json({"ok": False, "error": _message(exc)}, 403)
        except UserError as exc:
            return self._json({"ok": False, "error": _message(exc)}, 400)
        return self._json({"ok": True, "data": data})

    def _body(self):
        """A JSON body, which a cross-site form cannot send without a CORS
        preflight (same protection as /despacho/api/action)."""
        httpreq = request.httprequest
        if httpreq.mimetype != "application/json":
            return None, self._json({"ok": False, "error": "Se espera JSON."}, 415)
        if httpreq.headers.get("Sec-Fetch-Site") == "cross-site":
            return None, self._json({"ok": False, "error": "Origen no permitido."}, 403)
        try:
            body = json.loads(httpreq.get_data(as_text=True) or "{}")
        except ValueError:
            body = None
        if not isinstance(body, dict):
            return None, self._json({"ok": False, "error": "JSON inválido."}, 400)
        return body, None

    # ------------------------------------------------------------ the page

    @http.route("/cuentas", type="http", auth="user", sitemap=False)
    def app(self, **kw):
        if not app_scope(request.env):
            raise Forbidden()
        version = asset_version()
        html = request.env["ir.qweb"]._render("distri_pedido.cuentas_app", {
            "version": version,
            "static": STATIC,
            "icons": ICONS,
            "shared_css": SHARED_CSS,
            "empresa": FICHA["nombre"],
        })
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    @http.route("/cuentas/manifest.webmanifest", type="http", auth="public", sitemap=False)
    def manifest(self, **kw):
        """Add to the home screen; no service worker (online only)."""
        data = {
            "name": "Cuentas · %s" % FICHA["nombre"],
            "short_name": "Cuentas",
            "lang": "es-AR",
            "start_url": "/cuentas",
            "scope": "/cuentas",
            "display": "standalone",
            "orientation": "portrait",
            "background_color": "#f7f8f7",
            "theme_color": "#10243f",
            "icons": [
                {"src": ICONS + "icon-192.png", "sizes": "192x192", "type": "image/png"},
                {"src": ICONS + "icon-512.png", "sizes": "512x512", "type": "image/png"},
            ],
        }
        return request.make_response(json.dumps(data, ensure_ascii=False), headers=[
            ("Content-Type", "application/manifest+json; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    # ------------------------------------------------------------ reads

    @http.route("/cuentas/api/debtors", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def debtors(self, seller="all", **kw):
        return self._call("app_debtors", seller=seller)

    @http.route("/cuentas/api/account", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def account(self, id=None, period="3m", offset=0, **kw):
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            offset = 0
        return self._call("app_account", id, period=period, offset=offset)

    @http.route("/cuentas/api/line", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def line(self, id=None, key=None, **kw):
        return self._call("app_line", id, key or "")

    @http.route("/cuentas/api/receipt", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def receipt(self, id=None, card=None, **kw):
        """The receipt of one of the customer's Mercado Pago cards."""
        if not app_scope(request.env):
            raise Forbidden()
        try:
            data, mimetype, name = self._app().app_receipt(id, int(card or 0))
        except (AccessError, ValueError, TypeError):
            raise Forbidden()
        return self._file(data, mimetype, name)

    @http.route("/cuentas/api/sheet_file", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def sheet_file(self, sheet=None, file=None, **kw):
        """A photo or the xlsx of a cash sheet, only for a user who sees every
        row of it (models/cuentas_app.py app_sheet_file)."""
        if not app_scope(request.env):
            raise Forbidden()
        try:
            data, mimetype, name = self._app().app_sheet_file(sheet, file)
        except (AccessError, ValueError, TypeError):
            raise Forbidden()
        return self._file(data, mimetype, name)

    def _file(self, data, mimetype, name):
        # an image or a PDF opens in the page; anything else only downloads
        inline = mimetype.startswith("image/") or mimetype == "application/pdf"
        if mimetype == "image/svg+xml":
            inline = False
        return request.make_response(data, headers=[
            ("Content-Type", mimetype if inline else "application/octet-stream"),
            ("Content-Disposition", http.content_disposition(
                name, "inline" if inline else "attachment")),
            ("Cache-Control", "private, max-age=600"),
            ("X-Content-Type-Options", "nosniff"),
        ])

    # ------------------------------------------------------------ comments

    @http.route("/cuentas/api/comment", type="http", auth="user", methods=["POST"],
                csrf=False, sitemap=False)
    def add_comment(self, **kw):
        body, error = self._body()
        if error:
            return error
        return self._call("app_add_comment", body.get("partner_id"), body.get("body") or "",
                          line_key=body.get("line_key") or "")

    @http.route("/cuentas/api/comment/delete", type="http", auth="user", methods=["POST"],
                csrf=False, sitemap=False)
    def delete_comment(self, **kw):
        body, error = self._body()
        if error:
            return error
        return self._call("app_delete_comment", body.get("id"))
