"""«Tablero»: the staff home of the lab (19.0.1.76.0).

Opening the backend without a menu or record (/odoo) used to land on Odoo's
Discuss. Internal users that keep the backend (administración, gerencia,
system administrators) now land here instead: one big tile per screen of
this addon, and below them every other Odoo app the user can open (the app
grid that Odoo Community does not have). A tile shows only when the user can
see its menu, so everything follows the same groups as the Odoo menus.
/odoo?menu_id=<id> (or any query) skips the redirect to this page.
"""
import json

from odoo import http
from odoo.http import request

from .main import FICHA
from .reparto import SHARED_CSS

IMG = "/distri_pedido/static/src/img/iconos/"

# (menu xmlid, title, subtitle, icon, url or None = open the menu in the backend)
TILES = [
    ("distri_pedido.menu_cuentas_root", "Estado de cuentas", "Deudores y cuentas, también en el celular",
     IMG + "cuentas.png", "/cuentas"),
    ("distri_pedido.menu_cobros_root", "Cobros", "Tablero MP, tarjetas y planillas", IMG + "cobros.png", None),
    ("distri_pedido.menu_reparto_root", "Reparto", "Rutas, rendiciones y zonas",
     IMG + "reparto.png", None),
    ("distri_pedido.menu_armado_app", "Armado", "Pantalla del armador", IMG + "armado.png", "/armado"),
    ("distri_pedido.menu_reparto_despacho", "Despacho", "Salida de los repartos", IMG + "despacho.png",
     "/despacho"),
]


def staff_tiles(env):
    """The tiles the current user may open, in order."""
    visible = env["ir.ui.menu"]._visible_menu_ids()
    tiles = []
    for xmlid, title, subtitle, icon, url in TILES:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if not menu or menu.id not in visible:
            continue
        tiles.append({"title": title, "subtitle": subtitle, "icon": icon, "menu_id": menu.id,
                      "url": url or "/odoo?menu_id=%s" % menu.id})
    return tiles


def _menu_url(menu):
    action = menu.action
    if action and action._name == "ir.actions.act_url" and action.url.startswith("/"):
        return action.url
    return "/odoo?menu_id=%s" % menu.id


def _menu_icon(menu):
    if menu.web_icon and "," in menu.web_icon:
        module, path = menu.web_icon.split(",", 1)
        return "/%s/%s" % (module, path)
    if menu.web_icon_data:
        return "/web/image/ir.ui.menu/%s/web_icon_data" % menu.id
    return "/base/static/description/icon.png"


def other_apps(env, taken):
    """Every other top menu the user can see, in menu order (Odoo's app list)."""
    Menu = env["ir.ui.menu"]
    visible = Menu._visible_menu_ids()
    return [{"title": menu.name, "icon": _menu_icon(menu), "url": _menu_url(menu)}
            for menu in Menu.search([("parent_id", "=", False)])
            if menu.id in visible and menu.id not in taken]


def wants_tablero(user):
    """Internal users without their own screen (see res.users._distri_landing)."""
    return not user.share and not user._distri_landing()


class Tablero(http.Controller):

    @http.route("/tablero", type="http", auth="user", sitemap=False)
    def tablero(self, **kw):
        user = request.env.user
        if user.share:
            return request.redirect("/pedido")
        landing = user._distri_landing()
        if landing:
            return request.redirect(landing)
        tiles = staff_tiles(request.env)
        # the root menus behind our tiles (Armado/Despacho tiles are not root menus)
        taken = {t["menu_id"] for t in tiles}
        html = request.env["ir.qweb"]._render("distri_pedido.tablero_page", {
            "tiles": tiles,
            "apps": other_apps(request.env, taken),
            "user": user,
            "empresa": FICHA["nombre"],
            "shared_css": SHARED_CSS,
        })
        return request.make_response(html, headers=[("Content-Type", "text/html; charset=utf-8"),
                                                    ("Cache-Control", "no-store")])

    @http.route("/tablero/manifest.webmanifest", type="http", auth="public", sitemap=False)
    def manifest(self, **kw):
        """The whole lab as one home-screen app. Scope «/» keeps Odoo, /cuentas,
        /armado, /reparto... inside the app window: with a narrower scope the
        phone shows its «x · title · domain» frame on every other page."""
        data = {
            "name": FICHA["nombre"],
            "short_name": "Distri",
            "lang": "es-AR",
            "id": "/tablero",
            "start_url": "/tablero",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f7f8f7",
            "theme_color": "#10243f",
            "icons": [
                {"src": IMG + "app-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": IMG + "app-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": IMG + "app-maskable-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
        }
        return request.make_response(json.dumps(data, ensure_ascii=False), headers=[
            ("Content-Type", "application/manifest+json; charset=utf-8")])
