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
"""
from odoo import api, fields, models
from odoo.exceptions import AccessError

INCOME_GROUP = "distri_pedido.grupo_ingresos_reporte"
INCOME_DENIED = ("Por ahora los ingresos del reporte de Mercado Pago los confirma solo Sistemas: "
                 "esperá el comprobante del cliente.")
MIGRATION_DENIED = ("Por ahora el tablero de migración lo aprueba solo Sistemas: antes hay que "
                    "revisar la cuenta del cliente (ajustes posteriores que ya pudieron contar "
                    "esos comprobantes).")


def can_confirm_report_income(env):
    """Superuser mode (crons, scripts, a flow already gated) or the group."""
    return bool(env.su or env.user.has_group(INCOME_GROUP))


def check_report_income(env):
    if not can_confirm_report_income(env):
        raise AccessError(INCOME_DENIED)


def check_migration(env):
    if not can_confirm_report_income(env):
        raise AccessError(MIGRATION_DENIED)


class DistriMpCollectionIncomeGate(models.Model):
    _inherit = "distri.mp.collection"

    # the kanban hides «Confirmar» with it: `groups` on a button of a
    # t-if/t-elif chain would leave the next t-elif orphaned
    can_confirm_income = fields.Boolean(string="Puede confirmar ingresos del reporte",
                                        compute="_compute_can_confirm_income")
    board_visible = fields.Boolean(string="En el tablero", compute="_compute_board_visible",
                                   search="_search_board_visible")

    @api.depends_context("uid")
    def _compute_can_confirm_income(self):
        allowed = can_confirm_report_income(self.env)
        for card in self:
            card.can_confirm_income = allowed

    @api.model
    def _board_hidden_domain(self):
        """The pending incomes of the report, except the ones waiting for a
        tie (a credited receipt or a payment registered by hand)."""
        return [("kind", "=", "income"), ("state", "=", "draft"),
                ("proposal", "!=", "credited"), ("bank_twin_payment_id", "=", False)]

    @api.depends_context("uid")
    @api.depends("kind", "state", "proposal", "bank_twin_payment_id")
    def _compute_board_visible(self):
        allowed = can_confirm_report_income(self.env)
        for card in self:
            card.board_visible = allowed or not (
                card.kind == "income" and card.state == "draft"
                and card.proposal != "credited" and not card.bank_twin_payment_id)

    def _search_board_visible(self, operator, value):
        if operator not in ("=", "!=") or not isinstance(value, bool):
            return NotImplemented
        visible = (operator == "=") == value
        if can_confirm_report_income(self.env):
            return [] if visible else [("id", "=", 0)]
        hidden = self._board_hidden_domain()
        return ["!", "&", "&", "&", *hidden] if visible else hidden

    # ------------------------------------------------------------ gates

    def _confirm_todo(self):
        check_report_income(self.env)
        return super()._confirm_todo()

    def _confirm_one(self):
        check_report_income(self.env)
        return super()._confirm_one()

    @api.model
    def _ready_to_confirm(self):
        check_report_income(self.env)
        return super()._ready_to_confirm()

    def action_confirm_board(self):
        check_report_income(self.env)
        return super().action_confirm_board()

    def action_approve_candidate(self):
        check_report_income(self.env)
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

    def action_assign_confirm(self):
        check_report_income(self.env)
        return super().action_assign_confirm()


class DistriMpConfirmNoticeIncomeGate(models.TransientModel):
    _inherit = "distri.mp.confirm.notice"

    def action_apply(self):
        check_report_income(self.env)
        return super().action_apply()


class DistriMpVouchIncomeGate(models.TransientModel):
    _inherit = "distri.mp.vouch"

    def action_apply(self):
        check_report_income(self.env)
        return super().action_apply()


class DistriBalanceReviewLineIncomeGate(models.Model):
    _inherit = "distri.balance.review.line"

    def action_confirm_card(self):
        check_report_income(self.env)
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
