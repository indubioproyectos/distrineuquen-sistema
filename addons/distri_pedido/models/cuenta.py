import re

from odoo import api, fields, models


def clave_documento(numero, debe, haber):
    """Identidad de un documento para no contarlo dos veces.

    Un documento puede estar en la historia importada y tambien vivo en Odoo
    (por ejemplo, una factura abierta que se migro para cobrarla): ahi cuenta
    una sola vez y gana el de Odoo. Se compara el numero normalizado MAS el
    importe al debe y al haber: los numeros de Odoo (PBNK1/2026/00001, ...)
    se repiten con los de Odoo Online en otros documentos, y un numero igual
    con otro importe es otro documento que no se puede descartar.
    Sin numero no hay clave: nunca se descarta.
    """
    normal = re.sub(r"[^0-9a-z]", "", (numero or "").lower())
    if not normal:
        return None
    return (normal, round(debe or 0.0, 2), round(haber or 0.0, 2))



class CuentaMovimiento(models.Model):
    _name = "distri.cuenta.movimiento"
    _description = "Movimiento de cuenta corriente"
    _order = "fecha desc, id desc"
    _rec_name = "comprobante"

    partner_id = fields.Many2one(
        "res.partner", string="Cliente", required=True,
        index=True, ondelete="cascade",
    )
    fecha = fields.Date(string="Fecha", required=True, index=True)
    comprobante = fields.Char(string="Comprobante")
    concepto = fields.Char(string="Concepto")
    tipo = fields.Selection(
        [("factura", "Factura"), ("pago", "Pago"), ("nota", "Nota de crédito"), ("otro", "Otro")],
        string="Tipo", default="otro", index=True,
    )
    debe = fields.Monetary(string="Debe", currency_field="currency_id")
    haber = fields.Monetary(string="Haber", currency_field="currency_id")
    currency_id = fields.Many2one(
        "res.currency", string="Moneda",
        default=lambda self: self.env.company.currency_id,
    )
    origen_id = fields.Integer(string="Id en el sistema anterior", index=True)


class ResPartnerCuenta(models.Model):
    _inherit = "res.partner"

    movimiento_ids = fields.One2many(
        "distri.cuenta.movimiento", "partner_id", string="Movimientos",
        groups="distri_pedido.grupo_vendedor,distri_pedido.grupo_administracion,"
               "distri_pedido.grupo_gerencia",
    )
    cantidad_movimientos = fields.Integer(
        string="Movimientos", compute="_compute_cantidad_movimientos",
        groups="distri_pedido.grupo_vendedor,distri_pedido.grupo_administracion,"
               "distri_pedido.grupo_gerencia",
    )

    @api.depends("movimiento_ids")
    def _compute_cantidad_movimientos(self):
        datos = self.env["distri.cuenta.movimiento"]._read_group(
            [("partner_id", "in", self.ids)], ["partner_id"], ["__count"],
        )
        cuenta = {p.id: n for p, n in datos}
        for socio in self:
            socio.cantidad_movimientos = cuenta.get(socio.id, 0)

    def action_ver_cuenta(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Cuenta corriente de %s" % self.name,
            "res_model": "distri.cuenta.movimiento",
            "view_mode": "list",
            "domain": [("partner_id", "=", self.id)],
            "context": {"search_default_partner_id": self.id},
        }
