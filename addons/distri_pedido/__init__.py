from . import controllers
from . import models


def post_init_hook(env):
    """19.0.1.129.26 (#109): what migrations did on the existing database
    and a fresh install missed: remitos numbered R-NNNNN (migrations/
    19.0.1.3.0) and the «Odoo.com Accounts» login provider turned off
    (migrations/19.0.1.92.0). Runs only on install, never on an update."""
    from .models.remito import numerar_remitos

    numerar_remitos(env)
    odoo_com = env.ref("auth_oauth.provider_openerp", raise_if_not_found=False)
    if odoo_com and odoo_com.enabled:
        odoo_com.enabled = False
