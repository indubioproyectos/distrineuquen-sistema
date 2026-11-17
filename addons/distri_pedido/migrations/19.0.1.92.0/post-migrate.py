"""19.0.1.92.0, run once on the upgrade.

- «Mostrar cuenta corriente al cliente» (models/cuenta_visible.py). Phase 1
  policy: at launch no customer sees his account; administración turns it
  on one by one. The new column comes in empty; this sets it explicitly to
  false on every partner.
- auth_oauth comes in as a new dependency (resellers' «Entrar con Google»,
  models/alta_revendedor.py) and enables its «Odoo.com Accounts» provider by
  default: it is turned off, nobody signs in with Odoo.com here. The Google
  provider stays disabled until the owner pastes its client id.
"""
import logging

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("UPDATE res_partner SET distri_cuenta_visible = false "
               "WHERE distri_cuenta_visible IS DISTINCT FROM false")
    _logger.warning("19.0.1.92.0 cuenta corriente oculta al cliente en %s contactos", cr.rowcount)
    env = api.Environment(cr, SUPERUSER_ID, {})
    odoo_com = env.ref("auth_oauth.provider_openerp", raise_if_not_found=False)
    if odoo_com and odoo_com.enabled:
        odoo_com.enabled = False
        _logger.warning("19.0.1.92.0 proveedor OAuth «Odoo.com Accounts» desactivado")
