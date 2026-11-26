import re
import secrets
import string

from odoo import _, fields, models
from odoo.exceptions import UserError

# Largo de la clave que se le manda al comercio por WhatsApp. Corta para que
# se pueda tipear en un celular, pero al azar: dura hasta el primer ingreso.
LARGO_CLAVE = 10
# Sin caracteres que se confunden al leerlos de una pantalla: l/1/I, O/0.
ALFABETO = "abcdefghijkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"


def clave_al_azar():
    return "".join(secrets.choice(ALFABETO) for _ in range(LARGO_CLAVE))


class ResUsersAcceso(models.Model):
    _inherit = "res.users"

    # Mientras este en True, el comercio no puede hacer nada mas que cambiar
    # la clave que le mandamos nosotros.
    distri_debe_cambiar_clave = fields.Boolean(
        string="Tiene que cambiar la contraseña", default=False, copy=False,
    )

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ["distri_debe_cambiar_clave"]


class ResPartnerAcceso(models.Model):
    _inherit = "res.partner"

    distri_usuario_id = fields.Many2one(
        "res.users", string="Acceso web", copy=False, readonly=True,
        help="El usuario con el que este comercio entra a la pagina.",
    )
    # Management decision of 29/09/2026: the hidden list (Distribuidoras and
    # the «Revendedor NN» lists, cigarettes included) is never public. Only a
    # logged-in customer whose company carries this flag sees it on /pedido
    # (controllers/main.py, _calcular_autorizado). Read on the commercial
    # partner, so every contact of the company inherits it. Only
    # Administración (and Gerencia, which implies it) can read or change it.
    distri_lista_oculta = fields.Boolean(
        string="Distribuidora / revendedor (lista oculta)", copy=False, tracking=True,
        groups="distri_pedido.grupo_administracion,base.group_system",
        help="Con la marca, el comercio que entra a la página con su usuario ve la "
             "lista oculta: su lista asignada (May farmacia y varios, Lista 2/3/4 o la de su "
             "revendedor) "
             "o, si tiene la General, la sección DISTRIBUIDORAS con precios «May» y "
             "los cigarrillos a presupuestar. Sin la marca ve solo la lista General.",
    )

    def _login_sugerido(self):
        """El codigo de cliente sirve de usuario: lo tienen 665 de 676."""
        self.ensure_one()
        base = (self.ref or self.name or "").strip().lower()
        base = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
        if not base:
            raise UserError(_("Este contacto no tiene código ni nombre para armar el usuario."))
        login, n = base, 1
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        while Users.search_count([("login", "=", login)]):
            n += 1
            login = "%s-%s" % (base, n)
        return login

    # 19.0.1.129.52 (owner): web access is managed only by the group
    # «Listas y accesos de distribuidores» (models/listas_distri.py), from
    # «Listas y accesos» (/listas) or the backend buttons. Every public
    # method checks it, so nobody else can call them through RPC.

    def _distri_conflicto_cuit(self, cuit):
        """A clear message when the CUIT (11 digits) is already the login of
        another user or the CUIT of another customer; "" when it is free."""
        self.ensure_one()
        socio = self.sudo().commercial_partner_id
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        ocupado = Users.search([("login", "=", cuit),
                                ("id", "!=", socio.distri_usuario_id.id or 0)], limit=1)
        if ocupado:
            return ("El CUIT %s ya es el usuario de «%s»: no se creó nada. Revisá si es el mismo "
                    "cliente cargado dos veces." % (cuit, ocupado.partner_id.name or ocupado.name))
        self.env["res.partner"].flush_model(["vat", "active", "commercial_partner_id"])
        self.env.cr.execute("""
            SELECT id FROM res_partner
             WHERE active AND regexp_replace(coalesce(vat, ''), '[^0-9]', '', 'g') = %s
               AND commercial_partner_id != %s
             ORDER BY id LIMIT 1""", (cuit, socio.id))
        fila = self.env.cr.fetchone()
        if fila:
            otro = self.env["res.partner"].sudo().browse(fila[0])
            return ("El CUIT %s también lo tiene el cliente «%s» (id %s): no se creó nada. "
                    "Revisá si es el mismo cliente cargado dos veces."
                    % (cuit, otro.commercial_partner_id.name, otro.commercial_partner_id.id))
        return ""

    def _distri_login_nuevo(self):
        """With a CUIT, its 11 digits (refused with a clear message when
        another user or customer has it); without one, _login_sugerido."""
        from .listas_distri import cuit_digitos
        self.ensure_one()
        cuit = cuit_digitos(self.sudo().commercial_partner_id.vat)
        if not cuit:
            return self._login_sugerido()
        conflicto = self._distri_conflicto_cuit(cuit)
        if conflicto:
            raise UserError(conflicto)
        return cuit

    def _distri_crear_acceso(self):
        """Creates the portal user. Returns (user, temporary password): the
        password is stored nowhere and must change on the first login."""
        from .listas_distri import check_listas
        check_listas(self.env)
        self.ensure_one()
        socio = self.sudo()
        if socio.distri_usuario_id:
            raise UserError(_("Este comercio ya tiene acceso web: %s")
                            % socio.distri_usuario_id.login)
        login = socio._distri_login_nuevo()
        clave = clave_al_azar()
        usuario = self.env["res.users"].sudo().with_context(
            no_reset_password=True, mail_create_nosubscribe=True,
        ).create({
            "name": socio.name,
            "login": login,
            "password": clave,
            "partner_id": socio.id,
            # portal, no interno: no ocupa licencia y no ve el backoffice
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
            "distri_debe_cambiar_clave": True,
        })
        socio.distri_usuario_id = usuario.id
        socio.message_post(body=_("Acceso web creado por %(quien)s: usuario %(login)s "
                                  "(tiene que cambiar la contraseña al entrar).",
                                  quien=self.env.user.name, login=login))
        return usuario, clave

    def _distri_regenerar_clave(self):
        """New temporary password; reactivates a deactivated access."""
        from .listas_distri import check_listas
        check_listas(self.env)
        self.ensure_one()
        socio = self.sudo()
        usuario = socio.distri_usuario_id.with_context(active_test=False)
        if not usuario:
            raise UserError(_("Este comercio todavía no tiene acceso web."))
        clave = clave_al_azar()
        estaba_activo = usuario.active
        usuario.write({"password": clave, "distri_debe_cambiar_clave": True, "active": True})
        socio.message_post(body=_("Contraseña del acceso web %(login)s regenerada por "
                                  "%(quien)s%(extra)s.", login=usuario.login,
                                  quien=self.env.user.name,
                                  extra="" if estaba_activo else _(" (acceso reactivado)")))
        return usuario, clave

    def _distri_desactivar_acceso(self):
        from .listas_distri import check_listas
        check_listas(self.env)
        self.ensure_one()
        socio = self.sudo()
        usuario = socio.distri_usuario_id.with_context(active_test=False)
        if not usuario:
            raise UserError(_("Este comercio todavía no tiene acceso web."))
        if not usuario.active:
            return False
        usuario.active = False
        socio.message_post(body=_("Acceso web %(login)s desactivado por %(quien)s.",
                                  login=usuario.login, quien=self.env.user.name))
        return True

    def action_crear_acceso_web(self):
        """Le crea el usuario de portal al comercio y devuelve la clave.

        La clave NO se guarda en ningun lado: se muestra una sola vez para
        que el administrador la mande por WhatsApp. Si se pierde, se
        regenera con el otro boton.
        """
        usuario, clave = self._distri_crear_acceso()
        return self._avisar_clave(usuario.login, clave)

    def action_regenerar_clave_web(self):
        usuario, clave = self._distri_regenerar_clave()
        return self._avisar_clave(usuario.login, clave)

    def action_desactivar_acceso_web(self):
        self._distri_desactivar_acceso()
        return True

    def _distri_texto_whatsapp(self, login, clave):
        return ("Hola! Ya tenés acceso a los precios de Distri Neuquén en la página.\n"
                "Usuario: %s\nContraseña: %s\n"
                "La primera vez que entres te va a pedir que elijas una contraseña nueva."
                % (login, clave))

    def _distri_link_whatsapp(self, texto):
        """wa.me link with the message for the customer's WhatsApp (or
        phone), or "" when it has no readable number."""
        from urllib.parse import quote
        from .ar_phone import normalize_whatsapp, only_digits
        socio = self.sudo().commercial_partner_id
        for valor in (socio.distri_whatsapp, socio.phone):
            try:
                numero = normalize_whatsapp(valor)
            except ValueError:
                numero = False
            if numero:
                return "https://wa.me/%s?text=%s" % (only_digits(numero), quote(texto))
        return ""

    def _avisar_clave(self, login, clave):
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": _("Acceso web de %s") % self.name,
                "message": _(
                    "Usuario: %(login)s\nContraseña: %(clave)s\n\n"
                    "Mandásela por WhatsApp. No queda guardada en ningún lado: "
                    "si se pierde, regenerala.",
                    login=login, clave=clave,
                ),
                "sticky": True,
                "type": "warning",
            },
        }
