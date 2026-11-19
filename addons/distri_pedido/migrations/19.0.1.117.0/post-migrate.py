"""19.0.1.117.0, run once on the upgrade («Cobros en efectivo cargados en
Online», models/caja.py).

The new section of /caja lists the cash payments of Odoo Online of the last
days. The ones dated before the first «Caja del día» belong to no session (the
cash was handled before the caja existed), so the list starts at that day:
distri_pedido.caja_online_desde becomes the date of the first session (today,
Argentine time, when there is none). A value already set is kept.
"""
import logging

_logger = logging.getLogger(__name__)

PARAM = "distri_pedido.caja_online_desde"


def migrate(cr, version):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [PARAM])
    row = cr.fetchone()
    if row and row[0]:
        _logger.info("%s already set to %s", PARAM, row[0])
        return
    cr.execute("SELECT min(date) FROM distri_caja")
    first = cr.fetchone()[0]
    if not first:
        cr.execute("SELECT (now() AT TIME ZONE 'America/Argentina/Buenos_Aires')::date")
        first = cr.fetchone()[0]
    cr.execute("""
        INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
        VALUES (%s, %s, 1, 1, now() AT TIME ZONE 'utc', now() AT TIME ZONE 'utc')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value,
                                        write_date = EXCLUDED.write_date""",
               [PARAM, first.isoformat()])
    _logger.info("%s set to %s", PARAM, first)
