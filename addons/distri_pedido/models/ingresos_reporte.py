"""Report incomes and the migration board: only Sistemas (19.0.1.129.11).

Since v126 the receipt is credited when it arrives («Acreditar», «Subir y
acreditar», «Cobrar») and the Mercado Pago report is a later audit (v129).
Confirming an income of the report before its receipt arrives creates a
payment that later collides with the receipt and leaves the customer with a
wrong balance. The same holds for the «Tablero de migración»: its old cards
need an account analysis (later AJC and audit adjustments) only the owner
does.

`grupo_ingresos_reporte` is the only group that:

- turns an income of the report into a payment: every path ends in
  `_confirm_todo` / `_confirm_one` (models/cobros.py), gated here, plus a
  gate on each entry point so the message comes before any wizard or write
  («Confirmar», «Confirmar todos», «Confirmar los elegidos», «Asignar y
  confirmar», «Aprobar» and «Aprobar sin comprobante» of the statement,
  «Confirmar: es otro pago», «Confirmar: no es ese pago», «Confirmar» of the
  review per customer, «Venta sin registrar» of a pending income);
- approves anything from the migration board.

Superuser mode (crons, scripts, the auto ties) passes: the report import,
`_match_hand_verified` / `_match_credited` and the audit tie without any
payment and never reach these gates anyway. The ties of a person («Es un
comprobante acreditado», «Es el pago cargado a mano») create no payment and
stay with grupo_cobros.

The board («Tablero») hides the pending incomes from the rest
(`board_visible`, in the action's domain and the counters), except the ones
waiting for a tie, which a person without this group may still resolve.

19.0.1.129.13 (owner): the risk is an income confirmed WITHOUT the
customer's receipt. An income whose receipt is already tied to it
(`_distri_receipt_tied`: the receipt is «Con ingreso» with this very income
and names the same customer) is confirmed again by grupo_cobros and Settings
(`can_authorize_money`, the groups before 19.0.1.129.11): «Confirmar»
(card, form, list, chosen ones), «Asignar y confirmar», «Aprobar» of the
statement, «Confirmar» of the review per customer and the notice wizard of
«Confirmar». They see those incomes on the board too (`can_confirm_income`).
Everything else stays with grupo_ingresos_reporte: an income without its
receipt, «Confirmar todos», «Aprobar sin comprobante», «Confirmar: es otro
pago», «Confirmar: no es ese pago», «Venta sin registrar» of a pending
income and the migration board; so does a call that carries the context of
those paths (STRICT_CONTEXT_KEYS).

«Subir y acreditar» of a receipt whose transfer is already in the report
(the receipt ties to the pending income, so there is nothing to credit)
confirms that income in the same step when the match is sure
(`_distri_upload_confirm_income`): the same operation number, or the same
amount and customer within RECEIPT_TIME_CLOSE of the time read on the
receipt. The receipt's card ends as a credited receipt tied to the report
(`audit_state` «verificado»), the shape of a receipt credited first and
tied by the report later; one payment only.
"""
import logging

import psycopg2

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .acreditar import can_approve, can_credit
from .cobros import (
    AMOUNT_TOLERANCE, RECEIPT_TIME_CLOSE, can_authorize_money, digits, local_day, own_account,
)
from .ocr_comprobante import CONFIDENCE_HIGH
from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

INCOME_GROUP = "distri_pedido.grupo_ingresos_reporte"
INCOME_DENIED = ("Por ahora los ingresos del reporte de Mercado Pago los confirma solo Sistemas: "
                 "esperá el comprobante del cliente.")
MIGRATION_DENIED = ("Por ahora el tablero de migración lo aprueba solo Sistemas: antes hay que "
                    "revisar la cuenta del cliente (ajustes posteriores que ya pudieron contar "
                    "esos comprobantes).")
# 19.0.1.129.13: set only by the paths that stay with the group («es otro
# pago», «no es ese pago», «Aprobar sin comprobante», «Venta sin registrar»)
STRICT_CONTEXT_KEYS = ("distri_not_bank_twin", "distri_not_credited", "distri_vouched_ids",
                       "distri_skip_auto_apply")
UPLOAD_CONFIRMED = ("Acreditado: %s a %s. La transferencia ya estaba en el reporte de Mercado "
                    "Pago (%s): queda verificada.")


def can_confirm_report_income(env):
    """Superuser mode (crons, scripts, a flow already gated) or the group."""
    return bool(env.su or env.user.has_group(INCOME_GROUP))


def staff_may_confirm(env, cards, partner=None):
    """19.0.1.129.13: grupo_cobros and Settings confirm `cards` when every
    one has its customer's receipt tied (`_distri_receipt_tied`). Never an
    empty set, never with the context of a path kept for the group."""
    if not cards or not can_authorize_money(env):
        return False
    if any(env.context.get(key) for key in STRICT_CONTEXT_KEYS):
        return False
    return all(card._distri_receipt_tied(partner) for card in cards)


def check_report_income(env, cards=None, partner=None):
    """`cards` given: the incomes about to be confirmed, which the staff may
    confirm when their receipt is tied; without them, the group only."""
    if can_confirm_report_income(env):
        return
    if cards is not None and staff_may_confirm(env, cards, partner):
        return
    raise AccessError(INCOME_DENIED)


def check_migration(env):
    if not can_confirm_report_income(env):
        raise AccessError(MIGRATION_DENIED)


class DistriMpCollectionIncomeGate(models.Model):
    _inherit = "distri.mp.collection"

    # the kanban hides «Confirmar» with it: `groups` on a button of a
    # t-if/t-elif chain would leave the next t-elif orphaned. 19.0.1.129.13:
    # per card (the staff confirm an income with its receipt tied)
    can_confirm_income = fields.Boolean(string="Puede confirmar este ingreso del reporte",
                                        compute="_compute_can_confirm_income")
    # «Venta sin registrar» of a pending income stays with the group
    can_confirm_income_any = fields.Boolean(string="Puede confirmar ingresos del reporte",
                                            compute="_compute_can_confirm_income")
    board_visible = fields.Boolean(string="En el tablero", compute="_compute_board_visible",
                                   search="_search_board_visible")

    @api.depends_context("uid")
    @api.depends("kind", "state", "partner_id", "receipt_id")
    def _compute_can_confirm_income(self):
        allowed = can_confirm_report_income(self.env)
        for card in self:
            card.can_confirm_income_any = allowed
            card.can_confirm_income = allowed or (
                card.kind == "income" and card.state == "draft"
                and staff_may_confirm(self.env, card))

    def _distri_receipt_tied(self, partner=None):
        """19.0.1.129.13: a pending income of the report whose customer's
        receipt is already tied to it: the receipt is «Con ingreso» with this
        very income, and the customer of the receipt is the income's
        (`partner` instead, for «Asignar y confirmar»). Read as superuser:
        the board user may not read the receipts."""
        self.ensure_one()
        card = self.sudo()
        receipt = card.receipt_id
        customer = (partner or card.partner_id).commercial_partner_id
        return bool(
            card.kind == "income" and card.state == "draft" and card.source_id
            and customer and receipt and receipt.distri_is_receipt
            and receipt.distri_receipt_state == "matched"
            and receipt.distri_collection_id == card
            and receipt.distri_partner_id.commercial_partner_id == customer)

    @api.model
    def _board_hidden_domain(self):
        """The pending incomes of the report, except the ones waiting for a
        tie (a credited receipt or a payment registered by hand)."""
        return [("kind", "=", "income"), ("state", "=", "draft"),
                ("proposal", "!=", "credited"), ("bank_twin_payment_id", "=", False)]

    @api.model
    def _board_receipt_tied_ids(self):
        """19.0.1.129.13: the pending incomes of the hidden domain whose
        receipt is tied (`_distri_receipt_tied`): on everybody's board."""
        cards = self.sudo().search(self._board_hidden_domain() + [("receipt_id", "!=", False)])
        return cards.filtered(lambda c: c._distri_receipt_tied()).ids

    @api.depends_context("uid")
    @api.depends("kind", "state", "proposal", "bank_twin_payment_id", "partner_id", "receipt_id")
    def _compute_board_visible(self):
        allowed = can_confirm_report_income(self.env)
        for card in self:
            card.board_visible = allowed or not (
                card.kind == "income" and card.state == "draft"
                and card.proposal != "credited" and not card.bank_twin_payment_id) \
                or card._distri_receipt_tied()

    def _search_board_visible(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            return NotImplemented
        visible = (operator == "=") == value
        if can_confirm_report_income(self.env):
            return [] if visible else [("id", "=", 0)]
        hidden = self._board_hidden_domain()
        tied = self._board_receipt_tied_ids()
        if visible:
            return ["|", ("id", "in", tied), "!", "&", "&", "&", *hidden]
        return hidden + [("id", "not in", tied)]

    # ------------------------------------------------------------ gates

    def _pending_incomes(self):
        return self.filtered(lambda c: c.kind == "income" and c.state == "draft")

    def _confirm_todo(self):
        check_report_income(self.env, self._pending_incomes())
        return super()._confirm_todo()

    def _confirm_one(self):
        check_report_income(self.env, self)
        return super()._confirm_one()

    @api.model
    def _ready_to_confirm(self):
        check_report_income(self.env)
        return super()._ready_to_confirm()

    def action_confirm_board(self):
        check_report_income(self.env, self._pending_incomes())
        return super().action_confirm_board()

    def action_approve_candidate(self):
        check_report_income(self.env, self._pending_incomes())
        return super().action_approve_candidate()

    def action_open_vouch(self):
        check_report_income(self.env)
        return super().action_open_vouch()

    def action_confirm_other_payment(self):
        check_report_income(self.env)
        return super().action_confirm_other_payment()

    def action_confirm_not_bank_twin(self):
        check_report_income(self.env)
        return super().action_confirm_not_bank_twin()

    def action_unrecorded_sale(self):
        # a pending income is confirmed first (with sudo, in the wizard)
        if self.filtered(lambda c: c.kind == "income" and c.state == "draft"):
            check_report_income(self.env)
        return super().action_unrecorded_sale()

    def action_migration_approve(self):
        check_migration(self.env)
        return super().action_migration_approve()


class DistriMpAssignIncomeGate(models.TransientModel):
    _inherit = "distri.mp.assign"

    can_confirm_income = fields.Boolean(string="Puede confirmar el ingreso",
                                        compute="_compute_can_confirm_income")

    @api.depends_context("uid")
    @api.depends("collection_id", "partner_id")
    def _compute_can_confirm_income(self):
        allowed = can_confirm_report_income(self.env)
        for wizard in self:
            wizard.can_confirm_income = allowed or staff_may_confirm(
                self.env, wizard.collection_id, wizard.partner_id)

    def action_assign_confirm(self):
        # the customer chosen here is the one the receipt must name
        check_report_income(self.env, self.collection_id, self.partner_id)
        return super().action_assign_confirm()


class DistriMpConfirmNoticeIncomeGate(models.TransientModel):
    _inherit = "distri.mp.confirm.notice"

    def action_apply(self):
        # «Confirmar todos» («ready») stays with the group
        check_report_income(self.env, self.collection_ids if self.mode == "selected" else None)
        return super().action_apply()


class DistriMpVouchIncomeGate(models.TransientModel):
    _inherit = "distri.mp.vouch"

    def action_apply(self):
        check_report_income(self.env)
        return super().action_apply()


class DistriBalanceReviewLineIncomeGate(models.Model):
    _inherit = "distri.balance.review.line"

    can_confirm_income = fields.Boolean(string="Puede confirmar el ingreso",
                                        compute="_compute_can_confirm_income")

    @api.depends_context("uid")
    @api.depends("collection_id")
    def _compute_can_confirm_income(self):
        allowed = can_confirm_report_income(self.env)
        for line in self:
            line.can_confirm_income = allowed or staff_may_confirm(
                self.env, line.collection_id)

    def action_confirm_card(self):
        check_report_income(self.env, self.mapped("collection_id")._pending_incomes())
        return super().action_confirm_card()


class DistriUnrecordedSaleWizardIncomeGate(models.TransientModel):
    _inherit = "distri.unrecorded.sale.wizard"

    def action_apply(self):
        # the card is confirmed with sudo there: the gate goes first
        if self.collection_id and self.collection_id.state == "draft":
            check_report_income(self.env)
        return super().action_apply()


class DistriMigrationSummaryGate(models.TransientModel):
    _inherit = "distri.migration.summary"

    def action_batch_approve(self):
        check_migration(self.env)
        return super().action_batch_approve()


class DistriMigrationApproveGate(models.TransientModel):
    _inherit = "distri.migration.approve"

    @api.model
    def _open(self, cards, mode):
        check_migration(self.env)
        return super()._open(cards, mode)

    def action_approve(self):
        check_migration(self.env)
        return super().action_approve()


class WhatsappMessageUploadConfirm(models.Model):
    """19.0.1.129.13: «Subir y acreditar» after the report (see the module
    docstring)."""
    _inherit = "distri.whatsapp.message"

    def _distri_upload_credit_now(self, partner, amount=None, confirmed=False):
        result = super()._distri_upload_credit_now(partner, amount=amount, confirmed=confirmed)
        if result.get("credit") != "pendiente":
            return result
        return self._distri_upload_confirm_income(partner, amount) or result

    def _distri_upload_sure_income(self, partner, amount=None):
        """The pending income this just uploaded receipt tied to, when the
        tie is sure and `self.env.user` may credit it; else an empty
        recordset. Sure: the same operation number, or the same amount and
        customer within RECEIPT_TIME_CLOSE of the time read on the receipt.
        An amount given, typed or read with confidence must be the
        report's."""
        self.ensure_one()
        msg = self.sudo()
        Collection = self.env["distri.mp.collection"].sudo()
        user = self.env.user
        partner = partner.commercial_partner_id if partner else partner
        income = msg.distri_collection_id
        # the staff upload with a customer, the way it credits (the flow «staff»)
        if not (partner and can_credit(user) and msg.distri_forwarded_by_id
                and not msg.distri_from_app and not msg.distri_app_stop_id
                and not msg.distri_sender_note and not msg.distri_conflict):
            return Collection
        if not (income and income._distri_receipt_tied()
                and income.partner_id.commercial_partner_id == partner):
            return Collection
        if msg._distri_other_account(own_account(self.env)):
            return Collection
        # a receipt older than yesterday: the approvers, as when crediting
        day = local_day(msg.distri_receipt_date or msg.timestamp)
        if day and (today_ar() - day).days > 1 and not can_approve(user):
            return Collection
        read = msg.distri_read_amount if msg.distri_amount_confidence == CONFIDENCE_HIGH else 0
        known = amount or msg.distri_amount_typed or read or msg.distri_amount
        if known and abs(known - income.amount) > AMOUNT_TOLERANCE:
            return Collection
        operation = digits(msg.distri_operation)
        numbers = {digits(n) for n in (income.source_id, income.bank_transfer_id) if n}
        by_operation = bool(operation) and operation in numbers
        read_at = msg.distri_receipt_date
        by_time = bool(known and read_at and income.date
                       and abs(income.date - read_at) <= RECEIPT_TIME_CLOSE)
        return income if by_operation or by_time else Collection

    def _distri_upload_confirm_income(self, partner, amount=None):
        """Confirms the sure income (`_distri_upload_sure_income`) with every
        guard of a confirmation (`_confirm_todo`: a credited receipt or a
        manual bank payment that may be the same transfer stops it), then
        leaves the receipt's card credited and tied to it («verificado»).
        Returns the upload result, or None to leave the receipt pending."""
        self.ensure_one()
        income = self._distri_upload_sure_income(partner, amount)
        if not income:
            return None
        msg = self.sudo()
        user = self.env.user
        partner = partner.commercial_partner_id.sudo()
        try:
            with self.env.cr.savepoint():
                # sudo keeps the user: he confirms, his own groups were checked above
                income = income.sudo()
                income._confirm_todo()
                income._confirm_one()
                card = msg._distri_upload_verified_card(income)
        except (UserError, AccessError, psycopg2.Error) as exc:
            _logger.info("Upload %s: income %s not confirmed in one step: %s",
                         msg.id, income.id, exc)
            return None
        partner.message_post(body="Comprobante acreditado: %s por %s (%s), ya en el reporte de "
                                  "Mercado Pago (%s)." % (_money(income.amount), user.name,
                                                          card.name, income.name))
        _logger.info("Upload %s: income %s confirmed by %s with the receipt (payment %s)",
                     msg.id, income.id, user.login, income.payment_id.id)
        return {"credit": "acreditado",
                "message": UPLOAD_CONFIRMED % (_money(income.amount), partner.name, income.name)}

    def _distri_upload_verified_card(self, income):
        """The receipt's «verify» card, credited by the user with the
        income's payment and tied to it: what a receipt credited first and
        tied by the report later looks like (`_tie_hand_verified`)."""
        self.ensure_one()
        msg = self.sudo()
        Collection = self.env["distri.mp.collection"].sudo()
        user = self.env.user
        card = msg._distri_verify_card()
        if not card:
            card = Collection.create({"kind": "verify", "receipt_id": msg.id})
        card._fill_from_receipt(msg)
        now = fields.Datetime.now()
        card.write({
            "state": "confirmed",
            "partner_id": income.partner_id.commercial_partner_id.id,
            "amount": income.amount,
            "payment_id": income.payment_id.id,
            "income_id": income.id,
            "confirmed_by_id": user.id,
            "confirmed_at": now,
            "credited_by_id": user.id,
            "credited_at": now,
            "audit_state": "verificado",
            "ignore_reason": False,
            "ignore_note": False,
            "report_review": False,
            "proposal_note": "Llegó en el reporte: %s" % income.name,
        })
        # the unique index of credited operations decides here
        card.flush_recordset()
        msg.with_context(distri_receipt_matching=True).write(
            {"distri_receipt_state": "matched", "distri_collection_id": card.id})
        text = ("Acreditado por %s al subirlo: la transferencia ya estaba en el reporte (%s), "
                "confirmada con el pago %s de %s." % (user.name, income.name,
                                                       income.payment_id.name or "",
                                                       _money(income.amount)))
        card.message_post(body=text)
        income.message_post(body="El comprobante %s llegó después del reporte y se acreditó con "
                                 "esta transferencia (%s)." % (card.name, user.name))
        return card
