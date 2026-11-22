"""/p/<token>: the order's only link in the customer's WhatsApp message
(models/pedido_link.py, 19.0.1.87.0; one link since 19.0.1.129.3).

Administración, gerencia and system administrators go to the order form
in «Pedidos para revisar». Anyone else (the customer, a forwarded chat, a
logged-out team member) is sent to the customer's own order page,
/pedido/<access_token>, where the order is seen and modified and the
account shown when it is visible to the customer.

The token is 24 random letters and digits (~143 bits): guessing is out of
reach, so there is no rate limit; the format is checked before the lookup
and the stored value is compared in constant time.
"""
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
        if Order._distri_link_is_team(request.env.user):
            target = order._distri_link_backend_url()
        else:
            target = order._distri_link_customer_url()
        response = request.redirect(target, local=True)
        response.headers.extend(PRIVATE_HEADERS)
        return response
