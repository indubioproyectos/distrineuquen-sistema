"""«Mi vendedor es» on /pedido (19.0.1.98.0).

The customer picks their seller from a short list when sending an order.
The list is a small model administración edits without code (name, active,
order, optional user). The choice is stored on the order and becomes the
customer's default (res.partner), pre-selected next time. It never touches
the order's salesperson (user_id), which drives portfolios and rules.
"""
import unicodedata

from odoo import api, fields, models


def _plain(text):
    text = unicodedata.normalize("NFKD", text or "")
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


class DistriVendedorWeb(models.Model):
    _name = "distri.vendedor.web"
    _description = "Vendedor que elige el cliente en la página"
    _order = "sequence, name, id"

    name = fields.Char(string="Nombre", required=True)
    sequence = fields.Integer(string="Orden", default=10)
    active = fields.Boolean(default=True)
    user_id = fields.Many2one(
        "res.users", string="Usuario", domain=[("share", "=", False)], ondelete="set null",
        help="Opcional: el usuario del sistema de este vendedor.")
    order_count = fields.Integer(string="Pedidos", compute="_compute_order_count")

    _nombre_unico = models.Constraint("unique(name)", "Ya hay un vendedor con ese nombre.")

    def _compute_order_count(self):
        datos = self.env["sale.order"].sudo()._read_group(
            [("distri_vendedor_web_id", "in", self.ids)], ["distri_vendedor_web_id"], ["__count"])
        cuenta = {vendedor.id: n for vendedor, n in datos}
        for vendedor in self:
            vendedor.order_count = cuenta.get(vendedor.id, 0)

    @api.model
    def _distri_vincular_usuarios(self):
        """Link each entry without a user to the only internal user with the
        same name (accents and case ignored). Run from data on every update;
        an ambiguous or missing name stays unlinked."""
        usuarios = self.env["res.users"].sudo().search([("share", "=", False)])
        por_nombre = {}
        for usuario in usuarios:
            por_nombre.setdefault(_plain(usuario.name), []).append(usuario)
        for vendedor in self.sudo().with_context(active_test=False).search([("user_id", "=", False)]):
            candidatos = por_nombre.get(_plain(vendedor.name), [])
            if len(candidatos) == 1:
                vendedor.user_id = candidatos[0]

    @api.model
    def _distri_para_pagina(self):
        """The active entries for the /pedido dropdown (public page: sudo)."""
        return self.sudo().search([])

    @api.model
    def _distri_del_usuario(self, user):
        """The entry linked to a staff user, if any."""
        return self.sudo().search([("user_id", "=", user.id)], limit=1)


class ResPartner(models.Model):
    _inherit = "res.partner"

    distri_vendedor_web_id = fields.Many2one(
        "distri.vendedor.web", string="Vendedor (según el cliente)", ondelete="set null",
        help="El que eligió el cliente en «Mi vendedor es» de la página. Se propone en su "
             "próximo pedido.")


class SaleOrder(models.Model):
    _inherit = "sale.order"

    distri_vendedor_web_id = fields.Many2one(
        "distri.vendedor.web", string="Vendedor (según el cliente)", copy=False, index=True,
        ondelete="set null", help="Lo que eligió el cliente en «Mi vendedor es» de la página.")
    # 19.0.1.98.0: the customer's comment (/pedido, e.g. the hours they receive)
    # and the staff note for the depot (sellers, administración). Shown on
    # /armado (top), «Pedidos para revisar», the order and the driver's stop.
    distri_comentario = fields.Text(string="Comentario del cliente", copy=False)
    distri_nota_deposito = fields.Text(string="Nota para el depósito", copy=False)

    @api.model_create_multi
    def create(self, vals_list):
        pedidos = super().create(vals_list)
        pedidos._distri_recordar_vendedor()
        return pedidos

    def write(self, vals):
        res = super().write(vals)
        if "distri_vendedor_web_id" in vals or "partner_id" in vals:
            self._distri_recordar_vendedor()
        return res

    def _distri_recordar_vendedor(self):
        """The customer's choice becomes their default for next time (only on
        a real customer, never on the generic «Pedidos web» contacts)."""
        for pedido in self.sudo():
            socio = pedido.partner_id.commercial_partner_id
            if (pedido.distri_vendedor_web_id and socio and not socio.distri_generic_partner
                    and socio.distri_vendedor_web_id != pedido.distri_vendedor_web_id):
                socio.distri_vendedor_web_id = pedido.distri_vendedor_web_id
