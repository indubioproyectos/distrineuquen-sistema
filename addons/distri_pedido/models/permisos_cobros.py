"""Who authorizes money (19.0.1.73.0).

For now only the members of `distri_pedido.grupo_cobros` (and Settings,
base.group_system) authorize money: they confirm, vouch, discard and undo
the Mercado Pago cards, choose a card's customer, close the day, adjust
balances and approve the catch-up. The gates of those actions live next to
each action (`_check_cobros`, models/cobros.py).

This file closes the back door: grupo_administracion implies
account.group_account_invoice, so its members could register a customer
payment by hand in Accounting (or through «Registrar pago» on an invoice).
A customer payment (inbound, customer) is created, posted, cancelled, reset
to draft or moved to another customer only by a user who may authorize
money, or in superuser mode: the scripts run from odoo shell, the crons and
the flows that already passed their own gate and work with sudo() (the
board's confirmation, the route settlement, the depot delivery).
"""
from odoo import api, fields, models
from odoo.exceptions import AccessError

from .cobros import COBROS_DENIED, can_authorize_money

# a change of any of these on a customer payment is a change of the money
PAYMENT_MONEY_FIELDS = frozenset({
    "state", "partner_id", "amount", "payment_type", "partner_type", "journal_id",
    "currency_id", "date", "move_id", "company_id",
})
# a posted payment goes between these two by itself when it is reconciled
SETTLED_STATES = ("in_process", "paid")


class AccountPaymentCobros(models.Model):
    _inherit = "account.payment"

    def _distri_customer_inbound(self):
        return self.filtered(lambda p: p.payment_type == "inbound" and p.partner_type == "customer")

    def _distri_check_payment_access(self):
        if self._distri_customer_inbound() and not can_authorize_money(self.env):
            raise AccessError(COBROS_DENIED)

    @api.model_create_multi
    def create(self, vals_list):
        payments = super().create(vals_list)
        # checked on the records: payment_type and partner_type have defaults
        payments._distri_check_payment_access()
        return payments

    def write(self, vals):
        if PAYMENT_MONEY_FIELDS.intersection(vals) and not can_authorize_money(self.env):
            customer = self._distri_customer_inbound()
            if (customer and not customer._distri_only_settles(vals)) \
                    or vals.get("payment_type") == "inbound" \
                    or vals.get("partner_type") == "customer":
                raise AccessError(COBROS_DENIED)
        return super().write(vals)

    def _distri_only_settles(self, vals):
        """Reconciling a posted payment moves it between «En proceso» and
        «Pagado» (account.partial.reconcile): bookkeeping of a payment that
        already exists, never a new one, a cancellation or another customer."""
        return PAYMENT_MONEY_FIELDS.intersection(vals) == {"state"} \
            and vals["state"] in SETTLED_STATES \
            and all(payment.state in SETTLED_STATES for payment in self)

    def action_post(self):
        # action_post writes the state too; checked first for a clear message
        # before any journal entry is generated
        self._distri_check_payment_access()
        return super().action_post()

    def action_validate(self):
        # «Validar» marks it paid by hand: a decision, not a reconciliation
        self._distri_check_payment_access()
        return super().action_validate()


class DistriMpCollectionCobros(models.Model):
    _inherit = "distri.mp.collection"

    # the board's kanban hides the money buttons with it: `groups` on a
    # button of a t-if/t-elif chain would leave the next t-elif orphaned
    can_authorize = fields.Boolean(string="Puede autorizar cobros",
                                   compute="_compute_can_authorize")

    @api.depends_context("uid")
    def _compute_can_authorize(self):
        allowed = can_authorize_money(self.env)
        for card in self:
            card.can_authorize = allowed

    @api.model
    def distri_board_counters(self):
        """The bar on top of the board (static/src/js/cobros_board.xml)
        shows «Exportar a Online» only to who authorizes money."""
        data = super().distri_board_counters()
        data["can_authorize"] = can_authorize_money(self.env)
        return data
