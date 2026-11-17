"""19.0.1.93.0, run once on the upgrade.

The reseller self-signup becomes private (models/alta_revendedor.py): it only
works with the invitation link /revendedor?c=<code>. The secret code is
created here, so «Invitar revendedor» has a link from the first moment. An
existing code is kept.
"""
import logging
import secrets

_logger = logging.getLogger(__name__)

PARAM_INVITE_CODE = "distri_pedido.revendedor_codigo"


def migrate(cr, version):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = %s", [PARAM_INVITE_CODE])
    row = cr.fetchone()
    if row and row[0]:
        return
    cr.execute("""
        INSERT INTO ir_config_parameter (key, value, create_uid, write_uid, create_date, write_date)
        VALUES (%s, %s, 1, 1, now() at time zone 'UTC', now() at time zone 'UTC')
        ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value""",
               [PARAM_INVITE_CODE, secrets.token_urlsafe(12)])
    _logger.warning("19.0.1.93.0 código de invitación de revendedores creado")
