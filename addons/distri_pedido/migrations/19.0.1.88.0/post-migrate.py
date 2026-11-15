"""Several WhatsApp numbers with roles (19.0.1.88.0, models/whatsapp.py).

The single `distri_pedido.wa_phone_number_id` (today Meta's test number)
becomes a `distri.whatsapp.numero` with role «Pedidos y comprobantes», so
the behaviour does not change, and the messages already received on it get
the number. Messages without a phone_number_id (app photos, chat imports)
are left as they are. Idempotent.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    numero = env["distri.whatsapp.numero"]._wa_migrate_phone_param()
    if numero:
        _logger.warning("19.0.1.88.0 número de WhatsApp migrado: %s (%s, %s)",
                        numero.name, numero.phone_number_id, numero.rol)
    else:
        _logger.warning("19.0.1.88.0 sin distri_pedido.wa_phone_number_id: no se migró ningún "
                        "número de WhatsApp")
