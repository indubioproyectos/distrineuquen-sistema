"""«Caja v2» (19.0.1.129.27, owner's decisions of 24/11/2026).

Everything here sits behind the ir.config_parameter PARAM_CAJA_V2
(distri_pedido.caja_v2), OFF by default. With it off nothing below runs and
the daily cash behaves exactly as 19.0.1.129.26 (tests/test_v129_27_caja.py
proves it). Production gets the code with the flag off; the practice
environment turns it on (scripts/correcciones/v129_27_activar_caja_v2.sh).

A. Cash shortfalls are the employee's debt
------------------------------------------
Until 129.26 the closing (models/caja_contabilidad.py `_closing_plan`) sent
five kinds of lines to «Diferencias de caja» (5.3.1.01.150). With the flag
on, a shortfall that is a person's goes to «Deudores varios» (1.1.5.01.010,
type asset_receivable, PARAM_DEBT_ACCOUNT) under that EMPLOYEE's own partner:
the partner of his res.users (`employee_partner`), never a customer record
(production has customers named like employees, e.g. «Denis Barros»).

    line of the closing                        flag off          flag on
    -----------------------------------------  ----------------  -------------------------------
    rejected driver expense (no paper)         Diferencias       Deudores varios, driver
    route / box shortfall «a cargo de»         Diferencias       Deudores varios, driver
    route / box shortfall «lo trae mañana»     Diferencias       Deudores varios, driver
    «ajuste» when he brings it (bring_shortage) Diferencias      Deudores varios, same partner:
                                                                 the debt nets to zero
    route / box SURPLUS                        Diferencias       Diferencias (nobody owes it)
    cashier's count shortfall                  Diferencias       Deudores varios, cashier
    cashier's count surplus                    Diferencias       Diferencias
    opening float different from last closing  Diferencias       Diferencias (see below)
    «Adelanto para la ruta» (B)                -                 Deudores varios, driver
    advance coming back at the settlement (B)  -                 Deudores varios, driver (credit)

The decision is stored when it is taken, so a later change of the flag never
rewrites it: `debt_partner_id` on the «Rendición»/«Cobros de otro usuario»
movement (its difference), on the «ajuste» (copied from that movement or
from the case), on the rejected expense's case (set when the settlement is
received) and on the cashier's difference case. The closing reads them.
The opening float difference stays in «Diferencias de caja»: it is the gap
between yesterday's «queda para mañana» and today's opening, usually change
moved or counted again, and nothing says whose it is.

The distri.route.case (kind «cash» or «expense») stays the follow-up: it
names the debt (`debt_partner_id`) and «Deudas de empleados» lists it. The
customers' receivables never move: «Deudores varios» is left out of the
customer balances, the statement and «Deudores» (`debt_account_ids`).

B. «Adelanto para la ruta» (#103)
--------------------------------
A cashier gives a driver cash for a route out of his open caja
(`add_advance`, /caja/adelanto): a movement of kind «adelanto», negative,
tied to the route (`advance_route_id`). The route's «Efectivo a rendir»
becomes cash collected + advance − expenses (models/reparto.py), so the
settlement asks it back and any shortfall follows A. Accounting: the advance
leaves the cash journal to «Deudores varios» of the driver; at the closing
that receives the settlement a line brings it back from the same account
and partner, reconciled with the advance when both are posted.

Opening debts: `_distri_book_opening_debts` (run by
scripts/correcciones/v129_27_deudas_iniciales.sh, dry run by default) books
the shortfall cases still open before the flag as «Deudores varios» debts.
"""
import logging

from odoo import api, fields, models
from odoo.exceptions import UserError

from .reparto import _money

_logger = logging.getLogger(__name__)

PARAM_CAJA_V2 = "distri_pedido.caja_v2"
# PARAM_ACCOUNT % "deudores" of models/caja_contabilidad.py (that module
# imports this one)
PARAM_DEBT_ACCOUNT = "distri_pedido.caja_cuenta_deudores"
DEFAULT_DEBT_CODE = "1.1.5.01.010"
DEBT_LABEL = "Deudores varios"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")
# the routes a cashier can give an advance to: planned or out on the road
ADVANCE_ROUTE_STATES = ("draft", "on_route")
ADVANCE_ROUTE_DAYS = 7


def caja_v2_on(env):
    value = (env["ir.config_parameter"].sudo().get_param(PARAM_CAJA_V2) or "").strip().lower()
    return value in ON_VALUES


def _debt_code(env):
    return (env["ir.config_parameter"].sudo().get_param(PARAM_DEBT_ACCOUNT)
            or DEFAULT_DEBT_CODE).strip()


def debt_account_ids(env):
    """The ids of «Deudores varios» if it exists (never created here): the
    customer balances leave it out. Empty in a database that never used it,
    so those balances read exactly as before."""
    code = _debt_code(env)
    accounts = env["account.account"].sudo().with_context(active_test=False).search(
        [("code", "=", code), ("account_type", "=", "asset_receivable")])
    return accounts.ids


def debt_account(env, company):
    """«Deudores varios» of the company: an asset_receivable account that is
    NOT the customers' receivable. Created the first time if missing (like
    «Diferencias de caja»)."""
    code = _debt_code(env)
    Account = env["account.account"].sudo().with_company(company)
    account = Account.search([("company_ids", "in", company.ids), ("code", "=", code)], limit=1)
    if not account:
        account = Account.create({"name": DEBT_LABEL, "code": code,
                                  "account_type": "asset_receivable", "reconcile": True,
                                  "company_ids": [(6, 0, company.ids)]})
    if account.account_type != "asset_receivable" or not account.reconcile:
        raise UserError("La cuenta %s (%s) tiene que ser de tipo «A cobrar» y conciliable: revisá "
                        "el parámetro %s." % (account.code, account.name, PARAM_DEBT_ACCOUNT))
    customers = env["ir.default"].sudo()._get(
        "res.partner", "property_account_receivable_id", company_id=company.id)
    if customers and customers == account.id:
        raise UserError("«%s» no puede ser la cuenta de los clientes (%s): revisá el parámetro %s."
                        % (DEBT_LABEL, account.code, PARAM_DEBT_ACCOUNT))
    return account


def employee_partner(user):
    """The partner a debt of `user` is booked under: his own res.users
    partner. Never a customer looked up by name."""
    user = user.sudo()
    if not user or user.share or not user.partner_id:
        raise UserError("La deuda tiene que ser de un empleado con usuario interno (%s)."
                        % (user.name or "sin usuario"))
    return user.partner_id


class DistriCajaV2(models.Model):
    _inherit = "distri.caja"

    total_adelantos = fields.Monetary(string="Adelantos para rutas",
                                      compute="_compute_total_adelantos")

    @api.depends("movement_ids.amount", "movement_ids.kind")
    def _compute_total_adelantos(self):
        for caja in self:
            caja.total_adelantos = sum(
                caja.movement_ids.filtered(lambda m: m.kind == "adelanto").mapped("amount"))

    # ------------------------------------------------------------ advance

    @api.model
    def advance_routes(self):
        """The routes a cashier can give an advance to: planned or on the
        road, of the last ADVANCE_ROUTE_DAYS days and later."""
        since = fields.Date.subtract(fields.Date.context_today(self), days=ADVANCE_ROUTE_DAYS)
        return self.env["distri.route"].sudo().search([
            ("kind", "=", "route"), ("state", "in", ADVANCE_ROUTE_STATES),
            ("company_id", "=", self.env.company.id), ("date", ">=", since)],
            order="date, driver_id, trip, id")

    def add_advance(self, route, amount, note=""):
        """«Adelanto para la ruta»: cash for the driver out of this open caja,
        tied to `route`; it comes back in the settlement."""
        from .caja import NOTE_MAX, OUTFLOW_TWIN_SECONDS, DuplicateOutflow, parse_amount
        if not caja_v2_on(self.env):
            raise UserError("El adelanto para la ruta todavía no está habilitado.")
        caja = self._check_mine_open()
        route = route.sudo().exists()
        if not route or route.kind != "route" or route.company_id != caja.company_id:
            raise UserError("Elegí la ruta del adelanto.")
        if route.state not in ADVANCE_ROUTE_STATES:
            raise UserError("La ruta %s ya volvió: no se le puede dar un adelanto." % route.name)
        partner = employee_partner(route.driver_id)
        value = parse_amount(self.env, amount)
        note = " ".join((note or "").split())[:NOTE_MAX]
        self.env.cr.execute("UPDATE distri_caja SET write_date = write_date WHERE id = %s",
                            [caja.id])
        Movement = self.env["distri.caja.movimiento"].sudo()
        twin = Movement.search([
            ("caja_id", "=", caja.id), ("kind", "=", "adelanto"),
            ("advance_route_id", "=", route.id), ("amount", "=", -value),
            ("note", "=", note or False),
            ("moment", ">=", fields.Datetime.subtract(fields.Datetime.now(),
                                                      seconds=OUTFLOW_TWIN_SECONDS))], limit=1)
        if twin:
            raise DuplicateOutflow(twin)
        if self.currency_id.compare_amounts(value, caja.expected) > 0:
            raise UserError("No alcanza: en la caja tiene que haber %s y querés sacar %s."
                            % (_money(caja.expected), _money(value)))
        movement = Movement.create({
            "caja_id": caja.id,
            "kind": "adelanto",
            "amount": -value,
            "advance_route_id": route.id,
            "debt_partner_id": partner.id,
            "who": route.driver_id.name,
            "note": note or False,
        })
        route.message_post(body="Adelanto para la ruta: %s, de la caja de %s (%s)%s." % (
            _money(value), caja.user_id.name, caja.name, ": %s" % note if note else ""))
        return movement

    def remove_advance(self, movement):
        """«Quitar el adelanto»: undo `add_advance` while this caja is open
        and the route was not received yet."""
        caja = self._check_mine_open()
        movement = movement.sudo().exists()
        if not movement or movement.caja_id != caja or movement.kind != "adelanto":
            raise UserError("Ese adelanto no está en esta caja.")
        route = movement.advance_route_id
        if movement.statement_line_ids or route.state == "settled":
            raise UserError("La ruta %s ya se rindió: el adelanto no se puede quitar." % route.name)
        route.message_post(body="Se quitó el adelanto para la ruta de %s (caja de %s)." % (
            _money(-movement.amount), caja.user_id.name))
        movement.unlink()
        return True


class DistriRouteCaseV2(models.Model):
    _inherit = "distri.route.case"

    # ------------------------------------------------------------ opening debts

    @api.model
    def _distri_opening_debt_cases(self, case_ids=None):
        """The open cash shortfalls not booked as a debt yet: kind «cash»,
        negative, not replenished, no debt partner."""
        domain = [("kind", "=", "cash"), ("state", "=", "open"), ("amount", "<", 0),
                  ("replenished", "=", False), ("debt_partner_id", "=", False),
                  ("debt_move_id", "=", False)]
        if case_ids:
            domain.append(("id", "in", list(case_ids)))
        return self.sudo().search(domain, order="id")

    @api.model
    def _distri_customer_receivable_total(self, employee_partner_ids):
        """The customers' receivable total (posted lines of every
        asset_receivable account) leaving out the employees' partners."""
        self.env.flush_all()
        self.env.cr.execute("""
            SELECT COALESCE(SUM(l.balance), 0)
              FROM account_move_line l
              JOIN account_account a ON a.id = l.account_id
             WHERE a.account_type = 'asset_receivable' AND l.parent_state = 'posted'
               AND (l.partner_id IS NULL OR NOT (l.partner_id = ANY(%s)))
        """, [list(employee_partner_ids) or [0]])
        return round(self.env.cr.fetchone()[0], 2)

    @api.model
    def _distri_book_opening_debts(self, apply=False, date=None, counterpart_code=None,
                                   journal_code=None, case_ids=None, company=None):
        """Books every open cash shortfall case (`_distri_opening_debt_cases`)
        as the employee's debt: Deudores varios (his res.users partner) against
        `counterpart_code` (default «Diferencias de caja», where the closing
        booked it), on `date` (default today), in `journal_code` (default
        the first general journal). Dry run unless `apply`: everything is
        rolled back by the caller. Returns one row per case. Raises when the
        customers' receivable total (employees left out) would change."""
        company = company or self.env.company
        cases = self._distri_opening_debt_cases(case_ids)
        date = fields.Date.to_date(date) if date else fields.Date.context_today(self)
        Account = self.env["account.account"].sudo().with_company(company)
        if counterpart_code:
            counterpart = Account.search([("company_ids", "in", company.ids),
                                          ("code", "=", counterpart_code)], limit=1)
            if not counterpart:
                raise UserError("No existe la cuenta %s." % counterpart_code)
        else:
            from .caja_contabilidad import caja_account
            counterpart = caja_account(self.env, company, "diferencias")
        if counterpart.account_type == "asset_receivable":
            raise UserError("La contrapartida no puede ser una cuenta a cobrar (%s)."
                            % counterpart.code)
        Journal = self.env["account.journal"].sudo()
        journal_domain = [("company_id", "=", company.id), ("type", "=", "general")]
        if journal_code:
            journal_domain.append(("code", "=", journal_code))
        journal = Journal.search(journal_domain, order="sequence, id", limit=1)
        if not journal:
            raise UserError("No hay un diario de operaciones varias%s." % (
                " con código %s" % journal_code if journal_code else ""))
        debts = debt_account(self.env, company)
        employees = self.env["res.users"].sudo().with_context(active_test=False).search(
            [("share", "=", False)]).partner_id
        before = self._distri_customer_receivable_total(employees.ids)
        rows = []
        for case in cases:
            user = case.driver_id
            row = {"case_id": case.id, "case": case.name, "amount": -case.amount,
                   "employee": user.name or "", "partner_id": False, "partner": "",
                   "move": "", "error": ""}
            try:
                partner = employee_partner(user)
            except UserError as exc:
                row["error"] = str(exc.args[0])
                rows.append(row)
                continue
            row.update({"partner_id": partner.id, "partner": partner.name})
            if apply:
                label = "Deuda de %s · %s" % (user.name, case.name)
                move = self.env["account.move"].sudo().with_context(
                    distri_employee_debt=True).create({
                    "move_type": "entry",
                    "journal_id": journal.id,
                    "date": date,
                    "ref": label[:250],
                    "line_ids": [
                        (0, 0, {"name": label, "account_id": debts.id, "partner_id": partner.id,
                                "debit": -case.amount, "credit": 0.0}),
                        (0, 0, {"name": label, "account_id": counterpart.id,
                                "debit": 0.0, "credit": -case.amount}),
                    ],
                })
                move.action_post()
                case.write({"debt_partner_id": partner.id, "debt_move_id": move.id})
                case.message_post(body="Deuda inicial en %s de %s: %s (asiento %s, 19.0.1.129.27)."
                                  % (DEBT_LABEL, partner.name, _money(-case.amount), move.name))
                row["move"] = move.name
            rows.append(row)
        after = self._distri_customer_receivable_total(employees.ids)
        if after != before:
            raise UserError("Los saldos de los clientes cambiarían (%s -> %s): no se registra "
                            "nada." % (before, after))
        if apply:
            _logger.info("19.0.1.129.27: booked %s opening employee debts",
                         sum(1 for r in rows if r["move"]))
        return {"rows": rows, "customers_receivable": after, "journal": journal.code,
                "counterpart": counterpart.code, "debt_account": debts.code,
                "date": fields.Date.to_string(date)}


class DistriRouteV2(models.Model):
    _inherit = "distri.route"

    def _distri_caja_v2_debts(self, case, difference):
        """At the reception of this settlement (models/caja.py receive_route),
        with distri_pedido.caja_v2 on: the rejected expenses and a shortfall
        are the driver's debt in «Deudores varios». Stores the decision on
        the cases and returns the partner of the shortfall's debt (empty
        when there is none, or with the flag off)."""
        self.ensure_one()
        Partner = self.env["res.partner"]
        if not caja_v2_on(self.env):
            return Partner
        rejected = self.expense_ids.filtered(
            lambda e: e.state == "rejected" and e.case_id and not e.move_id
            and not e.case_id.debt_partner_id)
        short = difference < 0 and not self.currency_id.is_zero(difference)
        if not (rejected or short):
            return Partner
        partner = employee_partner(self.driver_id)
        for expense in rejected:
            expense.case_id.sudo().write({"debt_partner_id": partner.id})
        if not short:
            return Partner
        if case:
            case.sudo().write({"debt_partner_id": partner.id})
            case.message_post(body="El faltante de %s queda como deuda de %s en %s." % (
                _money(-difference), partner.name, DEBT_LABEL))
        return partner


class ResPartnerEmployeeDebt(models.Model):
    _inherit = "res.partner"

    def _increase_rank(self, field, n=1):
        """19.0.1.129.27: Odoo ranks the partner of a receivable line of an
        entry as a customer; an employee's debt in «Deudores varios» (the
        context key, set only by those postings) must not make him one."""
        if field == "customer_rank" and self.env.context.get("distri_employee_debt"):
            return
        return super()._increase_rank(field, n)
