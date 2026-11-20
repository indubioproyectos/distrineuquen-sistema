import functools
from urllib.parse import quote

from odoo import http
from odoo.http import request

# 19.0.1.119.0: the practice environment (/srv/odoo-lab/practica); the flag
# is distri_practica.activo, set only in the practice database
PRACTICE_URL = "https://practica.distrineuquen.com"
PRACTICE_FLAG = "distri_practica.activo"

# the DN app icons, same files and query as the public site (controllers/sitio.py)
LOGO = "/distri_pedido/static/src/img/iconos/"


def project_strip(code):
    """Values for the shared stage strip (template distri_pedido.proyecto_franja)."""
    return request.env["distri.project.module"].project_strip(code)


def staff_only(func):
    """19.0.1.119.0: the training and project pages (/proyecto,
    /presentacion*) are for the staff. Logged out: to the login, back here
    after it (query string kept). A customer's portal account: 404, the page
    is never revealed. Internal users: the page as before. The routes stay
    auth="public" so this decides, not Odoo's own login redirect (which
    would let a portal user in)."""
    @functools.wraps(func)
    def wrapper(self, *args, **kw):
        user = request.env.user
        if user._is_public():
            http_req = request.httprequest
            target = http_req.path
            if http_req.query_string:
                target += "?" + http_req.query_string.decode("latin-1")
            return request.redirect("/web/login?redirect=%s" % quote(target, safe=""), 303)
        if user.share:
            return request.not_found()
        return func(self, *args, **kw)
    return wrapper


class Proyecto(http.Controller):

    @http.route("/proyecto", type="http", auth="public", website=True, sitemap=False)
    @staff_only
    def proyecto(self, **kw):
        """«Capacitación Distri Neuquén» (19.0.1.114.0): the staff training
        page, one role at a time, with the former project hub collapsed in
        «Historia del proyecto» (distri.project.module rows). Staff only
        since 19.0.1.119.0 (staff_only), noindex, standalone like the public
        site (no website layout)."""
        # imported here: controllers/main.py imports this module before
        # kit/reparto can be loaded (reparto imports main)
        from .kit import LOGO_V, kit_url, kit_version
        from .reparto import fonts

        version = kit_version()
        # 19.0.1.119.0: «Ir a practicar»; inside the practice environment
        # (its sanitize step sets this parameter) it goes to its own landing
        in_practice = request.env["ir.config_parameter"].sudo().get_param(
            PRACTICE_FLAG) == "1"
        response = request.render("distri_pedido.proyecto", {
            "en_practica": in_practice,
            "practica_url": "/" if in_practice else PRACTICE_URL,
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
    @staff_only
    def presentacion_migracion(self, **kw):
        """Why the project started with the catalog, and what the migration implies."""
        return request.render("distri_pedido.presentacion_migracion", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("migracion"),
        })
