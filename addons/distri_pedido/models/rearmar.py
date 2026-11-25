"""19.0.1.122.0 «Rearmar»: an invoiced order goes back to /armado.

Once an order has an invoice or a done depot delivery it never returns to
the packing queue (`_distri_sin_entregar`, models/armado.py). On 20/11/2026
one was fixed by script; the button does the same:

    invoice.button_draft(); invoice.button_cancel()
    old._action_cancel()
    new = old.copy({"note": old.note}); new.action_confirm()

The new order is «Pendiente» again (distri_armado is copy=False) and the
packer takes it like any other. Both orders link each other in the chatter.

Only administración (gerencia implies it) runs it, checked here and not only
in the view. Refused when:
- the order is not confirmed (state «sale»);
- an invoice has payments or reconciled lines (a credit note, a receipt):
  money is involved, that is administración's to undo first;
- an invoice carries a CAE of ARCA (l10n_ar_edi): an authorized electronic
  invoice cannot be cancelled, only voided with a credit note. On 20/11/2026
  production has no electronic invoicing (l10n_ar_edi not installed, the
  sale journals are not ARCA points of sale), so this guard is for the day
  it is turned on;
- the order was delivered: a delivery stop delivered or dispatched by
  courier, or a dispatched parcel. The depot delivery (stock picking) is
  NOT this check: it is validated when packing ends (factura_armado.py), so
  every invoiced order has one done; it stays done, as in the 20/11 script;
- the order is on a route not visited yet: the stop would keep pointing at
  a cancelled order. It is taken off the route first.
"""
from markupsafe import Markup

from odoo import fields, models
from odoo.exceptions import UserError

REARMAR_GROUP = "distri_pedido.grupo_administracion"

DELIVERED_STATES = ("delivered_code", "delivered_no_code", "dispatched")

NOT_ADMIN = "Solo administración puede rearmar un pedido."
NOT_CONFIRMED = "Solo se rearma un pedido confirmado."
HAS_PAYMENTS = "La factura %s tiene pagos o conciliaciones: quitalos antes de rearmar."
HAS_CAE = ("La factura %s tiene CAE de ARCA: no se puede cancelar. "
           "Hacé una nota de crédito.")
DELIVERED = "El pedido %s ya fue entregado: no se rearma."
ON_ROUTE = "El pedido %s está en la ruta %s: sacalo de la ruta antes de rearmar."


def _has_cae(invoice):
    """An invoice authorized by ARCA (l10n_ar_edi fields, when installed)."""
    if "l10n_ar_afip_auth_code" in invoice._fields and invoice.l10n_ar_afip_auth_code:
        return True
    journal = invoice.journal_id
    return invoice.state == "posted" and "l10n_ar_afip_ws" in journal._fields \
        and bool(journal.l10n_ar_afip_ws)


def _has_payments(invoice):
    if invoice.payment_state not in ("not_paid", "invoicing_legacy"):
        return True
    lines = invoice.line_ids.filtered(
        lambda l: l.account_id.account_type in ("asset_receivable", "liability_payable"))
    return bool(lines.matched_debit_ids or lines.matched_credit_ids)


class SaleOrderRearmar(models.Model):
    _inherit = "sale.order"

    def _distri_rearmar_check(self):
        """Raise the reason this order cannot go back to /armado (sudo)."""
        self.ensure_one()
        order = self.sudo()
        if order.state != "sale":
            raise UserError(NOT_CONFIRMED)
        for invoice in order.invoice_ids.filtered(lambda f: f.state != "cancel"):
            if _has_cae(invoice):
                raise UserError(HAS_CAE % (invoice.name or invoice.display_name))
            if _has_payments(invoice):
                raise UserError(HAS_PAYMENTS % (invoice.name or invoice.display_name))
        stops = order.distri_stop_ids
        if order.distri_parcel_ids or stops.filtered(
                lambda s: s.delivery_state in DELIVERED_STATES):
            raise UserError(DELIVERED % order.name)
        open_stop = stops.filtered(lambda s: s.delivery_state == "pending")[:1]
        if open_stop:
            raise UserError(ON_ROUTE % (order.name, open_stop.route_id.display_name))

    def action_distri_rearmar(self):
        """«Rearmar»: cancel the invoice and the order, and confirm a copy
        that goes back to the packing queue. Opens the new order."""
        self.ensure_one()
        if not (self.env.su or self.env.user.has_group(REARMAR_GROUP)):
            raise UserError(NOT_ADMIN)
        old = self.sudo()
        # same lock as the packer and the customer's edits
        old._distri_bloquear()
        old._distri_rearmar_check()
        # 19.0.1.129.35 (plan, Etapa 4): with «Nueva venta» on, «Rearmar» is
        # a correction tool and asks its reason first (distri.rearmar.wizard)
        from .nueva_venta import nueva_venta_on
        motivo = " ".join((self.env.context.get("distri_rearmar_motivo") or "").split())[:250]
        correccion = nueva_venta_on(self.env)
        if correccion and not motivo:
            return {
                "type": "ir.actions.act_window",
                "name": "Rearmar · corrección",
                "res_model": "distri.rearmar.wizard",
                "view_mode": "form",
                "target": "new",
                "context": {"default_order_id": old.id},
            }

        # 19.0.1.129.26: the order is cancelled on purpose, no «factura
        # anulada» note (models/factura_anulada.py)
        invoices = old.invoice_ids.with_context(distri_skip_cancel_notice=True)
        for invoice in invoices.filtered(lambda f: f.state != "cancel"):
            if invoice.state == "posted":
                invoice.button_draft()
            invoice.button_cancel()
        old._action_cancel()
        new = old.copy({"note": old.note})
        new.action_confirm()

        nota = (Markup(" Corrección de %s. Motivo: %s.") % (self.env.user.name, motivo)
                if motivo else "")
        new.message_post(body=Markup("Pedido rearmado: reemplaza a %s, que se canceló con su "
                                     "factura. Vuelve al armado.") % old._get_html_link() + nota)
        old.message_post(body=Markup("Rearmado: se canceló con su factura y se creó %s para "
                                     "volver a armarlo.") % new._get_html_link() + nota)
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": new.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }


class DistriRearmarWizard(models.TransientModel):
    """19.0.1.129.35: «Rearmar» as a correction (with «Nueva venta» on): the
    reason, then the same «Rearmar» as before."""
    _name = "distri.rearmar.wizard"
    _description = "Rearmar un pedido (corrección)"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, readonly=True,
                               ondelete="cascade")
    motivo = fields.Char(string="Motivo de la corrección", required=True)

    def action_rearmar(self):
        self.ensure_one()
        motivo = " ".join((self.motivo or "").split())
        if len(motivo) < 3:
            raise UserError("Escribí el motivo de la corrección.")
        # without the wizard's default_* keys, which would leak into the
        # records «Rearmar» creates (as distri.wa.confirm.wizard does)
        context = {key: value for key, value in self.env.context.items()
                   if not key.startswith("default_")}
        context["distri_rearmar_motivo"] = motivo
        return self.order_id.with_context(context).action_distri_rearmar()
