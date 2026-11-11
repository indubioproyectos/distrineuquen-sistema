"""One icon family for every app of the lab (19.0.1.77.0).

The source SVGs and their PNGs live in static/src/img/iconos/. Our apps use
the brand blue, Odoo's own apps the same drawing style on slate, so the main
menu and /tablero read as one set. Odoo rewrites an app's web_icon when that
app is upgraded, so this runs on every update of distri_pedido
(data/iconos.xml) and simply puts ours back.
"""
from odoo import api, models

ICON_DIR = "distri_pedido,static/src/img/iconos/%s.png"

APP_ICONS = {
    "distri_pedido.menu_cuentas_root": "cuentas",
    "distri_pedido.menu_cobros_root": "cobros",
    "distri_pedido.menu_reparto_root": "reparto",
    "distri_pedido.menu_armado_app": "armado",
    "distri_pedido.menu_cuenta_raiz": "cuenta_corriente",
    "distri_pedido.menu_transicion_app": "transicion",
    "mail.menu_root_discuss": "discuss",
    "contacts.menu_contacts": "contacts",
    "sale.sale_menu_root": "sales",
    "distri_tema.menu_presupuestos_raiz": "presupuestos",
    "spreadsheet_dashboard.spreadsheet_dashboard_menu_root": "dashboards",
    "account.menu_finance": "invoicing",
    "website.menu_website_configuration": "website",
    "purchase.menu_purchase_root": "purchase",
    "stock.menu_stock_root": "inventory",
    "utm.menu_link_tracker_root": "links",
    "base.menu_management": "apps",
    "base.menu_administration": "settings",
    "base.menu_tests": "tests",
}


class IrUiMenu(models.Model):
    _inherit = "ir.ui.menu"

    @api.model
    def _distri_apply_app_icons(self):
        """Set our icon on each top menu that exists; returns how many changed."""
        changed = 0
        for xmlid, name in APP_ICONS.items():
            menu = self.env.ref(xmlid, raise_if_not_found=False)
            icon = ICON_DIR % name
            if menu and menu._name == "ir.ui.menu" and menu.web_icon != icon:
                menu.sudo().write({"web_icon": icon})  # Odoo refreshes web_icon_data
                changed += 1
        return changed
