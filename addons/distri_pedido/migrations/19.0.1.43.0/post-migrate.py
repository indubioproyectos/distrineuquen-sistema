"""Customer suggested by the text after the photo (19.0.1.43.0,
models/sugerencia_cliente.py).

The receipts already imported without a customer get the suggestion of the
text sent right after them. Nothing is assigned: the administrator confirms
each one on its card.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    result = env["distri.whatsapp.message"]._distri_refresh_suggestions()
    _logger.info("Clientes sugeridos: %(suggested)s de %(receipts)s comprobantes sin cliente",
                 result)
