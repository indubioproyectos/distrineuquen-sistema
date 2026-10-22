"""La numeración de facturas sigue la serie de Odoo Online.

Online factura en 0001/AAAA/NNNNN y el lab continúa esa serie (SWAP.md,
"Numeración"). Odoo arma el número mirando la última factura del diario: sin
ninguna, empezaría en 0001/2026/00001. El parámetro
distri_pedido.factura_ultimo_numero_online guarda el último número de Online y
es un piso: la próxima factura o nota de crédito del diario de ventas nunca
sale por debajo de ese número + 1. El día que el lab reemplace a Online se
pone ahí el último número de Online.
"""
from odoo import models

PARAMETRO = "distri_pedido.factura_ultimo_numero_online"


class AccountMoveNumeracion(models.Model):
    _inherit = "account.move"

    def _get_next_sequence_format(self):
        formato, valores = super()._get_next_sequence_format()
        if self.journal_id.type != "sale" or self.move_type not in ("out_invoice", "out_refund"):
            return formato, valores
        ultimo = (self.env["ir.config_parameter"].sudo().get_param(PARAMETRO) or "").strip()
        if not ultimo:
            return formato, valores
        formato_online, valores_online = self._get_sequence_format_param(ultimo)
        piso = valores_online.get("seq", 0)
        # solo si es la misma serie (mismo prefijo y mismo año): 0001/2026/00000
        misma_serie = formato_online.format(**dict(valores_online, seq=0)) == \
            formato.format(**dict(valores, seq=0))
        if misma_serie and valores.get("seq", 0) < piso:
            valores["seq"] = piso
        return formato, valores
