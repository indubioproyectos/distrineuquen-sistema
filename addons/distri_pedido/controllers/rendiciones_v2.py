"""«Rendiciones» and «Casos» on the DistriNeuquén design system
(19.0.1.129.44, behind distri_pedido.ui_v2; off, every URL here sends the
user where the old page sent him and /rendiciones is the 129.43 menu).

Screens (mockups s26-pc-rendicion and s26-07-casos of «Sistema 2026»):

    /rendiciones                    hub: routes to settle and settled in the
                                    last days, per driver, with their amounts;
                                    the follow-up lists (controllers/rendiciones.py
                                    home() calls hub_page() with ui_v2 on)
      /rendiciones/ruta/<id>        one route's settlement: visits, cash
                                    collected and counted, expenses, transfers,
                                    returns, undelivered orders, cases
      /rendiciones/comprobantes     «Comprobantes pendientes»
      /casos                        the route cases (distri.route.case)
        /casos/<id>                 one case, its history and its resolution
      /casos/cargos                 «Cargos al repartidor» (cases of the
                                    receipt/short/unpaid kinds, per driver)

Nothing here computes money or settles anything. Every action calls the very
method the backend button calls, in the user's own environment, so the
access rights and record rules are the backend's:

    Reabrir para el repartidor   distri.route._distri_reopen (as /caja and the
                                 129.25 button), now behind a confirmation
    Abrir caso por la diferencia distri.route.action_open_cash_case
    Recibir en mi caja           a link to the cashier's /caja/rendicion/<id>
                                 (distri.caja.receive_route: the only way a
                                 route is settled since 19.0.1.115.0)
    Guardar / Cerrar caso        write + distri.route.case.action_close
    Reclamar por WhatsApp        distri.promised.transfer.action_remind_whatsapp
    Marcar llegó / cobrado       distri.promised.transfer.action_mark_arrived
    Anular                       distri.promised.transfer.action_cancel

Who sees what mirrors the backend menus: the hub and the route screen whoever
sees «Reparto › Rendición de caja» (rendiciones.can_open: administración,
gerencia, Sistemas), each list whoever sees its own menu (Casos, Cargos al
repartidor, Comprobantes pendientes; a seller opens the last one under
«Cobros» too, his portfolio's only). «Ver en el sistema» (the Odoo form) is
for administración, gerencia and Sistemas (fichas.sees_odoo). Drivers keep
their own app (/reparto), untouched.
"""
from datetime import datetime, time, timedelta
from urllib.parse import urlencode

from werkzeug.exceptions import NotFound

from odoo import fields, http
from odoo.exceptions import AccessError, UserError
from odoo.http import request
from odoo.tools import html2plaintext

from ..models.comprobante_pendiente import OPEN_STATES
from ..models.reparto import ROUTE_TO_CASHIER, STOP_STATES, _money
from ..models.reparto_cliente import today_ar
from .armado import UTC
from .caja import _dia, _hora, is_cashier
from .fichas import sees_odoo
from .main import HORA_ARGENTINA
from .kit import HOME, SCREEN_PARENTS, kit_context, page_nav, route_matches, ui_v2_on
from .rendiciones import CHARGE_KINDS, ROUTE_URL, _backend, can_open, installed
from .tablero import wants_tablero

MENU_CASES = "distri_pedido.menu_reparto_cases"
MENU_CHARGES = "distri_pedido.menu_reparto_charges"
MENU_PROMISES = "distri_pedido.menu_reparto_promises"
MENU_PROMISES_COBROS = "distri_pedido.menu_cobros_promises"
ADMIN_GROUP = "distri_pedido.grupo_administracion"
# «Rendidas»: the routes settled in the last days
RECENT_DAYS = 7
PAGE = 50
CASE_STATES = {"abiertos": [("state", "=", "open")], "resueltos": [("state", "=", "closed")],
               "todos": []}
CASE_TABS = (("abiertos", "Abiertos"), ("resueltos", "Resueltos"), ("todos", "Todos"))
PROMISE_TABS = (("hoy", "Sin resolver"), ("vencidas", "Vencidas"), ("resueltas", "Resueltas"),
                ("todas", "Todas"))
# the backend's texts (views/reparto.xml view_promise_form)
CONFIRM_ARRIVED = "¿Llegó el comprobante a la empresa? Queda registrado a tu nombre."
CONFIRM_PAID = "¿El cliente pagó este pedido? Queda registrado a tu nombre."
CONFIRM_CANCEL = "Anular deja de reclamarlo y no lo carga al repartidor. ¿Seguir?"
CONFIRM_REOPEN = ("La ruta %s vuelve al celular de %s para que corrija las visitas. No se toca "
                  "ningún cobro. ¿Reabrir?")
PARENTS_OF_CASE = SCREEN_PARENTS.get("/casos/<id>", ())
MINUS = "−"


# ------------------------------------------------------------------ helpers

def _sees_menu(env, xmlid):
    menu = env.ref(xmlid, raise_if_not_found=False)
    return bool(menu) and not env.user.share and menu.id in env["ir.ui.menu"]._visible_menu_ids()


def sees_cases(env):
    return _sees_menu(env, MENU_CASES)


def sees_charges(env):
    return _sees_menu(env, MENU_CHARGES)


def sees_promises(env):
    """«Comprobantes pendientes»: the Reparto menu (administración, gerencia,
    Sistemas) or the same list under «Cobros», which sellers open too (only
    their portfolio's, security/reglas.xml)."""
    return _sees_menu(env, MENU_PROMISES) or _sees_menu(env, MENU_PROMISES_COBROS)


def signed(amount):
    """«−$ 2.300» / «+$ 1.000» / «$ 0» for a difference."""
    if not amount:
        return _money(0)
    if amount < 0:
        return MINUS + _money(-amount)
    return "+" + _money(amount)


def _local(moment):
    """A UTC datetime of Odoo in Argentina."""
    return moment.replace(tzinfo=UTC).astimezone(HORA_ARGENTINA) if moment else None


def _when(moment):
    """«25/11 13:40» for a UTC datetime, or ''."""
    return _local(moment).strftime("%d/%m %H:%M") if moment else ""


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _short_date(day):
    return day.strftime("%d/%m") if day else ""


def _label(record, field):
    return dict(record._fields[field]._description_selection(record.env)).get(record[field], "")


def _counts_by(model, domain, field):
    """{record id: count} of `model` records grouped by a many2one."""
    return {rec.id: n for rec, n in model._read_group(domain, [field], ["__count"]) if rec}


def case_url(case_id, desde=""):
    return "/casos/%s" % int(case_id) + ("?" + urlencode({"desde": desde}) if desde else "")


def _home(user):
    return "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero")


# ------------------------------------------------------------------ routes

def route_pill(route):
    """(label, tone) of a route in the lists."""
    if route.state == "settled":
        return "Rendida", "ok"
    if route.state == "closed":
        cur = route.currency_id
        if route.cash_count_done and not cur.is_zero(route.cash_difference):
            if route.cash_difference < 0:
                return "Falta %s" % _money(-route.cash_difference), "err"
            return "Sobra %s" % _money(route.cash_difference), "warn"
        return "Para rendir", "warn"
    return _label(route, "state"), "neutral"


def route_row(route, receipts, cases):
    label, tone = route_pill(route)
    counted = route.cash_count_done
    return {
        "id": route.id,
        "name": route.name,
        "driver": route.driver_id.name or "",
        "driver_id": route.driver_id.id,
        "date": _dia(route.date, short=True),
        "pill": label,
        "tone": tone,
        "expected": _money(route.cash_expected),
        "expenses": _money(route.expense_total),
        "counted": _money(route.cash_counted) if counted else "",
        "difference": signed(route.cash_difference) if counted else "",
        "difference_tone": ("err" if route.cash_difference < 0 else "warn")
        if counted and not route.currency_id.is_zero(route.cash_difference) else "",
        "receipts": receipts.get(route.id, 0),
        "cases": cases.get(route.id, 0),
        "stops": route.stop_count,
        "closed_at": _hora(route.closed_at),
        "settled_at": _hora(route.settled_at),
        "settled_on": _when(route.settled_at),
        "url": "/rendiciones/ruta/%s" % route.id,
    }


def hub_data(env, vista="para_rendir", repartidor=0, ruta=0):
    """The hub's lists, totals and the selected route of the PC side panel.
    Read with sudo after can_open (the same domains as the backend actions
    and /caja: pending_receptions)."""
    Route = env["distri.route"].sudo()
    to_settle, _boxes = env["distri.caja"].sudo().pending_receptions()
    since = fields.Datetime.now() - timedelta(days=RECENT_DAYS)
    settled = Route.search([("kind", "=", "route"), ("state", "=", "settled"),
                            ("company_id", "=", env.company.id), ("settled_at", ">=", since)],
                           order="settled_at desc, id desc", limit=200)
    every = to_settle | settled
    Promise = env["distri.promised.transfer"].sudo()
    Case = env["distri.route.case"].sudo()
    receipts = _counts_by(Promise, [("route_id", "in", every.ids), ("state", "in", OPEN_STATES)],
                          "route_id")
    cases = _counts_by(Case, [("route_id", "in", every.ids), ("state", "=", "open")], "route_id")
    drivers = sorted({(r.driver_id.id, r.driver_id.name or "") for r in every if r.driver_id},
                     key=lambda d: d[1].lower())
    vista = vista if vista in ("para_rendir", "rendidas") else "para_rendir"
    shown = to_settle if vista == "para_rendir" else settled
    driver_ids = {d[0] for d in drivers}
    repartidor = repartidor if repartidor in driver_ids else 0
    if repartidor:
        shown = shown.filtered(lambda r: r.driver_id.id == repartidor)
    rows = [route_row(r, receipts, cases) for r in shown]
    selected = next((r for r in rows if r["id"] == ruta), rows[0] if rows else None)
    pending = to_settle.filtered(lambda r: not repartidor or r.driver_id.id == repartidor)
    return {
        "vista": vista,
        "repartidor": repartidor,
        "drivers": drivers,
        "rows": rows,
        "selected": selected,
        "n_to_settle": len(to_settle),
        "n_settled": len(settled),
        "cash_to_settle": _money(sum(pending.mapped("cash_expected"))),
        "expenses_to_settle": _money(sum(pending.mapped("expense_total"))),
        "open_cases": Case.search_count([("state", "=", "open")]),
        "open_charges": Case.search_count([("state", "=", "open"),
                                           ("kind", "in", CHARGE_KINDS)]),
        "open_receipts": Promise.search_count([("state", "in", OPEN_STATES)]),
    }


def _visit_status(stop, open_promise_stops):
    states = dict(STOP_STATES)
    if stop.state == "not_delivered" or stop.delivery_state == "not_delivered":
        return "No se entregó", "err"
    if stop.state == "not_paid":
        return states["not_paid"], "err"
    if stop.state in ("pending", "dispatched"):
        return states[stop.state], "neutral"
    if stop in open_promise_stops:
        return "Sin comprobante", "warn"
    if stop.return_ids:
        return "Devolvió", "warn"
    return states.get(stop.state, ""), "ok"


def route_view(env, route):
    """Everything the route screen shows, from the route's own fields (the
    backend form's numbers: cash_expected, cash_counted, cash_difference,
    expense_total...). `route` is read with sudo after can_open."""
    user = env.user
    cur = route.currency_id
    stops = route.stop_ids.sorted(lambda s: (s.sequence, s.id))
    promises = route.promise_ids
    open_promises = promises.filtered(lambda p: p.state in OPEN_STATES)
    visits = []
    for n, stop in enumerate(stops, 1):
        label, tone = _visit_status(stop, open_promises.stop_id)
        visits.append({
            "n": n, "partner": stop.partner_id._distri_app_name() if stop.partner_id else "",
            "order": stop.order_id.name or "", "balance": _money(stop.expected_balance),
            "cash": _money(stop.cash_amount) if stop.cash_amount else "",
            "transfer": _money(stop.transfer_amount) if stop.transfer_amount else "",
            "pill": label, "tone": tone, "comment": stop.comment or stop.not_delivered_reason or "",
        })
    expenses = route.expense_ids.filtered(lambda e: e.state != "cancelled")
    transfers = []
    for promise in promises.sorted(lambda p: (p.captured_at or datetime.min, p.id)):
        tone = {"fulfilled": "ok", "open": "warn", "overdue": "err"}.get(promise.state, "neutral")
        transfers.append({"partner": promise.partner_id._distri_app_name()
                          if promise.partner_id else "",
                          "kind": _label(promise, "kind"), "amount": _money(promise.amount),
                          "pill": promise.status_label or _label(promise, "state"), "tone": tone})
    returns = [{"partner": r.partner_id._distri_app_name() if r.partner_id else "",
                "product": r.product_id.display_name or "", "qty": "%g" % r.quantity,
                "reason": r.reason or "", "amount": _money(r.amount),
                "pill": _label(r, "settlement_state"),
                "tone": {"received": "ok", "missing": "err"}.get(r.settlement_state, "warn")}
               for r in route.return_ids]
    undelivered = [{"partner": s.partner_id._distri_app_name() if s.partner_id else "",
                    "order": s.order_id.name or "", "reason": s.not_delivered_reason or "",
                    "pill": "Pasó a otra ruta" if s.reassigned_stop_id else (
                        "Volvió al depósito" if s.unloaded else "Sin descargar"),
                    "tone": "ok" if s.unloaded else "warn"}
                   for s in route.undelivered_stop_ids]
    cases = [{"id": c.id, "kind": _label(c, "kind"), "amount": signed(c.amount),
              "pill": _label(c, "state"), "tone": "warn" if c.state == "open" else "ok",
              "url": case_url(c.id, "/rendiciones/ruta/%s" % route.id)}
             for c in route.case_ids.sorted("id")]
    counted = route.cash_count_done
    refusal = route._distri_reopen_refusal(user) if route.state == "closed" else ""
    open_cash_case = route._cash_case()
    Case = env["distri.route.case"]
    return {
        "id": route.id,
        "name": route.name,
        "state": route.state,
        "state_label": _label(route, "state"),
        "pill": route_pill(route),
        "date": _dia(route.date),
        "driver": route.driver_id.name or "",
        "stop_count": route.stop_count,
        "closed_at": _hora(route.closed_at),
        "settled": _when(route.settled_at),
        "settled_by": route.settled_by_id.name or "",
        "reopened": route.reopen_count,
        "expected": _money(route.cash_expected),
        "collected": _money(route.cash_collected),
        "expense_total": _money(route.expense_total),
        "advance": _money(route.advance_total) if route.advance_total else "",
        "counted": _money(route.cash_counted) if counted else "",
        "difference": signed(route.cash_difference) if counted else "",
        "difference_tone": "" if not counted or cur.is_zero(route.cash_difference) else (
            "err" if route.cash_difference < 0 else "warn"),
        "difference_text": "" if not counted else (
            "Justo" if cur.is_zero(route.cash_difference) else
            ("Falta efectivo" if route.cash_difference < 0 else "Sobra efectivo")),
        "candidates": [c for c in (route.cash_candidates or "").split("\n") if c] if counted else [],
        "driver_explanation": route.driver_explanation or "",
        "transfer_total": _money(sum(stops.mapped("transfer_amount"))),
        "receipts_pending": len(open_promises.filtered(lambda p: p.kind == "done")),
        "receipts_ok": len(promises.filtered(lambda p: p.state == "fulfilled")),
        "visits": visits,
        "visits_balance": _money(sum(stops.mapped("expected_balance"))),
        "visits_cash": _money(sum(stops.mapped("cash_amount"))),
        "visits_transfer": _money(sum(stops.mapped("transfer_amount"))),
        "collections": [{"partner": c.partner_id._distri_app_name() if c.partner_id else "",
                         "who": c.user_id.name or "", "amount": _money(c.amount)}
                        for c in route.cash_collection_ids],
        "expenses": [{"kind": _label(e, "kind"), "amount": _money(e.amount),
                      "comment": e.comment or "", "pill": _label(e, "state"),
                      "tone": {"approved": "ok", "rejected": "err"}.get(e.state, "neutral"),
                      "paper": e.paper_received}
                     for e in expenses],
        "transfers": transfers,
        "returns": returns,
        "undelivered": undelivered,
        "cases": cases,
        "rejections": route.rejection_open_count,
        "double_cash": route.double_cash_note or "",
        # actions (each one checks again when it runs)
        "can_receive": is_cashier(user) and route.kind == "route" and route.state == "closed",
        "receive_url": "/caja/rendicion/%s" % route.id,
        "to_cashier": ROUTE_TO_CASHIER if route.state == "closed" and not is_cashier(user)
        else "",
        "can_reopen": route.state == "closed" and not refusal,
        "reopen_note": refusal if route.state == "closed" else "",
        "reopen_question": CONFIRM_REOPEN % (route.name, route.driver_id.name or "el repartidor"),
        "can_case": bool(counted and not cur.is_zero(route.cash_difference)
                         and not open_cash_case and Case.has_access("create")),
        "cash_case_url": case_url(open_cash_case.id, "/rendiciones/ruta/%s" % route.id)
        if open_cash_case and sees_cases(env) else "",
        "odoo_url": ROUTE_URL % route.id if sees_odoo(user) else "",
    }


# ------------------------------------------------------------------ cases

def _case_title(case):
    partner = (case.stop_id.partner_id or case.return_id.partner_id
               or case.promise_id.partner_id or case.debt_partner_id)
    if partner:
        return partner._distri_app_name()
    return case.route_id.name or case.caja_id.name or case.name or ""


def case_row(case, desde=""):
    bits = [_label(case, "kind"), case.route_id.name or case.caja_id.name or "",
            case.driver_id.name or "", _short_date(case.date)]
    return {
        "id": case.id,
        "title": _case_title(case),
        "sub": " · ".join(b for b in bits if b),
        "amount": signed(case.amount) if case.amount else _money(0),
        "negative": case.amount < 0,
        "pill": _label(case, "state"),
        "tone": "warn" if case.state == "open" else "ok",
        "replenished": case.replenished,
        "url": case_url(case.id, desde),
        "driver": case.driver_id.name or "Sin repartidor",
        "driver_id": case.driver_id.id,
    }


def case_list(env, base, estado="abiertos", tipo="", repartidor=0, offset=0, desde="/casos",
              kinds=None):
    """The rows, chips and totals of /casos and /casos/cargos, read in the
    user's environment (the backend's access rules)."""
    Case = env["distri.route.case"]
    labels = dict(Case._fields["kind"]._description_selection(env))
    kinds = kinds or tuple(labels)
    estado = estado if estado in CASE_STATES else "abiertos"
    tipo = tipo if tipo in kinds else ""
    state_dom = list(base) + CASE_STATES[estado]
    drivers = sorted(((d.id, d.name or "") for d, _n in
                      Case._read_group(state_dom, ["driver_id"], ["__count"]) if d),
                     key=lambda d: d[1].lower())
    repartidor = repartidor if repartidor in {d[0] for d in drivers} else 0
    driver_dom = [("driver_id", "=", repartidor)] if repartidor else []
    by_kind = dict(Case._read_group(state_dom + driver_dom, ["kind"], ["__count"]))
    chips = [(k, labels[k], by_kind.get(k, 0)) for k in kinds if by_kind.get(k)]
    domain = state_dom + driver_dom + ([("kind", "=", tipo)] if tipo else [])
    total, amount = Case._read_group(domain, [], ["__count", "amount:sum"])[0]
    offset = max(0, offset)
    cases = Case.search(domain, offset=offset, limit=PAGE)
    return {
        "estado": estado, "tipo": tipo, "repartidor": repartidor,
        "tabs": CASE_TABS, "drivers": drivers, "chips": chips,
        "n_all_kinds": sum(by_kind.values()),
        "total": total, "amount": signed(amount or 0) if amount else _money(0),
        "rows": [case_row(c, desde) for c in cases],
        "offset": offset, "more": offset + PAGE < total, "next_offset": offset + PAGE,
    }


def case_history(case):
    """The chatter of a case as lines: when, who, what (the backend's
    «Historial»: messages and tracked changes)."""
    out = []
    for message in case.sudo().message_ids.sorted(lambda m: (m.date, m.id)):
        text = html2plaintext(message.body or "").strip()
        changes = []
        for t in message.tracking_value_ids._tracking_value_format():
            info = t["fieldInfo"]

            def show(value, kind=info["fieldType"]):
                if kind == "boolean":
                    return "Sí" if value else "No"
                if kind == "monetary":
                    return _money(value)
                return value or "—"
            changes.append("%s: %s → %s" % (info["changedField"], show(t["oldValue"]),
                                            show(t["newValue"])))
        if not text and not changes:
            continue
        out.append({"when": _when(message.date),
                    "who": message.author_id.name or "", "text": text, "changes": changes})
    return list(reversed(out))


# ------------------------------------------------------------------ promises

def promise_domain(estado, tipo="", repartidor=0):
    if estado == "vencidas":
        domain = [("state", "=", "overdue")]
    elif estado == "resueltas":
        domain = [("state", "=", "fulfilled")]
    elif estado == "todas":
        domain = []
    else:
        # the backend's default filter «Sin resolver o resueltas hoy»
        since = datetime.combine(today_ar() - timedelta(days=1), time(3, 0))
        domain = ["|", ("state", "in", OPEN_STATES), "&", ("state", "=", "fulfilled"),
                  ("resolved_at", ">=", fields.Datetime.to_string(since))]
    if tipo in ("done", "unpaid"):
        domain = domain + [("kind", "=", tipo)]
    if repartidor:
        domain = domain + [("driver_id", "=", repartidor)]
    return domain


def promise_row(promise, is_admin):
    open_ = promise.state in OPEN_STATES
    return {
        "id": promise.id,
        "partner": promise.partner_id._distri_app_name() if promise.partner_id else "",
        "sub": " · ".join(b for b in (
            _label(promise, "kind"), promise.driver_id.name or "",
            _when(promise.captured_at), promise.order_id.name or "") if b),
        "amount": _money(promise.amount),
        "pill": promise.status_label or _label(promise, "state"),
        "tone": {"open": "warn", "overdue": "err", "fulfilled": "ok"}.get(promise.state,
                                                                          "neutral"),
        "route_url": "/rendiciones/ruta/%s" % promise.route_id.id
        if promise.route_id and promise.route_id.kind == "route" else "",
        "route": promise.route_id.name or "",
        "can_remind": open_,
        "can_arrive": open_,
        "arrive_label": "Marcar cobrado" if promise.kind == "unpaid"
        else "Marcar llegó el comprobante",
        "arrive_question": CONFIRM_PAID if promise.kind == "unpaid" else CONFIRM_ARRIVED,
        "can_cancel": open_ and is_admin,
        "reminded": "Reclamado %s" % _when(promise.reminded_at) if promise.reminded_at else "",
    }


# ------------------------------------------------------------------ controller

class RendicionesV2(http.Controller):

    def _render(self, template, values, status=200):
        user = request.env.user
        base = {
            "dn": kit_context("cuentas_v2.css", "rendiciones_v2.css", "rendiciones_v2.js"),
            "user": user,
            "money": _money,
            "inicio": _home(user),
            "nav": page_nav(request.env, "reparto"),
            "ok": "",
            "error": "",
            "csrf_token": request.csrf_token(),
            "sees_cases": sees_cases(request.env),
            "sees_charges": sees_charges(request.env),
            "sees_promises": sees_promises(request.env),
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    # ------------------------------------------------------------ guards

    def _off(self, menu):
        """ui_v2 off: where the 129.43 page sent the user (the backend list)."""
        return request.redirect(_backend(request.env, menu) or "/tablero", 303)

    def _refused_rendiciones(self):
        env = request.env
        if not can_open(env):
            return request.redirect("/tablero", 303)
        if not installed(env) or not ui_v2_on(env):
            return request.redirect("/rendiciones", 303)
        return None

    def _route(self, route_id):
        route = request.env["distri.route"].sudo().browse(route_id).exists()
        if not route or route.kind != "route" or route.company_id not in request.env.companies:
            raise NotFound()
        return route

    # ------------------------------------------------------------ hub

    def hub_page(self, vista="", repartidor="", ruta="", ok="", error=""):
        env = request.env
        data = hub_data(env, vista or "para_rendir", _int(repartidor), _int(ruta))
        return self._render("distri_pedido.rendiciones_v2_hub", {
            "h": data,
            "ok": ok,
            "error": error,
            "is_cashier": is_cashier(env.user),
            "ajustes": _backend(env, "distri_pedido.menu_reparto_zones"),
            "odoo_url": _backend(env, "distri_pedido.menu_reparto_settle")
            if sees_odoo(env.user) else "",
            "hub_url": lambda **kw: "/rendiciones?" + urlencode({k: v for k, v in dict(
                {"vista": data["vista"], "repartidor": data["repartidor"] or ""}, **kw).items()
                if v}),
        })

    # ------------------------------------------------------------ route

    def _route_page(self, route, ok="", error="", ask_reopen=False, status=200):
        return self._render("distri_pedido.rendiciones_v2_ruta", {
            "r": route_view(request.env, route),
            "ok": ok, "error": error, "ask_reopen": ask_reopen,
        }, status=status)

    @http.route("/rendiciones/ruta/<int:route_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def route_page(self, route_id, ok="", **kw):
        refused = self._refused_rendiciones()
        if refused:
            return refused
        return self._route_page(self._route(route_id), ok=ok)

    @http.route("/rendiciones/ruta/<int:route_id>/reabrir", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def route_reopen(self, route_id, confirmar="", **kw):
        """«Reabrir para el repartidor», asked first: without confirmar=1 (the
        dialog's «Sí, reabrir», or the page's own question without
        JavaScript) nothing changes."""
        refused = self._refused_rendiciones()
        if refused:
            return refused
        route = self._route(route_id)
        if confirmar != "1":
            return self._route_page(route, ask_reopen=True)
        try:
            route._distri_reopen(request.env.user, via="rendiciones")
        except UserError as exc:
            return self._route_page(route, error=exc.args[0] if exc.args else str(exc),
                                    status=400)
        return request.redirect("/rendiciones/ruta/%s?%s" % (route.id, urlencode(
            {"ok": "%s: reabierta para el repartidor." % route.name})), 303)

    @http.route("/rendiciones/ruta/<int:route_id>/caso", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def route_cash_case(self, route_id, **kw):
        """«Abrir caso por la diferencia»: the backend button's method, in the
        user's environment (creating a case needs the backend right)."""
        refused = self._refused_rendiciones()
        if refused:
            return refused
        route = self._route(route_id)
        try:
            with request.env.cr.savepoint():
                action = request.env["distri.route"].browse(route.id).action_open_cash_case()
        except (UserError, AccessError) as exc:
            return self._route_page(route, error=exc.args[0] if exc.args else str(exc),
                                    status=400)
        if sees_cases(request.env):
            return request.redirect(case_url(action["res_id"], "/rendiciones/ruta/%s" % route.id),
                                    303)
        return request.redirect("/rendiciones/ruta/%s?%s" % (route.id, urlencode(
            {"ok": "Caso abierto por la diferencia."})), 303)

    # ------------------------------------------------------------ promises

    def _promises_page(self, estado="hoy", tipo="", repartidor=0, ok="", error="", ask=None,
                       status=200):
        env = request.env
        Promise = env["distri.promised.transfer"]
        estado = estado if estado in dict(PROMISE_TABS) else "hoy"
        tipo = tipo if tipo in ("done", "unpaid") else ""
        base = promise_domain(estado)
        drivers = sorted(((d.id, d.name or "") for d, _n in
                          Promise._read_group(base, ["driver_id"], ["__count"]) if d),
                         key=lambda d: d[1].lower())
        repartidor = repartidor if repartidor in {d[0] for d in drivers} else 0
        domain = promise_domain(estado, tipo, repartidor)
        total, amount = Promise._read_group(domain, [], ["__count", "amount:sum"])[0]
        is_admin = env.user.has_group(ADMIN_GROUP)
        rows = [promise_row(p, is_admin) for p in Promise.search(domain, limit=200)]
        query = urlencode({k: v for k, v in (("estado", estado), ("tipo", tipo),
                                             ("repartidor", repartidor or "")) if v})
        return self._render("distri_pedido.rendiciones_v2_comprobantes", {
            "estado": estado, "tipo": tipo, "repartidor": repartidor, "tabs": PROMISE_TABS,
            "drivers": drivers, "rows": rows, "total": total, "amount": _money(amount or 0),
            "more": total > len(rows),
            "volver": "/rendiciones/comprobantes" + ("?" + query if query else ""),
            "q": lambda **kw: "/rendiciones/comprobantes?" + urlencode({k: v for k, v in dict(
                {"estado": estado, "tipo": tipo, "repartidor": repartidor or ""}, **kw).items()
                if v}),
            "ok": ok, "error": error, "ask": ask,
            "odoo_url": _backend(env, MENU_PROMISES) if sees_odoo(env.user) else "",
        }, status=status)

    @http.route("/rendiciones/comprobantes", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def promises(self, estado="hoy", tipo="", repartidor="", ok="", **kw):
        env = request.env
        if not sees_promises(env):
            return request.redirect("/tablero", 303)
        if not ui_v2_on(env):
            return self._off(MENU_PROMISES)
        return self._promises_page(estado, tipo, _int(repartidor), ok=ok)

    @http.route("/rendiciones/comprobantes/<int:promise_id>/<string:accion>", type="http",
                auth="user", methods=["POST"], sitemap=False)
    def promise_action(self, promise_id, accion, confirmar="", volver="", **kw):
        env = request.env
        if not sees_promises(env):
            return request.redirect("/tablero", 303)
        if not ui_v2_on(env):
            return self._off(MENU_PROMISES)
        if accion not in ("reclamar", "llego", "anular"):
            raise NotFound()
        if not volver.startswith("/rendiciones/comprobantes"):
            volver = "/rendiciones/comprobantes"
        params = dict(p.split("=", 1) for p in volver.partition("?")[2].split("&") if "=" in p)
        again = {"estado": params.get("estado", "hoy"), "tipo": params.get("tipo", ""),
                 "repartidor": _int(params.get("repartidor"))}
        promise = env["distri.promised.transfer"].browse(promise_id).exists()
        if not promise or not promise._filtered_access("read"):
            raise NotFound()
        row = promise_row(promise, env.user.has_group(ADMIN_GROUP))
        if accion in ("llego", "anular") and confirmar != "1":
            question = row["arrive_question"] if accion == "llego" else CONFIRM_CANCEL
            label = row["arrive_label"] if accion == "llego" else "Anular"
            return self._promises_page(**again, ask={"id": promise.id, "accion": accion,
                                                     "question": question, "label": label,
                                                     "partner": row["partner"]})
        try:
            with env.cr.savepoint():
                if accion == "reclamar":
                    result = promise.action_remind_whatsapp()
                elif accion == "llego":
                    promise.action_mark_arrived()
                else:
                    promise.action_cancel()
        except (UserError, AccessError) as exc:
            return self._promises_page(**again, error=exc.args[0] if exc.args else str(exc),
                                       status=400)
        if accion == "reclamar":
            return request.redirect(result["url"], 303, local=False)
        message = {"llego": "%s: quedó resuelto a tu nombre.",
                   "anular": "%s: quedó anulado."}[accion] % row["partner"]
        return request.redirect(volver + ("&" if "?" in volver else "?")
                                + urlencode({"ok": message}), 303)

    # ------------------------------------------------------------ cases

    def _refused_cases(self, menu):
        env = request.env
        if not _sees_menu(env, menu):
            return request.redirect("/tablero", 303)
        if not ui_v2_on(env):
            return self._off(menu)
        return None

    @http.route("/casos", type="http", auth="user", methods=["GET"], sitemap=False)
    def cases(self, estado="abiertos", tipo="", repartidor="", offset="", **kw):
        refused = self._refused_cases(MENU_CASES)
        if refused:
            return refused
        env = request.env
        data = case_list(env, [], estado, tipo, _int(repartidor), _int(offset), "/casos")
        return self._render("distri_pedido.rendiciones_v2_casos", {
            "c": data, "charges": False, "page_url": "/casos",
            "list_url": lambda **kw: "/casos?" + urlencode({k: v for k, v in dict(
                {"estado": data["estado"], "tipo": data["tipo"],
                 "repartidor": data["repartidor"] or ""}, **kw).items() if v}),
            "odoo_url": _backend(env, MENU_CASES) if sees_odoo(env.user) else "",
        })

    @http.route("/casos/cargos", type="http", auth="user", methods=["GET"], sitemap=False)
    def charges(self, estado="abiertos", tipo="", repartidor="", offset="", **kw):
        refused = self._refused_cases(MENU_CHARGES)
        if refused:
            return refused
        env = request.env
        data = case_list(env, [("kind", "in", CHARGE_KINDS)], estado, tipo, _int(repartidor),
                         _int(offset), "/casos/cargos", kinds=CHARGE_KINDS)
        groups = []
        for row in data["rows"]:
            if not groups or groups[-1]["driver_id"] != row["driver_id"]:
                groups.append({"driver_id": row["driver_id"], "driver": row["driver"],
                               "rows": []})
            groups[-1]["rows"].append(row)
        data["groups"] = groups
        return self._render("distri_pedido.rendiciones_v2_casos", {
            "c": data, "charges": True, "page_url": "/casos/cargos",
            "list_url": lambda **kw: "/casos/cargos?" + urlencode({k: v for k, v in dict(
                {"estado": data["estado"], "tipo": data["tipo"],
                 "repartidor": data["repartidor"] or ""}, **kw).items() if v}),
            "odoo_url": _backend(env, MENU_CHARGES) if sees_odoo(env.user) else "",
        })

    def _case(self, case_id):
        case = request.env["distri.route.case"].browse(case_id).exists()
        if not case or not case._filtered_access("read"):
            raise NotFound()
        return case

    @staticmethod
    def _case_parent(desde):
        path = (desde or "").split("?", 1)[0].split("#", 1)[0]
        if path.startswith("/") and not path.startswith("//"):
            for pattern in PARENTS_OF_CASE:
                if pattern != HOME and route_matches(pattern, path):
                    return path
        return "/casos"

    def _case_page(self, case, desde="", ok="", error="", form=None, status=200):
        env = request.env
        parent = self._case_parent(desde)
        row = case_row(case)
        values = {
            "case": case,
            "row": row,
            "kind": _label(case, "kind"),
            "state": _label(case, "state"),
            "parent": parent,
            "desde": desde if parent != "/casos" else "",
            "route_url": "/rendiciones/ruta/%s" % case.route_id.id
            if case.route_id and can_open(env) else "",
            "history": case_history(case),
            "amount_history": [{"date": _short_date(h.date) if h.date else "",
                                "old": _money(h.old_amount), "new": _money(h.new_amount),
                                "cause": h.cause or "", "move": h.move_id.name or ""}
                               for h in case.sudo().amount_history_ids]
            if case.kind == "unpaid" else [],
            "can_edit": case.state == "open" and case.has_access("write"),
            "form": form or {"investigacion": case.investigation or "",
                             "resolucion": case.resolution or "",
                             "repuesto": case.replenished},
            "ok": ok, "error": error,
            "odoo_url": "/odoo/distri.route.case/%s" % case.id if sees_odoo(env.user) else "",
        }
        return self._render("distri_pedido.rendiciones_v2_caso", values, status=status)

    @http.route("/casos/<int:case_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def case_page(self, case_id, desde="", ok="", **kw):
        refused = self._refused_cases(MENU_CASES)
        if refused:
            return refused
        return self._case_page(self._case(case_id), desde=desde, ok=ok)

    @http.route("/casos/<int:case_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def case_save(self, case_id, accion="guardar", investigacion="", resolucion="",
                  repuesto="", desde="", **kw):
        """«Guardar» (the backend form's save) or «Cerrar caso» (the save plus
        action_close, which asks for the resolution), as the user."""
        refused = self._refused_cases(MENU_CASES)
        if refused:
            return refused
        case = self._case(case_id)
        form = {"investigacion": investigacion, "resolucion": resolucion,
                "repuesto": repuesto == "1"}
        values = {"investigation": investigacion.strip() or False,
                  "resolution": resolucion.strip() or False,
                  "replenished": repuesto == "1"}
        try:
            with request.env.cr.savepoint():
                if case.state != "open":
                    raise UserError("El caso ya está resuelto.")
                changed = {k: v for k, v in values.items() if (case[k] or False) != v}
                if changed:
                    case.write(changed)
                if accion == "cerrar":
                    case.action_close()
        except (UserError, AccessError) as exc:
            return self._case_page(case, desde=desde, form=form, status=400,
                                   error=exc.args[0] if exc.args else str(exc))
        message = "Caso resuelto." if accion == "cerrar" else "Guardado."
        query = {"ok": message}
        if desde:
            query["desde"] = desde
        return request.redirect("/casos/%s?%s" % (case.id, urlencode(query)), 303)
