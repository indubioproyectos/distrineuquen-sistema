"""The «Caja del día» at the moment a payment is posted (19.0.1.124.0).

- Instant sync: a cash payment that qualifies under the 19.0.1.118.0 rule
  («Cobro registrado en la factura», `distri.caja._pending_invoice_cash`)
  enters the creating cashier's open caja as soon as it is posted, not only
  when /caja, the opening or the closing syncs. Same rule, same unique index
  on `payment_id`: the later syncs find it taken. It never blocks the post:
  a problem is logged and the periodic sync tries again.
- No open caja: the cashier gets a chatter note on the payment and, from
  «Registrar pago», a notification; the cash enters the next caja he opens.
- «Registrar pago» proposes the caja's cash journal (CSH1) to a cashier
  instead of core's first journal by sequence (BNK1). «Banco» stays in the
  choices for the transfer part of a split payment.

The module's own flows that post a payment and only then link it to a route
or a depot box (models/reparto.py, models/cobro_efectivo.py) post with
NO_INSTANT_CAJA: the settlement or the box brings that money.
"""
import logging

from odoo import api, models
from odoo.exceptions import UserError

from .caja import is_cashier

_logger = logging.getLogger(__name__)

NO_INSTANT_CAJA = "distri_caja_no_instant"
NO_OPEN_CAJA_NOTICE = ("No tenés la caja abierta: este efectivo va a entrar en la próxima caja "
                       "que abras.")


class AccountPaymentCaja(models.Model):
    _inherit = "account.payment"

    def action_post(self):
        res = super().action_post()
        if not self.env.context.get(NO_INSTANT_CAJA):
            self._distri_caja_on_post()
        return res

    def _distri_caja_on_post(self):
        payments = self.filtered(
            lambda p: p.payment_type == "inbound" and p.partner_type == "customer"
            and p.journal_id.type == "cash" and p.state in ("in_process", "paid"))
        for payment in payments:
            try:
                with self.env.cr.savepoint():
                    if not is_cashier(payment.create_uid):
                        continue
                    caja = payment._distri_open_caja()
                    if caja:
                        caja._sync_invoice_collections(only=payment)
                    elif payment._distri_waits_for_caja():
                        payment.sudo().message_post(body=NO_OPEN_CAJA_NOTICE)
            except Exception:
                _logger.exception("Caja del día: the payment %s could not be synced on post; "
                                  "the next sync of the caja takes it", payment.id)

    def _distri_open_caja(self):
        """The open caja (of any day) of the payment's creator."""
        self.ensure_one()
        return self.env["distri.caja"].sudo().search([
            ("user_id", "=", self.create_uid.id), ("state", "=", "open"),
            ("company_id", "=", self.company_id.id)], limit=1)

    def _distri_waits_for_caja(self):
        """The creator is a cashier without an open caja and the payment is
        one the next caja he opens takes (the 19.0.1.118.0 rule, checked on
        an unsaved caja of his)."""
        self.ensure_one()
        payment = self.sudo()
        if not is_cashier(payment.create_uid) or payment._distri_open_caja():
            return False
        Caja = self.env["distri.caja"].sudo()
        journal = Caja._session_journal(payment.company_id)
        if payment.journal_id != journal:
            return False
        draft = Caja.new({"user_id": payment.create_uid.id, "journal_id": journal.id,
                          "company_id": payment.company_id.id})
        return bool(draft._pending_invoice_cash(only=payment))


class AccountPaymentRegisterCaja(models.TransientModel):
    _inherit = "account.payment.register"

    @api.depends("available_journal_ids")
    def _compute_journal_id(self):
        # what core keeps (a journal already chosen) stays
        kept = self.filtered(lambda w: w.journal_id in w.available_journal_ids)
        super()._compute_journal_id()
        if not is_cashier(self.env.user):
            return
        for wizard in self - kept:
            if wizard.payment_type != "inbound" or wizard.partner_type != "customer" \
                    or not wizard.company_id \
                    or len(wizard.line_ids.move_id.preferred_payment_method_line_id) == 1:
                continue
            try:
                journal = self.env["distri.caja"]._session_journal(wizard.company_id)
            except UserError:
                continue
            # _origin: the wizard is still a new record when precomputed
            if journal in wizard.available_journal_ids._origin:
                wizard.journal_id = journal

    def action_create_payments(self):
        action = super().action_create_payments()
        if not isinstance(action, dict) or not is_cashier(self.env.user):
            return action
        ids = [action["res_id"]] if action.get("res_id") else next(
            (term[2] for term in action.get("domain") or () if term[0] == "id"), [])
        try:
            waiting = any(p._distri_waits_for_caja()
                          for p in self.env["account.payment"].sudo().browse(ids).exists())
        except Exception:
            _logger.exception("Caja del día: could not check the open caja after «Registrar pago»")
            return action
        if not waiting:
            return action
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {"title": "Caja del día", "message": NO_OPEN_CAJA_NOTICE,
                       "type": "warning", "sticky": True, "next": action},
        }
