from odoo import http
from odoo.http import request

# the DN app icons, same files and query as the public site (controllers/sitio.py)
LOGO = "/distri_pedido/static/src/img/iconos/"


def project_strip(code):
    """Values for the shared stage strip (template distri_pedido.proyecto_franja)."""
    return request.env["distri.project.module"].project_strip(code)


class Proyecto(http.Controller):

    @http.route("/proyecto", type="http", auth="public", website=True, sitemap=False)
    def proyecto(self, **kw):
        """«Capacitación Distri Neuquén» (19.0.1.114.0): the staff training
        page, one role at a time, with the former project hub collapsed in
        «Historia del proyecto» (distri.project.module rows). Public, noindex,
        standalone like the public site (no website layout)."""
        # imported here: controllers/main.py imports this module before
        # kit/reparto can be loaded (reparto imports main)
        from .kit import LOGO_V, kit_url, kit_version
        from .reparto import fonts

        version = kit_version()
        response = request.render("distri_pedido.proyecto", {
            "modulos": request.env["distri.project.module"].project_rows(),
            "css": kit_url("proyecto.css", version),
            "js": kit_url("proyecto.js", version),
            "font": (fonts() or [""])[0],
            "logo": LOGO,
            "logo_v": LOGO_V,
        })
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    @http.route("/presentacion/migracion", type="http", auth="public", website=True,
                sitemap=False)
    def presentacion_migracion(self, **kw):
        """Why the project started with the catalog, and what the migration implies."""
        return request.render("distri_pedido.presentacion_migracion", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("migracion"),
        })
