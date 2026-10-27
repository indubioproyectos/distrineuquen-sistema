"""Review of the new payments per customer (19.0.1.39.0).

The lab balance of a customer is its Odoo Online balance minus the payments
added in the lab (the Mercado Pago board and the cash sheets). Those
payments, and the pending board cards proposed for the customer, were never
checked one by one against that balance. Some of them may be sales the
manager made outside the system (never invoiced): applying the payment
leaves a credit balance that does not exist.

- `distri.balance.review` (one per commercial partner, «Revisión por
  cliente»): the starting point is the Online balance, every history row
  (archived ones too: they are Online's), the same rule as «Saldos lab vs
  Online». Then the new payments, oldest first, with the running balance:
  the payments posted in the lab not already in Online (no archived history
  row points to them), then the pending board cards. A line that leaves the
  customer in credit is flagged «Posible venta sin registrar».
- Resellers are out: the generic web-order contacts, the customers with the
  hidden list (Distribuidora / revendedor) and the ones whose own price list
  is a distribuidoras list. The wholesale payers set aside on the board have
  no customer, so they never reach this screen.
- «Confirmar» runs the board's own confirmation (action_confirm), only for a
  card with its WhatsApp receipt, not duplicated, not in Online and with the
  Online mirror synced less than 24 h ago (19.0.1.41.0, models/migracion.py).
- A pending Mercado Pago movement without a WhatsApp receipt never reduces
  the debt (19.0.1.41.0): its line keeps the running balance and its amount
  only shows in «Si se aplicaran los movimientos sin comprobante», a what-if
  figure that is not applied.
- «Venta sin registrar» (a wizard shared by this screen and the board card)
  invoices an amount in the journal «Ventas sin registrar» (VSR), with the
  product «Venta sin detalle», posts it and reconciles exactly that amount
  with the payment. From the review the default is the excess (the part of
  the payment below zero) and the line must leave the customer in credit;
  from the board it is the whole payment (administración knows the transfer
  was that sale). A pending card is confirmed first with the board's own
  confirmation, without applying the payment to older invoices
  (context distri_skip_auto_apply): older invoices keep their debt.
- The review rows are rebuilt from the accounting on every «Actualizar» and
  after every action; the review state of each line survives (it is keyed
  by its card, or by its payment when there is no card).
"""
from markupsafe import Markup

from odoo import Command, api, fields, models
from odoo.exceptions import UserError

from .cobros import AMOUNT_TOLERANCE
from .puesta_al_dia import _check_admin_or_raise
from .reparto import _money

VSR_JOURNAL_CODE = "VSR"
VSR_JOURNAL_NAME = "Ventas sin registrar"
VSR_PRODUCT_XMLID = "distri_pedido.product_venta_sin_detalle"
POSTED_PAYMENT_STATES = ("in_process", "paid")

LINE_ORIGINS = [
    ("mp", "Mercado Pago"),
    ("cash", "Efectivo"),
    ("other", "Otro diario"),
    ("card", "Tarjeta pendiente"),
]
LINE_STATES = [
    ("pending", "Pendiente"),
    ("reviewed", "Revisado"),
    ("unrecorded_sale", "Venta sin registrar"),
]


def is_reseller(partner):
    """Customers out of the review: the generic contacts, the ones with the
    hidden list, the ones whose own price list is a distribuidoras list (read
    on the commercial partner, in the current company) and the ones marked
    «Es mayorista» by administración (19.0.1.41.0)."""
    partner = partner.commercial_partner_id.sudo()
    return bool(partner.distri_generic_partner or partner.distri_lista_oculta
                or partner.distri_es_mayorista
                or partner.specific_property_product_pricelist.distri_mayorista)


class ResPartnerMayorista(models.Model):
    _inherit = "res.partner"

    distri_es_mayorista = fields.Boolean(
        string="Es mayorista", tracking=True, copy=False,
        help="Mayorista o revendedor fuera del alcance de la puesta al día: no entra en "
             "«Revisión por cliente» ni se aprueba desde el tablero de migración. Lo marca "
             "administración.")


class AccountJournalVsr(models.Model):
    _inherit = "account.journal"

    def _distri_vsr_journal(self, company=None):
        """«Ventas sin registrar» sales journal of the company, created the
        first time. Its own code, so its own series (VSR/2026/00001): the
        «Ventas Preimpreso» 0001 series that follows Online is never used."""
        company = company or self.env.company
        Journal = self.sudo().with_context(active_test=False)
        journal = Journal.search([("code", "=", VSR_JOURNAL_CODE),
                                  ("company_id", "=", company.id)], limit=1)
        if journal:
            return journal
        vals = {
            "name": VSR_JOURNAL_NAME,
            "code": VSR_JOURNAL_CODE,
            "type": "sale",
            "company_id": company.id,
            # after «Ventas Preimpreso»: never the default sales journal
            "sequence": 90,
            "refund_sequence": False,
            "show_on_dashboard": False,
        }
        if "l10n_latam_use_documents" in Journal._fields:
            # no AFIP documents: an internal adjustment, like the 0001 journal
            vals["l10n_latam_use_documents"] = False
        return Journal.with_company(company).create(vals)

    @api.model
    def _distri_ensure_vsr_journal(self):
        """Called on module update, in companies with accounting set up."""
        for company in self.env["res.company"].sudo().search([]):
            if self.sudo().search_count([("company_id", "=", company.id),
                                         ("type", "=", "sale")]):
                self._distri_vsr_journal(company)


class AccountMoveVsr(models.Model):
    _inherit = "account.move"

    distri_vsr_payment_id = fields.Many2one(
        "account.payment", string="Venta sin registrar del pago", readonly=True, copy=False,
        index="btree_not_null", ondelete="restrict",
        help="Factura de «Ventas sin registrar» por el excedente de este pago.")

    # one live invoice per payment; a cancelled one allows a new one
    _distri_vsr_payment_unique = models.UniqueIndex(
        "(distri_vsr_payment_id) WHERE distri_vsr_payment_id IS NOT NULL AND state != 'cancel'",
        "Ese pago ya tiene su factura de venta sin registrar.")


class DistriBalanceReview(models.Model):
    _name = "distri.balance.review"
    _description = "Revisión de cobros por cliente"
    # highest debt first (the current lab balance), then the name
    _order = "lab_balance desc, partner_name, id"
    _rec_name = "partner_id"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, readonly=True,
                                 index=True, ondelete="cascade")
    partner_name = fields.Char(related="partner_id.name", store=True, string="Nombre")
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    online_balance = fields.Monetary(string="Saldo en Online", readonly=True,
                                     help="Todas las filas de la historia, también las "
                                          "archivadas: es el saldo de Odoo Online.")
    history_receipt_count = fields.Integer(string="Recibos en la historia", readonly=True)
    history_receipt_amount = fields.Monetary(string="Importe de los recibos", readonly=True)
    matched_count = fields.Integer(
        string="Cobros del lab ya en Online", readonly=True,
        help="Filas de la historia archivadas porque son el mismo cobro que un pago del lab.")
    posted_count = fields.Integer(string="Pagos del lab", readonly=True)
    posted_amount = fields.Monetary(string="Importe pagos del lab", readonly=True)
    pending_count = fields.Integer(string="Tarjetas pendientes", readonly=True)
    pending_amount = fields.Monetary(string="Importe pendiente con comprobante", readonly=True)
    no_receipt_count = fields.Integer(string="Pendientes sin comprobante", readonly=True)
    no_receipt_amount = fields.Monetary(
        string="Importe sin comprobante (no aplicado)", readonly=True,
        help="Movimientos de Mercado Pago sin comprobante de WhatsApp: nunca bajan la deuda.")
    whatif_balance = fields.Monetary(
        string="Si se aplicaran los sin comprobante (no aplicado)", readonly=True,
        help="Solo como referencia: el saldo si los movimientos sin comprobante fueran de este "
             "cliente. No se aplica.")
    vsr_amount = fields.Monetary(string="Ventas sin registrar", readonly=True)
    lab_balance = fields.Monetary(string="Saldo en el lab", readonly=True,
                                  help="Saldo en Online − pagos del lab + ventas sin registrar.")
    final_balance = fields.Monetary(string="Saldo con las pendientes", readonly=True,
                                    help="Lo que quedaría si se confirman las tarjetas "
                                         "pendientes con comprobante de WhatsApp.")
    flag_count = fields.Integer(string="Alertas", readonly=True)
    has_flag = fields.Boolean(string="Posible venta sin registrar", readonly=True, index=True)
    summary = fields.Char(string="Ya conciliado", readonly=True)
    state = fields.Selection([("pending", "Pendiente"), ("reviewed", "Revisado")],
                             string="Estado", default="pending", readonly=True, index=True)
    line_ids = fields.One2many("distri.balance.review.line", "review_id", string="Pagos nuevos",
                               readonly=True)
    online_sync_label = fields.Char(string="Online", compute="_compute_online_sync")
    online_sync_stale = fields.Boolean(compute="_compute_online_sync")

    def _compute_online_sync(self):
        from .migracion import online_sync_state
        sync = online_sync_state(self.env)
        for review in self:
            review.online_sync_label = sync["label"]
            review.online_sync_stale = sync["stale"]

    _partner_unique = models.Constraint("unique(partner_id)",
                                        "Ese cliente ya tiene su revisión.")

    # ------------------------------------------------------------ build

    @api.model
    def _distri_refresh(self, partners=None):
        """Rebuilds the review of the given commercial partners (all when
        None) from the history, the payments and the board."""
        Review = self.sudo()
        Partner = self.env["res.partner"].sudo()
        cr = self.env.cr
        only = set(partners.mapped("commercial_partner_id").ids) if partners is not None else None
        self.env["distri.cuenta.movimiento"].flush_model()
        Partner.flush_model(["commercial_partner_id"])

        cr.execute("""
            SELECT p.commercial_partner_id,
                   round(sum(m.debe - m.haber), 2),
                   count(*) FILTER (WHERE m.tipo = 'pago'),
                   coalesce(sum(m.haber - m.debe) FILTER (WHERE m.tipo = 'pago'), 0),
                   count(*) FILTER (WHERE NOT m.active)
              FROM distri_cuenta_movimiento m JOIN res_partner p ON p.id = m.partner_id
             GROUP BY p.commercial_partner_id""")
        history = {row[0]: row[1:] for row in cr.fetchall()}
        cr.execute("SELECT lab_payment_id FROM distri_cuenta_movimiento "
                   "WHERE NOT active AND lab_payment_id IS NOT NULL")
        in_online = {row[0] for row in cr.fetchall()}

        pay_domain = [("payment_type", "=", "inbound"), ("state", "in", POSTED_PAYMENT_STATES)]
        card_domain = [("kind", "=", "income"), ("state", "=", "draft"),
                       ("partner_id", "!=", False)]
        if only is not None:
            pay_domain.append(("partner_id.commercial_partner_id", "in", list(only)))
            card_domain.append(("partner_id.commercial_partner_id", "in", list(only)))
        payments = self.env["account.payment"].sudo().search(pay_domain).filtered(
            lambda p: p.id not in in_online)
        cards = self.env["distri.mp.collection"].sudo().search(card_domain)
        paid_cards = {c.payment_id.id: c for c in self.env["distri.mp.collection"].sudo().search(
            [("payment_id", "in", payments.ids)])}
        invoices = {m.distri_vsr_payment_id.id: m for m in self.env["account.move"].sudo().search(
            [("distri_vsr_payment_id", "in", payments.ids), ("state", "=", "posted")])}

        by_partner = {}
        for pay in payments:
            by_partner.setdefault(pay.partner_id.commercial_partner_id.id, [[], []])[0].append(pay)
        for card in cards:
            by_partner.setdefault(card.partner_id.commercial_partner_id.id, [[], []])[1].append(card)

        # every retail customer with debt (or credit) in the Online history,
        # also without new payments (19.0.1.41.0): the review covers all of it
        owing = [pid for pid, row in history.items()
                 if abs(float(row[0] or 0.0)) > AMOUNT_TOLERANCE
                 and (only is None or pid in only)]
        candidates = Partner.browse(list(set(by_partner) | set(owing))).exists()
        wanted = {p.id for p in candidates if not is_reseller(p)}
        scope = [("partner_id", "in", list(only))] if only is not None else []
        existing = {r.partner_id.id: r for r in Review.search(scope)}
        # customers with nothing new left (or now resellers) leave the review
        Review.browse([r.id for pid, r in existing.items() if pid not in wanted]).unlink()

        for pid in wanted:
            review = existing.get(pid) or Review.create({"partner_id": pid})
            hist = history.get(pid, (0.0, 0, 0.0, 0))
            posted, pending = by_partner.get(pid, ([], []))
            review._distri_rebuild(hist, posted, pending, paid_cards, invoices)
        return True

    def _distri_rebuild(self, hist, posted, pending, paid_cards, invoices):
        """Writes the lines of one customer: running balance from Online."""
        self.ensure_one()
        Line = self.env["distri.balance.review.line"].sudo()
        online, receipts, receipts_amount, matched = hist
        online = float(online or 0.0)
        old = {line._distri_key(): line for line in self.line_ids}
        running = online
        seq = 0
        rows = []
        for pay in sorted(posted, key=lambda p: (p.date, p.id)):
            card = paid_cards.get(pay.id)
            journal = pay.journal_id
            if card or journal.code == "MP":
                origin = "mp"
            elif journal.type == "cash":
                origin = "cash"
            else:
                origin = "other"
            invoice = invoices.get(pay.id)
            rows.append({
                "collection_id": card.id if card else False,
                "payment_id": pay.id,
                "origin": origin,
                "date": pay.date,
                "amount": pay.amount,
                "operation": (card.source_id or card.operation) if card else pay.name,
                "note": (card.proposal_note if card else "") or pay.memo or "",
                "vsr_invoice_id": invoice.id if invoice else False,
                "invoiced": invoice.amount_total if invoice else 0.0,
                "has_receipt": bool(card and card.receipt_id),
                "applied": True,
            })
        for card in sorted(pending, key=lambda c: (c.date_day or fields.Date.today(), c.id)):
            rows.append({
                "collection_id": card.id,
                "payment_id": False,
                "origin": "card",
                "date": card.date_day,
                "amount": card.amount,
                "operation": card.source_id or "",
                "note": card.proposal_note or "",
                "vsr_invoice_id": False,
                "invoiced": 0.0,
                "has_receipt": bool(card.receipt_id),
                # a movement without its WhatsApp receipt never reduces the debt
                "applied": bool(card.receipt_id),
            })
        keep = Line.browse()
        totals = {"posted_count": 0, "posted_amount": 0.0, "pending_count": 0,
                  "pending_amount": 0.0, "vsr_amount": 0.0, "no_receipt_count": 0,
                  "no_receipt_amount": 0.0}
        lab = online
        flags = 0
        for row in rows:
            seq += 10
            invoiced = row.pop("invoiced")
            before = running
            if not row["applied"]:
                # shown, never applied: the balance stays
                totals["pending_count"] += 1
                totals["no_receipt_count"] += 1
                totals["no_receipt_amount"] += row["amount"]
                vals = dict(row, sequence=seq, balance_before=before, balance_after=running,
                            red_flag=False, excess=0.0)
                line = old.pop(("card", row["collection_id"]), None)
                if line:
                    changed = {k: v for k, v in vals.items() if line._distri_differs(k, v)}
                    if changed:
                        line.write(changed)
                else:
                    line = Line.create(dict(vals, review_id=self.id))
                keep |= line
                continue
            running = round(running - row["amount"] + invoiced, 2)
            net = row["amount"] - invoiced
            red = running < -AMOUNT_TOLERANCE and net > AMOUNT_TOLERANCE
            excess = round(min(net, -running), 2) if red else 0.0
            flags += red
            if row["origin"] == "card":
                totals["pending_count"] += 1
                totals["pending_amount"] += row["amount"]
            else:
                totals["posted_count"] += 1
                totals["posted_amount"] += row["amount"]
                totals["vsr_amount"] += invoiced
                lab = running
            vals = dict(row, sequence=seq, balance_before=before, balance_after=running,
                        red_flag=red, excess=excess)
            key = ("card", row["collection_id"]) if row["collection_id"] else \
                ("payment", row["payment_id"])
            line = old.pop(key, None)
            if line:
                changed = {k: v for k, v in vals.items() if line._distri_differs(k, v)}
                if changed:
                    line.write(changed)
            else:
                line = Line.create(dict(vals, review_id=self.id))
            keep |= line
        # lines whose card left the board (ignored, set aside, other customer)
        Line.browse([line.id for line in old.values()]).unlink()
        states = keep.mapped("review_state")
        summary = "%d recibos en la historia por %s" % (receipts, _money(receipts_amount))
        if matched:
            summary += " · %d cobros del lab ya en Online" % matched
        self.write(dict(
            totals,
            online_balance=online,
            history_receipt_count=receipts,
            history_receipt_amount=receipts_amount,
            matched_count=matched,
            lab_balance=lab,
            final_balance=running,
            whatif_balance=round(running - totals["no_receipt_amount"], 2),
            flag_count=flags,
            has_flag=bool(flags),
            summary=summary,
            # a customer with Online debt and nothing new keeps its own state
            # (pending until «Marcar cliente revisado»)
            state=("pending" if "pending" in states else "reviewed") if states else self.state,
        ))

    def _distri_refresh_self(self):
        self._distri_refresh(self.mapped("partner_id"))

    # ------------------------------------------------------------ actions

    @api.model
    def action_open_reviews(self):
        """Menu «Revisión por cliente»: rebuilds everything, then the list."""
        _check_admin_or_raise(self.env)
        self._distri_refresh()
        return self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_balance_review")

    def action_refresh(self):
        _check_admin_or_raise(self.env)
        if self:
            self._distri_refresh_self()
        else:
            self._distri_refresh()
        return True

    def action_mark_customer_reviewed(self):
        """«Marcar cliente revisado»: the customer's balance was checked
        (also when it has no new payment)."""
        _check_admin_or_raise(self.env)
        pending = self.sudo().line_ids.filtered(lambda l: l.review_state == "pending")
        if pending:
            raise UserError("Quedan %s pagos pendientes de revisar en este cliente." % len(pending))
        self.sudo().write({"state": "reviewed"})
        return True

    def action_view_account(self):
        """«Cuenta corriente», in a new browser tab."""
        from .migracion import account_url, tab_action
        self.ensure_one()
        return tab_action(account_url(self.partner_id))


class DistriBalanceReviewLine(models.Model):
    _name = "distri.balance.review.line"
    _description = "Pago nuevo en la revisión por cliente"
    _order = "review_id, sequence, id"

    review_id = fields.Many2one("distri.balance.review", string="Revisión por cliente", required=True,
                                readonly=True, index=True, ondelete="cascade")
    partner_id = fields.Many2one(related="review_id.partner_id", store=True, string="Cliente")
    sequence = fields.Integer(readonly=True)
    currency_id = fields.Many2one(related="review_id.currency_id")
    origin = fields.Selection(LINE_ORIGINS, string="Origen", readonly=True)
    date = fields.Date(string="Fecha", readonly=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    operation = fields.Char(string="Operación", readonly=True)
    note = fields.Char(string="Nota / evidencia", readonly=True)
    balance_before = fields.Monetary(string="Saldo antes", readonly=True)
    balance_after = fields.Monetary(string="Saldo después", readonly=True)
    red_flag = fields.Boolean(string="Posible venta sin registrar", readonly=True,
                              help="Con este pago el cliente queda con saldo a favor.")
    excess = fields.Monetary(string="Excedente", readonly=True,
                             help="La parte del pago que deja el saldo por debajo de cero.")
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True,
                                 index="btree_not_null", ondelete="cascade")
    collection_id = fields.Many2one("distri.mp.collection", string="Tarjeta del tablero",
                                    readonly=True, index="btree_not_null", ondelete="cascade")
    card_state = fields.Selection(related="collection_id.state", string="Estado de la tarjeta")
    has_receipt = fields.Boolean(string="Comprobante", readonly=True,
                                 help="La tarjeta tiene su comprobante de WhatsApp.")
    applied = fields.Boolean(string="Aplicado al saldo", readonly=True, default=True,
                             help="Sin comprobante de WhatsApp, un movimiento de Mercado Pago no "
                                  "baja la deuda: se muestra y no se aplica.")
    receipt_id = fields.Many2one(related="collection_id.receipt_id", string="Foto")
    money_grade = fields.Selection(related="collection_id.money_grade")
    customer_sources = fields.Char(related="collection_id.customer_sources")
    migration_duplicate = fields.Char(related="collection_id.migration_duplicate")
    invoice_id = fields.Many2one("account.move", string="Factura", compute="_compute_invoice_id")
    vsr_invoice_id = fields.Many2one("account.move", string="Factura de venta sin registrar",
                                     readonly=True)
    review_state = fields.Selection(LINE_STATES, string="Revisión", default="pending",
                                    readonly=True, index=True)
    reviewed_by_id = fields.Many2one("res.users", string="Revisó", readonly=True)
    reviewed_at = fields.Datetime(string="Revisado el", readonly=True)

    @api.depends("vsr_invoice_id", "payment_id")
    def _compute_invoice_id(self):
        for line in self:
            invoices = line.vsr_invoice_id or line.sudo().payment_id.reconciled_invoice_ids
            line.invoice_id = invoices.sorted(lambda m: (m.invoice_date or m.date, m.id))[-1:] \
                if invoices else False

    def _distri_key(self):
        self.ensure_one()
        if self.collection_id:
            return ("card", self.collection_id.id)
        return ("payment", self.payment_id.id)

    def _distri_differs(self, fname, value):
        current = self[fname]
        if isinstance(current, models.BaseModel):
            return current.id != (value or False)
        if isinstance(current, float):
            return round(current - (value or 0.0), 2) != 0
        return current != value

    # ------------------------------------------------------------ actions

    def action_confirm_card(self):
        """«Confirmar»: the board's own confirmation, only for a card that
        passes the guards of the migration board (receipt, customer, not
        duplicated, not in Online, Online synced in the last 24 h)."""
        from .migracion import cutoff_moment, online_sync_state
        _check_admin_or_raise(self.env)
        cards = self.mapped("collection_id").filtered(lambda c: c.state == "draft")
        if not cards:
            raise UserError("No hay tarjetas pendientes para confirmar.")
        sync = online_sync_state(self.env)
        cutoff = cutoff_moment(self.env)
        problems = ["%s: %s" % (card.name, problem) for card, problem in (
            (card, card._migration_problem("single", sync, cutoff)) for card in cards) if problem]
        if problems:
            raise UserError("No se confirma: %s." % "; ".join(problems))
        reviews = self.mapped("review_id")
        cards.action_confirm()
        reviews.sudo()._distri_refresh_self()
        return True

    def action_mark_reviewed(self):
        _check_admin_or_raise(self.env)
        lines = self.sudo().filtered(lambda l: l.review_state == "pending")
        lines.write({"review_state": "reviewed", "reviewed_by_id": self.env.user.id,
                     "reviewed_at": fields.Datetime.now()})
        reviews = self.mapped("review_id").sudo()
        for review in reviews:
            review.state = "pending" if "pending" in review.line_ids.mapped(
                "review_state") else "reviewed"
        return True

    def _distri_vsr_defaults(self):
        """Wizard values from the review: the excess that leaves the
        customer in credit."""
        self.ensure_one()
        line = self.sudo()
        return {
            "source": "review",
            "review_line_id": line.id,
            "collection_id": line.collection_id.id,
            "payment_id": line.payment_id.id,
            "partner_id": line.review_id.partner_id.id,
            "payment_amount": line.amount,
            "amount": line.excess,
            "invoice_date": line.date,
        }

    def action_unrecorded_sale(self):
        """«Venta sin registrar» from the review: only on a line that leaves
        the customer in credit (the wizard checks it again)."""
        _check_admin_or_raise(self.env)
        self.ensure_one()
        line = self.sudo()
        line.review_id._distri_refresh_self()
        if not line.exists() or not line.red_flag or line.excess <= 0:
            raise UserError("Este pago no deja al cliente con saldo a favor: no hay venta sin "
                            "registrar que facturar.")
        _vsr_check_free(self.env, line.payment_id)
        return _vsr_wizard_action(self.env, line._distri_vsr_defaults())

    # each one opens in a new browser tab (19.0.1.41.0): the review stays
    def action_view_payment(self):
        from .migracion import record_url, tab_action
        self.ensure_one()
        if not self.payment_id:
            raise UserError("Esta línea no tiene pago registrado.")
        return tab_action(record_url(self.payment_id))

    def action_view_card(self):
        from .migracion import record_url, tab_action
        self.ensure_one()
        if not self.collection_id:
            raise UserError("Esta línea no tiene tarjeta del tablero.")
        return tab_action(record_url(self.collection_id))

    def action_view_invoice(self):
        from .migracion import record_url, tab_action
        self.ensure_one()
        if not self.invoice_id:
            raise UserError("Esta línea no tiene factura.")
        return tab_action(record_url(self.invoice_id))

    def action_view_receipt(self):
        """The photo itself, full size, in a new tab."""
        self.ensure_one()
        if not self.collection_id.receipt_id:
            raise UserError("Esta línea no tiene foto.")
        return self.collection_id.action_tab_receipt()

    def action_receipt_viewer(self):
        """The photo large with the WhatsApp message under it (dialog)."""
        self.ensure_one()
        if not self.collection_id.receipt_id:
            raise UserError("Esta línea no tiene foto.")
        return self.collection_id.action_receipt_viewer()


# ====================================================================== «Venta sin registrar»


def _vsr_check_free(env, payment):
    """One live invoice per payment."""
    if not payment:
        return
    existing = env["account.move"].sudo().search([("distri_vsr_payment_id", "=", payment.id),
                                                  ("state", "!=", "cancel")], limit=1)
    if existing:
        raise UserError("El pago %s ya tiene su factura de venta sin registrar: %s." % (
            payment.name, existing.name))


def _vsr_wizard_action(env, defaults):
    return {
        "type": "ir.actions.act_window",
        "name": "Venta sin registrar",
        "res_model": "distri.unrecorded.sale.wizard",
        "view_mode": "form",
        "views": [(False, "form")],
        "target": "new",
        "context": {"default_%s" % k: v for k, v in defaults.items()},
    }


class DistriMpCollectionVsr(models.Model):
    _inherit = "distri.mp.collection"

    def _distri_vsr_defaults(self):
        """Wizard values from the board: administración already knows the
        whole transfer was that sale, so the default is the full amount and
        older invoices keep their debt."""
        self.ensure_one()
        card = self.sudo()
        payment = card.payment_id
        return {
            "source": "board",
            "collection_id": card.id,
            "payment_id": payment.id,
            "partner_id": card.partner_id.commercial_partner_id.id,
            "payment_amount": card.amount,
            "amount": card.amount,
            "invoice_date": payment.date if payment else card.date_day,
        }

    def action_unrecorded_sale(self):
        _check_admin_or_raise(self.env)
        self.ensure_one()
        card = self.sudo()
        if card.kind != "income" or card.state not in ("draft", "confirmed"):
            raise UserError("Solo un ingreso de Mercado Pago pendiente o confirmado puede ser una "
                            "venta sin registrar.")
        _vsr_check_free(self.env, card.payment_id)
        return _vsr_wizard_action(self.env, card._distri_vsr_defaults())


class DistriUnrecordedSaleWizard(models.TransientModel):
    """«Venta sin registrar», shared by the board card and the review per
    customer. A pending card is confirmed first with the board's own
    confirmation, without applying the payment to older invoices: the
    payment goes to this sale."""
    _name = "distri.unrecorded.sale.wizard"
    _description = "Venta sin registrar"

    source = fields.Selection([("board", "Tablero"), ("review", "Revisión por cliente")],
                              required=True, default="board", readonly=True)
    collection_id = fields.Many2one("distri.mp.collection", string="Tarjeta", readonly=True)
    review_line_id = fields.Many2one("distri.balance.review.line", readonly=True)
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True,
                                 domain="[('distri_generic_partner', '=', False)]")
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    payment_amount = fields.Monetary(string="Importe del pago", readonly=True)
    amount = fields.Monetary(string="Importe de la venta", required=True,
                             help="Lo que se factura. Desde el tablero, todo el pago; desde la "
                                  "revisión, el excedente que deja saldo a favor.")
    invoice_date = fields.Date(string="Fecha de la factura", required=True)
    note = fields.Char(string="Nota")
    card_pending = fields.Boolean(compute="_compute_card_pending")

    @api.depends("collection_id.state")
    def _compute_card_pending(self):
        for wiz in self:
            wiz.card_pending = wiz.collection_id.state == "draft"

    def action_apply(self):
        _check_admin_or_raise(self.env)
        self.ensure_one()
        # the wizard opens with default_* keys (payment_id, amount, ...): they
        # must not reach the payment, the invoice or its lines
        self = self.with_context(
            {k: v for k, v in self.env.context.items() if not k.startswith("default_")})
        partner = self.partner_id.commercial_partner_id
        card = self.collection_id.sudo()
        payment = self.payment_id.sudo() or card.payment_id
        if self.amount <= 0:
            raise UserError("El importe de la venta tiene que ser mayor que cero.")
        if self.amount > self.payment_amount + 0.005:
            raise UserError("El importe de la venta (%s) no puede superar el del pago (%s)." % (
                _money(self.amount), _money(self.payment_amount)))
        if not partner or partner.distri_generic_partner:
            raise UserError("Elegí el cliente.")
        _vsr_check_free(self.env, payment)
        if card and card.state == "draft":
            if card.partner_id.commercial_partner_id != partner:
                card.partner_id = partner
            card.with_context(distri_skip_auto_apply=True).action_confirm()
            payment = card.payment_id
        if not payment or payment.state not in POSTED_PAYMENT_STATES:
            raise UserError("No hay un pago registrado para facturar la venta.")
        if payment.partner_id.commercial_partner_id != partner:
            raise UserError("El pago %s es de %s: la venta se factura al mismo cliente. Si el "
                            "cliente está mal, deshacé la confirmación." % (
                                payment.name, payment.partner_id.commercial_partner_id.name))
        # one invoice per payment, even with two clicks at once
        self.env.cr.execute("SELECT id FROM account_payment WHERE id = %s FOR UPDATE",
                            [payment.id])
        _vsr_check_free(self.env, payment)
        if self.source == "review":
            line = self.review_line_id.sudo()
            line.review_id._distri_refresh_self()
            if not line.exists() or not line.red_flag:
                raise UserError("El pago %s ya no deja al cliente con saldo a favor." %
                                payment.name)
        invoice = self._distri_invoice(payment, card, partner)
        self.env["distri.balance.review.line"].sudo().search([
            "|", ("payment_id", "=", payment.id),
            ("collection_id", "=", card.id or 0)]).write({
                "review_state": "unrecorded_sale", "reviewed_by_id": self.env.user.id,
                "reviewed_at": fields.Datetime.now()})
        self.env["distri.balance.review"]._distri_refresh(partner)
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.move",
            "res_id": invoice.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }

    def _distri_invoice(self, payment, card, partner):
        """Creates, posts and reconciles the VSR invoice for self.amount."""
        amount = round(self.amount, 2)
        pay_line = payment.move_id.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
        if not pay_line or abs(sum(pay_line.mapped("amount_residual"))) < amount - 0.005:
            raise UserError("El pago %s ya está aplicado a otras facturas: no le queda %s para la "
                            "venta sin registrar." % (payment.name, _money(amount)))
        company = payment.company_id
        journal = self.env["account.journal"]._distri_vsr_journal(company)
        product = self.env.ref(VSR_PRODUCT_XMLID)
        operation = (card.source_id if card else "") or payment.name
        invoice = self.env["account.move"].sudo().with_company(company).create({
            "move_type": "out_invoice",
            "partner_id": partner.id,
            "journal_id": journal.id,
            "invoice_date": self.invoice_date,
            "date": self.invoice_date,
            "invoice_origin": payment.name,
            "ref": "Venta sin registrar · %s" % payment.name,
            "narration": self.note or False,
            "distri_vsr_payment_id": payment.id,
            "invoice_line_ids": [Command.create({
                "product_id": product.id,
                "name": "Venta sin detalle (cobro %s)" % operation,
                "quantity": 1,
                "price_unit": amount,
                "tax_ids": [Command.clear()],
            })],
        })
        invoice.action_post()
        inv_line = invoice.line_ids.filtered(
            lambda l: l.account_id.account_type == "asset_receivable")
        (inv_line + pay_line).reconcile()

        if self.source == "board":
            why = "administración indicó desde el tablero que el cobro fue una venta hecha " \
                  "fuera del sistema"
        else:
            why = "el pago dejaba a %s con saldo a favor (posible venta hecha fuera del " \
                  "sistema)" % partner.name
        if self.note:
            why += ". Nota: %s" % self.note
        stamp = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        head = Markup("<b>Venta sin registrar</b> por %s el %s: ") % (
            self.env.user.name, stamp.strftime("%d/%m/%Y %H:%M"))
        detail = "factura %s por %s, pago %s del %s por %s, porque %s." % (
            invoice.name, _money(amount), payment.name, payment.date.strftime("%d/%m/%Y"),
            _money(payment.amount), why)
        invoice.message_post(body=head + detail)
        payment.message_post(body=head + detail)
        if card:
            card.message_post(body=head + detail)
        return invoice
