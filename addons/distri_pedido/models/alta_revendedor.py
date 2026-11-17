"""«Alta de revendedor» by self-signup (19.0.1.92.0, controllers/alta_revendedor.py).

A reseller or distributor creates his own portal user from /revendedor: with
his email (auth_signup, free signup «b2c») or with Google (auth_oauth, once
the owner enables the provider). Nobody sends users or passwords.

19.0.1.93.0 (management request): the signup is PRIVATE. /pedido no longer
advertises it; /revendedor only answers to the invitation link that
gerencia/administración send by WhatsApp, /revendedor?c=<code>, where <code>
is the random secret in the system parameter PARAM_INVITE_CODE («Invitar
revendedor», distri.reseller.invite, shows the link and regenerates it). A
wrong or missing code is a 404. The code is kept in the visitor's session, and
a new user is only created by a signup (email or Google) from a session that
carries the current code (`_signup_create_user`).

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
- «rechazado»: a manager refused him; he keeps the General list. It stays
  so until a manager re-opens it («Reabrir alta», back to «perfil»): he can
  not send the profile again by himself (19.0.1.93.0).

/mi-perfil (19.0.1.93.0): only «perfil» and «pendiente» edit the profile.
Everyone else (enabled resellers, rejected ones, ordinary portal customers)
sees the business name and CUIT read-only, with «Para cambiar estos datos
escribinos por WhatsApp». A CUIT already used by another partner is refused,
the submissions are rate limited per IP and session (models/limites.py), and
the partner gets one open «Alta de revendedor pendiente» activity at most.
"""
import hmac
import logging
import re
import secrets

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from odoo.addons.auth_signup.models.res_partner import SignupError

from .ar_phone import normalize_whatsapp

_logger = logging.getLogger(__name__)

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

# 19.0.1.93.0: private signup by invitation
PARAM_INVITE_CODE = "distri_pedido.revendedor_codigo"
SESSION_INVITE = "distri_revendedor_codigo"
INVITE_PATH = "/revendedor"
# the states that edit «Mi perfil de revendedor»
EDITABLE_STATES = ("perfil", "pendiente")
ALTA_SUMMARY = "Alta de revendedor pendiente"
NOT_INVITED = ("El alta de revendedores es solo con invitación. Escribinos por WhatsApp y te "
               "mandamos el enlace.")
LOCKED_PROFILE = "Para cambiar estos datos escribinos por WhatsApp."
REJECTED_PROFILE = ("Tu alta de revendedor no fue aprobada. Si querés que la revisemos de nuevo, "
                    "escribinos por WhatsApp.")
DUPLICATE_CUIT = ("Ese CUIT ya está registrado con otro cliente. Escribinos por WhatsApp y lo "
                  "resolvemos.")


def new_invite_code():
    """A random secret for the invitation link (16 URL-safe characters)."""
    return secrets.token_urlsafe(12)


def invite_code(env):
    """The current invitation code, created the first time."""
    params = env["ir.config_parameter"].sudo()
    code = params.get_param(PARAM_INVITE_CODE)
    if not code:
        code = new_invite_code()
        params.set_param(PARAM_INVITE_CODE, code)
    return code


def invite_code_ok(env, code):
    """True only for the current code (constant-time comparison)."""
    stored = env["ir.config_parameter"].sudo().get_param(PARAM_INVITE_CODE) or ""
    return bool(stored and code) and hmac.compare_digest(str(code), stored)


def session_invited(env):
    """The visitor opened the current invitation link in this session."""
    return bool(request) and invite_code_ok(env, request.session.get(SESSION_INVITE))


def invite_link(env):
    base = (env["ir.config_parameter"].sudo().get_param("web.base.url") or "").rstrip("/")
    return "%s%s?c=%s" % (base, INVITE_PATH, invite_code(env))


def invite_message(env, link):
    """«Copiar mensaje para WhatsApp»: the short invitation text."""
    return ("Hola! Te paso el enlace para registrarte como revendedor o distribuidora en la "
            "página de pedidos de %s. Creá tu cuenta, completá los datos de tu comercio y, "
            "cuando te habilitemos, vas a ver los precios de tu lista: %s\n"
            "El enlace es personal: no lo compartas." % (env.company.name or "", link))


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

    def _signup_create_user(self, values):
        """A new user from a signup without token (email or Google): only
        from a session that opened the current invitation link (19.0.1.93.0).
        Outside an HTTP request (shell, tests of the ORM) nothing changes."""
        if request and not session_invited(self.env):
            raise SignupError(NOT_INVITED)
        return super()._signup_create_user(values)

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

    def _distri_profile_editable(self):
        """«Mi perfil de revendedor» is editable only in the signup flow."""
        self.ensure_one()
        return self.sudo().distri_alta_estado in EDITABLE_STATES

    def _distri_check_cuit_free(self, cuit):
        """A CUIT already on another partner (any format) is refused."""
        self.ensure_one()
        Partner = self.env["res.partner"].sudo()
        Partner.flush_model(["vat", "commercial_partner_id", "active"])
        self.env.cr.execute("""
            SELECT id FROM res_partner
             WHERE regexp_replace(coalesce(vat, ''), '[^0-9]', '', 'g') = %s
               AND id != %s AND coalesce(commercial_partner_id, id) != %s
             LIMIT 1""", [cuit, self.id, self.id])
        if self.env.cr.fetchone():
            raise UserError(DUPLICATE_CUIT)

    def _distri_save_profile(self, post):
        """Save the profile form on this (the user's own) partner, with sudo
        after validating it. «perfil» goes «pendiente» and Gerencia gets the
        activity; «pendiente» may still correct its data (no new activity).
        Any other state is refused (19.0.1.93.0). Returns the cleaned values."""
        self.ensure_one()
        estado = self.sudo().distri_alta_estado
        if estado == "rechazado":
            raise UserError(REJECTED_PROFILE)
        if estado not in EDITABLE_STATES:
            raise UserError(LOCKED_PROFILE)
        values = clean_profile(post)
        self._distri_check_cuit_free(values["cuit"])
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
        if estado == "perfil":
            partner.distri_alta_estado = "pendiente"
            partner._distri_notify_managers()
        else:
            partner.message_post(body=Markup("Corrigió el perfil de revendedor: CUIT %s, %s.")
                                 % (partner.vat or "", partner.distri_alta_contacto or ""))
        return values

    def _distri_notify_managers(self):
        """One «Alta de revendedor pendiente: <comercio>» activity for the
        partner (19.0.1.93.0: not one per Gerencia user, and never a second
        one while the first is open). It goes to the first Gerencia user;
        everyone sees it in «Revendedores pendientes de alta»."""
        self.ensure_one()
        partner = self.sudo()
        open_alta = partner.activity_ids.filtered(
            lambda a: (a.summary or "").startswith(ALTA_SUMMARY))
        manager = self.env["sale.order"].sudo()._distri_usuarios_gerencia()[:1]
        if manager and not open_alta:
            partner.with_context(mail_activity_quick_update=True).activity_schedule(
                "mail.mail_activity_data_todo", summary="%s: %s" % (ALTA_SUMMARY, partner.name),
                user_id=manager.id,
                note=Markup("Se registró con el enlace de invitación. Revisá los datos y tocá "
                            "«Habilitar revendedor» o «Rechazar» en la ficha."))
        partner.message_post(body=Markup("Completó el perfil de revendedor: CUIT %s, %s. "
                                         "Queda pendiente de alta.") % (
            partner.vat or "", partner.distri_alta_contacto or ""))

    # ------------------------------------------------------------ manager side

    def _distri_check_enable_rights(self):
        if not (self.env.su or any(self.env.user.has_group(g) for g in ENABLE_GROUPS)):
            raise AccessError("Solo administración y gerencia dan de alta revendedores.")

    def _distri_close_alta_activities(self, feedback):
        activities = self.sudo().activity_ids.filtered(
            lambda a: (a.summary or "").startswith(ALTA_SUMMARY))
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


    def action_distri_reopen_reseller(self):
        """«Reabrir alta» (19.0.1.93.0): a rejected reseller may send his
        profile again; he cannot re-open it himself."""
        self._distri_check_enable_rights()
        for partner in self.commercial_partner_id:
            if partner.distri_alta_estado != "rechazado":
                raise UserError("Solo se reabre un alta rechazada.")
            partner.sudo().distri_alta_estado = "perfil"
            partner.message_post(body=Markup(
                "Alta de revendedor <b>reabierta</b> por %s: puede volver a mandar su perfil.")
                % self.env.user.name)
        return True

    @api.model
    def action_distri_reseller_invite(self):
        """«Invitar revendedor» from the pending list."""
        return self.env["distri.reseller.invite"].action_open()


class DistriResellerInvite(models.TransientModel):
    """«Invitar revendedor» (19.0.1.93.0): the private signup link to send by
    WhatsApp, the prefilled message, and «Generar un enlace nuevo» (the old
    link stops working)."""
    _name = "distri.reseller.invite"
    _description = "Invitar revendedor"

    link = fields.Char(string="Enlace de invitación", readonly=True)
    message = fields.Text(string="Mensaje para WhatsApp", readonly=True)

    def _check_rights(self):
        self.env["res.partner"]._distri_check_enable_rights()

    @api.model
    def default_get(self, fields_list):
        self._check_rights()
        values = super().default_get(fields_list)
        link = invite_link(self.env)
        values.update({"link": link, "message": invite_message(self.env, link)})
        return values

    @api.model
    def action_open(self):
        self._check_rights()
        return {
            "type": "ir.actions.act_window",
            "name": "Invitar revendedor",
            "res_model": self._name,
            "view_mode": "form",
            "target": "new",
        }

    def action_regenerate(self):
        """A new code: the links already sent stop working (404)."""
        self._check_rights()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_INVITE_CODE, new_invite_code())
        _logger.warning("distri_pedido: %s generated a new reseller invitation code",
                        self.env.user.login)
        return self.action_open()


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
