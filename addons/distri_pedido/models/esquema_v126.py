"""Columns added by 19.0.1.126.0, checked before they are touched.

The addons folder is shared with a running server: a restart may load this
code before `-u distri_pedido` has created the new columns. Every new
stored field of 19.0.1.126.0 is declared with prefetch=False (an ordinary
read of the model never selects it) and every piece of code that reads or
writes one asks `v126_ready(env)` first. Until the update runs, crediting
is refused with a clear message and everything else works as in v125.
"""

V126_COLUMNS = (
    ("distri_mp_collection", "credited_by_id"),
    ("distri_mp_collection", "credited_at"),
    ("distri_mp_collection", "audit_state"),
    ("res_users", "distri_aviso_acreditar_visto"),
)
NOT_READY = ("Falta actualizar el módulo Distri (versión 19.0.1.126.0) para acreditar "
             "comprobantes. Avisale a Sistemas.")


def v126_ready(env):
    """True once every 19.0.1.126.0 column exists. Only a positive answer is
    cached on the registry (an update reloads it anyway)."""
    registry = env.registry
    if getattr(registry, "_distri_v126_ready", False):
        return True
    env.cr.execute("""
        SELECT count(*) FROM information_schema.columns
         WHERE table_schema = current_schema()
           AND (table_name, column_name) IN %s
    """, [V126_COLUMNS])
    ready = env.cr.fetchone()[0] == len(V126_COLUMNS)
    if ready:
        registry._distri_v126_ready = True
    return ready
