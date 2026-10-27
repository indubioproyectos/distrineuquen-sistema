"""Migration board (19.0.1.41.0, models/migracion.py).

- `distri.online.payment.linked_at` is new: the links already made get the
  moment they were written, so the board lists the links of the last sync.
- «Última sincronización con Online» is the parameter
  distri_pedido.online_sync_at, written by every sync from now on; before,
  the last `synced_at` of the mirror is the best record of it.
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    cr.execute("""UPDATE distri_online_payment
                     SET linked_at = date_trunc('second', write_date)
                   WHERE collection_id IS NOT NULL AND linked_at IS NULL""")
    env = api.Environment(cr, SUPERUSER_ID, {})
    params = env["ir.config_parameter"].sudo()
    if not params.get_param("distri_pedido.online_sync_at"):
        cr.execute("SELECT max(synced_at) FROM distri_online_payment")
        last = cr.fetchone()[0]
        if last:
            params.set_param("distri_pedido.online_sync_at", last.strftime("%Y-%m-%d %H:%M:%S"))
