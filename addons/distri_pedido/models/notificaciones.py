"""19.0.1.129.52 (owner, 26/11/2026): Odoo's notifications to staff (an
activity assigned, «Te asignaron S00504», a mention...) stay in Odoo's inbox
and are never e-mailed. Every new internal user starts with
notification_type «inbox», whoever creates it (the backend, «Equipo», data
files, practice); migrations/19.0.1.129.52 sets it on the existing ones.
Portal users are left alone: Odoo requires «email» for them.
"""
from odoo import api, models


def poner_bandeja(users):
    """Internal users of `users` that still get e-mails switch to the inbox."""
    internos = users.filtered(lambda u: not u.share and u.notification_type != "inbox")
    if internos:
        internos.sudo().write({"notification_type": "inbox"})
    return internos


class ResUsersNotificaciones(models.Model):
    _inherit = "res.users"

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        poner_bandeja(users)
        return users
