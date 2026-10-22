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
             "lista oculta: su lista asignada (Distribuidoras o la de su revendedor) "
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

    def action_crear_acceso_web(self):
        """Le crea el usuario de portal al comercio y devuelve la clave.

        La clave NO se guarda en ningun lado: se muestra una sola vez para
        que el administrador la mande por WhatsApp. Si se pierde, se
        regenera con el otro boton.
        """
        self.ensure_one()
        if self.distri_usuario_id:
            raise UserError(_("Este comercio ya tiene acceso web: %s")
                            % self.distri_usuario_id.login)
        clave = clave_al_azar()
        usuario = self.env["res.users"].sudo().with_context(
            no_reset_password=True, mail_create_nosubscribe=True,
        ).create({
            "name": self.name,
            "login": self._login_sugerido(),
            "password": clave,
            "partner_id": self.id,
            # portal, no interno: no ocupa licencia y no ve el backoffice
            "group_ids": [(6, 0, [self.env.ref("base.group_portal").id])],
            "distri_debe_cambiar_clave": True,
        })
        self.distri_usuario_id = usuario.id
        return self._avisar_clave(usuario.login, clave)

    def action_regenerar_clave_web(self):
        self.ensure_one()
        if not self.distri_usuario_id:
            raise UserError(_("Este comercio todavía no tiene acceso web."))
        clave = clave_al_azar()
        self.distri_usuario_id.sudo().write({
            "password": clave,
            "distri_debe_cambiar_clave": True,
        })
        return self._avisar_clave(self.distri_usuario_id.login, clave)

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
