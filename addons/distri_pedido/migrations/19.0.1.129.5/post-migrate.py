"""19.0.1.129.5: orders from before «Enviar a la empresa» existed count as
sent (`distri_enviado_empresa` True, no date): the step did not exist then,
and the warning and the «No enviado a la empresa» filter are for the orders
taken from now on."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE sale_order
           SET distri_enviado_empresa = TRUE
         WHERE distri_enviado_empresa IS NOT TRUE""")
    _logger.info("enviar a la empresa: %s earlier orders marked as sent", cr.rowcount)
