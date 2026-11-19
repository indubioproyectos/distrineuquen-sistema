"""Delivery routes, visits and the cash settlement ("rendición").

A route belongs to one driver, one day and one trip. Its stops are the
orders to deliver; each stop also holds the visit recorded by the phone
(see reparto_sync.py). The administrator settles the route at the depot:
cash expected vs counted, declared returns and undelivered orders.
"""
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .ar_phone import wa_digits
from .reparto_cliente import receipt_code_attempt_limit, today_ar

ROUTE_STATES = [
    ("draft", "Asignada"),
    ("on_route", "En reparto"),
    ("closed", "Cerrada por el repartidor"),
    ("settled", "Rendida"),
]

STOP_STATES = [
    ("pending", "Pendiente"),
    ("paid", "Pagó"),
    ("not_paid", "No pagó"),
    ("not_delivered", "No se entregó"),
    ("dispatched", "Encomienda despachada"),
]

DELIVERY_STATES = [
    ("pending", "Sin entregar"),
    ("delivered_code", "Entregado con código"),
    ("delivered_no_code", "Entregado sin código"),
    ("not_delivered", "No se entregó"),
    ("dispatched", "Despachado por encomienda"),
]

# «¿Paga con transferencia?» (approved design of 22/10/2026): the driver
# types the amount transferred and claims the receipt on the spot. The old
# «Ya transfirió» / «Transfiere después» answers are gone.
TRANSFER = [
    ("no", "No"),
    ("yes", "Sí"),
]

# What the driver answers about the receipt of the transfer. Since
# 19.0.1.86.0 (comprobante_app.py) he takes a photo of it inside the app
# («photo»), or says the customer sends it to the company's WhatsApp
# («customer_wa») or sends it later («not_yet»). «forwarded» (he forwarded it
# from his own WhatsApp) is what the app answered before; old phones may
# still send it.
RECEIPT_ANSWERS = [
    ("forwarded", "Ya lo reenvié"),
    ("photo", "Foto en la app"),
    ("customer_wa", "El cliente lo manda por WhatsApp"),
    ("not_yet", "Lo manda después"),
]
# the answers of a delivery at the depot (deposito.py): nobody takes a photo there
DEPOT_RECEIPT_ANSWERS = [a for a in RECEIPT_ANSWERS if a[0] in ("forwarded", "not_yet")]

# Before 19.0.1.25.0 the driver answered whether the customer had sent the
# receipt to the seller. Kept only to read old visits.
SENT_TO_VENDOR = [
    ("yes", "Sí, lo envió"),
    ("not_yet", "Todavía no"),
    ("unknown", "No sabe"),
]

TRIPS = [("1", "Viaje 1"), ("2", "Viaje 2")]

ROUTE_KINDS = [
    ("route", "Reparto"),
    ("depot", "Entregas en depósito"),
]

# statement rows shown on the driver's «Ver cuenta» (19.0.1.92.0)
ROUTE_ACCOUNT_ROWS = 15

# «No volvió» at settlement: the driver reported the return, so the customer
# handed the goods back. Decision of ale (27/09/2026): the customer keeps the
# credit (posted credit note, no stock return) and the missing goods are the
# driver's responsibility (a «Devolución que no volvió» case against the
# route). If management decides the customer must pay them instead, set this
# to False: the provisional line then disappears at settlement, no credit
# note is made and the invoice amount stands.
MISSING_RETURN_CREDITS_CUSTOMER = True

# The new cash journal where depot deliveries and counter sales enter.
DEPOT_JOURNAL_CODE = "CDEP"
DEPOT_JOURNAL_NAME = "Caja depósito"
# 19.0.1.109.0 (owner): nobody closes a depot box by hand any more
DEPOT_TO_CASHIER = "Esta plata la recibe un cajero en su Caja del día."


class DistriRoute(models.Model):
    _name = "distri.route"
    _description = "Ruta de reparto"
    _inherit = ["mail.thread"]
    _order = "date desc, trip, id desc"

    name = fields.Char(string="Ruta", compute="_compute_name", store=True)
    # "depot": one per user and day, holds the deliveries at the depot
    # (deposito.py) with the same stops, returns and promises as a route.
    kind = fields.Selection(ROUTE_KINDS, string="Tipo", required=True, default="route",
                            index=True, readonly=True)
    driver_id = fields.Many2one(
        "res.users", string="Repartidor", required=True, tracking=True,
        domain=lambda self: self._driver_domain(),
    )
    date = fields.Date(string="Fecha", required=True, default=lambda self: today_ar(),
                       tracking=True)
    trip = fields.Selection(TRIPS, string="Viaje", required=True, default="1")
    zone_ids = fields.Many2many("distri.zona", string="Zonas")
    state = fields.Selection(ROUTE_STATES, string="Estado", default="draft",
                             required=True, tracking=True, copy=False)
    company_id = fields.Many2one("res.company", required=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    stop_ids = fields.One2many("distri.route.stop", "route_id", string="Visitas")
    undelivered_stop_ids = fields.One2many(
        "distri.route.stop", "route_id", string="Pedidos sin entregar",
        domain=[("state", "=", "not_delivered")])
    return_ids = fields.One2many("distri.route.return", "route_id", string="Devoluciones")
    case_ids = fields.One2many("distri.route.case", "route_id", string="Casos")
    promise_ids = fields.One2many("distri.promised.transfer", "route_id",
                                  string="Comprobantes pendientes")
    expense_ids = fields.One2many("distri.route.expense", "route_id", string="Gastos",
                                  domain=[("state", "!=", "cancelled")])
    parcel_ids = fields.One2many("distri.parcel", "route_id", string="Encomiendas")
    # 19.0.1.92.0: «Cobro en efectivo» (/cobro) on the user's depot box
    cash_collection_ids = fields.One2many("distri.cash.collection", "route_id",
                                          string="Cobros en efectivo")
    stop_count = fields.Integer(string="Pedidos", compute="_compute_totals", store=True)
    parcel_count = fields.Integer(string="Bultos", compute="_compute_totals", store=True)
    departed_at = fields.Datetime(string="Salió a repartir", readonly=True, copy=False)
    closed_at = fields.Datetime(string="Cerrada en el celular", readonly=True, copy=False)

    # ---- settlement
    cash_collected = fields.Monetary(string="Efectivo cobrado", compute="_compute_totals",
                                     store=True)
    expense_total = fields.Monetary(string="Gastos", compute="_compute_totals", store=True,
                                    help="Gastos pagados con el efectivo del día (nafta, "
                                         "repuestos, estacionamiento, encomiendas).")
    cash_expected = fields.Monetary(string="Efectivo a rendir",
                                    compute="_compute_totals", store=True,
                                    help="Efectivo cobrado menos los gastos.")
    cash_counted = fields.Monetary(string="Efectivo contado", tracking=True, copy=False)
    cash_count_done = fields.Boolean(string="Contado cargado", readonly=True, copy=False)
    cash_difference = fields.Monetary(string="Diferencia",
                                      compute="_compute_cash_difference", store=True)
    cash_candidates = fields.Text(string="Candidatos", compute="_compute_cash_candidates")
    driver_explanation = fields.Text(string="Explicación del repartidor", copy=False)
    journal_id = fields.Many2one(
        "account.journal", string="Diario del efectivo",
        domain="[('type', 'in', ('cash', 'bank')), ('company_id', '=', company_id)]",
        default=lambda self: self._default_journal())
    payment_ids = fields.Many2many("account.payment", string="Cobros registrados",
                                   readonly=True, copy=False)
    settled_at = fields.Datetime(string="Rendida el", readonly=True, copy=False)
    settled_by_id = fields.Many2one("res.users", string="Rindió", readonly=True, copy=False)
    depot_status = fields.Selection([("open", "Abierta"), ("closed", "Cerrada")],
                                    string="Caja", compute="_compute_depot_status")
    # 19.0.1.93.0 (C16): a depot box of a day before still open
    depot_overdue = fields.Boolean(string="De un día anterior, sin cerrar",
                                   compute="_compute_depot_status")
    # 19.0.1.93.0 (C1): customers who paid this route's cash and also at the
    # local (/cobro) the same day
    double_cash_note = fields.Text(string="Cobro doble", compute="_compute_double_cash_note")

    # ---- departure control ("Salida de viajes")
    loaded_stop_count = fields.Integer(string="Pedidos cargados", compute="_compute_loading",
                                       store=True)
    parcel_loaded_count = fields.Integer(string="Bultos cargados", compute="_compute_loading",
                                         store=True)
    load_missing = fields.Boolean(string="Faltan bultos", compute="_compute_loading", store=True)
    load_color = fields.Integer(compute="_compute_load_color")
    orders_summary = fields.Char(string="Pedidos del viaje", compute="_compute_orders_summary")
    departure_controlled_by_id = fields.Many2one("res.users", string="Controló", readonly=True,
                                                 copy=False, tracking=True)
    departure_controlled_at = fields.Datetime(string="Controlado el", readonly=True, copy=False)
    departure_note = fields.Text(string="Nota de la salida", copy=False)

    @api.model
    def _default_journal(self):
        Journal = self.env["account.journal"]
        company = self.env.company
        # the street cash never goes to «Caja depósito»: that journal holds
        # the cash taken at the depot, closed per user in «Cierre de caja
        # depósito». By code it sorts first (CDEP < CSH1), hence the filter.
        return (Journal.search([("type", "=", "cash"), ("company_id", "=", company.id),
                                ("code", "!=", DEPOT_JOURNAL_CODE)], limit=1)
                or Journal.search([("type", "=", "bank"), ("company_id", "=", company.id)],
                                  limit=1))

    @api.model
    def _driver_domain(self):
        group = self.env.ref("distri_pedido.grupo_reparto", raise_if_not_found=False)
        return [("all_group_ids", "in", group.ids)] if group else []

    @api.depends("driver_id.name", "date", "trip", "kind")
    def _compute_name(self):
        for route in self:
            day = route.date.strftime("%d/%m") if route.date else ""
            if route.kind == "depot":
                route.name = "Depósito · %s · %s" % (route.driver_id.name or "", day)
                continue
            route.name = "%s · %s · Viaje %s" % (
                route.driver_id.name or "Sin repartidor", day, route.trip or "")

    @api.depends("state", "date", "kind")
    def _compute_depot_status(self):
        today = today_ar()
        for route in self:
            route.depot_status = "closed" if route.state == "settled" else "open"
            route.depot_overdue = (route.kind == "depot" and route.state != "settled"
                                   and bool(route.date) and route.date < today)

    def _double_cash_stops(self):
        """[(stop, collections)] of the stops with cash whose customer also
        paid in cash at the local (/cobro) the day of the route, or against
        the stop's own invoice since the stop exists (C1)."""
        self.ensure_one()
        Cash = self.env["distri.cash.collection"].sudo()
        out = []
        for stop in self.stop_ids.filtered(lambda s: s.cash_amount > 0):
            partner = stop.partner_id.commercial_partner_id
            same_day = Cash.search([("partner_id", "=", partner.id), "|",
                                    ("route_id.date", "=", self.date),
                                    ("create_date", ">=", stop.create_date)])
            if same_day or stop.paid_elsewhere > 0:
                out.append((stop, same_day))
        return out

    @api.depends("stop_ids.cash_amount", "date")
    def _compute_double_cash_note(self):
        for route in self:
            lines = []
            if route.kind == "route" and route.id:
                for stop, collections in route._double_cash_stops():
                    lines.append("%s (%s): cobró %s en la ruta y %s en el local%s." % (
                        stop.partner_id._distri_app_name(), stop.order_id.name,
                        _money(stop.cash_amount),
                        _money(sum(collections.mapped("amount")) or stop.paid_elsewhere),
                        " (%s)" % ", ".join(sorted(set(collections.user_id.mapped("name"))))
                        if collections else ""))
            route.double_cash_note = "\n".join(lines) or False

    @api.depends("stop_ids.loaded", "stop_ids.order_id.distri_bultos")
    def _compute_loading(self):
        for route in self:
            loaded = route.stop_ids.filtered("loaded")
            route.loaded_stop_count = len(loaded)
            route.parcel_loaded_count = sum(loaded.mapped("parcels"))
            route.load_missing = route.parcel_loaded_count < sum(
                route.stop_ids.mapped("parcels"))

    @api.depends("load_missing", "dispatch_missing", "state")
    def _compute_load_color(self):
        for route in self:
            # kanban highlight: 2 is the amber/orange of Odoo's palette. At
            # the depot the double check counts (despacho.py); a route that
            # already left keeps the old rule.
            missing = route.dispatch_missing if route.state == "draft" else route.load_missing
            route.load_color = 2 if missing else 0

    @api.depends("stop_ids.order_id.name", "stop_ids.order_id.distri_bultos", "stop_ids.loaded",
                 "stop_ids.load_state", "state")
    def _compute_orders_summary(self):
        at_depot = {"both": "", "driver": " falta el depósito", "dispatch": " falta el repartidor",
                    "none": " sin cargar"}
        for route in self:
            route.orders_summary = ", ".join(
                "%s (%s%s)" % (s.order_id.name, s.parcels,
                               at_depot.get(s.load_state, "") if route.state == "draft"
                               else ("" if s.loaded else " sin cargar"))
                for s in route.stop_ids)

    @api.depends("stop_ids.cash_amount", "stop_ids.order_id.distri_bultos",
                 "expense_ids.amount", "expense_ids.state", "cash_collection_ids.amount")
    def _compute_totals(self):
        for route in self:
            route.stop_count = len(route.stop_ids)
            route.parcel_count = sum(route.stop_ids.mapped("parcels"))
            # the stops' cash plus what was collected from /cobro (19.0.1.92.0)
            route.cash_collected = (sum(route.stop_ids.mapped("cash_amount"))
                                    + sum(route.cash_collection_ids.mapped("amount")))
            route.expense_total = sum(route.expense_ids.filtered(
                lambda e: e.state != "cancelled").mapped("amount"))
            route.cash_expected = route.cash_collected - route.expense_total

    @api.depends("cash_counted", "cash_expected", "cash_count_done")
    def _compute_cash_difference(self):
        for route in self:
            route.cash_difference = (
                route.cash_counted - route.cash_expected if route.cash_count_done else 0.0)

    @api.depends("cash_difference", "stop_ids.state", "stop_ids.cash_amount")
    def _compute_cash_candidates(self):
        for route in self:
            route.cash_candidates = "\n".join(route._cash_candidates())

    def _cash_candidates(self):
        """Stops that could explain a cash difference.

        Customers marked "no pagó", and cash lines whose amount (or balance
        minus cash) matches the difference.
        """
        self.ensure_one()
        currency = self.currency_id
        diff = abs(self.cash_difference)
        if currency.is_zero(diff):
            return []
        out = []
        for stop in self.stop_ids:
            name = stop.partner_id._distri_app_name()
            left = stop.expected_balance - stop.cash_amount
            if stop.state == "not_paid":
                out.append("%s (No pagó, saldo %s)" % (name, _money(stop.expected_balance)))
            elif stop.cash_amount and currency.compare_amounts(stop.cash_amount, diff) == 0:
                out.append("%s (cobro en efectivo de %s)" % (name, _money(stop.cash_amount)))
            elif stop.cash_amount and currency.compare_amounts(left, diff) == 0:
                out.append("%s (le quedó un saldo de %s)" % (name, _money(left)))
        for expense in self.expense_ids.filtered(lambda e: e.state != "cancelled"):
            label = dict(expense._fields["kind"].selection).get(expense.kind)
            if currency.compare_amounts(expense.amount, diff) == 0:
                out.append("Gasto %s de %s" % (label, _money(expense.amount)))
            elif expense.amount_review and expense.ocr_amount and currency.compare_amounts(
                    abs(expense.amount - expense.ocr_amount), diff) == 0:
                out.append("Gasto %s: cargado %s, la foto dice %s" % (
                    label, _money(expense.amount), _money(expense.ocr_amount)))
        return out

    # -------------------------------------------------------- assignment

    def _free_orders_domain(self):
        return [
            ("state", "=", "sale"),
            ("distri_armado", "=", "armado"),
            ("partner_id.distri_generic_partner", "=", False),
            # waiting for the customer's WhatsApp: not in the operation yet
            ("distri_wa_state", "!=", "waiting"),
            ("company_id", "=", self.company_id.id),
            # «Lo voy a retirar»: the customer picks it up at the depot
            # («Entrega en depósito»), it never goes on the truck
            ("distri_entrega", "!=", "retira"),
        ]

    def _order_is_free(self, order):
        """Not on any route, or only on routes where it came back unloaded."""
        stops = order.distri_stop_ids
        return all(s.state == "not_delivered" and s.unloaded for s in stops)

    def action_assign_orders(self):
        """Packed and confirmed orders of the route's zones, oldest first."""
        for route in self:
            if route.state not in ("draft", "on_route"):
                raise UserError("La ruta ya se cerró.")
            if not route.zone_ids:
                raise UserError("Elegí las zonas de la ruta.")
            orders = self.env["sale.order"].search(
                route._free_orders_domain(), order="date_order, id")
            # an order the dispatcher left at the depot does not come back
            # on its own (despacho.py: «Deshacer» puts it back)
            left = route.left_order_ids.order_id
            orders = orders.filtered(
                lambda o: o._distri_route_zone() in route.zone_ids and route._order_is_free(o)
                and o not in left)
            route._add_orders(orders)
        return True

    def _add_orders(self, orders):
        self.ensure_one()
        seq = max(self.stop_ids.mapped("sequence") or [0])
        vals = []
        for order in orders:
            if order in self.stop_ids.order_id:
                continue
            seq += 10
            vals.append({
                "route_id": self.id,
                "order_id": order.id,
                "sequence": seq,
                "zone_id": order._distri_route_zone().id,
            })
        return self.env["distri.route.stop"].create(vals)

    # -------------------------------------------------------- the phone

    def _app_payload(self):
        """What the phone downloads. Never the plain receipt code: only a
        salted hash that the phone checks offline."""
        self.ensure_one()
        self.stop_ids._refresh_amounts()
        return {
            "id": self.id,
            "name": self.name,
            "date": fields.Date.to_string(self.date),
            "trip": self.trip,
            "state": self.state,
            "zones": ", ".join(self.zone_ids.mapped("name")),
            "stops": [stop._app_payload() for stop in self.stop_ids],
            # orders the dispatcher left at the depot (despacho.py)
            "left": self._left_payload(),
            # «Controlado»: the load ticks are locked
            "controlled": bool(self.departure_controlled_at),
            # the driver's own expenses, to restore a phone that lost them
            # (parcel shipping comes with its stop)
            "expenses": [{
                "uuid": e.uuid, "kind": e.kind, "amount": e.amount, "comment": e.comment or "",
                "captured_at": fields.Datetime.to_string(e.captured_at) if e.captured_at else "",
                "synced": True,
            } for e in self.expense_ids.filtered(lambda e: not e.stop_id and e.state != "cancelled")],
        }

    def _app_pick_payload(self):
        """One row of «Tus rutas de hoy» on the phone (19.0.1.113.0)."""
        self.ensure_one()
        states = self.stop_ids.mapped("state")
        done = len([s for s in states if s != "pending"])
        day = self.date.strftime("%d/%m") if self.date else ""
        bits = [day, "%s %s" % (len(states), "pedido" if len(states) == 1 else "pedidos")]
        if self.state == "on_route":
            bits.append("%s de %s hechas" % (done, len(states)))
        return {
            "id": self.id,
            "name": self.name,
            "state": self.state,
            "state_label": dict(ROUTE_STATES).get(self.state, ""),
            "title": "Viaje %s%s" % (self.trip or "", " · %s" % ", ".join(
                self.zone_ids.mapped("name")) if self.zone_ids else ""),
            "detail": " · ".join(b for b in bits if b),
        }

    def action_reopen(self):
        """Back to "En reparto" so the driver can fix a visit."""
        for route in self:
            if route.state != "closed":
                raise UserError("Solo se reabre una ruta cerrada y sin rendir.")
            route.state = "on_route"

    # -------------------------------------------------------- settlement

    def write(self, vals):
        if "cash_counted" in vals:
            vals = dict(vals, cash_count_done=True)
            if any(r.state == "settled" for r in self):
                raise UserError("La rendición ya se cerró.")
        return super().write(vals)

    def action_open_cash_case(self):
        self.ensure_one()
        if not self.cash_count_done:
            raise UserError("Cargá primero el efectivo contado.")
        if self.currency_id.is_zero(self.cash_difference):
            raise UserError("No hay diferencia de efectivo.")
        case = self._cash_case()
        if not case:
            case = self.env["distri.route.case"].create({
                "route_id": self.id,
                "kind": "cash",
                "amount": self.cash_difference,
                "candidates": self.cash_candidates,
                "driver_explanation": self.driver_explanation,
            })
        return {
            "type": "ir.actions.act_window",
            "res_model": "distri.route.case",
            "res_id": case.id,
            "view_mode": "form",
        }

    def _cash_case(self):
        return self.case_ids.filtered(lambda c: c.kind == "cash" and c.state == "open")[:1]

    def action_settle(self):
        """Closes the settlement and registers the cash as customer payments."""
        for route in self:
            if route.kind != "route":
                raise UserError(DEPOT_TO_CASHIER)
            if route.state != "closed":
                raise UserError("La ruta tiene que estar cerrada por el repartidor.")
            if not route.cash_count_done:
                raise UserError("Cargá el efectivo contado.")
            if route.return_ids.filtered(lambda r: r.settlement_state == "pending"):
                raise UserError("Marcá cada devolución: recibida en el depósito o no volvió.")
            if route.undelivered_stop_ids.filtered(lambda s: not s.unloaded):
                raise UserError("Marcá como descargados los pedidos sin entregar.")
            if not route.journal_id:
                raise UserError("Elegí el diario donde entra el efectivo.")
            if not route.currency_id.is_zero(route.cash_difference) and not route._cash_case():
                route.action_open_cash_case()
            route._credit_received_returns()
            route._register_cash_payments()
            route._flag_double_cash()
            route.expense_ids._settle(route.journal_id)
            route.write({
                "state": "settled",
                "settled_at": fields.Datetime.now(),
                "settled_by_id": self.env.user.id,
            })
            # «Rinde al día siguiente»: the driver's receipts of the days
            # before today that never reached the company expire now and
            # are charged to him (comprobante_pendiente.py)
            self.env["distri.promised.transfer"].sudo()._distri_expire_for_driver(
                route.driver_id)
        return True

    def _flag_double_cash(self):
        """C1 (19.0.1.93.0): a customer paid in cash on the route and at the
        local the same day and now has a credit: a case on the route, so
        administración checks it with the driver and the customer."""
        self.ensure_one()
        Case = self.env["distri.route.case"].sudo()
        for stop, collections in self._double_cash_stops():
            partner = stop.partner_id.commercial_partner_id
            balance = partner._distri_account_balance()
            if balance >= -0.004:
                continue
            text = ("%s (%s) quedó con saldo a favor de %s: pagó %s en efectivo en la ruta y "
                    "%s en el local%s. Revisá si pagó dos veces." % (
                        partner.name, stop.order_id.name, _money(-balance),
                        _money(stop.cash_amount),
                        _money(sum(collections.mapped("amount")) or stop.paid_elsewhere),
                        " (cobró %s)" % ", ".join(sorted(set(collections.user_id.mapped("name"))))
                        if collections else ""))
            Case.create({"route_id": self.id, "kind": "other", "amount": -balance,
                         "stop_id": stop.id, "candidates": text})
            self.message_post(body="Posible cobro doble: %s" % text)
            partner.message_post(body="Posible cobro doble: %s" % text)

    def _register_cash_payments(self):
        """One inbound payment per stop with cash, applied oldest invoice first.

        What the driver noted about transfers never creates a payment
        (decision 10): transfers are confirmed from Mercado Pago.
        """
        self.ensure_one()
        for stop in self.stop_ids.filtered(lambda s: s.cash_amount > 0):
            self._register_stop_cash(stop)

    def _register_stop_cash(self, stop):
        """The cash of one visit as a customer payment in the route's journal,
        applied oldest invoice first. Shared by the route settlement and the
        depot delivery (deposito.py)."""
        self.ensure_one()
        if not self.journal_id:
            raise UserError("Elegí el diario donde entra el efectivo.")
        partner = stop.partner_id.commercial_partner_id
        label = "Depósito" if self.kind == "depot" else "Reparto"
        # sudo: the cash the driver (or the depot) handed in is registered by
        # the settlement itself, whoever closes it; customer payments are
        # otherwise reserved to grupo_cobros (models/permisos_cobros.py)
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": stop.cash_amount,
            "journal_id": self.journal_id.id,
            "date": today_ar(),
            "memo": "%s %s · %s" % (label, self.name, stop.order_id.name),
            "company_id": self.company_id.id,
        })
        payment.action_post()
        self._apply_oldest_first(payment, partner)
        self.payment_ids = [(4, payment.id)]
        return payment

    def _apply_oldest_first(self, payment, partner):
        counterpart = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        if not counterpart:
            return
        open_lines = self.env["account.move.line"].search([
            ("partner_id", "=", partner.id),
            ("account_id", "=", counterpart.account_id.id),
            ("parent_state", "=", "posted"),
            ("reconciled", "=", False),
            ("amount_residual", ">", 0),
            ("move_id.move_type", "=", "out_invoice"),
        ], order="date_maturity, date, id")
        for line in open_lines:
            if counterpart.reconciled:
                break
            (counterpart + line).reconcile()

    def _credit_received_returns(self):
        """Settle the returns: posted credit note, stock return if it came back.

        Decision of ale (27/09/2026): the return already counts for the
        customer from the moment the driver reports it («Devolución
        informada», models/devolucion.py). Here, at settlement:

        - «Recibido en el depósito»: the goods go back to stock against the
          order's delivery (DEV-…) and the credit note is created and posted.
        - «No volvió»: see MISSING_RETURN_CREDITS_CUSTOMER. By default the
          customer keeps the credit (they gave the goods to the driver): the
          credit note is posted too and the case opened by «No volvió»
          (action_missing) stays against the route for the missing goods.

        The posted credit note is reconciled with the order's invoice and
        replaces the provisional line, so nothing counts twice. Settled
        returns stop being invoiceable (devolucion.py). An order without a
        posted invoice gets no credit note: its invoice leaves the returned
        units out.
        """
        self.ensure_one()
        states = ("received", "missing") if MISSING_RETURN_CREDITS_CUSTOMER else ("received",)
        to_settle = self.return_ids.filtered(
            lambda r: r.settlement_state in states and not r.processed and r.quantity > 0)
        for order, returns in to_settle.grouped("order_id").items():
            received = returns.filtered(lambda r: r.settlement_state == "received")
            if received:
                order._distri_return_stock(received, self.name)
            returns.write({"processed": True})
            invoice = order._distri_facturas_emitidas()[:1]
            if not invoice:
                draft = order.sudo().invoice_ids.filtered(
                    lambda f: f.move_type == "out_invoice" and f.state == "draft")
                order.message_post(body="Devolución rendida (%s): %s. %s" % (
                    self.name, ", ".join("%g %s" % (r.quantity, r.product_id.display_name)
                                         for r in returns),
                    "La factura en borrador todavía la incluye: corregí la cantidad antes de "
                    "emitirla." if draft else
                    "El pedido no tiene factura: al facturar se descuenta sola."))
                continue
            refund = self._post_return_credit_note(order, invoice, returns)
            returns.write({"credit_note_id": refund.id})

    def _post_return_credit_note(self, order, invoice, returns):
        """Credit note of the returns, posted and applied to the invoice."""
        missing = returns.filtered(lambda r: r.settlement_state == "missing")
        where = "en depósito" if self.kind == "depot" else "en reparto"
        refund = self.env["account.move"].sudo().create({
            "move_type": "out_refund",
            "partner_id": invoice.partner_id.id,
            "journal_id": invoice.journal_id.id,
            "reversed_entry_id": invoice.id,
            "invoice_date": today_ar(),
            "invoice_origin": order.name,
            "ref": "Devolución %s · %s%s" % (
                where, self.name, " · no volvió al depósito" if missing else ""),
            "invoice_line_ids": [(0, 0, {
                "product_id": r.product_id.id,
                "quantity": r.quantity,
                "product_uom_id": r.order_line_id.product_uom_id.id,
                "price_unit": r.order_line_id.price_unit,
                "discount": r.order_line_id.discount,
                "tax_ids": [(6, 0, r.order_line_id.tax_ids.ids)],
                "sale_line_ids": [(6, 0, r.order_line_id.ids)],
            }) for r in returns],
        })
        refund.action_post()
        if invoice.amount_residual > 0:
            lines = (refund.line_ids | invoice.line_ids).filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            if len(lines.account_id) == 1:
                lines.reconcile()
        order.message_post(body="Nota de crédito %s emitida por la devolución (%s): %s." % (
            refund.name, self.name, ", ".join(
                "%g %s%s" % (r.quantity, r.product_id.display_name,
                             " (no volvió al depósito)" if r.settlement_state == "missing" else "")
                for r in returns)))
        return refund

    # -------------------------------------------------------- depot

    @api.model
    def _depot_journal(self, company=None):
        """«Caja depósito» of the company, created the first time."""
        company = company or self.env.company
        Journal = self.env["account.journal"].sudo()
        journal = Journal.search([("code", "=", DEPOT_JOURNAL_CODE),
                                  ("company_id", "=", company.id)], limit=1)
        if not journal:
            journal = Journal.create({
                "name": DEPOT_JOURNAL_NAME,
                "code": DEPOT_JOURNAL_CODE,
                "type": "cash",
                "company_id": company.id,
            })
        return journal

    @api.model
    def _depot_route_for(self, user=None, company=None):
        """The open depot "route" of the user for today, created if missing.

        One per user and day: the «Cierre de caja depósito» compares its cash
        with what that user counts.
        """
        user = user or self.env.user
        company = company or self.env.company
        domain = [("kind", "=", "depot"), ("driver_id", "=", user.id),
                  ("date", "=", today_ar()), ("state", "!=", "settled"),
                  ("company_id", "=", company.id)]
        route = self.sudo().search(domain, order="id", limit=1)
        if not route:
            # 19.0.1.93.0 (E2E-C C16): a box of a day before that was never
            # closed no longer blocks today's (before, nobody could register
            # cash after midnight until administración closed it). It stays
            # open for administración, highlighted in «Cierre de caja
            # depósito» (depot_overdue), and today's opens next to it.
            route = self.sudo().create({
                "kind": "depot",
                "driver_id": user.id,
                "date": today_ar(),
                "state": "on_route",
                "company_id": company.id,
                "journal_id": self._depot_journal(company).id,
            })
        return route.sudo(False)

    # -------------------------------------------------------- «Ver cuenta»

    @api.model
    def _driver_stop(self, user, stop_id):
        """The stop when it is on one of the user's own delivery routes, else
        an empty recordset: the driver's «Ver cuenta» (19.0.1.92.0) never
        opens another driver's stop. Any customer's account is in /cuentas."""
        try:
            wanted = int(stop_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        Stop = self.env["distri.route.stop"].sudo()
        stop = Stop.browse(wanted).exists() if wanted > 0 else Stop
        if not stop or stop.route_id.driver_id != user or stop.route_id.kind != "route":
            return Stop
        return stop

    @api.model
    def _app_partner_statement(self, partner, limit=ROUTE_ACCOUNT_ROWS):
        """Read-only account of one customer for the driver's app: balance and
        the newest statement rows (models/estado_cuenta.py)."""
        partner = partner.sudo().commercial_partner_id
        data = partner._distri_statement()
        rows = list(reversed(data["rows"]))[:limit]
        return {
            "partner_id": partner.id,
            "name": partner.name or "",
            "balance": data["balance"],
            "provisional": data["provisional"],
            "row_count": len(data["rows"]),
            "rows": [{
                "date": row["date"].isoformat() if row["date"] else "",
                "type": row["doc_type"] or "",
                "ref": row["remito"] or row["number"] or "",
                "concept": row["concept"] or "",
                "debit": row["debit"] or 0.0,
                "credit": row["credit"] or 0.0,
                "balance": row["balance"],
                "provisional": bool(row["provisional"]),
            } for row in rows],
        }

    def action_close_depot(self):
        """«Cerrar caja depósito» by hand: refused since 19.0.1.109.0.

        Owner's decision: the depot cash ALWAYS goes through a cashier. A
        cashier's own box closes with his «Caja del día» (distri.caja
        close_session); anybody else's (gerencia included, unless a cashier)
        is received by a cashier in /caja «Recibir plata» (receive_route),
        who counts it there. Both call _close_depot_box.
        """
        raise UserError(DEPOT_TO_CASHIER)

    def _close_depot_box(self):
        """Counted vs expected, like the settlement (only from the cashier's
        flows, see action_close_depot).

        The payments already exist (they are registered at each delivery);
        here a difference opens a case and received returns get their
        credit note.
        """
        self._check_admin()
        for route in self:
            if route.kind != "depot":
                raise UserError("Solo para las entregas en depósito.")
            if route.state == "settled":
                raise UserError("La caja ya se cerró.")
            if not route.cash_count_done:
                raise UserError("Cargá el efectivo contado.")
            if route.return_ids.filtered(lambda r: r.settlement_state == "pending"):
                raise UserError("Marcá cada devolución: recibida en el depósito o no volvió.")
            if not route.currency_id.is_zero(route.cash_difference) and not route._cash_case():
                route.action_open_cash_case()
            route._credit_received_returns()
            route.write({
                "state": "settled",
                "settled_at": fields.Datetime.now(),
                "settled_by_id": self.env.user.id,
            })
        return True

    # -------------------------------------------------------- departure

    def _check_admin(self):
        if not (self.env.su or self.env.user.has_group("distri_pedido.grupo_administracion")):
            raise UserError("Solo administración y gerencia.")

    def action_mark_controlled(self):
        """«Controlado»: closes the dispatch (despacho.py). A route still at
        the depot needs every order ticked by the driver and by the depot;
        one that already left (before the double check existed) is only
        noted, as before."""
        self._check_admin()
        for route in self:
            if route.kind != "route":
                raise UserError("Solo para las rutas de reparto.")
            if route.departure_controlled_at:
                raise UserError("La salida de %s ya se controló." % route.name)
            if route.state == "draft":
                if route.driver_id == self.env.user:
                    raise UserError("La carga la controla otra persona, no el repartidor de la ruta.")
                route._check_dispatch_complete("No se puede dar por controlada")
                body = route._dispatch_summary()
            else:
                body = "Salida controlada por %s: %s de %s bultos cargados." % (
                    self.env.user.name, route.parcel_loaded_count, route.parcel_count)
            route.write({
                "departure_controlled_by_id": self.env.user.id,
                "departure_controlled_at": fields.Datetime.now(),
            })
            route.message_post(body=body)
        return True


def _money(amount):
    """$ 62.300 / $ 62.300,50 like the app."""
    text = "{:,.2f}".format(amount or 0.0).replace(",", "X").replace(".", ",").replace("X", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return "$ " + text


class DistriRouteStop(models.Model):
    _name = "distri.route.stop"
    _description = "Visita de reparto"
    _order = "route_id, sequence, id"
    _rec_name = "order_id"

    route_id = fields.Many2one("distri.route", string="Ruta", required=True,
                               ondelete="cascade", index=True)
    route_state = fields.Selection(related="route_id.state", string="Estado de la ruta")
    route_kind = fields.Selection(related="route_id.kind", string="Tipo de entrega")
    driver_id = fields.Many2one(related="route_id.driver_id", store=True)
    currency_id = fields.Many2one(related="route_id.currency_id")
    sequence = fields.Integer(string="Orden", default=10)
    order_id = fields.Many2one("sale.order", string="Pedido", required=True,
                               ondelete="restrict", index=True,
                               domain="[('state', '=', 'sale')]")
    partner_id = fields.Many2one(related="order_id.partner_id", store=True, string="Cliente")
    shipping_partner_id = fields.Many2one(related="order_id.partner_shipping_id",
                                          string="Entrega en")
    parcels = fields.Integer(related="order_id.distri_bultos", string="Bultos")
    zone_id = fields.Many2one("distri.zona", string="Zona")

    loaded = fields.Boolean(string="Cargado", readonly=True)
    state = fields.Selection(STOP_STATES, string="Estado", default="pending",
                             required=True, readonly=True)
    delivery_state = fields.Selection(DELIVERY_STATES, string="Entrega", default="pending",
                                      required=True, readonly=True)

    # ---- visit, as recorded by the phone
    visit_uuid = fields.Char(string="Id de la visita", readonly=True, copy=False, index=True)
    visit_captured_at = fields.Datetime(string="Hora en el celular", readonly=True)
    visit_received_at = fields.Datetime(string="Recibida en el servidor", readonly=True)
    returns_answer = fields.Selection([("none", "No, recibió todo"), ("yes", "Sí, devolvió")],
                                      string="¿Devolvió algo?", readonly=True)
    code_mismatch = fields.Boolean(
        string="Código no coincide", readonly=True,
        help="El celular aceptó un código que no coincide con el del servidor "
             "(por ejemplo, se generó otro código después de descargar la ruta).")
    code_attempts = fields.Integer(
        string="Códigos equivocados", readonly=True,
        help="Códigos de recibido equivocados que contó el celular en esta visita (ya sumados "
             "a los intentos del pedido).")
    cash_amount = fields.Monetary(string="Efectivo", readonly=True)
    transfer = fields.Selection(TRANSFER, string="¿Paga con transferencia?", readonly=True)
    transfer_amount = fields.Monetary(string="Importe transferido", readonly=True)
    receipt_answer = fields.Selection(RECEIPT_ANSWERS, string="Comprobante", readonly=True)
    sent_to_vendor = fields.Selection(SENT_TO_VENDOR, string="Comprobante al vendedor (anterior)",
                                      readonly=True)
    # «Cliente en rojo» (comprobante_pendiente.py), snapshot sent to the
    # phone with the route; only while the setting is on
    red = fields.Boolean(string="Cliente en rojo", readonly=True,
                         help="Al descargar la ruta el cliente debía plata o comprobantes de "
                              "días anteriores.")
    red_debt = fields.Monetary(string="Deuda de días anteriores", readonly=True)
    red_receipts = fields.Integer(string="Comprobantes pendientes de días anteriores",
                                  readonly=True)
    red_breach = fields.Boolean(
        string="Falta grave: entregó en rojo", readonly=True,
        help="Se entregó a un cliente en rojo sin cobrar lo que debía de días anteriores.")
    comment = fields.Char(string="Comentario", readonly=True)
    not_delivered_reason = fields.Char(string="Motivo de no entrega", readonly=True)
    # «Datos para la entrega» (datos_entrega.py): what the customer asked
    # for this order from its link
    customer_notice = fields.Char(related="order_id.distri_customer_notice_text",
                                  string="Aviso del cliente")
    no_code_reason = fields.Char(
        string="Sin código porque", readonly=True,
        help="Entregas en depósito: «El cliente no tiene el código» o «Venta en mostrador "
             "(cliente presente)».")

    # ---- amounts: snapshot sent to the phone, so both compute the same
    order_amount = fields.Monetary(string="Pedido de hoy", readonly=True)
    previous_debt = fields.Monetary(string="Deuda anterior", readonly=True)
    return_ids = fields.One2many("distri.route.return", "stop_id", string="Devoluciones")
    returns_amount = fields.Monetary(string="Devolución", compute="_compute_balance", store=True)
    expected_balance = fields.Monetary(string="Saldo total", compute="_compute_balance",
                                       store=True)

    # ---- settlement of an undelivered order
    unloaded = fields.Boolean(string="Descargado")
    reassign_route_id = fields.Many2one(
        "distri.route", string="Pasar a la ruta",
        domain="[('state', 'in', ('draft', 'on_route')), ('kind', '=', 'route'), ('id', '!=', route_id)]")
    reassigned_stop_id = fields.Many2one("distri.route.stop", string="Nueva visita",
                                         readonly=True, copy=False)
    is_parcel = fields.Boolean(related="order_id.distri_encomienda", string="Encomienda")
    parcel_id = fields.Many2one("distri.parcel", string="Encomienda despachada",
                                compute="_compute_parcel_id")

    # 19.0.1.93.0 (C1): cash registered at the local (/cobro) against this
    # order's invoices since the stop exists; the app shows «Ya cobrado en el
    # local» and the amount to collect already counts it
    paid_elsewhere = fields.Monetary(string="Ya cobrado en el local",
                                     compute="_compute_paid_elsewhere")

    _order_once = models.Constraint(
        "unique(route_id, order_id)", "El pedido ya está en esta ruta.")

    def _compute_paid_elsewhere(self):
        Cash = self.env["distri.cash.collection"].sudo()
        for stop in self:
            total = 0.0
            invoices = stop.order_id._distri_facturas_emitidas() if stop.order_id else ()
            if invoices and stop.create_date:
                collections = Cash.search([
                    ("partner_id", "=", stop.partner_id.commercial_partner_id.id),
                    ("create_date", ">=", stop.create_date), ("payment_id", "!=", False)])
                receivable = collections.payment_id.move_id.line_ids.filtered(
                    lambda l: l.account_id.account_type == "asset_receivable")
                invoice_lines = invoices.line_ids.filtered(
                    lambda l: l.account_id.account_type == "asset_receivable")
                total = sum(receivable.matched_debit_ids.filtered(
                    lambda p: p.debit_move_id in invoice_lines).mapped("amount"))
            stop.paid_elsewhere = total

    @api.constrains("order_id", "route_id")
    def _check_one_active_route(self):
        """C3 (19.0.1.93.0): an order is on one route at a time, also when a
        stop is added by hand in the route form. A stop of a route not
        settled counts unless it came back undelivered and unloaded (the
        same rule as _order_is_free and «Deshacer»)."""
        for stop in self:
            busy = (stop.order_id.sudo().distri_stop_ids - stop).filtered(
                lambda s: s.route_id.state != "settled"
                and not (s.state == "not_delivered" and s.unloaded))
            if busy:
                raise ValidationError(
                    "El pedido %s ya está en %s. Sacalo de esa ruta (o marcalo como "
                    "descargado en la rendición) antes de sumarlo a otra." % (
                        stop.order_id.name, busy[:1].route_id.name))
    _visit_uuid_unique = models.Constraint(
        "unique(visit_uuid)", "Esa visita ya está registrada.")

    def _compute_parcel_id(self):
        Parcel = self.env["distri.parcel"].sudo()
        for stop in self:
            stop.parcel_id = Parcel.search([("stop_id", "=", stop.id)], limit=1) if stop.id \
                else Parcel

    @api.depends("order_amount", "previous_debt", "return_ids.amount")
    def _compute_balance(self):
        for stop in self:
            stop.returns_amount = sum(stop.return_ids.mapped("amount"))
            stop.expected_balance = stop.order_amount + stop.previous_debt - stop.returns_amount

    def _refresh_amounts(self):
        """Snapshot before the route goes to the phone. Visited stops keep theirs."""
        for stop in self.filtered(lambda s: s.state == "pending"):
            order = stop.order_id
            stop.write(dict({
                "order_amount": order._distri_amount_to_collect(),
                "previous_debt": order._distri_previous_debt(),
            }, **stop._distri_red_values()))

    @api.model_create_multi
    def create(self, vals_list):
        stops = super().create(vals_list)
        stops._refresh_amounts()
        return stops

    def _app_payload(self):
        self.ensure_one()
        order = self.order_id.sudo()
        partner = self.partner_id
        ship = self.shipping_partner_id or partner
        lines = order.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty)
        return {
            "id": self.id,
            "seq": self.sequence,
            "order_id": order.id,
            "order": order.name,
            # «Factura 0001-…», else the remito or the order (comprobante_pendiente.py)
            "document": self._distri_document_label(),
            "state": self.state,
            "delivery_state": self.delivery_state,
            "loaded": self.loaded,
            # the depot's tick (despacho.py), refreshed by /reparto/api/load
            "dispatch_checked": self.dispatch_checked,
            "dispatch_by": (self.dispatch_checked_by_id.name or "").split(" ")[0],
            "load_sequence": self.load_sequence,
            "partner_id": partner.id,
            "commercial_partner_id": partner.commercial_partner_id.id,
            "shipping_partner_id": ship.id,
            "name": partner._distri_app_name(),
            "town": partner._distri_town(),
            "address": ", ".join(p for p in (ship.street, ship.street2) if p),
            "parcels": order.distri_bultos,
            "zone": self.zone_id.name or "",
            "parcel": self._app_parcel(),
            "lines": [{
                "id": line.id,
                "product": line.product_id.with_context(
                    display_default_code=False).display_name,
                "qty": line.product_uom_qty,
                "uom": line.product_uom_id.name or "",
                "unit_price": (line.price_total / line.product_uom_qty),
            } for line in lines],
            "order_amount": self.order_amount,
            "previous_debt": self.previous_debt,
            # 19.0.1.93.0 (C1): «Ya cobrado en el local: $X»
            "paid_elsewhere": self.paid_elsewhere,
            # 19.0.1.93.0 (C4): held by administración, never delivered
            "retained": (order.distri_retenido_motivo or "Retenido por administración")
            if order.distri_retenido else "",
            # 19.0.1.99.0 (audit F12): a locked code is not sent; the phone
            # also stops after the wrong tries left
            "code_locked": order.distri_receipt_locked,
            "code_tries_left": max(receipt_code_attempt_limit(self.env)
                                   - order.distri_receipt_attempts, 0),
            "code_attempts_recorded": self.code_attempts,
            "code_salt": "" if order.distri_receipt_locked else (order.distri_receipt_salt or ""),
            "code_hash": "" if order.distri_receipt_locked else (order.distri_receipt_hash or ""),
            "whatsapp": partner._distri_whatsapp_shown(),
            "whatsapp_confirmed": partner.distri_whatsapp_confirmed,
            "customer": {
                "lat": ship.partner_latitude or None,
                "lng": ship.partner_longitude or None,
                "location_by": ship.distri_location_user_id.name or "",
                "location_date": fields.Datetime.to_string(ship.distri_location_date)
                if ship.distri_location_date else "",
                "days": ship.distri_delivery_days or "",
                "hours": ship.distri_delivery_hours or "",
                "note": ship.distri_driver_note or "",
                "text": ship.distri_delivery_text or "",
                "hours_by_customer_date": fields.Datetime.to_string(
                    ship.distri_customer_hours_date) if ship.distri_customer_hours_date else "",
                # sent by the customer from the order link; the location
                # above (the driver's) is never overwritten by it
                "customer_lat": ship.distri_customer_latitude
                if ship.distri_customer_location_date else None,
                "customer_lng": ship.distri_customer_longitude
                if ship.distri_customer_location_date else None,
                "customer_accuracy": round(ship.distri_customer_location_accuracy or 0),
                "customer_location_date": fields.Datetime.to_string(
                    ship.distri_customer_location_date)
                if ship.distri_customer_location_date else "",
            },
            "notice": order._distri_notice_payload(),
            # 19.0.1.98.0: the customer's comment and the depot note of the order
            "comment": order.distri_comentario or "",
            "depot_note": order.distri_nota_deposito or "",
            # «Cliente en rojo»: what the customer owes from the days before
            "red": self._distri_red_payload(),
            "visit": self._app_visit() if self.state != "pending" else None,
        }

    def _app_parcel(self):
        """Parcel stop: the driver takes it to the bus terminal."""
        order = self.order_id.sudo()
        if not order.distri_encomienda:
            return None
        from .encomienda import PAYER_LABELS
        return {
            "carrier": order.distri_carrier_id.name or "",
            "payer": order.distri_envio_paga or "distri",
            "payer_label": PAYER_LABELS.get(order.distri_envio_paga or "distri"),
            "town": order._distri_parcel_town(),
            "remito": order._distri_remitos_texto() or order.name,
            "recipient": order.distri_encomienda_nombre or "",
            "dni": order.distri_encomienda_dni or "",
        }

    def _app_visit(self):
        """The visit as the server has it, to restore a phone that lost it."""
        if self.state == "dispatched":
            parcel = self.parcel_id
            return {
                "visit_uuid": self.visit_uuid,
                "captured_at": fields.Datetime.to_string(self.visit_captured_at)
                if self.visit_captured_at else "",
                "outcome": "dispatched" if self.state == "dispatched" else "not_delivered",
                "reason": self.not_delivered_reason or "",
                "guide_number": parcel.guide_number or "",
                "cost": parcel.cost,
                "packages": parcel.packages,
                "has_photo": bool(parcel.photo),
                "comment": self.comment or "",
                "synced": True,
            }
        return {
            "visit_uuid": self.visit_uuid,
            "captured_at": fields.Datetime.to_string(self.visit_captured_at)
            if self.visit_captured_at else "",
            "outcome": "not_delivered" if self.state == "not_delivered" else "delivered",
            "reason": self.not_delivered_reason or "",
            "returns_answer": self.returns_answer or None,
            "returns": [{"uuid": r.uuid, "line_id": r.order_line_id.id, "qty": r.quantity,
                         "reason": r.reason or ""} for r in self.return_ids],
            "code_ok": self.delivery_state == "delivered_code",
            "without_code": self.delivery_state == "delivered_no_code",
            "cash": self.cash_amount,
            "transfer": self.transfer or "no",
            "transfer_amount": self.transfer_amount,
            "receipt_answer": self.receipt_answer or None,
            "red_ok": bool(self.red),
            "no_payment": self.state == "not_paid",
            "comment": self.comment or "",
            "synced": True,
        }

    def unlink(self):
        # «Anular ruta» (models/anular_ruta.py) undoes visits it checked for money
        if self.env.context.get("distri_route_cancel"):
            return super().unlink()
        if any(s.state != "pending" or s.route_id.state == "settled" for s in self):
            raise UserError("No se puede sacar de la ruta un pedido que ya tiene la visita.")
        return super().unlink()

    def action_reassign(self):
        """Puts an undelivered, unloaded order on another route."""
        for stop in self:
            if stop.state != "not_delivered":
                raise UserError("Solo se reasigna un pedido sin entregar.")
            if not stop.unloaded:
                raise UserError("Marcalo primero como descargado.")
            if not stop.reassign_route_id:
                raise UserError("Elegí la ruta.")
            if stop.reassigned_stop_id:
                raise UserError("Ya está en %s." % stop.reassigned_stop_id.route_id.name)
            new = stop.reassign_route_id._add_orders(stop.order_id)
            if not new:
                raise UserError("El pedido ya está en esa ruta.")
            stop.reassigned_stop_id = new[:1].id
        return True


class DistriRouteReturn(models.Model):
    _name = "distri.route.return"
    _description = "Devolución en reparto"
    _order = "id"

    stop_id = fields.Many2one("distri.route.stop", required=True, ondelete="cascade",
                              index=True, string="Visita")
    route_id = fields.Many2one(related="stop_id.route_id", store=True, index=True)
    order_id = fields.Many2one(related="stop_id.order_id", store=True, string="Pedido")
    partner_id = fields.Many2one(related="stop_id.partner_id", string="Cliente")
    currency_id = fields.Many2one(related="stop_id.currency_id")
    uuid = fields.Char(required=True, readonly=True, copy=False, index=True)
    order_line_id = fields.Many2one("sale.order.line", required=True, readonly=True,
                                    ondelete="restrict", string="Renglón")
    product_id = fields.Many2one(related="order_line_id.product_id", store=True,
                                 string="Producto")
    quantity = fields.Float(string="Cantidad", readonly=True, digits="Product Unit")
    reason = fields.Char(string="Motivo", readonly=True)
    unit_price = fields.Monetary(string="Precio unitario", readonly=True)
    amount = fields.Monetary(string="Importe", compute="_compute_amount", store=True)
    settlement_state = fields.Selection([
        ("pending", "Sin controlar"),
        ("received", "Recibido en el depósito"),
        ("missing", "No volvió"),
    ], string="Control", default="pending", required=True, readonly=True)
    credit_note_id = fields.Many2one("account.move", string="Nota de crédito",
                                     readonly=True, copy=False)
    case_id = fields.Many2one("distri.route.case", string="Caso", readonly=True, copy=False)

    _uuid_unique = models.Constraint("unique(uuid)", "Esa devolución ya está registrada.")

    @api.depends("quantity", "unit_price")
    def _compute_amount(self):
        for ret in self:
            ret.amount = ret.quantity * ret.unit_price

    def _check_open(self):
        if any(r.route_id.state == "settled" for r in self):
            raise UserError("La rendición ya se cerró.")
        if any(r.processed for r in self):
            raise UserError("La devolución ya volvió al stock y tiene su nota de crédito.")

    def action_received(self):
        self._check_open()
        for ret in self:
            if ret.case_id.state == "open" and ret.case_id.kind == "return":
                ret.case_id.unlink()
            ret.settlement_state = "received"

    def action_missing(self):
        self._check_open()
        for ret in self:
            if not ret.case_id:
                ret.case_id = self.env["distri.route.case"].create({
                    "route_id": ret.route_id.id,
                    "kind": "return",
                    "amount": -ret.amount,
                    "return_id": ret.id,
                    "candidates": "%s · %s: %g %s (motivo: %s)" % (
                        ret.partner_id._distri_app_name(), ret.order_id.name,
                        ret.quantity, ret.product_id.display_name, ret.reason or "-"),
                }).id
            ret.settlement_state = "missing"


class DistriRouteCase(models.Model):
    _name = "distri.route.case"
    _description = "Caso abierto contra una ruta"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(compute="_compute_name", store=True, string="Caso")
    # 19.0.1.97.0: a case is against a route or against a cashier's daily
    # cash session (models/caja.py: the difference at closing). One of both.
    route_id = fields.Many2one("distri.route", ondelete="cascade", string="Ruta", index=True)
    caja_id = fields.Many2one("distri.caja", ondelete="cascade", string="Caja", index=True,
                              readonly=True)
    driver_id = fields.Many2one("res.users", compute="_compute_person", store=True,
                                string="Repartidor")
    date = fields.Date(compute="_compute_person", store=True, string="Fecha de la ruta")
    currency_id = fields.Many2one("res.currency", compute="_compute_currency")
    kind = fields.Selection([
        ("cash", "Diferencia de efectivo"),
        ("return", "Devolución que no volvió"),
        ("expense", "Gasto sin comprobante o rechazado"),
        # charged to the driver (comprobante_pendiente.py)
        ("receipt", "Comprobante de transferencia no rendido"),
        ("short", "Transferencia menor al total"),
        ("unpaid", "Pedido no cobrado"),
        ("other", "Otro"),
    ], string="Tipo", required=True, default="other")
    amount = fields.Monetary(string="Importe",
                             help="Negativo: falta plata o mercadería. Positivo: sobra.")
    candidates = fields.Text(string="Candidatos")
    driver_explanation = fields.Text(string="Explicación del repartidor")
    investigation = fields.Text(string="Investigación")
    resolution = fields.Text(string="Resolución")
    replenished = fields.Boolean(string="Repuesto por el repartidor", tracking=True)
    state = fields.Selection([("open", "Abierto"), ("closed", "Resuelto")],
                             default="open", required=True, tracking=True, string="Estado")
    return_id = fields.Many2one("distri.route.return", string="Devolución", readonly=True)
    expense_id = fields.Many2one("distri.route.expense", string="Gasto", readonly=True)
    stop_id = fields.Many2one("distri.route.stop", string="Visita", readonly=True,
                              ondelete="set null")
    promise_id = fields.Many2one("distri.promised.transfer", string="Comprobante pendiente",
                                 readonly=True, ondelete="set null")

    @api.depends("kind", "route_id.name", "caja_id.name")
    def _compute_name(self):
        labels = dict(self._fields["kind"].selection)
        for case in self:
            case.name = "%s · %s" % (labels.get(case.kind, ""),
                                     case.route_id.name or case.caja_id.name or "")

    @api.depends("route_id.driver_id", "route_id.date", "caja_id.user_id", "caja_id.date")
    def _compute_person(self):
        """The driver and date of the route, or the cashier and day of the
        cash session (the person the case is about)."""
        for case in self:
            case.driver_id = case.route_id.driver_id or case.caja_id.user_id
            case.date = case.route_id.date or case.caja_id.date

    @api.depends("route_id", "caja_id")
    def _compute_currency(self):
        for case in self:
            case.currency_id = (case.route_id.currency_id or case.caja_id.currency_id
                                or self.env.company.currency_id)

    @api.constrains("route_id", "caja_id")
    def _check_route_or_caja(self):
        for case in self:
            if not case.route_id and not case.caja_id:
                raise ValidationError("El caso tiene que ser de una ruta o de una caja.")

    def action_close(self):
        for case in self:
            if not (case.resolution or "").strip():
                raise UserError("Escribí la resolución antes de cerrar el caso.")
            case.state = "closed"


class DistriPromisedTransfer(models.Model):
    """«Comprobante pendiente»: the receipt of a transfer the driver claimed
    at a delivery (approved design of 22/10/2026).

    The technical name comes from the «Transferencias prometidas» it
    replaces. The driver is responsible for it until the receipt reaches the
    company: «Pendiente» the day of the delivery, «Vencida» (charged to the
    driver) from the settlement of the next day, «Resuelta» when the receipt
    arrives by WhatsApp, someone marks it or the Mercado Pago report brings
    the transfer. The rules live in comprobante_pendiente.py.
    """
    _name = "distri.promised.transfer"
    _description = "Comprobante pendiente"
    _order = "captured_at desc, id desc"
    _rec_name = "partner_id"

    stop_id = fields.Many2one("distri.route.stop", required=True, ondelete="cascade",
                              index=True, string="Visita")
    route_id = fields.Many2one(related="stop_id.route_id", store=True, string="Ruta")
    driver_id = fields.Many2one(related="stop_id.driver_id", store=True,
                                string="Repartidor responsable")
    partner_id = fields.Many2one(related="stop_id.partner_id", store=True, string="Cliente")
    order_id = fields.Many2one(related="stop_id.order_id", store=True, string="Pedido")
    currency_id = fields.Many2one(related="stop_id.currency_id")
    # «done»: a transfer whose receipt must reach the company. «unpaid»
    # (19.0.1.26.0): «No pagó nada», the order delivered and not collected.
    kind = fields.Selection([("done", "Transferencia sin comprobante"),
                             ("unpaid", "Pedido sin cobrar")],
                            string="Tipo", default="done", index=True)
    sent_to_vendor = fields.Selection(SENT_TO_VENDOR, string="Comprobante al vendedor (anterior)")
    receipt_answer = fields.Selection(RECEIPT_ANSWERS, string="Respuesta del repartidor")
    amount = fields.Monetary(
        string="Importe",
        help="Importe transferido que dijo el repartidor: el comprobante tiene que llegar por "
             "ese importe. Nunca crea un pago: la transferencia se confirma con el reporte de "
             "Mercado Pago.")
    comment = fields.Char(string="Comentario")
    captured_at = fields.Datetime(string="Entregado")
    state = fields.Selection([
        ("open", "Pendiente"),
        ("fulfilled", "Resuelta"),
        ("cancelled", "Anulada"),
    ], default="open", required=True, string="Estado", index=True)
    reminded_at = fields.Datetime(string="Último reclamo", readonly=True)
    reminded_by_id = fields.Many2one("res.users", string="Reclamó", readonly=True)

    _one_per_stop = models.Constraint("unique(stop_id)", "La visita ya tiene su comprobante.")

    def _whatsapp_digits(self):
        self.ensure_one()
        return wa_digits(self.partner_id._distri_whatsapp_shown())

    def action_remind_whatsapp(self):
        """«Reclamar por WhatsApp»: opens the customer's chat with the claim
        already written."""
        self.ensure_one()
        self.check_access("read")
        digits = self.sudo()._whatsapp_digits()
        if not digits:
            raise UserError("El cliente no tiene WhatsApp cargado.")
        # comprobante_pendiente.py: every open item of the customer
        text = self._reminder_text()
        self.sudo().write({"reminded_at": fields.Datetime.now(),
                           "reminded_by_id": self.env.user.id})
        return {
            "type": "ir.actions.act_url",
            "url": "https://wa.me/%s?text=%s" % (digits, quote(text)),
            "target": "new",
        }

    def action_fulfilled(self):
        self.write({"state": "fulfilled"})

    def action_cancel(self):
        self.write({"state": "cancelled"})
