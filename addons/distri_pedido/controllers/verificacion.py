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
        /verificacion/reclamar         POST  19.0.1.129.38: «Reclamar por
                                             WhatsApp» of an income of queue
                                             7 (a note, then wa.me)
        /verificacion/subidos          GET   19.0.1.129.38: «Comprobantes
                                             subidos» by who sent them and
                                             state (administración, Ale)

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
UPLOADS_PAGE = "distri_pedido.verificacion_subidos"
# who sees «Comprobantes subidos»: the members of «Para acreditar»
UPLOADS_QUEUE = "para_acreditar"


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
        Board = request.env["distri.verificacion"]
        # 19.0.1.129.38: «sin cliente» (#48) next to queue 7, «Comprobantes subidos»
        sin_cliente = Board._distri_v2_unidentified_count() \
            if vf.queue_member(user, "sin_comprobante") else 0
        # 19.0.1.129.37: «Subir comprobantes», several at once
        return self._render(PAGE, {"colas": colas, "subir": can_upload(user),
                                   "sin_cliente": sin_cliente,
                                   "subidos": vf.queue_member(user, UPLOADS_QUEUE)})

    @http.route("/verificacion/subidos", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def subidos(self, dias="", quien="", estado="", **kw):
        """19.0.1.129.38: «Comprobantes subidos» (#10 and #66)."""
        user = self._check(UPLOADS_QUEUE)
        from ..models.verificacion_tramo2 import UPLOAD_DAYS
        days = int(dias) if str(dias).isdigit() else UPLOAD_DAYS
        data = request.env["distri.verificacion"]._distri_v2_uploads(
            days=days, who=quien[:120], bucket=estado[:20])
        return self._render(UPLOADS_PAGE, {"data": data, "user": user})

    @http.route("/verificacion/<string:cola>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def cola(self, cola, ok="", error="", filtro="", **kw):
        user = self._check(cola)
        from ..models.verificacion_tramo2 import FILTER_OTHER, FILTER_UNIDENTIFIED
        Board = request.env["distri.verificacion"]
        filtro = filtro if cola == "sin_comprobante" and filtro in (
            FILTER_OTHER, FILTER_UNIDENTIFIED) else ""
        return self._render(QUEUE_PAGE, {
            "cola": Board._summary(cola, user),
            "filas": Board._rows(cola, user, filtro=filtro),
            "filtro": filtro,
            "sin_cliente": Board._distri_v2_unidentified_count()
            if cola == "sin_comprobante" else 0,
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

    @http.route("/verificacion/reclamar", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def reclamar(self, card_id="", **kw):
        """19.0.1.129.38: «Reclamar por WhatsApp» of an income of queue 7: a
        note on the card, then the customer's chat with the claim written."""
        self._check("sin_comprobante")
        card = request.env["distri.mp.collection"].sudo().browse(
            int(card_id) if str(card_id).isdigit() else 0).exists()
        if not card or not card._distri_v2_in_no_receipt_queue():
            return self._back("sin_comprobante", error="Ese ingreso ya no está en la cola.")
        url = card.with_user(request.env.user)._distri_v2_claim()
        if not url:
            return self._back("sin_comprobante", error="El cliente no tiene WhatsApp cargado.")
        return request.redirect(url, 303, local=False)
