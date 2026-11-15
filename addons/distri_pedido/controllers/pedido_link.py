"""/p/<token>: the order's short link in the customer's WhatsApp message
(models/pedido_link.py, 19.0.1.87.0).

Administración, gerencia and system administrators go to the order form
in «Pedidos para revisar». Anyone else (the customer, a forwarded chat)
sees only the order name and its status in plain words: no prices, no
customer data. Not logged in, a small link logs the team in and comes back.

The token is 24 random letters and digits (~143 bits): guessing is out of
reach, so there is no rate limit; the format is checked before the lookup
and the stored value is compared in constant time.
"""
from urllib.parse import quote

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

PRIVATE_HEADERS = [
    ("Cache-Control", "no-store"),
    ("X-Robots-Tag", "noindex, nofollow"),
    ("Referrer-Policy", "no-referrer"),
]


class PedidoLink(http.Controller):

    @http.route("/p/<string:token>", type="http", auth="public", website=True, sitemap=False)
    def pedido_link(self, token, **kw):
        Order = request.env["sale.order"]
        order = Order.sudo()._distri_link_find(token)
        if not order:
            raise NotFound()
        user = request.env.user
        if Order._distri_link_is_team(user):
            return request.redirect(order._distri_link_backend_url(), local=True)
        values = {
            "nombre": order.name,
            "estado": order._distri_link_status(),
            "empresa_id": order.company_id.id or request.env.company.id,
            "login_url": ("/web/login?redirect=%s" % quote("/p/%s" % token, safe="")
                          if user._is_public() else ""),
        }
        response = request.render("distri_pedido.pedido_link", values)
        response.headers.extend(PRIVATE_HEADERS)
        return response
