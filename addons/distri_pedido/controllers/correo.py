"""19.0.1.129.51: «Mi correo» and «¿Olvidaste la contraseña?» (see
models/correo_verificado.py for the rules).

- /mi-correo (staff, from «Ajustes»): the confirmed address, «Cambiar»,
  the code step. Portal customers go to /mi-cuenta (not in scope).
- /clave/olvide (public): user or e-mail; always the same neutral answer.
- /clave/nueva?t=<token> (public): a new password with the rules of
  /mi-clave; the link works once and expires.
The public pages look like the custom login (views/correo.xml, login_estilo).
"""
from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..models.correo_verificado import (CODE_MINUTES, mask_email, password_problem)

NEUTRAL = ("Si el usuario tiene un correo confirmado, le mandamos un enlace para elegir una "
           "contraseña nueva. Revisá tu correo (también la carpeta de spam).")
NO_EMAIL_HINT = "Si no cargaste tu correo, pedí a administración que te la restablezca."
LINK_DEAD = "El enlace venció o ya se usó. Pedí uno nuevo."


class Correo(http.Controller):

    # ------------------------------------------------------------ staff

    def _mi_correo_values(self, user, **extra):
        user = user.sudo()
        pending = user._distri_pending_code()
        values = {
            "login": user.login,
            "verificado": user._distri_verified_email(),
            "fecha": user.distri_email_verificado_fecha,
            "pendiente": pending.email if pending else "",
            "minutos": CODE_MINUTES,
            "ok": "",
            "error": "",
            "cambiar": False,
            "escrito": "",
        }
        values.update(extra)
        return values

    @http.route("/mi-correo", type="http", auth="user", website=True, sitemap=False,
                methods=["GET", "POST"])
    def mi_correo(self, **post):
        user = request.env.user
        if user.share:
            return request.redirect("/mi-cuenta")
        extra = {}
        if request.httprequest.method == "POST":
            action = post.get("accion") or ""
            try:
                if action == "enviar":
                    record = user._distri_request_code(post.get("email"))
                    extra["ok"] = "Te mandamos un código a %s." % record.email
                elif action == "reenviar":
                    pending = user.sudo()._distri_pending_code()
                    if not pending:
                        raise UserError("No hay un código pendiente. Escribí tu correo.")
                    record = user._distri_request_code(pending.email)
                    extra["ok"] = "Te mandamos otro código a %s." % record.email
                elif action == "confirmar":
                    email = user._distri_confirm_code(post.get("codigo"))
                    extra["ok"] = "Listo: tu correo %s quedó confirmado." % email
                elif action == "cancelar":
                    user.sudo()._distri_pending_code().write({"estado": "anulado"})
            except UserError as exc:
                extra["error"] = str(exc.args[0] if exc.args else exc)
                if action == "enviar":
                    extra["cambiar"] = True
                    extra["escrito"] = (post.get("email") or "").strip()[:254]
        return request.render("distri_pedido.mi_correo", self._mi_correo_values(user, **extra))

    # ------------------------------------------------------------ public

    def _public_values(self, **extra):
        values = {"ok": "", "error": "", "aviso": "", "escrito": "", "muerto": "", "hecho": False}
        values.update(extra)
        return values

    @http.route("/clave/olvide", type="http", auth="public", website=True, sitemap=False,
                methods=["GET", "POST"])
    def clave_olvide(self, **post):
        if request.httprequest.method != "POST":
            return request.render("distri_pedido.clave_olvide", self._public_values(
                escrito=(post.get("login") or "").strip()[:80]))
        typed = (post.get("login") or "").strip()[:254]
        if typed:
            base_url = request.env["ir.config_parameter"].sudo().get_param("web.base.url")
            request.env["res.users"].sudo()._distri_request_reset(
                typed, request.httprequest.remote_addr or "", base_url)
            return request.render("distri_pedido.clave_olvide", self._public_values(
                ok=NEUTRAL, aviso=NO_EMAIL_HINT))
        return request.render("distri_pedido.clave_olvide", self._public_values(
            error="Escribí tu usuario o tu correo."))

    @http.route("/clave/nueva", type="http", auth="public", website=True, sitemap=False,
                methods=["GET", "POST"])
    def clave_nueva(self, t="", **post):
        token = request.env["distri.clave.token"].sudo()._live(t)
        values = self._public_values(t=t, login="")
        if not token:
            values["muerto"] = LINK_DEAD
            return request.render("distri_pedido.clave_nueva", values)
        values["login"] = token.user_id.login
        if request.httprequest.method == "POST":
            new = (post.get("nueva") or "").strip()
            repeated = (post.get("repetida") or "").strip()
            problem = password_problem(new, repeated)
            if problem:
                values["error"] = problem
            else:
                token._use(new)
                values.update(t="", hecho=True,
                              ok="Listo: tu contraseña nueva ya funciona. Entrá con ella.")
        return request.render("distri_pedido.clave_nueva", values)


def masked_or_full(viewer, address):
    """Sistemas sees the address; everybody else the masked one."""
    if not address:
        return ""
    return address if viewer.has_group("base.group_system") else mask_email(address)
