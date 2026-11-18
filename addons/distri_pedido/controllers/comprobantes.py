"""«Subir comprobante» (19.0.1.99.0, audit proposal 3a): /comprobantes.

    /tablero (staff home)
      /comprobantes              take a photo / pick photos or PDF, customer
                                 (optional), «Subidos hoy por vos»
    /comprobantes/compartir      Web Share Target of the staff app (manifest
                                 of /tablero): stages the shared files and
                                 opens /comprobantes with them

Plain HTML forms that also work without JavaScript; comprobantes.js sends
them with fetch() (X-Distri-Fetch: 1) and gets JSON. The rules live in
models/comprobante_subir.py; every route checks the groups first
(can_upload) and then works with sudo.
"""
from urllib.parse import quote

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.comprobante_subir import MAX_FILE_BYTES, MAX_FILES, can_upload
from ..models.reparto import _money
from .kit import kit_context
from .tablero import wants_tablero

MAX_BODY = MAX_FILES * MAX_FILE_BYTES + 1024 * 1024


def _error_text(exc):
    return exc.args[0] if exc.args else str(exc)


class Comprobantes(http.Controller):

    def _check(self):
        user = request.env.user
        if not can_upload(user):
            raise Forbidden()
        return user

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _render(self, values, status=200):
        user = request.env.user
        Message = request.env["distri.whatsapp.message"]
        uploads = Message._distri_uploads_today(user)
        base = {
            "dn": kit_context("comprobantes.css", "comprobantes.js"),
            "user": user,
            "money": _money,
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            "csrf_token": request.csrf_token(),
            "uploads": [m._distri_upload_row() for m in uploads],
            "results": [],
            "error": "",
            "stage": None,
            "preview": [],
            "cliente": None,
        }
        base.update(values)
        html = request.env["ir.qweb"]._render("distri_pedido.comprobantes_page", base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _files(self):
        """[(filename, bytes)] of the request's «archivos» inputs."""
        out = []
        for storage in request.httprequest.files.getlist("archivos"):
            if not storage or not storage.filename:
                continue
            raw = storage.read(MAX_FILE_BYTES + 1)
            out.append((storage.filename, raw))
        return out

    def _partner(self, value):
        try:
            partner_id = int(value or 0)
        except (TypeError, ValueError):
            partner_id = 0
        if not partner_id:
            return request.env["res.partner"]
        partner = request.env["res.partner"].sudo().browse(partner_id).exists()
        if not partner or partner.distri_generic_partner:
            return request.env["res.partner"]
        return partner

    # ---------------------------------------------------------------- page

    @http.route("/comprobantes", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, compartido="", **kw):
        self._check()
        values = {}
        if compartido:
            stage = request.env["distri.receipt.share"]._distri_claim(compartido)
            if stage:
                values.update(stage=stage, preview=stage._distri_preview())
            else:
                values["error"] = ("Los archivos compartidos ya no están: pasaron más de 24 horas "
                                   "o ya se subieron. Compartilos de nuevo.")
        return self._render(values)

    @http.route("/comprobantes/hoy", type="http", auth="user", methods=["GET"], sitemap=False)
    def today(self, **kw):
        """«Subidos hoy por vos» for the page to refresh while files are read."""
        user = self._check()
        uploads = request.env["distri.whatsapp.message"]._distri_uploads_today(user)
        return self._json({"ok": True, "uploads": [m._distri_upload_row() for m in uploads]})

    @http.route("/comprobantes/clientes", type="http", auth="user", methods=["GET"], sitemap=False)
    def customers(self, q="", **kw):
        self._check()
        return self._json({"ok": True, "results": request.env["distri.cash.collection"].cash_search(q)})

    @http.route("/comprobantes/subir", type="http", auth="user", methods=["POST"], sitemap=False)
    def upload(self, cliente_id="", compartido="", **kw):
        self._check()
        if (request.httprequest.content_length or 0) > MAX_BODY:
            return self._refuse("Son demasiados archivos juntos: subilos de a menos.", status=413)
        files = self._files()
        stage = request.env["distri.receipt.share"]
        if compartido:
            stage = stage._distri_claim(compartido)
            if stage:
                files = stage._distri_files() + files
        partner = self._partner(cliente_id)
        try:
            with request.env.cr.savepoint():
                results = request.env["distri.whatsapp.message"]._distri_upload_receipts(
                    files, partner=partner, via="compartido" if stage else "pantalla")
        except (UserError, AccessError) as exc:
            return self._refuse(_error_text(exc))
        if stage:
            stage._distri_done()
        if self._by_fetch():
            uploads = request.env["distri.whatsapp.message"]._distri_uploads_today()
            return self._json({"ok": True, "results": results,
                               "uploads": [m._distri_upload_row() for m in uploads]})
        return self._render({"results": results})

    def _refuse(self, text, status=400):
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=status)
        return self._render({"error": text}, status=status)

    # ---------------------------------------------------------------- share target

    @http.route("/comprobantes/compartir", type="http", auth="public", methods=["POST"],
                csrf=False, sitemap=False)
    def share(self, **kw):
        """Web Share Target (POST, multipart, files «archivos»). The phone's
        share sheet sends no CSRF token: nothing is created here, the files
        only wait for the user to confirm them on /comprobantes."""
        if (request.httprequest.content_length or 0) > MAX_BODY:
            return request.make_response("Son demasiados archivos juntos.", status=413)
        user = request.env.user
        logged = not user._is_public()
        if logged and not can_upload(user):
            raise Forbidden()
        if not logged:
            # a logged-out share: limited like the public order form
            limit = request.env["distri.rate.hit"].sudo()._distri_check(
                "share", request.httprequest.remote_addr or "", request.session.sid or "")
            if limit:
                return request.make_response("Demasiados envíos seguidos. Esperá unos minutos.",
                                             status=429)
        stage = request.env["distri.receipt.share"]._distri_stage(
            self._files(), user=user if logged else None)
        if not stage:
            target = "/comprobantes"
        else:
            target = "/comprobantes?compartido=%s" % stage.token
        if not logged:
            return request.redirect("/web/login?redirect=%s" % quote(target, safe=""), 303)
        return request.redirect(target, 303)
