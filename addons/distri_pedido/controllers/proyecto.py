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

# 19.0.1.129.42 (ui_v2): the training roles of /proyecto (its articles
# #rol-<key>, in page order) and who sees each one. administración (and
# gerencia, which implies it) and Sistemas see them all.
TRAINING_ROLES = ("armado", "reparto", "deposito", "caja", "administracion", "vendedores")
TRAINING_GROUPS = {
    "armado": "distri_pedido.grupo_armador",
    "reparto": "distri_pedido.grupo_reparto",
    "caja": "distri_pedido.grupo_cajero",
    "administracion": "distri_pedido.grupo_administracion",
    "vendedores": "distri_pedido.grupo_vendedor",
}
SEES_ALL = ("distri_pedido.grupo_administracion", "base.group_system")
DESPACHO_MENU = "distri_pedido.menu_reparto_despacho"

# 19.0.1.129.42: the practice landing's role keys (distri_practica ROLES) of a
# production user, most specific first: the landing shows them first and
# the signed pass (controllers/practica_pase.py) carries them.
PRACTICE_KEYS = ("gerencia", "administracion", "deposito", "cajero", "armador", "repartidor",
                 "vendedor")


def training_roles(env):
    """The /proyecto roles the user sees, in page order (all of them for
    administración, gerencia and Sistemas)."""
    user = env.user
    if any(user.has_group(g) for g in SEES_ALL):
        return list(TRAINING_ROLES)
    roles = []
    for key in TRAINING_ROLES:
        if key == "deposito":
            menu = env.ref(DESPACHO_MENU, raise_if_not_found=False)
            if menu and menu.id in env["ir.ui.menu"]._visible_menu_ids():
                roles.append(key)
        elif user.has_group(TRAINING_GROUPS[key]):
            roles.append(key)
    return roles


def practice_roles(user):
    """The practice landing keys of the user, most specific first. The
    groups implied by administración (seller, packer, driver) are not his
    roles: an administrator is «administración» and «depósito»."""
    if not user or user.share or user._is_public():
        return []
    admin = user.has_group("distri_pedido.grupo_administracion")
    keys = []
    if user.has_group("distri_pedido.grupo_gerencia"):
        keys.append("gerencia")
    elif admin:
        keys += ["administracion", "deposito"]
    if user.has_group("distri_pedido.grupo_cajero"):
        keys.append("cajero")
    if not admin:
        for key, group in (("armador", "distri_pedido.grupo_armador"),
                           ("repartidor", "distri_pedido.grupo_reparto"),
                           ("vendedor", "distri_pedido.grupo_vendedor")):
            if user.has_group(group):
                keys.append(key)
    return keys


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

        from .kit import ui_v2_on
        from .practica_pase import pass_enabled

        version = kit_version()
        # 19.0.1.119.0: «Ir a practicar»; inside the practice environment
        # (its sanitize step sets this parameter) it goes to its own landing
        in_practice = request.env["ir.config_parameter"].sudo().get_param(
            PRACTICE_FLAG) == "1"
        practica_url = "/" if in_practice else PRACTICE_URL
        # 19.0.1.129.42 (ui_v2): only the user's own training roles, and
        # «Ir a practicar» with a signed pass when the key is configured
        v2 = ui_v2_on(request.env)
        roles = training_roles(request.env) if v2 else list(TRAINING_ROLES)
        if v2 and not in_practice and pass_enabled(request.env):
            practica_url = "/proyecto/practicar"
        response = request.render("distri_pedido.proyecto", {
            "en_practica": in_practice,
            "practica_url": practica_url,
            "roles_visibles": roles,
            "filtrar_roles": v2,
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
