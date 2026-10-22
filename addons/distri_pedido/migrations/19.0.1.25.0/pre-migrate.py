"""«Transferencias prometidas» become «Comprobantes pendientes» (design
approved by management on 22/10/2026, models/comprobante_pendiente.py).

Before the new selections load, old values are mapped:
- pending receipt states: «Sin comprobante» -> «Pendiente», «En cobranza»
  -> «Vencida»; «Transfiere después» -> a transfer.
- visits: «Transfiere después» (state promised) -> «Pagó»; transfer
  «Ya transfirió» / «Transfiere después» -> «Sí».
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("UPDATE distri_promised_transfer SET state = 'open' WHERE state = 'no_receipt'")
    opened = cr.rowcount
    cr.execute("UPDATE distri_promised_transfer SET state = 'overdue' WHERE state = 'collection'")
    overdue = cr.rowcount
    cr.execute("UPDATE distri_promised_transfer SET kind = 'done' WHERE kind = 'later'")
    cr.execute("UPDATE distri_route_stop SET state = 'paid' WHERE state = 'promised'")
    stops = cr.rowcount
    cr.execute("UPDATE distri_route_stop SET transfer = 'yes' WHERE transfer IN ('done', 'later')")
    _logger.info("Comprobantes pendientes: %s reopened, %s overdue, %s visits remapped",
                 opened, overdue, stops)
