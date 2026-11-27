"""«Tablero»: the staff home of the lab (19.0.1.76.0).

Opening the backend without a menu or record (/odoo) used to land on Odoo's
Discuss. Internal users that keep the backend (administración, gerencia,
system administrators) now land here instead: one big tile per screen of
this addon, and below them every other Odoo app the user can open (the app
grid that Odoo Community does not have). A tile shows only when the user can
see its menu, so everything follows the same groups as the Odoo menus.
/odoo?menu_id=<id> (or any query) skips the redirect to this page.

19.0.1.129.33 («Etapa 3b», part 1), behind distri_pedido.ui_v2 only:
- #3: each user's own order of the tiles (res.users.distri_inicio_orden,
  set on /tablero/ordenar, controllers/equipo.py), applied on top of
  DEFAULT_TILE_ORDER by apply_user_order(); a link «Ordenar el Inicio»
  under the tiles.
- #74: the «Equipo» tile (/equipo) for administración, gerencia and Sistemas.
With the flag off the tiles, their order and the page are as before.

19.0.1.129.42, behind ui_v2 too: «Tablero de verificación» before
«Capacitación», administración's tiles in groups (TILE_GROUPS), the
subtitles at 13 px, and no Odoo apps for packers and drivers (hides_apps).

19.0.1.129.47, behind ui_v2: Inicio on the design system (views/
tablero.xml tablero_v2_page, «HomeTile»): the same tiles, visibility and
links; every role's tiles in labelled groups, the first one highlighted,
2 columns on the phone and 4 on the PC.

19.0.1.129.48, behind ui_v2: «Mis comprobantes a reclamar» is the first
block of Inicio (reclamar_block), listing each receipt with its actions,
instead of the banner line of 129.37 (reclamar_row, kept for the flag off).

19.0.1.129.50, behind ui_v2: the Inicio tiles get a short name (TILE_LABELS,
never broken mid-word), a larger icon on top and the same height; the full
title and subtitle stay as the tooltip (and the subtitle shows on the PC).
"""
import json
import re
from datetime import timedelta

from odoo import fields, http
from odoo.fields import Domain
from odoo.http import request

from ..models.acreditar import CREDIT_NOTICE
from ..models.marca import marca_manifest_icons
from .main import FICHA
from .kit import kit_context, ui_v2_on
from .reparto import SHARED_CSS

IMG = "/distri_pedido/static/src/img/iconos/"

# 19.0.1.108.0 (owner-approved mockup «Inicio»): the tablero is a kit page
# (views/tablero.xml, static/src/tablero/tablero.css). Every tile has a
# sprite glyph (static/src/kit/dn-icons.svg) and, when it is cheap, a count
# badge (TILE_BADGES): one search_count per visible tile, nothing for the
# hidden ones. Above the tiles, the «pedidos sin cliente» banner
# (19.0.1.121.0, before: «pedidos en rojo» of the risk).
#
# (key, visibility, title, subtitle, legacy PNG icon, glyph, url)
# visibility: a menu xmlid (the tile shows when the user sees that menu),
# None = every internal user, or one of the special keys of _special_visible.
# url None = open the menu in the backend.
TILES = [
    # 19.0.1.108.0: /pedido as staff (seller, administración, gerencia)
    ("pedido", "staff", "Tomar un pedido", "Cargá el pedido de un cliente",
     IMG + "cuentas.png", "order", "/pedido"),
    # 19.0.1.129.18: controllers/mis_ventas.py, the seller's own orders and
    # how they go (Gerencia: everyone's)
    ("misventas", "misventas", "Mis ventas", "Tus pedidos: armado, entrega y cobro",
     IMG + "cuentas.png", "bag", "/mis-ventas"),
    # 19.0.1.108.0: controllers/revisar.py, administración and gerencia
    ("revisar", "revisar", "Pedidos para revisar", "Qué falta confirmar, retener y liberar",
     IMG + "cuentas.png", "target", "/revisar"),
    # 19.0.1.115.0: the driver's app, for a driver with another role too
    # (res.users._distri_landing sends him to Inicio)
    ("mireparto", "repartidor", "Mi reparto", "La app del repartidor: tu ruta del día",
     IMG + "reparto.png", "truck", "/reparto"),
    ("armado", "distri_pedido.menu_armado_app", "Armado", "Pantalla del armador",
     IMG + "armado.png", "box", "/armado"),
    ("despacho", "distri_pedido.menu_reparto_despacho", "Despacho", "Salida de los repartos",
     IMG + "despacho.png", "truck", "/despacho"),
    # 19.0.1.110.0: controllers/rutas.py, administración and gerencia (PC)
    ("rutas", "rutas", "Planificación de rutas", "Pedidos armados a cada ruta del día",
     IMG + "reparto.png", "pin", "/rutas"),
    # 19.0.1.111.0: controllers/agilpedido.py, administración and gerencia
    ("agilpedido", "agilpedido", "AgilPedido", "Pedidos que entran por WhatsApp y cargar uno",
     IMG + "transicion.png", "paste", "/agilpedido"),
    # 19.0.1.109.0: controllers/deposito.py, administración and gerencia
    ("deposito", "deposito", "Depósito", "Entregas, venta en mostrador y caja",
     IMG + "despacho.png", "store", "/deposito"),
    # 19.0.1.92.0: controllers/cobro.py; 19.0.1.128.0: «Cobrar» (cash and
    # transfers in one step), only for the roles that collect (can_collect)
    ("cobro", "cobro", "Cobrar", "Efectivo y transferencias con comprobante, en un paso",
     IMG + "cobros.png", "cash", "/cobro"),
    # 19.0.1.97.0: controllers/caja.py, for the «Cajero» group
    ("caja", "distri_pedido.menu_caja_app", "Caja del día",
     "Abrir, recibir rendiciones, pagar y cerrar", IMG + "cobros.png", "register", "/caja"),
    # 19.0.1.99.0: controllers/comprobantes.py (sellers, administración,
    # gerencia, cajero: the menu of «Cuentas» is the closest group match)
    ("comprobantes", "comprobantes", "Comprobantes", "Subir transferencias y ver cómo siguen",
     IMG + "cobros.png", "receipt", "/comprobantes"),
    # 19.0.1.120.0: whoever opens the accounts app (models/cuentas_app.py
    # app_scope: drivers too, who had it only as a link of /reparto)
    ("cuentas", "cuentas", "Cuentas", "Deudores y estado de cuenta",
     IMG + "cuentas.png", "chart", "/cuentas"),
    # 19.0.1.127.0 (owner): planning is /rutas and loading /despacho, so this
    # tile opens «Rendición de caja»; the whole «Reparto» menu bar stays on top
    # (the web client selects the app of a sub-menu), nothing is lost.
    # 19.0.1.127.1: it opens the kit page /rendiciones (controllers/
    # rendiciones.py); still shown by that menu, so tile_roots() keeps the
    # «Reparto» app out of «Todas las aplicaciones»
    ("reparto", "distri_pedido.menu_reparto_settle", "Rendiciones y casos",
     "Rendir rutas, casos, cargos y ajustes", IMG + "reparto.png", "pin", "/rendiciones"),
    ("cobros", "distri_pedido.menu_cobros_root", "Cobros", "Tablero MP, tarjetas y planillas",
     IMG + "cobros.png", "card", None),
    # 19.0.1.105.0: every cashier's sessions and their closings, for gerencia
    ("historial", "distri_pedido.menu_caja_historial", "Historial de cajas",
     "Las cajas de todos los cajeros, con sus cierres", IMG + "cobros.png", "clock",
     "/caja/historial"),
    # 19.0.1.119.0: the staff training page (controllers/proyecto.py), for
    # every internal user, last so it never pushes the daily work down
    # (19.0.1.122.0: the order is DEFAULT_TILE_ORDER)
    # 19.0.1.129.54: «Novedades» (controllers/novedades.py), what's new and
    # the selling guide, for every internal user, right before «Capacitación»
    ("novedades", None, "Novedades", "Lo nuevo y cómo se trabaja ahora",
     IMG + "app.png", "news", "/novedades"),
    ("capacitacion", None, "Capacitación", "Cómo se usa cada pantalla y dónde practicar",
     IMG + "app.png", "doc", "/proyecto"),
]


# 19.0.1.122.0 (owner): the default order of the tiles, the order of the
# daily work: take the order, review it, pack it, deliver it, collect and
# settle, then the accounts, the cash history, the payments boards and the
# training last. TILES only defines the tiles; this one list orders them
# (a per-user order can replace it later). «Reparto» (backend: routes,
# settlements, cases) goes after «Mi reparto» and «Caja del día» after
# «Cobro en efectivo». A key missing here goes after the listed ones.
DEFAULT_TILE_ORDER = (
    "pedido", "misventas", "agilpedido", "revisar", "armado", "deposito", "rutas", "despacho",
    "mireparto", "reparto", "cobro", "caja", "comprobantes", "cuentas", "historial",
    "cobros", "novedades", "capacitacion",
)


def ordered_tiles(order=DEFAULT_TILE_ORDER):
    """TILES sorted by a list of keys; unknown keys keep their TILES order
    after the listed ones."""
    rank = {key: i for i, key in enumerate(order)}
    return sorted(TILES, key=lambda tile: rank.get(tile[0], len(rank)))


# 19.0.1.120.0: the root menu of a special tile (one not shown by its menu)
SPECIAL_MENUS = {"cuentas": "distri_pedido.menu_cuentas_root"}


def _special_visible(env, key):
    user = env.user
    if key == "staff":
        from ..models.pedido_whatsapp import is_staff
        return is_staff(user)
    if key == "revisar":
        from ..models.revisar import puede_revisar
        return puede_revisar(env)
    if key == "deposito":
        from .deposito import installed, is_depot
        return is_depot(user) and installed(env)
    if key == "rutas":
        from .rutas import installed as rutas_installed, is_planner
        return is_planner(user) and rutas_installed(env)
    if key == "agilpedido":
        from .agilpedido import installed as agil_installed, puede_agil
        return puede_agil(env) and agil_installed(env)
    if key == "comprobantes":
        from ..models.comprobante_subir import can_upload
        return can_upload(user)
    if key == "cuentas":
        from ..models.cuentas_app import app_scope
        return bool(app_scope(env))
    if key == "repartidor":
        return not user.share and user.has_group("distri_pedido.grupo_reparto")
    if key == "misventas":
        from ..models.mis_ventas import puede_ver_mis_ventas
        return puede_ver_mis_ventas(user)
    if key == "cobro":
        from ..models.cobrar import can_collect
        return not user.share and can_collect(user)
    return False


def staff_tiles(env):
    """The tiles the current user may open, in order."""
    visible = None
    tiles = []
    for key, xmlid, title, subtitle, icon, glyph, url in ordered_tiles():
        tile = {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
                "menu_id": False, "url": url, "badge": None}
        if xmlid is None:
            tiles.append(tile)
            continue
        if "." not in xmlid:
            if _special_visible(env, xmlid):
                # the root menu behind a special tile stays out of «Todas
                # las aplicaciones» (Tablero.tablero: taken)
                menu = SPECIAL_MENUS.get(key) and env.ref(SPECIAL_MENUS[key],
                                                          raise_if_not_found=False)
                tile["menu_id"] = menu.id if menu else False
                tiles.append(tile)
            continue
        if visible is None:
            visible = env["ir.ui.menu"]._visible_menu_ids()
        menu = env.ref(xmlid, raise_if_not_found=False)
        if not menu or menu.id not in visible:
            continue
        tile.update(menu_id=menu.id, url=url or "/odoo?menu_id=%s" % menu.id)
        tiles.append(tile)
    nueva = nueva_venta_tile(env)
    if nueva:
        # 19.0.1.129.35 (plan, Etapa 4): «Nueva venta» replaces «Tomar un
        # pedido» (/pedido itself still answers; #27 obsolete)
        tiles = [t for t in tiles if t["key"] != "pedido"]
        tiles.insert(0, nueva)
    verificacion = verificacion_tile(env)
    if verificacion:
        keys = [t["key"] for t in tiles]
        if ui_v2_on(env) and "capacitacion" in keys:
            # 19.0.1.129.42: before «Capacitación», which always goes last
            tiles.insert(keys.index("capacitacion"), verificacion)
        else:
            tiles.append(verificacion)
    contactos = contactos_tile(env)
    if contactos:
        # 19.0.1.129.41: right after «Cuentas» (or last before «Capacitación»)
        keys = [t["key"] for t in tiles]
        where = keys.index("cuentas") + 1 if "cuentas" in keys else (
            keys.index("capacitacion") if "capacitacion" in keys else len(tiles))
        tiles.insert(where, contactos)
    # Owner, 26/11/2026: Ale's direct links to the old payments board and the
    # manual Mercado Pago report import (both under «Cobros»), before
    # «Capacitación»
    for extra in sistemas_tiles(env):
        last = [t["key"] for t in tiles].index("capacitacion") if any(
            t["key"] == "capacitacion" for t in tiles) else len(tiles)
        tiles.insert(last, extra)
    listas = listas_tile(env)
    if listas:
        # 19.0.1.129.52: after «Contactos»/«Cuentas» (or before «Capacitación»)
        keys = [t["key"] for t in tiles]
        where = (keys.index("contactos") + 1 if "contactos" in keys else
                 keys.index("cuentas") + 1 if "cuentas" in keys else
                 keys.index("capacitacion") if "capacitacion" in keys else len(tiles))
        tiles.insert(where, listas)
    equipo = equipo_tile(env)
    if equipo:
        # before «Capacitación», which always goes last
        last = [t["key"] for t in tiles].index("capacitacion") if any(
            t["key"] == "capacitacion" for t in tiles) else len(tiles)
        tiles.insert(last, equipo)
    return tiles


# Owner, 26/11/2026: only for Ale (verificacion.is_owner: admin or Sistemas),
# with or without ui_v2. Not in TILES: «Cobros» already shows these menus to
# administración and gerencia.
SISTEMAS_TILES = (
    ("tablerocobros", "distri_pedido.menu_cobros_board", "Tablero de cobros",
     "El tablero de Mercado Pago de siempre", IMG + "cobros.png", "card"),
    ("reportemp", "distri_pedido.menu_puesta_mp", "Importar reporte MP",
     "Subir a mano un reporte de Mercado Pago", IMG + "cobros.png", "receipt"),
)


def sistemas_tiles(env):
    from ..models.verificacion import is_owner
    if not is_owner(env.user):
        return []
    tiles = []
    for key, xmlid, title, subtitle, icon, glyph in SISTEMAS_TILES:
        menu = env.ref(xmlid, raise_if_not_found=False)
        if not menu:
            continue
        tiles.append({"key": key, "title": title, "subtitle": subtitle, "icon": icon,
                      "glyph": glyph, "menu_id": menu.id, "url": "/odoo?menu_id=%s" % menu.id,
                      "badge": None})
    return tiles


# 19.0.1.129.41: «Contactos» (controllers/contactos.py). Not in TILES: it
# exists only while distri_pedido.ui_v2 is on, for the roles of the record
# screens (vendedor, administración, gerencia, Sistemas); administración's
# badge is the count of customers «a confirmar».
CONTACTOS_TILE = ("contactos", "Contactos", "Clientes: buscar, ver y corregir sus datos",
                  IMG + "app.png", "user", "/contactos")


def contactos_tile(env):
    from ..models.contactos import puede_contactos
    if not (ui_v2_on(env) and puede_contactos(env.user)):
        return None
    key, title, subtitle, icon, glyph, url = CONTACTOS_TILE
    return {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
            "menu_id": False, "url": url, "badge": None}


def _badge_contactos(env):
    """«a confirmar»: the customers created by sellers (and /cobro) that
    administración still has to confirm or merge."""
    from ..models.cliente_provisorio import can_confirm
    if not can_confirm(env):
        return None
    n = env["res.partner"].sudo().search_count([("distri_cliente_provisorio", "=", True)])
    return {"text": "%s a confirmar" % n, "kind": "warn", "n": n} if n else None


# 19.0.1.129.33 (#74): «Equipo» (controllers/equipo.py). Not in TILES: it
# exists only while distri_pedido.ui_v2 is on, for administración (gerencia
# implies it) and Sistemas.
EQUIPO_TILE = ("equipo", "Equipo", "Quién está, su rol y su último ingreso",
               IMG + "app.png", "user", "/equipo")


def equipo_tile(env):
    from .equipo import can_see_team
    if not (ui_v2_on(env) and can_see_team(env.user)):
        return None
    key, title, subtitle, icon, glyph, url = EQUIPO_TILE
    return {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
            "menu_id": False, "url": url, "badge": None}


# 19.0.1.129.52: «Listas y accesos» (controllers/listas.py), only for the
# group «Listas y accesos de distribuidores» (Gonzalo and Ale), with or
# without ui_v2.
LISTAS_TILE = ("listas", "Listas y accesos", "Categoría y precios de clientes, accesos web",
               IMG + "app.png", "list", "/listas")


def listas_tile(env):
    from ..models.listas_distri import puede_listas
    if not puede_listas(env.user):
        return None
    key, title, subtitle, icon, glyph, url = LISTAS_TILE
    return {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
            "menu_id": False, "url": url, "badge": None}


def apply_user_order(tiles, order_text):
    """19.0.1.129.33 (#3): the tiles in the user's own order (a comma list of
    keys); tiles missing from it keep their default place after the listed
    ones, keys of tiles he does not see are ignored."""
    keys = [k for k in (order_text or "").split(",") if k]
    if not keys:
        return list(tiles)
    rank = {key: i for i, key in enumerate(keys)}
    default = {t["key"]: i for i, t in enumerate(tiles)}
    return sorted(tiles, key=lambda t: (0, rank[t["key"]]) if t["key"] in rank
                  else (1, default[t["key"]]))


# 19.0.1.129.30: «Nueva venta» (controllers/nueva_venta.py). Not in TILES:
# it exists only while distri_pedido.nueva_venta is on, first of all.
# 19.0.1.129.35: then it takes the place of «Tomar un pedido» (staff_tiles).
NUEVA_VENTA_TILE = ("nuevaventa", "Nueva venta", "Cliente, productos y cómo sigue la venta",
                    IMG + "cuentas.png", "plus", "/nueva-venta")


def nueva_venta_tile(env):
    """The «Nueva venta» tile for staff who take orders, with the flag on."""
    from ..models.nueva_venta import nueva_venta_on, puede_nueva_venta
    if not (nueva_venta_on(env) and puede_nueva_venta(env.user)):
        return None
    key, title, subtitle, icon, glyph, url = NUEVA_VENTA_TILE
    return {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
            "menu_id": False, "url": url, "badge": None}


# 19.0.1.129.31: «Tablero de verificación» (controllers/verificacion.py).
# Not in TILES: it exists only while distri_pedido.verificacion_v2 is on and
# for a member of one of its queues; its badge is the count of the queues
# with a term (models/verificacion.py DistriVerificacion._badge).
VERIFICACION_TILE = ("verificacion", "Tablero de verificación",
                     "Lo que necesita a una persona, por cola", IMG + "cobros.png", "check",
                     "/verificacion")


def verificacion_tile(env):
    from ..models.verificacion import user_queues, verificacion_on
    if not (verificacion_on(env) and user_queues(env.user)):
        return None
    key, title, subtitle, icon, glyph, url = VERIFICACION_TILE
    return {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
            "menu_id": False, "url": url, "badge": None}


def _badge_verificacion(env):
    return env["distri.verificacion"]._badge(env.user)


# ------------------------------------------------------------ badges

def _money_text(amount):
    from ..models.reparto import _money
    return _money(amount)


def _plural(n, one, many):
    return "%s %s" % (n, one if n == 1 else many)


def pendientes_domain(env):
    """19.0.1.121.0: orders of «Pedidos para revisar» still missing the
    confirmation (its «Por confirmar» tab, held ones included): the badge of
    the «Pedidos para revisar» tile."""
    return (env["sale.order"]._distri_revisar_dominio()
            & Domain("distri_revisar_color", "in", ["rojo", "amarillo"]))


def sin_cliente_domain(env):
    """The ones of pendientes_domain without a real customer (red): the
    banner above the tiles."""
    return (env["sale.order"]._distri_revisar_dominio()
            & Domain("distri_revisar_color", "=", "rojo"))


def _badge_revisar(env):
    n = env["sale.order"].sudo().search_count(pendientes_domain(env))
    # 19.0.1.116.0: packed orders whose automatic invoice failed
    # (models/factura_armado.py), retried with «Facturar lo armado»
    sin_factura = env["sale.order"].sudo().search_count([
        ("state", "=", "sale"), ("distri_factura_error", "!=", False)])
    partes = []
    if n:
        partes.append("%s sin confirmar" % n)
    if sin_factura:
        partes.append("%s armado sin factura" % sin_factura if sin_factura == 1
                      else "%s armados sin factura" % sin_factura)
    sin_cliente = env["sale.order"].sudo().search_count(sin_cliente_domain(env)) if n else 0
    return {"text": " · ".join(partes), "kind": "err" if sin_factura or sin_cliente else "warn",
            "n": n, "sin_cliente": sin_cliente, "sin_factura": sin_factura} if partes else None


def _badge_armado(env):
    """Orders waiting for the packer or being packed: the cheap version of
    /armado's list (controllers/armado.py _pedidos_del_dia), without the
    per-order checks."""
    from .armado import DIAS_PRESUPUESTO
    limit = fields.Datetime.now() - timedelta(days=DIAS_PRESUPUESTO)
    n = env["sale.order"].sudo().search_count([
        ("state", "in", ["draft", "sent", "sale"]),
        "|", "|", ("state", "=", "sale"), ("distri_original", "!=", False),
        ("partner_id.distri_generic_partner", "=", True),
        "|", ("distri_armado", "=", "en_armado"),
        "&", ("distri_armado", "=", "pendiente"), ("distri_retenido", "=", False),
        "|", ("state", "=", "sale"), ("create_date", ">=", limit),
    ])
    return {"text": str(n), "kind": "", "n": n} if n else None


def _badge_despacho(env):
    from ..models.reparto_cliente import today_ar
    n = env["distri.route"].sudo().search_count([
        ("state", "=", "draft"), ("date", "=", today_ar()), ("kind", "=", "route")])
    return {"text": str(n), "kind": "", "n": n} if n else None


def _badge_reparto(env):
    n = env["distri.route"].sudo().search_count([("state", "=", "on_route"),
                                                 ("kind", "=", "route")])
    return {"text": str(n), "kind": "", "n": n} if n else None


def _badge_deposito(env):
    """«Lo voy a retirar» orders packed and waiting at the depot."""
    from .deposito import pickup_count
    n = pickup_count(env)
    return {"text": "%s para retirar" % n, "kind": "", "n": n} if n else None


def _badge_agilpedido(env):
    """AgilPedido orders that came by WhatsApp today."""
    from .agilpedido import pedidos_de_hoy
    n = pedidos_de_hoy(env)
    return {"text": "%s hoy" % n, "kind": "", "n": n} if n else None


def _badge_caja(env):
    if env["distri.caja"].current_for(env.user):
        return {"text": "Abierta", "kind": "warn", "n": 1}
    return None


def _badge_historial(env):
    n = env["distri.caja"].sudo().search_count([("state", "=", "open"),
                                                ("company_id", "=", env.company.id)])
    return {"text": _plural(n, "abierta", "abiertas"), "kind": "warn", "n": n} if n else None


def _badge_comprobantes(env):
    """Uploaded receipts still waiting for administración: the user's own,
    or everybody's for administración and gerencia (who see them all)."""
    from ..models.comprobante_subir import LIST_DAYS, SEE_ALL_GROUPS, _since
    user = env.user
    domain = [("distri_upload", "=", True), ("create_date", ">=", _since(LIST_DAYS)),
              ("distri_receipt_state", "in", ("pending", "no_money"))]
    if not any(user.has_group(g) for g in SEE_ALL_GROUPS):
        domain.append(("distri_forwarded_by_id", "=", user.id))
    n = env["distri.whatsapp.message"].sudo().search_count(domain)
    return {"text": str(n), "kind": "", "n": n} if n else None


def _badge_cobros(env):
    """«Clientes nuevos a confirmar» (menu Cobros), for who confirms them."""
    from ..models.cliente_provisorio import can_confirm
    if not can_confirm(env):
        return None
    n = env["res.partner"].sudo().search_count([("distri_cliente_provisorio", "=", True)])
    return {"text": _plural(n, "nuevo", "nuevos"), "kind": "warn", "n": n} if n else None


def _badge_novedades(env):
    """19.0.1.129.54: «Nuevo» while the newest entry is recent (no query)."""
    from ..models.reparto_cliente import today_ar
    from .novedades import has_new
    return {"text": "Nuevo", "kind": "", "n": 1} if has_new(today_ar()) else None


TILE_BADGES = {
    "revisar": _badge_revisar,
    "armado": _badge_armado,
    "despacho": _badge_despacho,
    "deposito": _badge_deposito,
    "agilpedido": _badge_agilpedido,
    "reparto": _badge_reparto,
    "caja": _badge_caja,
    "historial": _badge_historial,
    "comprobantes": _badge_comprobantes,
    "cobros": _badge_cobros,
    # 19.0.1.129.31 (verificacion_v2 only: the tile exists only then)
    "verificacion": _badge_verificacion,
    # 19.0.1.129.41 (ui_v2 only)
    "contactos": _badge_contactos,
    # 19.0.1.129.54
    "novedades": _badge_novedades,
}


def tile_badges(env, tiles):
    """Fills tile["badge"] for the visible tiles ({text, kind, n} or None)
    and returns the «pedidos sin cliente» banner (or None)."""
    banner = None
    for tile in tiles:
        badge_fn = TILE_BADGES.get(tile["key"])
        tile["badge"] = badge_fn(env) if badge_fn else None
        if tile["key"] == "revisar" and tile["badge"] and tile["badge"].get("sin_cliente"):
            n = tile["badge"]["sin_cliente"]
            banner = {
                "text": ("1 pedido sin cliente espera que le asignes el cliente" if n == 1 else
                         "%s pedidos sin cliente esperan que les asignes el cliente" % n),
                "url": "/revisar",
            }
    return banner


def agil_banner(env):
    """19.0.1.113.0: the red «Se frenó la sincronización con AgilPedido» banner
    for administración and gerencia while the incident is open
    (models/catalogo_agil.py), else None."""
    user = env.user
    if not (user.has_group("distri_pedido.grupo_administracion")
            or user.has_group("base.group_system")):
        return None
    return env["distri.agil.sincronizacion"].sudo()._distri_banner_incidente()


def reclamar_row(env):
    """19.0.1.129.37: «Mis comprobantes a reclamar · N · $ X» of a user who
    delivered at the depot with «Lo manda después» (nueva_venta only; None
    when there is nothing or the user cannot open /deposito)."""
    from .deposito import is_depot
    user = env.user
    if not is_depot(user):
        return None
    count, total = env["distri.promised.transfer"]._distri_user_summary(user)
    if not count:
        return None
    from ..models.reparto import _money
    return {"url": "/deposito/reclamar",
            "text": "Mis comprobantes a reclamar: %s · %s" % (
                _plural(count, "comprobante", "comprobantes"), _money(total))}


# 19.0.1.129.48 (owner): «Mis comprobantes a reclamar» as the first block of
# Inicio (ui_v2), for whoever is asked to help claim receipts: the depot
# («Lo manda después»), the driver (his pending receipts, reparto_v2) and the
# seller (his customers' pending receipts). One merged list, no totals,
# nothing «a tu cargo»; the first RECLAMAR_MAX rows show, the rest fold.
RECLAMAR_MAX = 5


def reclamar_roles(env):
    """The roles of the Inicio block that apply to the user (a set of
    models/reclamar_inicio.py ROLE_ORDER keys); empty with ui_v2 off."""
    from .deposito import is_depot
    from .rendiciones_v2 import sees_promises
    from ..models.nueva_venta import nueva_venta_on
    from ..models.reparto_v2 import reparto_v2_on
    user = env.user
    if user.share or not ui_v2_on(env):
        return set()
    roles = set()
    if is_depot(user) and nueva_venta_on(env):
        roles.add("deposito")
    if user.has_group("distri_pedido.grupo_reparto") and reparto_v2_on(env):
        roles.add("reparto")
    # administración implies the seller group: only a real seller (or an
    # administración user with his own portfolio) gets his customers here
    staff = user.has_group("distri_pedido.grupo_administracion") \
        or user.has_group("base.group_system")
    seller = user.has_group("distri_pedido.grupo_vendedor") and (
        not staff or user.sudo().etiqueta_cartera_id)
    if seller and sees_promises(env):
        roles.add("vendedor")
    return roles


def reclamar_block(env):
    """{"rows", "more", "n"} for the Inicio block, or None when there is
    nothing to claim (the block is then not drawn)."""
    roles = reclamar_roles(env)
    if not roles:
        return None
    rows = env["distri.promised.transfer"]._distri_inicio_claims(env.user, roles)
    if not rows:
        return None
    return {"rows": rows[:RECLAMAR_MAX], "more": rows[RECLAMAR_MAX:], "n": len(rows)}


def my_route_today(env):
    """«Mi reparto de hoy» for a user that also drives (administración
    implies the driver group, so only with a route of today)."""
    user = env.user
    if not user.has_group("distri_pedido.grupo_reparto"):
        return None
    from ..models.reparto_cliente import today_ar
    route = env["distri.route"].sudo().search([
        ("driver_id", "=", user.id), ("date", "=", today_ar()), ("kind", "=", "route"),
        ("state", "in", ("draft", "on_route"))], order="id desc", limit=1)
    if not route:
        return None
    states = route.stop_ids.mapped("state")
    done = len([s for s in states if s != "pending"])
    return {"url": "/reparto",
            "detail": "%s · %s · %s" % (route.name, _plural(len(states), "visita", "visitas"),
                                        _plural(done, "hecha", "hechas"))}


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


def tile_roots(env, tiles):
    """The menus of the tiles and the root menu of each (19.0.1.127.0)."""
    ids = {t["menu_id"] for t in tiles if t["menu_id"]}
    for menu in env["ir.ui.menu"].sudo().browse(sorted(ids)).exists():
        root = (menu.parent_path or "").split("/")[0]
        if root.isdigit():
            ids.add(int(root))
    return ids


# 19.0.1.129.42 (ui_v2): administración's Inicio in groups, each with its
# label; inside a group the tiles keep their order (the user's own order
# too). A tile of no group goes to «Sistema».
TILE_GROUPS = (
    ("Ventas", ("nuevaventa", "pedido", "misventas", "agilpedido", "revisar")),
    ("Reparto", ("armado", "deposito", "rutas", "despacho", "mireparto", "reparto")),
    ("Cobros y caja", ("cobro", "caja", "comprobantes", "historial", "cobros", "verificacion",
                       "tablerocobros", "reportemp")),
    ("Clientes", ("contactos", "cuentas", "listas")),
    ("Sistema", ("equipo", "novedades", "capacitacion")),
)
GROUPED_ROLES = ("distri_pedido.grupo_administracion", "base.group_system")


def grouped_tiles(tiles):
    """[{"label", "tiles"}] in TILE_GROUPS order, empty groups left out.
    19.0.1.129.47: the group of the first tile (the main one, main_tile)
    goes first, so the main tile is always on top; with the default order
    it already is the first group."""
    where = {key: i for i, (_label, keys) in enumerate(TILE_GROUPS) for key in keys}
    last = len(TILE_GROUPS) - 1
    groups = [{"label": label, "tiles": []} for label, _keys in TILE_GROUPS]
    for tile in tiles:
        groups[where.get(tile["key"], last)]["tiles"].append(tile)
    groups = [g for g in groups if g["tiles"]]
    if tiles:
        lead = [g for g in groups if g["tiles"][0]["key"] == tiles[0]["key"]]
        groups = lead + [g for g in groups if g not in lead]
    return groups


# 19.0.1.129.47 (ui_v2): the design system's Inicio («HomeTile»): every role
# sees its tiles in labelled groups, the first tile of his order (the role's
# main task: «Nueva venta», «Armado», «Cobrar»...) highlighted on top, and
# the other tiles with the name only and a short badge.
BADGE_SHORT = 8


def main_tile(tiles):
    """The highlighted tile: the first one of the user's order, or None."""
    return tiles[0] if tiles else None


# 19.0.1.129.50 (owner: «los textos de cada icono son largos y se rompen»):
# the short name a design-system tile shows (ui_v2 Inicio only). The full
# title stays everywhere else (flag-off Inicio, «Ordenar el Inicio», menus)
# and, on Inicio, in the tile's tooltip and its spoken name.
TILE_LABELS = {
    "revisar": "Revisar",
    "rutas": "Rutas",
    "reparto": "Rendiciones",
    "historial": "Cajas",
    "verificacion": "Verificación",
    "reportemp": "Reporte MP",
    "tablerocobros": "Cobros MP",
}


def tile_label(tile):
    """The tile's short name on Inicio (TILE_LABELS), else its title."""
    return TILE_LABELS.get(tile["key"]) or tile["title"]


def badge_compact(badge):
    """19.0.1.129.50: the badge of a phone tile (3 columns): its leading
    number («4 nuevos» -> «4», «441 · 436 vencidos» -> «441»), or "" for a
    word («Abierta»), drawn as a dot. The full text is in the spoken name."""
    if not badge:
        return ""
    match = re.match(r"\d+", badge.get("text") or "")
    return match.group(0) if match else ""


def label_is_long(label):
    """A name with a word of 11+ letters: one pixel smaller on narrow tiles."""
    return max((len(word) for word in (label or "").split()), default=0) >= 11


def badge_short(badge):
    """The badge text a two-column tile has room for: the text itself when
    it is short («5», «Abierta», «2 hoy»), else its count («12 sin
    confirmar» -> «12»); the full text stays in the tile's label."""
    if not badge:
        return ""
    text = badge.get("text") or ""
    if len(text) <= BADGE_SHORT:
        return text
    n = badge.get("n") or badge.get("sin_factura") or ""
    return str(n) if n else text.split(" ")[0]


def wants_groups(user):
    """Administración (gerencia implies it) and Sistemas: many tiles.
    Unused since 19.0.1.129.47 (every role gets the groups with ui_v2)."""
    return any(user.has_group(g) for g in GROUPED_ROLES)


def hides_apps(user):
    """19.0.1.129.42 (ui_v2): Odoo's own apps («Todas las aplicaciones del
    sistema») are nothing of a packer's or driver's work: hidden for a user
    whose roles are only those (not administración, Sistemas, cajero or
    vendedor, who may use some)."""
    keep = ("distri_pedido.grupo_administracion", "base.group_system",
            "distri_pedido.grupo_cajero", "distri_pedido.grupo_vendedor")
    if any(user.has_group(g) for g in keep):
        return False
    return user.has_group("distri_pedido.grupo_armador") or user.has_group(
        "distri_pedido.grupo_reparto")


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
        env = request.env
        tiles = staff_tiles(env)
        # a seller locked out of the backend lands here (19.0.1.129.20, before
        # only if he also packed or drove); a backend page would only send him
        # back (controllers/tienda.py web_client), so he gets no backend tile
        # nor «Todas las aplicaciones»
        locked = user._distri_seller_only_accounts()
        if locked:
            tiles = [t for t in tiles if not (t["url"] or "").startswith("/odoo")]
        # 19.0.1.129.33 (#3): the user's own order (ui_v2 only)
        ordering = ui_v2_on(env)
        if ordering:
            tiles = apply_user_order(tiles, user.distri_inicio_orden)
        banner = tile_badges(env, tiles)
        # the root menus behind our tiles (Armado/Despacho tiles are not root menus;
        # 19.0.1.127.0: a tile on a sub-menu takes its root too)
        taken = tile_roots(env, tiles)
        # 19.0.1.129.47: the design system's page with ui_v2 on
        template = "distri_pedido.tablero_v2_page" if ordering else "distri_pedido.tablero_page"
        html = env["ir.qweb"]._render(template, {
            "tiles": tiles,
            "banner": banner,
            "agil_banner": agil_banner(env),
            "mi_ruta": my_route_today(env),
            # 19.0.1.129.37 (nueva_venta): «Lo manda después» left at his name;
            # 19.0.1.129.48: with ui_v2 the block below replaces that line
            "reclamar": None if ordering else reclamar_row(env),
            "reclamos": reclamar_block(env) if ordering else None,
            "money": _money_text,
            "apps": [] if locked or (ordering and hides_apps(user)) else other_apps(env, taken),
            # 19.0.1.129.42 (ui_v2): administración's tiles in groups
            # 19.0.1.129.47 (ui_v2): every role's tiles in groups (until
            # 129.46 only administración and Sistemas: wants_groups)
            "grupos": grouped_tiles(tiles) if ordering else [],
            "main": main_tile(tiles) if ordering else None,
            "badge_short": badge_short,
            "tile_label": tile_label,
            "badge_compact": badge_compact,
            "label_is_long": label_is_long,
            "v2": ordering,
            "user": user,
            "empresa": FICHA["nombre"],
            "dn": kit_context("tablero.css"),
            # the v107 template (until the module is updated) uses these
            "shared_css": SHARED_CSS,
            # 19.0.1.126.0: the one-time notice of the instant crediting
            "aviso_acreditar": CREDIT_NOTICE if user._distri_credit_notice_pending() else "",
            "csrf_token": request.csrf_token(),
            # 19.0.1.129.33 (#3): «Ordenar el Inicio» (ui_v2 only)
            "ordenar_url": "/tablero/ordenar" if ordering else "",
        })
        return request.make_response(html, headers=[("Content-Type", "text/html; charset=utf-8"),
                                                    ("Cache-Control", "no-store")])

    @http.route("/tablero/aviso-acreditar", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def aviso_acreditar(self, volver="", **kw):
        """«Entendido» on the notice of the instant crediting (19.0.1.126.0):
        the moment is kept on the user (res.users.distri_aviso_acreditar_visto)."""
        request.env.user._distri_credit_notice_seen()
        target = volver if volver in ("/tablero", "/comprobantes") else "/tablero"
        return request.redirect(target, 303)

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
            "icons": marca_manifest_icons(),
            # 19.0.1.99.0 (audit 3a step 2): «Compartir con Distri» from
            # WhatsApp on Android (controllers/comprobantes.py)
            "share_target": {
                "action": "/comprobantes/compartir",
                "method": "POST",
                "enctype": "multipart/form-data",
                "params": {
                    "title": "titulo",
                    "text": "texto",
                    "files": [{"name": "archivos",
                               "accept": ["image/*", "application/pdf", ".pdf"]}],
                },
            },
        }
        return request.make_response(json.dumps(data, ensure_ascii=False), headers=[
            ("Content-Type", "application/manifest+json; charset=utf-8")])
