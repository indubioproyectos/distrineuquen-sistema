"""Distrineuquén kit (19.0.1.94.0): the shared look of the standalone pages.

static/src/kit/ holds dn.css (tokens and components), dn.js (helpers and
the up-one-level router) and dn-icons.svg (the icon sprite). Pages keep
their own small files next to their code (static/src/armado/). None of this
goes through an Odoo asset bundle: every file is served as is by /dn/<name>
with a version in the query (kit_version(), same idea as reparto's
asset_version()) and a one-year «immutable» cache, so a phone downloads it
once per release. Pages include the kit with the QWeb macros in
views/kit.xml (distri_pedido.dn_head and distri_pedido.dn_nav).
"""
import hashlib
import os

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from ..models.marca import marca_manifest_icons
from .reparto import _disk, fonts

_SRC = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src")
# published name -> (path under static/src, content type)
FILES = {
    "dn.css": ("kit/dn.css", "text/css; charset=utf-8"),
    "dn.js": ("kit/dn.js", "text/javascript; charset=utf-8"),
    "dn-icons.svg": ("kit/dn-icons.svg", "image/svg+xml; charset=utf-8"),
    "armado.css": ("armado/armado.css", "text/css; charset=utf-8"),
    "armado.js": ("armado/armado.js", "text/javascript; charset=utf-8"),
    # 19.0.1.97.0: «Caja del día» (controllers/caja.py)
    "caja.css": ("caja/caja.css", "text/css; charset=utf-8"),
    "caja.js": ("caja/caja.js", "text/javascript; charset=utf-8"),
    # 19.0.1.99.0: «Subir comprobante» (controllers/comprobantes.py)
    "comprobantes.css": ("comprobantes/comprobantes.css", "text/css; charset=utf-8"),
    "comprobantes.js": ("comprobantes/comprobantes.js", "text/javascript; charset=utf-8"),
    # 19.0.1.108.0: «Pedidos para revisar» (controllers/revisar.py) and «Inicio»
    "revisar.css": ("revisar/revisar.css", "text/css; charset=utf-8"),
    "revisar.js": ("revisar/revisar.js", "text/javascript; charset=utf-8"),
    "tablero.css": ("tablero/tablero.css", "text/css; charset=utf-8"),
    # 19.0.1.109.0: «Depósito» (controllers/deposito.py)
    "deposito.css": ("deposito/deposito.css", "text/css; charset=utf-8"),
    "deposito.js": ("deposito/deposito.js", "text/javascript; charset=utf-8"),
    # 19.0.1.110.0: «Planificación de rutas» on the PC (controllers/rutas.py)
    "rutas.css": ("rutas/rutas.css", "text/css; charset=utf-8"),
    "rutas.js": ("rutas/rutas.js", "text/javascript; charset=utf-8"),
    # 19.0.1.111.0: «Asignar cliente» (controllers/asignar.py) and «Pedidos de
    # AgilPedido» (controllers/agilpedido.py)
    "asignar.css": ("asignar/asignar.css", "text/css; charset=utf-8"),
    "asignar.js": ("asignar/asignar.js", "text/javascript; charset=utf-8"),
    "agilpedido.css": ("agilpedido/agilpedido.css", "text/css; charset=utf-8"),
    "agilpedido.js": ("agilpedido/agilpedido.js", "text/javascript; charset=utf-8"),
    # 19.0.1.129.18: «Mis ventas» (controllers/mis_ventas.py)
    "mis_ventas.css": ("mis_ventas/mis_ventas.css", "text/css; charset=utf-8"),
    "mis_ventas.js": ("mis_ventas/mis_ventas.js", "text/javascript; charset=utf-8"),
    # 19.0.1.98.0: the «Ajustes» gear of every page (views/kit.xml dn_ajustes)
    "ajustes.css": ("kit/ajustes.css", "text/css; charset=utf-8"),
    "ajustes.js": ("kit/ajustes.js", "text/javascript; charset=utf-8"),
    # 19.0.1.104.0: the public site (controllers/sitio.py)
    "sitio.css": ("sitio/sitio.css", "text/css; charset=utf-8"),
    # 19.0.1.114.0: «Capacitación Distri Neuquén» (/proyecto, controllers/proyecto.py)
    "proyecto.css": ("proyecto/proyecto.css", "text/css; charset=utf-8"),
    "proyecto.js": ("proyecto/proyecto.js", "text/javascript; charset=utf-8"),
    # 19.0.1.95.0: the legacy pages' history fix (models/ir_http.py)
    "sin_historial.js": ("js/sin_historial.js", "text/javascript; charset=utf-8"),
}
# served without comments and indentation (budget: < 3 KB gzip); the file
# on disk keeps its documentation
COMPACT = {"sin_historial.js"}
IMMUTABLE = "public, max-age=31536000, immutable"
# 19.0.1.95.0: query of the DN app icons (static/src/img/iconos/app*.png,
# dn-logo.svg); bump it when they change so phones and manifests pick them
# up. The QWeb pages write the same "?v=95" by hand.
LOGO_V = "?v=95"


def _path(name):
    return os.path.join(_SRC, FILES[name][0])


def kit_version():
    """Changes whenever a kit or page file (or the font) changes."""
    digest = hashlib.sha256()
    paths = [_path(name) for name in sorted(FILES)] + [_disk(url) for url in fonts()]
    for path in paths:
        stat = os.stat(path)
        digest.update(("%s:%s:%s;" % (path, stat.st_mtime_ns, stat.st_size)).encode())
    return digest.hexdigest()[:12]


def compact_js(source):
    """Drop the leading /* */ header, whole-line // comments and indentation.
    Only for files written for it (no multi-line strings or templates)."""
    if source.lstrip().startswith("/*"):
        source = source[source.index("*/") + 2:]
    lines = (line.strip() for line in source.splitlines())
    return "\n".join(line for line in lines if line and not line.startswith("//")) + "\n"


def kit_url(name, version=None):
    return "/dn/%s?v=%s" % (name, version or kit_version())


def kit_context(*page_files):
    """Values for the dn_head macro: dn['css'], dn['js'], dn['icons'],
    dn['font'] and the page's own files (dn['armado.css']...)."""
    version = kit_version()
    dn = {
        "css": kit_url("dn.css", version),
        "js": kit_url("dn.js", version),
        "icons": kit_url("dn-icons.svg", version),
        "font": (fonts() or [""])[0],
        "page_css": [],
        "page_js": [],
    }
    for name in page_files:
        key = "page_css" if name.endswith(".css") else "page_js"
        dn[key].append(kit_url(name, version))
    return dn


def staff_nav(env, current):
    """The bottom nav of a staff page, from the same groups as the menus.

    Packers: Pedidos · Faltantes, plus Despacho when the user can open it and
    Inicio (/tablero) for users that have the staff home (a user whose only
    screen is /armado would just come back here). `current` is the key of
    the active item. «Inicio» goes UP (the home is the parent of every app);
    the rest are siblings and replace the page (dn.js).
    """
    from .tablero import wants_tablero

    user = env.user
    items = []
    if wants_tablero(user):
        items.append({"key": "inicio", "label": "Inicio", "icon": "home", "url": "/tablero", "how": "up"})
    if user.has_group("distri_pedido.grupo_armador"):
        items += [
            {"key": "armado", "label": "Pedidos", "icon": "list", "url": "/armado", "how": "side"},
            {"key": "faltantes", "label": "Faltantes", "icon": "alert", "url": "/armado/faltantes",
             "how": "side"},
        ]
    menu = env.ref("distri_pedido.menu_reparto_despacho", raise_if_not_found=False)
    if menu and menu.id in env["ir.ui.menu"]._visible_menu_ids():
        items.append({"key": "despacho", "label": "Despacho", "icon": "truck", "url": "/despacho",
                      "how": "side"})
    for item in items:
        item["on"] = item["key"] == current
    return items


def depot_nav(env, current):
    """19.0.1.109.0 (mockup «12 Barra inferior»): the bottom nav of the depot
    role (administración: who delivers at the depot), only on its own screens
    (/deposito): Armado · Despacho · Cobro · Entregas, each one when the user
    can open it, Armado with the count of /armado's list. They are sibling
    apps of the same home: they replace the page (dn.js «side»).
    """
    from .deposito import is_depot
    from .tablero import _badge_armado

    user = env.user
    if not is_depot(user):
        return []
    items = []
    if user.has_group("distri_pedido.grupo_armador"):
        badge = _badge_armado(env)
        items.append({"key": "armado", "label": "Armado", "icon": "box", "url": "/armado",
                      "how": "side", "badge": badge["text"] if badge else ""})
    menu = env.ref("distri_pedido.menu_reparto_despacho", raise_if_not_found=False)
    if menu and menu.id in env["ir.ui.menu"]._visible_menu_ids():
        items.append({"key": "despacho", "label": "Despacho", "icon": "truck", "url": "/despacho",
                      "how": "side"})
    items += [
        {"key": "cobro", "label": "Cobro", "icon": "cash", "url": "/cobro", "how": "side"},
        {"key": "entregas", "label": "Entregas", "icon": "store", "url": "/deposito",
         "how": "side"},
    ]
    for item in items:
        item["on"] = item["key"] == current
    return items


class Kit(http.Controller):

    @http.route("/dn/<string:name>", type="http", auth="public", methods=["GET"], sitemap=False)
    def kit_file(self, name, v="", **kw):
        if name == "cliente.webmanifest":
            return self._manifest_cliente()
        if name not in FILES:
            raise NotFound()
        with open(_path(name), "rb") as handle:
            body = handle.read()
        if name in COMPACT:
            body = compact_js(body.decode("utf-8")).encode("utf-8")
        # only the current version is immutable: an old ?v= gets today's
        # file, and the next page view asks for the new URL anyway
        cache = IMMUTABLE if v == kit_version() else "no-cache"
        return request.make_response(body, headers=[
            ("Content-Type", FILES[name][1]),
            ("Cache-Control", cache),
            ("X-Content-Type-Options", "nosniff"),
        ])

    def _manifest_cliente(self):
        """19.0.1.98.0: «Instalar la app» for customers (the staff use
        /tablero/manifest.webmanifest): the order page as a home-screen app."""
        import json
        from .main import FICHA
        data = {
            "name": FICHA["nombre"],
            "short_name": "Distri",
            "lang": "es-AR",
            "id": "/pedido",
            "start_url": "/pedido",
            "scope": "/",
            "display": "standalone",
            "background_color": "#f7f8f7",
            "theme_color": "#10243f",
            "icons": marca_manifest_icons(),
        }
        return request.make_response(json.dumps(data, ensure_ascii=False), headers=[
            ("Content-Type", "application/manifest+json; charset=utf-8"),
            ("Cache-Control", "public, max-age=86400")])
