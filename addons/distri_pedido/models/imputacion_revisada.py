"""«Imputación revisada» (19.0.1.129.9): reviewed allocations of the red rows.

mark_owed (models/estado_cuenta.py) decides which invoices show red by
priority passes. Its total always equals the balance, but WHICH invoices are
red can be wrong (a split payment arriving after 7 days, a reconciliation
pairing a payment with the wrong document). An analyst reviews the account
and states explicit allocations: «this credit row pays that debit row for
this amount». They are stored here and applied first (mark_owed, pass 0),
so they survive every recomputation; the other passes run on what is left.

Rows are identified by their stable `line_key` (statement_line_key):
«account.move:<id>[:n]», «online:<origen_id>», «distri.route.return:<id>».
An allocation whose keys are not in the statement, or whose credit / debit
side is wrong, is skipped (never an error): the statement may have changed
since the review.

Staff see the reason as «Revisado: …» in the row's review note; the customer
views (models/remito.py, «Mi cuenta», the public link) never read it.

Loaded by scripts/rojo/cargar-imputaciones.sh (dry-run by default).
"""
from odoo import api, fields, models
from odoo.exceptions import ValidationError

CONFIDENCE = [("segura", "Segura"), ("probable", "Probable"), ("dudosa", "Dudosa")]
REVIEWED_PREFIX = "Revisado: "


class DistriOwedAllocation(models.Model):
    _name = "distri.owed.allocation"
    _description = "Imputación revisada"
    _order = "partner_id, id"
    _rec_name = "debit_key"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, index=True,
                                 ondelete="cascade", domain=[("parent_id", "=", False)])
    credit_key = fields.Char(string="Pago / crédito (clave)", required=True,
                             help="line_key de la fila del estado de cuenta que paga.")
    debit_key = fields.Char(string="Factura / débito (clave)", required=True,
                            help="line_key de la fila del estado de cuenta que se paga.")
    amount = fields.Float(string="Importe", required=True, digits=(16, 2))
    confidence = fields.Selection(CONFIDENCE, string="Confianza", required=True,
                                  default="segura")
    reason = fields.Text(string="Motivo")
    source = fields.Char(string="Origen", index=True,
                         help="De dónde sale, p. ej. «Revisión Fable 23/11/2026».")
    approved_by = fields.Char(string="Aprobada por")
    active = fields.Boolean(string="Activa", default=True)

    @api.constrains("amount", "credit_key", "debit_key")
    def _check_allocation(self):
        for alloc in self:
            if alloc.amount <= 0:
                raise ValidationError("El importe de la imputación debe ser mayor que cero.")
            if (alloc.credit_key or "").strip() == (alloc.debit_key or "").strip():
                raise ValidationError("El pago y la factura de la imputación son la misma fila.")

    @api.model
    def _commercial(self, vals):
        if vals.get("partner_id"):
            vals["partner_id"] = self.env["res.partner"].browse(
                vals["partner_id"]).commercial_partner_id.id
        for key in ("credit_key", "debit_key"):
            if vals.get(key):
                vals[key] = vals[key].strip()
        return vals

    @api.model_create_multi
    def create(self, vals_list):
        return super().create([self._commercial(dict(vals)) for vals in vals_list])

    def write(self, vals):
        return super().write(self._commercial(dict(vals)))


class ResPartnerOwedAllocation(models.Model):
    _inherit = "res.partner"

    def _distri_owed_allocations(self):
        """The active reviewed allocations of the customer, as plain dicts
        for mark_owed (one query, oldest first; active even when the caller
        reads with active_test=False)."""
        self.ensure_one()
        Alloc = self.env["distri.owed.allocation"].sudo().with_context(active_test=True)
        allocs = Alloc.search([("partner_id", "=", self.commercial_partner_id.id)], order="id")
        return [{"id": a.id, "credit_key": a.credit_key, "debit_key": a.debit_key,
                 "amount": a.amount, "reason": a.reason or "", "confidence": a.confidence}
                for a in allocs]
