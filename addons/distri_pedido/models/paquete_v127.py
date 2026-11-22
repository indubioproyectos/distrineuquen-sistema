"""Small improvements of 19.0.1.127.0.

- Who packed the order: the common label prints «Armó: Juan» (first name,
  the same cut as `dispatch_by`), the driver's stop carries `packed_by`,
  `packed_at` (HH:MM, Argentina) and `missing` (the lines with a shortage,
  also the fully missing ones the payload's `lines` leave out), and the
  backend «Armado» tab shows /armado's sentence «Armado por X a las HH:MM,
  en N bultos.» (`distri_armado_resumen`).
- «Foto en pestaña nueva» on a WhatsApp receipt (`action_tab_receipt`, as on
  the migration board).
- Customer suggestion by invoice number (`invoice_suggestions`): our format
  «0001/2026/07963» and a bare 5-digit tail, parsed like the v123 memo
  (models/estado_cuenta.py `_memo_refs`: never «S00241», an amount or a
  date; an amount after «$» is dropped first), looked up in the lab
  invoices and the Online history (distri.cuenta.movimiento tipo factura).
  A tail suggests only when it names one customer.

The self-dispatch (administración/gerencia controlling their own route's
load) lives in models/despacho.py. No new column is stored.
"""
import re

from odoo import api, fields, models
from odoo.exceptions import UserError

from .despacho import ar_time, first_name
from .estado_cuenta import _memo_refs
from .migracion import receipt_file_url, tab_action

_AMOUNT_RE = re.compile(r"\$\s*[\d.,]+")


def packed_sentence(order):
    """«Armado por Juan Pérez a las 10:42, en 3 bultos.» (/armado's banner),
    or "" while the order is not packed."""
    if order.distri_armado != "armado":
        return ""
    text = "Armado por %s" % (order.distri_armador_id.name or "alguien")
    if order.distri_armado_fin:
        text += " a las %s" % ar_time(order.distri_armado_fin)
    if order.distri_bultos:
        text += ", en %s %s" % (order.distri_bultos,
                                "bulto" if order.distri_bultos == 1 else "bultos")
    return text + "."


def invoice_suggestions(env, text):
    """[(partners, reason)] for the invoice numbers in a receipt's text: a full
    number gives every customer it belongs to (lab and Online), a tail only
    when exactly one customer has an invoice ending in it."""
    Move = env["account.move"].sudo()
    History = env["distri.cuenta.movimiento"].sudo()
    Partner = env["res.partner"]
    out = []
    # a receipt's text is full of amounts: «$ 12345» is never an invoice tail
    for kind, ref in _memo_refs(_AMOUNT_RE.sub(" ", text or "")):
        if kind == "full":
            name_domain, history_domain = [("name", "=", ref)], [("comprobante", "=", ref)]
        else:
            pattern = "____/____/%s" % ref
            name_domain, history_domain = [("name", "=like", pattern)], \
                [("comprobante", "=like", pattern)]
        moves = Move.search([("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                             ("company_id", "in", env.companies.ids)] + name_domain,
                            order="invoice_date desc, id desc", limit=20)
        rows = History.search([("tipo", "=", "factura")] + history_domain,
                              order="fecha desc, id desc", limit=20)
        partners = (moves.partner_id | rows.partner_id).commercial_partner_id
        if not partners or (kind == "tail" and len(partners) != 1):
            continue
        number = ref if kind == "full" else (moves[:1].name or rows[:1].comprobante)
        out.append((Partner.sudo().browse(partners.ids), "por factura %s" % number))
    return out


class SaleOrderPackedV127(models.Model):
    _inherit = "sale.order"

    distri_armado_resumen = fields.Char(
        string="Resumen del armado", compute="_compute_distri_armado_resumen",
        help="La misma frase que /armado: quién lo armó, a qué hora y en cuántos bultos.")

    @api.depends("distri_armado", "distri_armador_id", "distri_armado_fin", "distri_bultos")
    def _compute_distri_armado_resumen(self):
        for order in self:
            order.distri_armado_resumen = packed_sentence(order)

    def _distri_missing_lines(self):
        """[{product, qty}] of the lines the packer marked short."""
        self.ensure_one()
        return [{
            "product": line.product_id.with_context(display_default_code=False).display_name,
            "qty": line.distri_faltante,
        } for line in self.sudo().order_line
            if not line.display_type and line.distri_faltante > 0]


class DistriPackageV127(models.Model):
    _inherit = "distri.package"

    def _label_values(self):
        values = super()._label_values()
        values["packer"] = first_name(self.order_id.sudo().distri_armador_id)
        return values


class DistriRouteStopV127(models.Model):
    _inherit = "distri.route.stop"

    def _app_payload(self):
        values = super()._app_payload()
        order = self.order_id.sudo()
        values.update({
            "packed_by": first_name(order.distri_armador_id),
            "packed_at": ar_time(order.distri_armado_fin),
            "missing": order._distri_missing_lines(),
        })
        return values


class WhatsappMessageTabV127(models.Model):
    _inherit = "distri.whatsapp.message"

    def action_tab_receipt(self):
        """«Foto en pestaña nueva»: the stored file in a new browser tab."""
        self.ensure_one()
        url = receipt_file_url(self)
        if not url:
            raise UserError("Este comprobante no tiene foto.")
        return tab_action(url)
