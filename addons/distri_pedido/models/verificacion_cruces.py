"""Proposed ties of credited receipts (19.0.1.129.39, owner, 25/11/2026).
Behind distri_pedido.verificacion_v2: with it off nothing here runs and no
screen shows it.

A receipt credited by a person (a «verify» card with its payment) waits for
its transfer in the Mercado Pago report. The report's automatic tie needs
the operation number or the time within 2 minutes (models/acreditar.py
`_match_credited`), and a round amount only the operation number
(verificacion_tramo2.py). Real case of 25/11: 9 credited cards had an
income of the same amount in the report and stayed untied.

The proposal: a credited card waiting for its transfer and a report income
still pending («Pendiente», or «apartado» as a wholesaler's) are proposed
as one tie when
- the amount is the same (± $ 1, AMOUNT_TOLERANCE),
- the income's day is the receipt's day or the day before,
- nothing says they are two transfers (another operation number on the
  receipt, or two different payer CUITs), and
- each one is the ONLY candidate of the other.
A round amount is proposed too: a person clicks.

They show in queue 5 «Diferencias» (where the board's «Revisar» column
lands) with the gap in time and the amount, and «Cruzar» runs the existing
standard tie: the wizard «Es un comprobante acreditado»
(distri.mp.tie.credited `action_apply` → `_tie_credited(card, "manual",
by_hand=True)`), with its own checks (grupo_cobros, still pending, still
waiting, same amount). Never automatic, never a new payment: the income
takes the card's payment.

An income set aside by the wholesale CUIT rule («apartado») is shown with
the warning «mayorista fuera del sistema: posible doble acreditación» and
no «Cruzar»: a person decides on the backend.
"""
from datetime import timedelta

from odoo import api, models
from odoo.exceptions import UserError

from .cobros import AMOUNT_TOLERANCE, digits, local_day, local_when, operation_close
from .mayoristas import APARTADO
from .reparto import _money
from .verificacion import verificacion_on

PROPOSAL_QUEUE = "diferencias"
WHOLESALE_WARNING = "Mayorista fuera del sistema: posible doble acreditación"
GONE = "Ese cruce ya no está propuesto: el ingreso o el comprobante cambió."


def gap_text(delta):
    """«3 h 20 min» for a timedelta (its absolute value)."""
    minutes = int(abs(delta).total_seconds() // 60)
    days, rest = divmod(minutes, 24 * 60)
    hours, mins = divmod(rest, 60)
    parts = []
    if days:
        parts.append("1 día" if days == 1 else "%d días" % days)
    if hours:
        parts.append("%d h" % hours)
    if mins or not parts:
        parts.append("%d min" % mins)
    return " ".join(parts)


class DistriMpCollectionCruces(models.Model):
    _inherit = "distri.mp.collection"

    def _distri_v2_card_moment(self):
        """When the credited card's transfer was made, as far as it says:
        the date read on the receipt, else when it arrived, else the card."""
        self.ensure_one()
        receipt = self.sudo().receipt_id
        return receipt.distri_receipt_date or receipt.timestamp or self.date or self.create_date

    def _distri_v2_may_be_pair(self, income, card_day):
        """`income` may be this credited card's transfer (module docstring;
        the uniqueness is checked by the caller)."""
        card = self.sudo()
        if abs(card.amount - income.amount) > AMOUNT_TOLERANCE:
            return False
        if income.date_day not in (card_day, card_day - timedelta(days=1)):
            return False
        operation = digits(card.operation or card.receipt_id.distri_operation)
        numbers = [n for n in (income.source_id, income.bank_transfer_id) if n]
        if operation and not any(operation_close(operation, n) for n in numbers):
            # its operation number says it is another transfer
            return False
        cuit = digits(card.payer_cuit or card.receipt_id.distri_payer_cuit)
        return not (cuit and income.payer_cuit and cuit != digits(income.payer_cuit))

    @api.model
    def _distri_v2_tie_proposals(self):
        """[(card, income)]: the unique pairs (module docstring), oldest
        receipt first. Reads only; [] with the flag off."""
        if not verificacion_on(self.env):
            return []
        pool = self._distri_credited_pool().filtered(lambda c: c.amount > 0)
        if not pool:
            return []
        days = {card.id: card._distri_credit_day() for card in pool}
        incomes = self.sudo().search([
            ("kind", "=", "income"), ("state", "in", ("draft", APARTADO)),
            ("receipt_id", "=", False), ("payment_id", "=", False),
            ("bank_twin_payment_id", "=", False), ("amount", ">", 0),
            ("date_day", ">=", min(days.values()) - timedelta(days=1)),
            ("date_day", "<=", max(days.values()))])
        incomes = incomes.filtered(lambda i: not i.reversal)
        if not incomes:
            return []
        by_peso = {}
        for income in incomes:
            by_peso.setdefault(round(income.amount), []).append(income)
        of_card, of_income = {}, {}
        for card in pool:
            peso = round(card.amount)
            for income in (i for p in (peso - 1, peso, peso + 1) for i in by_peso.get(p, ())):
                if card._distri_v2_may_be_pair(income, days[card.id]):
                    of_card.setdefault(card.id, []).append(income)
                    of_income.setdefault(income.id, []).append(card)
        pairs = []
        for card in pool:
            found = of_card.get(card.id, [])
            if len(found) == 1 and len(of_income[found[0].id]) == 1:
                pairs.append((card, found[0]))
        pairs.sort(key=lambda pair: (pair[0]._distri_v2_card_moment(), pair[0].id))
        return pairs

    def _distri_v2_proposal_row(self, income, user):
        """The screen's row of the pair (this credited card, `income`)."""
        self.ensure_one()
        card = self.sudo()
        income = income.sudo()
        moment = card._distri_v2_card_moment()
        delta = (moment - income.date) if (moment and income.date) else None
        if delta is None:
            gap = ""
        elif abs(delta) < timedelta(minutes=1):
            gap = "a la misma hora"
        else:
            gap = "%s %s" % (gap_text(delta), "antes del comprobante" if delta > timedelta()
                             else "después del comprobante")
        apartado = income.state == APARTADO
        from .cobros import can_authorize_money
        return {
            "card_id": card.id,
            "income_id": income.id,
            "partner": card.partner_id.commercial_partner_id.name or "Sin cliente",
            "amount": _money(income.amount),
            "card_amount": _money(card.amount),
            "card": "%s · %s · acreditado por %s" % (
                card.name or "", local_when(moment), card.credited_by_id.name or ""),
            "income": "%s · %s%s" % (income.name or "", local_when(income.date),
                                     " · %s" % income.payer_name if income.payer_name else ""),
            "gap": gap,
            "day": local_day(income.date),
            "warning": (WHOLESALE_WARNING + (" (%s)" % income.wholesale_label
                                             if income.wholesale_label else ""))
            if apartado else "",
            "can_tie": not apartado and can_authorize_money(self.env(user=user, su=False)),
            "card_url": "/odoo/distri.mp.collection/%s" % card.id,
            "income_url": "/odoo/distri.mp.collection/%s" % income.id
            if income.with_user(user).board_visible else "",
        }

    @api.model
    def _distri_v2_proposal_rows(self, user=None):
        user = user or self.env.user
        return [card._distri_v2_proposal_row(income, user)
                for card, income in self._distri_v2_tie_proposals()]

    @api.model
    def _distri_v2_cruzar(self, card_id, income_id):
        """«Cruzar»: the pair must still be a proposal one can tie; then the
        standard wizard «Es un comprobante acreditado» as the current user
        (its own checks; no payment is created). Returns the income."""
        pairs = {(card.id, income.id): (card, income)
                 for card, income in self._distri_v2_tie_proposals()}
        pair = pairs.get((card_id, income_id))
        if not pair:
            raise UserError(GONE)
        card, income = pair
        if income.state != "draft":
            raise UserError("%s está apartado como mayorista: %s. Decidilo desde el tablero de "
                            "cobros." % (income.name, WHOLESALE_WARNING.lower()))
        Wizard = self.env["distri.mp.tie.credited"]
        wizard = Wizard.create({"income_id": income.id, "candidate_ids": [(6, 0, card.ids)],
                                "card_id": card.id})
        wizard.action_apply()
        return income
