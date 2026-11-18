"""«Cobro en efectivo» (19.0.1.92.0, controllers/cobro.py, page /cobro).

Any internal user who receives cash from a customer, anywhere (a driver off
his route, a seller, the depot), registers it on the spot: against one open
invoice or «a cuenta». It is posted at once like the depot cash
(models/deposito.py): a customer payment in «Caja depósito» (CDEP) on the
user's cash box of the day (distri.route kind «depot», _depot_route_for), so
«Cierre de caja depósito» counts it and closes it with the rest of that
user's cash. Each registration is a `distri.cash.collection` row on that box;
the box's expected cash adds them (models/reparto.py _compute_totals).

Checks, all before any sudo: an internal user; a real commercial customer
(not a generic contact; a seller without Administración never reaches a
distributor's account, models/lista_oculta.py); an amount above zero; the
invoice, when one is chosen, posted, open and of that customer, and the
amount not above what it still owes. The same user, customer, amount and
invoice again within DUPLICATE_MINUTES is refused unless confirmed (a double
tap or a page sent twice). An amount above the customer's debt (the
statement balance, models/estado_cuenta.py) is asked first (19.0.1.99.0,
audit F4): the difference stays in his favor.
"""
import re
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .lista_oculta import solo_vendedor
from .reparto import _money
from .reparto_cliente import today_ar

THOUSANDS_RE = re.compile(r"\d{1,3}(\.\d{3})+")
# «1500.5»: a decimal dot after four digits or more is not a thousands dot
DOT_DECIMAL_RE = re.compile(r"\d{4,}\.\d{1,2}")
AMBIGUOUS = ("No se entiende el importe «%s»: escribí 1500 (o 1.500) para mil quinientos, y "
             "usá la coma para los centavos (1500,50).")
# open route states where a stop may still be collected by the driver
OPEN_ROUTE_STATES = ("draft", "on_route")
DUPLICATE_MINUTES = 5
MAX_AMOUNT = 100000000.0
NOTE_MAX_LENGTH = 200
SEARCH_LIMIT = 20
OPEN_INVOICES_LIMIT = 30
RECENT_ROWS = 10
NOT_A_CUSTOMER = "Ese cliente no existe o no se puede cobrar desde acá."


class DuplicateCashError(UserError):
    """The same cash was registered a moment ago: the page asks first."""


class OverDebtWarning(UserError):
    """19.0.1.99.0 (audit F4): the amount is more than the customer owes; the
    page asks first («deuda_ok»)."""
    field = "deuda_ok"


class RouteCashWarning(UserError):
    """The order is on a route (19.0.1.93.0): the page asks first. `field` is
    the form flag that confirms it («ruta_ok», or «forzar» for
    administración over a driver's collection)."""

    def __init__(self, message, field="ruta_ok"):
        super().__init__(message)
        self.field = field


class DistriCashCollection(models.Model):
    _name = "distri.cash.collection"
    _description = "Cobro en efectivo"
    _order = "create_date desc, id desc"
    _rec_name = "memo"

    route_id = fields.Many2one("distri.route", string="Caja", required=True, readonly=True,
                               index=True, ondelete="restrict")
    user_id = fields.Many2one("res.users", string="Cobró", required=True, readonly=True,
                              index=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, readonly=True,
                                 index=True)
    invoice_id = fields.Many2one("account.move", string="Factura", readonly=True)
    company_id = fields.Many2one(related="route_id.company_id", store=True)
    currency_id = fields.Many2one(related="route_id.currency_id")
    amount = fields.Monetary(string="Importe", required=True, readonly=True)
    note = fields.Char(string="Nota", readonly=True)
    memo = fields.Char(string="Detalle", readonly=True)
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True)

    # ------------------------------------------------------------ checks

    @api.model
    def _cash_check_user(self):
        user = self.env.user
        if user._is_public() or user.share:
            raise AccessError("El cobro en efectivo es solo para el personal.")
        return user

    @api.model
    def _cash_excluded_partner_ids(self):
        """Never a customer (19.0.1.93.0, C2): the companies, the partners of
        internal users (archived ones too) and the public user."""
        Users = self.env["res.users"].sudo().with_context(active_test=False)
        partners = (self.env["res.company"].sudo().search([]).partner_id
                    | Users.search([("share", "=", False)]).partner_id)
        public = self.env.ref("base.public_user", raise_if_not_found=False)
        if public:
            partners |= public.sudo().partner_id
        return partners.ids

    @api.model
    def _cash_partner_domain(self):
        domain = [("parent_id", "=", False), ("distri_generic_partner", "=", False),
                  ("id", "not in", self._cash_excluded_partner_ids())]
        if solo_vendedor(self.env.user):
            domain.append(("distri_lista_oculta", "=", False))
        return domain

    @api.model
    def _cash_partner(self, partner_id):
        """The commercial customer the user may collect from, with sudo."""
        self._cash_check_user()
        try:
            wanted = int(partner_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        Partner = self.env["res.partner"].sudo()
        partner = Partner.search([("id", "=", wanted)] + self._cash_partner_domain(),
                                 limit=1) if wanted > 0 else Partner
        if not partner:
            raise UserError(NOT_A_CUSTOMER)
        return partner

    @api.model
    def _cash_amount(self, amount):
        """«1.500», «1500,50», «1.500,50» or «1500.5» -> float (Argentine
        style first: a dot followed by three digits groups thousands). «1.5»
        is refused as ambiguous (19.0.1.93.0, C12)."""
        typed = str(amount if amount is not None else "").strip()
        raw = typed.replace("$", "").replace(" ", "")
        if "," in raw:
            whole = raw.split(",", 1)[0]
            if "." in whole and not THOUSANDS_RE.fullmatch(whole):
                raise UserError(AMBIGUOUS % typed)
            raw = raw.replace(".", "").replace(",", ".")
        elif THOUSANDS_RE.fullmatch(raw):
            raw = raw.replace(".", "")
        elif "." in raw and not DOT_DECIMAL_RE.fullmatch(raw):
            raise UserError(AMBIGUOUS % typed)
        try:
            value = round(float(raw), 2)
        except (TypeError, ValueError):
            value = 0.0
        if not 0 < value <= MAX_AMOUNT:
            raise UserError("Poné un importe mayor que cero.")
        return value

    @api.model
    def _cash_invoice(self, partner, invoice_id, amount):
        if not invoice_id:
            return self.env["account.move"]
        try:
            wanted = int(invoice_id)
        except (TypeError, ValueError):
            wanted = 0
        invoice = self.env["account.move"].sudo().browse(wanted).exists() if wanted > 0 \
            else self.env["account.move"]
        if not invoice or invoice.move_type != "out_invoice" or invoice.state != "posted" \
                or invoice.commercial_partner_id != partner:
            raise UserError("Esa factura no es de este cliente.")
        if invoice.amount_residual <= 0.004:
            raise UserError("Esa factura ya está pagada.")
        if amount > invoice.amount_residual + 0.004:
            raise UserError("El importe supera lo que falta pagar de la factura (%s). Cobrá ese "
                            "saldo o registralo «a cuenta»." % _money(invoice.amount_residual))
        return invoice

    @api.model
    def _cash_duplicate(self, user, partner, amount, invoice):
        since = fields.Datetime.now() - timedelta(minutes=DUPLICATE_MINUTES)
        return self.sudo().search([
            ("user_id", "=", user.id), ("partner_id", "=", partner.id),
            ("amount", "=", amount), ("invoice_id", "=", invoice.id or False),
            ("create_date", ">=", since)], limit=1)

    # ------------------------------------------------------------ routes (C1)

    @api.model
    def _cash_route_stops(self, partner, invoice=None):
        """Stops of the customer's orders (only the invoice's orders when one
        is chosen) on delivery routes not settled yet."""
        Stop = self.env["distri.route.stop"].sudo()
        if invoice:
            orders = invoice.sudo().invoice_line_ids.sale_line_ids.order_id
            domain = [("order_id", "in", orders.ids)]
        else:
            domain = [("partner_id", "child_of", partner.id)]
        return Stop.search(domain + [("route_id.kind", "=", "route"),
                                     ("route_id.state", "!=", "settled")])

    @api.model
    def _cash_route_day(self, route):
        return "de hoy" if route.date == today_ar() else "del %s" % route.date.strftime("%d/%m")

    @api.model
    def _cash_stop_collected(self, stop):
        """What the driver already took for the stop (visit synced)."""
        return stop.cash_amount + (stop.transfer_amount if stop.transfer == "yes" else 0.0)

    @api.model
    def _cash_route_note(self, stop):
        """The line /cobro shows for an order on a route."""
        route = stop.route_id
        driver = route.driver_id.name or "el repartidor"
        if stop.state == "pending":
            return "Este pedido está en la ruta de %s %s: si ya le pagó al repartidor, no lo " \
                   "cobres de nuevo." % (driver, self._cash_route_day(route))
        collected = self._cash_stop_collected(stop)
        if collected > 0:
            return "%s ya cobró %s por el pedido %s en la ruta %s." % (
                driver, _money(collected), stop.order_id.name, self._cash_route_day(route))
        return ""

    @api.model
    def _cash_check_routes(self, partner, invoice, route_ok=False, force=False):
        """C1: ask before collecting an order the driver may still collect,
        refuse one he already collected (unless administración forces it)."""
        stops = self._cash_route_stops(partner, invoice)
        collected = stops.filtered(lambda s: s.state != "pending"
                                   and self._cash_stop_collected(s) > 0)
        if invoice and collected:
            notes = " ".join(self._cash_route_note(s) for s in collected)
            if not self.env.user.has_group("distri_pedido.grupo_administracion"):
                raise UserError("%s No se puede cobrar de nuevo esta factura: lo rinde el "
                                "repartidor. Si de verdad pagó dos veces, que lo registre "
                                "administración." % notes)
            if not force:
                raise RouteCashWarning("%s ¿Cobrar igual? El cliente va a quedar con saldo a "
                                       "favor si es el mismo pago." % notes, field="forzar")
        pending = stops.filtered(lambda s: s.state == "pending"
                                 and s.route_id.state in OPEN_ROUTE_STATES)
        asked = pending | (collected if not invoice else collected.browse())
        if asked and not route_ok:
            raise RouteCashWarning(" ".join(n for n in map(self._cash_route_note, asked) if n))
        return stops

    # ------------------------------------------------------------ register

    @api.model
    def _cash_memo(self, user, route, invoice, note):
        parts = ["Efectivo", "cobró %s" % user.name, "caja %s" % route.date.strftime("%d/%m"),
                 invoice.name if invoice else "a cuenta"]
        if note:
            parts.append(note)
        return " · ".join(parts)

    @api.model
    def cash_register(self, partner_id, amount, invoice_id=False, note="", confirmed=False,
                      route_ok=False, force=False, over_ok=False):
        """Register and post one cash payment (see the module docstring).
        Raises DuplicateCashError when it looks like a repeat and
        `confirmed` is not set, RouteCashWarning when the order is on a
        route and `route_ok` (or `force`) is not set, OverDebtWarning when
        the amount is above the debt and `over_ok` is not set."""
        user = self._cash_check_user()
        partner = self._cash_partner(partner_id)
        amount = self._cash_amount(amount)
        invoice = self._cash_invoice(partner, invoice_id, amount)
        note = " ".join((note or "").split())[:NOTE_MAX_LENGTH]
        stops = self._cash_check_routes(partner, invoice, route_ok=route_ok, force=force)
        if not over_ok:
            self._cash_check_debt(partner, amount)
        # two pages sent at the same time for one customer: touching his row
        # makes the second wait, fail to serialize and be retried by Odoo,
        # and the retry sees the first one below
        self.env.cr.execute("UPDATE res_partner SET write_date = write_date WHERE id = %s",
                            [partner.id])
        previous =self._cash_duplicate(user, partner, amount, invoice)
        if previous and not confirmed:
            raise DuplicateCashError(
                "Ya registraste %s de %s%s hace un momento (%s). Si es otro cobro, confirmalo." % (
                    _money(amount), partner.name, " por la factura %s" % invoice.name if invoice
                    else " a cuenta", previous.memo))
        # every check passed: the box, the payment and the row with sudo, as
        # the depot delivery does (customer payments are otherwise reserved
        # to grupo_cobros, models/permisos_cobros.py)
        company = invoice.company_id or self.env.company
        route = self.env["distri.route"].sudo()._depot_route_for(user=user, company=company).sudo()
        memo = self._cash_memo(user, route, invoice, note)
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": amount,
            "journal_id": route.journal_id.id,
            "date": today_ar(),
            "memo": memo,
            "company_id": route.company_id.id,
        })
        payment.action_post()
        if invoice:
            self._cash_reconcile(payment, invoice)
        else:
            # C13: like the route's cash, the oldest open invoices first
            route._apply_oldest_first(payment, partner)
        route.payment_ids = [(4, payment.id)]
        collection = self.sudo().create({
            "route_id": route.id, "user_id": user.id, "partner_id": partner.id,
            "invoice_id": invoice.id or False, "amount": amount, "note": note or False,
            "memo": memo, "payment_id": payment.id,
        })
        partner.message_post(body=Markup(
            "Cobro en efectivo de <b>%s</b> (%s), registrado por %s en %s.") % (
            _money(amount), "factura %s" % invoice.name if invoice else "a cuenta", user.name,
            route.name) + (Markup("<br/>Nota: %s") % note if note else ""))
        # C1: the stops still to visit show what is left to collect, and
        # «Ya cobrado en el local» (the app gets it with the route)
        pending = stops.filtered(lambda s: s.state == "pending")
        if pending:
            pending._refresh_amounts()
            for stop in pending:
                stop.route_id.message_post(body=Markup(
                    "%s cobró %s en el local a %s (%s): el pedido %s de esta ruta ya no tiene "
                    "que cobrarse por ese importe.") % (
                    user.name, _money(amount), partner.name,
                    invoice.name if invoice else "a cuenta", stop.order_id.name))
        return collection

    @api.model
    def _cash_check_debt(self, partner, amount):
        """Audit F4: ask before an amount above what the customer owes."""
        debt = max(partner._distri_account_balance(), 0.0)
        if amount > debt + 0.004:
            raise OverDebtWarning(
                "El importe supera la deuda de %s. ¿Registrar igual? La diferencia queda a "
                "favor del cliente (%s)." % (_money(debt), _money(amount - debt)))

    @api.model
    def _cash_reconcile(self, payment, invoice):
        counterpart = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        lines = invoice.line_ids.filtered(
            lambda l: l.account_id in counterpart.account_id and not l.reconciled
            and l.amount_residual > 0)
        if counterpart and lines:
            (counterpart + lines).reconcile()

    # ------------------------------------------------------------ page data

    @api.model
    def cash_search(self, query):
        """Customers by name, reference or phone (at least 2 characters)."""
        self._cash_check_user()
        text = " ".join((query or "").split())
        if len(text) < 2:
            return []
        terms = ["|", "|", "|", ("name", "ilike", text), ("ref", "ilike", text),
                 ("phone", "ilike", text), ("distri_whatsapp", "ilike", text)]
        digits = "".join(ch for ch in text if ch.isdigit())
        if len(digits) >= 6:
            # a phone typed without its dashes and spaces, or with others
            self.env["res.partner"].flush_model(["phone", "distri_whatsapp"])
            self.env.cr.execute("""
                SELECT id FROM res_partner
                 WHERE regexp_replace(coalesce(phone, ''), '[^0-9]', '', 'g') LIKE %s
                    OR regexp_replace(coalesce(distri_whatsapp, ''), '[^0-9]', '', 'g') LIKE %s""",
                                ["%" + digits + "%"] * 2)
            terms = ["|", ("id", "in", [row[0] for row in self.env.cr.fetchall()])] + terms
        partners = self.env["res.partner"].sudo().search(
            self._cash_partner_domain() + terms, order="name", limit=SEARCH_LIMIT)
        return [{"id": p.id, "name": p.name or "", "ref": p.ref or "",
                 "town": p.city or "", "phone": p.distri_whatsapp or p.phone or ""}
                for p in partners]

    @api.model
    def cash_account(self, partner_id):
        """Balance, open invoices with what they still owe and the newest
        movements of the statement (models/estado_cuenta.py)."""
        partner = self._cash_partner(partner_id)
        data = partner._distri_statement()
        invoices = self.env["account.move"].sudo().search([
            ("commercial_partner_id", "=", partner.id), ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"), ("amount_residual", ">", 0.004)],
            order="invoice_date, id", limit=OPEN_INVOICES_LIMIT)
        rows = list(reversed(data["rows"]))[:RECENT_ROWS]
        # C1: what the routes say about each open invoice
        notes = {}
        for stop in self._cash_route_stops(partner):
            note = self._cash_route_note(stop)
            for inv in stop.order_id._distri_facturas_emitidas() if note else ():
                notes.setdefault(inv.id, []).append(note)
        return {
            "partner": partner,
            "balance": data["balance"],
            "invoices": [{"id": inv.id, "name": inv.name or "",
                          "date": inv.invoice_date.strftime("%d/%m/%Y") if inv.invoice_date else "",
                          "remito": inv._distri_remito_texto() or "",
                          "total": inv.amount_total, "residual": inv.amount_residual,
                          "route_note": " ".join(notes.get(inv.id, []))}
                         for inv in invoices],
            "rows": [{"date": r["date"].strftime("%d/%m/%Y") if r["date"] else "",
                      "type": r["doc_type"] or "", "ref": r["number"] or r["remito"] or "",
                      "debit": r["debit"] or 0.0, "credit": r["credit"] or 0.0,
                      "balance": r["balance"]} for r in rows],
        }

    @api.model
    def cash_today(self):
        """The user's cash collections of his open box, newest first."""
        user = self._cash_check_user()
        return self.sudo().search([("user_id", "=", user.id),
                                   ("route_id.state", "!=", "settled")], limit=RECENT_ROWS)
