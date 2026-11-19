"""19.0.1.118.0, run once on the upgrade («Cobro registrado en la factura»,
models/caja.py).

From now on a cash payment the cashier registers himself on the invoice
(«Registrar pago» in the caja's cash journal) enters his open «Caja del día».
The payments registered before this rule belong to no session (they were
handled outside the caja), so distri_pedido.caja_factura_desde becomes the
day the rule starts, 2026-11-19. A value already set is kept.
"""
import logging

_logger = logging.getLogger(__name__)

PARAM = "distri_pedido.caja_factura_desde"
SINCE = "2026-11-19"


def migrate(cr, version):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [PARAM])
    row = cr.fetchone()
    if row and row[0]:
        _logger.info("%s already set to %s", PARAM, row[0])
        return
    cr.execute("""
        INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
        VALUES (%s, %s, 1, 1, now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                        write_date = EXCLUDED.write_date""",
               [PARAM, SINCE])
    _logger.info("%s set to %s", PARAM, SINCE)
