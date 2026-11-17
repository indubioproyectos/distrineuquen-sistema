"""«Soy revendedor / Soy distribuidora» (19.0.1.92.0): self-signup pages.

/revendedor offers «Crear cuenta con mi correo» (auth_signup, back to
/mi-perfil) and «Entrar con Google» (auth_oauth, shown only while the Google
provider is enabled and has its client id). /mi-perfil is «Mi perfil de
revendedor»: the rules and the states live in models/alta_revendedor.py.
"""
import json
from urllib.parse import quote_plus, urlencode

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from .main import FICHA

PROFILE_URL = "/mi-perfil"


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

    def _render(self, template, values):
        values = dict({"empresa": FICHA["nombre"], "empresa_id": request.env.company.id}, **values)
        return request.render(template, values)

    @http.route("/revendedor", type="http", auth="public", website=True, sitemap=False)
    def revendedor(self, **kw):
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

    @http.route(PROFILE_URL, type="http", auth="user", website=True, sitemap=False,
                methods=["GET", "POST"])
    def mi_perfil(self, **post):
        user = request.env.user
        if not user.share:
            return request.redirect("/pedido")
        partner = user.partner_id.commercial_partner_id
        error = ""
        if request.httprequest.method == "POST":
            try:
                with request.env.cr.savepoint():
                    partner._distri_save_profile(post)
            except UserError as exc:
                error = exc.args[0]
            else:
                return request.redirect(PROFILE_URL + "?listo=1")
        estado = partner.sudo().distri_alta_estado
        p = partner.sudo()
        form = post if error else {
            "comercio": p.name if estado != "perfil" else "",
            "contacto": p.distri_alta_contacto or (p.name if estado == "perfil" else ""),
            "cuit": p.vat or "", "telefono": p.phone or p.distri_whatsapp or "",
            "direccion": p.street or "", "localidad": p.city or "",
        }
        text = ("Hola, me registré como revendedor en la página de pedidos y quiero que me "
                "habiliten mi lista de precios. Comercio: %s. Nombre: %s. CUIT: %s." % (
                    p.name or "", p.distri_alta_contacto or "", p.vat or ""))
        return self._render("distri_pedido.mi_perfil_page", {
            "form": form,
            "error": error,
            "estado": estado,
            "listo": post.get("listo") == "1" and estado in ("pendiente", "habilitado"),
            "whatsapp": self._whatsapp(text),
            "csrf_token": request.csrf_token(),
        })
