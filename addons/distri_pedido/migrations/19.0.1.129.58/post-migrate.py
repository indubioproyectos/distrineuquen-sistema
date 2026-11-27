"""19.0.1.129.58 (27/11/2026), idempotent (a second run changes nothing).

The operation number stored on a receipt card (distri.mp.collection,
kind verify) and on a supplier payment keeps the letters of a bank's
alphanumeric id (models/cobros.py operation_key). Before, only its digits
were kept, and two different transfers («ORD6LEN8MZRK3KZPNM1Y30» and
«ORD6LEN8M3YVEYVONM1Y30») collapsed to the same «683130».

Here a stored operation that is exactly the digits of its receipt's
operation, when that operation has letters, becomes operation_key of it. A
purely numeric operation (Mercado Pago, also after a label like «Nº») is
never touched, nor a card or payment without a receipt, nor an operation
that is not that collapse.
"""
import logging

from odoo.addons.distri_pedido.models.cobros import digits, operation_key

_logger = logging.getLogger(__name__)

QUERIES = (
    ("distri_mp_collection", """
        SELECT c.id, c.operation, m.distri_operation
          FROM distri_mp_collection c JOIN distri_whatsapp_message m ON m.id = c.receipt_id
         WHERE c.kind = 'verify' AND c.operation IS NOT NULL
           AND m.distri_operation ~ '[A-Za-z]'"""),
    ("distri_supplier_payment", """
        SELECT p.id, p.operation, m.distri_operation
          FROM distri_supplier_payment p
          JOIN distri_whatsapp_message m ON m.id = p.receipt_message_id
         WHERE p.operation IS NOT NULL AND m.distri_operation ~ '[A-Za-z]'"""),
)


def migrate(cr, version):
    if not version:
        return
    done = {}
    for table, query in QUERIES:
        cr.execute(query)
        changes = []
        for row_id, stored, read in cr.fetchall():
            key = operation_key(read)
            if stored == digits(read) and key and key != stored:
                changes.append((key, row_id))
        for key, row_id in changes:
            cr.execute("UPDATE %s SET operation = %%s WHERE id = %%s" % table, (key, row_id))
        done[table] = len(changes)
    _logger.info("19.0.1.129.58: alphanumeric operation kept whole on %s receipt cards and "
                 "%s supplier payments", done["distri_mp_collection"],
                 done["distri_supplier_payment"])
