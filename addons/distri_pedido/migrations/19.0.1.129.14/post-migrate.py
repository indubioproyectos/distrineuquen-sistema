"""19.0.1.129.14: the supplier payments loaded by hand before this version
(PPC-00002...) get the checksum of their receipt file, so the same receipt
uploaded at /comprobantes is not registered or credited a second time."""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    records = env["distri.supplier.payment"].search([("receipt_checksum", "=", False)])
    done = 0
    for record in records:
        attachment = env["ir.attachment"].search([
            ("res_model", "=", record._name), ("res_id", "=", record.id),
            ("res_field", "=", "receipt_file")], limit=1)
        if attachment.checksum:
            cr.execute("UPDATE distri_supplier_payment SET receipt_checksum = %s WHERE id = %s",
                       [attachment.checksum, record.id])
            done += 1
    _logger.info("Supplier payments: %s of %s got their receipt checksum", done, len(records))
