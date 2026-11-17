"""«Caja del día» (19.0.1.97.0, audit 2026-11-17 proposal 3c, F3, F10).

The cashier's daily cash session: one per cashier and day, with an opening
float, every movement of the day (signed), the bill count at the closing and
its difference. All the cash of the company converges here:

- «Cobro en el local»: the cash the cashier took himself, from /cobro, the
  depot delivery or the counter sale. Those keep entering his own per-user
  «Caja depósito» box (distri.route kind «depot», models/reparto.py); the
  session reads them from there (`_sync_own_collections`) and closes that
  box at the closing.
- «Rendición»: a driver's route closed on the phone, received here
  (`receive_route`, the /caja/rendicion screen) instead of the long backend
  form: the expenses without paper are asked BEFORE (audit F10), the route is
  settled, and its cash enters the session.
- «Cobros de otro usuario»: another user's own «Caja depósito», received the
  same way: the cash stays «in the hands» of that person until the cashier
  takes it.
- Outflows (gasto, proveedor, retiro, depósito al banco) and «ajuste» (cash
  that came back later, e.g. a driver's shortage he brought the next day).

Rules: one open session per cashier; today's cannot open while a previous
one is open; the opening float is proposed from the previous closing's «queda
para mañana»; a difference needs a reason, is posted to the «diferencias de
caja» account and opens a case for the cashier (distri.route.case with
caja_id); a closed session is reopened only by gerencia (or the system
administrator) with a reason, logged in the chatter.

Accounting (owner's decision, option B; models/caja_contabilidad.py): the
cash payments keep going to «Recibos pendientes»; the closing writes one cash
statement on the session journal (CSH1 «Efectivo» by default) whose lines
reconcile those payments (they become «paid» and the money reaches the cash
account), post the outflows to their accounts and the differences to the
«diferencias de caja» account.
"""
import re
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .reparto import _money
from .reparto_cliente import today_ar

GRUPO_CAJERO = "distri_pedido.grupo_cajero"
GRUPO_GERENCIA = "distri_pedido.grupo_gerencia"

MOVEMENT_KINDS = [
    ("cobro", "Cobro en el local"),
    ("rendicion", "Rendición"),
    ("recepcion", "Cobros de otro usuario"),
    ("gasto", "Gasto"),
    ("proveedor", "Proveedor"),
    ("retiro", "Retiro"),
    ("banco", "Depósito al banco"),
    ("ajuste", "Ajuste"),
]
INFLOW_KINDS = ("cobro", "rendicion", "recepcion")
OUTFLOW_KINDS = ("gasto", "proveedor", "retiro", "banco")

EXPENSE_CONCEPTS = [
    ("flete", "Flete"),
    ("nafta", "Nafta"),
    ("libreria", "Librería"),
    ("sueldo", "Sueldo / adelanto"),
    ("otro", "Otro gasto"),
]

# The chips of «Salida de caja»: key -> (label, movement kind, concept)
OUTFLOW_CHIPS = [
    ("proveedor", "Proveedor", "proveedor", False),
    ("flete", "Flete", "gasto", "flete"),
    ("nafta", "Nafta", "gasto", "nafta"),
    ("libreria", "Librería", "gasto", "libreria"),
    ("sueldo", "Sueldo / adelanto", "gasto", "sueldo"),
    ("otro", "Otro gasto", "gasto", "otro"),
    ("retiro", "Retiro del dueño", "retiro", False),
    ("banco", "Banco", "banco", False),
]
CHIPS = {key: (label, kind, concept) for key, label, kind, concept in OUTFLOW_CHIPS}

DIFFERENCE_MODES = [
    ("cargo", "Queda a cargo del repartidor"),
    ("manana", "Lo trae mañana"),
]

MAX_QUANTITY = 9999  # the closing label fits four digits per row
NOTE_MAX = 250


class CajaQuestion(UserError):
    """Something the cashier has to answer first (never decided silently):
    the page shows the question and sends the answer back in `field`."""

    def __init__(self, message, field):
        super().__init__(message)
        self.field = field


def is_cashier(user):
    return bool(user) and not user.share and user.has_group(GRUPO_CAJERO)


def can_reopen(user):
    return user.has_group(GRUPO_GERENCIA) or user.has_group("base.group_system")


def parse_amount(env, value, allow_zero=False):
    """The Argentine way of /cobro («1.500», «1500,50»); zero only when
    allowed (left for tomorrow, «monedas / otros»)."""
    text = str(value if value is not None else "").strip()
    if allow_zero and text.replace("$", "").strip() in ("", "0", "0,00", "0,0"):
        return 0.0
    return env["distri.cash.collection"]._cash_amount(text)


def parse_quantity(value):
    text = re.sub(r"\s", "", str(value if value is not None else ""))
    if not text:
        return 0
    if not text.isdigit():
        raise UserError("La cantidad de billetes es un número entero: «%s»." % value)
    quantity = int(text)
    if quantity > MAX_QUANTITY:
        raise UserError("Revisá la cantidad de billetes: %s es demasiado." % quantity)
    return quantity


class DistriCajaDenominacion(models.Model):
    _name = "distri.caja.denominacion"
    _description = "Billete para contar la caja"
    _order = "value desc"
    _rec_name = "display_label"

    value = fields.Integer(string="Valor", required=True)
    active = fields.Boolean(default=True)
    display_label = fields.Char(string="Billete", compute="_compute_display_label")

    _value_unique = models.Constraint("unique(value)", "Ese billete ya está en la lista.")

    @api.depends("value")
    def _compute_display_label(self):
        for denomination in self:
            denomination.display_label = _money(denomination.value)

    @api.constrains("value")
    def _check_value(self):
        if any(d.value <= 0 for d in self):
            raise ValidationError("El valor del billete tiene que ser mayor que cero.")


class DistriCaja(models.Model):
    _name = "distri.caja"
    _description = "Caja del día"
    _inherit = ["mail.thread"]
    _order = "date desc, id desc"

    name = fields.Char(string="Caja", compute="_compute_name", store=True)
    user_id = fields.Many2one("res.users", string="Cajero", required=True, readonly=True,
                              index=True, default=lambda self: self.env.user)
    date = fields.Date(string="Fecha", required=True, readonly=True,
                       default=lambda self: today_ar())
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    journal_id = fields.Many2one("account.journal", string="Diario", required=True,
                                 readonly=True, domain=[("type", "=", "cash")])
    state = fields.Selection([("open", "Abierta"), ("closed", "Cerrada")], string="Estado",
                             default="open", required=True, readonly=True, tracking=True)
    opened_at = fields.Datetime(string="Abierta el", readonly=True,
                                default=fields.Datetime.now)
    closed_at = fields.Datetime(string="Cerrada el", readonly=True, copy=False)
    closed_by_id = fields.Many2one("res.users", string="La cerró", readonly=True, copy=False)

    previous_id = fields.Many2one("distri.caja", string="Caja anterior", readonly=True)
    proposed_float = fields.Monetary(string="Fondo propuesto", readonly=True,
                                     help="Lo que quedó para hoy en el cierre anterior.")
    opening_float = fields.Monetary(string="Fondo inicial", readonly=True, tracking=True)
    opening_note = fields.Char(string="Motivo del fondo distinto", readonly=True)

    movement_ids = fields.One2many("distri.caja.movimiento", "caja_id", string="Movimientos",
                                   readonly=True)
    count_ids = fields.One2many("distri.caja.conteo", "caja_id", string="Conteo de billetes",
                                readonly=True)
    other_amount = fields.Monetary(string="Monedas / otros", readonly=True)

    total_cobros = fields.Monetary(string="Cobros en el local", compute="_compute_totals",
                                   store=True)
    total_rendiciones = fields.Monetary(string="Rendiciones y recepciones",
                                        compute="_compute_totals", store=True)
    total_salidas = fields.Monetary(string="Gastos y proveedores", compute="_compute_totals",
                                    store=True)
    total_retiros = fields.Monetary(string="Retiros y depósitos", compute="_compute_totals",
                                    store=True)
    total_ajustes = fields.Monetary(string="Ajustes", compute="_compute_totals", store=True)
    expected = fields.Monetary(string="Tiene que haber", compute="_compute_totals", store=True)

    counted = fields.Monetary(string="Contado", compute="_compute_counted", store=True)
    difference = fields.Monetary(string="Diferencia", compute="_compute_counted", store=True,
                                 help="Contado menos lo que tiene que haber. Negativo: falta.")
    reason = fields.Text(string="Motivo de la diferencia", readonly=True, tracking=True)
    left_for_tomorrow = fields.Monetary(string="Queda para mañana", readonly=True,
                                        tracking=True)
    withdrawn = fields.Monetary(string="Se retira", readonly=True, tracking=True)

    statement_id = fields.Many2one("account.bank.statement", string="Extracto de caja",
                                   readonly=True, copy=False)
    case_id = fields.Many2one("distri.route.case", string="Caso de la diferencia",
                              readonly=True, copy=False)
    reopen_count = fields.Integer(string="Veces reabierta", readonly=True, copy=False)

    _user_date_unique = models.Constraint(
        "unique(user_id, date, company_id)", "Ese cajero ya tiene la caja de ese día.")
    _one_open = models.UniqueIndex(
        "(user_id, company_id) WHERE state = 'open'",
        "Ese cajero ya tiene una caja abierta: cerrala antes de abrir otra.")

    @api.depends("user_id.name", "date")
    def _compute_name(self):
        for caja in self:
            caja.name = "Caja · %s · %s" % (
                caja.date.strftime("%d/%m/%Y") if caja.date else "", caja.user_id.name or "")

    @api.depends("opening_float", "movement_ids.amount", "movement_ids.kind")
    def _compute_totals(self):
        for caja in self:
            by_kind = {}
            for move in caja.movement_ids:
                by_kind[move.kind] = by_kind.get(move.kind, 0.0) + move.amount
            caja.total_cobros = by_kind.get("cobro", 0.0)
            caja.total_rendiciones = by_kind.get("rendicion", 0.0) + by_kind.get("recepcion", 0.0)
            caja.total_salidas = by_kind.get("gasto", 0.0) + by_kind.get("proveedor", 0.0)
            caja.total_retiros = by_kind.get("retiro", 0.0) + by_kind.get("banco", 0.0)
            caja.total_ajustes = by_kind.get("ajuste", 0.0)
            caja.expected = caja.opening_float + sum(by_kind.values())

    @api.depends("count_ids.subtotal", "other_amount", "expected", "state")
    def _compute_counted(self):
        for caja in self:
            caja.counted = sum(caja.count_ids.mapped("subtotal")) + caja.other_amount
            caja.difference = (caja.counted - caja.expected) if caja.state == "closed" else 0.0

    # ------------------------------------------------------------ access

    @api.model
    def _check_cashier(self):
        if not is_cashier(self.env.user):
            raise AccessError("Solo el cajero maneja la caja del día.")
        return self.env.user

    def _check_mine_open(self):
        """The session is the user's own and open (gerencia may act on
        anyone's)."""
        self.ensure_one()
        user = self._check_cashier()
        caja = self.sudo()
        if caja.user_id != user and not can_reopen(user):
            raise AccessError("Esa caja es de otro cajero.")
        if caja.state != "open":
            raise UserError("La caja de %s ya se cerró." % caja.date.strftime("%d/%m"))
        caja._sync_own_collections()
        return caja

    # ------------------------------------------------------------ opening

    @api.model
    def _session_journal(self, company=None):
        from .caja_contabilidad import session_journal
        return session_journal(self.env, company or self.env.company)

    @api.model
    def current_for(self, user=None):
        """The open session of the user (of any day), or an empty recordset."""
        user = user or self.env.user
        return self.sudo().search([("user_id", "=", user.id), ("state", "=", "open"),
                                   ("company_id", "=", self.env.company.id)], limit=1)

    @api.model
    def last_closed(self, user=None):
        user = user or self.env.user
        return self.sudo().search([("user_id", "=", user.id), ("state", "=", "closed"),
                                   ("company_id", "=", self.env.company.id)],
                                  order="date desc, id desc", limit=1)

    @api.model
    def open_session(self, opening_float, note=""):
        """«Abrir la caja» of today for the current cashier."""
        user = self._check_cashier()
        current = self.current_for(user)
        if current:
            raise UserError("Tenés abierta la caja del %s: cerrala antes de abrir la de hoy."
                            % current.date.strftime("%d/%m"))
        today = today_ar()
        if self.sudo().search_count([("user_id", "=", user.id), ("date", "=", today),
                                     ("company_id", "=", self.env.company.id)]):
            raise UserError("La caja de hoy ya se cerró. Si hace falta, gerencia la reabre.")
        previous = self.last_closed(user)
        proposed = previous.left_for_tomorrow if previous else 0.0
        amount = parse_amount(self.env, opening_float, allow_zero=True)
        note = " ".join((note or "").split())[:NOTE_MAX]
        currency = self.env.company.currency_id
        if previous and currency.compare_amounts(amount, proposed) and not note:
            raise CajaQuestion(
                "En el cierre anterior quedaron %s para hoy y pusiste %s. Escribí el motivo "
                "de la diferencia." % (_money(proposed), _money(amount)), "motivo")
        caja = self.sudo().create({
            "user_id": user.id,
            "date": today,
            "company_id": self.env.company.id,
            "journal_id": self._session_journal().id,
            "previous_id": previous.id or False,
            "proposed_float": proposed,
            "opening_float": amount,
            "opening_note": note or False,
        })
        caja.message_post(body="Caja abierta por %s con un fondo de %s%s." % (
            user.name, _money(amount), " (motivo: %s)" % note if note else ""))
        caja._sync_own_collections()
        return caja

    # ------------------------------------------------------------ inflows

    def _own_boxes(self):
        """The cashier's own «Caja depósito» boxes still open (any day)."""
        self.ensure_one()
        return self.env["distri.route"].sudo().search([
            ("kind", "=", "depot"), ("driver_id", "=", self.user_id.id),
            ("state", "!=", "settled"), ("company_id", "=", self.company_id.id)])

    def _sync_own_collections(self):
        """Every cash payment of the cashier's own boxes becomes a «Cobro en
        el local» of the open session, once (from /cobro, the depot delivery
        or the counter sale)."""
        Movement = self.env["distri.caja.movimiento"].sudo()
        for caja in self.sudo().filtered(lambda c: c.state == "open"):
            boxes = caja._own_boxes()
            payments = boxes.payment_ids.filtered(lambda p: p.state in ("in_process", "paid"))
            if not payments:
                continue
            taken = Movement.search([("payment_id", "in", payments.ids)]).payment_id
            for box in boxes:
                for payment in box.payment_ids.filtered(
                        lambda p: p in payments and p not in taken).sorted("id"):
                    Movement.create({
                        "caja_id": caja.id,
                        "kind": "cobro",
                        "amount": payment.amount,
                        "partner_id": payment.partner_id.id,
                        "payment_id": payment.id,
                        "box_id": box.id,
                        "moment": payment.create_date,
                        "note": payment.memo or False,
                    })
        return True

    @api.model
    def pending_receptions(self):
        """What the cashier can receive: driver routes closed on the phone
        and the open boxes of the other users, oldest first."""
        user = self.env.user
        Route = self.env["distri.route"].sudo()
        routes = Route.search([("kind", "=", "route"), ("state", "=", "closed"),
                               ("company_id", "=", self.env.company.id)], order="date, id")
        boxes = Route.search([("kind", "=", "depot"), ("state", "!=", "settled"),
                              ("driver_id", "!=", user.id),
                              ("company_id", "=", self.env.company.id)], order="date, id")
        return routes, boxes

    def receive_route(self, route, counted, papers=(), no_paper_ok=False, difference_mode="",
                      unloaded=(), returns=None, note=""):
        """«Recibir y pasar a la caja»: a driver's route (settled here) or
        another user's box (closed here). Raises CajaQuestion when something
        must be answered first: expenses without paper, or a difference."""
        caja = self._check_mine_open()
        route = route.sudo().exists()
        if not route:
            raise UserError("Esa rendición ya no existe.")
        if route.kind == "route" and route.state != "closed":
            raise UserError("La ruta %s todavía no la cerró el repartidor en el celular."
                            % route.name)
        if route.kind == "depot" and (route.state == "settled" or route.driver_id == caja.user_id):
            raise UserError("Esa caja no se puede recibir.")
        amount = parse_amount(self.env, counted, allow_zero=True)
        note = " ".join((note or "").split())[:NOTE_MAX]
        papers = {int(x) for x in papers or ()}
        unloaded = {int(x) for x in unloaded or ()}
        returns = {int(k): v for k, v in (returns or {}).items()}
        # the questions first, before anything is written
        open_expenses = route.expense_ids.filtered(lambda e: e.state == "reported") \
            if route.kind == "route" else route.expense_ids.browse()
        without = open_expenses.filtered(lambda e: e.id not in papers)
        if without and not no_paper_ok:
            raise CajaQuestion(
                "%s sin el papel (%s): si seguís, %s y queda a cargo de %s. ¿Seguro?" % (
                    "1 gasto" if len(without) == 1 else "%s gastos" % len(without),
                    _money(sum(without.mapped("amount"))),
                    "se rechaza" if len(without) == 1 else "se rechazan",
                    route.driver_id.name), "sin_papel_ok")
        difference = amount - route.cash_expected
        if route.currency_id.is_zero(difference):
            difference, difference_mode = 0.0, ""
        elif difference_mode not in dict(DIFFERENCE_MODES):
            raise CajaQuestion(
                "%s %s. ¿Queda a cargo de %s o lo trae mañana?" % (
                    "Faltan" if difference < 0 else "Sobran", _money(abs(difference)),
                    route.driver_id.name), "diferencia")
        if route.kind == "route":
            for stop in route.undelivered_stop_ids.filtered(lambda s: not s.unloaded):
                if stop.id in unloaded:
                    stop.unloaded = True
            if route.undelivered_stop_ids.filtered(lambda s: not s.unloaded):
                raise UserError("Tildá «Volvió al depósito» en cada pedido sin entregar.")
            for ret in route.return_ids.filtered(lambda r: r.settlement_state == "pending"):
                answer = returns.get(ret.id)
                if answer == "received":
                    ret.action_received()
                elif answer == "missing":
                    ret.action_missing()
            if route.return_ids.filtered(lambda r: r.settlement_state == "pending"):
                raise UserError("Marcá cada devolución: volvió al depósito o no volvió.")
            for expense in open_expenses:
                expense.paper_received = expense.id in papers
            if route.journal_id != caja.journal_id:
                route.journal_id = caja.journal_id
        route.cash_counted = amount
        if route.kind == "route":
            route.with_context(distri_caja_defer_post=True).action_settle()
        else:
            route.action_close_depot()
        case = route._cash_case()
        if case and difference_mode:
            text = dict(DIFFERENCE_MODES)[difference_mode]
            case.write({"driver_explanation": "\n".join(
                x for x in (case.driver_explanation, "%s (recibió %s en %s)%s" % (
                    text, caja.user_id.name, caja.name, ": %s" % note if note else "")) if x)})
            case.message_post(body="Al recibir la rendición: %s." % text)
        movement = self.env["distri.caja.movimiento"].sudo().create({
            "caja_id": caja.id,
            "kind": "rendicion" if route.kind == "route" else "recepcion",
            "amount": amount,
            "route_id": route.id,
            "route_difference": difference,
            "difference_mode": difference_mode or False,
            "who": route.driver_id.name,
            "note": note or False,
            "case_id": case.id or False,
        })
        route.message_post(body=Markup("Recibida en la caja de %s: %s%s.") % (
            caja.user_id.name, _money(amount),
            " (%s %s, %s)" % ("faltan" if difference < 0 else "sobran", _money(abs(difference)),
                              dict(DIFFERENCE_MODES)[difference_mode]) if difference_mode else ""))
        return movement

    @api.model
    def pending_shortages(self):
        """Shortages of a received settlement that the driver said he brings
        («Lo trae mañana») and nobody brought yet."""
        movements = self.env["distri.caja.movimiento"].sudo().search([
            ("difference_mode", "=", "manana"), ("route_difference", "<", 0),
            ("caja_id.company_id", "=", self.env.company.id)])
        return movements.filtered(lambda m: m.case_id and m.case_id.state == "open"
                                  and not m.case_id.replenished)

    def bring_shortage(self, movement):
        """«Lo trajo»: the driver's shortage enters as an «ajuste»."""
        caja = self._check_mine_open()
        movement = movement.sudo().exists()
        if movement not in self.pending_shortages():
            raise UserError("Ese faltante ya no está pendiente.")
        amount = -movement.route_difference
        result = self.env["distri.caja.movimiento"].sudo().create({
            "caja_id": caja.id,
            "kind": "ajuste",
            "amount": amount,
            "who": movement.who,
            "note": "Trajo el faltante de %s" % (movement.route_id.name or ""),
            "case_id": movement.case_id.id,
        })
        movement.case_id.write({"replenished": True})
        movement.case_id.message_post(body="Lo trajo: %s, en %s." % (_money(amount), caja.name))
        return result

    # ------------------------------------------------------------ outflows

    def add_outflow(self, chip, amount, who="", partner_id=False, note="", photo=False):
        """«Salida de caja»: a supplier, an expense, a withdrawal or a bank
        deposit. The amount is positive on the screen and negative here."""
        caja = self._check_mine_open()
        if chip not in CHIPS:
            raise UserError("Elegí el tipo de salida.")
        _label, kind, concept = CHIPS[chip]
        value = parse_amount(self.env, amount)
        who = " ".join((who or "").split())[:120]
        note = " ".join((note or "").split())[:NOTE_MAX]
        partner = self.env["res.partner"]
        if partner_id:
            try:
                partner = self.env["res.partner"].sudo().browse(int(partner_id)).exists()
            except (TypeError, ValueError):
                partner = self.env["res.partner"]
        if kind == "proveedor" and not (partner or who):
            raise UserError("Escribí a qué proveedor le pagaste.")
        if kind == "gasto" and not (who or note):
            raise UserError("Escribí a quién le pagaste o qué se compró.")
        if self.currency_id.compare_amounts(value, caja.expected) > 0:
            raise UserError("No alcanza: en la caja tiene que haber %s y querés sacar %s."
                            % (_money(caja.expected), _money(value)))
        return self.env["distri.caja.movimiento"].sudo().create({
            "caja_id": caja.id,
            "kind": kind,
            "concept": concept or False,
            "amount": -value,
            "partner_id": partner.id or False,
            "who": who or partner.name or False,
            "note": note or False,
            "photo": photo or False,
        })

    # ------------------------------------------------------------ closing

    def close_session(self, quantities, other="0", left="0", reason="", expected_seen=None):
        """«Cerrar e imprimir»: the bill count, what stays for tomorrow, the
        reason of a difference; then the accounting and the case."""
        caja = self._check_mine_open()
        caja._sync_own_collections()
        currency = caja.currency_id
        if expected_seen not in (None, ""):
            try:
                seen = float(expected_seen)
            except (TypeError, ValueError):
                seen = None
            if seen is not None and currency.compare_amounts(seen, caja.expected):
                raise UserError("Entró un movimiento mientras contabas: ahora tiene que haber %s. "
                                "Revisá el conteo." % _money(caja.expected))
        Denomination = self.env["distri.caja.denominacion"].sudo().with_context(active_test=False)
        rows = []
        for key, value in (quantities or {}).items():
            quantity = parse_quantity(value)
            if not quantity:
                continue
            denomination = Denomination.browse(int(key)).exists()
            if not denomination:
                raise UserError("Ese billete no existe.")
            rows.append((denomination, quantity))
        other_amount = parse_amount(self.env, other, allow_zero=True)
        counted = sum(d.value * q for d, q in rows) + other_amount
        left_amount = parse_amount(self.env, left, allow_zero=True)
        if currency.compare_amounts(left_amount, counted) > 0:
            raise UserError("No puede quedar para mañana más de lo contado (%s)." % _money(counted))
        difference = counted - caja.expected
        reason = (reason or "").strip()[:1000]
        if not currency.is_zero(difference) and not reason:
            raise CajaQuestion("%s %s. Escribí el motivo de la diferencia." % (
                "Faltan" if difference < 0 else "Sobran", _money(abs(difference))), "motivo")
        # the cashier's own boxes close with the session: their cash is here
        for box in caja._own_boxes():
            box.cash_counted = box.cash_expected
            box.action_close_depot()
            box.message_post(body="Cerrada con %s." % caja.name)
        caja.count_ids.unlink()
        caja.write({
            "count_ids": [(0, 0, {"denomination_id": d.id, "value": d.value, "quantity": q})
                          for d, q in rows],
            "other_amount": other_amount,
            "left_for_tomorrow": left_amount,
            "withdrawn": counted - left_amount,
            "reason": reason or False,
            "state": "closed",
            "closed_at": fields.Datetime.now(),
            "closed_by_id": self.env.user.id,
        })
        caja._post_closing()
        if not currency.is_zero(caja.difference):
            caja._open_difference_case()
        caja.message_post(body="Caja cerrada por %s: tenía que haber %s, se contó %s%s. Queda "
                               "para mañana %s, se retira %s." % (
            self.env.user.name, _money(caja.expected), _money(caja.counted),
            " (diferencia %s)" % _money(caja.difference) if caja.difference else "",
            _money(caja.left_for_tomorrow), _money(caja.withdrawn)))
        return caja

    def _open_difference_case(self):
        self.ensure_one()
        if self.case_id and self.case_id.state == "open":
            self.case_id.write({"amount": self.difference})
            return self.case_id
        case = self.env["distri.route.case"].sudo().create({
            "caja_id": self.id,
            "kind": "cash",
            "amount": self.difference,
            "candidates": "%s al cerrar la caja: tenía que haber %s y se contó %s." % (
                "Faltante" if self.difference < 0 else "Sobrante",
                _money(self.expected), _money(self.counted)),
            "driver_explanation": self.reason or False,
        })
        self.case_id = case
        return case

    def action_reopen(self, reason):
        """Gerencia reopens a closed session (the latest of that cashier):
        the closing's statement is undone and the case closed."""
        self.ensure_one()
        user = self.env.user
        if not can_reopen(user):
            raise AccessError("Solo gerencia reabre una caja cerrada.")
        reason = " ".join((reason or "").split())
        if not reason:
            raise UserError("Escribí el motivo para reabrir la caja.")
        caja = self.sudo()
        if caja.state != "closed":
            raise UserError("La caja está abierta.")
        later = caja.search([("user_id", "=", caja.user_id.id), ("id", "!=", caja.id),
                             ("company_id", "=", caja.company_id.id),
                             "|", ("date", ">", caja.date), ("state", "=", "open")], limit=1)
        if later:
            raise UserError("%s ya abrió %s: solo se reabre la última caja." % (
                caja.user_id.name, later.name))
        caja._undo_closing()
        if caja.case_id.state == "open":
            caja.case_id.write({"state": "closed", "resolution": "Caja reabierta por %s: %s" % (
                user.name, reason)})
        caja.write({"state": "open", "closed_at": False, "closed_by_id": False,
                    "case_id": False, "reopen_count": caja.reopen_count + 1})
        caja.message_post(body="Caja reabierta por %s. Motivo: %s" % (user.name, reason))
        return True

    # ------------------------------------------------------------ label

    def _label_values(self):
        """The closing label: date, cashier, the bill count and the total."""
        self.ensure_one()
        rows = [{"value": _money(c.value).replace("$ ", ""), "quantity": c.quantity,
                 "subtotal": _money(c.subtotal).replace("$ ", "")}
                for c in self.count_ids.filtered("quantity").sorted("value", reverse=True)]
        return {
            "date": self.date.strftime("%d/%m/%Y"),
            "cashier": self.user_id.name or "",
            "rows": rows,
            "other": _money(self.other_amount).replace("$ ", "") if self.other_amount else "",
            "total": _money(self.counted),
        }

    def action_open_reopen(self):
        self.ensure_one()
        if not can_reopen(self.env.user):
            raise AccessError("Solo gerencia reabre una caja cerrada.")
        return {
            "type": "ir.actions.act_window",
            "name": "Reabrir la caja",
            "res_model": "distri.caja.reabrir",
            "view_mode": "form",
            "target": "new",
            "context": {"default_caja_id": self.id},
        }

    def action_print_label(self):
        return self.env.ref("distri_pedido.report_caja_cierre").report_action(self, config=False)


class DistriCajaMovimiento(models.Model):
    _name = "distri.caja.movimiento"
    _description = "Movimiento de la caja del día"
    _order = "moment desc, id desc"
    _rec_name = "label"

    caja_id = fields.Many2one("distri.caja", string="Caja", required=True, ondelete="cascade",
                              index=True, readonly=True)
    company_id = fields.Many2one(related="caja_id.company_id", store=True)
    currency_id = fields.Many2one(related="caja_id.currency_id")
    moment = fields.Datetime(string="Hora", required=True, readonly=True,
                             default=fields.Datetime.now)
    kind = fields.Selection(MOVEMENT_KINDS, string="Tipo", required=True, readonly=True)
    concept = fields.Selection(EXPENSE_CONCEPTS, string="Concepto", readonly=True)
    amount = fields.Monetary(string="Importe", required=True, readonly=True,
                             help="Positivo: entra a la caja. Negativo: sale.")
    partner_id = fields.Many2one("res.partner", string="Cliente o proveedor", readonly=True)
    who = fields.Char(string="A quién / de quién", readonly=True)
    note = fields.Char(string="Nota", readonly=True)
    photo = fields.Image(string="Comprobante", max_width=2000, max_height=2000,
                         attachment=True, readonly=True)
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True,
                                 ondelete="restrict")
    box_id = fields.Many2one("distri.route", string="Caja depósito", readonly=True)
    route_id = fields.Many2one("distri.route", string="Rendición recibida", readonly=True,
                               ondelete="restrict")
    route_difference = fields.Monetary(string="Diferencia de la rendición", readonly=True)
    difference_mode = fields.Selection(DIFFERENCE_MODES, string="La diferencia", readonly=True)
    case_id = fields.Many2one("distri.route.case", string="Caso", readonly=True)
    user_id = fields.Many2one("res.users", string="Cargó", readonly=True,
                              default=lambda self: self.env.user)
    statement_line_ids = fields.Many2many("account.bank.statement.line",
                                          string="Líneas del extracto", readonly=True)
    label = fields.Char(string="Detalle", compute="_compute_label")

    _payment_unique = models.UniqueIndex("(payment_id) WHERE payment_id IS NOT NULL",
                                         "Ese cobro ya está en una caja.")
    _route_unique = models.UniqueIndex("(route_id) WHERE route_id IS NOT NULL",
                                       "Esa rendición ya se recibió.")

    @api.depends("kind", "concept", "who", "partner_id.name", "route_id.name", "note")
    def _compute_label(self):
        kinds = dict(MOVEMENT_KINDS)
        concepts = dict(EXPENSE_CONCEPTS)
        for move in self:
            title = concepts.get(move.concept) if move.kind == "gasto" and move.concept \
                else kinds.get(move.kind, "")
            if move.kind == "retiro":
                title = "Retiro del dueño"
            if move.kind == "rendicion":
                detail = move.route_id.name or move.who
            elif move.kind == "recepcion":
                detail = "de %s" % (move.who or move.route_id.driver_id.name or "")
            else:
                detail = move.partner_id.name or move.who or move.note or ""
            move.label = " · ".join(x for x in (title, detail) if x)


class DistriCajaConteo(models.Model):
    _name = "distri.caja.conteo"
    _description = "Conteo de billetes de la caja"
    _order = "value desc"

    caja_id = fields.Many2one("distri.caja", string="Caja", required=True, ondelete="cascade",
                              index=True)
    currency_id = fields.Many2one(related="caja_id.currency_id")
    denomination_id = fields.Many2one("distri.caja.denominacion", string="Billete",
                                      required=True, ondelete="restrict")
    value = fields.Integer(string="Valor", required=True,
                           help="El valor del billete al contar (no cambia si después se "
                                "edita la lista).")
    quantity = fields.Integer(string="Cantidad", required=True, default=0)
    subtotal = fields.Monetary(string="Total", compute="_compute_subtotal", store=True)

    @api.depends("value", "quantity")
    def _compute_subtotal(self):
        for row in self:
            row.subtotal = row.value * row.quantity

    @api.constrains("quantity")
    def _check_quantity(self):
        if any(r.quantity < 0 for r in self):
            raise ValidationError("La cantidad no puede ser negativa.")


class DistriCajaReabrir(models.TransientModel):
    _name = "distri.caja.reabrir"
    _description = "Reabrir una caja cerrada"

    caja_id = fields.Many2one("distri.caja", required=True)
    reason = fields.Text(string="Motivo", required=True)

    def action_confirm(self):
        self.ensure_one()
        self.caja_id.action_reopen(self.reason)
        return {"type": "ir.actions.act_window_close"}


class ReportCajaCierre(models.AbstractModel):
    _name = "report.distri_pedido.caja_cierre_documento"
    _description = "Etiqueta del cierre de caja"

    @api.model
    def _get_report_values(self, docids, data=None):
        cajas = self.env["distri.caja"].sudo().browse(docids)
        return {"doc_ids": docids, "doc_model": "distri.caja", "docs": cajas,
                "labels": [c._label_values() for c in cajas]}
