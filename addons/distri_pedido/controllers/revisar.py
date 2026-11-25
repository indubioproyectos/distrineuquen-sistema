"""«Pedidos para revisar» on the phone (19.0.1.108.0, owner-approved mockup
«01 Pedidos para revisar» + «Pegar número»): /revisar.

    /tablero (staff home)
      /revisar                       the cards of today's and yesterday's
                                     orders plus every held one, what is
                                     missing confirmation first (tabs Por
                                     confirmar / Confirmados / Retenidos /
                                     Todos, filtered in the page)
      /revisar/<id>/retener   POST   «Retener» with its reason
      /revisar/<id>/liberar   POST   «Liberar»
      /revisar/<id>/numero    GET    «Pegar número»: what the pasted text
                                     reads as, the warnings and «Ya tienen
                                     este número» (JSON)
      /revisar/<id>/numero    POST   saves it as the verified WhatsApp
      /revisar/<id>/asignar          «Asignar cliente» of an order without
                                     customer: the kit screen since
                                     19.0.1.111.0 (controllers/asignar.py)

Same access as the backend action «Pedidos para revisar»: administración
(gerencia implies it). The rules are the model's (models/revisar.py,
models/pegar_numero.py, models/pedido_link.py): this file only translates
HTTP. The page sends every action with fetch() (X-Distri-Fetch: 1) and gets
JSON with the card re-rendered; Retener and Liberar also work as plain
forms without JavaScript.

19.0.1.121.0 (owner, from real use): the colour of a card is the order
status (models/semaforo.py color_estado): red not confirmed and without
customer, yellow not confirmed, green confirmed. The risk reasons are chips
of the orders not confirmed yet.
"""
from datetime import datetime, timedelta

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.asignar_cliente import puede_asignar
from ..models.reparto import _money
from ..models.revisar import MOTIVO_MAX, ORIGENES_REVISAR, puede_revisar
from ..models.semaforo import AR_TZ, GREEN, GREY, RED, SIN_CONFIRMAR, UTC, YELLOW, chips
from .fichas import record_link
from .kit import kit_context, page_nav, ui_v2_on
from .tablero import wants_tablero

LIMIT = 300
# 19.0.1.121.0: «Por confirmar» (red and yellow) is the default tab
TABS = [("pendientes", "Por confirmar"), ("confirmados", "Confirmados"),
        ("retenidos", "Retenidos"), ("todos", "Todos")]
DEFAULT_TAB = "pendientes"
# the tabs of before 19.0.1.121.0 (old links, the Inicio banner)
OLD_TABS = {"rojos": "pendientes", "amarillos": "pendientes"}
COLOR_LABEL = {RED: "Sin cliente", YELLOW: "Sin confirmar", GREEN: "Confirmado",
               GREY: "Cancelado"}
# the order's WhatsApp state worth a chip on the card: (label, chip kind)
WA_CHIP = {
    "waiting": ("Esperando el WhatsApp del cliente", "warn"),
    "received": ("Llegó el WhatsApp con el código", "ok"),
    "manual": ("WhatsApp confirmado a mano", "ok"),
}
DIAS = ("lun", "mar", "mié", "jue", "vie", "sáb", "dom")
LIBERAR_TEXTO = ("El pedido vuelve al circuito normal: se arma, se factura y sale en ruta. "
                 "¿Seguimos?")


def _error_text(exc):
    return exc.args[0] if exc.args else str(exc)


def cuando(moment, now=None):
    """«hoy 09:42», «ayer 19:30» or «lun 16/11 08:05» (Argentine time)."""
    if not moment:
        return ""
    local = moment.replace(tzinfo=UTC).astimezone(AR_TZ)
    today = (now or datetime.now(UTC)).astimezone(AR_TZ).date()
    hora = local.strftime("%H:%M")
    if local.date() == today:
        return "hoy %s" % hora
    if local.date() == today - timedelta(days=1):
        return "ayer %s" % hora
    return "%s %s/%s %s" % (DIAS[local.weekday()], local.day, local.month, hora)


def _phone(value):
    """«299 512-0447» for a stored Argentine phone (no line break inside),
    as written otherwise."""
    from ..models.ar_phone import format_local, phone_match_key, split_ar_national
    national = phone_match_key(value)
    if len(national) != 10:
        return value
    area, local = split_ar_national(national)
    return "%s\u00a0%s" % (area, format_local(local).replace("-", "\u2011"))


def card(order):
    """What the card of one order shows (views/revisar_app.xml revisar_card)."""
    env = order.env
    o = order.sudo()
    generic = bool(not o.partner_id or o.partner_id.distri_generic_partner)
    color = o.distri_revisar_color or GREEN
    # the risk chips only while the order waits for the confirmation
    riesgos = chips(o.distri_semaforo_motivos) if o.state in SIN_CONFIRMAR else []
    origen = dict(ORIGENES_REVISAR).get(o.distri_revisar_origen, "")
    meta = [origen] if origen else []
    if o.distri_wa_code:
        meta.append("código %s" % o.distri_wa_code)
    meta.append(cuando(o.date_order))
    typed = [v for v in ((o.distri_telefono or "").strip(), (o.distri_domicilio or "").strip()) if v]
    vivo = o.state != "cancel"
    retenido = bool(o.distri_retenido)
    nota_armado = ""
    if retenido and o.distri_armado == "en_armado":
        nota_armado = ("El armador ya lo había tomado: puede terminarlo, pero no se factura ni "
                       "sale en una ruta hasta liberarlo.")
    elif retenido and o.distri_armado == "armado":
        nota_armado = ("Ya está armado: no se factura ni sale en una ruta hasta liberarlo. Al "
                       "liberarlo sale la factura del armado.")
    wa_state = o.distri_wa_state
    return {
        "id": o.id,
        "name": o.name,
        "color": color,
        "color_label": COLOR_LABEL.get(color, ""),
        "amount": _money(o.amount_total),
        "cliente": ((o.distri_nombre or "Sin nombre") if generic else o.partner_id.name) or "",
        "generic": generic,
        "typed": " · ".join(typed),
        "meta": " · ".join(m for m in meta if m),
        "chips": riesgos,
        "motivos": " · ".join(riesgos),
        "pendiente": color in (RED, YELLOW),
        "wa_label": WA_CHIP.get(wa_state, ("", ""))[0],
        "wa_kind": WA_CHIP.get(wa_state, ("", ""))[1],
        "code_line": o.distri_wa_code_line or "",
        "wa_url": o.distri_revisar_wa_url or "",
        "estado": "Cancelado" if not vivo else "",
        "retenido": retenido,
        "ret_quien": o.distri_retenido_por_id.name or ("Automático" if retenido else ""),
        "ret_cuando": cuando(o.distri_retenido_el),
        "ret_motivo": o.distri_retenido_motivo or "",
        "ret_nota": nota_armado,
        "ret_rutas": o.distri_retenido_rutas or "",
        "ret_sin_cliente": bool(o.distri_retenido_sin_cliente and generic),
        "can_retener": vivo and not retenido,
        "can_pegar": vivo and bool(o.partner_id),
        "can_asignar": vivo and generic and bool(o.partner_id) and puede_asignar(env),
        # 19.0.1.111.0: the kit screen (controllers/asignar.py)
        "asignar_url": "/revisar/%s/asignar" % o.id,
        # the green ones fold to one line, as in the mockup
        "compact": color == GREEN and not retenido and vivo,
        # 19.0.1.129.34: the order's kit screen with ui_v2 on (controllers/fichas.py)
        "odoo_url": record_link(env, "pedido", o.id, "/revisar"),
        "odoo_label": "Ver el pedido" if ui_v2_on(env) else "Abrir en el sistema",
    }


class Revisar(http.Controller):

    # ---------------------------------------------------------------- helpers

    def _check(self):
        user = request.env.user
        if user.share or not puede_revisar(request.env):
            raise Forbidden()
        return user

    def _order(self, order_id):
        order = request.env["sale.order"].browse(int(order_id)).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        return order

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _dn(self):
        return kit_context("revisar.css", "revisar.js")

    def _card_html(self, order):
        return request.env["ir.qweb"]._render("distri_pedido.revisar_card", {
            "c": card(order), "dn": self._dn(), "csrf_token": request.csrf_token()})

    def _done(self, order, message):
        """An action went fine: the card again (fetch) or back to the list."""
        if self._by_fetch():
            order.invalidate_recordset()
            return self._json({"ok": True, "message": message, "id": order.id,
                               "card": str(self._card_html(order))})
        return request.redirect("/revisar?ok=%s" % order.id, local=True)

    def _refuse(self, text, status=400):
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=status)
        return self._page(error=text, status=status)

    def _page(self, ver=DEFAULT_TAB, retener="", error="", ok="", status=200, asignado="",
              sin_wa=""):
        user = request.env.user
        orders = request.env["sale.order"].search(
            [("distri_revisar_en_lista", "=", True)],
            order="distri_revisar_orden, date_order desc, id desc", limit=LIMIT)
        cards = [card(o) for o in orders]
        counts = {
            "pendientes": len([c for c in cards if c["pendiente"]]),
            "confirmados": len([c for c in cards if c["color"] == GREEN]),
            "retenidos": len([c for c in cards if c["retenido"]]),
            "todos": len(cards),
        }
        ver = OLD_TABS.get(ver, ver)
        ver = ver if ver in dict(TABS) else DEFAULT_TAB
        hold = None
        if retener:
            try:
                hold = next((c for c in cards if c["id"] == int(retener) and c["can_retener"]),
                            None)
            except ValueError:
                hold = None
        html = request.env["ir.qweb"]._render("distri_pedido.revisar_page", {
            "dn": self._dn(),
            "user": user,
            "cards": cards,
            "counts": counts,
            "tabs": TABS,
            "ver": ver,
            "full": len(orders) >= LIMIT,
            "hold": hold,
            "motivo_max": MOTIVO_MAX,
            "liberar_texto": LIBERAR_TEXTO,
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            # 19.0.1.129.29: the role's bottom bar (ui_v2 only)
            "nav": page_nav(request.env, "revisar"),
            "csrf_token": request.csrf_token(),
            "error": error,
            "ok": self._ok_text(ok, asignado, sin_wa),
        })
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _ok_text(self, ok, asignado="", sin_wa=""):
        """«Listo.», or what «Asignar cliente» did (19.0.1.111.0)."""
        if not ok:
            return ""
        if asignado:
            from .asignar import asignado_texto
            try:
                order = request.env["sale.order"].browse(int(asignado)).exists()
            except ValueError:
                order = None
            if order and not order.distri_partner_generic:
                return asignado_texto(order, quiso_aprobar=bool(sin_wa))
        return "Listo."

    # ---------------------------------------------------------------- page

    @http.route("/revisar", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, ver=DEFAULT_TAB, retener="", ok="", asignado="", sin_wa="", aviso="", **kw):
        self._check()
        return self._page(ver=ver, retener=retener, ok=ok, asignado=asignado, sin_wa=sin_wa,
                          error=(aviso or "")[:300])

    # ---------------------------------------------------------------- hold

    @http.route("/revisar/<int:order_id>/retener", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def retener(self, order_id, motivo="", **kw):
        self._check()
        order = self._order(order_id)
        try:
            with request.env.cr.savepoint():
                order.distri_retener(motivo)
        except (UserError, AccessError) as exc:
            return self._refuse(_error_text(exc))
        return self._done(order, "%s retenido: no se arma, no se factura y no sale en ruta."
                          % order.name)

    @http.route("/revisar/<int:order_id>/liberar", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def liberar(self, order_id, **kw):
        self._check()
        order = self._order(order_id)
        try:
            with request.env.cr.savepoint():
                order.action_distri_liberar()
        except (UserError, AccessError) as exc:
            return self._refuse(_error_text(exc))
        return self._done(order, "%s liberado: sigue el circuito normal." % order.name)

    # ---------------------------------------------------------------- «Pegar número»

    def _wizard_values(self, order, pegado="", area="", numero=""):
        return {"order_id": order.id, "then": "verify", "pegado": (pegado or "").strip(),
                "area": (area or "").strip(), "number": (numero or "").strip()}

    def _wizard_model(self):
        return request.env["distri.wa.confirm.wizard"].with_context(distri_wa_pegar=True)

    @http.route("/revisar/<int:order_id>/numero", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def numero_leer(self, order_id, pegado="", area="", numero="", **kw):
        """What «Pegar número» reads, computed by the dialog's own model
        (an unsaved record: nothing is stored)."""
        self._check()
        order = self._order(order_id)
        values = self._wizard_values(order, pegado, area, numero)
        wizard = self._wizard_model().new(values)
        national = wizard.pegado_nacional or ""
        split = {}
        if national and values["pegado"]:
            from ..models.ar_phone import split_ar_national
            from ..models.ar_phone import format_local
            area, local = split_ar_national(national)
            split["area"], split["numero"] = area, format_local(local)
        clientes = [{
            "name": p.display_name,
            "detail": " · ".join(v for v in (
                p.city or "",
                ("WhatsApp %s" % _phone(p.distri_whatsapp)) if p.distri_whatsapp else
                ("Teléfono %s" % _phone(p.phone) if p.phone else "")) if v),
        } for p in wizard.pegado_cliente_ids.sudo()]
        error = wizard.pegado_error or ("" if values["pegado"] else wizard.error) or ""
        return self._json({
            "ok": True,
            "preview": wizard.pegado_preview or "",
            "error": error,
            "aviso": wizard.pegado_aviso or "",
            "del_cliente": bool(wizard.pegado_del_cliente),
            "cliente": wizard.customer_name or "",
            "clientes": clientes,
            "generic": bool(wizard.generic),
            **split,
        })

    @http.route("/revisar/<int:order_id>/numero", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def numero_guardar(self, order_id, pegado="", area="", numero="", **kw):
        self._check()
        order = self._order(order_id)
        try:
            with request.env.cr.savepoint():
                # the backend button's checks (group, cancelled order)
                order.action_distri_pegar_numero()
                wizard = self._wizard_model().create(
                    self._wizard_values(order, pegado, area, numero))
                wizard.action_save_confirm()
        except (UserError, AccessError) as exc:
            return self._refuse(_error_text(exc))
        return self._done(order, "Listo: quedó como WhatsApp verificado del cliente.")
