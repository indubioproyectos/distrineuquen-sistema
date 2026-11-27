"""19.0.1.129.34 («Etapa 3b», part 2; plan «Flujos innecesarios» 7 and 12):
with distri_pedido.ui_v2 on, the Odoo menus that only Sistemas uses leave the
backend's menu bar of everybody else.

How: ir.ui.menu._load_menus_blacklist(), the hook Odoo's web client menu
load (load_menus) already has. Nothing is written on a menu, a group or an
access right:
- reversible at once: with the flag off the list is empty and every menu is
  back on the next page load (the parameter's write clears the menu cache,
  see IrConfigParameter below);
- a module update cannot undo it or leave it half done, as it could with
  group_ids written on the menus;
- only the menu bar: the actions keep their access rights and record rules,
  a bookmark or a link to them still opens, and _visible_menu_ids() is
  unchanged, so the Inicio tiles and the kit bars (controllers/tablero.py,
  kit.py, rendiciones.py read it) are exactly as before;
- Sistemas (base.group_system) always sees every menu.

The list (SISTEMAS_ONLY_MENUS) comes from the plan: the second copy of
«Transición AgilPedido» under Ventas (the app of the same name keeps the same
actions), and the configuration and technical entries of Reparto, Cobros,
Transición AgilPedido and Ventas that no daily role lists among the backend
screens it still uses (plan, «Diseño: lo hecho y lo que falta», column
«Todavía Odoo»). A menu a kit page links to (Reparto › Casos, Cargos,
Comprobantes pendientes, Zonas, Rendición de caja, Despacho) is never in it.
"""
from odoo import api, models

PARAM_UI_V2 = "distri_pedido.ui_v2"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")
SYSTEM_GROUP = "base.group_system"
# 19.0.1.129.56: the parameters the menu bar depends on (this one and
# models/entradas_viejas.py's): a change clears the cached menus
PARAMS_MENUS = (PARAM_UI_V2, "distri_pedido.ocultar_presupuesto_factura")

# xmlid of each menu (its sub-menus go with it) only Sistemas keeps
SISTEMAS_ONLY_MENUS = (
    # Ventas › Transición AgilPedido: a copy of the «Transición AgilPedido» app
    "distri_pedido.menu_transicion",
    # Ventas: configuration, and a copy of Reparto › Zonas
    "distri_pedido.menu_vendedor_web",
    "distri_pedido.menu_zona",
    # Reparto: the driver app's technical log, GPS times and settings, carriers
    "distri_pedido.menu_reparto_rejections",
    "distri_pedido.menu_reparto_tiempos",
    "distri_pedido.menu_reparto_settings",
    "distri_pedido.menu_reparto_carriers",
    # Cobros: configuration and the audit's technical runs and raw rows
    "distri_pedido.menu_cobros_comprobantes_config",
    "distri_pedido.menu_cobros_wholesale_rules",
    "distri_pedido.menu_cobros_auditoria_corridas",
    "distri_pedido.menu_cobros_cash_sheet_lines",
    # Transición AgilPedido: the project status page, the sync log and the
    # section photos (configuration)
    "distri_pedido.menu_project_status",
    "distri_pedido.menu_app_agil_sincronizaciones",
    "distri_pedido.menu_fotos_secciones",
)


def ui_v2_flag(env):
    value = env["ir.config_parameter"].sudo().get_param(PARAM_UI_V2) or "0"
    return str(value).strip().lower() in ON_VALUES


def sistemas_only_menu_ids(env):
    """The ids of SISTEMAS_ONLY_MENUS that exist."""
    ids = []
    for xmlid in SISTEMAS_ONLY_MENUS:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if menu and menu._name == "ir.ui.menu":
            ids.append(menu.id)
    return ids


def hidden_menu_ids(env):
    """The menus hidden from this user's menu bar: SISTEMAS_ONLY_MENUS with
    ui_v2 on, for anybody but Sistemas; none otherwise."""
    user = env.user
    if not ui_v2_flag(env) or user.has_group(SYSTEM_GROUP):
        return []
    return sistemas_only_menu_ids(env)


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    def _load_menus_blacklist(self):
        return list(super()._load_menus_blacklist()) + hidden_menu_ids(self.env)


class IrConfigParameter(models.Model):
    _inherit = "ir.config_parameter"

    # load_menus() is cached per user in the default cache, which a parameter
    # write does not clear (it clears the «stable» one): clear it when the
    # ui_v2 flag (or 19.0.1.129.56's ocultar_presupuesto_factura) changes, so
    # the menus follow the flag on the next page load.

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        if any(vals.get("key") in PARAMS_MENUS for vals in vals_list):
            self.env.registry.clear_cache()
        return records

    def write(self, vals):
        touched = bool(set(PARAMS_MENUS) & set(self.mapped("key"))) \
            or vals.get("key") in PARAMS_MENUS
        result = super().write(vals)
        if touched:
            self.env.registry.clear_cache()
        return result

    def unlink(self):
        touched = bool(set(PARAMS_MENUS) & set(self.mapped("key")))
        result = super().unlink()
        if touched:
            self.env.registry.clear_cache()
        return result
