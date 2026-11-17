"""«Alta de revendedor» by self-signup (19.0.1.92.0, controllers/alta_revendedor.py).

A reseller or distributor creates his own portal user from /revendedor: with
his email (auth_signup, free signup «b2c») or with Google (auth_oauth, once
the owner enables the provider). Nobody sends users or passwords.

States of the partner (`distri_alta_estado`):
- «perfil»: the user signed up (set in `_create_user_from_template`, which
  both signup flows use; «Crear acceso web» creates its users directly and
  never gets here). /pedido sends him to «Mi perfil de revendedor»
  (/mi-perfil) until he completes it;
- «pendiente»: profile complete. Gerencia gets an activity on the partner;
  he sees the General list only (`distri_lista_oculta` is still off, so
  controllers/main.py `_calcular_autorizado` never shows a hidden price);
- «habilitado»: a manager enabled him («Habilitar revendedor»): hidden list
  allowed and his price list chosen;
- «rechazado»: a manager refused him; he keeps the General list.
"""
import re

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .ar_phone import normalize_whatsapp

MANAGER_GROUP = "distri_pedido.grupo_gerencia"
ENABLE_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")
ALTA_STATES = [
    ("perfil", "Falta completar el perfil"),
    ("pendiente", "Pendiente de alta"),
    ("habilitado", "Habilitado"),
    ("rechazado", "Rechazado"),
]
CUIT_WEIGHTS = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
PROFILE_FIELDS = ("comercio", "contacto", "cuit", "telefono", "direccion", "localidad")
MAX_LENGTH = 120


def cuit_digits(value):
    """The 11 digits of a CUIT/CUIL, or raises UserError: 11 digits and the
    AFIP check digit (mod 11)."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) != 11:
        raise UserError("El CUIT/CUIL tiene que tener 11 números.")
    total = sum(int(d) * w for d, w in zip(digits[:10], CUIT_WEIGHTS))
    check = 11 - total % 11
    check = 0 if check == 11 else 9 if check == 10 else check
    if check != int(digits[10]):
        raise UserError("El CUIT/CUIL no es válido: revisá los números.")
    return digits


def clean_profile(post):
    """{field: value} of the «Mi perfil de revendedor» form, validated;
    UserError with the message to show otherwise."""
    values = {name: " ".join((post.get(name) or "").split())[:MAX_LENGTH]
              for name in PROFILE_FIELDS}
    missing = [name for name in PROFILE_FIELDS if not values[name]]
    if missing:
        raise UserError("Completá todos los datos.")
    values["cuit"] = cuit_digits(values["cuit"])
    try:
        values["whatsapp"] = normalize_whatsapp(values["telefono"])
    except ValueError as error:
        raise UserError(str(error)) from None
    return values


class ResUsersAltaRevendedor(models.Model):
    _inherit = "res.users"

    def _create_user_from_template(self, values):
        """Signup with email or Google: the new portal user still has to
        complete «Mi perfil de revendedor»."""
        user = super()._create_user_from_template(values)
        if user.share:
            user.partner_id.sudo().distri_alta_estado = "perfil"
        return user


class ResPartnerAltaRevendedor(models.Model):
    _inherit = "res.partner"

    distri_alta_estado = fields.Selection(
        ALTA_STATES, string="Alta de revendedor", copy=False, index=True, tracking=True,
        help="Solo para los que se registraron solos desde la página de pedidos.")
    distri_alta_contacto = fields.Char(string="Nombre y apellido (alta)", copy=False)

    # ------------------------------------------------------------ customer side

    def _distri_save_profile(self, post):
        """Save the profile form on this (the user's own) partner, with sudo
        after validating it; the first time it goes «pendiente» and Gerencia
        gets an activity. Returns the cleaned values."""
        self.ensure_one()
        values = clean_profile(post)
        partner = self.sudo()
        vals = {
            "name": values["comercio"],
            "distri_alta_contacto": values["contacto"],
            "vat": values["cuit"],
            "phone": values["telefono"],
            "distri_whatsapp": values["whatsapp"],
            "street": values["direccion"],
            "city": values["localidad"],
        }
        cuit_type = self.env.ref("l10n_ar.it_cuit", raise_if_not_found=False)
        if cuit_type:
            vals["l10n_latam_identification_type_id"] = cuit_type.id
        country = self.env.ref("base.ar", raise_if_not_found=False)
        if country:
            vals["country_id"] = country.id
        try:
            with self.env.cr.savepoint():
                partner.write(vals)
        except ValidationError as error:
            raise UserError("Revisá los datos: %s" % error.args[0]) from None
        if partner.distri_alta_estado in ("perfil", "rechazado", False):
            partner.distri_alta_estado = "pendiente"
            partner._distri_notify_managers()
        return values

    def _distri_notify_managers(self):
        """«Alta de revendedor pendiente: <comercio>» for every Gerencia user."""
        self.ensure_one()
        group = self.env.ref(MANAGER_GROUP, raise_if_not_found=False)
        managers = self.env["res.users"].sudo().search(
            [("all_group_ids", "in", group.ids), ("share", "=", False)]) \
            if group else self.env["res.users"]
        summary = "Alta de revendedor pendiente: %s" % self.name
        for manager in managers:
            self.sudo().activity_schedule(
                "mail.mail_activity_data_todo", summary=summary, user_id=manager.id,
                note=Markup("Se registró desde la página de pedidos. Revisá los datos y tocá "
                            "«Habilitar revendedor» o «Rechazar» en la ficha."))
        self.sudo().message_post(body=Markup("Completó el perfil de revendedor: CUIT %s, %s. "
                                             "Queda pendiente de alta.") % (
            self.vat or "", self.distri_alta_contacto or ""))

    # ------------------------------------------------------------ manager side

    def _distri_check_enable_rights(self):
        if not (self.env.su or any(self.env.user.has_group(g) for g in ENABLE_GROUPS)):
            raise AccessError("Solo administración y gerencia dan de alta revendedores.")

    def _distri_close_alta_activities(self, feedback):
        activities = self.sudo().activity_ids.filtered(
            lambda a: (a.summary or "").startswith("Alta de revendedor pendiente"))
        if activities:
            activities.action_feedback(feedback=feedback)

    def action_distri_enable_reseller(self):
        """«Habilitar revendedor»: asks the price list."""
        self.ensure_one()
        self._distri_check_enable_rights()
        return {
            "type": "ir.actions.act_window",
            "name": "Habilitar revendedor",
            "res_model": "distri.reseller.enable",
            "view_mode": "form",
            "target": "new",
            "context": {"default_partner_id": self.commercial_partner_id.id},
        }

    def _distri_enable_reseller(self, pricelist):
        self._distri_check_enable_rights()
        for partner in self.commercial_partner_id:
            partner.sudo().write({
                "distri_lista_oculta": True,
                "property_product_pricelist": pricelist.id,
                "distri_alta_estado": "habilitado",
            })
            partner._distri_close_alta_activities("Habilitado por %s" % self.env.user.name)
            partner.message_post(body=Markup(
                "Revendedor <b>habilitado</b> por %s: lista %s.") % (
                self.env.user.name, pricelist.name))
        return True

    def action_distri_reject_reseller(self):
        """«Rechazar»: keeps the General list."""
        self._distri_check_enable_rights()
        for partner in self.commercial_partner_id:
            partner.sudo().write({"distri_alta_estado": "rechazado",
                                  "distri_lista_oculta": False})
            partner._distri_close_alta_activities("Rechazado por %s" % self.env.user.name)
            partner.message_post(body=Markup("Alta de revendedor <b>rechazada</b> por %s.")
                                 % self.env.user.name)
        return True


class DistriResellerEnable(models.TransientModel):
    _name = "distri.reseller.enable"
    _description = "Habilitar revendedor"

    partner_id = fields.Many2one("res.partner", string="Revendedor", required=True,
                                 readonly=True)
    pricelist_id = fields.Many2one("product.pricelist", string="Lista de precios",
                                   required=True)

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        partner = self.env["res.partner"].browse(values.get("partner_id")).exists()
        if partner and "pricelist_id" in fields_list:
            values["pricelist_id"] = partner.property_product_pricelist.id
        return values

    def action_confirm(self):
        self.ensure_one()
        self.partner_id._distri_enable_reseller(self.pricelist_id)
        return {"type": "ir.actions.act_window_close"}
