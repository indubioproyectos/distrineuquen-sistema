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
from datetime import timedelta

from odoo import fields, http
from odoo.fields import Domain
from odoo.http import request

from .main import FICHA
from .kit import LOGO_V, kit_context
from .reparto import SHARED_CSS

IMG = "/distri_pedido/static/src/img/iconos/"

# 19.0.1.108.0 (owner-approved mockup «Inicio»): the tablero is a kit page
# (views/tablero.xml, static/src/tablero/tablero.css). Every tile has a
# sprite glyph (static/src/kit/dn-icons.svg) and, when it is cheap, a count
# badge (TILE_BADGES): one search_count per visible tile, nothing for the
# hidden ones. Above the tiles, the «pedidos en rojo» banner.
#
# (key, visibility, title, subtitle, legacy PNG icon, glyph, url)
# visibility: a menu xmlid (the tile shows when the user sees that menu),
# None = every internal user, or one of the special keys of _special_visible.
# url None = open the menu in the backend.
TILES = [
    # 19.0.1.108.0: /pedido as staff (seller, administración, gerencia)
    ("pedido", "staff", "Tomar un pedido", "Cargá el pedido de un cliente",
     IMG + "cuentas.png", "order", "/pedido"),
    # 19.0.1.108.0: controllers/revisar.py, administración and gerencia
    ("revisar", "revisar", "Pedidos para revisar", "Semáforo, retener y liberar",
     IMG + "cuentas.png", "target", "/revisar"),
    ("armado", "distri_pedido.menu_armado_app", "Armado", "Pantalla del armador",
     IMG + "armado.png", "box", "/armado"),
    ("despacho", "distri_pedido.menu_reparto_despacho", "Despacho", "Salida de los repartos",
     IMG + "despacho.png", "truck", "/despacho"),
    # 19.0.1.92.0: controllers/cobro.py, for every internal user
    ("cobro", None, "Cobro en efectivo", "Registrar el efectivo que te dio un cliente",
     IMG + "cobros.png", "cash", "/cobro"),
    # 19.0.1.97.0: controllers/caja.py, for the «Cajero» group
    ("caja", "distri_pedido.menu_caja_app", "Caja del día",
     "Abrir, recibir rendiciones, pagar y cerrar", IMG + "cobros.png", "register", "/caja"),
    # 19.0.1.99.0: controllers/comprobantes.py (sellers, administración,
    # gerencia, cajero: the menu of «Cuentas» is the closest group match)
    ("comprobantes", "comprobantes", "Comprobantes", "Subir transferencias y ver cómo siguen",
     IMG + "cobros.png", "receipt", "/comprobantes"),
    ("cuentas", "distri_pedido.menu_cuentas_root", "Cuentas", "Deudores y estado de cuenta",
     IMG + "cuentas.png", "chart", "/cuentas"),
    ("reparto", "distri_pedido.menu_reparto_root", "Reparto", "Rutas, rendiciones y casos",
     IMG + "reparto.png", "pin", None),
    ("cobros", "distri_pedido.menu_cobros_root", "Cobros", "Tablero MP, tarjetas y planillas",
     IMG + "cobros.png", "card", None),
    # 19.0.1.105.0: every cashier's sessions and their closings, for gerencia
    ("historial", "distri_pedido.menu_caja_historial", "Historial de cajas",
     "Las cajas de todos los cajeros, con sus cierres", IMG + "cobros.png", "clock",
     "/caja/historial"),
]


def _special_visible(env, key):
    user = env.user
    if key == "staff":
        from ..models.pedido_whatsapp import is_staff
        return is_staff(user)
    if key == "revisar":
        from ..models.revisar import puede_revisar
        return puede_revisar(env)
    if key == "comprobantes":
        from ..models.comprobante_subir import can_upload
        return can_upload(user)
    return False


def staff_tiles(env):
    """The tiles the current user may open, in order."""
    visible = None
    tiles = []
    for key, xmlid, title, subtitle, icon, glyph, url in TILES:
        tile = {"key": key, "title": title, "subtitle": subtitle, "icon": icon, "glyph": glyph,
                "menu_id": False, "url": url, "badge": None}
        if xmlid is None:
            tiles.append(tile)
            continue
        if "." not in xmlid:
            if _special_visible(env, xmlid):
                tiles.append(tile)
            continue
        if visible is None:
            visible = env["ir.ui.menu"]._visible_menu_ids()
        menu = env.ref(xmlid, raise_if_not_found=False)
        if not menu or menu.id not in visible:
            continue
        tile.update(menu_id=menu.id, url=url or "/odoo?menu_id=%s" % menu.id)
        tiles.append(tile)
    return tiles


# ------------------------------------------------------------ badges

def _plural(n, one, many):
    return "%s %s" % (n, one if n == 1 else many)


def red_domain(env):
    """Red orders of «Pedidos para revisar» that nobody held and nobody
    packed yet: the banner and the badge of the «Pedidos para revisar» tile."""
    return (env["sale.order"]._distri_revisar_dominio()
            & Domain("distri_semaforo", "=", "rojo") & Domain("distri_retenido", "=", False)
            & Domain("distri_armado", "=", "pendiente"))


def _badge_revisar(env):
    n = env["sale.order"].sudo().search_count(red_domain(env))
    return {"text": _plural(n, "rojo", "rojos"), "kind": "err", "n": n} if n else None


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


TILE_BADGES = {
    "revisar": _badge_revisar,
    "armado": _badge_armado,
    "despacho": _badge_despacho,
    "reparto": _badge_reparto,
    "caja": _badge_caja,
    "historial": _badge_historial,
    "comprobantes": _badge_comprobantes,
    "cobros": _badge_cobros,
}


def tile_badges(env, tiles):
    """Fills tile["badge"] for the visible tiles ({text, kind, n} or None)
    and returns the «pedidos en rojo» banner (or None)."""
    banner = None
    for tile in tiles:
        badge_fn = TILE_BADGES.get(tile["key"])
        tile["badge"] = badge_fn(env) if badge_fn else None
        if tile["key"] == "revisar" and tile["badge"]:
            n = tile["badge"]["n"]
            banner = {
                "text": ("1 pedido en rojo espera revisión antes de armarse" if n == 1 else
                         "%s pedidos en rojo esperan revisión antes de armarse" % n),
                "url": "/revisar?ver=rojos",
            }
    return banner


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
        banner = tile_badges(env, tiles)
        # the root menus behind our tiles (Armado/Despacho tiles are not root menus)
        taken = {t["menu_id"] for t in tiles}
        html = env["ir.qweb"]._render("distri_pedido.tablero_page", {
            "tiles": tiles,
            "banner": banner,
            "mi_ruta": my_route_today(env),
            "apps": other_apps(env, taken),
            "user": user,
            "empresa": FICHA["nombre"],
            "dn": kit_context("tablero.css"),
            # the v107 template (until the module is updated) uses these
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
                {"src": IMG + "app-192.png" + LOGO_V, "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": IMG + "app-512.png" + LOGO_V, "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": IMG + "app-maskable-512.png" + LOGO_V, "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
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
