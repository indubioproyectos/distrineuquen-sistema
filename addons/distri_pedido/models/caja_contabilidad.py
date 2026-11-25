"""Accounting of the daily cash (19.0.1.97.0, audit F3, owner's option B).

The cash payments of CSH1 «Efectivo» and CDEP «Caja depósito» keep going to
the company's outstanding receipts account («Recibos pendientes»): their
payment method lines have no account of their own, so Odoo leaves them «en
proceso». The cash reaches the cash account only through a cash statement:

- Closing of a session (`_post_closing`): ONE account.bank.statement on the
  session journal (parameter PARAM_JOURNAL, CSH1 by default) with a line per
  payment that entered the box, reconciled with that payment (it becomes
  «paid» and its «Recibos pendientes» line is emptied); a line per outflow
  against its account; the driver expenses approved at a received
  settlement against their expense account; a rejected driver expense, a
  driver's difference, an «ajuste», the session difference and a changed
  opening float against the «diferencias de caja» account; and the amount
  withdrawn at the closing against the owner's withdrawal account. The
  statement goes from the opening float to what stays for tomorrow.
  19.0.1.129.27 (distri_pedido.caja_v2, models/caja_v2.py has the mapping
  table): with the flag on, the lines that are a person's shortfall (the
  rejected expense, a settlement's or the cashier's shortfall, the «ajuste»
  that cancels one) go to «Deudores varios» under the employee's own
  partner instead of «diferencias de caja»; an «Adelanto para la ruta» and
  its return at the settlement too. Surpluses and the opening float stay.
  19.0.1.117.0: a «Cobro cargado en Online» (a cash payment the cashier
  registered in Odoo Online, kind "online") has no lab payment to reconcile.
  Its line debits the cash account (the money IS in the box, and without it
  the statement would not reach «queda para mañana») against the «cobros de
  Online» account (PARAM_ACCOUNT % "online"; unset: the journal's suspense
  account, «Cuenta transitoria»), never against the customer's receivable:
  Online already settled that receivable and the lab books only the cash.
  The accountant clears that account against Online's books.
  19.0.1.118.0: a «Cobro registrado en la factura» (kind "factura": a
  payment the cashier registered on the invoice directly in CSH1) is booked
  exactly like a «Cobro en el local». Being in the cash journal does NOT put
  it in the cash account: CSH1's method lines have no account of their own,
  so the payment waits in «Recibos pendientes» like a CDEP one. Its closing
  line reconciles that open line, so the cash is booked once; a payment
  whose «Recibos pendientes» line is already reconciled (an earlier
  statement, the catch-up) gets no line, so it is never booked twice.
- Catch-up (`DistriCajaConciliacionInicial`, «Conciliación inicial de
  efectivo»): ONE statement with ONE line per journal reconciling every cash
  payment of CSH1 and CDEP still open in «Recibos pendientes». Run by hand,
  never on install; running it again finds nothing to do.

Accounts: each one is an account code in an ir.config_parameter (PARAM_ACCOUNT
% key, seeded by data/caja.xml), so the accountant changes them in Settings
without code. A missing code falls back to an account of the same type; the
«diferencias de caja» account is created the first time if missing.
"""
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .reparto import _money
from .reparto_cliente import today_ar

PARAM_JOURNAL = "distri_pedido.caja_diario"
PARAM_ACCOUNT = "distri_pedido.caja_cuenta_%s"
DEFAULT_JOURNAL = "CSH1"
CASH_JOURNAL_CODES = ("CSH1", "CDEP")

# key -> (default code of the Argentine chart, fallback account types, label)
ACCOUNTS = {
    "proveedor": ("2.1.1.01.010", ("liability_payable",), "Proveedores"),
    "flete": ("5.3.1.01.100", ("expense",), "Cadetería y franqueo"),
    "nafta": ("5.2.1.01.050", ("expense",), "Movilidad y viáticos"),
    "libreria": ("5.3.1.01.120", ("expense",), "Artículos de librería"),
    "sueldo": ("2.1.4.01.010", ("liability_payable", "liability_current"), "Sueldos a pagar"),
    "otro": ("5.2.1.01.030", ("expense",), "Gastos varios comercial"),
    "retiro": ("2.1.5.01.010", ("liability_non_current", "equity"), "Cuenta particular socio"),
    "banco": ("", ("asset_current",), "Transferencia de liquidez"),
    "diferencias": ("5.3.1.01.150", ("expense",), "Diferencias de caja"),
    # 19.0.1.117.0: no default code: the suspense account unless configured
    "online": ("", ("asset_current",), "Cobros en efectivo cargados en Online"),
}


def session_journal(env, company):
    """The cash journal of the sessions: PARAM_JOURNAL (a code), CSH1, or
    the route's default cash journal."""
    Journal = env["account.journal"].sudo()
    code = env["ir.config_parameter"].sudo().get_param(PARAM_JOURNAL) or DEFAULT_JOURNAL
    journal = Journal.search([("code", "=", code), ("company_id", "=", company.id),
                              ("type", "=", "cash")], limit=1)
    journal = journal or env["distri.route"].sudo().with_company(company)._default_journal()
    if not journal or journal.type != "cash":
        raise UserError("No hay un diario de efectivo para la caja (parámetro %s)." % PARAM_JOURNAL)
    return journal


def caja_account(env, company, key):
    """The account of an outflow kind, of «banco» or of «diferencias»."""
    default, types, label = ACCOUNTS[key]
    Account = env["account.account"].sudo().with_company(company)
    code = env["ir.config_parameter"].sudo().get_param(PARAM_ACCOUNT % key) or default
    domain = [("company_ids", "in", company.ids), ("active", "=", True)]
    account = Account.search(domain + [("code", "=", code)], limit=1) if code else Account
    if not account and key == "banco":
        account = company.transfer_account_id
    if not account and key == "online":
        account = company.account_journal_suspense_account_id
    if not account and key == "diferencias":
        # the first time: «Diferencias de caja» with the default code (or
        # the configured one when it does not exist yet)
        account = Account.create({"name": label, "code": code or default,
                                  "account_type": "expense",
                                  "company_ids": [(6, 0, company.ids)]})
    if not account:
        account = Account.search(domain + [("account_type", "in", types)], order="code", limit=1)
    if not account:
        raise UserError("No hay cuenta para «%s»: configurá el parámetro %s con su código."
                        % (label, PARAM_ACCOUNT % key))
    return account


def open_outstanding_lines(payments):
    """The payments' lines still open in their outstanding (receipts)
    account: what a statement line has to reconcile."""
    lines = payments.env["account.move.line"]
    for payment in payments:
        if payment.state not in ("in_process", "paid") or not payment.outstanding_account_id:
            continue
        lines |= payment.move_id.line_ids.filtered(
            lambda l, p=payment: l.account_id == p.outstanding_account_id
            and l.account_id.reconcile and not l.reconciled
            and not l.company_currency_id.is_zero(l.amount_residual))
    return lines


class DistriCajaAccounting(models.Model):
    _inherit = "distri.caja"

    # ------------------------------------------------------------ closing

    def _st_line(self, amount, label, account, partner=None, to_reconcile=None):
        """One posted statement line of the session journal; `to_reconcile`
        (journal items in `account`) are reconciled with its counterpart."""
        self.ensure_one()
        line = self.env["account.bank.statement.line"].sudo().create({
            "journal_id": self.journal_id.id,
            "date": self.date,
            "payment_ref": label[:250],
            "amount": amount,
            "partner_id": partner.id if partner else False,
            "counterpart_account_id": account.id,
        })
        if to_reconcile:
            counterpart = line.move_id.line_ids.filtered(lambda l: l.account_id == account)
            (counterpart | to_reconcile).reconcile()
        return line

    def _payment_lines(self, payments, movement, plan):
        for payment in payments.sorted("id"):
            open_lines = open_outstanding_lines(payment)
            if not open_lines:
                continue
            plan.append({
                "movement": movement,
                "amount": sum(open_lines.mapped("amount_residual")),
                "label": "%s · %s" % (payment.name, payment.memo or payment.partner_id.name or ""),
                "account": open_lines.account_id,
                "partner": payment.partner_id,
                "reconcile": open_lines,
            })

    def _debt_item(self, item, partner, match=None):
        """19.0.1.129.27: `item` goes to «Deudores varios» of `partner` (an
        employee's own partner, models/caja_v2.py); `match` = (movements,
        moves) whose open lines on that account it cancels."""
        from .caja_v2 import debt_account
        item.update({"account": debt_account(self.env, self.company_id), "partner": partner,
                     "debt": True})
        if match:
            item["match"] = match
        return item

    def _closing_plan(self):
        """The statement lines of the closing, in order (see the module
        docstring). Their sum goes from the opening to «queda para mañana»
        when every payment was still open."""
        self.ensure_one()
        company = self.company_id
        differences = caja_account(self.env, company, "diferencias")
        Movement = self.env["distri.caja.movimiento"].sudo()
        plan = []
        for move in self.movement_ids.sorted(lambda m: (m.moment, m.id)):
            if move.kind in ("cobro", "factura"):
                # 19.0.1.118.0 (see the module docstring): a payment
                # registered on the invoice is reconciled like a collection
                self._payment_lines(move.payment_id, move, plan)
            elif move.kind in ("rendicion", "recepcion"):
                route = move.route_id
                self._payment_lines(route.payment_ids, move, plan)
                # 19.0.1.129.27 (#103): the advance for the route comes back
                for advance in route.advance_ids.sorted("id"):
                    plan.append(self._debt_item({
                        "movement": move, "amount": -advance.amount,
                        "label": "Devuelve el adelanto para la ruta · %s · %s" % (
                            route.name, advance.caja_id.name),
                    }, advance.debt_partner_id, match=(advance, self.env["account.move"])))
                for expense in route.expense_ids.filtered(
                        lambda e: e.state == "approved" and not e.move_id):
                    plan.append({
                        "movement": move, "expense": expense, "amount": -expense.amount,
                        "label": "Gasto del repartidor · %s · %s" % (
                            dict(expense._fields["kind"].selection).get(expense.kind),
                            route.name),
                        "account": expense._expense_account(),
                    })
                for expense in route.expense_ids.filtered(
                        lambda e: e.state == "rejected" and e.case_id and not e.move_id):
                    item = {
                        "movement": move, "amount": -expense.amount, "account": differences,
                        "label": "Gasto sin comprobante, a cargo de %s · %s" % (
                            route.driver_id.name, route.name),
                    }
                    if expense.case_id.debt_partner_id:
                        self._debt_item(item, expense.case_id.debt_partner_id)
                    plan.append(item)
                if not self.currency_id.is_zero(move.route_difference):
                    item = {
                        "movement": move, "amount": move.route_difference, "account": differences,
                        "label": "Diferencia de %s · %s" % (route.name, dict(
                            move._fields["difference_mode"].selection).get(
                            move.difference_mode, "")),
                    }
                    if move.debt_partner_id:
                        self._debt_item(item, move.debt_partner_id)
                    plan.append(item)
            elif move.kind in ("gasto", "proveedor", "retiro", "banco"):
                key = move.concept if move.kind == "gasto" else move.kind
                account = caja_account(self.env, company, key)
                partner = move.partner_id
                if move.kind == "proveedor" and partner:
                    account = partner.with_company(company).property_account_payable_id or account
                plan.append({"movement": move, "amount": move.amount, "account": account,
                             "partner": partner, "label": move.label})
            elif move.kind == "online":
                # 19.0.1.117.0 (see the module docstring): cash in, receivable
                # untouched; no partner either, so no customer ledger moves
                plan.append({"movement": move, "amount": move.amount,
                             "account": caja_account(self.env, company, "online"),
                             "label": "Cobro cargado en Online · %s · %s" % (
                                 move.note or "", move.who or "")})
            elif move.kind == "ajuste":
                item = {"movement": move, "amount": move.amount, "account": differences,
                        "label": "Ajuste · %s" % (move.label or "")}
                if move.debt_partner_id:
                    # 19.0.1.129.27: he brought the shortfall booked as his
                    # debt: cancel it on the same account and partner
                    origin = Movement.search([
                        ("case_id", "=", move.case_id.id), ("id", "!=", move.id),
                        ("kind", "in", ("rendicion", "recepcion"))]) if move.case_id else Movement
                    self._debt_item(item, move.debt_partner_id,
                                    match=(origin, move.case_id.debt_move_id))
                plan.append(item)
            elif move.kind == "adelanto":
                # 19.0.1.129.27 (#103): cash out to the driver's debt
                plan.append(self._debt_item({
                    "movement": move, "amount": move.amount,
                    "label": "Adelanto para la ruta · %s · %s" % (
                        move.advance_route_id.name or "", move.who or ""),
                }, move.debt_partner_id))
        start = self.previous_id.left_for_tomorrow if self.previous_id else self.opening_float
        if not self.currency_id.is_zero(self.opening_float - start):
            plan.append({"amount": self.opening_float - start, "account": differences,
                         "label": "Fondo inicial distinto del cierre anterior · %s" % (
                             self.opening_note or "")})
        if not self.currency_id.is_zero(self.difference):
            item = {"amount": self.difference, "account": differences,
                    "label": "%s de caja · %s" % (
                        "Faltante" if self.difference < 0 else "Sobrante", self.reason or "")}
            # 19.0.1.129.27: the cashier's own shortfall is his debt
            debt = self._distri_cashier_debt_partner()
            if debt:
                self._debt_item(item, debt)
            plan.append(item)
        if not self.currency_id.is_zero(self.withdrawn):
            plan.append({"amount": -self.withdrawn,
                         "account": caja_account(self.env, company, "retiro"),
                         "label": "Se retira al cerrar la caja"})
        return plan, start

    def _post_closing(self):
        """The cash statement of the closing (see the module docstring)."""
        self.ensure_one()
        plan, start = self._closing_plan()
        plan = [p for p in plan if not self.currency_id.is_zero(p["amount"])]
        if not plan:
            return self.env["account.bank.statement"]
        lines = self.env["account.bank.statement.line"]
        matches = []
        for item in plan:
            # 19.0.1.129.27: an employee's debt does not rank him as a customer
            caja = self.with_context(distri_employee_debt=True) if item.get("debt") else self
            line = caja._st_line(item["amount"], "%s · %s" % (self.name, item["label"]),
                                 item["account"], item.get("partner"), item.get("reconcile"))
            lines |= line
            if item.get("match"):
                matches.append((item, line))
            if item.get("movement"):
                item["movement"].sudo().statement_line_ids = [(4, line.id)]
            expense = item.get("expense")
            if expense:
                expense.sudo().move_id = line.move_id
                if expense.photo:
                    self.env["ir.attachment"].sudo().create({
                        "name": "Comprobante %s.jpg" % item["label"], "datas": expense.photo,
                        "res_model": "account.move", "res_id": line.move_id.id,
                        "mimetype": "image/jpeg"})
        for move in self.movement_ids.filtered("photo"):
            for line in move.statement_line_ids:
                self.env["ir.attachment"].sudo().create({
                    "name": "Comprobante %s.jpg" % move.label, "datas": move.photo,
                    "res_model": "account.move", "res_id": line.move_id.id,
                    "mimetype": "image/jpeg"})
        # 19.0.1.103.0: a PDF receipt goes with its line the same way
        for move in self.movement_ids.filtered("document"):
            for line in move.statement_line_ids:
                self.env["ir.attachment"].sudo().create({
                    "name": "Comprobante %s.pdf" % move.label, "datas": move.document,
                    "res_model": "account.move", "res_id": line.move_id.id,
                    "mimetype": "application/pdf"})
        statement = self.env["account.bank.statement"].sudo().create({
            "name": self.name,
            "balance_start": start,
            "balance_end_real": self.left_for_tomorrow,
            "line_ids": [(6, 0, lines.ids)],
        })
        self.statement_id = statement
        self._reconcile_debts(matches)
        return statement

    def _reconcile_debts(self, matches):
        """19.0.1.129.27: a line that cancels an employee's debt (the advance
        coming back, the «ajuste» of a shortfall he brought) is reconciled
        with that debt's open line on «Deudores varios», when it is posted
        (the other caja may still be open: then it stays open on the same
        account and partner, and the balance nets anyway)."""
        for item, line in matches:
            account, partner = item["account"], item["partner"]
            movements, moves = item["match"]

            def open_lines(records):
                return records.line_ids.filtered(
                    lambda l: l.account_id == account and l.partner_id == partner
                    and l.parent_state == "posted" and not l.reconciled)

            mine = open_lines(line.move_id)
            others = open_lines(movements.statement_line_ids.move_id | moves) - mine
            if not mine or not others:
                continue
            sign = 1 if sum(mine.mapped("balance")) > 0 else -1
            others = others.filtered(lambda l: l.balance * sign < 0)
            if others:
                (mine | others).reconcile()

    def _undo_closing(self):
        """Reopening: unreconcile and delete the closing's statement; the
        deferred driver expenses go back to «not posted yet»."""
        self.ensure_one()
        statement = self.statement_id
        if not statement:
            return
        lines = statement.line_ids
        moves = lines.move_id
        self.env["distri.route.expense"].sudo().search([("move_id", "in", moves.ids)]).write(
            {"move_id": False})
        moves.line_ids.remove_move_reconcile()
        self.statement_id = False
        # the statement first: a line of a complete statement cannot be deleted
        statement.unlink()
        lines.unlink()


class DistriCajaConciliacionInicial(models.TransientModel):
    """«Conciliación inicial de efectivo»: the one-off catch-up (owner's
    decision: a single entry per journal). Preview first, then «Conciliar»."""
    _name = "distri.caja.conciliacion.inicial"
    _description = "Conciliación inicial de efectivo"

    company_id = fields.Many2one("res.company", required=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    summary = fields.Html(string="Qué se va a conciliar", compute="_compute_summary",
                          sanitize=False)
    payment_count = fields.Integer(string="Pagos", compute="_compute_summary")
    total = fields.Monetary(string="Total", compute="_compute_summary")
    done = fields.Boolean(readonly=True)
    result = fields.Html(readonly=True, sanitize=False)

    @api.model
    def _check_manager(self):
        user = self.env.user
        if not (user.has_group("distri_pedido.grupo_gerencia")
                or user.has_group("base.group_system")):
            raise AccessError("Solo gerencia hace la conciliación inicial del efectivo.")

    @api.model
    def _backlog(self, company):
        """{journal: payments} still open in their outstanding account, of
        CSH1 and CDEP, that no daily cash session will reconcile."""
        self._check_manager()
        Journal = self.env["account.journal"].sudo()
        journals = Journal.search([("code", "in", CASH_JOURNAL_CODES),
                                   ("company_id", "=", company.id), ("type", "=", "cash")])
        journals = (journals | session_journal(self.env, company)).sorted("code", reverse=True)
        Movement = self.env["distri.caja.movimiento"].sudo()
        # left to the daily cash: what a session already holds, and the cash
        # still in an open «Caja depósito» box (a cashier receives it)
        in_sessions = (Movement.search([("payment_id", "!=", False)]).payment_id
                       | Movement.search([("route_id", "!=", False)]).route_id.payment_ids
                       | self.env["distri.route"].sudo().search([
                           ("kind", "=", "depot"), ("state", "!=", "settled"),
                           ("company_id", "=", company.id)]).payment_ids)
        result = {}
        for journal in journals:
            payments = self.env["account.payment"].sudo().search([
                ("journal_id", "=", journal.id), ("payment_type", "=", "inbound"),
                ("state", "in", ("in_process", "paid"))], order="date, id")
            payments -= in_sessions
            lines = open_outstanding_lines(payments)
            payments = payments.filtered(lambda p, moves=lines.move_id: p.move_id in moves)
            result[journal] = (payments, lines)
        return result

    @api.depends("company_id")
    def _compute_summary(self):
        for wizard in self:
            backlog = wizard._backlog(wizard.company_id)
            rows = []
            count = total = 0
            for journal, (payments, lines) in backlog.items():
                amount = sum(lines.mapped("amount_residual"))
                in_process = payments.filtered(lambda p: p.state == "in_process")
                paid = payments - in_process
                count += len(payments)
                total += amount
                rows.append(Markup(
                    "<tr><td><b>%s</b> %s</td><td class='text-end'>%s</td><td class='text-end'>%s</td>"
                    "<td class='text-end'>%s · %s</td><td class='text-end'>%s · %s</td></tr>") % (
                    journal.code, journal.name, len(payments), _money(amount),
                    len(in_process), _money(sum(in_process.mapped("amount"))),
                    len(paid), _money(sum(paid.mapped("amount")))))
            wizard.payment_count = count
            wizard.total = total
            wizard.summary = Markup(
                "<table class='table table-sm'><thead><tr><th>Diario</th><th class='text-end'>Pagos</th>"
                "<th class='text-end'>Importe</th><th class='text-end'>En proceso</th>"
                "<th class='text-end'>Pagados por factura, con el efectivo pendiente</th></tr>"
                "</thead><tbody>%s</tbody></table><p>Se hace <b>un solo extracto con una sola "
                "línea por diario</b>, con fecha de hoy: el importe pasa de «Recibos pendientes» "
                "a la cuenta de caja del diario y cada pago queda «Pagado».</p>") % Markup(
                "").join(rows)

    def action_apply(self):
        self.ensure_one()
        self._check_manager()
        company = self.company_id
        backlog = self._backlog(company)
        done = []
        for journal, (payments, lines) in backlog.items():
            if not lines:
                continue
            outstanding = lines.account_id
            if len(outstanding) != 1:
                raise UserError("Los pagos de %s están en más de una cuenta de recibos pendientes."
                                % journal.code)
            amount = sum(lines.mapped("amount_residual"))
            label = "Conciliación inicial de efectivo · %s · %s pagos" % (journal.code, len(payments))
            line = self.env["account.bank.statement.line"].sudo().create({
                "journal_id": journal.id,
                "date": today_ar(),
                "payment_ref": label,
                "amount": amount,
                "counterpart_account_id": outstanding.id,
            })
            counterpart = line.move_id.line_ids.filtered(lambda l: l.account_id == outstanding)
            (counterpart | lines).reconcile()
            statement = self.env["account.bank.statement"].sudo().create({
                "name": label,
                "line_ids": [(6, 0, line.ids)],
            })
            statement.balance_end_real = statement.balance_end
            done.append(Markup("<li><b>%s</b>: %s pagos, %s (extracto %s, asiento %s)</li>") % (
                journal.code, len(payments), _money(amount), statement.name, line.move_id.name))
        self.write({"done": True, "result": Markup("<ul>%s</ul>") % Markup("").join(done)
                    if done else Markup("<p>No había nada pendiente: no se hizo ningún asiento.</p>")})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new"}
