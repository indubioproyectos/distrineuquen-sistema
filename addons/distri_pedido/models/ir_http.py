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


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

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
