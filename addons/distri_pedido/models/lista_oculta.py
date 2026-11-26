"""Who sees and manages the hidden price list (rule of 30/09/2026).

Sellers work on the street with small shops; distributors and resellers are
handled only by Gerencia and Administración. So, among staff, the hidden list
(Distribuidoras and the «Revendedor NN» lists built on it, cigarettes
included) is seen and managed only by Gerencia, Administración and the
system administrator. A seller (grupo_vendedor without Administración) sees
only the General list, like any shop:

- /pedido (controllers/main.py) never renders or accepts the hidden section
  for a seller, and a seller cannot take, open or edit an order of a
  distributor there («Pedido de distribuidora: lo gestiona Administración»).
- Backend: global record rules (security/reglas.xml) hide the hidden lists,
  their items and the distributors' orders from sellers; the guards below
  stop a seller from assigning a hidden list to a customer or an order, or
  writing a distributor's order, with a clear message.

Customers and visitors (portal and public users, 19.0.1.24.2): website_sale
grants them read access to every price list and price rule, which the data
API (/web/dataset/call_kw, /json/2) exposes. Global record rules
(security/reglas.xml) narrow that to the non-hidden lists plus the
customer's own hidden list when its company is authorized
(res.users.distri_lista_cliente_id). /pedido and the shop price with sudo.

Everything done with sudo (the /pedido controller, crons, wizards) is not
affected.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError

from ..controllers.main import MENSAJE_DISTRIBUIDORA, NOMBRES_LISTA_OCULTA
from .pedido_whatsapp import is_staff

MENSAJE_LISTA = ("La lista de precios oculta (Distribuidoras y revendedores) la asignan "
                 "Gerencia y Administración.")
CAMPOS_LISTA_SOCIO = ("property_product_pricelist", "specific_property_product_pricelist")


def ve_lista_oculta(user):
    """Staff that sees and manages the hidden list: Gerencia (implies
    Administración), Administración and the system administrator."""
    if not user or user._is_public() or user.share:
        return False
    return (user.has_group("distri_pedido.grupo_administracion")
            or user.has_group("base.group_system"))


def ve_mayoristas(user):
    """19.0.1.115.0: who sees the wholesale customers (_distri_es_distribuidora)
    in the staff screens (/cuentas, /pedido, /cobro, /comprobantes, remitos):
    administración, gerencia and the system administrators, the same people
    as the hidden list. Every other staff member sees the direct customers."""
    return ve_lista_oculta(user)


# the cheap part of _distri_es_distribuidora as a domain (the price list is
# checked on the found records)
MAYORISTA_FUERA = [("distri_lista_oculta", "=", False), ("distri_es_mayorista", "=", False)]


def solo_vendedor(user):
    """A seller without Administración: sees the General list only."""
    return is_staff(user) and not ve_lista_oculta(user)


def _vendedor_sin_sudo(env):
    return not env.su and solo_vendedor(env.user)


def _limpiar_reglas(env):
    """The customers' rule embeds the id of their own hidden list, and rule
    domains are cached per user: drop the cache when that can change."""
    env["res.users"].invalidate_model(["distri_lista_cliente_id"])
    env.registry.clear_cache()


class ResUsersListaOculta(models.Model):
    _inherit = "res.users"

    # Read by the customers' record rules (security/reglas.xml). Computed
    # with sudo: the customer cannot read the hidden list nor the flag.
    distri_lista_cliente_id = fields.Many2one(
        "product.pricelist", string="Lista oculta propia", compute="_compute_distri_lista_cliente",
        compute_sudo=True,
        help="La lista oculta asignada a la empresa del cliente, solo si tiene la marca "
             "«Distribuidora / revendedor (lista oculta)».")

    def _compute_distri_lista_cliente(self):
        for usuario in self:
            socio = usuario.partner_id.commercial_partner_id
            lista = socio.property_product_pricelist
            usuario.distri_lista_cliente_id = (
                lista if usuario.share and socio.distri_lista_oculta and lista._distri_es_oculta()
                else False)


class ProductPricelistOculta(models.Model):
    _inherit = "product.pricelist"

    def write(self, vals):
        resultado = super().write(vals)
        if {"distri_mayorista", "name", "distri_clave", "active"} & set(vals):
            _limpiar_reglas(self.env)
        return resultado

    def _distri_es_oculta(self):
        """«May farmacia y varios» (Distribuidoras until 19.0.1.129.52) or a
        list flagged «Lista de distribuidoras» (the «Revendedor NN» lists,
        «Lista 2/3/4 + May farmacia»). Any list with a distri_clave other
        than the General one is hidden too, whatever its name."""
        return any(lista.distri_mayorista or lista.name in NOMBRES_LISTA_OCULTA
                   or (lista.distri_clave and lista.distri_clave != "general")
                   for lista in self.sudo())


class ResPartnerListaOculta(models.Model):
    _inherit = "res.partner"

    def _distri_es_distribuidora(self):
        """Authorized for the hidden list, or with a hidden list assigned.
        19.0.1.115.0 (owner: «los que tienen etiqueta de mayorista solo los
        ven Denis y Gonzalo y admin»): also the customers marked «Es
        mayorista» (revision_cliente.py). Staff without Administración never
        see these customers (ve_mayoristas)."""
        self.ensure_one()
        socio = self.sudo()
        comercial = socio.commercial_partner_id
        return bool(comercial.distri_lista_oculta or comercial.distri_es_mayorista
                    or socio.property_product_pricelist._distri_es_oculta())

    def _distri_check_lista_vendedor(self, vals_list):
        if not any(campo in vals for vals in vals_list for campo in CAMPOS_LISTA_SOCIO):
            return
        if not _vendedor_sin_sudo(self.env):
            return
        if any(socio.sudo().property_product_pricelist._distri_es_oculta() for socio in self):
            raise UserError(MENSAJE_LISTA)

    @api.model_create_multi
    def create(self, vals_list):
        socios = super().create(vals_list)
        socios._distri_check_lista_vendedor(vals_list)
        return socios

    def write(self, vals):
        resultado = super().write(vals)
        self._distri_check_lista_vendedor([vals])
        if {"distri_lista_oculta", *CAMPOS_LISTA_SOCIO} & set(vals):
            _limpiar_reglas(self.env)
        return resultado


class SaleOrderListaOculta(models.Model):
    _inherit = "sale.order"

    def _distri_de_distribuidora(self):
        """Priced with a hidden list, or of an authorized distributor."""
        return any(
            pedido.pricelist_id._distri_es_oculta()
            or pedido.partner_id.commercial_partner_id.distri_lista_oculta
            for pedido in self.sudo()
        )

    def _distri_check_vendedor(self):
        if _vendedor_sin_sudo(self.env) and self._distri_de_distribuidora():
            raise UserError(MENSAJE_DISTRIBUIDORA)

    @api.model_create_multi
    def create(self, vals_list):
        pedidos = super().create(vals_list)
        pedidos._distri_check_vendedor()
        return pedidos

    def write(self, vals):
        # before: a seller never changes a distributor's order (no repricing
        # with a list it cannot see); after: nor turns one into it
        self._distri_check_vendedor()
        resultado = super().write(vals)
        if {"pricelist_id", "partner_id"} & set(vals):
            self._distri_check_vendedor()
        return resultado
