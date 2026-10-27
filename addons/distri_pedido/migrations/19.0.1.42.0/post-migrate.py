"""«Confirmado sin comprobante» (19.0.1.42.0, models/cobros.py).

The Mercado Pago incomes already confirmed without a WhatsApp receipt get
the flag, so the receipts to claim from the past are visible too.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    count, amount = env["distri.mp.collection"]._flag_confirmed_without_receipt()
    _logger.info("Confirmado sin comprobante: %s cobros confirmados marcados por %.2f",
                 count, amount)
