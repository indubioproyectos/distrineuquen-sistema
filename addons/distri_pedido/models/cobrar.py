"""«Cobrar» in one step (19.0.1.128.0, docs cobros/analisis-cobro-un-paso-2026-11-21.md,
«Objetivo 1»).

One engine, `distri.cobro.collect`, behind the «Cobrar» button of the
customer invoice (the wizard of models/cobrar_wizard.py) and the page /cobro
(controllers/cobro.py). One collection may mix cash and one or more
transfers, to one destination:

- «A cuenta»: the oldest open lab invoices first, as always;
- «Factura del sistema»: an open invoice of this Odoo, reconciled;
- «Factura de Online»: an invoice of the Odoo Online history still owed on
  the statement, paid the 19.0.1.125.0 way (an unreconciled payment whose
  memo ends with «· 0001/2026/NNNNN» and whose `distri_history_invoice_id`
  links it; never `_apply_oldest_first`).

The cash: a cashier's goes to the caja journal (CSH1, `distri.caja.
_session_journal`) and is posted without NO_INSTANT_CAJA, so the v124 hook
(models/caja_pago.py) puts it in his open caja at once; without an open caja
it is collected anyway with the v124 notice. Anyone else's goes to his own
«Caja depósito» box exactly as /cobro did (`cash_register`).

Each transfer REQUIRES its receipt (photo or PDF). It goes through the v126
machinery only: the staff upload (`_distri_upload_receipts`), the reading,
the receipt's «verify» card (customer and destination set on it) and
`_distri_credit` with every v126 defense and warning, so the customer's
account goes down at once. No parallel crediting path.

Two phases, so nothing has to be attached twice: `collect_receipts` stores
and reads the new receipt files (kept even when the rest is refused: a
pending staff upload, as with «Subir y acreditar»), then `collect`, inside the
caller's savepoint, checks everything and writes. The questions (order on a
route, amount above the debt, a repeat, the v126 credit warnings) raise
before anything is written; the caller asks and sends the flag back.

A `distri.cobro` row keeps what was collected together: its payments and
receipt cards (the links of «Comprobante» on the payment and «Pagos del
cobro» on the card).

Who collects (`can_collect`, also who opens /cobro): cajero, reparto,
vendedor, administración, gerencia and Sistemas, the v126 «Acreditar
comprobantes» set.
"""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .acreditar import CREDIT_GROUPS, CreditWarning, _in_groups
from .caja import is_cashier
from .caja_pago import NO_OPEN_CAJA_NOTICE
from .cobro_efectivo import DUPLICATE_MINUTES, DuplicateCashError, OverDebtWarning, RouteCashWarning
from .comprobante_subir import MAX_FILE_BYTES, receipt_kind
from .ocr_comprobante import CONFIDENCE_HIGH, ocr_deadline, ocr_seconds
from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

COLLECT_GROUPS = CREDIT_GROUPS
COLLECT_DENIED = ("Tu usuario no cobra. Cobran cajeros, repartidores, vendedores, administración, "
                  "gerencia y Sistemas: si lo necesitás, pedile acceso a administración.")
DESTINATIONS = [("account", "A cuenta"), ("lab", "Factura del sistema"),
                ("online", "Factura de Online")]
MAX_TRANSFERS = 5
RECEIPT_MISSING = "Transferencia %d: adjuntá la foto o el PDF del comprobante."
AMOUNT_MISSING = ("Transferencia %d: escribí el importe (no se pudo leer del comprobante con "
                  "seguridad).")
NOTHING_TO_COLLECT = ("Poné un importe mayor que cero: en efectivo o en una transferencia con su "
                      "comprobante.")
CHOOSE_INVOICE = "Elegí la factura que paga."
# the form flag that answers each question (controllers/cobro.py, the wizard)
CREDIT_OK = "acreditar_ok"


def can_collect(user):
    """«Cobrar» and /cobro: the staff that collects (see the module docstring)."""
    return bool(user) and (user._is_superuser() or _in_groups(user, COLLECT_GROUPS))


def question_field(exc):
    """The form flag that confirms the question `exc` asked, or None."""
    if isinstance(exc, RouteCashWarning):
        return exc.field
    if isinstance(exc, OverDebtWarning):
        return "deuda_ok"
    if isinstance(exc, DuplicateCashError):
        return "confirmado"
    if isinstance(exc, CreditWarning):
        return CREDIT_OK
    return None


QUESTIONS = (RouteCashWarning, OverDebtWarning, DuplicateCashError, CreditWarning)


class DistriCobro(models.Model):
    _name = "distri.cobro"
    _description = "Cobro en un paso"
    _order = "id desc"
    _rec_name = "memo"

    user_id = fields.Many2one("res.users", string="Cobró", required=True, readonly=True,
                              index=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, readonly=True,
                                 index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    destination = fields.Selection(DESTINATIONS, string="Destino", required=True, readonly=True)
    invoice_id = fields.Many2one("account.move", string="Factura del sistema", readonly=True)
    history_invoice_id = fields.Many2one("distri.cuenta.movimiento", string="Factura de Online",
                                         readonly=True)
    cash_amount = fields.Monetary(string="Efectivo", readonly=True)
    transfer_amount = fields.Monetary(string="Transferencias", readonly=True)
    amount = fields.Monetary(string="Total", readonly=True)
    note = fields.Char(string="Nota", readonly=True)
    memo = fields.Char(string="Detalle", readonly=True)
    payment_ids = fields.Many2many("account.payment", "distri_cobro_payment_rel", "cobro_id",
                                   "payment_id", string="Pagos", readonly=True)
    card_ids = fields.Many2many("distri.mp.collection", "distri_cobro_card_rel", "cobro_id",
                                "card_id", string="Comprobantes", readonly=True)
    cash_payment_id = fields.Many2one("account.payment", string="Pago en efectivo", readonly=True)
    cash_collection_id = fields.Many2one("distri.cash.collection", string="Caja depósito",
                                         readonly=True)
    caja_id = fields.Many2one("distri.caja", string="Caja del día", readonly=True)
    caja_notice = fields.Char(string="Aviso de caja", readonly=True)

    # ------------------------------------------------------------ checks

    @api.model
    def _collect_check_user(self):
        user = self.env.user
        if user._is_public() or user.share or not can_collect(user):
            raise AccessError(COLLECT_DENIED)
        return user

    @api.model
    def _collect_amount(self, value):
        """An optional amount: empty or zero is 0.0; else the /cobro rules."""
        text = str(value if value not in (None, False) else "").strip()
        if text.replace("$", "").strip() in ("", "0", "0,00", "0.00", "0,0"):
            return 0.0
        return self.env["distri.cash.collection"]._cash_amount(value)

    @api.model
    def _collect_id(self, value):
        try:
            return max(int(value or 0), 0)
        except (TypeError, ValueError):
            return 0

    # ------------------------------------------------------------ phase 1: receipts

    @api.model
    def collect_receipts(self, partner_id, transfers):
        """Stores and reads the receipt of each transfer that brings a new
        file; returns `transfers` with «receipt» (a distri.whatsapp.message)
        in each. A transfer is {"amount": text, "file": (name, bytes) or
        None, "receipt_id": id or None, "rotation": degrees}. Empty slots
        (no file, no receipt, no amount) are dropped. Commits nothing by
        itself; the caller keeps what this stores even if `collect` refuses."""
        user = self._collect_check_user()
        partner = self.env["distri.cash.collection"]._cash_partner(partner_id)
        Message = self.env["distri.whatsapp.message"]
        out = []
        for transfer in transfers or ():
            raw_file = transfer.get("file")
            if raw_file and not raw_file[1]:
                raw_file = None
            receipt_id = self._collect_id(transfer.get("receipt_id"))
            amount = str(transfer.get("amount") or "").strip()
            if not (raw_file or receipt_id or amount):
                continue
            out.append(dict(transfer, file=raw_file, receipt_id=receipt_id, amount=amount))
        if len(out) > MAX_TRANSFERS:
            raise UserError("Cargá hasta %d transferencias por cobro." % MAX_TRANSFERS)
        for n, transfer in enumerate(out, 1):
            raw_file = transfer["file"]
            if raw_file:
                if len(raw_file[1]) > MAX_FILE_BYTES:
                    raise UserError("Transferencia %d: el comprobante es demasiado grande (más de "
                                    "10 MB)." % n)
                if not receipt_kind(raw_file[1]):
                    raise UserError("Transferencia %d: el comprobante no es una foto (JPG, PNG o "
                                    "WEBP) ni un PDF." % n)
            elif not transfer["receipt_id"]:
                raise UserError(RECEIPT_MISSING % n)
        receipts = Message.browse()
        for n, transfer in enumerate(out, 1):
            if transfer["file"]:
                transfer["receipt"] = self._collect_store_receipt(n, partner, transfer)
            else:
                transfer["receipt"] = self._collect_own_receipt(n, user, transfer["receipt_id"])
            receipts |= transfer["receipt"]
        # one request, one OCR budget (models/ocr_comprobante.py)
        with ocr_deadline(ocr_seconds(self.env)):
            receipts._distri_read_now()
        return out

    @api.model
    def _collect_own_receipt(self, n, user, receipt_id):
        """A receipt stored by an earlier try of this same collection: the
        user's own staff upload, still pending."""
        msg = self.env["distri.whatsapp.message"].sudo().browse(receipt_id).exists()
        if not msg or not msg.distri_upload or msg.distri_forwarded_by_id != user:
            raise UserError(RECEIPT_MISSING % n)
        return msg

    @api.model
    def _collect_store_receipt(self, n, partner, transfer):
        """The staff upload of 19.0.1.126.1 for one transfer. The same file
        already uploaded by this user and still pending (an earlier try that
        was not collected) is used again instead of being refused."""
        Message = self.env["distri.whatsapp.message"]
        name, raw = transfer["file"]
        existing = Message.sudo()._distri_upload_duplicate(raw)
        if existing and existing.distri_upload \
                and existing.distri_forwarded_by_id == self.env.user \
                and existing.distri_receipt_state == "pending" \
                and existing._distri_verify_card().state in (False, "draft"):
            return existing
        [result] = Message._distri_upload_receipts(
            [(name, raw)], partner=partner, via="cobrar", amounts=[transfer["amount"]],
            rotations=[transfer.get("rotation") or 0])
        if result["status"] != "nuevo":
            raise UserError("Transferencia %d: %s" % (n, result["message"]))
        return Message.sudo().browse(result["id"])

    # ------------------------------------------------------------ phase 2: collect

    @api.model
    def collect(self, partner_id, destination="account", invoice_id=False, history_id=False,
                cash_amount=0, transfers=None, note="", confirmed=False, route_ok=False,
                force=False, over_ok=False, credit_ok=False):
        """Collects (see the module docstring). `transfers`: what
        `collect_receipts` returned. Raises a question of QUESTIONS when its
        flag is not set, UserError/AccessError when refused. Returns the
        distri.cobro."""
        user = self._collect_check_user()
        Cash = self.env["distri.cash.collection"]
        partner = Cash._cash_partner(partner_id)
        if destination not in dict(DESTINATIONS):
            raise UserError("Elegí a qué se aplica el cobro.")
        cash = self._collect_amount(cash_amount)
        note = " ".join((note or "").split())[:200]
        lines = self._collect_transfer_lines(user, transfers or [])
        total = round(cash + sum(line["amount"] for line in lines), 2)
        if not total > 0:
            raise UserError(NOTHING_TO_COLLECT)
        invoice, history = self.env["account.move"], self.env["distri.cuenta.movimiento"]
        if destination == "lab":
            if not self._collect_id(invoice_id):
                raise UserError(CHOOSE_INVOICE)
            invoice = Cash._cash_invoice(partner, self._collect_id(invoice_id), total)
        elif destination == "online":
            if not self._collect_id(history_id):
                raise UserError(CHOOSE_INVOICE)
            history, _owed = Cash._cash_history(partner, self._collect_id(history_id), total)
        stops = self.env["distri.route.stop"].sudo()
        if destination != "online":
            # an Online invoice is older than any route of this Odoo
            stops = Cash._cash_check_routes(partner, invoice, route_ok=route_ok, force=force)
        if not over_ok:
            Cash._cash_check_debt(partner, total)
        # two pages sent at once for one customer: the second waits here
        self.env.cr.execute("UPDATE res_partner SET write_date = write_date WHERE id = %s",
                            [partner.id])
        if cash and not confirmed:
            previous = self._collect_duplicate(user, partner, cash, destination, invoice, history)
            if previous:
                raise DuplicateCashError(
                    "Ya registraste %s en efectivo de %s hace un momento (%s). Si es otro cobro, "
                    "confirmalo." % (_money(cash), partner.name, previous.memo))
        self._collect_review(lines, partner, destination, invoice, history, credit_ok)
        # every check passed: write
        payments = self.env["account.payment"]
        for line in lines:
            payments |= self.env["distri.mp.collection"].browse(line["card"].id)._distri_credit(
                partner, line["amount"], confirmed=credit_ok)
        cobro_vals = {}
        if cash:
            cobro_vals = self._collect_cash(user, partner, cash, invoice, history, note, stops)
            payments |= cobro_vals["cash_payment_id"]
        cards = self.env["distri.mp.collection"].sudo().browse([line["card"].id for line in lines])
        transfer_total = round(total - cash, 2)
        cobro = self.sudo().create(dict({
            "user_id": user.id,
            "partner_id": partner.id,
            "company_id": self.env.company.id,
            "destination": destination,
            "invoice_id": invoice.id or False,
            "history_invoice_id": history.id or False,
            "cash_amount": cash,
            "transfer_amount": transfer_total,
            "amount": total,
            "note": note or False,
            "memo": self._collect_memo(user, cash, transfer_total, len(lines), invoice, history),
            "payment_ids": [(6, 0, payments.ids)],
            "card_ids": [(6, 0, cards.ids)],
        }, **{key: value.id if hasattr(value, "id") else value
              for key, value in cobro_vals.items()}))
        body = Markup("Cobro en un paso de <b>%s</b> (%s), registrado por %s.") % (
            _money(total), cobro.memo, user.name)
        partner.sudo().message_post(body=body)
        if invoice:
            invoice.sudo().message_post(body=body)
        _logger.info("Cobro %s by %s: partner %s, cash %s, transfers %s (%s), %s", cobro.id,
                     user.login, partner.id, cash, transfer_total, len(lines), destination)
        return cobro

    @api.model
    def _collect_transfer_lines(self, user, transfers):
        """[{n, receipt, card, amount}] of the transfers: the amount typed,
        else the one read with high confidence; the receipt's draft «verify»
        card the user may credit."""
        lines = []
        for n, transfer in enumerate(transfers, 1):
            msg = transfer.get("receipt")
            if not msg:
                raise UserError(RECEIPT_MISSING % n)
            msg = msg.sudo()
            typed = str(transfer.get("amount") or "").strip()
            amount = self._collect_amount(typed) if typed else 0.0
            if not amount:
                read = msg.distri_read_amount \
                    if msg.distri_amount_confidence == CONFIDENCE_HIGH else 0.0
                amount = msg.distri_amount_typed or read
            if not amount:
                raise UserError(AMOUNT_MISSING % n)
            card = msg._distri_credit_card(user)
            if not card:
                msg.invalidate_recordset()
                if msg.distri_receipt_state != "pending":
                    state = dict(msg._fields["distri_receipt_state"].selection).get(
                        msg.distri_receipt_state, msg.distri_receipt_state or "")
                    reason = "el comprobante quedó «%s»%s" % (
                        state, (": %s" % msg.distri_receipt_note) if msg.distri_receipt_note
                        else "")
                else:
                    reason = "no se puede acreditar desde acá; lo revisa administración"
                raise UserError("Transferencia %d: %s." % (n, reason))
            lines.append({"n": n, "receipt": msg, "card": card, "amount": round(amount, 2)})
        return lines

    @api.model
    def _collect_review(self, lines, partner, destination, invoice, history, credit_ok):
        """The v126 review of each transfer's card, with the customer and the
        destination set on it: errors refuse, warnings ask (CREDIT_OK)."""
        errors, warnings = [], []
        for line in lines:
            card = line["card"].sudo()
            vals = {"pay_move_id": invoice.id or False, "pay_invoice_id": history.id or False}
            if card.partner_id.commercial_partner_id != partner:
                vals.update(partner_id=partner.id, proposal="manual",
                            proposal_note="Elegido por %s al cobrar" % self.env.user.name)
            card.write(vals)
            card_errors, card_warnings = self.env["distri.mp.collection"].browse(
                card.id)._distri_credit_review(partner, line["amount"])
            prefix = "Transferencia %d: " % line["n"] if len(lines) > 1 else ""
            errors += [prefix + text for text in card_errors]
            warnings += [prefix + text for text in card_warnings]
        if errors:
            raise UserError("\n".join(errors))
        if warnings and not credit_ok:
            raise CreditWarning(warnings)

    @api.model
    def _collect_duplicate(self, user, partner, cash, destination, invoice, history):
        since = fields.Datetime.now() - timedelta(minutes=DUPLICATE_MINUTES)
        return self.sudo().search([
            ("user_id", "=", user.id), ("partner_id", "=", partner.id),
            ("cash_amount", "=", cash), ("destination", "=", destination),
            ("invoice_id", "=", invoice.id or False),
            ("history_invoice_id", "=", history.id or False),
            ("create_date", ">=", since)], limit=1)

    @api.model
    def _collect_cash(self, user, partner, cash, invoice, history, note, stops):
        """Posts the cash part; returns the distri.cobro values it fills."""
        Cash = self.env["distri.cash.collection"]
        if not is_cashier(user):
            # the /cobro box of the user, as before (models/cobro_efectivo.py);
            # every question was asked above
            collection = Cash.cash_register(
                partner.id, cash, invoice_id=invoice.id or False, note=note, confirmed=True,
                route_ok=True, force=True, over_ok=True, history_id=history.id or False)
            return {"cash_payment_id": collection.payment_id,
                    "cash_collection_id": collection}
        company = self.env.company
        journal = self.env["distri.caja"]._session_journal(company)
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": cash,
            "journal_id": journal.id,
            "date": today_ar(),
            "memo": Cash._cash_memo_day(user, today_ar(), invoice, note, history),
            "company_id": company.id,
            "distri_history_invoice_id": history.id or False,
        })
        # no NO_INSTANT_CAJA: the v124 hook takes it into the open caja now
        payment.action_post()
        if invoice:
            Cash._cash_reconcile(payment, invoice)
        elif not history:
            self.env["distri.route"].sudo()._apply_oldest_first(payment, partner)
        Cash._cash_notify_stops(user, partner, cash, invoice, stops)
        caja = payment._distri_open_caja()
        notice = NO_OPEN_CAJA_NOTICE if not caja and payment._distri_waits_for_caja() else False
        return {"cash_payment_id": payment, "caja_id": caja, "caja_notice": notice}

    @api.model
    def _collect_memo(self, user, cash, transfers, count, invoice, history):
        parts = []
        if cash:
            parts.append("efectivo %s" % _money(cash))
        if transfers:
            parts.append("%s %s" % ("transferencia" if count == 1 else "%d transferencias" % count,
                                    _money(transfers)))
        target = "factura %s" % invoice.name if invoice else (
            "factura %s (sistema anterior)" % (history.comprobante or "") if history else "a cuenta")
        text = " + ".join(parts)
        return " · ".join((text[:1].upper() + text[1:], target, "cobró %s" % user.name))

    # ------------------------------------------------------------ links

    def action_open_payments(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Pagos del cobro",
            "res_model": "account.payment",
            "view_mode": "list,form",
            "domain": [("id", "in", self.sudo().payment_ids.ids)],
            "context": {"create": False},
        }

    def action_open_cards(self):
        self.ensure_one()
        cards = self.sudo().card_ids
        action = {"type": "ir.actions.act_window", "name": "Comprobantes del cobro",
                  "res_model": "distri.mp.collection", "context": {"create": False}}
        if len(cards) == 1:
            return dict(action, view_mode="form", res_id=cards.id,
                        views=[(self.env.ref("distri_pedido.view_mp_collection_form").id, "form")])
        return dict(action, view_mode="list,form", domain=[("id", "in", cards.ids)])


class AccountPaymentCobroLink(models.Model):
    """«Comprobante» and «Cobro» on the payment (19.0.1.128.0)."""
    _inherit = "account.payment"

    distri_receipt_card_ids = fields.Many2many(
        "distri.mp.collection", string="Comprobantes", compute="_compute_distri_cobro_links")
    distri_receipt_card_count = fields.Integer(compute="_compute_distri_cobro_links")
    distri_cobro_id = fields.Many2one("distri.cobro", string="Cobro en un paso",
                                      compute="_compute_distri_cobro_links")

    def _compute_distri_cobro_links(self):
        cards = self.env["distri.mp.collection"].sudo().with_context(active_test=False).search(
            [("payment_id", "in", self.ids)]) if self.ids else self.env["distri.mp.collection"]
        cobros = self.env["distri.cobro"].sudo().search(
            [("payment_ids", "in", self.ids)]) if self.ids else self.env["distri.cobro"]
        for payment in self:
            mine = cards.filtered(lambda c: c.payment_id == payment)
            cobro = cobros.filtered(lambda c: payment in c.payment_ids)[:1]
            # the receipts of the same one-step collection, for its cash too
            mine |= cobro.card_ids
            payment.distri_receipt_card_ids = mine
            payment.distri_receipt_card_count = len(mine)
            payment.distri_cobro_id = cobro

    def action_distri_open_receipt(self):
        self.ensure_one()
        cards = self.sudo().distri_receipt_card_ids
        if not cards:
            raise UserError("Este pago no tiene comprobante.")
        action = {"type": "ir.actions.act_window", "name": "Comprobante",
                  "res_model": "distri.mp.collection", "context": {"create": False}}
        if len(cards) == 1:
            return dict(action, view_mode="form", res_id=cards.id,
                        views=[(self.env.ref("distri_pedido.view_mp_collection_form").id, "form")])
        return dict(action, view_mode="list,form", domain=[("id", "in", cards.ids)])

    def action_distri_open_cobro(self):
        self.ensure_one()
        cobro = self.sudo().distri_cobro_id
        if not cobro:
            raise UserError("Este pago no es de un cobro en un paso.")
        return {"type": "ir.actions.act_window", "name": "Cobro", "res_model": "distri.cobro",
                "view_mode": "form", "res_id": cobro.id}


class DistriMpCollectionCobroLink(models.Model):
    """«Pagos del cobro» on the receipt card (19.0.1.128.0)."""
    _inherit = "distri.mp.collection"

    distri_cobro_id = fields.Many2one("distri.cobro", string="Cobro en un paso",
                                      compute="_compute_distri_cobro_payments")
    distri_cobro_payment_count = fields.Integer(string="Pagos del cobro",
                                                compute="_compute_distri_cobro_payments")

    def _compute_distri_cobro_payments(self):
        cobros = self.env["distri.cobro"].sudo().search(
            [("card_ids", "in", self.ids)]) if self.ids else self.env["distri.cobro"]
        for card in self:
            cobro = cobros.filtered(lambda c: card in c.card_ids)[:1]
            card.distri_cobro_id = cobro
            card.distri_cobro_payment_count = len(cobro.payment_ids | card.sudo().payment_id)

    def action_distri_open_payments(self):
        """The card's payment and the other payments of its one-step
        collection (the cash of a mixed one)."""
        self.ensure_one()
        card = self.sudo()
        payments = card.distri_cobro_id.payment_ids | card.payment_id
        if not payments:
            raise UserError("Este comprobante no tiene pagos.")
        if len(payments) == 1:
            return {"type": "ir.actions.act_window", "name": "Pago",
                    "res_model": "account.payment", "view_mode": "form", "res_id": payments.id}
        return {"type": "ir.actions.act_window", "name": "Pagos del cobro",
                "res_model": "account.payment", "view_mode": "list,form",
                "domain": [("id", "in", payments.ids)], "context": {"create": False}}
