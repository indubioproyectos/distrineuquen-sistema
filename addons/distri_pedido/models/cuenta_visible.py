"""«Mostrar cuenta corriente al cliente» (19.0.1.92.0).

Phase 1 policy: the customer does not see his account (balance and
movements) until administración turns it on for him. Everyone starts hidden
(migrations/19.0.1.92.0). The flag lives on the commercial partner; a
contact follows its company.

Hidden means only what the CUSTOMER sees: /pedido/<token>/cuenta and
/mi-cuenta show a neutral page, the «Ver mi cuenta corriente» links and the
amounts of the «cliente en rojo» box are not shown, and the order WhatsApp
text does not promise the account (controllers/main.py). No operation is
restricted: ordering, changes, delivery and payments work as before, and
staff, drivers and sellers see the account as always.
"""
from markupsafe import Markup

from odoo import fields, models
from odoo.exceptions import AccessError

TOGGLE_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")


class ResPartnerCuentaVisible(models.Model):
    _inherit = "res.partner"

    distri_cuenta_visible = fields.Boolean(
        string="Mostrar cuenta corriente al cliente", default=False, copy=False,
        help="Con la marca, el cliente ve su saldo y sus movimientos en el link del pedido y "
             "en «Mi cuenta». Sin ella ve un aviso neutro. No cambia nada para el personal, "
             "los repartidores ni los vendedores.")

    def _distri_account_visible(self):
        """Whether the customer himself may see his account."""
        self.ensure_one()
        partner = self.sudo().commercial_partner_id
        return bool(partner.distri_cuenta_visible) and not partner.distri_generic_partner

    def _distri_set_account_visible(self, visible):
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in TOGGLE_GROUPS)):
            raise AccessError("Solo administración y gerencia muestran u ocultan la cuenta "
                              "al cliente.")
        for partner in self.commercial_partner_id:
            if partner.distri_cuenta_visible == visible:
                continue
            partner.sudo().distri_cuenta_visible = visible
            partner.message_post(body=Markup(
                "Cuenta corriente <b>%s</b> para el cliente. Lo cambió %s.") % (
                "visible" if visible else "oculta", user.name))
        return True

    def action_distri_show_account(self):
        """«Mostrar cuenta al cliente»."""
        return self._distri_set_account_visible(True)

    def action_distri_hide_account(self):
        """«Ocultar cuenta al cliente»."""
        return self._distri_set_account_visible(False)
