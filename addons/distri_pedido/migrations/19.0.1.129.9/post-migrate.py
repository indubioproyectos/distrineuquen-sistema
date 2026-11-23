"""19.0.1.129.9: the AgilPedido products without a section were stored with
section order 9999 (shown last, under «Otros»); AgilPedido shows them first.
They get SIN_SECCION_ORDEN (-1) now, without waiting for the next sync,
which writes the same value (models/agil_tienda.py filas_agil)."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE product_template
           SET distri_agil_seccion_orden = -1
         WHERE distri_agil_temporal IS TRUE
           AND distri_agil_seccion_orden = 9999""")
    _logger.info("agil: %s products without a section moved first", cr.rowcount)
