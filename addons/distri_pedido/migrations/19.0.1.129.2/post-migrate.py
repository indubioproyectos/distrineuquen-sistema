"""19.0.1.129.2: the driver's cash is credited at the visit
(models/efectivo_a_rendir.py). Decision for the routes in progress at the
upgrade: every delivery route already on the road or closed by the driver
and not settled keeps the old path (`distri_cash_at_settlement`): its cash
becomes customer payments only when a cashier receives it, as before, so no
route ends with some visits credited and others not. Routes still
«Asignada» (no visit possible yet), settled routes and depot boxes are left
alone. Once: a route created later never gets the flag."""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    cr.execute("""
        UPDATE distri_route
           SET distri_cash_at_settlement = TRUE
         WHERE kind = 'route' AND state IN ('on_route', 'closed')
           AND distri_cash_at_settlement IS NOT TRUE
     RETURNING id""")
    ids = [row[0] for row in cr.fetchall()]
    _logger.info("driver cash: %s routes in progress keep the payments at settlement: %s",
                 len(ids), ids)
