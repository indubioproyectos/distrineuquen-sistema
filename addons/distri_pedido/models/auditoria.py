"""Full audit of the collections on every Mercado Pago report (19.0.1.129.0).

Docs: cobros/analisis-cobro-un-paso-2026-11-21.md («Objetivo 2», decisions
5-7 taken on 21/11/2026) and proyecto/auditoria-general-2026-11-22.md («En
v129»).

Every report upload (by hand or from the SFTP folder) runs
`distri.mp.audit._run` at the end of `distri.mp.report._load_rows`. The
audit is total, never a sample. Its coverage is the moment of the last
movement of the reports loaded (`period_end`); it checks:

- every credited receipt (`distri.mp.collection` kind «verify» with
  `credited_at`), against every income of the reports, with the cascade
  exact operation -> operation with one OCR slip and the same amount -> same
  amount and time within 2 minutes (and the CUIT when read) -> same amount,
  customer and day when unique -> same customer and amount within ±2 days
  (review only). A unique candidate ties (no new payment); several go to
  «Revisar». `audit_state`: pendiente (not covered yet), verificado,
  diferencia, sin_transferencia, revertido, anulado (by «Anular pago»);
- every electronic payment (inbound customer payments of the bank journals:
  «Mercado Pago», «Banco»...), however it was loaded: «sin comprobante» when
  no receipt is attached anywhere; a manual payment in «Mercado Pago» with no
  transfer in the report;
- duplicates: the same operation credited twice; a transfer of the report
  confirmed with its own payment while a credited receipt already paid it;
  the same customer and amount (±$1) within ±2 days on two payments that are
  not two distinct transfers of the report;
- bank twins: an income that may be a payment registered by hand in «Banco»
  is proposed (`bank_twin_payment_id`), never tied by itself.

Each finding is a `distri.mp.anomaly` (menu «Auditoría de cobros»), kept by
a stable key: found again it is updated, not found any more it is
«Resuelta» by itself, reviewed it stays reviewed. A credited payment still
without its transfer when the coverage passed its crediting by
PARAM_ALERT_DAYS (3) becomes an alert for Gerencia: an activity on the
anomaly and a post on the receipt card. Coverage, never wall clock: without
a new report nothing is alerted.

19.0.1.129.2 (models/efectivo_a_rendir.py): a driver's cash payment still
«a rendir» (credited at the visit, the route not received in a caja yet)
older than PARAM_DRIVER_CASH_DAYS (1) days, by the wall clock this time, is
an «efectivo_a_rendir» anomaly with the same alert to Gerencia. It is
checked with or without a Mercado Pago report, and the daily cron runs for
it too. Those payments are cash: never «sin comprobante», never matched
against the report.

`distri.credit.summary`: what each user credited that day, built by a daily
cron, with an activity for Gerencia (the backend, not email: SMTP fails).
"""
import logging
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta, timezone

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .cobros import AMOUNT_TOLERANCE, can_authorize_money, digits, local_day, local_when
from .reparto import _money
from .reparto_cliente import AR_TZ, today_ar

_logger = logging.getLogger(__name__)

AUDIT_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia",
                "base.group_system")
GERENCIA_GROUP = "distri_pedido.grupo_gerencia"
PARAM_ALERT_DAYS = "distri_pedido.auditoria_dias_alerta"
DEFAULT_ALERT_DAYS = 3
ALERT_SUMMARY = "Acreditado sin transferencia en el reporte"
# 19.0.1.129.2: the driver's cash credited at the visit and not settled yet
PARAM_DRIVER_CASH_DAYS = "distri_pedido.auditoria_dias_efectivo_a_rendir"
DEFAULT_DRIVER_CASH_DAYS = 1
DRIVER_CASH_ALERT_SUMMARY = "Efectivo del repartidor sin rendir"
DAILY_SUMMARY = "Resumen diario de lo acreditado"
# same customer and amount within this many days: a possible duplicate
DUP_DAYS = 2
CASCADE = ("operation", "operation_close", "time", "customer", "near")

ANOMALY_KINDS = [
    ("sin_transferencia", "Acreditado sin transferencia en el reporte"),
    ("diferencia", "Importe distinto al del reporte"),
    ("revertido", "Devolución o contracargo en Mercado Pago"),
    ("revisar", "Revisar: puede ser otra transferencia"),
    ("doble_acreditacion", "Acreditado dos veces: otro pago por la misma transferencia"),
    ("operacion_repetida", "Misma operación acreditada dos veces"),
    ("cliente_importe", "Mismo cliente e importe en dos días"),
    ("manual_sin_transferencia", "Pago manual en Mercado Pago sin transferencia"),
    ("mellizo_banco", "Pago del banco que puede ser una transferencia del reporte"),
    ("sin_comprobante", "Pago electrónico sin comprobante"),
    ("efectivo_a_rendir", "Efectivo del repartidor sin rendir"),
]
# found again: the money may be missing; the rest are checks
ALERT_KINDS = ("sin_transferencia", "manual_sin_transferencia")
ANOMALY_STATES = [("open", "Abierta"), ("reviewed", "Revisada"), ("resolved", "Resuelta sola"),
                  ("annulled", "Pago anulado")]


def audit_user(user):
    return bool(user) and (user._is_superuser() or any(user.has_group(g) for g in AUDIT_GROUPS))


def _check_audit(env):
    if not (env.su or audit_user(env.user)):
        raise AccessError("La auditoría de cobros es de administración, gerencia y sistemas.")


def can_annul(env):
    """«Anular pago» moves money: who authorizes money or Gerencia."""
    return bool(env.su or can_authorize_money(env) or env.user.has_group(GERENCIA_GROUP))


def alert_days(env):
    value = env["ir.config_parameter"].sudo().get_param(PARAM_ALERT_DAYS)
    try:
        return max(int(value), 1) if value not in (None, False, "") else DEFAULT_ALERT_DAYS
    except ValueError:
        return DEFAULT_ALERT_DAYS


def driver_cash_days(env):
    value = env["ir.config_parameter"].sudo().get_param(PARAM_DRIVER_CASH_DAYS)
    try:
        return max(int(value), 1) if value not in (None, False, "") else DEFAULT_DRIVER_CASH_DAYS
    except ValueError:
        return DEFAULT_DRIVER_CASH_DAYS


def overdue_driver_cash(env, company):
    """The «a rendir» payments of the company older than the parameter."""
    limit = fields.Datetime.now() - timedelta(days=driver_cash_days(env))
    return env["account.payment"].sudo().search([
        ("company_id", "=", company.id), ("distri_driver_cash_state", "=", "a_rendir"),
        ("state", "in", ("in_process", "paid")), ("create_date", "<=", limit)],
        order="create_date, id")


def gerencia_users(env):
    root = env.ref("base.user_root")
    return env.ref(GERENCIA_GROUP).sudo().user_ids.filtered(
        lambda u: u.active and not u.share and u != root).sorted("id")


def day_bounds(day):
    """Naive UTC [start, end) of an Argentine calendar day."""
    start = datetime.combine(day, time.min, tzinfo=AR_TZ).astimezone(timezone.utc)
    start = start.replace(tzinfo=None)
    return start, start + timedelta(days=1)


# ====================================================================== collection


class DistriMpCollectionAudit(models.Model):
    _inherit = "distri.mp.collection"

    # 19.0.1.129.0 (audit 2026-11-22, «índice único de operación»): two
    # receipts of the same operation are never both credited, even at the
    # same moment. The advisory lock of `_distri_credit` does not cover it on
    # its own: under REPEATABLE READ the second transaction keeps the snapshot
    # it took before waiting for the lock. The unique index is checked
    # against the rows being written, whatever the snapshot.
    _credited_operation_uniq = models.UniqueIndex(
        "(operation) WHERE kind = 'verify' AND state = 'confirmed' "
        "AND credited_at IS NOT NULL AND operation IS NOT NULL AND operation <> ''",
        "Esa operación ya se acreditó con otro comprobante: no se acredita dos veces.")
    # the searches of the audit, the board and the crediting
    _kind_state_idx = models.Index("(kind, state)")
    _kind_amount_idx = models.Index("(kind, amount)")
    _payment_idx = models.Index("(payment_id) WHERE payment_id IS NOT NULL")
    _receipt_idx = models.Index("(receipt_id) WHERE receipt_id IS NOT NULL")
    _partner_idx = models.Index("(partner_id) WHERE partner_id IS NOT NULL")
    _credited_idx = models.Index("(credited_at) WHERE credited_at IS NOT NULL")

    anomaly_ids = fields.One2many("distri.mp.anomaly", "card_id", string="Anomalías")

    def _distri_audit_moment(self):
        """When the transfer of this credited receipt was made: the time read
        on it, else when it arrived, else the crediting."""
        self.ensure_one()
        card = self.sudo()
        return card.receipt_id.distri_receipt_date or card.receipt_id.timestamp or card.date \
            or card.credited_at

    def _distri_audit_candidates(self, claimed):
        """(level, incomes): the incomes of the reports that may be this
        credited receipt's transfer, by the first level of the cascade that
        finds any. `claimed`: ids of incomes already tied to a card."""
        self.ensure_one()
        card = self.sudo()
        operation = digits(card.operation or card.receipt_id.distri_operation)
        amount = card.amount
        domain = [("kind", "=", "income"), ("state", "in", ("draft", "confirmed")),
                  ("company_id", "=", card.company_id.id), "|",
                  "&", ("amount", ">=", amount - AMOUNT_TOLERANCE),
                  ("amount", "<=", amount + AMOUNT_TOLERANCE)]
        if operation:
            domain += ["|", ("source_id", "=", operation), ("bank_transfer_id", "=", operation)]
        else:
            domain += [("id", "=", 0)]
        incomes = self.sudo().search(domain).filtered(
            lambda i: i.id not in claimed
            and not (i.payment_id and i.payment_id == card.payment_id))
        for level in CASCADE:
            found = incomes.filtered(lambda i: i._credited_match_level(card, level))
            if found:
                return level, found
        return False, self.browse()

    # ------------------------------------------------------------ bank twins

    def action_tie_bank_twin(self):
        """«Es el pago cargado a mano»: the person confirms that the income
        is the payment registered by hand; no new payment."""
        _check_cobros_or_raise(self.env)
        for rec in self.sudo():
            if rec.kind != "income" or rec.state != "draft":
                raise UserError("Solo se vincula un ingreso pendiente del reporte.")
            twins = rec._bank_twin_payments()
            if not twins:
                raise UserError("%s: ya no hay un pago cargado a mano que coincida." % rec.name)
            rec._check_one_bank_twin(twins)
            if not rec.partner_id:
                raise UserError("Elegí el cliente antes de vincular.")
            rec._tie_bank_payment(twins)
            rec.bank_twin_payment_id = False
            rec.anomaly_income_ids.filtered(lambda a: a.state == "open").write(
                {"state": "resolved", "resolved_at": fields.Datetime.now(),
                 "review_note": "Vinculado a mano por %s" % self.env.user.name})
        return True

    def action_confirm_not_bank_twin(self):
        """«Confirmar: no es ese pago»: the person checked that the payment
        registered by hand is another transfer; the income gets its own."""
        _check_cobros_or_raise(self.env)
        for rec in self:
            rec.message_post(body="%s confirmó que no es el pago cargado a mano %s." % (
                self.env.user.name, rec.sudo().bank_twin_payment_id.name or ""))
        self.sudo().write({"bank_twin_payment_id": False})
        return self.with_context(distri_not_bank_twin=True).action_confirm_board()

    anomaly_income_ids = fields.One2many("distri.mp.anomaly", "income_id",
                                         string="Anomalías del ingreso")


def _check_cobros_or_raise(env):
    if not can_authorize_money(env):
        from .cobros import COBROS_DENIED
        raise AccessError(COBROS_DENIED)


# ====================================================================== payment


class AccountPaymentAudit(models.Model):
    _inherit = "account.payment"

    distri_without_receipt = fields.Boolean(
        string="Sin comprobante", compute="_compute_distri_without_receipt",
        search="_search_distri_without_receipt",
        help="Pago electrónico sin comprobante adjunto, según la última auditoría de cobros.")

    def _compute_distri_without_receipt(self):
        marked = set()
        if self.ids:
            marked = set(self.env["distri.mp.anomaly"].sudo().search([
                ("kind", "=", "sin_comprobante"), ("state", "=", "open"),
                ("payment_id", "in", self.ids)]).payment_id.ids)
        for payment in self:
            payment.distri_without_receipt = payment.id in marked

    def _search_distri_without_receipt(self, operator, value):
        ids = self.env["distri.mp.anomaly"].sudo().search([
            ("kind", "=", "sin_comprobante"), ("state", "=", "open")]).payment_id.ids
        if operator == "in":
            positive = True in value
        elif operator == "not in":
            positive = True not in value
        else:
            positive = (operator == "=") == bool(value)
        return [("id", "in" if positive else "not in", ids)]


# ====================================================================== anomaly


class DistriMpAnomaly(models.Model):
    _name = "distri.mp.anomaly"
    _description = "Anomalía de la auditoría de cobros"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "state_order, first_seen desc, id desc"

    name = fields.Char(string="Anomalía", compute="_compute_name", store=True)
    key = fields.Char(required=True, readonly=True, index=True, copy=False)
    kind = fields.Selection(ANOMALY_KINDS, string="Tipo", required=True, readonly=True,
                            index=True)
    state = fields.Selection(ANOMALY_STATES, string="Estado", default="open", required=True,
                             readonly=True, index=True, tracking=True)
    state_order = fields.Integer(compute="_compute_state_order", store=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True, index=True,
                                 ondelete="set null")
    payment_state = fields.Selection(related="payment_id.state", string="Estado del pago")
    journal_id = fields.Many2one(related="payment_id.journal_id", string="Diario", store=True)
    related_payment_id = fields.Many2one("account.payment", string="Otro pago", readonly=True,
                                         ondelete="set null")
    card_id = fields.Many2one("distri.mp.collection", string="Comprobante", readonly=True,
                              index=True, ondelete="set null")
    related_card_id = fields.Many2one("distri.mp.collection", string="Otro comprobante",
                                      readonly=True, ondelete="set null")
    income_id = fields.Many2one("distri.mp.collection", string="Transferencia del reporte",
                                readonly=True, index=True, ondelete="set null")
    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True, index=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    credited_by_id = fields.Many2one("res.users", string="Acreditó o cargó", readonly=True)
    credited_at = fields.Datetime(string="Acreditado o cargado el", readonly=True)
    detail = fields.Char(string="Detalle", readonly=True)
    audit_id = fields.Many2one("distri.mp.audit", string="Última auditoría", readonly=True,
                               ondelete="set null")
    first_seen = fields.Datetime(string="Detectada el", readonly=True,
                                 default=fields.Datetime.now)
    last_seen = fields.Datetime(string="Vista por última vez", readonly=True)
    overdue = fields.Boolean(string="Alerta a Gerencia", readonly=True,
                             help="Sigue sin transferencia cuando el reporte ya cubre los días "
                                  "de alerta desde la acreditación.")
    reviewed_by_id = fields.Many2one("res.users", string="Revisó", readonly=True)
    reviewed_at = fields.Datetime(string="Revisada el", readonly=True)
    resolved_at = fields.Datetime(string="Resuelta el", readonly=True)
    review_note = fields.Text(string="Nota", readonly=True)

    _key_unique = models.Constraint("unique(key)", "Esa anomalía ya está registrada.")

    @api.depends("kind", "partner_id", "amount")
    def _compute_name(self):
        labels = dict(ANOMALY_KINDS)
        for rec in self:
            rec.name = "%s · %s · %s" % (labels.get(rec.kind, rec.kind),
                                         rec.partner_id.name or "sin cliente",
                                         _money(rec.amount or 0.0))

    @api.depends("state")
    def _compute_state_order(self):
        order = {"open": 0, "reviewed": 1, "annulled": 2, "resolved": 3}
        for rec in self:
            rec.state_order = order.get(rec.state, 9)

    def _close_alerts(self, feedback):
        alerts = self.sudo().activity_ids.filtered(
            lambda a: a.summary in (ALERT_SUMMARY, DRIVER_CASH_ALERT_SUMMARY))
        if alerts:
            alerts.action_feedback(feedback=feedback)

    # ------------------------------------------------------------ actions

    def action_open_review(self):
        _check_audit(self.env)
        return self.env["distri.mp.anomaly.review"]._open("review", self)

    def action_open_annul(self):
        self.ensure_one()
        _check_audit(self.env)
        if not can_annul(self.env):
            raise AccessError("Anulan un pago quienes autorizan cobros y Gerencia.")
        if not self.payment_id or self.payment_id.state in ("draft", "canceled"):
            raise UserError("La anomalía no tiene un pago vigente para anular.")
        return self.env["distri.mp.anomaly.review"]._open("annul", self)

    def _mark_reviewed(self, note):
        _check_audit(self.env)
        if not (note or "").strip():
            raise UserError("Escribí qué revisaste.")
        now = fields.Datetime.now()
        for rec in self.sudo().filtered(lambda a: a.state == "open"):
            rec.write({"state": "reviewed", "reviewed_by_id": self.env.user.id,
                       "reviewed_at": now, "review_note": note.strip()})
            rec.message_post(body="Revisada por %s: %s" % (self.env.user.name, note.strip()))
            rec._close_alerts("Revisada por %s: %s" % (self.env.user.name, note.strip()))
        return True

    def _annul_payment(self, note):
        """«Anular pago» (decision 6, manual): the payment is cancelled (its
        invoices get their debt back, the account updates), the receipt card
        is «Anulado» and leaves the credited ones; an income of the report
        goes back to the board, where it can tie to its credited receipt."""
        self.ensure_one()
        _check_audit(self.env)
        if not can_annul(self.env):
            raise AccessError("Anulan un pago quienes autorizan cobros y Gerencia.")
        note = (note or "").strip()
        if not note:
            raise UserError("Escribí por qué se anula el pago.")
        payment = self.sudo().payment_id
        if not payment or payment.state in ("draft", "canceled"):
            raise UserError("La anomalía no tiene un pago vigente para anular.")
        if payment.distri_driver_cash_state:
            # 19.0.1.129.2: the customer paid the driver; a missing cash is
            # the driver's (the route's cash case), never the customer's
            raise UserError("Es efectivo que cobró el repartidor: el cliente ya pagó y no se le "
                            "anula. Se resuelve cuando un cajero recibe la ruta; un faltante "
                            "queda a cargo del repartidor.")
        Collection = self.env["distri.mp.collection"].sudo()
        cards = Collection.with_context(active_test=False).search([("payment_id", "=", payment.id)])
        credited = cards.filtered(lambda c: c.kind == "verify" and c.credited_at)
        incomes = cards.filtered(lambda c: c.kind == "income")
        if credited.filtered("income_id"):
            raise UserError("La transferencia de ese pago está en el reporte de Mercado Pago (%s): "
                            "no es un acreditado falso. Si el cliente estaba mal, usá «Deshacer» "
                            "en el comprobante." % credited.income_id[:1].name)
        user = self.env.user
        partner = payment.partner_id.commercial_partner_id
        text = "Pago %s de %s anulado por %s desde la auditoría de cobros: %s" % (
            payment.name, _money(payment.amount), user.name, note)
        for income in incomes:
            # the transfer is real: the income goes back to the board
            income.with_context(distri_audit_annul=True).action_undo()
        if payment.state not in ("draft", "canceled"):
            payment.action_draft()
            payment.action_cancel()
        for card in credited:
            card.write({"state": "ignored", "audit_state": "anulado", "ignore_reason": "other",
                        "ignore_note": "Pago anulado en la auditoría: %s" % note,
                        "report_review": False})
            card.message_post(body=text)
            card._close_alerts(text)
        supplier = self.env["distri.supplier.payment"].sudo().search(
            [("payment_id", "=", payment.id)])
        supplier.write({"state": "cancelled"})
        payment.message_post(body=text)
        if partner:
            partner.message_post(body=text)
        related = self.sudo().search([("payment_id", "=", payment.id), ("state", "=", "open")])
        now = fields.Datetime.now()
        (related | self.sudo()).write({"state": "annulled", "reviewed_by_id": user.id,
                                       "reviewed_at": now, "review_note": note})
        for rec in related | self.sudo():
            rec.message_post(body=text)
            rec._close_alerts(text)
        if incomes:
            incomes._match_hand_verified()
            incomes._propose()
        _logger.info("Audit: payment %s annulled by %s", payment.id, user.login)
        return payment

    def action_view_payment(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "account.payment",
                "view_mode": "form", "res_id": self.payment_id.id}

    def action_view_card(self):
        self.ensure_one()
        card = self.card_id or self.income_id
        return {"type": "ir.actions.act_window", "res_model": "distri.mp.collection",
                "view_mode": "form", "res_id": card.id,
                "views": [(self.env.ref("distri_pedido.view_mp_collection_form").id, "form")]}


class DistriMpAnomalyReview(models.TransientModel):
    """«Marcar revisado» or «Anular pago», with the note."""
    _name = "distri.mp.anomaly.review"
    _description = "Revisar anomalía de cobros"

    mode = fields.Selection([("review", "Marcar revisado"), ("annul", "Anular pago")],
                            required=True, readonly=True)
    anomaly_ids = fields.Many2many("distri.mp.anomaly", string="Anomalías", readonly=True)
    note = fields.Text(string="Nota")
    warning = fields.Char(compute="_compute_warning")

    @api.depends("mode", "anomaly_ids")
    def _compute_warning(self):
        for wizard in self:
            payment = wizard.anomaly_ids[:1].payment_id
            wizard.warning = (
                "Se anula el pago %s de %s a %s: vuelve la deuda de lo que pagaba." % (
                    payment.name, _money(payment.amount), payment.partner_id.name or "")
                if wizard.mode == "annul" and payment else False)

    @api.model
    def _open(self, mode, anomalies):
        wizard = self.create({"mode": mode, "anomaly_ids": [(6, 0, anomalies.ids)]})
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": wizard.id,
                "view_mode": "form", "target": "new",
                "name": "Anular pago" if mode == "annul" else "Marcar revisado"}

    def action_apply(self):
        self.ensure_one()
        if self.mode == "annul":
            self.anomaly_ids[:1]._annul_payment(self.note)
        else:
            self.anomaly_ids._mark_reviewed(self.note)
        return {"type": "ir.actions.act_window_close"}


# ====================================================================== audit run


class DistriMpAudit(models.Model):
    _name = "distri.mp.audit"
    _description = "Auditoría de cobros"
    _order = "run_at desc, id desc"

    name = fields.Char(string="Auditoría", compute="_compute_name")
    run_at = fields.Datetime(string="Corrió el", default=fields.Datetime.now, readonly=True)
    report_id = fields.Many2one("distri.mp.report", string="Reporte", readonly=True,
                                ondelete="set null", index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    covers_until = fields.Datetime(string="Cubre hasta", readonly=True,
                                   help="La hora del último movimiento de los reportes cargados.")
    credited_count = fields.Integer(string="Acreditados revisados", readonly=True)
    verified_count = fields.Integer(string="Verificados", readonly=True)
    pending_count = fields.Integer(string="A verificar (sin reporte aún)", readonly=True)
    difference_count = fields.Integer(string="Con diferencia", readonly=True)
    missing_count = fields.Integer(string="Sin transferencia", readonly=True)
    reversed_count = fields.Integer(string="Revertidos", readonly=True)
    annulled_count = fields.Integer(string="Anulados", readonly=True)
    review_count = fields.Integer(string="Para revisar", readonly=True)
    tied_count = fields.Integer(string="Vinculados ahora", readonly=True)
    payment_count = fields.Integer(string="Pagos electrónicos revisados", readonly=True)
    without_receipt_count = fields.Integer(string="Sin comprobante", readonly=True)
    duplicate_count = fields.Integer(string="Duplicados posibles", readonly=True)
    bank_twin_count = fields.Integer(string="Mellizos del banco propuestos", readonly=True)
    open_count = fields.Integer(string="Anomalías abiertas", readonly=True)
    alert_count = fields.Integer(string="Alertas nuevas a Gerencia", readonly=True)
    driver_cash_count = fields.Integer(string="Efectivo del repartidor sin rendir",
                                       readonly=True)
    summary = fields.Text(string="Resumen", readonly=True)

    def _compute_name(self):
        for run in self:
            run.name = "Auditoría del %s" % local_when(run.run_at)

    @api.model
    def _coverage(self, company):
        report = self.env["distri.mp.report"].sudo().search(
            [("company_id", "=", company.id), ("period_end", "!=", False)],
            order="period_end desc", limit=1)
        return report.period_end

    @api.model
    def _run(self, report=None, company=None):
        """The full audit (see the module docstring). Returns the run."""
        company = company or (report.company_id if report else self.env.company)
        coverage = self._coverage(company)
        run = self.sudo().create({"report_id": report.id if report else False,
                                  "company_id": company.id, "covers_until": coverage})
        found, stats = {}, Counter()
        if coverage:
            run._audit_credited(found, stats)
            run._audit_payments(found, stats)
            run._audit_duplicates(found, stats)
            run._audit_bank_twins(found, stats)
        # 19.0.1.129.2: by the wall clock, with or without a report
        run._audit_driver_cash(found, stats)
        run._sync_anomalies(found)
        stats["alerts"] = len(run._alert_overdue()) + len(run._alert_driver_cash())
        Anomaly = self.env["distri.mp.anomaly"].sudo()
        stats["open"] = Anomaly.search_count([("company_id", "=", company.id),
                                              ("state", "=", "open"),
                                              ("kind", "!=", "sin_comprobante")])
        run.write({
            "credited_count": stats["credited"], "verified_count": stats["verificado"],
            "pending_count": stats["pendiente"], "difference_count": stats["diferencia"],
            "missing_count": stats["sin_transferencia"], "reversed_count": stats["revertido"],
            "annulled_count": stats["anulado"], "review_count": stats["revisar"],
            "tied_count": stats["tied"], "payment_count": stats["payments"],
            "without_receipt_count": stats["sin_comprobante"],
            "duplicate_count": stats["duplicates"], "bank_twin_count": stats["bank_twins"],
            "open_count": stats["open"], "alert_count": stats["alerts"],
            "driver_cash_count": stats["driver_cash"],
        })
        run.summary = run._summary_text()
        _logger.info("Collections audit %s: %s", run.id, run.summary)
        return run

    def _summary_text(self):
        self.ensure_one()
        cash = (" %s cobros en efectivo del repartidor sin rendir." % self.driver_cash_count
                if self.driver_cash_count else "")
        if not self.covers_until:
            return "Auditoría: no hay reportes de Mercado Pago cargados.%s" % cash
        return ("Auditoría hasta el %s: %s acreditados (%s verificados, %s a verificar, %s con "
                "diferencia, %s sin transferencia, %s revertidos, %s anulados, %s para revisar; "
                "%s vinculados ahora). %s pagos electrónicos, %s sin comprobante. %s duplicados "
                "posibles. %s mellizos del banco para confirmar. %s anomalías abiertas, %s "
                "alertas nuevas a Gerencia.") % (
            local_when(self.covers_until), self.credited_count, self.verified_count,
            self.pending_count, self.difference_count, self.missing_count, self.reversed_count,
            self.annulled_count, self.review_count, self.tied_count, self.payment_count,
            self.without_receipt_count, self.duplicate_count, self.bank_twin_count,
            self.open_count, self.alert_count) + cash

    # ------------------------------------------------------------ finders

    @staticmethod
    def _card_values(card):
        return {"card_id": card.id, "partner_id": card.partner_id.commercial_partner_id.id,
                "amount": card.amount, "payment_id": card.payment_id.id,
                "credited_by_id": card.credited_by_id.id, "credited_at": card.credited_at}

    @staticmethod
    def _payment_values(payment):
        return {"payment_id": payment.id, "partner_id": payment.partner_id.commercial_partner_id.id,
                "amount": payment.amount, "credited_by_id": payment.create_uid.id,
                "credited_at": payment.create_date}

    def _audit_credited(self, found, stats):
        """Every credited receipt against every income of the reports."""
        coverage = self.covers_until
        Collection = self.env["distri.mp.collection"].sudo()
        stats["anulado"] = Collection.search_count([("kind", "=", "verify"),
                                                    ("audit_state", "=", "anulado")])
        cards = Collection.search([("kind", "=", "verify"), ("state", "=", "confirmed"),
                                   ("credited_at", "!=", False), ("payment_id", "!=", False),
                                   ("company_id", "=", self.company_id.id)], order="id")
        claimed = set(Collection.search([("kind", "=", "verify"),
                                         ("income_id", "!=", False)]).income_id.ids)
        for card in cards:
            stats["credited"] += 1
            values = self._card_values(card)
            if card.income_id:
                state = self._tied_state(card, found, values)
            else:
                state = self._untied_state(card, claimed, coverage, found, values, stats)
            if card.audit_state != state:
                card.audit_state = state
            stats[state] += 1

    def _tied_state(self, card, found, values):
        income = card.income_id
        if income.reversal:
            found["revertido:%s" % card.id] = dict(values, kind="revertido",
                                                   income_id=income.id, detail=income.reversal)
            return "revertido"
        if abs(income.amount - card.amount) > AMOUNT_TOLERANCE:
            found["diferencia:%s" % card.id] = dict(
                values, kind="diferencia", income_id=income.id,
                detail="Acreditado %s, Mercado Pago informa %s (%s)" % (
                    _money(card.amount), _money(income.amount), income.name))
            return "diferencia"
        if income.payment_id and income.payment_id != card.payment_id \
                and income.payment_id.state not in ("draft", "canceled"):
            self._double_credit(card, income, found, values)
        return "verificado"

    def _double_credit(self, card, income, found, values):
        found["doble_acreditacion:%s:%s" % (card.id, income.id)] = dict(
            values, kind="doble_acreditacion", income_id=income.id,
            payment_id=income.payment_id.id, related_payment_id=card.payment_id.id,
            detail="La transferencia %s se confirmó con el pago %s y el comprobante %s ya estaba "
                   "acreditado con el pago %s: anulá uno de los dos." % (
                       income.name, income.payment_id.name, card.name, card.payment_id.name))

    def _untied_state(self, card, claimed, coverage, found, values, stats):
        moment = card._distri_audit_moment()
        covered = bool(moment and moment <= coverage)
        level, incomes = card._distri_audit_candidates(claimed)
        if not incomes:
            if not covered:
                return "pendiente"
            found["sin_transferencia:%s" % card.id] = dict(
                values, kind="sin_transferencia",
                detail="El reporte cubre hasta el %s y no trae la transferencia de %s (%s)." % (
                    local_when(coverage), card.name, local_when(moment)))
            return "sin_transferencia"
        # an income that may be several credited receipts is a tie too
        ambiguous = len(incomes) == 1 and incomes.state == "draft" and \
            len(incomes._credited_candidates()) > 1
        if len(incomes) == 1 and level != "near" and not ambiguous:
            income = incomes
            if income.state == "draft":
                claimed.add(income.id)
                stats["tied"] += 1
                if level == "operation" and abs(income.amount - card.amount) > AMOUNT_TOLERANCE:
                    income._tie_hand_verified(card)
                    found["diferencia:%s" % card.id] = dict(
                        values, kind="diferencia", income_id=income.id,
                        detail="Acreditado %s, Mercado Pago informa %s (%s)" % (
                            _money(card.amount), _money(income.amount), income.name))
                    return "diferencia"
                income._tie_credited(card, level)
                return "revertido" if income.reversal else "verificado"
            # confirmed with a payment of its own: the customer got it twice
            self._double_credit(card, income, found, values)
            return "verificado"
        found["revisar:%s" % card.id] = dict(
            values, kind="revisar",
            income_id=incomes[:1].id,
            detail="Puede ser %s (%s): vinculalo desde la transferencia o anulá el pago." % (
                ", ".join(incomes[:3].mapped("name")), LEVEL_TEXT.get(level, level)))
        for income in incomes.filtered(lambda i: i.state == "draft" and i.proposal != "credited"):
            income._send_credited_review(card)
        stats["revisar"] += 1
        return "pendiente"

    def _scope_payments(self):
        """The electronic payments the reports cover: inbound customer
        payments of the bank journals, except «Pagos a proveedores por cuenta
        de clientes» (authorized apart, never against the report)."""
        Journal = self.env["account.journal"].sudo()
        journals = Journal.search([("type", "=", "bank"), ("company_id", "=", self.company_id.id)])
        journals -= Journal._distri_supplier_journal(self.company_id)
        # 19.0.1.129.2: the driver's cash (a cash journal anyway) never enters
        return self.env["account.payment"].sudo().search([
            ("company_id", "=", self.company_id.id), ("payment_type", "=", "inbound"),
            ("partner_type", "=", "customer"), ("state", "in", ("in_process", "paid")),
            ("journal_id", "in", journals.ids), ("date", "<=", local_day(self.covers_until)),
            ("distri_driver_cash_state", "=", False)],
            order="date, id")

    def _audit_payments(self, found, stats):
        """«Sin comprobante» and the manual payments in «Mercado Pago»
        without a transfer."""
        payments = self._scope_payments()
        stats["payments"] = len(payments)
        if not payments:
            return
        Collection = self.env["distri.mp.collection"].sudo().with_context(active_test=False)
        cards = Collection.search([("payment_id", "in", payments.ids)])
        by_payment = defaultdict(lambda: Collection.browse())
        for card in cards:
            by_payment[card.payment_id.id] |= card
        self.env.cr.execute("""
            SELECT DISTINCT res_id FROM ir_attachment
             WHERE res_model = 'account.payment' AND res_id = ANY(%s)""", [payments.ids])
        attached = {row[0] for row in self.env.cr.fetchall()}
        cobros = self.env["distri.cobro"].sudo().search([("payment_ids", "in", payments.ids)])
        cobro_receipt = {p.id for c in cobros if c.card_ids.filtered("receipt_id")
                         for p in c.payment_ids}
        mp_journal = self.env["account.journal"]._distri_mp_journal(self.company_id)
        for payment in payments:
            mine = by_payment[payment.id]
            if not (mine.filtered("receipt_id") or payment.id in attached
                    or payment.id in cobro_receipt):
                stats["sin_comprobante"] += 1
                found["sin_comprobante:%s" % payment.id] = dict(
                    self._payment_values(payment), kind="sin_comprobante",
                    card_id=mine[:1].id,
                    detail="%s en %s del %s, sin comprobante adjunto." % (
                        payment.name, payment.journal_id.name, payment.date.strftime("%d/%m/%Y")))
            if payment.journal_id == mp_journal and not mine:
                self._manual_mp_payment(payment, found)

    def _manual_mp_payment(self, payment, found):
        partner = payment.partner_id.commercial_partner_id
        incomes = self.env["distri.mp.collection"].sudo().search([
            ("kind", "=", "income"), ("state", "=", "draft"),
            ("partner_id", "child_of", partner.id),
            ("amount", ">=", payment.amount - AMOUNT_TOLERANCE),
            ("amount", "<=", payment.amount + AMOUNT_TOLERANCE),
            ("date_day", ">=", payment.date - timedelta(days=DUP_DAYS)),
            ("date_day", "<=", payment.date + timedelta(days=DUP_DAYS))])
        values = self._payment_values(payment)
        if incomes:
            found["revisar:pago:%s" % payment.id] = dict(
                values, kind="revisar", income_id=incomes[:1].id,
                detail="El pago %s se cargó a mano en Mercado Pago y puede ser %s: no lo "
                       "confirmes como otro pago." % (payment.name, ", ".join(
                           incomes[:3].mapped("name"))))
        else:
            found["manual_sin_transferencia:%s" % payment.id] = dict(
                values, kind="manual_sin_transferencia",
                detail="El pago %s se cargó a mano en Mercado Pago y el reporte no trae su "
                       "transferencia." % payment.name)

    def _audit_duplicates(self, found, stats):
        """The same operation credited twice; the same customer and amount
        (±$1) within ±2 days that are not two distinct transfers."""
        Collection = self.env["distri.mp.collection"].sudo()
        cards = Collection.search([("kind", "=", "verify"), ("state", "=", "confirmed"),
                                   ("credited_at", "!=", False), ("operation", "!=", False),
                                   ("company_id", "=", self.company_id.id)], order="id")
        by_operation = defaultdict(lambda: Collection.browse())
        for card in cards:
            by_operation[digits(card.operation)] |= card
        before = len(found)
        for operation, group in by_operation.items():
            if operation and len(group) > 1:
                first = group[0]
                for other in group[1:]:
                    found["operacion_repetida:%s:%s" % (first.id, other.id)] = dict(
                        self._card_values(other), kind="operacion_repetida",
                        related_card_id=first.id, related_payment_id=first.payment_id.id,
                        detail="La operación %s está acreditada en %s y en %s." % (
                            operation, first.name, other.name))
        payments = self._scope_payments()
        sources = defaultdict(set)
        for income in Collection.search([("kind", "=", "income"),
                                         ("payment_id", "in", payments.ids)]):
            sources[income.payment_id.id].add(income.source_id)
        by_partner = defaultdict(list)
        for payment in payments:
            by_partner[payment.partner_id.commercial_partner_id.id].append(payment)
        for group in by_partner.values():
            for i, first in enumerate(group):
                for second in group[i + 1:]:
                    if (second.date - first.date).days > DUP_DAYS:
                        break
                    if abs(second.amount - first.amount) > AMOUNT_TOLERANCE:
                        continue
                    one, two = sources[first.id], sources[second.id]
                    if one and two and one.isdisjoint(two):
                        continue  # two distinct transfers of the report
                    found["cliente_importe:%s:%s" % (first.id, second.id)] = dict(
                        self._payment_values(second), kind="cliente_importe",
                        related_payment_id=first.id,
                        detail="%s (%s, %s) y %s (%s, %s): mismo cliente e importe; revisá que "
                               "no sea la misma transferencia cargada dos veces." % (
                                   first.name, first.journal_id.name,
                                   first.date.strftime("%d/%m"), second.name,
                                   second.journal_id.name, second.date.strftime("%d/%m")))
        stats["duplicates"] = len(found) - before

    def _audit_bank_twins(self, found, stats):
        """The incomes that may be a payment registered by hand in «Banco»:
        proposed, never tied (19.0.1.129.0)."""
        Collection = self.env["distri.mp.collection"].sudo()
        Journal = self.env["account.journal"].sudo()
        excluded = Journal._distri_mp_journal(self.company_id) | \
            Journal._distri_supplier_journal(self.company_id)
        manual = self.env["account.payment"].sudo().search([
            ("company_id", "=", self.company_id.id), ("payment_type", "=", "inbound"),
            ("partner_type", "=", "customer"), ("state", "in", ("in_process", "paid")),
            ("journal_id.type", "=", "bank"), ("journal_id", "not in", excluded.ids)])
        partners = manual.partner_id.commercial_partner_id
        candidates = Collection.search([("kind", "=", "income"), ("state", "=", "draft"),
                                        ("partner_id", "child_of", partners.ids or [0])])
        proposed = Collection.search([("bank_twin_payment_id", "!=", False)])
        for income in candidates | proposed:
            twins = income._bank_twin_payments() if income.state == "draft" else \
                self.env["account.payment"]
            twin = twins if len(twins) == 1 else self.env["account.payment"]
            if income.bank_twin_payment_id != twin:
                income.bank_twin_payment_id = twin
            for payment in twins:
                stats["bank_twins"] += 1
                found["mellizo_banco:%s:%s" % (income.id, payment.id)] = dict(
                    self._payment_values(payment), kind="mellizo_banco", income_id=income.id,
                    detail="La transferencia %s del reporte puede ser el pago %s cargado a mano "
                           "en %s: confirmalo desde la transferencia." % (
                               income.name, payment.name, payment.journal_id.name))

    def _audit_driver_cash(self, found, stats):
        """19.0.1.129.2: the driver's cash still «a rendir» after the days of
        the parameter (see the module docstring)."""
        days = driver_cash_days(self.env)
        for payment in overdue_driver_cash(self.env, self.company_id):
            route = payment.distri_driver_route_id
            stats["driver_cash"] += 1
            found["efectivo_a_rendir:%s" % payment.id] = dict(
                self._payment_values(payment), kind="efectivo_a_rendir",
                credited_by_id=route.driver_id.id or payment.create_uid.id,
                detail="%s de %s cobrado por %s en %s el %s: sigue a rendir después de %s %s." % (
                    payment.name, _money(payment.amount), route.driver_id.name or "",
                    route.name or "la ruta", local_when(payment.create_date), days,
                    "día" if days == 1 else "días"))

    # ------------------------------------------------------------ anomalies

    def _sync_anomalies(self, found):
        Anomaly = self.env["distri.mp.anomaly"].sudo().with_context(
            mail_create_nolog=True, tracking_disable=True)
        now = fields.Datetime.now()
        existing = {a.key: a for a in Anomaly.search([("key", "in", list(found))])}
        new = []
        for key, values in found.items():
            values = dict(values, audit_id=self.id, last_seen=now)
            anomaly = existing.get(key)
            if not anomaly:
                new.append(dict(values, key=key, company_id=self.company_id.id,
                                first_seen=now))
                continue
            if anomaly.state == "resolved":
                values.update(state="open", resolved_at=False, overdue=False)
            elif anomaly.state != "open":
                values = {"audit_id": self.id, "last_seen": now}
            anomaly.write(values)
        if new:
            Anomaly.create(new)
        gone = Anomaly.search([("company_id", "=", self.company_id.id), ("state", "=", "open"),
                               ("key", "not in", list(found) or [""])])
        if gone:
            gone.write({"state": "resolved", "resolved_at": now})
            for anomaly in gone:
                anomaly._close_alerts("La auditoría del %s ya no la encuentra." % local_when(now))

    def _alert_overdue(self):
        """A credited payment still without its transfer when the coverage
        passed its crediting by the alert days: an activity for each Gerencia
        user on the anomaly and a post on the receipt card."""
        self.ensure_one()
        if not self.covers_until:
            return self.env["distri.mp.anomaly"]
        days = alert_days(self.env)
        limit = self.covers_until - timedelta(days=days)
        anomalies = self.env["distri.mp.anomaly"].sudo().search([
            ("company_id", "=", self.company_id.id), ("state", "=", "open"),
            ("kind", "in", ALERT_KINDS), ("overdue", "=", False),
            ("credited_at", "!=", False), ("credited_at", "<=", limit)])
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        users = gerencia_users(self.env)
        quiet = self.env["distri.mp.anomaly"].with_context(
            mail_activity_quick_update=True, mail_notrack=True).sudo()
        for anomaly in anomalies:
            text = ("%s: %s de %s, acreditado el %s por %s. El reporte ya cubre %s días desde "
                    "entonces y no trae la transferencia. Revisalo y, si es un comprobante falso, "
                    "anulá el pago.") % (
                ALERT_SUMMARY, _money(anomaly.amount), anomaly.partner_id.name or "sin cliente",
                local_when(anomaly.credited_at), anomaly.credited_by_id.name or "", days)
            anomaly.overdue = True
            if todo:
                for user in users:
                    quiet.browse(anomaly.id).activity_schedule(
                        activity_type_id=todo.id, summary=ALERT_SUMMARY, user_id=user.id,
                        note=text)
            anomaly.message_post(body=text)
            if anomaly.card_id:
                anomaly.card_id.message_post(body=text)
        return anomalies

    def _alert_driver_cash(self):
        """19.0.1.129.2: each new «efectivo_a_rendir» anomaly is an activity
        for every Gerencia user and a post on the payment, once."""
        self.ensure_one()
        anomalies = self.env["distri.mp.anomaly"].sudo().search([
            ("company_id", "=", self.company_id.id), ("state", "=", "open"),
            ("kind", "=", "efectivo_a_rendir"), ("overdue", "=", False)])
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        users = gerencia_users(self.env)
        quiet = self.env["distri.mp.anomaly"].with_context(
            mail_activity_quick_update=True, mail_notrack=True).sudo()
        for anomaly in anomalies:
            text = ("%s: %s. El cliente ya lo tiene acreditado; falta que un cajero reciba la "
                    "ruta del repartidor.") % (DRIVER_CASH_ALERT_SUMMARY, anomaly.detail or "")
            anomaly.overdue = True
            if todo:
                for user in users:
                    quiet.browse(anomaly.id).activity_schedule(
                        activity_type_id=todo.id, summary=DRIVER_CASH_ALERT_SUMMARY,
                        user_id=user.id, note=text)
            anomaly.message_post(body=text)
            if anomaly.payment_id:
                anomaly.payment_id.message_post(body=text)
        return anomalies

    def action_open_anomalies(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "distri.mp.anomaly",
                "name": "Anomalías", "view_mode": "list,form",
                "domain": [("audit_id", "=", self.id)]}

    @api.model
    def action_run_now(self):
        """«Auditar ahora» (the same audit as on a report upload)."""
        _check_audit(self.env)
        run = self._run()
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": run.id,
                "view_mode": "form"}

    @api.model
    def _cron_audit(self):
        """Daily: the audit with the coverage as it is (only the alerts can
        change without a new report), then the summary per person."""
        for company in self.env["res.company"].sudo().search([]):
            # 19.0.1.129.2: the driver's cash is checked without a report too
            if self._coverage(company) or overdue_driver_cash(self.env, company) or \
                    self.env["distri.mp.anomaly"].sudo().search_count([
                        ("company_id", "=", company.id), ("state", "=", "open"),
                        ("kind", "=", "efectivo_a_rendir")]):
                self.with_company(company)._run(company=company)
        self.env["distri.credit.summary"]._cron_daily()


LEVEL_TEXT = {
    "operation": "mismo número de operación",
    "operation_close": "número de operación con un dígito distinto y mismo importe",
    "time": "mismo importe y hora",
    "customer": "mismo importe, cliente y día",
    "near": "mismo cliente e importe con hasta dos días de diferencia",
}


# ====================================================================== report hook


class DistriMpReportAudit(models.Model):
    _inherit = "distri.mp.report"

    audit_id = fields.Many2one("distri.mp.audit", string="Auditoría", readonly=True)

    def _load_rows(self, rows):
        """Every upload runs the full audit (19.0.1.129.0)."""
        result = super()._load_rows(rows)
        try:
            with self.env.cr.savepoint():
                run = self.env["distri.mp.audit"].sudo()._run(report=self)
            self.write({"audit_id": run.id,
                        "summary": "%s\n%s" % (self.summary or "", run.summary)})
        except Exception:
            _logger.exception("Collections audit failed after report %s", self.id)
            self.summary = "%s\nLa auditoría de cobros falló: revisá el log del servidor." % (
                self.summary or "")
        return result


# ====================================================================== daily summary


class DistriCreditSummary(models.Model):
    _name = "distri.credit.summary"
    _description = "Resumen diario de lo acreditado"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char(string="Resumen", compute="_compute_name", store=True)
    date = fields.Date(string="Día", required=True, readonly=True, index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    line_ids = fields.One2many("distri.credit.summary.line", "summary_id", string="Por persona",
                               readonly=True)
    count = fields.Integer(string="Acreditados", readonly=True)
    amount = fields.Monetary(string="Total acreditado", readonly=True)
    anomaly_count = fields.Integer(string="Con anomalías", readonly=True)
    text = fields.Text(string="Detalle", readonly=True)

    _day_unique = models.Constraint("unique(date, company_id)", "Ya hay un resumen de ese día.")

    @api.depends("date")
    def _compute_name(self):
        for rec in self:
            rec.name = "Acreditado el %s" % (rec.date.strftime("%d/%m/%Y") if rec.date else "")

    @api.model
    def _build(self, day, company=None):
        """The summary of `day` (rebuilt if it exists)."""
        company = company or self.env.company
        start, end = day_bounds(day)
        Collection = self.env["distri.mp.collection"].sudo()
        cards = Collection.with_context(active_test=False).search([
            ("kind", "=", "verify"), ("credited_at", ">=", start), ("credited_at", "<", end),
            ("company_id", "=", company.id)])
        supplier = self.env["distri.supplier.payment"].sudo().search([
            ("posted_at", ">=", start), ("posted_at", "<", end), ("state", "=", "posted"),
            ("company_id", "=", company.id)])
        anomalous = set(self.env["distri.mp.anomaly"].sudo().search([
            ("card_id", "in", cards.ids), ("state", "in", ("open", "annulled")),
            ("kind", "!=", "sin_comprobante")]).card_id.ids)
        lines = {}
        for card in cards:
            line = lines.setdefault(card.credited_by_id.id, Counter())
            line["count"] += 1
            line["amount"] += card.amount
            line["verified"] += card.audit_state == "verificado"
            line["pending"] += card.audit_state == "pendiente"
            line["anomaly"] += card.id in anomalous or card.audit_state in (
                "diferencia", "sin_transferencia", "revertido", "anulado")
        for record in supplier:
            line = lines.setdefault(record.loaded_by_id.id, Counter())
            line["supplier"] += 1
            line["supplier_amount"] += record.amount
        summary = self.sudo().search([("date", "=", day), ("company_id", "=", company.id)])
        if not summary:
            summary = self.sudo().create({"date": day, "company_id": company.id})
        Users = self.env["res.users"].sudo()
        text = []
        values = []
        for user_id, line in sorted(lines.items(), key=lambda kv: -kv[1]["amount"]):
            values.append((0, 0, {
                "user_id": user_id, "count": line["count"], "amount": line["amount"],
                "verified_count": line["verified"], "pending_count": line["pending"],
                "anomaly_count": line["anomaly"], "supplier_count": line["supplier"],
                "supplier_amount": line["supplier_amount"]}))
            text.append("%s: %s acreditados por %s (%s verificados, %s a verificar, %s con "
                        "anomalías)%s." % (
                            Users.browse(user_id).name or "?", line["count"],
                            _money(line["amount"]), line["verified"], line["pending"],
                            line["anomaly"],
                            "; %s pagos a proveedores por %s" % (
                                line["supplier"], _money(line["supplier_amount"]))
                            if line["supplier"] else ""))
        summary.write({
            "line_ids": [(5, 0, 0)] + values,
            "count": len(cards), "amount": sum(cards.mapped("amount")),
            "anomaly_count": sum(v[2]["anomaly_count"] for v in values),
            "text": "\n".join(text) or "Nadie acreditó comprobantes ese día.",
        })
        return summary

    @api.model
    def _cron_daily(self, day=None):
        """Every evening: today's summary, and yesterday's if the cron missed
        it; an activity for Gerencia when someone credited."""
        today = day or today_ar()
        done = self.browse()
        for company in self.env["res.company"].sudo().search([]):
            for target in (today - timedelta(days=1), today):
                exists = self.sudo().search([("date", "=", target),
                                             ("company_id", "=", company.id)])
                if target != today and exists:
                    continue
                summary = self._build(target, company)
                summary._notify_gerencia()
                done |= summary
        return done

    def _notify_gerencia(self):
        self.ensure_one()
        if not self.count and not self.line_ids:
            return
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        body = "%s: %s comprobantes por %s.\n%s" % (self.name, self.count, _money(self.amount),
                                                     self.text)
        self.message_post(body=Markup("<br/>").join(body.split("\n")))
        if not todo:
            return
        quiet = self.with_context(mail_activity_quick_update=True, mail_notrack=True).sudo()
        for user in gerencia_users(self.env):
            if self.activity_ids.filtered(lambda a: a.user_id == user
                                          and a.summary == DAILY_SUMMARY):
                continue
            quiet.activity_schedule(activity_type_id=todo.id, summary=DAILY_SUMMARY,
                                    user_id=user.id, note=body)


class DistriCreditSummaryLine(models.Model):
    _name = "distri.credit.summary.line"
    _description = "Lo acreditado por una persona en el día"
    _order = "amount desc, id"

    summary_id = fields.Many2one("distri.credit.summary", required=True, ondelete="cascade",
                                 index=True)
    date = fields.Date(related="summary_id.date", store=True)
    currency_id = fields.Many2one(related="summary_id.currency_id")
    user_id = fields.Many2one("res.users", string="Persona", readonly=True)
    count = fields.Integer(string="Acreditados", readonly=True)
    amount = fields.Monetary(string="Total", readonly=True)
    verified_count = fields.Integer(string="Verificados", readonly=True)
    pending_count = fields.Integer(string="A verificar", readonly=True)
    anomaly_count = fields.Integer(string="Con anomalías", readonly=True)
    supplier_count = fields.Integer(string="Pagos a proveedores", readonly=True)
    supplier_amount = fields.Monetary(string="Importe a proveedores", readonly=True)
