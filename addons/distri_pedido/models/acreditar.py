"""Instant crediting of payment receipts (19.0.1.126.0).

Decision D11 «sin reporte no hay pago» is replaced (manager, 21/11/2026,
docs cobros/analisis-cobro-un-paso-2026-11-21.md): a receipt checked by the
staff credits the customer's account at once; the Mercado Pago report
becomes a later check.

Who credits («Acreditar comprobantes», `can_credit`): cajero, reparto,
administración contable, vendedores, gerencia and the system administrators. It is a check
over those groups, not a group of its own: nobody has to be added anywhere
and grupo_cobros is not needed. Who receives the receipt decides who may
credit it (`_distri_credit_flow`):

- «staff»: a receipt the staff uploaded themselves (/comprobantes/subir,
  a seller's WhatsApp number): anyone who may credit;
- «customer»: sent by the customer (WhatsApp, his order page, the chat): it
  never credits itself, it waits in «Para acreditar» for administración,
  gerencia or the admin (`can_approve`);
- «driver»: registered in the driver's flow (the photo of the driver app,
  linked to a stop of his route). Parameter PARAM_DRIVER_RULE:
  «inmediato» (default, manager 21/11/2026): credits by itself, in the
  driver's name, as soon as the photo is read (the background reading of
  the app photos), with every defense of the staff; what would need a
  person to confirm (an amount not read, read with doubt or different from
  the one he typed, an old date...) waits in «Para acreditar» for anyone
  who may credit. «rendicion»: never by itself, it waits for an approver,
  usually at the route settlement. «coincide»: by itself only when the
  amount the driver typed is the one read with high confidence, else it
  waits for an approver (`_distri_auto_credit_driver`).
  A user who is both driver and seller follows the driver rule for what he
  registers in the driver's flow and the seller rule elsewhere: the flow is
  decided by the receipt, never by the user's groups.

`action_credit` (the card, `distri.mp.collection` kind «verify») opens the
wizard `distri.mp.credit`; the upload screen calls `_distri_credit` directly.
Crediting:

- creates the payment in the «Mercado Pago» journal, dated on the receipt's
  day, with the memo of `_payment_memo`; an invoice of the Odoo Online
  history chosen on the card is paid the v125 way (unreconciled payment,
  `distri_history_invoice_id`, the number in the memo, never oldest first);
  a lab invoice is reconciled first; «a cuenta» goes oldest first;
- sets the card «confirmed» with `credited_by_id`, `credited_at` and
  `audit_state` «pendiente»; the receipt is «matched» to it, so no transfer
  takes it by amount and the report ties to the card instead;
- fulfils the customer's pending receipts like a confirmation does.

Defenses, before anything is written (`_distri_credit_review`): errors stop
it, warnings need the person to confirm them («¿Acreditar igual?»):

- errors: the operation already credited (or authorized) on another card,
  the same image on another receipt, a repeated receipt, a transfer already
  in the report, a destination that is not the own account, a receipt older
  than yesterday credited by someone who is not an approver;
- warnings: an amount not read, read with doubt or different from the one
  typed; the receipt older than yesterday (approvers); a manual payment in
  «Banco» that may be the same transfer; the customer left with a credit
  balance above PARAM_CREDIT_WARNING (default $ 100.000, no cap).

The report (`_match_hand_verified`): a new income ties to a credited card
(no new payment, `audit_state` «verificado») by the exact operation (the
D10 machinery, `_tie_hand_verified`), then by the operation with one OCR
slip and the same amount, then the same amount and the receipt's time
within RECEIPT_TIME_CLOSE (and the payer's CUIT when both were read), then
the same amount, the customer of the payer's known CUIT and the same day
when the candidate is unique. A tie between several goes to «Revisar»
(proposal «credited») and confirming it is refused until a person ties it
(«Es un comprobante acreditado») or says it is another payment. The v124
guard (`_bank_twin_payments`) looks only at manual payments outside the
«Mercado Pago» journal, and an income tied here is confirmed before any
confirmation runs: the two never tie the same income.

The audit of every credited receipt against the report is v129 (owner, 22/11/2026).
"""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .cobros import (
    AMOUNT_TOLERANCE, BANK_TWIN_DAYS, RECEIPT_TIME_CLOSE, _check_cobros, digits, local_day,
    local_when, operation_close, own_account,
)
from .ocr_comprobante import CONFIDENCE_HIGH, CONFIDENCE_LOW, ocr_deadline, ocr_seconds
from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

# «Acreditar comprobantes»: who credits a receipt the staff uploaded
CREDIT_GROUPS = (
    "distri_pedido.grupo_cajero",
    "distri_pedido.grupo_reparto",
    "distri_pedido.grupo_administracion",
    "distri_pedido.grupo_vendedor",
    "distri_pedido.grupo_gerencia",
    "base.group_system",
)
# who approves the receipts the customer sent (and the drivers', «rendicion»)
APPROVE_GROUPS = (
    "distri_pedido.grupo_administracion",
    "distri_pedido.grupo_gerencia",
    "base.group_system",
)
PARAM_CREDIT_WARNING = "distri_pedido.acreditar_aviso_saldo_a_favor"
DEFAULT_CREDIT_WARNING = 100000.0
PARAM_DRIVER_RULE = "distri_pedido.acreditar_repartidor"
DRIVER_RULES = ("inmediato", "rendicion", "coincide")
DEFAULT_DRIVER_RULE = "inmediato"

AUDIT_STATES = [
    ("pendiente", "A verificar con MP"),
    ("verificado", "Verificado con MP"),
    ("diferencia", "Diferencia con MP"),
    ("sin_transferencia", "Sin transferencia en MP"),
    ("revertido", "Revertido en MP"),
    ("anulado", "Anulado"),
]

CREDIT_REMINDER = "Se acredita en el momento: revisá importe, cliente y que el comprobante sea real."
CREDIT_NOTICE = (
    "Atención: cambió la carga de comprobantes. Desde ahora, cada comprobante que acreditás baja "
    "el saldo del cliente en el momento. El control con Mercado Pago se hace después. Antes de "
    "acreditar, revisá bien el importe, el cliente y que el comprobante sea real.")
CREDIT_DENIED = "Tu usuario no acredita comprobantes."
APPROVE_DENIED = {
    "customer": "Este comprobante lo mandó el cliente: lo acreditan administración, gerencia o el "
                "admin.",
    "driver": "Este comprobante lo registró el repartidor en su reparto y la regla vigente "
              "(«rendicion» o «coincide») pide que lo acrediten administración, gerencia o el "
              "admin, en la rendición.",
    # 19.0.1.126.1: uploaded with «No está en el sistema»
    "unknown": "Este comprobante se subió sin cliente («No está en el sistema»): lo asignan y lo "
               "acreditan administración, gerencia o el admin.",
}
TIE_NOTE = "Puede ser el comprobante acreditado %s (%s): vinculalo o, si es otro pago, " \
           "confirmalo como otro pago."
LEVEL_LABELS = {
    "operation": "mismo número de operación",
    "operation_close": "número de operación con un dígito distinto y mismo importe",
    "time": "mismo importe y hora del comprobante",
    "customer": "mismo importe, cliente y día",
}


def _in_groups(user, groups):
    return bool(user) and not user.share and any(user.has_group(g) for g in groups)


def can_credit(user):
    """«Acreditar comprobantes»: may credit a receipt the staff uploaded."""
    return bool(user) and (user._is_superuser() or _in_groups(user, CREDIT_GROUPS))


def can_approve(user):
    """May credit the receipts the customer sent and the drivers'."""
    return bool(user) and (user._is_superuser() or _in_groups(user, APPROVE_GROUPS))


def credit_warning_threshold(env):
    value = env["ir.config_parameter"].sudo().get_param(PARAM_CREDIT_WARNING)
    try:
        return float(value) if value not in (None, False, "") else DEFAULT_CREDIT_WARNING
    except ValueError:
        return DEFAULT_CREDIT_WARNING


def driver_rule(env):
    value = (env["ir.config_parameter"].sudo().get_param(PARAM_DRIVER_RULE) or "").strip()
    return value if value in DRIVER_RULES else DEFAULT_DRIVER_RULE


class CreditWarning(UserError):
    """The crediting needs the person to confirm `warnings` first."""

    def __init__(self, warnings):
        super().__init__("\n".join(warnings))
        self.warnings = list(warnings)


class DistriMpCollectionCredit(models.Model):
    _inherit = "distri.mp.collection"

    credited_by_id = fields.Many2one("res.users", string="Acreditó", readonly=True, copy=False)
    credited_at = fields.Datetime(string="Acreditado el", readonly=True, copy=False)
    audit_state = fields.Selection(
        AUDIT_STATES, string="Control con MP", readonly=True, copy=False,
        help="El control del comprobante acreditado contra el reporte de Mercado Pago. «A "
             "verificar» hasta que el reporte trae la transferencia.")
    credit_flow = fields.Selection(
        [("staff", "Subido por el personal"), ("customer", "Enviado por el cliente"),
         ("driver", "Registrado por el repartidor")],
        string="Origen del comprobante", compute="_compute_credit_flags")
    can_credit = fields.Boolean(string="Puede acreditar", compute="_compute_credit_flags")
    credit_reminder = fields.Char(compute="_compute_credit_flags")
    credited_label = fields.Char(string="Acreditación", compute="_compute_credit_flags")
    credited_candidate_count = fields.Integer(
        string="Acreditados posibles", compute="_compute_credited_candidate_count")

    # ------------------------------------------------------------ flags

    @api.depends("kind", "state", "receipt_id")
    @api.depends_context("uid")
    def _compute_credit_flags(self):
        user = self.env.user
        for card in self:
            flow = card._distri_credit_flow() if card.kind == "verify" else False
            card.credit_flow = flow
            card.credit_reminder = CREDIT_REMINDER
            card.can_credit = card._distri_user_can_credit(user)
            sudo = card.sudo()
            card.credited_label = (
                "Acreditado por %s el %s" % (sudo.credited_by_id.name or "",
                                             local_when(sudo.credited_at))
                if card.kind == "verify" and sudo.credited_at else False)

    @api.depends("kind", "state", "amount", "source_id", "date")
    def _compute_credited_candidate_count(self):
        for card in self:
            card.credited_candidate_count = len(card._credited_candidates()) \
                if card.kind == "income" and card.state == "draft" else 0

    def _distri_user_can_credit(self, user):
        """`user` may credit this draft «verify» card (by its flow)."""
        self.ensure_one()
        card = self.sudo()
        if card.kind != "verify" or card.state != "draft" or not card.receipt_id:
            return False
        flow = card._distri_credit_flow()
        approver_only = flow == "customer" or (
            flow == "driver" and driver_rule(self.env) != "inmediato") \
            or card._distri_unknown_sender()
        return can_approve(user) if approver_only else can_credit(user)

    def _distri_unknown_sender(self):
        """19.0.1.126.1: the receipt was uploaded with «No está en el
        sistema» (who sent it, typed): an assignment, so only the approvers
        credit it."""
        self.ensure_one()
        receipt = self.sudo().receipt_id
        return bool(receipt and receipt.distri_sender_note)

    def _distri_credit_flow(self):
        """«staff», «customer» or «driver»: decided by how the receipt
        arrived, never by the user's groups (a seller who also drives follows
        the driver rule only for what he registers in the driver's flow)."""
        self.ensure_one()
        receipt = self.sudo().receipt_id
        if receipt.distri_from_app or receipt.distri_app_stop_id:
            return "driver"
        if receipt.distri_forwarded_by_id:
            return "staff"
        return "customer"

    # ------------------------------------------------------------ review

    def _distri_credit_day(self):
        """The receipt's day: the date read on it, else when it arrived."""
        self.ensure_one()
        receipt = self.sudo().receipt_id
        return local_day(receipt.distri_receipt_date or receipt.timestamp or self.date) \
            or today_ar()

    def _distri_credited_pool(self):
        """Credited cards still waiting for their transfer."""
        return self.sudo().search([
            ("kind", "=", "verify"), ("state", "=", "confirmed"), ("income_id", "=", False),
            ("credited_at", "!=", False), ("payment_id", "!=", False)])

    def _distri_report_incomes(self, amount=None, partner=None):
        """(sure, maybe): the incomes already loaded that are this receipt's
        transfer (same or close operation, or same amount within 2 minutes of
        the receipt's time), and the ones that may be (same amount, customer
        and day)."""
        self.ensure_one()
        card = self.sudo()
        Collection = self.sudo().browse()
        receipt = card.receipt_id
        operation = digits(card.operation or receipt.distri_operation)
        amount = amount or card.amount
        if not amount:
            return Collection, Collection
        near = self.sudo().search([("kind", "=", "income"),
                                   ("amount", ">=", amount - AMOUNT_TOLERANCE),
                                   ("amount", "<=", amount + AMOUNT_TOLERANCE)])
        sure = Collection
        if operation:
            sure |= self.sudo().search([("kind", "=", "income"), "|",
                                        ("source_id", "=", operation),
                                        ("bank_transfer_id", "=", operation)])
            sure |= near.filtered(lambda c: operation_close(operation, c.source_id)
                                  or operation_close(operation, c.bank_transfer_id))
        read = receipt.distri_receipt_date
        if read:
            sure |= near.filtered(lambda c: c.date and abs(c.date - read) <= RECEIPT_TIME_CLOSE
                                  and not (operation and not (
                                      operation_close(operation, c.source_id)
                                      or operation_close(operation, c.bank_transfer_id))))
        partner = (partner or card.partner_id).commercial_partner_id
        day = card._distri_credit_day()
        maybe = near.filtered(
            lambda c: c not in sure and c.state in ("draft", "confirmed")
            and c.date_day == day and partner
            and c.partner_id.commercial_partner_id == partner)
        return sure, maybe

    def _distri_credit_review(self, partner, amount, driver_auto=False):
        """(errors, warnings) of crediting this card to `partner` for
        `amount` (see the module docstring). `driver_auto`: the «coincide»
        rule crediting a driver's receipt by itself, in his name; it
        replaces the approver's check, the rest applies."""
        self.ensure_one()
        user = self.env.user
        card = self.sudo()
        errors, warnings = [], []
        if card.kind != "verify" or card.state != "draft":
            return ["%s no es un comprobante pendiente de acreditar." % card.name], []
        receipt = card.receipt_id
        if not receipt:
            return ["%s no tiene el comprobante." % card.name], []
        flow = card._distri_credit_flow()
        rule = driver_rule(self.env)
        if driver_auto:
            if flow != "driver" or rule not in ("inmediato", "coincide"):
                return ["Solo un comprobante del repartidor se acredita solo, con la regla "
                        "«inmediato» o «coincide»."], []
        elif card._distri_unknown_sender():
            if not can_approve(user):
                return [APPROVE_DENIED["unknown"]], []
        elif flow == "customer" or (flow == "driver" and rule != "inmediato"):
            if not can_approve(user):
                return [APPROVE_DENIED[flow]], []
        elif not can_credit(user):
            return [CREDIT_DENIED], []
        if receipt.distri_receipt_state != "pending":
            state = dict(receipt._fields["distri_receipt_state"].selection).get(
                receipt.distri_receipt_state, receipt.distri_receipt_state or "")
            return ["El comprobante está «%s»: no se acredita." % state], []
        if card.conflict_note:
            errors.append("%s: una persona decide de qué cliente es antes de acreditarlo." % (
                card.conflict_note))
        partner = partner.commercial_partner_id if partner else partner
        if not partner:
            errors.append("Elegí el cliente.")
        elif partner.distri_generic_partner:
            errors.append("Elegí un cliente con cuenta corriente: el genérico no lleva saldo.")
        if not amount or amount <= 0:
            errors.append("Escribí el importe del comprobante.")
        if receipt._distri_other_account(own_account(self.env)):
            errors.append("El comprobante es una transferencia a otra cuenta (%s), no a la de la "
                          "empresa: no se acredita." % (receipt._distri_destination_label() or ""))
        operation = digits(card.operation or receipt.distri_operation)
        if operation:
            twin = self.sudo().search([
                ("kind", "=", "verify"), ("state", "in", ("confirmed", "authorized")),
                ("id", "!=", card.id), ("operation", "!=", False)]).filtered(
                lambda c: c.operation == operation or (
                    operation_close(operation, c.operation) and amount
                    and abs(c.amount - amount) <= AMOUNT_TOLERANCE))[:1]
            if twin:
                errors.append("La operación %s ya se %s con otro comprobante (%s): es un "
                              "comprobante repetido." % (
                                  operation, "acreditó" if twin.state == "confirmed"
                                  else "autorizó a mano", twin.name))
        same_image = receipt._distri_same_image()
        if same_image:
            errors.append("La misma imagen ya está cargada en otro comprobante (%s, %s): no se "
                          "acredita dos veces." % (
                              local_when(same_image.timestamp), same_image._distri_upload_label()))
        repeated = receipt._distri_twins().filtered(
            lambda m: m.distri_receipt_state in ("matched", "registered"))
        if repeated:
            errors.append("Es el mismo comprobante que otro ya %s (%s): no se acredita dos "
                          "veces." % ("registrado" if repeated[:1].distri_receipt_state ==
                                      "registered" else "vinculado", local_when(
                                          repeated[:1].timestamp)))
        if amount and amount > 0:
            sure, maybe = card._distri_report_incomes(amount, partner)
            for income in sure[:1]:
                errors.append(
                    "La transferencia ya está en el reporte de Mercado Pago (%s, %s): %s." % (
                        income.name, dict(income._fields["state"].selection).get(income.state),
                        "ya está registrada" if income.state == "confirmed"
                        else "trabajala desde esa tarjeta"))
            for income in maybe[:1]:
                warnings.append(
                    "El reporte de Mercado Pago ya trae una transferencia de %s de este cliente "
                    "ese día (%s, %s): puede ser la misma." % (
                        _money(income.amount), income.name,
                        dict(income._fields["state"].selection).get(income.state)))
        day = card._distri_credit_day()
        today = today_ar()
        if day < today - timedelta(days=1):
            if can_approve(user):
                warnings.append("El comprobante es del %s, más viejo que ayer." % (
                    day.strftime("%d/%m/%Y")))
            else:
                errors.append("El comprobante es del %s, más viejo que ayer: lo acreditan "
                              "administración, gerencia o el admin." % day.strftime("%d/%m/%Y"))
        elif day > today:
            warnings.append("La fecha leída en el comprobante (%s) es posterior a hoy." % (
                day.strftime("%d/%m/%Y")))
        if amount and amount > 0:
            warnings += card._distri_amount_warnings(amount)
        if partner and not partner.distri_generic_partner and amount and amount > 0:
            warnings += card._distri_bank_twin_warnings(partner, amount)
            balance = partner.sudo()._distri_statement()["balance"]
            after = balance - amount
            if after < -credit_warning_threshold(self.env) - AMOUNT_TOLERANCE:
                warnings.append("El cliente va a quedar con %s a favor. ¿Acreditar igual?" % (
                    _money(-after)))
        return errors, warnings

    def _distri_amount_warnings(self, amount):
        """«Cargado contra leído»: an amount nobody read, read with doubt or
        different from the one read or typed is confirmed by the person."""
        self.ensure_one()
        receipt = self.sudo().receipt_id
        read = receipt.distri_read_amount
        out = []
        if not read:
            if receipt.distri_amount_confidence != "manual" or \
                    abs((receipt.distri_amount or 0.0) - amount) > AMOUNT_TOLERANCE:
                out.append("No se pudo leer el importe del comprobante: confirmá que %s es el "
                           "importe que figura." % _money(amount))
        elif abs(read - amount) > AMOUNT_TOLERANCE:
            out.append("El importe a acreditar (%s) no es el leído en el comprobante (%s): "
                       "confirmalo mirando la foto." % (_money(amount), _money(read)))
        elif receipt.distri_amount_confidence == CONFIDENCE_LOW:
            out.append("El importe leído (%s) es dudoso: confirmalo mirando la foto." % (
                _money(read)))
        typed = receipt.distri_amount_typed
        if typed and abs(typed - amount) > AMOUNT_TOLERANCE and not out:
            out.append("Quien subió el comprobante cargó %s y se acredita %s: confirmalo "
                       "mirando la foto." % (_money(typed), _money(amount)))
        return out

    def _distri_bank_twin_warnings(self, partner, amount):
        """A payment registered by hand in a bank journal (v124) that may be
        this same transfer."""
        self.ensure_one()
        record = self.sudo()
        day = record._distri_credit_day()
        mp_journal = self.env["account.journal"]._distri_mp_journal(record.company_id)
        payments = self.env["account.payment"].sudo().search([
            ("company_id", "=", record.company_id.id), ("payment_type", "=", "inbound"),
            ("partner_type", "=", "customer"), ("state", "in", ("in_process", "paid")),
            ("journal_id.type", "=", "bank"), ("journal_id", "!=", mp_journal.id),
            ("partner_id", "child_of", partner.id),
            ("amount", ">=", amount - AMOUNT_TOLERANCE), ("amount", "<=", amount + AMOUNT_TOLERANCE),
            ("date", ">=", day - timedelta(days=BANK_TWIN_DAYS)),
            ("date", "<=", day + timedelta(days=BANK_TWIN_DAYS))])
        if payments:
            payments -= self.with_context(active_test=False).sudo().search(
                [("payment_id", "in", payments.ids)]).payment_id
        return ["Ya hay un pago cargado a mano en %s por %s (%s del %s): puede ser esta misma "
                "transferencia." % (p.journal_id.name, _money(p.amount), p.name,
                                    p.date.strftime("%d/%m/%Y")) for p in payments[:2]]

    # ------------------------------------------------------------ credit

    def action_credit(self):
        """«Acreditar» on the board: the wizard with the amount, the customer
        and the warnings."""
        self.ensure_one()
        if not self.can_credit:
            flow = self._distri_credit_flow()
            raise AccessError(APPROVE_DENIED.get(flow, CREDIT_DENIED))
        return self.env["distri.mp.credit"]._open(self)

    def _distri_credit(self, partner, amount, confirmed=False, driver_auto=False):
        """Credits this «verify» card (see the module docstring). Raises
        UserError on an error and CreditWarning when warnings were not
        confirmed. Returns the payment."""
        self.ensure_one()
        # one crediting at a time per card, and per operation number
        self.env.cr.execute("SELECT id FROM distri_mp_collection WHERE id = %s FOR UPDATE",
                            [self.id])
        self.invalidate_recordset()
        card = self.sudo()
        operation = digits(card.operation or card.receipt_id.distri_operation)
        if operation:
            self.env.cr.execute("SELECT pg_advisory_xact_lock(hashtext(%s))",
                                ["distri_credit_op:%s" % operation])
        errors, warnings = self._distri_credit_review(partner, amount, driver_auto=driver_auto)
        if driver_auto and warnings:
            # nothing that needs a person is credited by itself
            errors = errors + warnings
        if errors:
            raise UserError("\n".join(errors))
        if warnings and not confirmed:
            raise CreditWarning(warnings)
        user = self.env.user
        receipt = card.receipt_id
        partner = partner.commercial_partner_id.sudo()
        # the receipt and the card say what the person checked
        receipt_vals = {}
        if receipt.distri_partner_id.commercial_partner_id != partner:
            receipt_vals.update(distri_partner_id=partner.id,
                                distri_partner_source="Elegido por %s al acreditarlo" % user.name)
        if abs((receipt.distri_amount or 0.0) - amount) > 0.004:
            receipt_vals.update(distri_amount=amount, distri_amount_confidence="manual")
        if receipt_vals:
            receipt.with_context(distri_receipt_matching=True).write(receipt_vals)
        card_vals = {"amount": amount}
        if card.partner_id.commercial_partner_id != partner:
            card_vals.update(partner_id=partner.id, proposal="manual",
                             proposal_note="Elegido por %s al acreditarlo" % user.name)
        card.write(card_vals)
        day = min(card._distri_credit_day(), today_ar())
        history = card.pay_invoice_id
        journal = self.env["account.journal"]._distri_mp_journal(card.company_id)
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": amount,
            "journal_id": journal.id,
            "date": day,
            "memo": card.with_context(distri_crediting=True)._payment_memo(),
            "company_id": card.company_id.id,
            "distri_history_invoice_id": history.id or False,
        })
        payment.action_post()
        if not history:
            # 19.0.1.125.0: an Online invoice stays unreconciled (the id link
            # clears its red); a lab invoice first, then oldest first
            card._reconcile_chosen_invoice(payment)
            self.env["distri.route"].sudo()._apply_oldest_first(payment, partner)
        promises = self.env["distri.promised.transfer"].sudo()._distri_fulfil_for(
            partner, card.date, amount, card)
        now = fields.Datetime.now()
        card.write({
            "state": "confirmed",
            "payment_id": payment.id,
            "confirmed_by_id": user.id,
            "confirmed_at": now,
            "credited_by_id": user.id,
            "credited_at": now,
            "audit_state": "pendiente",
            "fulfilled_promise_ids": [(6, 0, promises.ids)],
            "report_review": False,
        })
        # the receipt waits no more: no transfer takes it by amount, the
        # report ties to this card (`_match_hand_verified`)
        receipt.with_context(distri_receipt_matching=True).write({
            "distri_receipt_state": "matched", "distri_collection_id": card.id})
        self.env["distri.promised.transfer"].sudo()._distri_resolve_from_receipt(receipt)
        text = "Acreditado por %s: pago %s de %s a %s (%s).%s" % (
            user.name, payment.name, _money(amount), partner.name,
            card.pay_invoice_label or "a cuenta",
            (" Advertencias confirmadas: %s" % " ".join(warnings)) if warnings else "")
        card.message_post(body=text)
        partner.message_post(body=Markup("Comprobante acreditado: <b>%s</b> por %s (%s).") % (
            _money(amount), user.name, card.name))
        _logger.info("Receipt card %s credited by %s: payment %s of %s to partner %s",
                     card.id, user.login, payment.id, amount, partner.id)
        return payment

    # ------------------------------------------------------------ undo

    def action_undo(self):
        """A credited card undone goes back to «Para acreditar»: its
        payment is cancelled by the D10 path; the credit marks go."""
        credited = self.sudo().filtered(
            lambda c: c.kind == "verify" and c.state == "confirmed" and c.credited_at)
        result = super().action_undo()
        for card in credited.filtered(lambda c: c.state == "draft"):
            card.write({"credited_by_id": False, "credited_at": False, "audit_state": False})
        return result

    # ------------------------------------------------------------ report matching

    def _tie_hand_verified(self, card):
        """The exact operation (D10): a credited card of the same amount is
        «verificado»."""
        result = super()._tie_hand_verified(card)
        if card.sudo().credited_at and self.state == "confirmed":
            card.sudo().audit_state = "verificado"
        return result

    def _match_hand_verified(self):
        """After the exact operation (D10), the cascade of the module
        docstring against the credited cards."""
        tied = super()._match_hand_verified()
        rest = self.filtered(lambda c: c.kind == "income" and c.state == "draft"
                             and c.proposal not in ("verified", "authorized", "credited")) - tied
        return tied | rest._match_credited()

    def _credited_match_level(self, card, level):
        """This income is `card`'s transfer by the rule of `level`."""
        self.ensure_one()
        card = card.sudo()
        numbers = [n for n in (self.source_id, self.bank_transfer_id) if n]
        operation = digits(card.operation)
        same = abs(card.amount - self.amount) <= AMOUNT_TOLERANCE
        close = bool(operation) and any(operation_close(operation, n) for n in numbers)
        if level == "operation":
            return bool(operation) and operation in numbers
        if level == "operation_close":
            return close and same
        if not same or (operation and not close):
            # its operation number says it is another transfer
            return False
        if level == "time":
            read = card.receipt_id.distri_receipt_date
            if not (read and self.date and abs(read - self.date) <= RECEIPT_TIME_CLOSE):
                return False
            cuit = digits(card.payer_cuit or card.receipt_id.distri_payer_cuit)
            return not (cuit and self.payer_cuit and cuit != digits(self.payer_cuit))
        if level == "customer":
            if not self.payer_cuit:
                return False
            partners = self.env["res.partner"].sudo()._distri_partners_by_cuit(self.payer_cuit)
            return len(partners) == 1 and partners == card.partner_id.commercial_partner_id \
                and self.date_day == card._distri_credit_day()
        return False

    def _credited_candidates(self, pool=None):
        """The credited cards waiting for their transfer that this income
        may be, by the first level of the cascade that finds any."""
        self.ensure_one()
        if self.kind != "income":
            return self.browse()
        pool = self._distri_credited_pool() if pool is None else pool
        for level in ("operation", "operation_close", "time", "customer"):
            found = pool.filtered(lambda c: self._credited_match_level(c, level))
            if found:
                return found
        return self.browse()

    def _match_credited(self):
        """Ties the incomes of a report to the credited cards, level by
        level; a tie between several goes to «Revisar». Returns the incomes
        tied or sent to review."""
        handled = self.browse()
        pool = self._distri_credited_pool()
        todo = self
        for level in ("operation_close", "time", "customer"):
            if not (todo and pool):
                break
            found = {rec.id: pool.filtered(lambda c, rec=rec: rec._credited_match_level(c, level))
                     for rec in todo}
            for rec in todo:
                cards = found[rec.id]
                if not cards:
                    continue
                incomes = todo.filtered(lambda r: set(found[r.id].ids) & set(cards.ids))
                if len(cards) == 1 and len(incomes) == 1:
                    rec._tie_credited(cards, level)
                    pool -= cards
                else:
                    rec._send_credited_review(cards)
                handled |= rec
            todo -= handled
        return handled

    def _tie_credited(self, card, level, by_hand=False):
        """The income is the credited card's transfer: confirmed with its
        payment, no new one (the way `_tie_hand_verified` ties)."""
        self.ensure_one()
        card = card.sudo()
        when = "%s por %s" % (local_when(card.credited_at), card.credited_by_id.name or "")
        how = ("vinculado a mano por %s" % self.env.user.name) if by_hand \
            else LEVEL_LABELS.get(level, level)
        card.write({"income_id": self.id, "report_review": False, "audit_state": "verificado",
                    "proposal_note": "Llegó en el reporte: %s" % self.name})
        self.sudo().write({
            "state": "confirmed",
            "partner_id": card.partner_id.id,
            "proposal": "credited",
            "receipt_id": card.receipt_id.id,
            "payment_id": card.payment_id.id,
            "confirmed_by_id": card.credited_by_id.id,
            "confirmed_at": card.credited_at,
            "receipt_candidate": False,
            "conflict_note": False,
            "proposal_note": "Acreditado el %s (%s)" % (when, how),
        })
        self.message_post(body="Ya estaba acreditado el %s (pago %s), %s: no se registra otro "
                               "pago." % (when, card.payment_id.name or "", how))
        card.message_post(body="La transferencia llegó en el reporte: %s de %s (%s)." % (
            self.name, _money(self.amount), how))

    def _send_credited_review(self, cards):
        self.ensure_one()
        names = ", ".join("%s de %s" % (c.name, c.partner_id.name or "") for c in cards[:3])
        partners = cards.partner_id.commercial_partner_id
        self.sudo().write({
            "proposal": "credited",
            "partner_id": partners.id if len(partners) == 1 else False,
            "proposal_note": TIE_NOTE % (names, _money(self.amount)),
        })
        self.message_post(body=TIE_NOTE % (names, _money(self.amount)))

    def _confirm_todo(self):
        """Never a second payment for a credited receipt: an income that may
        be one waits until a person ties it or says it is another payment."""
        todo = super()._confirm_todo()
        if not self.env.context.get("distri_not_credited"):
            for rec in todo:
                cards = rec._credited_candidates()
                if cards:
                    raise UserError(
                        "%s puede ser el comprobante ya acreditado %s: vinculalo con «Es un "
                        "comprobante acreditado» o, si es otro pago, tocá «Confirmar: es otro "
                        "pago»." % (rec.name, ", ".join(cards[:3].mapped("name"))))
        return todo

    def _bulk_problem(self):
        problem = super()._bulk_problem()
        if not problem and self.kind == "income" and \
                self._credited_candidates():
            return "puede ser un comprobante ya acreditado"
        return problem

    def action_confirm_other_payment(self):
        """«Confirmar: es otro pago»: the person checked that the income is
        not the credited receipt it looked like."""
        _check_cobros(self.env)
        for rec in self:
            rec.message_post(body="%s confirmó que no es un comprobante acreditado." % (
                self.env.user.name))
        return self.with_context(distri_not_credited=True).action_confirm_board()

    def action_open_tie_credited(self):
        """«Es un comprobante acreditado»: choose which."""
        self.ensure_one()
        _check_cobros(self.env)
        if self.kind != "income" or self.state != "draft":
            raise UserError("Solo se vincula un ingreso pendiente del reporte.")
        return self.env["distri.mp.tie.credited"]._open(self)


class WhatsappMessageCredit(models.Model):
    _inherit = "distri.whatsapp.message"

    def _distri_same_image(self):
        """Another receipt with the same file (bytes as stored or as they
        arrived) already credited or tied to a transfer. A copy still
        pending is not: the first one credited wins, the other is refused."""
        self.ensure_one()
        msg = self.sudo()
        checksums = {c for c in (msg.attachment_id.checksum, msg.distri_original_checksum) if c}
        if not checksums:
            return self.browse()
        attachments = self.env["ir.attachment"].sudo().search([
            ("checksum", "in", list(checksums)), ("res_model", "=", self._name),
            ("res_id", "!=", msg.id)])
        others = self.sudo().browse(attachments.mapped("res_id")).exists() | self.sudo().search(
            [("distri_original_checksum", "in", list(checksums)), ("id", "!=", msg.id)])
        return others.filtered(lambda m: m.distri_is_receipt and (
            m.distri_receipt_state in ("matched", "registered")
            or m.distri_collection_id.state in ("confirmed", "authorized")))[:1]

    def _distri_verify_card(self):
        self.ensure_one()
        return self.env["distri.mp.collection"].sudo().search(
            [("kind", "=", "verify"), ("receipt_id", "=", self.id)], order="id desc", limit=1)

    def _distri_credit_card(self, user):
        """The draft «verify» card of this receipt that `user` may credit,
        or an empty recordset."""
        self.ensure_one()
        if self.sudo().distri_receipt_state != "pending":
            return self.env["distri.mp.collection"]
        card = self._distri_verify_card()
        if not card or card.state != "draft":
            return self.env["distri.mp.collection"]
        return card if card._distri_user_can_credit(user) else self.env["distri.mp.collection"]

    def _distri_read_now(self):
        """Reads the file now when the background reading has not run yet
        (the person is about to credit it). 19.0.1.128.0: the OCR gives up
        after `ocr_seconds` (models/ocr_comprobante.py); what was not read is
        typed by the person."""
        with ocr_deadline(ocr_seconds(self.env)):
            self._distri_read_now_capped()

    def _distri_read_now_capped(self):
        for msg in self.sudo().filtered(lambda m: not m.media_state and m.attachment_id):
            try:
                with self.env.cr.savepoint():
                    msg.write({"media_state": "done"})
            except Exception:
                _logger.exception("Receipt %s: reading before crediting failed", msg.id)
                msg.invalidate_recordset()
                msg.write({"media_state": "error",
                           "media_error": "No se pudo leer el archivo (ver el log del servidor)"})
                msg._distri_use_typed_amount()

    def _distri_credit_form(self, user):
        """What the «Acreditar» form of «Ver comprobante» shows, or None."""
        self.ensure_one()
        card = self._distri_credit_card(user)
        if not card:
            return None
        msg = self.sudo()
        read = msg.distri_read_amount
        amount = read if read and msg.distri_amount_confidence != "manual" else (
            msg.distri_amount or msg.distri_amount_typed)
        partner = card.partner_id.commercial_partner_id
        return {
            "card_id": card.id,
            "amount": amount or 0.0,
            "amount_text": ("%.2f" % amount).replace(".", ",") if amount else "",
            "read_amount": read or 0.0,
            "typed_amount": msg.distri_amount_typed or 0.0,
            "partner_id": partner.id or "",
            "partner_name": partner.name or "",
            "reminder": CREDIT_REMINDER,
        }

    def _distri_read_receipt(self, read_file=False):
        result = super()._distri_read_receipt(read_file=read_file)
        if read_file and \
                driver_rule(self.env) in ("inmediato", "coincide"):
            for msg in self.sudo().filtered(lambda m: m.distri_from_app):
                msg._distri_auto_credit_driver()
        return result

    def _distri_auto_credit_driver(self):
        """The driver's receipt credits by itself once read, in his name,
        when nothing needs a person (no error, no warning): «inmediato»
        with the amount of the receipt, «coincide» only when the amount he
        typed is the one read with high confidence."""
        self.ensure_one()
        msg = self.sudo()
        card = msg._distri_verify_card()
        driver = msg.distri_forwarded_by_id
        stop = msg.distri_app_stop_id
        typed = stop.transfer_amount if stop and stop.transfer == "yes" and \
            stop.transfer_amount > 0 else msg.distri_amount
        read = msg.distri_read_amount
        if not (card and card.state == "draft" and driver and card.partner_id and read):
            return False
        amount = msg.distri_amount
        if driver_rule(self.env) == "coincide":
            if not (typed and msg.distri_amount_confidence == CONFIDENCE_HIGH
                    and abs(read - typed) <= AMOUNT_TOLERANCE):
                return False
            amount = read
        # the driver's rule replaces the groups' check; credited in his name
        try:
            with self.env.cr.savepoint():
                card.with_user(driver).sudo()._distri_credit(
                    card.partner_id, amount, confirmed=False, driver_auto=True)
        except UserError as exc:
            _logger.info("Driver receipt %s not credited by itself: %s", msg.id, exc)
            return False
        return True


class DistriMpCredit(models.TransientModel):
    """«Acreditar» on the board: amount (the one read, editable), customer
    and the warnings to confirm."""
    _name = "distri.mp.credit"
    _description = "Acreditar comprobante"

    collection_id = fields.Many2one("distri.mp.collection", required=True, readonly=True,
                                    ondelete="cascade")
    currency_id = fields.Many2one(related="collection_id.currency_id")
    partner_id = fields.Many2one("res.partner", string="Cliente",
                                 domain="[('distri_generic_partner', '=', False), "
                                        "('parent_id', '=', False)]")
    amount = fields.Monetary(string="Importe a acreditar")
    read_amount = fields.Float(related="collection_id.receipt_id.distri_read_amount",
                               string="Importe leído")
    typed_amount = fields.Float(related="collection_id.receipt_id.distri_amount_typed",
                                string="Importe cargado por quien lo subió")
    operation = fields.Char(related="collection_id.operation")
    receipt_date_label = fields.Char(related="collection_id.receipt_date_label")
    pays_label = fields.Char(related="collection_id.pay_invoice_label", string="Paga")
    receipt_image = fields.Binary(related="collection_id.receipt_image")
    reminder = fields.Char(default=CREDIT_REMINDER, readonly=True)
    errors_text = fields.Text(compute="_compute_review")
    warnings_text = fields.Text(compute="_compute_review")
    confirm = fields.Boolean(string="Revisé las advertencias: acreditar igual")

    @api.model
    def _open(self, card):
        receipt = card.sudo().receipt_id
        amount = receipt.distri_read_amount if receipt.distri_read_amount and \
            receipt.distri_amount_confidence != "manual" else card.amount
        wizard = self.create({"collection_id": card.id,
                              "partner_id": card.partner_id.commercial_partner_id.id,
                              "amount": amount or card.amount})
        return {
            "type": "ir.actions.act_window",
            "name": "Acreditar %s" % card.name,
            "res_model": self._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    @api.depends("partner_id", "amount", "collection_id")
    def _compute_review(self):
        for wizard in self:
            errors, warnings = wizard.collection_id._distri_credit_review(
                wizard.partner_id, wizard.amount) if wizard.collection_id else ([], [])
            wizard.errors_text = "\n".join(errors) or False
            wizard.warnings_text = "\n".join(warnings) or False

    def action_apply(self):
        self.ensure_one()
        try:
            self.collection_id._distri_credit(self.partner_id, self.amount, confirmed=self.confirm)
        except CreditWarning as exc:
            raise UserError("Revisá las advertencias y marcá «%s»:\n%s" % (
                self._fields["confirm"].string, "\n".join(exc.warnings))) from None
        return {"type": "ir.actions.act_window_close"}


class DistriMpTieCredited(models.TransientModel):
    """«Es un comprobante acreditado»: the income of the report is the
    transfer of a credited card; ties it without a new payment."""
    _name = "distri.mp.tie.credited"
    _description = "Vincular con un comprobante acreditado"

    income_id = fields.Many2one("distri.mp.collection", required=True, readonly=True,
                                ondelete="cascade")
    candidate_ids = fields.Many2many("distri.mp.collection", string="Acreditados posibles",
                                     readonly=True)
    card_id = fields.Many2one("distri.mp.collection", string="Comprobante acreditado",
                              domain="[('id', 'in', candidate_ids)]")

    @api.model
    def _open(self, income):
        pool = income._distri_credited_pool().filtered(
            lambda c: abs(c.amount - income.amount) <= AMOUNT_TOLERANCE)
        if not pool:
            raise UserError("No hay comprobantes acreditados de %s esperando el reporte." % (
                _money(income.amount)))
        first = income._credited_candidates(pool)
        wizard = self.create({"income_id": income.id, "candidate_ids": [(6, 0, pool.ids)],
                              "card_id": first.id if len(first) == 1 else False})
        return {"type": "ir.actions.act_window", "name": "Vincular %s" % income.name,
                "res_model": self._name, "res_id": wizard.id, "view_mode": "form",
                "target": "new"}

    def action_apply(self):
        self.ensure_one()
        _check_cobros(self.env)
        income, card = self.income_id.sudo(), self.card_id.sudo()
        if not card:
            raise UserError("Elegí el comprobante acreditado.")
        if income.state != "draft" or card not in income._distri_credited_pool():
            raise UserError("Ya no se puede vincular: el ingreso o el comprobante cambió.")
        if abs(card.amount - income.amount) > AMOUNT_TOLERANCE:
            raise UserError("El importe no coincide: no se vincula.")
        income._tie_credited(card, "manual", by_hand=True)
        return {"type": "ir.actions.act_window_close"}


class AccountPaymentCredit(models.Model):
    _inherit = "account.payment"

    distri_audit_state = fields.Selection(
        AUDIT_STATES, string="Control con MP", compute="_compute_distri_audit_state",
        search="_search_distri_audit_state",
        help="Pago de un comprobante acreditado al validarlo: su control contra el reporte de "
             "Mercado Pago.")

    def _compute_distri_audit_state(self):
        states = {}
        if self.ids:
            cards = self.env["distri.mp.collection"].sudo().search(
                [("kind", "=", "verify"), ("payment_id", "in", self.ids),
                 ("credited_at", "!=", False)])
            states = {c.payment_id.id: c.audit_state for c in cards}
        for payment in self:
            payment.distri_audit_state = states.get(payment.id, False)

    def _search_distri_audit_state(self, operator, value):
        cards = self.env["distri.mp.collection"].sudo().search(
            [("kind", "=", "verify"), ("payment_id", "!=", False), ("credited_at", "!=", False),
             ("audit_state", operator, value)])
        return [("id", "in", cards.payment_id.ids)]


class ResUsersCreditNotice(models.Model):
    _inherit = "res.users"

    distri_aviso_acreditar_visto = fields.Datetime(
        string="Vio el aviso de acreditar comprobantes", copy=False,
        help="Cuándo tocó «Entendido» en el aviso de la acreditación inmediata (19.0.1.126.0).")

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ["distri_aviso_acreditar_visto"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ["distri_aviso_acreditar_visto"]

    def _distri_credit_notice_pending(self):
        """The one-time notice of the instant crediting is shown to this user."""
        self.ensure_one()
        user = self.sudo()
        if user._is_public() or not can_credit(user) \
                or user._is_superuser():
            return False
        return not user.distri_aviso_acreditar_visto

    def _distri_credit_notice_seen(self):
        """«Entendido»: kept with the moment (the field is the log)."""
        self.ensure_one()
        if not self._distri_credit_notice_pending():
            return False
        self.sudo().distri_aviso_acreditar_visto = fields.Datetime.now()
        _logger.info("User %s acknowledged the instant crediting notice", self.login)
        return True
