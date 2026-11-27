"""19.0.1.129.56 (owner, 27/11/2026: «sacar los botones de Presupuesto y
Facturación para que no los vuelvan a usar»): with the parameter
distri_pedido.ocultar_presupuesto_factura on, the old entry points to the
quotations and to the customer invoices leave the screens of the staff, who
sell through «Nueva venta». OFF by default: with it off nothing changes.

Who keeps everything (EXENTOS): Sistemas (base.group_system), the owner
(models/verificacion.py is_owner) and Gerencia.

What is hidden for everybody else:
- the backend's menu bar (ir.ui.menu._load_menus_blacklist, as
  models/menus_sistemas.py: nothing is written on a menu or a group, the
  flag is reversible at once and a module update cannot leave it half
  done): MENUS_OCULTOS below;
- Inicio › «Todas las aplicaciones del sistema»: the «PRESUPUESTOS» app of
  distri_tema (controllers/tablero.py other_apps reads hidden_root_ids);
- «Crear factura» on the order form (views/entradas_viejas.xml, through
  sale.order.distri_sin_entradas_viejas).

What stays, flag on or off:
- «Nueva venta» and its four exits («Presupuesto» among them), the kit
  screens of an order and of an invoice (/pedidos/<id>, /facturas/<id>),
  «Mis ventas», «Cuentas»: reading an existing quotation or invoice;
- «Facturación directa de servicios»: its members get their own menu,
  Facturación › Clientes › «Facturas directas de servicios»
  (views/entradas_viejas.xml), the customer invoices that do not come from
  an order; the posting rules are models/factura_directa.py's, unchanged;
- Facturación › Clientes › «Notas de crédito» and «Revertir» on an invoice;
- the actions themselves: only menus and buttons are hidden, no access
  right changes, so a link or a bookmark to a quotation or an invoice still
  opens (the accounting audit reads them). What must not be done is refused
  by the server since 129.36 / 129.49 (models/factura_directa.py,
  models/bloqueo_factura.py), not by this flag.
"""
from odoo import api, fields, models

from .menus_sistemas import ON_VALUES

PARAM_OCULTAR = "distri_pedido.ocultar_presupuesto_factura"
SYSTEM_GROUP = "base.group_system"
GERENCIA_GROUP = "distri_pedido.grupo_gerencia"

# xmlid of each menu hidden (its sub-menus go with it); one that does not
# exist in the database (distri_tema not installed) is skipped
MENUS_OCULTOS = (
    # the «PRESUPUESTOS» app of the home screen (distri_tema)
    "distri_tema.menu_presupuestos_raiz",
    # Ventas › Pedidos › Cotizaciones / Pedidos, Ventas › A facturar
    "sale.menu_sale_quotations",
    "sale.menu_sale_order",
    "sale.menu_sale_invoicing",
    # Facturación › Clientes › Facturas
    "account.menu_action_move_out_invoice_type",
)


def ocultar_on(env):
    value = env["ir.config_parameter"].sudo().get_param(PARAM_OCULTAR) or "0"
    return str(value).strip().lower() in ON_VALUES


def exento(user):
    """Sistemas, the owner and Gerencia keep every entry point."""
    from .verificacion import is_owner
    return bool(user) and (user.has_group(SYSTEM_GROUP) or user.has_group(GERENCIA_GROUP)
                           or is_owner(user))


def oculta_para(env):
    """The old entry points are hidden from the current user."""
    return ocultar_on(env) and not exento(env.user)


def hidden_menu_ids(env):
    """The ids of MENUS_OCULTOS hidden from this user's menu bar."""
    if not oculta_para(env):
        return []
    ids = []
    for xmlid in MENUS_OCULTOS:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if menu and menu._name == "ir.ui.menu":
            ids.append(menu.id)
    return ids


def hidden_root_ids(env):
    """The hidden menus that are apps (Inicio's «Todas las aplicaciones»)."""
    ids = hidden_menu_ids(env)
    if not ids:
        return set()
    return set(env["ir.ui.menu"].sudo().browse(ids).filtered(lambda m: not m.parent_id).ids)


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    def _load_menus_blacklist(self):
        return list(super()._load_menus_blacklist()) + hidden_menu_ids(self.env)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    distri_sin_entradas_viejas = fields.Boolean(
        string="Sin los botones de facturación del pedido",
        compute="_compute_distri_sin_entradas_viejas")

    @api.depends_context("uid")
    def _compute_distri_sin_entradas_viejas(self):
        oculta = oculta_para(self.env)
        for order in self:
            order.distri_sin_entradas_viejas = oculta
