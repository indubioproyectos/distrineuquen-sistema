"""«Ajuste de cuentas» (19.0.1.44.0).

Management confirms the real current debt of some customers (often $0)
without a record of the payments behind it. The adjustment sets the lab
balance of the customer to that figure without pretending money came in:
no payment, no cash, Mercado Pago or bank journal.

- «Ajustar saldo» (customer form, «Revisión por cliente»): the current lab
  balance is the same figure as the review (Online − lab payments + unrecorded
  sales + earlier adjustments, models/revision_cliente.py). The administrator
  types the real balance, the motive and who authorized it.
- Real balance below the lab balance: an internal credit note (out_refund)
  for the difference. Above it (rare): an internal debit (out_invoice), only
  with the extra confirmation checkbox. Both in the journal «Ajuste de
  cuentas» (AJC, its own series, no AFIP documents, never the default sales
  journal), created per company like «Ventas sin registrar».
- The posted move enters the review (`adjustment_amount`) and, as any posted
  receivable line, the account statement.
- «Ajustes de cuenta» (Cobros) lists every adjustment for the administrator
  to replicate in Odoo Online, with «Pasado a Online» to mark the done ones.
"""
from markupsafe import Markup

from odoo import Command, api, fields, models
from odoo.exceptions import UserError

from .puesta_al_dia import _check_admin_or_raise
from .reparto import _money

AJC_JOURNAL_CODE = "AJC"
AJC_JOURNAL_NAME = "Ajuste de cuentas"
ADJUSTMENT_KINDS = [("credit", "Baja la deuda"), ("debit", "Sube la deuda")]


class AccountJournalAjc(models.Model):
    _inherit = "account.journal"

    def _distri_ajc_journal(self, company=None):
        """«Ajuste de cuentas» sales journal of the company, created the first
        time, with its own series (AJC/2026/00001)."""
        return self._distri_internal_sale_journal(AJC_JOURNAL_CODE, AJC_JOURNAL_NAME, company)

    @api.model
    def _distri_ensure_ajc_journal(self):
        """Called on module update, in companies with accounting set up."""
        for company in self.env["res.company"].sudo().search([]):
            if self.sudo().search_count([("company_id", "=", company.id),
                                         ("type", "=", "sale")]):
                self._distri_ajc_journal(company)


class DistriBalanceAdjustment(models.Model):
    _name = "distri.balance.adjustment"
    _description = "Ajuste de cuenta"
    _order = "date desc, id desc"

    name = fields.Char(related="move_id.name", string="Comprobante")
    date = fields.Date(string="Fecha", required=True, readonly=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, readonly=True,
                                 index=True, ondelete="restrict")
    online_ref = fields.Char(related="partner_id.ref", string="Ref. Online")
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda s: s.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    balance_before = fields.Monetary(string="Saldo del lab antes", readonly=True)
    target_balance = fields.Monetary(string="Saldo real", readonly=True)
    difference = fields.Monetary(string="Diferencia", readonly=True,
                                 help="Saldo real − saldo del lab: negativo baja la deuda.")
    kind = fields.Selection(ADJUSTMENT_KINDS, string="Tipo", readonly=True)
    motive = fields.Text(string="Motivo", required=True, readonly=True)
    authorized_by = fields.Char(string="Autorizado por", required=True, readonly=True)
    move_id = fields.Many2one("account.move", string="Asiento", readonly=True,
                              index="btree_not_null", ondelete="restrict")
    move_state = fields.Selection(related="move_id.state", store=True, string="Estado del asiento")
    online_done = fields.Boolean(string="Pasado a Online", readonly=True, index=True)
    online_done_by_id = fields.Many2one("res.users", string="Pasado por", readonly=True)
    online_done_at = fields.Datetime(string="Pasado el", readonly=True)

    def action_mark_online(self):
        """«Pasado a Online»: the administrator replicated it in Odoo Online."""
        _check_admin_or_raise(self.env)
        self.sudo().filtered(lambda a: not a.online_done).write({
            "online_done": True, "online_done_by_id": self.env.user.id,
            "online_done_at": fields.Datetime.now()})
        return True

    def action_unmark_online(self):
        _check_admin_or_raise(self.env)
        self.sudo().write({"online_done": False, "online_done_by_id": False,
                           "online_done_at": False})
        return True

    def action_view_move(self):
        from .migracion import record_url, tab_action
        self.ensure_one()
        return tab_action(record_url(self.move_id))


class ResPartnerAjusteCuenta(models.Model):
    _inherit = "res.partner"

    def action_distri_balance_adjustment(self):
        """«Ajustar saldo»: the wizard for the commercial partner."""
        _check_admin_or_raise(self.env)
        self.ensure_one()
        partner = self.commercial_partner_id
        if partner.distri_generic_partner:
            raise UserError("El contacto genérico de pedidos web no tiene saldo para ajustar.")
        return {
            "type": "ir.actions.act_window",
            "name": "Ajustar saldo",
            "res_model": "distri.balance.adjustment.wizard",
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
            "context": {"default_partner_id": partner.id},
        }


class DistriBalanceAdjustmentWizard(models.TransientModel):
    """«Ajustar saldo»: sets the lab balance of a customer to the real one."""
    _name = "distri.balance.adjustment.wizard"
    _description = "Ajustar saldo"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, readonly=True)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    current_balance = fields.Monetary(
        string="Saldo en el lab", compute="_compute_current_balance",
        help="El mismo saldo de «Revisión por cliente»: saldo en Online − pagos del lab + ventas "
             "sin registrar + ajustes anteriores.")
    target_balance = fields.Monetary(string="Saldo real", default=0.0,
                                     help="La deuda actual confirmada por la gerencia.")
    difference = fields.Monetary(string="Diferencia", compute="_compute_difference")
    is_debit = fields.Boolean(compute="_compute_difference")
    confirm_debit = fields.Boolean(
        string="Confirmo que el cliente debe más de lo que dice el lab",
        help="Obligatorio cuando el saldo real es mayor: se registra un débito interno.")
    motive = fields.Text(string="Motivo")
    authorized_by = fields.Char(string="Autorizado por")
    date = fields.Date(string="Fecha", default=lambda s: fields.Date.context_today(s))

    @api.depends("partner_id")
    def _compute_current_balance(self):
        Review = self.env["distri.balance.review"].sudo()
        for wiz in self:
            wiz.current_balance = Review._distri_lab_balance(wiz.partner_id) \
                if wiz.partner_id else 0.0

    @api.depends("current_balance", "target_balance")
    def _compute_difference(self):
        for wiz in self:
            wiz.difference = round(wiz.target_balance - wiz.current_balance, 2)
            wiz.is_debit = wiz.difference > 0.005

    @api.model_create_multi
    def create(self, vals_list):
        _check_admin_or_raise(self.env)
        return super().create(vals_list)

    def action_apply(self):
        _check_admin_or_raise(self.env)
        self.ensure_one()
        # default_partner_id must not reach the move or the adjustment
        self = self.with_context(
            {k: v for k, v in self.env.context.items() if not k.startswith("default_")})
        partner = self.partner_id.commercial_partner_id
        motive = (self.motive or "").strip()
        authorized_by = (self.authorized_by or "").strip()
        if not partner or partner.distri_generic_partner:
            raise UserError("Elegí el cliente.")
        if not motive:
            raise UserError("Escribí el motivo del ajuste.")
        if not authorized_by:
            raise UserError("Indicá quién autorizó el ajuste.")
        if not self.date:
            raise UserError("Indicá la fecha del ajuste.")
        # one adjustment at a time per customer: the balance is read again
        # under the lock, never the one shown when the wizard opened
        self.env.cr.execute("SELECT id FROM res_partner WHERE id = %s FOR UPDATE", [partner.id])
        before = self.env["distri.balance.review"].sudo()._distri_lab_balance(partner)
        target = round(self.target_balance, 2)
        difference = round(target - before, 2)
        if abs(difference) < 0.005:
            raise UserError("El saldo del lab ya es %s: no hay nada que ajustar." % _money(before))
        if difference > 0 and not self.confirm_debit:
            raise UserError("El saldo real (%s) es mayor que el del lab (%s): se registra un débito "
                            "interno de %s. Marcá la confirmación para seguir." % (
                                _money(target), _money(before), _money(difference)))
        move = self._distri_move(partner, difference, motive)
        adjustment = self.env["distri.balance.adjustment"].sudo().create({
            "date": self.date,
            "partner_id": partner.id,
            "company_id": move.company_id.id,
            "balance_before": before,
            "target_balance": target,
            "difference": difference,
            "kind": "debit" if difference > 0 else "credit",
            "motive": motive,
            "authorized_by": authorized_by,
            "move_id": move.id,
        })
        stamp = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        head = Markup("<b>Ajuste de cuenta</b> por %s el %s: ") % (
            self.env.user.name, stamp.strftime("%d/%m/%Y %H:%M"))
        detail = "saldo del lab %s, saldo real %s, %s %s (%s). Autorizó: %s. Motivo: %s" % (
            _money(before), _money(target),
            "débito" if difference > 0 else "nota de crédito", _money(abs(difference)),
            move.name, authorized_by, motive)
        partner.sudo().message_post(body=head + detail)
        move.message_post(body=head + detail)
        self.env["distri.balance.review"]._distri_refresh(partner)
        return {"type": "ir.actions.act_window_close", "res_id": adjustment.id}

    def _distri_move(self, partner, difference, motive):
        """Creates and posts the AJC credit note (or debit) for the difference."""
        company = self.env.company
        journal = self.env["account.journal"]._distri_ajc_journal(company)
        move = self.env["account.move"].sudo().with_company(company).create({
            "move_type": "out_invoice" if difference > 0 else "out_refund",
            "partner_id": partner.id,
            "journal_id": journal.id,
            "invoice_date": self.date,
            "date": self.date,
            # the concept on the printed statement: never the authorizer
            "ref": "Ajuste de cuenta",
            "narration": motive,
            "invoice_line_ids": [Command.create({
                "name": "Ajuste de cuenta: %s" % motive.splitlines()[0][:200],
                "quantity": 1,
                "price_unit": abs(difference),
                "tax_ids": [Command.clear()],
            })],
        })
        move.action_post()
        return move
