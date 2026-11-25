"""19.0.1.129.34 («Etapa 3b», part 2), behind distri_pedido.ui_v2 (OFF by
default: with it off the three routes answer 404 and every kit link still
opens the Odoo form, as in 129.33).

The order, the invoice and the customer as screens of the kit, so the daily
roles (administración, cajero, depósito, vendedor) look them up without the
Odoo form:

    /pedidos/<id>    a sale order: customer, state (the «Mis ventas» chips:
                     confirmation, packing, delivery, payment), lines, its
                     deliveries, its route stops and its invoices
    /facturas/<id>   a customer invoice or credit note: header, lines, the
                     payments already reconciled with it and its balance
    /clientes/<id>   a customer: contact data, price list, balance (the
                     /cuentas statement), recent orders and open invoices

Read-only. Nothing here writes: no route creates, edits or deletes an
account move, a payment or a reconciliation (not even the invoice PDF, whose
print saves an attachment on the move: it stays in «Ver en el sistema», the Odoo form). The
actions each screen offers are links to the kit screens that already do them
(«Asignar cliente», «Retener», «Entregar en depósito», «Cobrar», «Estado de
cuenta») or the same form post as /revisar («Liberar»); each of those checks
its own permissions again.

Who sees what: exactly what the backend gives the user. The record is read
with the user's own environment and `check_access("read")` (ACLs and record
rules: an external seller only his customers, an internal seller his
portfolio and his orders, administración and gerencia everything), plus
res.users._distri_may_reach_customer for the customer (the kit's one check of
the external seller). A record the user cannot read answers 404, the same as
one that does not exist. The screens are for the daily roles (vendedor,
administración, gerencia, Sistemas); a packer or driver without them gets 403.
Related records (the invoice's payments, the order's pickings and stops) are
read with sudo only after that check, as the backend form shows them. «Ver en
el sistema» (the Odoo form; #86 keeps «Odoo» out of visible text) is offered to administración (gerencia implies it) and Sistemas.
"""
from urllib.parse import quote

from werkzeug.exceptions import Forbidden, NotFound

from odoo import fields, http
from odoo.exceptions import AccessError
from odoo.http import request

from ..models.agilpedido_wa import cantidad_texto
from ..models.reparto import _money
from .kit import HOME, SCREEN_PARENTS, kit_context, page_nav, route_matches, ui_v2_on
from .tablero import wants_tablero

DAILY_GROUPS = ("distri_pedido.grupo_vendedor", "distri_pedido.grupo_administracion",
                "base.group_system")
ODOO_GROUPS = ("distri_pedido.grupo_administracion", "base.group_system")
# the screen of each kind of record (SCREEN_PARENTS keys)
SCREENS = {"pedido": "/pedidos/<id>", "factura": "/facturas/<id>", "cliente": "/clientes/<id>"}
ODOO_FORMS = {"pedido": "/odoo/sale.order/%s", "factura": "/odoo/account.move/%s",
              "cliente": "/odoo/res.partner/%s"}
INVOICE_TYPES = ("out_invoice", "out_refund")
MOVE_STATE = {"draft": ("Borrador", "neutral"), "posted": ("Emitida", "ok"),
              "cancel": ("Anulada", "neutral")}
PAYMENT_STATE = {
    "not_paid": ("Sin pagar", "err"),
    "partial": ("Pago parcial", "warn"),
    "in_payment": ("Pagada", "ok"),
    "paid": ("Pagada", "ok"),
    "reversed": ("Revertida", "neutral"),
    "blocked": ("Bloqueada", "neutral"),
    "invoicing_legacy": ("Sistema anterior", "neutral"),
}
PICKING_STATE = {"draft": "Borrador", "waiting": "Esperando", "confirmed": "Esperando",
                 "assigned": "Listo para salir", "done": "Hecho", "cancel": "Cancelado"}
RECENT_ORDERS = 10
OPEN_INVOICES = 20
# 19.0.1.129.35: /pedidos/<id>?hecho=… after «Aprobar» or «Anular venta»
HECHO = {
    "aprobado": "Listo: el pedido quedó aprobado.",
    "anulada": "Listo: la venta quedó anulada.",
}


def may_use(user):
    """The daily roles: vendedor, administración (gerencia implies it) and
    Sistemas. Customers, packers and drivers do not use these screens."""
    return bool(user) and not user.share and not user._is_public() and any(
        user.has_group(g) for g in DAILY_GROUPS)


def sees_odoo(user):
    """«Ver en Odoo»: administración (gerencia implies it) and Sistemas."""
    return bool(user) and not user.share and any(user.has_group(g) for g in ODOO_GROUPS)


def ficha_url(kind, record_id, desde=""):
    """The kit screen of a record; `desde` (a path of one of its declared
    parents) is where «Volver» goes."""
    url = SCREENS[kind].replace("<id>", str(int(record_id)))
    return url + ("?desde=%s" % quote(desde, safe="/") if desde else "")


def record_link(env, kind, record_id, desde="", fallback=None):
    """19.0.1.129.34: where a kit link to a record goes: its kit screen with
    ui_v2 on, else `fallback` (the Odoo form, as before)."""
    if ui_v2_on(env):
        return ficha_url(kind, record_id, desde)
    return fallback if fallback is not None else ODOO_FORMS[kind] % int(record_id)


def _home(user):
    return "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero")


def _parent(kind, desde):
    """The declared parent `desde` names (SCREEN_PARENTS), else Inicio."""
    path = (desde or "").split("?", 1)[0].split("#", 1)[0]
    if path.startswith("/") and not path.startswith("//"):
        for pattern in SCREEN_PARENTS[SCREENS[kind]]:
            if pattern != HOME and route_matches(pattern, path):
                return path
    return ""


def _readable(records):
    """The records (in the user's environment) the user may read."""
    try:
        return records._filtered_access("read")
    except AccessError:
        return records.browse()


def _date(value):
    """«25/11/2026» for a date (or an ISO date string, as some JSON fields
    of Odoo carry it)."""
    if not value:
        return ""
    if isinstance(value, str):
        try:
            value = fields.Date.to_date(value[:10])
        except ValueError:
            return value
    return value.strftime("%d/%m/%Y")


def _when(value):
    from .revisar import cuando
    return cuando(value)


def _label(record, field):
    return dict(record._fields[field]._description_selection(record.env)).get(record[field], "")


class Fichas(http.Controller):

    # ------------------------------------------------------------ helpers

    def _check(self):
        env = request.env
        if not ui_v2_on(env):
            raise NotFound()
        if not may_use(env.user):
            raise Forbidden()
        return env.user

    def _get(self, model, record_id):
        """The record in the user's environment, when he may read it (the
        backend's rules); 404 otherwise, as if it did not exist."""
        record = request.env[model].browse(int(record_id)).exists()
        if not record:
            raise NotFound()
        try:
            record.check_access("read")
        except AccessError:
            raise NotFound() from None
        return record

    def _may_reach(self, partner):
        return request.env.user._distri_may_reach_customer(partner)

    def _customer_link(self, partner, desde=""):
        """The customer's screen, when the user may open it."""
        commercial = partner.sudo().commercial_partner_id
        if not commercial or commercial.distri_generic_partner:
            return ""
        mine = request.env["res.partner"].browse(commercial.id)
        if not _readable(mine) or not self._may_reach(commercial):
            return ""
        return ficha_url("cliente", commercial.id, desde)

    def _account_link(self, commercial):
        """/cuentas on that customer, when the accounts app shows it to the
        user (models/cuentas_app.py, the same rule as the app)."""
        if not commercial or commercial.distri_generic_partner:
            return ""
        App = request.env["distri.account.app"]
        try:
            allowed = App._app_allowed(commercial.sudo())
        except AccessError:
            return ""
        return "/cuentas#cuenta/%s" % commercial.id if allowed else ""

    def _collect_link(self, commercial):
        from ..models.cobrar import can_collect
        if not commercial or commercial.distri_generic_partner:
            return ""
        if not can_collect(request.env.user) or not self._may_reach(commercial):
            return ""
        return "/cobro?cliente=%s" % commercial.id

    def _upload_link(self, partner):
        """19.0.1.129.37 (verificacion_v2): «Subir comprobantes» of this
        customer, several at once (controllers/comprobantes.py)."""
        from ..models.comprobante_subir import can_upload
        from ..models.verificacion import verificacion_on
        env = request.env
        if partner.distri_generic_partner or not (verificacion_on(env) and can_upload(env.user)):
            return ""
        return "/comprobantes?cliente_id=%s" % partner.commercial_partner_id.id

    def _render(self, template, kind, record, desde, values, status=200):
        user = request.env.user
        home = _home(user)
        parent = _parent(kind, desde) or home
        base = {
            "dn": kit_context("deposito.css", "fichas.css"),
            "user": user,
            "volver": parent,
            "parent": parent,
            "nav": page_nav(request.env, ""),
            "odoo_url": ODOO_FORMS[kind] % record.id if sees_odoo(user) else "",
            "csrf_token": request.csrf_token(),
            "ok": "",
            "error": "",
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    # ------------------------------------------------------------ /pedidos/<id>

    def _order_actions(self, order, commercial):
        """Links to the kit screens that already act on an order, each one
        only when that screen would let this user do it."""
        from ..models.asignar_cliente import puede_asignar
        from ..models.revisar import puede_revisar
        from .deposito import deliverable_domain, is_depot
        env = request.env
        user = env.user
        o = order.sudo()
        vivo = o.state != "cancel"
        generic = not o.partner_id or o.partner_id.distri_generic_partner
        acts, liberar = [], ""
        if puede_revisar(env):
            if vivo and generic and o.partner_id and puede_asignar(env):
                acts.append({"label": "Asignar cliente", "icon": "user", "kind": "outline",
                             "url": "/revisar/%s/asignar" % o.id})
            if vivo and not o.distri_retenido and o.distri_revisar_en_lista:
                acts.append({"label": "Retener", "icon": "pause", "kind": "warn",
                             "url": "/revisar?retener=%s#rv-retener" % o.id})
            if o.distri_retenido:
                liberar = "/revisar/%s/liberar" % o.id
            if o.distri_revisar_wa_url:
                acts.append({"label": "Abrir chat", "icon": "chat", "kind": "ghost",
                             "url": o.distri_revisar_wa_url, "blank": True})
        if is_depot(user) and env["sale.order"].sudo().search_count(
                deliverable_domain() + [("id", "=", o.id)], limit=1):
            acts.append({"label": "Entregar en depósito", "icon": "store", "kind": "",
                         "url": "/deposito/entrega/%s" % o.id})
        if vivo and o.state == "sale":
            cobro = self._collect_link(commercial)
            if cobro:
                acts.append({"label": "Cobrar", "icon": "cash", "kind": "ghost", "url": cobro})
        cuenta = self._account_link(commercial)
        if cuenta:
            acts.append({"label": "Estado de cuenta", "icon": "chart", "kind": "ghost",
                         "url": cuenta})
        return acts, liberar

    def _despacho_visible(self):
        menu = request.env.ref("distri_pedido.menu_reparto_despacho", raise_if_not_found=False)
        return bool(menu and menu.id in request.env["ir.ui.menu"]._visible_menu_ids())

    @http.route("/pedidos/<int:order_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def pedido(self, order_id, desde="", hecho="", **kw):
        # 19.0.1.129.35: the result of «Aprobar» / «Anular venta» (a fixed
        # text per key: nothing of the URL is shown)
        return self._pedido_page(order_id, desde, ok=HECHO.get(hecho, ""))

    def _pedido_page(self, order_id, desde="", ok="", error="", status=200):
        from ..models.mis_ventas import fila
        self._check()
        order = self._get("sale.order", order_id)
        o = order.sudo()
        here = "/pedidos/%s" % o.id
        row = fila(o)
        commercial = o.partner_id.commercial_partner_id
        generic = not o.partner_id or o.partner_id.distri_generic_partner
        lines = []
        for line in o.order_line:
            if line.display_type:
                lines.append({"section": line.name or ""})
                continue
            uom = line.product_uom_id
            lines.append({
                "section": "",
                "name": line.product_id.display_name or line.name or "",
                "q": cantidad_texto(line.product_uom_qty),
                "unidad": uom.name if uom else "",
                "precio": _money(line.price_unit),
                "sub": _money(line.price_subtotal),
                "falta": cantidad_texto(line.distri_faltante) if line.distri_faltante > 0 else "",
            })
        invoices = _readable(request.env["account.move"].browse(o.invoice_ids.ids))
        facturas = [{
            "name": m.name or "Borrador",
            "date": _date(m.invoice_date),
            "state": (PAYMENT_STATE.get(m.payment_state, ("", ""))
                      if m.state == "posted" else MOVE_STATE.get(m.state, ("", ""))),
            "type": "Nota de crédito" if m.move_type == "out_refund" else "Factura",
            "total": _money(m.amount_total),
            "saldo": _money(m.amount_residual) if m.state == "posted" else "",
            "url": ficha_url("factura", m.id, here),
        } for m in invoices.sudo()]
        entregas = [{
            "name": p.name,
            "state": PICKING_STATE.get(p.state, p.state),
            "date": _date(p.date_done or p.scheduled_date),
        } for p in o.picking_ids]
        despacho = self._despacho_visible()
        rutas = [{
            "name": s.route_id.display_name,
            "date": _date(s.route_id.date),
            "driver": s.route_id.driver_id.name or "",
            "route_state": _label(s.route_id, "state"),
            "state": _label(s, "delivery_state"),
            "url": "/despacho#ruta/%s" % s.route_id.id if despacho else "",
        } for s in o.distri_stop_ids.sorted(lambda s: (s.route_id.date or s.create_date.date(),
                                                         s.id), reverse=True)]
        acts, liberar = self._order_actions(order, commercial)
        return self._render("distri_pedido.ficha_pedido", "pedido", o, desde, {
            "o": o,
            "row": row,
            "cliente": row["cliente"],
            "cliente_url": "" if generic else self._customer_link(o.partner_id, ""),
            "generic": bool(generic),
            "vendedor": row["vendedor"],
            "cuando": _when(o.date_order),
            "codigo": o.distri_wa_code or "",
            "lista": o.pricelist_id.name or "",
            "nota": (o.distri_retenido_motivo or "") if o.distri_retenido else "",
            "total": _money(o.amount_total),
            "lines": lines,
            "facturas": facturas,
            "entregas": entregas,
            "rutas": rutas,
            "acts": acts,
            "liberar": liberar,
            "presupuesto": o.state in ("draft", "sent"),
            # 19.0.1.129.35 (distri_pedido.nueva_venta): «Aprobar» and
            # «Anular venta» (controllers/anular_venta.py)
            "aprobar": self._aprobar(order),
            "anular_url": self._anular_url(order),
            "anulada": (o.distri_anulada_motivo or "") if o.state == "cancel" else "",
            "ok": ok,
            "error": error,
        }, status=status)

    def _aprobar(self, order):
        """«Aprobar» on a quotation, for whoever may confirm it in the
        backend (write access on it: the same ACLs and record rules), with
        the WhatsApp question when the confirmation would ask it."""
        from ..models.nueva_venta import nueva_venta_on
        o = order.sudo()
        if not nueva_venta_on(request.env) or o.state not in ("draft", "sent"):
            return None
        try:
            order.check_access("write")
        except AccessError:
            return None
        pide = order._distri_kit_pide_whatsapp()
        requerido = request.env["distri.pedido.whatsapp.settings"].requires_whatsapp()
        area = numero = ""
        if pide:
            from ..models.ar_phone import ar_national, split_ar_national
            for value in (o.partner_id.phone, o.partner_id.commercial_partner_id.phone):
                national = ar_national(value)
                if national:
                    area, numero = split_ar_national(national)
                    break
        return {"url": "/pedidos/%s/aprobar" % o.id, "bloqueo": order._distri_kit_bloqueo_aprobar(),
                "pide_wa": pide, "sin_wa": pide and not requerido, "area": area,
                "numero": numero}

    def _anular_url(self, order):
        from ..models.anular_venta import puede_anular
        from ..models.nueva_venta import nueva_venta_on
        if (nueva_venta_on(request.env) and puede_anular(request.env.user)
                and order.sudo().state != "cancel"):
            return "/pedidos/%s/anular" % order.id
        return ""

    # ------------------------------------------------------------ /facturas/<id>

    @http.route("/facturas/<int:move_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def factura(self, move_id, desde="", **kw):
        self._check()
        move = self._get("account.move", move_id)
        m = move.sudo()
        if m.move_type not in INVOICE_TYPES:
            raise NotFound()
        here = "/facturas/%s" % m.id
        commercial = m.partner_id.commercial_partner_id
        if m.state == "posted":
            estado, estado_kind = PAYMENT_STATE.get(m.payment_state, ("", "neutral"))
        else:
            estado, estado_kind = MOVE_STATE.get(m.state, ("", "neutral"))
        lines = []
        for line in m.invoice_line_ids:
            if line.display_type in ("line_section", "line_note"):
                lines.append({"section": line.name or ""})
                continue
            if line.display_type != "product":
                continue
            lines.append({
                "section": "",
                "name": line.product_id.display_name or line.name or "",
                "q": cantidad_texto(line.quantity),
                "unidad": line.product_uom_id.name or "",
                "precio": _money(line.price_unit),
                "sub": _money(line.price_subtotal),
            })
        widget = m.invoice_payments_widget or {}
        pagos = [{
            "date": _date(p.get("date")),
            "name": p.get("ref") or p.get("name") or "",
            "journal": p.get("journal_name") or "",
            "amount": _money(p.get("amount")),
        } for p in (widget.get("content") or []) if isinstance(p, dict)]
        orders = _readable(request.env["sale.order"].browse(
            m.line_ids.sale_line_ids.order_id.ids))
        pedidos = [{"name": o.name, "url": ficha_url("pedido", o.id, here),
                    "total": _money(o.amount_total)} for o in orders.sudo()]
        acts = []
        if m.state == "posted" and m.move_type == "out_invoice" and m.amount_residual > 0.005:
            cobro = self._collect_link(commercial)
            if cobro:
                acts.append({"label": "Cobrar", "icon": "cash", "kind": "", "url": cobro})
        cuenta = self._account_link(commercial)
        if cuenta:
            acts.append({"label": "Estado de cuenta", "icon": "chart", "kind": "ghost",
                         "url": cuenta})
        return self._render("distri_pedido.ficha_factura", "factura", m, desde, {
            "m": m,
            "tipo": "Nota de crédito" if m.move_type == "out_refund" else "Factura",
            "estado": estado,
            "estado_kind": estado_kind,
            "cliente": commercial.name or m.partner_id.name or "",
            "cliente_url": self._customer_link(m.partner_id, ""),
            "fecha": _date(m.invoice_date),
            "vence": _date(m.invoice_date_due),
            "total": _money(m.amount_total),
            "saldo": _money(m.amount_residual),
            "posted": m.state == "posted",
            "lines": lines,
            "pagos": pagos,
            "pedidos": pedidos,
            "acts": acts,
        })

    # ------------------------------------------------------------ /clientes/<id>

    def _balance(self, commercial):
        """The balance /cuentas shows (the account statement), only when the
        accounts app shows this customer to the user."""
        if not self._account_link(commercial):
            return None
        rows = commercial.sudo()._distri_statement_rows()
        return rows[-1]["balance"] if rows else 0.0

    @http.route("/clientes/<int:partner_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def cliente(self, partner_id, desde="", **kw):
        self._check()
        partner = self._get("res.partner", partner_id)
        commercial = partner.sudo().commercial_partner_id
        if commercial.id != partner.id:
            return request.redirect(ficha_url("cliente", commercial.id, desde), 303)
        if not self._may_reach(commercial):
            raise NotFound()
        here = "/clientes/%s" % commercial.id
        p = commercial.sudo()
        env = request.env
        try:
            orders = env["sale.order"].search(
                [("partner_id", "child_of", p.id)], order="date_order desc, id desc",
                limit=RECENT_ORDERS)
        except AccessError:
            orders = env["sale.order"].browse()
        pedidos = []
        if orders:
            from ..models.mis_ventas import fila
            for o in orders.sudo():
                row = fila(o)
                pedidos.append({"name": o.name, "cuando": _when(o.date_order),
                                "total": row["amount"], "estado": row["estado"],
                                "estado_kind": row["estado_kind"],
                                "pago": row["pago"], "pago_kind": row["pago_kind"],
                                "url": ficha_url("pedido", o.id, here)})
        try:
            invoices = env["account.move"].search([
                ("partner_id", "child_of", p.id), ("move_type", "in", INVOICE_TYPES),
                ("state", "=", "posted"), ("payment_state", "in", ("not_paid", "partial"))],
                order="invoice_date, id", limit=OPEN_INVOICES)
        except AccessError:
            invoices = env["account.move"].browse()
        facturas = [{
            "name": m.name,
            "date": _date(m.invoice_date),
            "vence": _date(m.invoice_date_due),
            "type": "Nota de crédito" if m.move_type == "out_refund" else "Factura",
            "saldo": _money(m.amount_residual),
            "url": ficha_url("factura", m.id, here),
        } for m in invoices.sudo()]
        balance = self._balance(p)
        acts = []
        cobro = self._collect_link(p)
        if cobro:
            acts.append({"label": "Cobrar", "icon": "cash", "kind": "", "url": cobro})
        cuenta = self._account_link(p)
        if cuenta:
            acts.append({"label": "Estado de cuenta", "icon": "chart", "kind": "ghost",
                         "url": cuenta})
        subir = self._upload_link(p)
        if subir:
            acts.append({"label": "Subir comprobantes", "icon": "receipt", "kind": "ghost",
                         "url": subir})
        street = ", ".join(x for x in (p.street, p.street2) if x)
        town = ", ".join(x for x in (p.city, p.state_id.name) if x)
        return self._render("distri_pedido.ficha_cliente", "cliente", p, desde, {
            "p": p,
            "datos": [(k, v) for k, v in (
                ("CUIT", p.vat or ""),
                ("Código", p.ref or ""),
                ("WhatsApp", p.distri_whatsapp or ""),
                ("Teléfono", p.phone or ""),
                ("Domicilio", " · ".join(x for x in (street, town) if x)),
                ("Lista de precios", p.property_product_pricelist.name or ""),
                ("Vendedor", p.user_id.name or ""),
                ("Etiquetas", ", ".join(p.category_id.mapped("name"))),
            ) if v],
            "saldo": _money(balance) if balance is not None else "",
            "saldo_kind": ("err" if (balance or 0) > 0.005 else "ok") if balance is not None else "",
            "pedidos": pedidos,
            "facturas": facturas,
            "acts": acts,
        })
