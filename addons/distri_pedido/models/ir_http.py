"""Saving on the legacy pages without stacking history (19.0.1.95.0).

The legacy pages (css/distri_ui.css and the customer pages of
views/templates.xml) save with plain HTML forms. Every native POST, and the
redirect after it, added one browser history entry, so after N saves the
phone's «back» needed N+1 presses. static/src/js/sin_historial.js (included
with the QWeb macro distri_pedido.sin_historial) posts the same form with
fetch() and the header X-Distri-Nav: 1, then replaces the page instead of
adding one. Without JavaScript nothing changes: same forms, same CSRF, same
303 redirects.

The server half lives here. fetch() follows redirects but drops the
#fragment of the Location («/pedido/<token>#aviso») and downloads the target
only for the page to load it again; so a redirect answered to an
X-Distri-Nav request becomes «200 + X-Distri-Location: <the Location>» with
an empty body, and the page goes there with location.replace() (fragment
kept; another site, like Mercado Pago, gets a normal navigation).

A POST that renders HTML instead of redirecting (the same page with an
error, the «¿Es otro cobro?» question, the login with a wrong password,
/pedido when its token expired) is written over the current document with
document.open/write/close, keeping the page's address: no extra entry, no
POST-only URL in the address bar, and no change to those controllers. An
error answer without a form (the one-line pages of /pedido/enviar with
«Volver») is shown as a notice over the page instead, so the cart stays.

Forms, audited for 19.0.1.95.0:
  login_page     /web/login                 intercepted
  mi_clave       /mi-clave                  intercepted
  pantalla       /pedido/enviar             intercepted (own validation runs first)
  seguimiento    /pedido/<token>/pagar      data-native: goes to Mercado Pago
  datos_entrega  /pedido/<token>/entrega x4 intercepted (#aviso/#horarios/#ubicacion kept)
  cobro_page     /cobro/registrar           intercepted; GET search: data-side
  mi_perfil_page /mi-perfil                 intercepted
No intercepted form uploads a file or answers with one. /armado uses the kit
(static/src/kit/dn.js) and does not load this script; /reparto, /despacho and
/cuentas are single-page apps without forms (hash routes) and do not either.
"""
from odoo import api, models
from odoo.http import request

NAV_HEADER = "X-Distri-Nav"
LOCATION_HEADER = "X-Distri-Location"


# 19.0.1.99.0 (audit F14): our own 403/404 page instead of Odoo's (English
# text, the shop's cart and the sample footer «Somos un equipo...»)
ERROR_CODES = {403: 403, 404: 404, "page_404": 404, "protected_403": 403}


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _get_error_html(cls, env, code, values):
        status = ERROR_CODES.get(code)
        if status and not env.user.has_group("base.group_system"):
            user = env.user
            logged = not user._is_public()
            home = (user._distri_landing() or ("/tablero" if not user.share else "/pedido")) \
                if logged else "/"
            return status, env["ir.ui.view"]._render_template("distri_pedido.error_page", {
                "status": status, "logged": logged, "home": home,
                "user_name": user.name if logged else "",
            })
        return super()._get_error_html(env, code, values)

    @classmethod
    def _post_dispatch(cls, response):
        super()._post_dispatch(response)
        if (request.httprequest.headers.get(NAV_HEADER) == "1"
                and 300 <= getattr(response, "status_code", 0) < 400
                and response.headers.get("Location")):
            location = response.headers.pop("Location")
            response.status_code = 200
            response.headers[LOCATION_HEADER] = location
            response.headers["Cache-Control"] = "no-store"
            response.set_data(b"")

    @api.model
    def distri_nav_script(self):
        """URL of sin_historial.js for the distri_pedido.sin_historial macro."""
        from ..controllers.kit import kit_url
        return kit_url("sin_historial.js")

    @api.model
    def distri_ajustes_urls(self):
        """19.0.1.98.0: files of the «Ajustes» gear (views/kit.xml
        distri_pedido.dn_ajustes) and the manifest «Instalar la app» uses
        when the page has none: staff get the lab app, customers /pedido."""
        from ..controllers.kit import kit_url
        user = self.env.user
        staff = not user._is_public() and not user.share
        return {
            "css": kit_url("ajustes.css"),
            "js": kit_url("ajustes.js"),
            # 19.0.1.129.40: the phone keyboard helper (static/src/kit/teclado.js)
            "teclado": kit_url("teclado.js"),
            "manifest": "/tablero/manifest.webmanifest" if staff else "/dn/cliente.webmanifest",
        }


# 19.0.1.99.0 (audit F14): the shop is hidden (controllers/tienda.py sends it
# to /pedido), so its 1,208 product and category URLs leave the sitemap. The
# website_sale rules keep their own sitemap functions even with the routes
# overridden, so they are dropped here, where the sitemap is listed.
SITEMAP_HIDDEN_PREFIXES = ("/shop",)
SITEMAP_HIDDEN = {"/contactus", "/contactus-thank-you", "/website/info",
                  # 19.0.1.104.0: Odoo's sample terms (account), not a page of the site
                  "/terms"}


class WebsiteSitemap(models.Model):
    _inherit = "website"

    def _enumerate_pages(self, query_string=None, force=False):
        for page in super()._enumerate_pages(query_string=query_string, force=force):
            loc = (page.get("loc") or "").split("?", 1)[0]
            if loc in SITEMAP_HIDDEN or loc.startswith(SITEMAP_HIDDEN_PREFIXES):
                continue
            yield page
