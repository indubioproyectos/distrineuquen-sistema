"""19.0.1.129.33 («Etapa 3b», part 1), both behind distri_pedido.ui_v2
(OFF by default: with it off both routes answer 404 and Inicio shows
neither the tile nor the link).

- «Equipo» (/equipo, #74): who is on the team, their role(s), active or
  suspended, and their last login. Read-only, for administración (gerencia
  implies it) and Sistemas. Nothing here writes a user, a group or a
  password: suspending and reactivating users stays in the backend
  (Ajustes › Usuarios, Sistemas).
- «Ordenar el Inicio» (/tablero/ordenar, #3): each user puts his Inicio
  tiles in his own order with ↑/↓ (the same choice as the driver's
  «Cambiar el orden»: on a phone arrows work better than dragging) and
  «Restablecer» goes back to the default order (tablero.DEFAULT_TILE_ORDER).
  The order is a list of tile keys on res.users.distri_inicio_orden; the
  only write is that field, on the user himself.
"""
from zoneinfo import ZoneInfo

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from .kit import kit_context, page_nav, ui_v2_on
from .tablero import apply_user_order, staff_tiles, wants_tablero

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
TEAM_GROUPS = ("distri_pedido.grupo_administracion", "base.group_system")
# users that are not people of the team
NOT_PEOPLE = ("base.user_root", "base.default_user", "base.public_user")


def can_see_team(user):
    """Administración (gerencia implies it) and Sistemas."""
    return bool(user) and not user.share and any(user.has_group(g) for g in TEAM_GROUPS)


def _when(value):
    if not value:
        return ""
    return value.replace(tzinfo=ZoneInfo("UTC")).astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")


def team_rows(env):
    """Every internal user (active or not), active first, by name: name,
    role(s) as the «Ajustes» sheet names them, active and the last login.
    19.0.1.129.51: whether the user confirmed an e-mail («Mi correo») and
    the address, masked (m***@gmail.com) for everybody but Sistemas."""
    from .correo import masked_or_full
    skip = set()
    for xmlid in NOT_PEOPLE:
        rec = env.ref(xmlid, raise_if_not_found=False)
        if rec:
            skip.add(rec.id)
    users = env["res.users"].sudo().with_context(active_test=False).search(
        [("share", "=", False)], order="active desc, name, id")
    rows = []
    for user in users:
        if user.id in skip:
            continue
        rows.append({
            "id": user.id,
            "name": user.name,
            "roles": user._distri_rol_texto(),
            "active": user.active,
            "last_login": _when(user.login_date),
            "email": masked_or_full(env.user, user._distri_verified_email()),
        })
    return rows


def _order_keys(env):
    """The keys of the current user's tiles, in the order Inicio shows them."""
    user = env.user
    return [t["key"] for t in apply_user_order(staff_tiles(env), user.distri_inicio_orden)]


class Equipo(http.Controller):

    def _page(self, template, values):
        user = request.env.user
        base = {
            "dn": kit_context("deposito.css", "equipo.css"),
            "user": user,
            "inicio": "/tablero",
            "nav": page_nav(request.env, "equipo"),
            "ok": "",
            "error": "",
            "csrf_token": request.csrf_token(),
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    # ------------------------------------------------------------ #74

    @http.route("/equipo", type="http", auth="user", methods=["GET"], sitemap=False)
    def equipo(self, **kw):
        env = request.env
        if not ui_v2_on(env):
            raise NotFound()
        if not can_see_team(env.user):
            return request.redirect("/tablero", 303)
        rows = team_rows(env)
        active = len([r for r in rows if r["active"]])
        off = len(rows) - active
        return self._page("distri_pedido.equipo_page", {
            "rows": rows,
            "summary": "%s · %s" % ("1 activo" if active == 1 else "%s activos" % active,
                                    "1 suspendido" if off == 1 else "%s suspendidos" % off)})

    # ------------------------------------------------------------ #3

    def _ordering_allowed(self):
        env = request.env
        if not ui_v2_on(env):
            raise NotFound()
        return wants_tablero(env.user)

    @http.route("/tablero/ordenar", type="http", auth="user", methods=["GET"], sitemap=False)
    def ordenar(self, **kw):
        if not self._ordering_allowed():
            return request.redirect("/tablero", 303)
        env = request.env
        tiles = apply_user_order(staff_tiles(env), env.user.distri_inicio_orden)
        return self._page("distri_pedido.tablero_ordenar_page", {
            "tiles": tiles, "custom": bool(env.user.distri_inicio_orden)})

    @http.route("/tablero/ordenar", type="http", auth="user", methods=["POST"], sitemap=False)
    def ordenar_guardar(self, key="", direction="", reset="", **kw):
        """↑/↓ on one tile, or «Restablecer». Saves the whole visible order
        (plus the saved keys of tiles the user does not see now, at the end,
        so a tile that comes back keeps its place)."""
        if not self._ordering_allowed():
            return request.redirect("/tablero", 303)
        env = request.env
        user = env.user
        if reset:
            user.sudo().write({"distri_inicio_orden": False})
        else:
            keys = _order_keys(env)
            if key in keys and direction in ("up", "down"):
                i = keys.index(key)
                j = i - 1 if direction == "up" else i + 1
                if 0 <= j < len(keys):
                    keys[i], keys[j] = keys[j], keys[i]
                    saved = [k for k in (user.distri_inicio_orden or "").split(",") if k]
                    keys += [k for k in saved if k not in keys]
                    user.sudo().write({"distri_inicio_orden": ",".join(keys)})
        if request.httprequest.headers.get("X-Distri-Fetch"):
            return request.make_json_response({"ok": True, "order": _order_keys(env)},
                                              headers=[("Cache-Control", "no-store")])
        return request.redirect("/tablero/ordenar", 303)
