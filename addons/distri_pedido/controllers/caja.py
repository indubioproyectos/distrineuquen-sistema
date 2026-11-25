"""«Caja del día» (19.0.1.97.0): the cashier's screens, /caja.

On the Distrineuquén kit (controllers/kit.py, views/kit.xml) with its own
static/src/caja/caja.css and caja.js. Phone first, and the same pages on the
depot PC. Levels (back goes UP one level, dn.js):

    /tablero  (staff home)
      /caja                      Caja del día (or «Abrir la caja»)
        /caja/rendicion          what can be received: routes, other boxes
          /caja/rendicion/<id>   receive one settlement / box
        /caja/salida             Salida de caja (gasto, proveedor, retiro, banco)
        /caja/cierre             the bill count and the closing
        /caja/cerradas           19.0.1.105.0: «Cajas cerradas», his own
          /caja/cerradas/<id>    one session: movements, count, history,
                                 «Reabrir mi caja» (POST .../reabrir)
        /caja/historial          19.0.1.105.0: «Historial de cajas», every
                                 cashier (gerencia and the system admin)
        POST /caja/online/<id>/traer         19.0.1.117.0: «Traer a mi caja»,
                                 a cash payment registered in Odoo Online
        POST /caja/online/<id>/sacar         «Sacar de mi caja» (the movement)
      /caja/<id>/etiqueta        the closing label (PDF, new tab)

Every form posts plain HTML (works without JavaScript: the answer is a
redirect or the page again with the error). caja.js sends the same forms
with fetch() (X-Distri-Fetch: 1) and gets JSON: {ok, url} goes UP to the
parent with a message, {ok: false, error} shows it, {ok, ask, question}
asks first (an expense without paper) and sends again with the answer.
The rules live in models/caja.py; this file only translates HTTP. Every
route checks the «Cajero» group first and then works with sudo. 19.0.1.105.0:
the closed sessions are read by their own cashier and by gerencia (and the
system administrator); anybody else gets a 404, administración included
(it keeps the backend «Cajas», unchanged).
"""
import base64
from datetime import timedelta
from urllib.parse import urlencode

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.caja import (CHIPS, DIFFERENCE_MODES, OUTFLOW_CHIPS, OUTFLOW_KINDS, CajaQuestion,
                           DuplicateOutflow,
                           can_reopen, is_cashier)
from ..models.cliente_provisorio import can_confirm
from ..models.gasto import MAX_PHOTO_BYTES, decode_outflow_receipt
from ..models.reparto import _money
from ..models.reparto_cliente import today_ar
from .armado import DIAS, UTC
from .main import HORA_ARGENTINA
from .kit import kit_context
from .tablero import wants_tablero


def _hora(moment):
    """A UTC datetime of Odoo as the Argentine hour (HH:MM)."""
    if not moment:
        return ""
    return moment.replace(tzinfo=UTC).astimezone(HORA_ARGENTINA).strftime("%H:%M")


def _error_text(exc):
    return exc.args[0] if exc.args else str(exc)


HISTORY_DAYS = 60  # «Cajas cerradas» and «Historial de cajas» look this far back


def _dia(day, short=False):
    """«Martes 17/11» (or «Mar 17/11»)."""
    if not day:
        return ""
    name = DIAS[day.weekday()]
    return "%s %s" % (name[:3] if short else name, day.strftime("%d/%m"))


def _fecha_hora(moment):
    if not moment:
        return ""
    local = moment.replace(tzinfo=UTC).astimezone(HORA_ARGENTINA)
    return local.strftime("%d/%m %H:%M")


def _firmado(amount):
    """A difference with its sign: «− $ 500», «+ $ 200», «$ 0»."""
    if not round(amount or 0.0, 2):
        return "$ 0"
    return ("− " if amount < 0 else "+ ") + _money(abs(amount))


def _dif_class(amount):
    if not round(amount or 0.0, 2):
        return "ok"
    return "falta" if amount < 0 else "sobra"


class Caja(http.Controller):

    # ---------------------------------------------------------------- helpers

    def _check(self):
        user = request.env.user
        if not is_cashier(user):
            raise Forbidden()
        return user

    def _caja(self):
        return request.env["distri.caja"].current_for()

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _render(self, template, values, status=200):
        user = request.env.user
        today = today_ar()
        base = {
            "dn": kit_context("caja.css", "caja.js"),
            "user": user,
            "money": _money,
            "fecha": "%s %s/%s" % (DIAS[today.weekday()].lower(), today.day, today.month),
            "hoy": today,
            "hora": _hora,
            "inicio": "/tablero" if wants_tablero(user) else "",
            "csrf_token": request.csrf_token(),
            "error": "",
            "form": {},
            # 19.0.1.103.0: «Clientes nuevos a confirmar» (models/cliente_provisorio.py),
            # shown on receiving and closing; they never block the closing
            "provisorios": [p._distri_provisorio_row() for p in
                            request.env["res.partner"]._distri_provisorios_pendientes()],
            "puede_confirmar": can_confirm(request.env),
            # 19.0.1.105.0: closed sessions and their history
            "dia": _dia,
            "fecha_hora": _fecha_hora,
            "firmado": _firmado,
            "dif_class": _dif_class,
            "es_cajero": is_cashier(user),
            "es_gerencia": not user.share and can_reopen(user),
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _done(self, url, message=""):
        """Success: up to the parent `url`, with a message for its banner."""
        if message:
            url += ("&" if "?" in url else "?") + urlencode({"ok": message})
        if self._by_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url, 303)

    def _refuse(self, exc, page, status=400):
        """An error or a question: JSON for fetch(), else the page again."""
        text = _error_text(exc)
        if self._by_fetch():
            if isinstance(exc, CajaQuestion) and exc.field == "sin_papel_ok":
                return self._json({"ok": True, "ask": exc.field, "question": text})
            return self._json({"ok": False, "error": text,
                               "field": getattr(exc, "field", "")}, status=status)
        return page(text, getattr(exc, "field", ""))

    def _form(self):
        form = request.httprequest.form
        return {key: form.get(key) for key in form}

    def _movements_view(self, caja):
        return caja.movement_ids.sorted(lambda m: (m.moment, m.id), reverse=True)

    # ---------------------------------------------------------------- home

    @http.route("/caja", type="http", auth="user", methods=["GET"], sitemap=False)
    def home(self, ok="", cerrada=None, **kw):
        user = request.env.user
        if not is_cashier(user) and not user.share and can_reopen(user):
            # 19.0.1.105.0: gerencia without the «Cajero» group: the history
            return request.redirect("/caja/historial", 303)
        self._check()
        Caja = request.env["distri.caja"]
        caja = self._caja()
        if caja:
            caja._sync_own_collections()
        closed = Caja.browse()
        if cerrada:
            try:
                closed = Caja.sudo().browse(int(cerrada)).exists()
            except (TypeError, ValueError):
                closed = Caja.browse()
            if closed and (closed.user_id != request.env.user or closed.state != "closed"):
                closed = Caja.browse()
        return self._home(caja, ok=ok, closed=closed)

    def _home(self, caja, ok="", closed=None, error="", field="", form=None, status=200):
        Caja = request.env["distri.caja"]
        values = {"caja": caja, "ok": ok, "closed": closed, "error": error, "field": field,
                  "form": form or {}}
        if caja:
            routes, boxes = Caja.pending_receptions()
            values.update({
                "movimientos": self._movements_view(caja),
                "pendientes": len(routes) + len(boxes),
                # 19.0.1.117.0: «Cobros en efectivo cargados en Online»
                "online": Caja.pending_online_cash(),
            })
        else:
            previous = Caja.last_closed()
            today = Caja.sudo().search([("user_id", "=", request.env.user.id),
                                        ("date", "=", today_ar()),
                                        ("company_id", "=", request.env.company.id)], limit=1)
            values.update({"previous": previous, "today_closed": today,
                           "proposed": previous.left_for_tomorrow if previous else 0.0,
                           # 19.0.1.105.0: «Reabrir mi caja» on the latest one
                           "reabrible": previous if previous and previous.can_reopen_now()
                           else Caja.browse()})
        return self._render("distri_pedido.caja_inicio", values, status=status)

    @http.route("/caja/abrir", type="http", auth="user", methods=["POST"], sitemap=False)
    def open(self, fondo="", motivo="", **kw):
        self._check()
        try:
            request.env["distri.caja"].open_session(fondo, motivo)
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._home(
                self._caja(), error=text, field=field,
                form={"fondo": fondo, "motivo": motivo}, status=400))
        return self._done("/caja", "Caja abierta.")

    # ---------------------------------------------------------------- receive

    @http.route("/caja/rendicion", type="http", auth="user", methods=["GET"], sitemap=False)
    def receptions(self, ok="", **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        return self._receptions(caja, ok=ok)

    def _receptions(self, caja, ok="", error="", status=200):
        Caja = request.env["distri.caja"]
        routes, boxes = Caja.pending_receptions()
        return self._render("distri_pedido.caja_rendiciones", {
            "caja": caja, "routes": routes, "boxes": boxes, "ok": ok, "error": error,
            "shortages": Caja.pending_shortages(),
        }, status=status)

    def _route(self, route_id):
        route = request.env["distri.route"].sudo().browse(route_id).exists()
        if not route:
            raise NotFound()
        return route

    @http.route("/caja/rendicion/<int:route_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def reception(self, route_id, **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        return self._reception(caja, self._route(route_id))

    def _reception(self, caja, route, error="", field="", form=None, status=200):
        expenses = route.expense_ids.filtered(lambda e: e.state != "cancelled") \
            if route.kind == "route" else route.expense_ids.browse()
        promises = route.promise_ids.filtered(lambda p: p.state == "open")
        return self._render("distri_pedido.caja_rendicion", {
            "caja": caja, "route": route, "expenses": expenses,
            "returns": route.return_ids.filtered(lambda r: r.settlement_state == "pending"),
            "undelivered": route.undelivered_stop_ids.filtered(lambda s: not s.unloaded),
            "promises": promises, "modes": DIFFERENCE_MODES,
            "denominaciones": self._denominations(),
            "error": error, "field": field, "form": form or {},
            "can_receive": (route.kind == "route" and route.state == "closed") or (
                route.kind == "depot" and route.state != "settled"
                and route.driver_id != caja.user_id),
        }, status=status)

    @http.route("/caja/rendicion/<int:route_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def receive(self, route_id, **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        route = self._route(route_id)
        form = request.httprequest.form
        returns = {key.split("_", 1)[1]: form.get(key) for key in form
                   if key.startswith("devolucion_")}
        try:
            with request.env.cr.savepoint():
                movement = caja.receive_route(
                    route, form.get("entrego", ""), papers=form.getlist("papel"),
                    no_paper_ok=form.get("sin_papel_ok") == "1",
                    difference_mode=form.get("diferencia", ""),
                    unloaded=form.getlist("descargado"), returns=returns,
                    note=form.get("nota", ""))
        except (UserError, AccessError) as exc:
            values = self._form()
            values["papel"] = form.getlist("papel")
            values["descargado"] = form.getlist("descargado")
            return self._refuse(exc, lambda text, field: self._reception(
                caja, route, error=text, field=field, form=values, status=400))
        return self._done("/caja", "%s: entraron %s a la caja." % (
            movement.label, _money(movement.amount)))

    @http.route("/caja/rendicion/<int:route_id>/reabrir", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def reopen_route(self, route_id, **kw):
        """19.0.1.129.25 (#105): «Reabrir para el repartidor» from «Recibir
        rendición»: the route goes back to his phone. No reason asked; who
        and when are logged (models/reparto.py `_distri_reopen`)."""
        user = self._check()
        route = self._route(route_id)
        try:
            route._distri_reopen(user, via="caja")
        except UserError as exc:
            caja = self._caja()
            if not caja:
                return request.redirect("/caja", 303)
            return self._reception(caja, route, error=_error_text(exc), status=400)
        return self._done("/caja/rendicion", "%s: reabierta para el repartidor." % route.name)

    @http.route("/caja/cliente-nuevo/<int:partner_id>/confirmar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def confirm_new_customer(self, partner_id, volver="", **kw):
        """19.0.1.103.0: «Confirmar» a new customer from the list of /caja."""
        self._check()
        back = volver if volver in ("/caja/rendicion", "/caja/cierre") else "/caja"
        partner = request.env["res.partner"].sudo().browse(partner_id).exists()
        if not partner or not partner.distri_cliente_provisorio:
            raise NotFound()
        try:
            partner.with_env(request.env)._distri_check_confirm()
        except AccessError:
            raise Forbidden()
        partner.action_distri_provisorio_confirmar()
        return self._done(back, "%s quedó confirmado." % partner.name)

    @http.route("/caja/movimiento/<int:movement_id>/comprobante", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def movement_receipt(self, movement_id, **kw):
        """19.0.1.103.0: the photo or PDF of a «Salida de caja», for the
        cashier of that box (and gerencia)."""
        user = self._check_viewer()
        movement = request.env["distri.caja.movimiento"].sudo().browse(movement_id).exists()
        if not movement or not movement.has_receipt:
            raise NotFound()
        if movement.caja_id.user_id != user and not can_reopen(user):
            raise NotFound()
        if movement.document:
            name = (movement.document_name or "comprobante.pdf").replace('"', "")
            return request.make_response(base64.b64decode(movement.document), headers=[
                ("Content-Type", "application/pdf"),
                ("Content-Disposition", 'inline; filename="%s"' % name),
                ("X-Content-Type-Options", "nosniff"),
                ("Cache-Control", "private, max-age=3600")])
        raw = base64.b64decode(movement.photo)
        mime = "image/png" if raw.startswith(b"\x89PNG") else (
            "image/webp" if raw[:4] == b"RIFF" else "image/jpeg")
        return request.make_response(raw, headers=[
            ("Content-Type", mime), ("X-Content-Type-Options", "nosniff"),
            ("Cache-Control", "private, max-age=3600")])

    @http.route("/caja/gasto/<int:expense_id>/foto", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def expense_photo(self, expense_id, **kw):
        """The photo of a driver's expense, for the cashier (no ACL on it)."""
        self._check()
        expense = request.env["distri.route.expense"].sudo().browse(expense_id).exists()
        if not expense or not expense.photo:
            raise NotFound()
        return request.make_response(base64.b64decode(expense.photo), headers=[
            ("Content-Type", "image/jpeg"), ("Cache-Control", "private, max-age=3600")])

    @http.route("/caja/trajo/<int:movement_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def brought(self, movement_id, **kw):
        """«Lo trajo»: a driver's shortage that came back the next day."""
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        movement = request.env["distri.caja.movimiento"].sudo().browse(movement_id)
        try:
            with request.env.cr.savepoint():
                added = caja.bring_shortage(movement)
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._receptions(
                caja, error=text, status=400))
        return self._done("/caja", "%s: entraron %s." % (added.label, _money(added.amount)))

    # ---------------------------------------------------------------- Online cash
    # 19.0.1.117.0: a cash payment the cashier registered in Odoo Online
    # enters (or leaves) his open caja; models/caja.py has the rules.

    @http.route("/caja/online/<int:payment_id>/traer", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def take_online(self, payment_id, **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        payment = request.env["distri.online.payment"].sudo().browse(payment_id)
        try:
            with request.env.cr.savepoint():
                movement = caja.take_online(payment)
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._home(
                self._caja(), error=text, status=400))
        return self._done("/caja", "%s: entraron %s a la caja." % (
            movement.label, _money(movement.amount)))

    @http.route("/caja/online/<int:movement_id>/sacar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def release_online(self, movement_id, **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        movement = request.env["distri.caja.movimiento"].sudo().browse(movement_id)
        label, amount = (movement.label, movement.amount) if movement.exists() else ("", 0.0)
        try:
            with request.env.cr.savepoint():
                caja.release_online(movement)
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._home(
                self._caja(), error=text, status=400))
        return self._done("/caja", "%s: salió de la caja (%s)." % (label, _money(amount)))

    # ---------------------------------------------------------------- outflow

    @http.route("/caja/salida", type="http", auth="user", methods=["GET"], sitemap=False)
    def outflow_page(self, tipo="", **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        return self._outflow(caja, form={"tipo": tipo if tipo in CHIPS else ""})

    def _outflow(self, caja, error="", form=None, status=200):
        suppliers = request.env["res.partner"].sudo().search(
            [("supplier_rank", ">", 0), ("parent_id", "=", False)], order="name", limit=300)
        return self._render("distri_pedido.caja_salida", {
            "caja": caja, "chips": OUTFLOW_CHIPS, "suppliers": suppliers,
            "error": error, "form": form or {},
        }, status=status)

    @http.route("/caja/salida", type="http", auth="user", methods=["POST"], sitemap=False)
    def outflow(self, tipo="", importe="", quien="", nota="", **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        try:
            receipt = {}
            upload = request.httprequest.files.get("foto")
            if upload and upload.filename:
                # 19.0.1.103.0: a photo or a PDF
                receipt = decode_outflow_receipt(upload.read(MAX_PHOTO_BYTES + 1),
                                                 upload.filename)
            partner = request.env["res.partner"].sudo().search(
                [("supplier_rank", ">", 0), ("name", "=ilike", (quien or "").strip())],
                limit=1) if quien else request.env["res.partner"]
            with request.env.cr.savepoint():
                movement = caja.add_outflow(tipo, importe, who=quien, partner_id=partner.id,
                                            note=nota, **receipt)
        except DuplicateOutflow as exc:
            # the second «Guardar» of the same outflow: the first one counts
            return self._done("/caja", str(exc.args[0]))
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._outflow(
                caja, error=text, form=self._form(), status=400))
        return self._done("/caja", "%s: salieron %s." % (movement.label, _money(-movement.amount)))

    # ---------------------------------------------------------------- closing

    def _denominations(self):
        return request.env["distri.caja.denominacion"].sudo().search([], order="value desc")

    @http.route("/caja/cierre", type="http", auth="user", methods=["GET"], sitemap=False)
    def closing_page(self, **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        caja._sync_own_collections()
        return self._closing(caja)

    def _closing(self, caja, error="", field="", form=None, status=200):
        Caja = request.env["distri.caja"]
        routes, boxes = Caja.pending_receptions()
        return self._render("distri_pedido.caja_cierre", {
            "caja": caja, "denominaciones": self._denominations(), "error": error,
            "field": field, "form": form or {},
            # 19.0.1.109.0: the depot PC sees the day and his last closings
            # next to the count (views/caja.xml, .caja-pc-only)
            "movimientos": self._movements_view(caja)[:6],
            "pendientes": len(routes) + len(boxes),
            "cerradas": Caja.sudo().search([
                ("user_id", "=", request.env.user.id), ("state", "=", "closed"),
                ("company_id", "=", request.env.company.id), ("date", ">=", self._since())],
                order="date desc, id desc", limit=3),
            "dias": HISTORY_DAYS,
        }, status=status)

    @http.route("/caja/cierre", type="http", auth="user", methods=["POST"], sitemap=False)
    def close(self, otros="", queda="", motivo="", esperado="", **kw):
        self._check()
        caja = self._caja()
        if not caja:
            return request.redirect("/caja", 303)
        form = request.httprequest.form
        quantities = {key[5:]: form.get(key) for key in form if key.startswith("cant_")}
        try:
            with request.env.cr.savepoint():
                closed = caja.close_session(quantities, other=otros, left=queda, reason=motivo,
                                            expected_seen=esperado)
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._closing(
                caja, error=text, field=field, form=self._form(), status=400))
        return self._done("/caja?cerrada=%s" % closed.id, "Caja cerrada.")

    @http.route("/caja/<int:caja_id>/etiqueta", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def label(self, caja_id, **kw):
        # 19.0.1.105.0: its cashier or gerencia (before: any administración
        # user with the «Cajero» group); also «Reimprimir etiqueta»
        caja = self._viewable(caja_id)
        if caja.state != "closed":
            raise NotFound()
        pdf, _type = request.env["ir.actions.report"].sudo()._render_qweb_pdf(
            "distri_pedido.report_caja_cierre", caja.ids)
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Disposition", 'inline; filename="Cierre de caja %s.pdf"'
             % caja.date.strftime("%d-%m-%Y")),
            ("Cache-Control", "no-store"),
        ])

    # ---------------------------------------------------------------- closed sessions
    # 19.0.1.105.0: «Cajas cerradas» (the cashier), «Historial de cajas»
    # (gerencia), one session read only, and «Reabrir mi caja».

    def _check_viewer(self):
        """A cashier (his own sessions) or gerencia / the system
        administrator (every cashier's)."""
        user = request.env.user
        if user.share or not (is_cashier(user) or can_reopen(user)):
            raise Forbidden()
        return user

    def _viewable(self, caja_id):
        """The session when the user may read it; a 404 otherwise (another
        cashier's session does not exist for him)."""
        user = self._check_viewer()
        caja = request.env["distri.caja"].sudo().browse(caja_id).exists()
        if not caja or caja.company_id != request.env.company:
            raise NotFound()
        if caja.user_id != user and not can_reopen(user):
            raise NotFound()
        return caja

    def _since(self):
        return today_ar() - timedelta(days=HISTORY_DAYS)

    @http.route("/caja/cerradas", type="http", auth="user", methods=["GET"], sitemap=False)
    def closed_list(self, ok="", **kw):
        user = request.env.user
        if not is_cashier(user):
            if not user.share and can_reopen(user):
                return request.redirect("/caja/historial", 303)
            raise Forbidden()
        Caja = request.env["distri.caja"]
        cajas = Caja.sudo().search([
            ("user_id", "=", user.id), ("state", "=", "closed"),
            ("company_id", "=", request.env.company.id), ("date", ">=", self._since())],
            order="date desc, id desc")
        latest = Caja.last_closed()
        reopenable = latest if latest and latest.can_reopen_now() else Caja.browse()
        return self._render("distri_pedido.caja_cerradas", {
            "cajas": cajas, "reabrible": reopenable, "ok": ok,
            "bloqueo": latest.reopen_block() if latest and not reopenable else "",
            "dias": HISTORY_DAYS,
        })

    def _closed_detail(self, caja, ok="", error="", field="", form=None, status=200):
        user = request.env.user
        moves = caja.movement_ids.sorted(lambda m: (m.moment, m.id))
        grupos = [
            ("Cobros en el local", moves.filtered(lambda m: m.kind == "cobro")),
            # 19.0.1.118.0
            ("Cobros registrados en la factura", moves.filtered(lambda m: m.kind == "factura")),
            # 19.0.1.117.0
            ("Cobros en efectivo cargados en Online", moves.filtered(lambda m: m.kind == "online")),
            ("Rendiciones y cobros de otros usuarios",
             moves.filtered(lambda m: m.kind in ("rendicion", "recepcion"))),
            ("Salidas de caja", moves.filtered(lambda m: m.kind in OUTFLOW_KINDS)),
            ("Otros movimientos de efectivo", moves.filtered(lambda m: m.kind == "ajuste")),
        ]
        own = caja.user_id == user
        states = dict(request.env["distri.route.case"]._fields["state"].selection)
        backend = user.has_group("distri_pedido.grupo_administracion") or can_reopen(user)
        return self._render("distri_pedido.caja_cerrada", {
            "caja": caja, "grupos": grupos, "own": own,
            "volver": "/caja/cerradas" if own and is_cashier(user) else "/caja/historial",
            "puede_reabrir": caja.can_reopen_now(),
            "bloqueo": caja.reopen_block() if caja.state == "closed" else "",
            "caso_estado": lambda case: states.get(case.state, ""),
            "caso_url": (lambda case: "/odoo/distri.route.case/%s" % case.id) if backend
            else (lambda case: ""),
            "ok": ok, "error": error, "field": field, "form": form or {},
        }, status=status)

    @http.route("/caja/cerradas/<int:caja_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def closed_detail(self, caja_id, ok="", **kw):
        return self._closed_detail(self._viewable(caja_id), ok=ok)

    @http.route("/caja/cerradas/<int:caja_id>/reabrir", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def reopen(self, caja_id, motivo="", **kw):
        """«Reabrir mi caja» (or gerencia, any cashier's): the model checks
        who and which one (models/caja.py, action_reopen)."""
        caja = self._viewable(caja_id)
        try:
            with request.env.cr.savepoint():
                request.env["distri.caja"].browse(caja.id).action_reopen(motivo)
        except (UserError, AccessError) as exc:
            return self._refuse(exc, lambda text, field: self._closed_detail(
                caja, error=text, field=field, form={"motivo": motivo}, status=400))
        if caja.user_id == request.env.user:
            return self._done("/caja", "Reabriste la caja del %s." % caja.date.strftime("%d/%m"))
        return self._done("/caja/historial", "Caja de %s del %s reabierta." % (
            caja.user_id.name, caja.date.strftime("%d/%m")))

    @http.route("/caja/historial", type="http", auth="user", methods=["GET"], sitemap=False)
    def history(self, ver="", cajero="", ok="", **kw):
        """«Historial de cajas»: every cashier's sessions of the last
        HISTORY_DAYS days (and any still open), for gerencia."""
        user = request.env.user
        if user.share or not can_reopen(user):
            raise Forbidden()
        Caja = request.env["distri.caja"].sudo()
        cajas = Caja.search([("company_id", "=", request.env.company.id),
                             "|", ("date", ">=", self._since()), ("state", "=", "open")],
                            order="date desc, id desc")
        cashiers = cajas.user_id.sorted(lambda u: (u.name or "").lower())
        try:
            selected = cashiers.filtered(lambda u: u.id == int(cajero or 0))
        except (TypeError, ValueError):
            selected = cashiers.browse()
        scoped = cajas.filtered(lambda c: c.user_id == selected) if selected else cajas
        currency = request.env.company.currency_id
        closed = scoped.filtered(lambda c: c.state == "closed")
        filtros = [
            ("", "Todas", scoped),
            ("dif", "Con diferencia", closed.filtered(lambda c: not currency.is_zero(c.difference))),
            ("reabiertas", "Reabiertas", scoped.filtered(lambda c: c.reopen_count)),
            ("abiertas", "Abiertas", scoped.filtered(lambda c: c.state == "open")),
        ]
        ver = ver if ver in dict((k, 1) for k, _l, _c in filtros) else ""
        shown = dict((k, c) for k, _l, c in filtros)[ver]

        def url(v=None, c=None):
            query = {"ver": ver if v is None else v,
                     "cajero": (selected.id or "") if c is None else c}
            query = {k: x for k, x in query.items() if x}
            return "/caja/historial" + ("?" + urlencode(query) if query else "")

        return self._render("distri_pedido.caja_historial", {
            "cajas": shown, "filtros": filtros, "ver": ver, "cajeros": cashiers,
            "cajero": selected, "url": url, "ok": ok, "dias": HISTORY_DAYS,
            "total_dif": sum(closed.mapped("difference")), "cantidad": len(scoped),
            "volver": "/caja" if is_cashier(user) else (
                "/tablero" if wants_tablero(user) else ""),
        })
