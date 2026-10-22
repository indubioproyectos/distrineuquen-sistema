"""Catriel picks up at the depot (decision of management, 22/10/2026).

The zone record is noupdate: it gets «Retiran en el depósito» here (and no
parcel), which puts its open orders in «Lo voy a retirar»
(models/retiro_deposito.py). The /proyecto card that asked whether Catriel
is delivered or shipped is reworded, only if nobody edited it."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

OLD_STEP = ("Probarla el día de prueba con un cliente del interior. El gerente define si "
            "Catriel se reparte o sale por encomienda.")
NEW_STEP = ("Probarla el día de prueba con un cliente del interior. Catriel retira en el "
            "depósito (decisión de la gerencia del 22/10/2026).")


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    zona = env.ref("distri_pedido.zona_catriel", raise_if_not_found=False)
    if zona:
        zona.write({"encomienda": False})
        zona.write({"retira_deposito": True})
        _logger.info("Catriel: picks up at the depot")
    cr.execute("UPDATE distri_project_module SET next_step = %s WHERE next_step = %s",
               (NEW_STEP, OLD_STEP))
