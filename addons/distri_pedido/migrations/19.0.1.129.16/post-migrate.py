"""19.0.1.129.16: the board no longer alerts the incomes of the report
(models/cobros.py _cron_alerts). The open «Cobro sin resolver en el tablero»
activities of the income cards are deleted: only that To-Do on
distri.mp.collection, without a message in the chatter."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["distri.mp.collection"]._distri_drop_income_alerts()
