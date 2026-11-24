"""«Mis ventas» on the phone (19.0.1.129.18, pendiente 78): /mis-ventas.

    /tablero (staff home, tile «Mis ventas»; also in «Ajustes» for a seller
    locked to /cuentas)
      /mis-ventas?q=&periodo=&estado=&vendedor=   GET, read only

Who and what is decided in models/mis_ventas.py (own orders, Gerencia all
of them): this file only translates HTTP. A portal or public user gets 403
(public: the login first). The search box filters the loaded cards as you
type (static/src/mis_ventas/mis_ventas.js) and the server honours ?q= too
(a shared link, no JavaScript).
"""
from urllib.parse import urlencode

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.http import request

from ..models import mis_ventas
from .kit import kit_context
from .tablero import wants_tablero


class MisVentas(http.Controller):

    @http.route("/mis-ventas", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, q="", periodo=mis_ventas.DEFAULT_PERIODO, estado=mis_ventas.DEFAULT_ESTADO,
             vendedor="", **kw):
        env = request.env
        user = env.user
        if not mis_ventas.puede_ver_mis_ventas(user):
            raise Forbidden()
        data = mis_ventas.buscar(env, q=q, periodo=periodo, estado=estado, vendedor=vendedor)
        current = {"q": data["q"], "periodo": data["periodo"], "estado": data["estado"],
                   "vendedor": data["vendedor_id"] or ""}

        def url(**changes):
            """/mis-ventas with the current filters and `changes`."""
            params = {k: v for k, v in dict(current, **changes).items() if v}
            return "/mis-ventas" + ("?" + urlencode(params) if params else "")

        html = env["ir.qweb"]._render("distri_pedido.mis_ventas_page", dict(
            data,
            dn=kit_context("mis_ventas.css", "mis_ventas.js"),
            user=user,
            periodos=mis_ventas.PERIODOS,
            estados=mis_ventas.ESTADOS,
            vendedores=mis_ventas.vendedores(env) if data["gerencia"] else [],
            url=url,
            inicio="/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
        ))
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
