"""19.0.1.129.35 (plan unificado, Etapa 4): the two actions of the order
screen (/pedidos/<id>, controllers/fichas.py). Behind distri_pedido.
nueva_venta and, since they live on that kit screen, distri_pedido.ui_v2:
with either off every route answers 404.

    /pedidos/<id>/aprobar   POST  «Aprobar» a quotation: the backend's
                                  «Confirmar» as the user (his write access
                                  on the order), with the WhatsApp question
                                  of the backend wizard when it would open
                                  (models/nueva_venta.py distri_kit_aprobar)
    /pedidos/<id>/anular    GET   «Anular venta»: what will happen (the plan,
                                  models/anular_venta.py) or why it cannot
                            POST  the reason and the plan seen: run it

Only «Anular venta» writes accounting, and only by Odoo's standard reversal
(a credit note posted and reconciled with each invoice): no posted move is
edited or deleted and nothing is unreconciled.
"""
from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models.anular_venta import puede_anular
from ..models.nueva_venta import nueva_venta_on
from ..models.reparto import _money
from .fichas import Fichas, ficha_url, may_use
from .kit import ui_v2_on


def _error_text(exc):
    return exc.args[0] if exc.args else str(exc)


class VentaAcciones(http.Controller):

    def _check(self):
        env = request.env
        if not (ui_v2_on(env) and nueva_venta_on(env)):
            raise NotFound()
        if not may_use(env.user):
            raise Forbidden()
        return env.user

    def _order(self, order_id):
        """The order in the user's environment, when he may read it (404
        otherwise, as the order screen)."""
        return Fichas()._get("sale.order", order_id)

    # ------------------------------------------------------------ «Aprobar»

    @http.route("/pedidos/<int:order_id>/aprobar", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def aprobar(self, order_id, area="", numero="", sin_whatsapp="", **kw):
        self._check()
        order = self._order(order_id)
        try:
            order.check_access("write")
        except AccessError:
            raise Forbidden() from None
        try:
            with request.env.cr.savepoint():
                order.distri_kit_aprobar(area=(area or "").strip()[:8],
                                         numero=(numero or "").strip()[:16],
                                         sin_whatsapp=sin_whatsapp == "1")
        except (UserError, AccessError, ValidationError) as exc:
            return Fichas()._pedido_page(order.id, error=_error_text(exc), status=400)
        return request.redirect("/pedidos/%s?hecho=aprobado" % order.id, 303)

    # ------------------------------------------------------------ «Anular venta»

    def _anular_page(self, order, motivo="", error="", status=200):
        o = order.sudo()
        plan = o._distri_anular_plan()
        here = "/pedidos/%s" % o.id
        values = {
            "o": o,
            "cliente": o.partner_id.commercial_partner_id.name or o.partner_id.name or "",
            "total": _money(o.amount_total),
            "refusals": plan["refusals"],
            "steps": plan["steps"],
            "huella": plan["huella"],
            "action": "/pedidos/%s/anular" % o.id,
            "motivo": motivo,
            "error": error,
            # one level under the order screen
            "volver": here,
            "parent": here,
        }
        return Fichas()._render("distri_pedido.ficha_anular", "pedido", o, "", values,
                                status=status)

    @http.route("/pedidos/<int:order_id>/anular", type="http", auth="user",
                methods=["GET", "POST"], sitemap=False)
    def anular(self, order_id, motivo="", huella="", **kw):
        user = self._check()
        if not puede_anular(user):
            raise Forbidden()
        order = self._order(order_id)
        if request.httprequest.method != "POST":
            return self._anular_page(order)
        try:
            with request.env.cr.savepoint():
                order.distri_anular_venta(motivo, huella=huella)
        except (UserError, AccessError, ValidationError) as exc:
            return self._anular_page(order, motivo=(motivo or "")[:250],
                                     error=_error_text(exc), status=400)
        return request.redirect(ficha_url("pedido", order.id) + "?hecho=anulada", 303)
