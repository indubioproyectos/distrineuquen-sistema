from odoo import api, fields, models
from odoo.fields import Domain
from odoo.exceptions import UserError

# Solo estos roles ven la plata del cliente. El armador y el reparto ven
# el contacto, pero no el saldo.
VE_SALDOS = (
    "distri_pedido.grupo_vendedor,"
    "distri_pedido.grupo_administracion,"
    "distri_pedido.grupo_gerencia"
)

# Web orders without a customer are filed under one generic contact per list.
# They are identified by the distri_generic_partner flag, never by name: the
# names below are only used to create a missing generic.
GENERIC_RETAIL = "retail"
GENERIC_WHOLESALE = "wholesale"
GENERIC_NAMES = {
    GENERIC_RETAIL: "Pedidos web - sin asignar",
    GENERIC_WHOLESALE: "Pedidos web - lista distribuidoras - sin asignar",
}

GENERIC_LOCKED_MESSAGE = (
    "Este es el contacto genérico de los pedidos web sin cliente. No se "
    "modifica: para asignar el cliente, abrí el pedido y tocá «Asignar cliente»."
)

# Identity fields of a generic contact. Changing any of them would silently
# re-label every unassigned web order (the "CASTAÑEDA JUAN" incident).
GENERIC_LOCKED_FIELDS = frozenset({
    "name", "complete_name", "vat", "ref", "company_registry", "phone", "email",
    "website", "street", "street2", "city", "zip", "state_id", "country_id",
    "company_type", "is_company", "parent_id", "type", "active", "company_id",
    "l10n_latam_identification_type_id", "l10n_ar_afip_responsibility_type_id",
    "distri_generic_partner", "distri_generic_kind",
})

# Context key that lets a system administrator edit a generic on purpose
# (developer tools or odoo shell). The regular forms never set it.
GENERIC_UNLOCK_KEY = "distri_generic_unlock"


class ResPartner(models.Model):
    _inherit = "res.partner"

    saldo_odoo = fields.Monetary(
        string="Saldo en Odoo real",
        currency_field="currency_id",
        groups=VE_SALDOS,
        help="Saldo de cuenta corriente traido del Odoo de produccion.",
    )
    facturas_sin_conciliar = fields.Integer(
        string="Facturas sin conciliar",
        groups=VE_SALDOS,
        help="Cantidad de facturas que figuran impagas por falta de conciliacion.",
    )
    monto_sin_conciliar = fields.Monetary(
        string="Importe sin conciliar",
        currency_field="currency_id",
        groups=VE_SALDOS,
    )

    distri_generic_partner = fields.Boolean(
        string="Contacto genérico de pedidos web", copy=False, index=True,
        help="Los pedidos de la página sin cliente entran a nombre de este "
             "contacto. No se edita: al pedido se le asigna el cliente.",
    )
    distri_generic_kind = fields.Selection(
        [(GENERIC_RETAIL, "Pedidos web sin asignar"),
         (GENERIC_WHOLESALE, "Lista distribuidoras sin asignar")],
        string="Tipo de contacto genérico", copy=False,
    )

    # ------------------------------------------------------------ lookup

    @api.model
    def _distri_generic_partner_for(self, wholesale=False):
        """The generic contact for web orders without a customer.

        Found by flag. If none exists (new database, or someone got rid of
        it), a flagged one is created so the web order is never lost.
        """
        kind = GENERIC_WHOLESALE if wholesale else GENERIC_RETAIL
        Partner = self.sudo()
        partner = Partner.search([
            ("distri_generic_partner", "=", True),
            ("distri_generic_kind", "=", kind),
        ], order="id", limit=1)
        if not partner:
            partner = Partner.create({
                "name": GENERIC_NAMES[kind],
                "company_type": "company",
                "distri_generic_partner": True,
                "distri_generic_kind": kind,
            })
        return partner

    # ------------------------------------------------------------ guard

    def _distri_generic_unlocked(self):
        if self.env.su:
            return True
        return bool(self.env.context.get(GENERIC_UNLOCK_KEY)) and \
            self.env.user.has_group("base.group_system")

    def _distri_generic_changes(self, vals):
        """Locked fields of generic contacts that this write would change."""
        locked = GENERIC_LOCKED_FIELDS.intersection(vals)
        if not locked:
            return []
        changed = []
        for partner in self.sudo().filtered("distri_generic_partner"):
            for fname in locked:
                if fname not in partner._fields:
                    continue
                field = partner._fields[fname]
                current = field.convert_to_write(partner[fname], partner)
                new = vals[fname]
                if isinstance(new, models.BaseModel):
                    new = new.id
                if (current or False) != (new or False):
                    changed.append(fname)
        return changed

    def write(self, vals):
        if not self._distri_generic_unlocked() and self._distri_generic_changes(vals):
            raise UserError(GENERIC_LOCKED_MESSAGE)
        return super().write(vals)

    @api.ondelete(at_uninstall=False)
    def _distri_unlink_except_generic(self):
        if not self._distri_generic_unlocked() and \
                self.sudo().filtered("distri_generic_partner"):
            raise UserError(GENERIC_LOCKED_MESSAGE)

    # ------------------------------------------------------------ search

    @api.model
    def _search_display_name(self, operator, value):
        """With distri_search_phone in the context, also match the phone.

        Used by the "Asignar cliente" wizard: the buyer on the web form
        often only left a phone number.
        """
        domain = super()._search_display_name(operator, value)
        if (self.env.context.get("distri_search_phone") and operator == "ilike"
                and isinstance(value, str)
                and sum(c.isdigit() for c in value) >= 6
                and "phone_mobile_search" in self._fields):
            digits = "".join(c for c in value if c.isdigit())
            domain = Domain.OR([domain, [("phone_mobile_search", "ilike", digits)]])
        return domain
