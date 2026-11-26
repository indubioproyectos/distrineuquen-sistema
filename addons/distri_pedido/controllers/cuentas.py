"""«Cuentas (celular)»: the phone app to review customer accounts
(19.0.1.74.0; cash sheets 19.0.1.75.0). Same shape as /despacho: a standalone page and a small JSON
API that runs as the logged-in user. The rules and the seller scope live in
models/cuentas_app.py (distri.account.app); this file only translates HTTP.

Read-only: the only writes are adding and deleting a comment
(distri.account.comment). Nothing here registers, confirms or adjusts money.

19.0.1.112.0: the statement takes a free range (period=custom, date_from,
date_to), /cuentas/api/statement_pdf prints the existing statement report
(«Compartir PDF», «Imprimir») and the debtors take the PC filters (zone,
min_balance, min_age). The page loads cuentas_app.js / cuentas_app.css;
the old cuentas.js / cuentas.css are no longer loaded (19.0.1.113.0).

19.0.1.129.8: «Transferencias» (/cuentas/api/transfers, transfer_receipt),
the loaded transfers for administración, gerencia and sistemas only.

19.0.1.129.33 («Etapa 3b»): with distri_pedido.ui_v2 on, the page takes the
kit's look (dn.css + kit/cuentas_v2.css instead of distri_ui.css) and the
role's bottom bar (kit.legacy_ui). Same data, buttons, API and scope; with
the flag off the page is the same as before.

19.0.1.129.43 («Cuentas de clientes», DistriNeuquén design system): with
ui_v2 on, the list is ONE list of every customer by pages
(/cuentas/api/list, models/cuentas_app.py app_list) with the system's
header; «Descargar PDF / Excel» of the ⋯ menu answer /cuentas/deudores.pdf
and .xlsx (the «Deudores» report of administración, unchanged).
/cuentas/api/debtors stays for the page with the flag off.
"""
import hashlib
import json
import os

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.cuentas_app import app_scope, can_see_totals
from ..models.marca import marca_manifest_icons
from .kit import legacy_ui, ui_v2_on
from .main import FICHA
from .reparto import SHARED_CSS, _disk

STATIC = "/distri_pedido/static/src/cuentas/"
_DIR = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "cuentas")
ASSETS = ("cuentas_app.js", "cuentas_app.css")


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
            "uid": request.env.uid,
            "static": STATIC,
            "shared_css": SHARED_CSS,
            "empresa": FICHA["nombre"],
            # 19.0.1.129.33: the kit's look and the role's bar (ui_v2 only)
            **legacy_ui(request.env, "cuentas", "cuentas_v2.css"),
        })
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    @http.route("/cuentas/deudores.<string:fmt>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def debtors_export(self, fmt, seller="all", **kw):
        """19.0.1.129.43: «Descargar PDF» and «Descargar Excel» of the ⋯
        menu: a fresh «Deudores» snapshot (distri.debtor.report, the backend
        report as it is: administración and gerencia only, its own check)
        and its PDF or xlsx. Nothing else is written."""
        if fmt not in ("pdf", "xlsx"):
            raise NotFound()
        # 19.0.1.129.45: part of the 129.43 screen, so only with ui_v2
        if not ui_v2_on(request.env):
            raise NotFound()
        # 19.0.1.129.50: the report carries the list's totals, so it follows
        # the same rule (gerencia, administración, Sistemas), checked here too
        if not app_scope(request.env) or not can_see_totals(request.env):
            raise Forbidden()
        if seller not in ("all", "italo", "distrineuquen"):
            seller = "all"
        try:
            report = request.env["distri.debtor.report"].create({"seller": seller})
            if fmt == "xlsx":
                url = report.action_export_xlsx()["url"]
            else:
                url = "/report/pdf/distri_pedido.deudores_documento/%d" % report.id
        except AccessError:
            raise Forbidden() from None
        return request.redirect(url, local=True)

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
            "icons": marca_manifest_icons(),
        }
        return request.make_response(json.dumps(data, ensure_ascii=False), headers=[
            ("Content-Type", "application/manifest+json; charset=utf-8"),
            ("Cache-Control", "no-cache"),
        ])

    # ------------------------------------------------------------ reads

    @http.route("/cuentas/api/debtors", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def debtors(self, seller="all", settled="0", zone="", min_balance=0, min_age=0, **kw):
        return self._call("app_debtors", seller=seller, settled=settled == "1", zone=zone,
                          min_balance=min_balance, min_age=min_age)

    @http.route("/cuentas/api/list", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def list_page(self, q="", seller="all", zone="", min_balance=0, min_age=0, state="all",
             sort="debt", offset=0, limit=50, zones="0", **kw):
        """19.0.1.129.43: one page of the one list of customers (app_list).
        19.0.1.129.45: only with ui_v2, like the screen that calls it."""
        if not ui_v2_on(request.env):
            raise NotFound()
        return self._call("app_list", q=q, seller=seller, zone=zone, min_balance=min_balance,
                          min_age=min_age, state=state, sort=sort, offset=offset, limit=limit,
                          with_zones=zones == "1")

    @http.route("/cuentas/api/account", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def account(self, id=None, period="3m", offset=0, date_from="", date_to="", **kw):
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            offset = 0
        return self._call("app_account", id, period=period, offset=offset,
                          date_from=date_from, date_to=date_to)

    @http.route("/cuentas/api/statement_pdf", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def statement_pdf(self, id=None, period="3m", date_from="", date_to="", download="0", **kw):
        """The statement report of the range («Compartir PDF», «Imprimir»):
        the statement wizard's rules decide (models/cuentas_app.py
        app_statement_pdf); a refusal answers JSON, like the other reads."""
        if not app_scope(request.env):
            return self._json({"ok": False, "error": "Sin acceso a esta pantalla."}, 403)
        try:
            pdf, name = self._app().app_statement_pdf(id, period=period, date_from=date_from,
                                                      date_to=date_to)
        except AccessError as exc:
            return self._json({"ok": False, "error": _message(exc)}, 403)
        except UserError as exc:
            return self._json({"ok": False, "error": _message(exc)}, 400)
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Disposition", http.content_disposition(
                name, "attachment" if download == "1" else "inline")),
            ("Cache-Control", "no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])

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

    @http.route("/cuentas/api/transfers", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def transfers(self, period="today", date_from="", date_to="", user="0", state="all",
                  offset=0, **kw):
        """19.0.1.129.8 «Transferencias»: the loaded transfers, only for
        administración, gerencia and sistemas (models/cuentas_transferencias.py)."""
        try:
            offset = int(offset or 0)
        except (TypeError, ValueError):
            offset = 0
        return self._call("app_transfers", period=period, date_from=date_from,
                          date_to=date_to, user_id=user, state=state, offset=offset)

    @http.route("/cuentas/api/transfer_receipt", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def transfer_receipt(self, id=None, **kw):
        """The receipt of one loaded transfer, same groups as /transfers."""
        if not app_scope(request.env):
            raise Forbidden()
        try:
            data, mimetype, name = self._app().app_transfer_receipt(id)
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
