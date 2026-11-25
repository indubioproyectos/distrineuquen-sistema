from odoo import fields, models
from odoo.exceptions import UserError

from .res_partner import GENERIC_NAMES, GENERIC_RETAIL, GENERIC_WHOLESALE

# Default names of the generic contacts web orders come in under. They are
# only used to create a missing one: the contacts are identified by the
# res.partner.distri_generic_partner flag (see res_partner.py).
SOCIO_SIN_ASIGNAR = GENERIC_NAMES[GENERIC_RETAIL]
SOCIO_MAYORISTA = GENERIC_NAMES[GENERIC_WHOLESALE]

# 19.0.1.92.0: the stock legend of every order, in one place. Shown on
# /pedido before sending, on the order page, in the order WhatsApp texts
# (controllers/main.py) and, its first sentence only, on the quotation/order
# PDF and the remito (views/templates.xml leyenda_stock). Never on invoices.
# 19.0.1.129.40 (owner, 25/11): the link also lets the customer add products
STOCK_NOTICE = ("Pedido sujeto a disponibilidad de stock. Con este link podés agregar productos "
                "hasta que lo armemos.")
STOCK_NOTICE_SHORT = STOCK_NOTICE.split(". ")[0] + "."


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def _distri_stock_notice(self, short=False):
        """The stock legend (STOCK_NOTICE); `short`: the printed documents."""
        return STOCK_NOTICE_SHORT if short else STOCK_NOTICE

    # Lo que el cliente pidio desde la pagina, tal cual lo mando. El deposito
    # despues saca faltantes o cambia marcas, y con esto el cliente puede ver
    # que le cambiaron en lugar de enterarse cuando llega el reparto.
    distri_original = fields.Text(
        string="Pedido original del cliente", copy=False, readonly=True,
    )
    # Los datos que cargo el cliente en la pagina. Antes vivian solo adentro
    # de la nota, en texto libre, asi que al editar el pedido habia que
    # volver a escribirlos todos.
    distri_nombre = fields.Char(string="Comercio (web)", copy=False)
    distri_telefono = fields.Char(string="Teléfono (web)", copy=False)
    distri_domicilio = fields.Char(string="Domicilio (web)", copy=False)
    distri_entrega = fields.Selection(
        [("domicilio", "Envío a domicilio"), ("retira", "Retira en el depósito")],
        string="Entrega (web)", copy=False,
    )
    distri_modificado = fields.Boolean(
        string="Modificado por el cliente", copy=False, readonly=True,
    )
    # 19.0.1.93.0 (E2E-C C8): the fingerprint of the /pedido form that created
    # the order (session, page token and cart). The same form posted twice
    # returns the first order instead of creating another one.
    distri_envio_clave = fields.Char(string="Clave del envío (web)", copy=False, readonly=True)

    _distri_envio_clave_unique = models.Constraint(
        "unique(distri_envio_clave)", "Ese pedido ya se recibió.")
    # The order is still under a generic "Pedidos web" contact.
    distri_partner_generic = fields.Boolean(
        string="Sin cliente asignado", related="partner_id.distri_generic_partner",
    )

    def _es_pedido_web_sin_asignar(self):
        """Un pedido que entro por la pagina y todavia no tiene cliente real."""
        return (
            self.state == "draft"
            and self.partner_id.distri_generic_partner
        )

    def _distri_facturas_emitidas(self):
        """Las facturas de cliente ya emitidas (publicadas) de este pedido.

        Recien con la factura el importe es el final: el armador ya descontó
        los faltantes y el administrador facturó lo entregado. Las notas de
        crédito no cuentan aparte: al conciliarse bajan el saldo de la factura.
        """
        self.ensure_one()
        return self.sudo().invoice_ids.filtered(
            lambda f: f.move_type == "out_invoice" and f.state == "posted"
        )

    def _distri_saldo_facturado(self):
        """Lo que falta pagar de las facturas emitidas del pedido.

        Descuenta las devoluciones que informó el repartidor y todavía no se
        rindieron («Devolución informada»): el cliente ya pagó de menos.
        """
        self.ensure_one()
        facturas = self._distri_facturas_emitidas()
        saldo = sum(facturas.mapped("amount_residual"))
        if facturas:
            saldo -= sum(self.env["distri.route.return"]._distri_provisional_for_order(
                self).mapped("amount"))
        return saldo

    def _distri_importe_a_pagar(self):
        """Lo que el cliente tiene que transferir por este pedido («Cómo pagar»).

        El saldo de las facturas del pedido, sin pasar el saldo de su cuenta
        corriente: un saldo a favor (del historial de Online o de otro pago)
        ya cubre parte o todo, y la página de la cuenta dice «Estás al día».
        """
        self.ensure_one()
        saldo = self._distri_saldo_facturado()
        if self.partner_id.distri_generic_partner:
            return saldo
        cuenta = self.sudo().partner_id._distri_account_balance()
        return max(min(saldo, cuenta), 0.0)

    def write(self, vals):
        """Al asignarle el cliente, el pedido se valoriza con SU lista.

        Es el paso que hoy el gerente hace a mano: el revendedor manda los
        cigarrillos sin precio, y el precio sale de la lista del cliente
        recien cuando se sabe quien es. Odoo ya trae _recompute_prices(),
        que es el boton "Actualizar precios"; aca se dispara solo.
        """
        asignaba = {}
        if "partner_id" in vals and not self.env.context.get("distri_asignar_cliente"):
            # New process: the customer of a web order is only assigned
            # through the "Asignar cliente" wizard. Moving between the two
            # generic contacts (retail/wholesale) is still allowed.
            nuevo = self.env["res.partner"].browse(vals["partner_id"])
            if not nuevo.distri_generic_partner and any(
                p.partner_id.distri_generic_partner for p in self
            ):
                raise UserError(
                    "Este pedido entró por la página sin cliente. El cliente se asigna "
                    "con el botón «Asignar cliente», arriba del pedido."
                )
        if "partner_id" in vals:
            asignaba = {
                pedido.id: pedido._es_pedido_web_sin_asignar()
                for pedido in self
            }

        resultado = super().write(vals)

        recien_asignados = self.browse([
            pedido_id for pedido_id, era in asignaba.items() if era
        ]).filtered(lambda p: not p.partner_id.distri_generic_partner)

        for pedido in recien_asignados:
            lista = pedido.partner_id.property_product_pricelist
            if lista and pedido.pricelist_id != lista:
                # no pasa por write() de nuevo con partner_id, asi que no recursa
                super(SaleOrder, pedido).write({"pricelist_id": lista.id})
            # el armador puede haberlo tomado antes de la asignacion
            # (decision 7 del armado): el cambio de precios no es un cambio
            # del cliente y no se bloquea
            pedido.with_context(distri_armado=True)._recompute_prices()

        return resultado

    def _create_invoices(self, grouped=False, final=False, date=None):
        """Sin cliente asignado no se factura.

        El armador puede armar un pedido que el administrador todavia no
        aprobo (decision 7 del armado); la factura espera la asignacion.
        """
        if any(p.partner_id.distri_generic_partner for p in self):
            raise UserError("Asigná el cliente al pedido antes de facturarlo.")
        return super()._create_invoices(grouped=grouped, final=final, date=date)
