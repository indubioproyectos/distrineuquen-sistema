"""Tablero de cobros (models/cobros.py): the photos and PDFs received by
WhatsApp before this version are read once as possible payment receipts,
so the board can match them with the next Mercado Pago report. Nothing is
imported and no payment is created here."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    messages = env["distri.whatsapp.message"].search([
        ("direction", "=", "in"), ("message_type", "in", ("image", "document")),
        ("distri_receipt_state", "=", False),
    ])
    messages._distri_read_receipt(read_file=True)
    _logger.info("distri_pedido: %s WhatsApp photos/PDFs read as receipts (%s look like one)",
                 len(messages), len(messages.filtered("distri_is_receipt")))
