"""«Novedades» (19.0.1.129.54): what's new for the staff, on the phone.

    /novedades           GET  the entries, newest first
    /novedades/<slug>    GET  one entry (views/novedades.xml)

The first entry is the «Nueva venta» selling guide, the content of the
training poster sent to the staff (1080 x 5700 px, unreadable on a phone)
laid out for a 380 px screen. Staff only, like /proyecto (proyecto.
staff_only): logged out -> the app's login (/web/login, controllers/
tienda.py) and back to the same entry after it, so a link shared by
WhatsApp works; a customer's portal account -> 404. Read only: nothing here
writes a record.

Adding an entry: its template in views/novedades.xml (copy
novedades_venta: <body data-parent="/novedades">, the top bar, the share
block) and one line in ENTRIES. The screen's parent (controllers/kit.py
SCREEN_PARENTS) and the Inicio tile's «Nuevo» badge follow from ENTRIES.
"""
from datetime import date, timedelta
from urllib.parse import quote

from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request

from .kit import SCREEN_PARENTS, kit_context, page_nav
from .proyecto import staff_only
from .tablero import wants_tablero

INDEX = "/novedades"
INDEX_TEMPLATE = "distri_pedido.novedades_index"

# (slug, date, title, summary, template), newest first.
ENTRIES = (
    ("venta", date(2026, 11, 26), "Cómo vender con «Nueva venta»",
     "Toda venta empieza en «Nueva venta»: los cuatro caminos, qué mueve cada documento "
     "y el botón para cada caso.",
     "distri_pedido.novedades_venta"),
)
# the Inicio tile shows «Nuevo» while the newest entry is this recent
NEW_DAYS = 14

# every entry is a declared screen under the index (#100: «Atrás» goes up)
SCREEN_PARENTS.update({"%s/%s" % (INDEX, slug): (INDEX,) for slug, *_rest in ENTRIES})


def entries():
    """ENTRIES as dicts, newest first."""
    rows = [{"slug": slug, "date": when, "date_text": when.strftime("%d/%m/%Y"),
             "title": title, "summary": summary, "template": template,
             "url": "%s/%s" % (INDEX, slug)}
            for slug, when, title, summary, template in ENTRIES]
    return sorted(rows, key=lambda row: row["date"], reverse=True)


def entry(slug):
    return next((row for row in entries() if row["slug"] == slug), None)


def has_new(today):
    """Whether the newest entry is at most NEW_DAYS old on `today`."""
    rows = entries()
    return bool(rows) and rows[0]["date"] >= today - timedelta(days=NEW_DAYS)


def absolute_url(env, path):
    """`path` on this server: web.base.url, else the request's host (the
    same rule as controllers/main.py and controllers/sitio.py)."""
    base = env["ir.config_parameter"].sudo().get_param("web.base.url") or (
        request.httprequest.host_url if request else "")
    return (base or "").rstrip("/") + path


def whatsapp_text(title, url):
    """The message «Compartir por WhatsApp» prefills."""
    if title:
        return "Novedades de Distri Neuquén: %s. Miralo acá: %s" % (title, url)
    return "Novedades de Distri Neuquén. Miralas acá: %s" % url


def share_values(env, path, title=""):
    url = absolute_url(env, path)
    return {"url": url,
            "whatsapp": "https://wa.me/?text=%s" % quote(whatsapp_text(title, url), safe="")}


class Novedades(http.Controller):

    def _render(self, template, values):
        env = request.env
        user = env.user
        base = {
            "dn": kit_context("deposito.css", "novedades.css"),
            "user": user,
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            # the role's bottom bar (ui_v2 only)
            "nav": page_nav(env, "novedades"),
        }
        base.update(values)
        html = env["ir.qweb"]._render(template, base)
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store"),
            ("X-Robots-Tag", "noindex, nofollow")])

    # auth="public" so staff_only decides: the app's login with the way back
    # for a visitor, 404 for a portal account (never Odoo's own redirect)
    @http.route(INDEX, type="http", auth="public", methods=["GET"], sitemap=False)
    @staff_only
    def index(self, **kw):
        return self._render(INDEX_TEMPLATE, {
            "entries": entries(),
            "share": share_values(request.env, INDEX),
        })

    @http.route("%s/<string:slug>" % INDEX, type="http", auth="public", methods=["GET"],
                sitemap=False)
    @staff_only
    def show(self, slug, **kw):
        row = entry(slug)
        if not row:
            raise NotFound()
        return self._render(row["template"], {
            "entry": row,
            "share": share_values(request.env, row["url"], row["title"]),
        })
