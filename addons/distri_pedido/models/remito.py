"""El remito: el único comprobante que ve el cliente.

Decisión de ale (26/09/2026): el cliente nunca ve la factura. La factura
("Factura C" interna, diario Ventas Preimpreso 0001) es un registro contable
para administración. Lo que el cliente ve en su cuenta corriente, en la
página del pedido y en el portal es el remito.

En este proyecto el remito es la entrega del depósito (stock.picking de
salida, R-NNNNN): se valida sola al terminar el armado
(factura_armado._distri_entregar_lo_armado), con lo que se armó, y es el
papel que viaja con la mercadería ("Albarán de entrega" / "Remito" en el
menú Imprimir de la entrega). Si algún día se habilita el remito de
l10n_ar_stock (tipo de documento R con CAI), se muestra ese número.

La historia importada del Odoo anterior (distri.cuenta.movimiento) solo trae
números de factura: esos movimientos se muestran como "Remito" sin número.

Numeración (ale, 26/09/2026): el remito sale como R-00047 (prefijo "R-",
cinco dígitos) en lugar de WH/OUT/00047. numerar_remitos() cambia la
secuencia del tipo de operación de salida y renombra las entregas viejas; la
llama la migración 19.0.1.3.0. El contador sigue donde estaba.
"""
import re

from odoo import models

PREFIJO_REMITO = "R-"
# WH/OUT/00042: el nombre que Odoo le daba a la entrega antes del prefijo R-
NOMBRE_VIEJO = re.compile(r"^[^/]+/OUT/(\d+)$")

# 0001/2026/06275: el número de factura o nota de crédito de Odoo Online
NUMERO_DE_FACTURA = re.compile(r"^\s*\d{4}/\d{4}/\d+\s*$")


def es_numero_de_factura(texto):
    return bool(NUMERO_DE_FACTURA.match(texto or ""))


def numero_de_remito(entrega):
    """El número que va impreso en el remito."""
    guia = entrega._fields.get("l10n_ar_delivery_guide_number") and \
        entrega.l10n_ar_delivery_guide_number
    return guia or entrega.name


def numerar_remitos(env):
    """Entregas al cliente como R-NNNNN: la secuencia y las entregas viejas.

    Devuelve cuántas entregas se renombraron. Se puede correr más de una vez.
    """
    tipos = env["stock.picking.type"].sudo().with_context(active_test=False).search(
        [("code", "=", "outgoing")])
    for secuencia in tipos.sequence_id:
        if secuencia.prefix != PREFIJO_REMITO:
            secuencia.prefix = PREFIJO_REMITO
    renombradas = 0
    entregas = env["stock.picking"].sudo().search(
        [("picking_type_id", "in", tipos.ids), ("name", "like", "/OUT/")])
    for entrega in entregas:
        viejo = NOMBRE_VIEJO.match(entrega.name)
        if viejo:
            entrega.name = PREFIJO_REMITO + viejo.group(1)
            renombradas += 1
    return renombradas


class SaleOrderRemito(models.Model):
    _inherit = "sale.order"

    def _distri_remitos(self):
        """Las entregas validadas al cliente, en orden."""
        return self.sudo().picking_ids.filtered(
            lambda p: p.picking_type_code == "outgoing" and p.state == "done"
        ).sorted("id")

    def _distri_remitos_texto(self):
        return ", ".join(numero_de_remito(p) for p in self._distri_remitos())


class AccountMoveRemito(models.Model):
    _inherit = "account.move"

    def _distri_pedidos(self):
        """Los pedidos detrás de una factura, o de la factura que corrige una NC."""
        self.ensure_one()
        asiento = self.sudo()
        if asiento.move_type == "out_refund" and asiento.reversed_entry_id:
            asiento = asiento.reversed_entry_id
        return asiento.line_ids.sale_line_ids.order_id

    def _distri_remito_texto(self):
        """Lo que ve el cliente en lugar del número de factura.

        El remito de la entrega; si el pedido no pasó por el depósito, el
        número de pedido, que el cliente ya conoce. Nunca el de la factura.
        """
        self.ensure_one()
        pedidos = self._distri_pedidos()
        remitos = pedidos._distri_remitos_texto()
        if remitos:
            return remitos
        if pedidos:
            return "Pedido %s" % ", ".join(pedidos.mapped("name"))
        return ""
