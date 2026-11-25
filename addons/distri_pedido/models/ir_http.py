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
import logging

from werkzeug.exceptions import HTTPException

from odoo import api, models
from odoo.http import request

_logger = logging.getLogger(__name__)

NAV_HEADER = "X-Distri-Nav"
LOCATION_HEADER = "X-Distri-Location"


# 19.0.1.99.0 (audit F14): our own 403/404 page instead of Odoo's (English
# text, the shop's cart and the sample footer «Somos un equipo...»)
ERROR_CODES = {403: 403, 404: 404, "page_404": 404, "protected_403": 403}

# 19.0.1.129.42 (ui_v2): the kit screens without website=True (caja,
# comprobantes, cuentas, depósito, despacho, cobro, revisar, rutas,
# verificación, mis ventas, fichas, reparto...) answered a refusal with
# werkzeug's bare English page («Forbidden. You don't have the permission»).
# Their 403/404 now get the same branded page as the website routes
# (_distri_kit_error). Paths below these prefixes, plus every declared
# screen of controllers/kit.py SCREEN_PARENTS; never their JSON APIs.
KIT_PREFIXES = (
    "/caja", "/comprobantes", "/cuentas", "/deposito", "/despacho", "/cobro", "/revisar",
    "/rutas", "/verificacion", "/mis-ventas", "/clientes", "/pedidos", "/facturas",
    "/reparto", "/nueva-venta", "/contactos", "/rendiciones", "/equipo", "/armado",
    "/agilpedido", "/asignar", "/tablero", "/proyecto",
)


def _kit_path(path):
    from ..controllers.kit import screen_parents
    if "/api/" in path or path.endswith("/api"):
        return False
    if screen_parents(path) is not None:
        return True
    return any(path == p or path.startswith(p + "/") for p in KIT_PREFIXES)


def _kit_error_hint(env, path):
    """A next step for a refusal: the cash screens are the cashier's; who
    settles routes without being one goes to «Rutas para rendir»."""
    if not path.startswith("/caja"):
        return {}
    from ..controllers.rendiciones import can_open
    if can_open(env):
        return {"hint": "La caja del día es del cajero. Las rutas cerradas por el repartidor "
                        "se rinden desde «Rutas para rendir».",
                "hint_url": "/rendiciones/rutas", "hint_label": "Ir a Rutas para rendir"}
    return {}


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
    def _handle_error(cls, exception):
        response = super()._handle_error(exception)
        try:
            branded = cls._distri_kit_error(response)
        except Exception:  # noqa: BLE001 - never worse than the plain answer
            _logger.exception("distri_pedido: kit error page failed")
            branded = None
        return branded or response

    @classmethod
    def _distri_kit_error(cls, response):
        """19.0.1.129.42 (ui_v2 only): the branded Spanish 403/404 page for a
        kit screen whose route is not website=True (the website ones already
        get it from _get_error_html). Only for a page the browser opened:
        never for fetch() calls (X-Distri-Fetch), JSON routes or APIs."""
        from ..controllers.kit import ui_v2_on
        if not isinstance(response, HTTPException) or response.code not in (403, 404):
            return None
        if not request or getattr(request, "is_frontend", False) or not request.db:
            return None
        http_req = request.httprequest
        if (http_req.headers.get("X-Distri-Fetch") == "1"
                or getattr(request.dispatcher, "routing_type", "") != "http"
                or not _kit_path(http_req.path)):
            return None
        env = request.env
        if env is None or not env.uid:
            return None
        env.cr.rollback()
        if not ui_v2_on(env):
            return None
        user = env.user
        logged = not user._is_public()
        home = (user._distri_landing() or ("/tablero" if not user.share else "/pedido")) \
            if logged else "/"
        values = {"status": response.code, "logged": logged, "home": home,
                  "user_name": user.name if logged else ""}
        if response.code == 403 and logged:
            values.update(_kit_error_hint(env, http_req.path))
        html = env["ir.ui.view"]._render_template("distri_pedido.error_page", values)
        branded = request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])
        branded.status_code = response.code
        return branded

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
