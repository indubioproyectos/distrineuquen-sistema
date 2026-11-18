"""«Subir comprobante» (19.0.1.99.0, audit proposal 3a): /comprobantes.

    /tablero (staff home)
      /comprobantes              take a photo / pick photos or PDF, customer
                                 (optional), «Subidos hoy por vos»
    /comprobantes/compartir      Web Share Target of the staff app (manifest
                                 of /tablero): stages the shared files and
                                 opens /comprobantes with them
    /comprobantes/<id>           «Ver comprobante» (19.0.1.100.0): the file,
                                 amount, customer, state; read only
    /comprobantes/<id>/archivo   the stored file, for that page
    /comprobantes/mios           «Mis comprobantes»: last 30 days, by state
    /comprobantes/intento/<id>   19.0.1.101.0: a refused duplicate of the user,
                                 the existing receipt it matched (few facts,
                                 read only); /archivo its file

19.0.1.100.0: an optional «Importe» and a «Girar» per file. comprobantes.js
sends one «importes» and one «giros» value per «archivos» file, in the same
order, and one «importe_compartido» / «giro_compartido» per shared file (the
page renders those inputs); the model parses and checks them. An older
server ignores these fields.

19.0.1.101.0: the JSON of /comprobantes/hoy and /comprobantes/subir keeps
«uploads» (only the receipts created) and adds «attempts» (the refused
duplicates of today); the page merges both by time.

Plain HTML forms that also work without JavaScript; comprobantes.js sends
them with fetch() (X-Distri-Fetch: 1) and gets JSON. The rules live in
models/comprobante_subir.py; every route checks the groups first
(can_upload) and then works with sudo.
"""
from urllib.parse import quote

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.comprobante_subir import (
    ATTEMPT_FILTER, BUCKETS, LIST_DAYS, MAX_FILE_BYTES, MAX_FILES, can_upload, merge_rows)
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

    def _today(self, user):
        """(rows of today's uploads, rows of today's refused attempts)."""
        uploads = request.env["distri.whatsapp.message"]._distri_uploads_today(user)
        attempts = request.env["distri.receipt.upload.attempt"]._distri_for(user, 1)
        return ([m._distri_upload_row() for m in uploads],
                [a._distri_row(user) for a in attempts[:50]])

    def _render(self, values, status=200):
        user = request.env.user
        uploads, attempts = self._today(user)
        base = {
            "dn": kit_context("comprobantes.css", "comprobantes.js"),
            "user": user,
            "money": _money,
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            "csrf_token": request.csrf_token(),
            "uploads": uploads,
            "attempts": attempts,
            "rows": merge_rows(uploads, attempts),
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
        return [(name, raw) for name, raw, _amount, _turn in self._files_with_amounts()]

    def _files_with_amounts(self):
        """[(filename, bytes, typed amount text, «Girar» value)]: the n-th
        «importes» and «giros» values belong to the n-th «archivos» file."""
        form = request.httprequest.form
        amounts, turns = form.getlist("importes"), form.getlist("giros")
        out = []
        for n, storage in enumerate(request.httprequest.files.getlist("archivos")):
            if not storage or not storage.filename:
                continue
            raw = storage.read(MAX_FILE_BYTES + 1)
            out.append((storage.filename, raw, amounts[n] if n < len(amounts) else "",
                        turns[n] if n < len(turns) else ""))
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
        uploads, attempts = self._today(user)
        return self._json({"ok": True, "uploads": uploads, "attempts": attempts})

    @http.route("/comprobantes/clientes", type="http", auth="user", methods=["GET"], sitemap=False)
    def customers(self, q="", **kw):
        self._check()
        return self._json({"ok": True, "results": request.env["distri.cash.collection"].cash_search(q)})

    @http.route("/comprobantes/subir", type="http", auth="user", methods=["POST"], sitemap=False)
    def upload(self, cliente_id="", compartido="", **kw):
        self._check()
        if (request.httprequest.content_length or 0) > MAX_BODY:
            return self._refuse("Son demasiados archivos juntos: subilos de a menos.", status=413)
        chosen = self._files_with_amounts()
        files = [(name, raw) for name, raw, _amount, _turn in chosen]
        amounts = [amount for _name, _raw, amount, _turn in chosen]
        turns = [turn for _name, _raw, _amount, turn in chosen]
        stage = request.env["distri.receipt.share"]
        if compartido:
            stage = stage._distri_claim(compartido)
            if stage:
                shared = stage._distri_files()
                form = request.httprequest.form
                shared_amounts = form.getlist("importe_compartido")
                shared_turns = form.getlist("giro_compartido")
                files = shared + files
                amounts = [shared_amounts[n] if n < len(shared_amounts) else ""
                           for n in range(len(shared))] + amounts
                turns = [shared_turns[n] if n < len(shared_turns) else ""
                         for n in range(len(shared))] + turns
        partner = self._partner(cliente_id)
        try:
            with request.env.cr.savepoint():
                results = request.env["distri.whatsapp.message"]._distri_upload_receipts(
                    files, partner=partner, via="compartido" if stage else "pantalla",
                    amounts=amounts, rotations=turns)
        except (UserError, AccessError) as exc:
            return self._refuse(_error_text(exc))
        if stage:
            stage._distri_done()
        if self._by_fetch():
            uploads, attempts = self._today(request.env.user)
            return self._json({"ok": True, "results": results, "uploads": uploads,
                               "attempts": attempts})
        return self._render({"results": results})

    def _refuse(self, text, status=400):
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=status)
        return self._render({"error": text}, status=status)

    # ---------------------------------------------------------------- «Ver comprobante»

    def _receipt(self, msg_id):
        """The uploaded receipt the user may see, else 404 (whether it exists
        is not told)."""
        user = self._check()
        msg = request.env["distri.whatsapp.message"].sudo().browse(msg_id).exists()
        if not msg or not msg._distri_upload_visible_to(user):
            raise NotFound()
        return msg

    def _page_response(self, template, values):
        user = request.env.user
        base = {
            "dn": kit_context("comprobantes.css", "comprobantes.js"),
            "user": user,
            "money": _money,
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    @http.route("/comprobantes/<int:msg_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def view(self, msg_id, desde="", **kw):
        msg = self._receipt(msg_id)
        parent = "/comprobantes/mios" if desde == "mios" else "/comprobantes"
        return self._page_response("distri_pedido.comprobante_ver_page", {
            "r": msg._distri_upload_detail(), "parent": parent})

    @http.route("/comprobantes/<int:msg_id>/archivo", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def file(self, msg_id, **kw):
        return self._file_response(self._receipt(msg_id))

    def _file_response(self, msg):
        attachment = msg.attachment_id.sudo()
        if not attachment:
            raise NotFound()
        name = (attachment.name or "comprobante").replace('"', "")
        return request.make_response(attachment.raw or b"", headers=[
            ("Content-Type", attachment.mimetype or "application/octet-stream"),
            ("Content-Disposition", "inline; filename*=UTF-8''%s" % quote(name)),
            ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, no-store")])

    @http.route("/comprobantes/mios", type="http", auth="user", methods=["GET"], sitemap=False)
    def mine(self, estado="", **kw):
        user = self._check()
        bucket = estado if estado in BUCKETS or estado == ATTEMPT_FILTER else ""
        rows = []
        if bucket != ATTEMPT_FILTER:
            msgs = request.env["distri.whatsapp.message"]._distri_uploads_recent(
                user, bucket or None)
            rows = [m._distri_upload_row() for m in msgs]
        attempts = []
        if bucket in ("", ATTEMPT_FILTER):
            found = request.env["distri.receipt.upload.attempt"]._distri_for(user, LIST_DAYS)
            attempts = [a._distri_row(user) for a in found]
        return self._page_response("distri_pedido.comprobantes_mios_page", {
            "rows": merge_rows(rows, attempts),
            "estado": bucket,
            "filters": [("", "Todos"), ("pendiente", "Pendientes"), ("aprobado", "Aprobados"),
                        ("rechazado", "Rechazados"), (ATTEMPT_FILTER, "Ya estaban cargados")],
            "days": LIST_DAYS,
        })

    # ---------------------------------------------------------------- refused duplicates

    def _attempt(self, attempt_id):
        """The user's own refused attempt, else 404."""
        user = self._check()
        attempt = request.env["distri.receipt.upload.attempt"].sudo().browse(attempt_id).exists()
        if not attempt or not attempt._distri_visible_to(user):
            raise NotFound()
        return attempt

    @http.route("/comprobantes/intento/<int:attempt_id>", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def attempt(self, attempt_id, desde="", **kw):
        attempt = self._attempt(attempt_id)
        parent = "/comprobantes/mios" if desde == "mios" else "/comprobantes"
        return self._page_response("distri_pedido.comprobante_intento_page", {
            "r": attempt._distri_detail(), "parent": parent})

    @http.route("/comprobantes/intento/<int:attempt_id>/archivo", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def attempt_file(self, attempt_id, **kw):
        return self._file_response(self._attempt(attempt_id).message_id)

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
