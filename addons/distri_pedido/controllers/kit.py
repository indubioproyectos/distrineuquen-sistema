"""Distrineuquén kit (19.0.1.94.0): the shared look of the standalone pages.

static/src/kit/ holds dn.css (tokens and components), dn.js (helpers and
the up-one-level router) and dn-icons.svg (the icon sprite). Pages keep
their own small files next to their code (static/src/armado/). None of this
goes through an Odoo asset bundle: every file is served as is by /dn/<name>
with a version in the query (kit_version(), same idea as reparto's
asset_version()) and a one-year «immutable» cache, so a phone downloads it
once per release. Pages include the kit with the QWeb macros in
views/kit.xml (distri_pedido.dn_head and distri_pedido.dn_nav).

19.0.1.129.29 («Etapa 3a · Navegación y kit mínimo»):
- SCREEN_PARENTS: the parent of every staff screen, declared once (#100,
  «Atrás» goes UP one level). Every kit page writes it on <body
  data-parent>; tests/test_v129_29_navegacion.py walks every chain up to
  Inicio and checks the pages say the same.
- ui_v2_on(): the parameter distri_pedido.ui_v2 (OFF by default). With it
  on, the bottom bar is the bar of the user's role (role_nav: Inicio plus
  the role's screens, the same on every top-level screen) and /pedido and
  /cobro take the kit's look (kit/pedido_v2.css, kit/cobro_v2.css). Off, everything is as in
  19.0.1.129.28.

19.0.1.129.33 («Etapa 3b», part 1), all behind ui_v2:
- legacy_ui(): /despacho and /cuentas (the older design) take the kit's look
  (dn.css + kit/despacho_v2.css, kit/cuentas_v2.css) and the role's bottom
  bar, like /cobro in 129.29. Same data, buttons and permissions.
- /equipo («Equipo», #74, read-only) and /tablero/ordenar («Ordenar el
  Inicio», #3) are declared screens (controllers/equipo.py).

19.0.1.129.34 («Etapa 3b», part 2), behind ui_v2: the order, invoice and
customer screens (/pedidos/<id>, /facturas/<id>, /clientes/<id>,
controllers/fichas.py) with their parents here; route_matches() is the
pattern test of screen_parents(), also used to accept a ?desde= parent.
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
    # 19.0.1.129.29: the kit's look for /pedido and /cobro (ui_v2 only)
    "pedido_v2.css": ("kit/pedido_v2.css", "text/css; charset=utf-8"),
    "cobro_v2.css": ("kit/cobro_v2.css", "text/css; charset=utf-8"),
    # 19.0.1.129.30: «Nueva venta» (controllers/nueva_venta.py, nueva_venta only)
    "nueva_venta.css": ("nueva_venta/nueva_venta.css", "text/css; charset=utf-8"),
    "nueva_venta.js": ("nueva_venta/nueva_venta.js", "text/javascript; charset=utf-8"),
    # 19.0.1.129.33: /despacho and /cuentas with the kit's look, «Equipo» and
    # «Ordenar el Inicio» (ui_v2 only)
    "despacho_v2.css": ("kit/despacho_v2.css", "text/css; charset=utf-8"),
    "cuentas_v2.css": ("kit/cuentas_v2.css", "text/css; charset=utf-8"),
    "equipo.css": ("kit/equipo.css", "text/css; charset=utf-8"),
    # 19.0.1.129.34: the order, invoice and customer screens (ui_v2 only)
    "fichas.css": ("kit/fichas.css", "text/css; charset=utf-8"),
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


# ------------------------------------------------------------ 19.0.1.129.29

PARAM_UI_V2 = "distri_pedido.ui_v2"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")
# the staff home: /tablero for whoever has it (tablero.wants_tablero)
HOME = "inicio"

# 19.0.1.129.29 (#100): the parent of every staff screen, by route. «Atrás»
# (and the phone back, dn.js) goes to it, never to the previous sibling.
# A tuple lists the parents a screen may have, by where it was opened from
# (?desde=, ?volver=) or by the user (a cashier's /caja/historial goes back
# to /caja, gerencia's to Inicio). () = the top. HOME = Inicio. The
# in-page levels of the three hash apps (/despacho, /cuentas, /reparto)
# are in SCREEN_LEVELS.
SCREEN_PARENTS = {
    "/tablero": (),
    # depot / packing
    "/armado": (HOME,),
    "/armado/faltantes": (HOME,),
    "/armado/pedido/<id>": ("/armado",),
    "/deposito": (HOME,),
    "/deposito/entrega/<id>": ("/deposito",),
    "/deposito/mostrador": ("/deposito",),
    "/deposito/caja": ("/deposito",),
    "/deposito/caja/<id>": ("/deposito",),
    "/despacho": (HOME,),
    # cash
    "/caja": (HOME,),
    "/caja/rendicion": ("/caja",),
    "/caja/rendicion/<id>": ("/caja/rendicion",),
    "/caja/salida": ("/caja",),
    "/caja/adelanto": ("/caja",),
    "/caja/cierre": ("/caja",),
    "/caja/cerradas": ("/caja",),
    "/caja/cerradas/<id>": ("/caja/cerradas", "/caja/historial"),
    "/caja/historial": ("/caja", HOME),
    "/cobro": ("/caja", HOME),
    # administración
    "/revisar": (HOME,),
    "/revisar/<id>/asignar": ("/revisar", "/agilpedido", HOME),
    "/rutas": (HOME,),
    "/agilpedido": (HOME,),
    "/agilpedido/cargar": ("/agilpedido",),
    "/agilpedido/cargar/<id>": ("/agilpedido",),
    "/agilpedido/pedido/<id>": ("/agilpedido",),
    "/agilpedido/creado/<id>": ("/agilpedido",),
    "/rendiciones": (HOME,),
    "/rendiciones/rutas": ("/rendiciones",),
    "/comprobantes": (HOME,),
    "/comprobantes/mios": ("/comprobantes",),
    "/comprobantes/<id>": ("/comprobantes", "/comprobantes/mios"),
    "/comprobantes/<id>/pago-proveedor": ("/comprobantes/<id>",),
    "/comprobantes/intento/<id>": ("/comprobantes", "/comprobantes/mios"),
    "/cuentas": (HOME,),
    # sellers, drivers, everyone
    "/pedido": (HOME,),
    # 19.0.1.129.30: «Nueva venta» (distri_pedido.nueva_venta only)
    "/nueva-venta": (HOME,),
    "/nueva-venta/hecho/<id>": ("/nueva-venta",),
    "/mis-ventas": (HOME,),
    # 19.0.1.129.31: «Tablero de verificación» (distri_pedido.verificacion_v2 only)
    "/verificacion": (HOME,),
    "/reparto": (HOME,),
    "/proyecto": (HOME,),
    # 19.0.1.129.33 (ui_v2 only): «Equipo» (#74) and «Ordenar el Inicio» (#3)
    "/equipo": (HOME,),
    "/tablero/ordenar": (HOME,),
    # 19.0.1.129.34 (ui_v2 only): the customer, order and invoice screens
    # (controllers/fichas.py). The customer is the top of the three: an
    # order goes up to its customer when opened from it, an invoice to its
    # order or its customer; ?desde= says which parent the screen was opened
    # from, anything else goes up to Inicio.
    "/clientes/<id>": ("/revisar", "/cuentas", HOME),
    "/pedidos/<id>": ("/revisar", "/agilpedido", "/agilpedido/pedido/<id>",
                      "/agilpedido/creado/<id>", "/deposito/mostrador", "/mis-ventas",
                      "/clientes/<id>", HOME),
    "/facturas/<id>": ("/pedidos/<id>", "/clientes/<id>", HOME),
}
# 19.0.1.129.31: one screen per queue of «Tablero de verificación»
SCREEN_PARENTS.update({"/verificacion/%s" % key: ("/verificacion",) for key in (
    "para_acreditar", "revision_humana", "esperando_reporte", "sin_transferencia",
    "diferencias", "duplicados", "sin_comprobante", "historicos")})

# The in-page levels (URL hash) of the hash apps and their parent level;
# «‹ Atrás» there goes up with history.back() when the level was opened
# from its parent, else it replaces the entry (static/src/*: «nav-up»).
SCREEN_LEVELS = {
    "/despacho": {"": None, "#ruta/<id>": ""},
    "/cuentas": {"": None, "#cuenta/<id>": "", "#cuenta/<id>/mov/<key>": "#cuenta/<id>",
                 "#transferencias": "", "#transferencias/<id>": "#transferencias"},
    "/reparto": {"": None, "#visitas": None, "#carga": None, "#visita/<id>": "#visitas",
                 "#cliente/<id>": "#visitas", "#cuenta/<id>": "#visita/<id>",
                 "#gasto": "#visitas", "#cierre": "#visitas", "#rechazos": "#visitas",
                 "#rutas": "", "#orden": "#carga"},
}


def ui_v2_on(env):
    """19.0.1.129.29: the kit's navigation and look on /pedido and /cobro."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_UI_V2) or "0"
    return str(value).strip().lower() in ON_VALUES


def route_matches(pattern, path):
    """Whether a route path ("/caja/cerradas/12") is the screen of a
    SCREEN_PARENTS pattern ("/caja/cerradas/<id>")."""
    if pattern == path:
        return True
    bits, parts = pattern.strip("/").split("/"), path.strip("/").split("/")
    return len(bits) == len(parts) and all(
        b == p or (b == "<id>" and p.isdigit()) for b, p in zip(bits, parts))


def screen_parents(path):
    """The declared parents of a route path ("/caja/cerradas/12"), or None
    when it is not a staff screen of SCREEN_PARENTS."""
    if path in SCREEN_PARENTS:
        return SCREEN_PARENTS[path]
    for pattern, parents in SCREEN_PARENTS.items():
        if route_matches(pattern, path):
            return parents
    return None


# 19.0.1.129.29: the bottom bar of each role. A user takes the first role he
# has, in this order (gerencia implies administración, which implies the
# seller, packer and driver groups). Each item shows only when the user can
# open that screen (the same rule as its Inicio tile, tablero.staff_tiles).
ROLES = (
    ("gerencia", "distri_pedido.grupo_gerencia",
     ("revisar", "misventas", "historial", "cuentas")),
    ("administracion", "distri_pedido.grupo_administracion",
     ("revisar", "rutas", "comprobantes", "cuentas")),
    ("cajero", "distri_pedido.grupo_cajero",
     ("caja", "cobro", "comprobantes")),
    ("armador", "distri_pedido.grupo_armador",
     ("armado", "faltantes", "despacho")),
    ("repartidor", "distri_pedido.grupo_reparto",
     ("mireparto", "cuentas", "cobro")),
    ("vendedor", "distri_pedido.grupo_vendedor",
     ("pedido", "misventas", "cuentas", "comprobantes")),
)
# (label, sprite icon) of a bar item; the URL is its Inicio tile's
NAV_ITEMS = {
    "revisar": ("Revisar", "target"),
    "misventas": ("Mis ventas", "bag"),
    "historial": ("Cajas", "clock"),
    "cuentas": ("Cuentas", "chart"),
    "rutas": ("Rutas", "pin"),
    "comprobantes": ("Comprobantes", "receipt"),
    "caja": ("Caja", "register"),
    "cobro": ("Cobrar", "cash"),
    "armado": ("Pedidos", "list"),
    "faltantes": ("Faltantes", "alert"),
    "despacho": ("Despacho", "truck"),
    "mireparto": ("Mi reparto", "truck"),
    "pedido": ("Pedido", "order"),
}
# screens that come in a group: /armado's two tabs travel together
NAV_GROUPS = {"armado": ("armado", "faltantes"), "faltantes": ("armado", "faltantes")}
NAV_MAX = 5


def user_role(user):
    """The role whose bottom bar the user gets, or "" (customers, users of
    none of the roles)."""
    if not user or user.share or user._is_public():
        return ""
    for role, group, _keys in ROLES:
        if user.has_group(group):
            return role
    return ""


def role_nav(env, current):
    """19.0.1.129.29: Inicio plus the screens of the user's role (ROLES), in
    that order and only the ones he can open. On a screen of his that is
    not in his bar (an administrator on /armado) that screen is added,
    marked, so he sees where he is. «Inicio» goes UP; the rest are sibling
    screens and replace the page (dn.js «side»)."""
    from .tablero import staff_tiles, wants_tablero

    user = env.user
    role = user_role(user)
    if not role:
        return []
    keys = list(dict(((r, k) for r, _g, k in ROLES))[role])
    urls = {tile["key"]: tile["url"] for tile in staff_tiles(env) if tile["url"]}
    if "armado" in urls:
        urls["faltantes"] = "/armado/faltantes"
    room = NAV_MAX - (1 if wants_tablero(user) else 0)
    keys = [k for k in keys if k in urls]
    if current in urls and current in NAV_ITEMS and current not in keys:
        extra = [k for k in NAV_GROUPS.get(current, (current,)) if k in urls and k not in keys]
        keys = keys[:max(0, room - len(extra))] + extra
    keys = keys[:room]
    items = []
    if wants_tablero(user):
        items.append({"key": "inicio", "label": "Inicio", "icon": "home", "url": "/tablero",
                      "how": "up"})
    for key in keys:
        label, icon = NAV_ITEMS[key]
        items.append({"key": key, "label": label, "icon": icon, "url": urls[key], "how": "side"})
    for item in items:
        item["on"] = item["key"] == current
    return items


def page_nav(env, current):
    """The bottom bar of a top-level screen that had none before 19.0.1.129.29
    (/revisar, /caja, /cobro...): the role's bar with ui_v2 on, nothing off."""
    return role_nav(env, current) if ui_v2_on(env) else []


def legacy_ui(env, current, page_css):
    """19.0.1.129.33: the values a page of the older design (/despacho,
    /cuentas) needs to take the kit's look with ui_v2 on: dn.css (it carries
    distri_ui.css's class names on the kit tokens) plus the page's own
    kit/<page>_v2.css instead of distri_ui.css, and the role's bottom bar.
    Off: {"ui_v2": False, "nav": []} and the page renders as before."""
    if not ui_v2_on(env):
        return {"ui_v2": False, "nav": []}
    return {"ui_v2": True, "dn": kit_context(page_css), "nav": role_nav(env, current)}


def staff_nav(env, current):
    """The bottom nav of a staff page, from the same groups as the menus.

    Packers: Pedidos · Faltantes, plus Despacho when the user can open it and
    Inicio (/tablero) for users that have the staff home (a user whose only
    screen is /armado would just come back here). `current` is the key of
    the active item. «Inicio» goes UP (the home is the parent of every app);
    the rest are siblings and replace the page (dn.js).
    19.0.1.129.29: with ui_v2 on, the bar of the user's role (role_nav).
    """
    from .tablero import wants_tablero

    if ui_v2_on(env):
        return role_nav(env, current)
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
