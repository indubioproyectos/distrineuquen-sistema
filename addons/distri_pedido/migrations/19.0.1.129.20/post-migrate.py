"""19.0.1.129.20: internal and external sellers (res.users
distri_tipo_vendedor). The new field starts «interno» for everybody; the two
independent sellers, logins mariana and sebastian, become «externo»: they
only reach the customers who chose them (models/cuentas_app.py,
security/reglas.xml regla_partner_vendedor)."""
from odoo import SUPERUSER_ID, api

EXTERNAL_LOGINS = ("mariana", "sebastian")


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    users = env["res.users"].with_context(active_test=False).search(
        [("login", "in", EXTERNAL_LOGINS)])
    users.write({"distri_tipo_vendedor": "externo"})
