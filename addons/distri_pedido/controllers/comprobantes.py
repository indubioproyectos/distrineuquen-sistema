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
    /comprobantes/<id>/acreditar 19.0.1.126.0: «Acreditar» (POST), the staff
                                 who may credit it (models/acreditar.py)
    /comprobantes/intento/<id>   19.0.1.101.0: a refused duplicate of the user,
                                 the existing receipt it matched (few facts,
                                 read only); /archivo its file
    /comprobantes/sugerir        19.0.1.126.1: reads a chosen file before the
                                 upload (nothing stored): suggested customers
                                 and the amount read
    /comprobantes/<id>/cancelar, /cambiar-foto, /girar
                                 19.0.1.126.1: the uploader manages his upload
                                 (models/subir_v126_1.py)

19.0.1.126.2: the same file as the user's own receipt pending assignment is
not refused: its result «completar» links /comprobantes/<id>/completar
(«Completar y acreditar» on the existing receipt, models/subir_v126_2.py).

19.0.1.126.1: «/comprobantes/subir» takes «no_en_sistema» + «remitente» (who
sent it, when the customer is not in the system) and «acreditar» («Subir y
acreditar»); the customer is mandatory once the module is updated
(`_distri_staff_upload`). «Compartir con Distri» opens the same form, so the
same rule applies to it.

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

from ..models.acreditar import CREDIT_NOTICE, CreditWarning, can_credit
from ..models.esquema_v126 import v126_1_ready, v126_ready
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
            # 19.0.1.126.0: the one-time notice, also here (where they credit)
            "aviso_acreditar": CREDIT_NOTICE if user._distri_credit_notice_pending() else "",
            "puede_acreditar": can_credit(user) and v126_ready(request.env),
            "v126_1": v126_1_ready(request.env),
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
        # 19.0.1.115.0: a wholesale customer, only for who sees them (the
        # picker already hides them, lista_oculta.ve_mayoristas)
        return request.env["distri.cash.collection"]._cash_visible(partner)

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
    def upload(self, cliente_id="", compartido="", no_en_sistema="", remitente="", acreditar="",
               **kw):
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
                results = request.env["distri.whatsapp.message"]._distri_staff_upload(
                    files, partner=partner, unknown=bool(no_en_sistema), sender_note=remitente,
                    credit=bool(acreditar), via="compartido" if stage else "pantalla",
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
        return self._view_page(msg, parent)

    def _view_page(self, msg, parent, **values):
        """«Ver comprobante», with the «Acreditar» form for who may credit it."""
        user = request.env.user
        if msg._distri_credit_card(user):
            # 19.0.1.126.0: the amount read is shown before crediting
            msg._distri_read_now()
        base = {"r": msg._distri_upload_detail(), "parent": parent,
                "credit": msg._distri_credit_form(user),
                "credit_warnings": [], "credit_error": "", "credit_done": "",
                "manage_error": "", "manage_done": "",
                # 19.0.1.126.2: the uploader's own receipt pending assignment
                "complete": msg._distri_upload_complete_form(user), "complete_error": "",
                "csrf_token": request.csrf_token()}
        base.update(values)
        if base["complete"]:
            # completing it is the way to credit it: one form only
            base["credit"] = None
        return self._page_response("distri_pedido.comprobante_ver_page", base)

    @http.route("/comprobantes/<int:msg_id>/acreditar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def credit(self, msg_id, importe="", cliente_id="", confirmado="", desde="", **kw):
        """«Acreditar» (19.0.1.126.0): the amount (the one read, editable),
        the customer and, after a warning, «Acreditar igual»."""
        msg = self._receipt(msg_id)
        parent = "/comprobantes/mios" if desde == "mios" else "/comprobantes"
        user = request.env.user
        card = msg._distri_credit_card(user)
        if not card:
            return self._credit_answer(msg, parent, credit_error="Este comprobante ya no se "
                                       "puede acreditar desde acá.")
        msg._distri_read_now()
        partner = self._partner(cliente_id) or card.sudo().partner_id.commercial_partner_id
        try:
            amount = request.env["distri.cash.collection"]._cash_amount(importe)
        except (UserError, AccessError) as exc:
            return self._credit_answer(msg, parent, credit_error=_error_text(exc))
        try:
            with request.env.cr.savepoint():
                payment = request.env["distri.mp.collection"].browse(card.id)._distri_credit(
                    partner, amount, confirmed=bool(confirmado))
        except CreditWarning as exc:
            form = msg._distri_credit_form(user) or {}
            form.update(amount_text=importe, partner_id=partner.id or "",
                        partner_name=partner.name or "")
            return self._credit_answer(msg, parent, credit=form, credit_warnings=exc.warnings)
        except (UserError, AccessError) as exc:
            return self._credit_answer(msg, parent, credit_error=_error_text(exc))
        return self._credit_answer(msg, parent, credit_done="Acreditado: %s a %s (pago %s)." % (
            _money(amount), partner.name, payment.name))

    def _credit_answer(self, msg, parent, **values):
        """«Ver comprobante» again, or JSON for the upload page's «Acreditar
        igual» (19.0.1.126.1)."""
        if not self._by_fetch():
            return self._view_page(msg, parent, **values)
        uploads, attempts = self._today(request.env.user)
        data = {"ok": True, "uploads": uploads, "attempts": attempts,
                "credited": bool(values.get("credit_done")),
                "message": values.get("credit_done") or values.get("credit_error") or "",
                "warnings": values.get("credit_warnings") or []}
        return self._json(data)

    # ---------------------------------------------------------------- 19.0.1.126.1

    @http.route("/comprobantes/sugerir", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def suggest(self, compartido="", indice="", giro="", **kw):
        """Reads one chosen file (or a shared one, by its index) without
        storing it: {suggestions, amount, amount_text, amount_sure}."""
        self._check()
        if (request.httprequest.content_length or 0) > MAX_FILE_BYTES + 1024 * 1024:
            return self._json({"ok": False, "error": "El archivo es demasiado grande."},
                              status=413)
        raw = b""
        storage = request.httprequest.files.get("archivo")
        if storage and storage.filename:
            raw = storage.read(MAX_FILE_BYTES + 1)
        elif compartido:
            stage = request.env["distri.receipt.share"]._distri_claim(compartido)
            try:
                index = int(indice or 0)
            except ValueError:
                index = -1
            shared = stage._distri_files() if stage else []
            if 0 <= index < len(shared):
                raw = shared[index][1]
        preview = request.env["distri.whatsapp.message"]._distri_upload_preview(raw, giro)
        return self._json(dict(preview, ok=True))

    def _manage(self, msg_id, action):
        """Runs `action(msg)` (cancel, replace, turn) and shows «Ver
        comprobante» with its answer."""
        msg = self._receipt(msg_id)
        parent = "/comprobantes/mios" if request.params.get("desde") == "mios" else "/comprobantes"
        try:
            with request.env.cr.savepoint():
                done = action(msg.with_user(request.env.user).sudo(False))
        except (UserError, AccessError) as exc:
            if self._by_fetch():
                return self._json({"ok": False, "error": _error_text(exc)}, status=400)
            return self._view_page(msg, parent, manage_error=_error_text(exc))
        if self._by_fetch():
            uploads, attempts = self._today(request.env.user)
            return self._json({"ok": True, "message": done, "uploads": uploads,
                               "attempts": attempts})
        return self._view_page(msg, parent, manage_done=done)

    @http.route("/comprobantes/<int:msg_id>/cancelar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def cancel(self, msg_id, motivo="", **kw):
        return self._manage(msg_id, lambda msg: msg._distri_upload_cancel(motivo))

    @http.route("/comprobantes/<int:msg_id>/cambiar-foto", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def replace(self, msg_id, giro="", **kw):
        if (request.httprequest.content_length or 0) > MAX_FILE_BYTES + 1024 * 1024:
            return self._refuse("La foto es demasiado grande (más de 10 MB).", status=413)
        storage = request.httprequest.files.get("archivo")
        name, raw = (storage.filename, storage.read(MAX_FILE_BYTES + 1)) \
            if storage and storage.filename else ("", b"")

        def replace(msg):
            if not raw:
                raise UserError("Elegí la foto nueva.")
            return msg._distri_upload_replace(name, raw, giro)
        return self._manage(msg_id, replace)

    @http.route("/comprobantes/<int:msg_id>/girar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def turn(self, msg_id, sentido="", **kw):
        degrees = 270 if sentido == "izquierda" else 90
        return self._manage(msg_id, lambda msg: msg._distri_upload_rotate(degrees))

    # ---------------------------------------------------------------- 19.0.1.126.2

    @http.route("/comprobantes/<int:msg_id>/completar", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def complete_page(self, msg_id, cliente_id="", desde="", **kw):
        """«Ya subiste este comprobante y quedó sin acreditar. Completalo:»
        (the result of uploading the same file again), else «Ver comprobante»."""
        msg = self._receipt(msg_id)
        parent = "/comprobantes/mios" if desde == "mios" else "/comprobantes"
        form = msg._distri_upload_complete_form(request.env.user, self._partner(cliente_id))
        if not form:
            return request.redirect("/comprobantes/%d" % msg.id)
        return self._view_page(msg, parent, complete=form)

    @http.route("/comprobantes/<int:msg_id>/completar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def complete(self, msg_id, cliente_id="", importe="", confirmado="", desde="", **kw):
        """«Completar y acreditar»: the customer on the existing receipt and
        the credit of `_distri_credit` (models/subir_v126_2.py)."""
        msg = self._receipt(msg_id)
        parent = "/comprobantes/mios" if desde == "mios" else "/comprobantes"
        user = request.env.user
        partner = self._partner(cliente_id)
        try:
            amount = request.env["distri.cash.collection"]._cash_amount(importe) \
                if str(importe or "").strip() else None
            with request.env.cr.savepoint():
                result = msg.with_user(user).sudo(False)._distri_upload_complete(
                    partner, amount=amount, confirmed=bool(confirmado))
        except (UserError, AccessError) as exc:
            if self._by_fetch():
                return self._json({"ok": False, "error": _error_text(exc)}, status=400)
            form = msg._distri_upload_complete_form(user, partner)
            if form:
                form["amount_text"] = importe or form["amount_text"]
            return self._view_page(msg, parent, complete=form, complete_error=_error_text(exc))
        msg.invalidate_recordset()
        if self._by_fetch():
            uploads, attempts = self._today(user)
            return self._json(dict(result, ok=True, id=msg.id,
                                   credited=result.get("credit") == "acreditado",
                                   uploads=uploads, attempts=attempts))
        if result.get("credit") == "acreditado":
            return self._view_page(msg, parent, credit_done=result["message"])
        if result.get("credit") == "aviso":
            form = msg._distri_credit_form(user) or {}
            form.update(amount_text=result.get("amount_text", ""),
                        partner_id=result.get("partner_id") or "",
                        partner_name=result.get("partner_name") or "")
            return self._view_page(msg, parent, credit=form,
                                   credit_warnings=result.get("warnings") or [])
        return self._view_page(msg, parent, credit_error=result.get("message") or "")

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
