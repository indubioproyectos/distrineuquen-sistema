"""19.0.1.122.0 «Rutas anteriores» of the driver app (/reparto): his own
routes of the last days, read only (models/reparto_anteriores.py).

Plain server pages with the look of the app (distri_ui.css and the app's
app.css), online only: the service worker does not cache them. No form, no
button that changes anything. A route that is not one of the user's past
routes answers 404, whoever asks.
"""
from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.http import request

from .main import FICHA
from .reparto import GROUP_DRIVER, SHARED_CSS, STATIC, asset_version
from ..models.reparto_anteriores import PAST_DAYS


class RepartoAnteriores(http.Controller):

    def _check_driver(self):
        if not request.env.user.has_group(GROUP_DRIVER):
            raise Forbidden()

    def _render(self, template, values):
        values.update({
            "version": asset_version(),
            "static": STATIC,
            "shared_css": SHARED_CSS,
            "empresa": FICHA["nombre"],
            "dias": PAST_DAYS,
        })
        html = request.env["ir.qweb"]._render(template, values)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])

    @http.route("/reparto/anteriores", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def past_routes(self, **kw):
        self._check_driver()
        Route = request.env["distri.route"]
        routes = Route._distri_past_routes(request.env.user)
        return self._render("distri_pedido.reparto_anteriores", {
            "rutas": [route._distri_past_summary() for route in routes],
        })

    @http.route("/reparto/anteriores/<int:route_id>", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def past_route(self, route_id, **kw):
        self._check_driver()
        route = request.env["distri.route"]._distri_past_route(request.env.user, route_id)
        if not route:
            raise NotFound()
        return self._render("distri_pedido.reparto_anterior", {
            "ruta": route._distri_past_summary(),
            "visitas": route._distri_past_stops(),
        })
