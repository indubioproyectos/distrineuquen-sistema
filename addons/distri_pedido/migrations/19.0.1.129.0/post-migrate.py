"""19.0.1.129.0: «Cobros: pago a proveedor de Distri» (decision 7) for the
administrator (Denis, login «denis») and the technical admin (base.user_admin,
already in the group's data). Gerencia gets it through grupo_gerencia. Once:
later changes to the group are made by hand and never undone here."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

LOGINS = ("denis",)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {"active_test": False})
    group = env.ref("distri_pedido.grupo_pago_proveedor", raise_if_not_found=False)
    if not group:
        return
    users = env["res.users"].search([("login", "in", LOGINS), ("share", "=", False)])
    if users:
        group.write({"user_ids": [(4, u.id) for u in users]})
    _logger.info("grupo_pago_proveedor granted to %s", ", ".join(users.mapped("login")) or "nobody")
