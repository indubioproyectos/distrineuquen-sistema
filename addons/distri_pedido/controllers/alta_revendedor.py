"""Reseller self-signup pages (19.0.1.92.0).

19.0.1.93.0: private. /revendedor answers only to the invitation link
/revendedor?c=<code> (models/alta_revendedor.py PARAM_INVITE_CODE); without
the right code it is a 404, like an unknown page. The code stays in the
session: /web/signup and the Google signup only create a user from that
session (AuthSignupHome.get_auth_signup_config below, and
res.users._signup_create_user). Every page carries noindex.

/revendedor offers «Crear cuenta con mi correo» (auth_signup, back to
/mi-perfil) and «Entrar con Google» (auth_oauth, shown only while the Google
provider is enabled and has its client id). /mi-perfil is «Mi perfil de
revendedor»: the rules and the states live in models/alta_revendedor.py.
"""
import json
from urllib.parse import quote_plus, urlencode

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from odoo.addons.auth_signup.controllers.main import AuthSignupHome

from ..models.alta_revendedor import (
    EDITABLE_STATES, SESSION_INVITE, invite_code_ok, session_invited,
)
from .main import FICHA

PROFILE_URL = "/mi-perfil"
NOINDEX = ("X-Robots-Tag", "noindex, nofollow")
# «Mi perfil de revendedor» submissions (models/limites.py)
PROFILE_LIMITS = {
    "ip_10min": ("distri_pedido.perfil_limite_ip_10min", 10, None),
    "ip_day": ("distri_pedido.perfil_limite_ip_dia", 40, None),
    "session_10min": ("distri_pedido.perfil_limite_sesion_10min", 5, None),
}


class SignupSoloConInvitacion(AuthSignupHome):
    """The free signup («b2c») only for a session that opened the current
    invitation link (19.0.1.93.0): /web/signup is a 404 otherwise and the
    login page shows no «create an account». A signup by token (an
    invitation sent by Odoo) works as before."""

    def get_auth_signup_config(self):
        config = super().get_auth_signup_config()
        if config.get("signup_enabled") and not session_invited(request.env):
            config["signup_enabled"] = False
        return config


class AltaRevendedor(http.Controller):

    def _whatsapp(self, text):
        return request.env["distri.pedido.whatsapp.settings"].sudo().company_url(text)

    def _google_link(self):
        """The Google sign-in URL, like auth_oauth builds it (OAuthLogin
        list_providers), landing back on /mi-perfil; "" while the provider
        is disabled or has no client id."""
        provider = request.env.ref("auth_oauth.provider_google", raise_if_not_found=False)
        provider = provider.sudo() if provider else None
        if not provider or not provider.enabled or not provider.client_id:
            return ""
        root = request.httprequest.url_root
        state = {"d": request.session.db, "p": provider.id,
                 "r": quote_plus(root + PROFILE_URL.lstrip("/"))}
        params = {"response_type": "token", "client_id": provider.client_id,
                  "redirect_uri": root + "auth_oauth/signin", "scope": provider.scope,
                  "state": json.dumps(state)}
        return "%s?%s" % (provider.auth_endpoint, urlencode(params))

    def _render(self, template, values, status=200):
        values = dict({"empresa": FICHA["nombre"], "empresa_id": request.env.company.id}, **values)
        response = request.render(template, values, status=status)
        response.headers.set(*NOINDEX)
        return response

    @http.route("/revendedor", type="http", auth="public", website=True, sitemap=False)
    def revendedor(self, c="", **kw):
        if not invite_code_ok(request.env, c):
            raise NotFound()
        request.session[SESSION_INVITE] = c
        user = request.env.user
        if not user._is_public():
            if user.share and user.partner_id.sudo().distri_alta_estado == "perfil":
                return request.redirect(PROFILE_URL)
            return request.redirect("/pedido")
        return self._render("distri_pedido.revendedor_page", {
            "signup": "/web/signup?redirect=%s" % quote_plus(PROFILE_URL),
            "login": "/web/login?redirect=%2Fpedido",
            "google": self._google_link(),
            "whatsapp": self._whatsapp("Hola, soy revendedor/distribuidora y quiero ver mi "
                                       "lista de precios en la página de pedidos."),
        })

    def _limit_reached(self):
        return request.env["distri.rate.hit"].sudo()._distri_check(
            "perfil", request.httprequest.remote_addr, request.session.sid,
            limits=PROFILE_LIMITS)

    @http.route(PROFILE_URL, type="http", auth="user", website=True, sitemap=False,
                methods=["GET", "POST"])
    def mi_perfil(self, **post):
        user = request.env.user
        if not user.share:
            return request.redirect("/pedido")
        partner = user.partner_id.commercial_partner_id
        p = partner.sudo()
        estado = p.distri_alta_estado
        editable = estado in EDITABLE_STATES
        error = ""
        status = 200
        if request.httprequest.method == "POST":
            if self._limit_reached():
                error = ("Recibimos muchos envíos seguidos. Esperá unos minutos y probá de nuevo, "
                         "o escribinos por WhatsApp.")
                status = 429
            else:
                try:
                    with request.env.cr.savepoint():
                        partner._distri_save_profile(post)
                except UserError as exc:
                    error = exc.args[0]
                    status = 200 if editable else 403
                else:
                    return request.redirect(PROFILE_URL + "?listo=1")
        form = post if error and editable else {
            "comercio": p.name if estado != "perfil" else "",
            "contacto": p.distri_alta_contacto or (p.name if estado == "perfil" else ""),
            "cuit": p.vat or "", "telefono": p.phone or p.distri_whatsapp or "",
            "direccion": p.street or "", "localidad": p.city or "",
        }
        if editable:
            text = ("Hola, me registré como revendedor en la página de pedidos y quiero que me "
                    "habiliten mi lista de precios. Comercio: %s. Nombre: %s. CUIT: %s." % (
                        p.name or "", p.distri_alta_contacto or "", p.vat or ""))
        else:
            text = ("Hola, quiero cambiar los datos de mi cuenta en la página de pedidos. "
                    "Cliente: %s. CUIT: %s." % (p.name or "", p.vat or ""))
        return self._render("distri_pedido.mi_perfil_page", {
            "form": form,
            "error": error,
            "estado": estado,
            # not "editable": website overrides that name in the render context
            "puede_editar": editable,
            "listo": post.get("listo") == "1" and estado in ("pendiente", "habilitado"),
            "whatsapp": self._whatsapp(text),
            "csrf_token": request.csrf_token(),
        }, status=status)
