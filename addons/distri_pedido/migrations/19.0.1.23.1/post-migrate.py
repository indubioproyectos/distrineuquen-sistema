"""The own-account parameter created by 19.0.1.23.0 lacked the trade name:
add it after the holder when it is missing (19.0.1.32.0: the rule works on
whatever value the server has; no account data lives in the code)."""
from odoo import SUPERUSER_ID, api

TRADE_NAME = "Distri Neuquén"


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Param = env["ir.config_parameter"].sudo()
    value = (Param.get_param("distri_pedido.cuenta_propia") or "").strip()
    parts = [p.strip() for p in value.split(",") if p.strip()]
    if len(parts) >= 2 and TRADE_NAME.lower() not in value.lower():
        parts.insert(2, TRADE_NAME)
        Param.set_param("distri_pedido.cuenta_propia", ", ".join(parts))
