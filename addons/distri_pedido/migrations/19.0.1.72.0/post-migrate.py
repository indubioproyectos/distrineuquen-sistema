"""Operation numbers without their label (19.0.1.72.0, models/ocr_comprobante.py).

Chat import 20 (06/11/2026): two photos of one Mercado Pago receipt lost the
header and the «N.° de operación de Mercado Pago» label in the OCR, so the
parser left their operation number empty and the second photo was paired by
amount to another transfer. The parser now takes a bare Mercado Pago id when
no bank is named and it is the only one. This reads the stored text of the
receipts without a number again (`_distri_recompute_operations`): a waiting
receipt is filled and matched again; one tied to a confirmed or authorized
card is only logged, never changed.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    report = env["distri.whatsapp.message"]._distri_recompute_operations()
    for row in report:
        _logger.warning("19.0.1.72.0 operación de comprobante: %s", row)
    _logger.warning("19.0.1.72.0 comprobantes con número leído de nuevo: %s (cambiados %s)",
                    len(report), sum(row["action"] == "filled" for row in report))
