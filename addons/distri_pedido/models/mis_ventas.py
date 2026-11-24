"""«Mis ventas» (19.0.1.129.18, pendiente 78): each seller looks up the orders
he loaded and how they go. Read only: nothing here writes.

The page (controllers/mis_ventas.py, views/mis_ventas_app.xml) shows the
orders whose salesperson (sale.order.user_id) is the current user; Gerencia
sees everyone's and can pick a seller. The access is decided here, on the
server, from the user's groups: the request never chooses whose orders it
gets unless the user is Gerencia.

Each order shows what already exists in the module, nothing new is stored:
- status: the order state (sin confirmar / confirmado / cancelado) and
  «Retenido» (models/revisar.py);
- armado: sale.order.distri_armado (models/armado.py) and the shortages the
  packer wrote down (sale.order.line.distri_faltante);
- delivery: sale.order.distri_delivery_state (models/reparto_cliente.py,
  the last visit of a route or of the depot) and «Retira en el depósito»;
- payment: the payment state of its posted customer invoices (the cobros
  reconcile the payments with them, oldest first).
"""
from datetime import datetime, time, timedelta

from odoo.fields import Domain

from .reparto import _money
from .semaforo import AR_TZ, UTC

VENDEDOR_GROUP = "distri_pedido.grupo_vendedor"
GERENCIA_GROUP = "distri_pedido.grupo_gerencia"

LIMIT = 300
PERIODOS = [("hoy", "Hoy"), ("semana", "Esta semana"), ("mes", "Este mes")]
DEFAULT_PERIODO = "semana"
ESTADOS = [("todos", "Todos"), ("sin_confirmar", "Sin confirmar"),
           ("confirmados", "Confirmados"), ("cancelados", "Cancelados")]
DEFAULT_ESTADO = "todos"
_ESTADO_DOMAIN = {
    "todos": [],
    "sin_confirmar": [("state", "in", ["draft", "sent"])],
    "confirmados": [("state", "=", "sale")],
    "cancelados": [("state", "=", "cancel")],
}
# (label, chip kind) of each part of a card
STATE_LABEL = {"draft": ("Sin confirmar", "warn"), "sent": ("Sin confirmar", "warn"),
               "sale": ("Confirmado", "ok"), "cancel": ("Cancelado", "neutral")}
ARMADO_LABEL = {"pendiente": ("Sin armar", "neutral"), "en_armado": ("En armado", "warn"),
                "armado": ("Armado", "ok")}
ENTREGA_LABEL = {
    "unassigned": ("Sin salir todavía", "neutral"),
    "on_route": ("En reparto", "warn"),
    "delivered": ("Entregado", "ok"),
    "pending": ("No se entregó: vuelve a salir", "err"),
    "dispatched": ("Despachado por encomienda", "ok"),
}
PAGO_PAID = ("paid", "in_payment", "reversed")


def es_gerencia(user):
    return not user.share and user.has_group(GERENCIA_GROUP)


def puede_ver_mis_ventas(user):
    """Internal sellers and Gerencia (Administración implies the seller
    group: it sees its own orders, like any seller)."""
    return not user.share and not user._is_public() and (
        user.has_group(VENDEDOR_GROUP) or user.has_group(GERENCIA_GROUP))


def rango_periodo(periodo, now=None):
    """(start, end) naive UTC of «hoy», «esta semana» (from Monday) or «este
    mes» (from the 1st), Argentine time; end is now."""
    now = (now or datetime.now(UTC)).astimezone(AR_TZ)
    day = now.date()
    if periodo == "semana":
        day -= timedelta(days=day.weekday())
    elif periodo == "mes":
        day = day.replace(day=1)
    start = datetime.combine(day, time.min, tzinfo=AR_TZ).astimezone(UTC).replace(tzinfo=None)
    return start, now.astimezone(UTC).replace(tzinfo=None)


def vendedores(env):
    """The sellers Gerencia can pick: internal users of the seller group."""
    group = env.ref(VENDEDOR_GROUP)
    return env["res.users"].sudo().search(
        [("share", "=", False), ("all_group_ids", "in", group.ids)], order="name")


def _cantidad(qty):
    return ("%g" % qty).replace(".", ",")


def _cuando(moment):
    from ..controllers.revisar import cuando
    return cuando(moment)


def _cliente(order):
    if not order.partner_id or order.partner_id.distri_generic_partner:
        return order.distri_nombre or "Sin cliente asignado"
    return order.partner_id.name or ""


def _pago(order):
    """(label, kind, invoiced, residual) from the order's posted invoices."""
    invoices = order.invoice_ids.filtered(
        lambda m: m.state == "posted" and m.move_type == "out_invoice")
    if not invoices:
        return ("Sin facturar", "neutral", 0.0, 0.0)
    invoiced = sum(invoices.mapped("amount_total_signed"))
    residual = sum(invoices.mapped("amount_residual_signed"))
    states = set(invoices.mapped("payment_state"))
    if states <= set(PAGO_PAID) or residual <= 0.005:
        return ("Pagado", "ok", invoiced, 0.0)
    if residual < invoiced - 0.005:
        return ("Pago parcial · falta %s" % _money(residual), "warn", invoiced, residual)
    return ("A cobrar", "err", invoiced, residual)


def fila(order):
    """What the card of one order shows (views/mis_ventas_app.xml)."""
    o = order.sudo()
    vivo = o.state != "cancel"
    estado, estado_kind = STATE_LABEL.get(o.state, (o.state, "neutral"))
    row = {
        "id": o.id,
        "name": o.name,
        "cliente": _cliente(o),
        "vendedor": _vendedor_nombre(o),
        "cuando": _cuando(o.date_order),
        "amount": _money(o.amount_total),
        "amount_raw": o.amount_total if vivo else 0.0,
        "estado": estado,
        "estado_kind": estado_kind,
        "cancelado": not vivo,
        "retenido": vivo and bool(o.distri_retenido),
        "armado": "", "armado_kind": "", "faltantes": [],
        "entrega": "", "entrega_kind": "",
        "pago": "", "pago_kind": "", "facturado": 0.0, "a_cobrar": 0.0,
    }
    if not vivo:
        return row
    if o.state == "sale":
        row["armado"], row["armado_kind"] = ARMADO_LABEL.get(o.distri_armado, ("", ""))
        row["faltantes"] = ["%s × %s" % (_cantidad(line.distri_faltante), line.product_id.name)
                            for line in o.order_line if line.distri_faltante > 0]
        if o.distri_armado == "armado":
            state = o.distri_delivery_state
            if state == "unassigned" and o.distri_entrega == "retira":
                row["entrega"], row["entrega_kind"] = "Para retirar en el depósito", "neutral"
            else:
                row["entrega"], row["entrega_kind"] = ENTREGA_LABEL.get(state, ("", ""))
        row["pago"], row["pago_kind"], row["facturado"], row["a_cobrar"] = _pago(o)
    return row


def de_vendedor(user_id):
    """The orders of one seller: the one the customer picked in «Mi vendedor
    es» (distri.vendedor.web linked to a user) wins over the salesperson
    (user_id), which often stays the user who loaded or confirmed the order.
    Each order belongs to one seller only, so the totals never count it
    twice."""
    return (Domain("distri_vendedor_web_id.user_id", "=", user_id)
            | (Domain("user_id", "=", user_id)
               & (Domain("distri_vendedor_web_id", "=", False)
                  | Domain("distri_vendedor_web_id.user_id", "=", False))))


def _vendedor_nombre(order):
    web = order.distri_vendedor_web_id
    if web.user_id:
        return web.user_id.name or web.name
    return order.user_id.name or ""


def buscar(env, q="", periodo=DEFAULT_PERIODO, estado=DEFAULT_ESTADO, vendedor=None, now=None):
    """The orders of the page and its totals. `vendedor` (a res.users id)
    counts only for Gerencia; anybody else always gets his own orders."""
    user = env.user
    gerencia = es_gerencia(user)
    periodo = periodo if periodo in dict(PERIODOS) else DEFAULT_PERIODO
    estado = estado if estado in dict(ESTADOS) else DEFAULT_ESTADO
    start, end = rango_periodo(periodo, now)
    domain = Domain([("date_order", ">=", start), ("date_order", "<=", end)]) \
        & Domain(_ESTADO_DOMAIN[estado])
    vendedor_id = 0
    if gerencia:
        try:
            vendedor_id = int(vendedor or 0)
        except (TypeError, ValueError):
            vendedor_id = 0
        if vendedor_id:
            domain &= de_vendedor(vendedor_id)
    else:
        domain &= de_vendedor(user.id)
    q = (q or "").strip()[:80]
    if q:
        domain &= (Domain("name", "ilike", q) | Domain("partner_id.name", "ilike", q)
                   | Domain("distri_nombre", "ilike", q) | Domain("client_order_ref", "ilike", q))
    # sudo: the domain above is the access (own orders, or Gerencia)
    orders = env["sale.order"].sudo().search(domain, order="date_order desc, id desc",
                                             limit=LIMIT + 1)
    full = len(orders) > LIMIT
    rows = [fila(o) for o in orders[:LIMIT]]
    vivas = [r for r in rows if not r["cancelado"]]
    facturado = sum(r["facturado"] for r in vivas)
    a_cobrar = sum(r["a_cobrar"] for r in vivas)
    return {
        "rows": rows,
        "full": full,
        "q": q,
        "periodo": periodo,
        "estado": estado,
        "gerencia": gerencia,
        "vendedor_id": vendedor_id,
        "totales": {
            "n": len(vivas),
            "importe": _money(sum(r["amount_raw"] for r in vivas)),
            "cobrado": _money(facturado - a_cobrar),
            "a_cobrar": _money(a_cobrar),
            "cancelados": len(rows) - len(vivas),
        },
    }
