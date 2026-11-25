"""«Tablero de verificación» on the kit (19.0.1.129.31; models/verificacion.py
has the rules). Behind distri_pedido.verificacion_v2, OFF by default: with
it off every route answers 404 and Inicio has no tile.

    /tablero
      /verificacion                    GET   the queues of the user: count,
                                             overdue, oldest, owner and term
        /verificacion/<cola>           GET   the oldest items of one queue
        /verificacion/revisado         POST  «Revisado» of an auto-credited
                                             receipt (Denis, Gonzalo, Ale)
        /verificacion/duplicado        POST  «Es el mismo pago» / «Son dos
                                             pagos» of a held twin

Each item opens its card (or, for an income the board hides from the user,
the customer) in the backend, where today's buttons do the work.
"""
from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models import verificacion as vf
from .kit import kit_context, staff_nav
from .tablero import wants_tablero

PAGE = "distri_pedido.verificacion_page"
QUEUE_PAGE = "distri_pedido.verificacion_cola"


class Verificacion(http.Controller):

    def _check(self, key=None):
        env = request.env
        if not vf.verificacion_on(env):
            raise NotFound()
        user = env.user
        if key is not None:
            if key not in vf.QUEUE_KEYS:
                raise NotFound()
            if not vf.queue_member(user, key):
                raise Forbidden()
        elif not vf.user_queues(user):
            raise Forbidden()
        return user

    def _render(self, template, values, status=200):
        user = request.env.user
        base = {
            "dn": kit_context(),
            "user": user,
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            "csrf_token": request.csrf_token(),
            "nav": staff_nav(request.env, "verificacion"),
            "error": "",
            "ok": "",
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    @http.route("/verificacion", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, **kw):
        user = self._check()
        colas = request.env["distri.verificacion"]._summaries(user)
        from ..models.comprobante_subir import can_upload
        # 19.0.1.129.37: «Subir comprobantes», several at once
        return self._render(PAGE, {"colas": colas, "subir": can_upload(user)})

    @http.route("/verificacion/<string:cola>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def cola(self, cola, ok="", error="", **kw):
        user = self._check(cola)
        Board = request.env["distri.verificacion"]
        return self._render(QUEUE_PAGE, {
            "cola": Board._summary(cola, user),
            "filas": Board._rows(cola, user),
            "ok": ok[:200], "error": error[:200],
        })

    def _back(self, cola, ok="", error=""):
        from urllib.parse import urlencode
        query = urlencode({k: v for k, v in (("ok", ok), ("error", error)) if v})
        return request.redirect("/verificacion/%s%s" % (cola, "?" + query if query else ""), 303)

    @http.route("/verificacion/revisado", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def revisado(self, card_id="", **kw):
        self._check("revision_humana")
        card = request.env["distri.mp.collection"].browse(
            int(card_id) if str(card_id).isdigit() else 0).exists()
        if not card:
            return self._back("revision_humana", error="No encontré ese comprobante.")
        try:
            card.action_distri_human_check()
        except (AccessError, UserError) as exc:
            return self._back("revision_humana", error=exc.args[0] if exc.args else str(exc))
        return self._back("revision_humana", ok="Listo: %s quedó revisado." % card.sudo().name)

    @http.route("/verificacion/duplicado", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def duplicado(self, msg_id="", decision="", **kw):
        self._check("duplicados")
        msg = request.env["distri.whatsapp.message"].sudo().browse(
            int(msg_id) if str(msg_id).isdigit() else 0).exists()
        if not msg or decision not in ("mismo", "dos"):
            return self._back("duplicados", error="No encontré ese comprobante.")
        mine = msg.with_user(request.env.user)
        try:
            if decision == "mismo":
                mine.action_distri_twin_same()
                ok = "Listo: quedó como el mismo pago («Ya recibido»)."
            else:
                mine.action_distri_twin_two()
                ok = "Listo: quedan como dos pagos distintos."
        except (AccessError, UserError) as exc:
            return self._back("duplicados", error=exc.args[0] if exc.args else str(exc))
        return self._back("duplicados", ok=ok)
