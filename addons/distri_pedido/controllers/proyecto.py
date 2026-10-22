from odoo import http
from odoo.http import request


def project_strip(code):
    """Values for the shared stage strip (template distri_pedido.proyecto_franja)."""
    return request.env["distri.project.module"].project_strip(code)


class Proyecto(http.Controller):

    @http.route("/proyecto", type="http", auth="public", website=True, sitemap=False)
    def proyecto(self, **kw):
        """Project hub: every part with its stage, next step and presentation."""
        Module = request.env["distri.project.module"]
        rows = Module.project_rows()
        counts = {}
        for row in rows:
            counts[row["stage"]] = counts.get(row["stage"], 0) + 1
        return request.render("distri_pedido.proyecto", {
            "empresa_id": request.env.company.id,
            "modulos": rows,
            "pasos": Module.track_labels(),
            "cuentas": counts,
        })

    @http.route("/presentacion/migracion", type="http", auth="public", website=True,
                sitemap=False)
    def presentacion_migracion(self, **kw):
        """Why the project started with the catalog, and what the migration implies."""
        return request.render("distri_pedido.presentacion_migracion", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("migracion"),
        })
