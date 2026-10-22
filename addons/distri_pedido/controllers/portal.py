"""El portal de Odoo no le muestra facturas al cliente.

Decisión de ale (26/09/2026): el cliente ve el remito, nunca la factura
(models/remito.py). Odoo trae "Mis facturas" en el portal (/my/invoices) y
el bloque "Últimas facturas" en la página del pedido: el comercio que entra
con su usuario va a su cuenta corriente, que muestra los remitos. El
personal interno no pasa por acá: sigue viendo las facturas en el backend.
La regla de registro "Clientes: sin facturas" (security/reglas.xml) cierra
el resto (PDF por /report, búsquedas).
"""
from odoo import http
from odoo.http import request

from odoo.addons.account.controllers.portal import PortalAccount


def _es_cliente():
    usuario = request.env.user
    return usuario._is_public() or usuario.share


class PortalSinFacturas(PortalAccount):

    def _prepare_home_portal_values(self, counters):
        valores = super()._prepare_home_portal_values(counters)
        if _es_cliente():
            for clave in ("invoice_count", "overdue_invoice_count"):
                if clave in valores:
                    valores[clave] = 0
        return valores

    @http.route()
    def portal_my_invoices(self, *args, **kw):
        if _es_cliente():
            return request.redirect("/mi-cuenta")
        return super().portal_my_invoices(*args, **kw)

    @http.route()
    def portal_my_invoice_detail(self, invoice_id, *args, **kw):
        if _es_cliente():
            return request.redirect("/mi-cuenta")
        return super().portal_my_invoice_detail(invoice_id, *args, **kw)
