"""Mayoristas apartados (19.0.1.31.0).

Management decided the system starts with retail customers only. The money
of mayoristas, revendedores and distribuidoras enters the same Mercado Pago
account (historically only 54 % of the incomes by count, 30 % by amount, were
retail), and a revendedor's own customers pay into his account from many
different CUITs. Without triage the collections board fills with payments
nobody here has to act on.

- «Es de mayorista» (administración, gerencia) moves a pending income that
  has no customer to the state «Mayorista / fuera del sistema»: no customer,
  no payment, out of the alerts, of the daily close and of the «Sin
  identificar» totals. Always reversible («Volver al tablero»), with who and
  when.
- «Recordar este CUIT» keeps a payer rule (distri.mp.wholesale.rule): the
  next payments of that CUIT are set aside on import. A rule can name the
  wholesale account to group its payers. A CUIT that belongs to a customer
  of the system is never set aside automatically: the customer wins and the
  rule is flagged «En conflicto» for review. A bank transfer without CUIT is
  only set aside by hand.
- The board shows by default only what has to be done for retail; the
  unknown payers are grouped by payer (distri.mp.payer.bucket, grouped in
  SQL) and the set-aside ones are behind a counter.
"""
import re
from datetime import timedelta

from odoo import api, fields, models, tools
from odoo.exceptions import UserError

from .cobros import ADMIN_GROUP, _check_admin, digits
from .reparto import _money

APARTADO = "apartado"
APARTADO_LABEL = "Mayorista / fuera del sistema"
# what the board shows by default: the last N days (the counters say how many
# older ones are still pending, so nothing hides)
BOARD_WINDOW_DAYS = 60


def normalize_cuit(value):
    number = digits(value or "")
    return number if len(number) == 11 else ""


class DistriMpWholesaleRule(models.Model):
    _name = "distri.mp.wholesale.rule"
    _description = "Pagador mayorista (fuera del sistema)"
    _inherit = ["mail.thread"]
    _order = "label, cuit"
    _rec_name = "cuit"

    cuit = fields.Char(string="CUIT", required=True, index=True, tracking=True)
    label = fields.Char(
        string="Cuenta mayorista", tracking=True,
        help="Texto libre para agrupar: el revendedor o la distribuidora a la que pertenece este "
             "pagador (por ejemplo, el nombre del revendedor).")
    note = fields.Text(string="Nota")
    active = fields.Boolean(default=True, tracking=True)
    conflict = fields.Boolean(
        string="En conflicto", compute="_compute_conflict", store=True,
        help="El CUIT es de un cliente del sistema: la regla no se aplica y alguien tiene que "
             "revisarla.")
    conflict_note = fields.Char(string="Cliente con ese CUIT", compute="_compute_conflict",
                                store=True)
    payment_count = fields.Integer(string="Pagos apartados", compute="_compute_payments")
    payment_total = fields.Monetary(string="Importe apartado", compute="_compute_payments")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)

    _cuit_unique = models.Constraint("unique(cuit)", "Ese CUIT ya tiene su regla.")

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            cuit = normalize_cuit(vals.get("cuit"))
            if not cuit:
                raise UserError("El CUIT tiene que tener 11 números.")
            vals["cuit"] = cuit
        return super().create(vals_list)

    def write(self, vals):
        if "cuit" in vals:
            cuit = normalize_cuit(vals["cuit"])
            if not cuit:
                raise UserError("El CUIT tiene que tener 11 números.")
            vals = dict(vals, cuit=cuit)
        return super().write(vals)

    @api.depends("cuit")
    def _compute_conflict(self):
        Partner = self.env["res.partner"].sudo()
        for rule in self:
            partners = Partner._distri_partners_by_cuit(rule.cuit) if rule.cuit else Partner
            rule.conflict = bool(partners)
            rule.conflict_note = ", ".join(partners.mapped("display_name"))[:250] or False

    def _compute_payments(self):
        Collection = self.env["distri.mp.collection"].sudo()
        for rule in self:
            groups = Collection._read_group(
                [("wholesale_rule_id", "=", rule.id), ("state", "=", APARTADO)],
                [], ["__count", "amount:sum"]) if rule.id else [(0, 0.0)]
            rule.payment_count, rule.payment_total = groups[0][0], groups[0][1] or 0.0

    def action_refresh_conflict(self):
        """«Revisar»: the customer of the CUIT may have changed."""
        self._compute_conflict()
        return True

    def _distri_apply(self):
        """Sets aside the pending payments of these CUITs that nothing ties
        to retail (no customer proposed, no receipt, no pending receipt)."""
        Collection = self.env["distri.mp.collection"].sudo()
        moved = Collection.browse()
        for rule in self.filtered(lambda r: r.active and not r.conflict):
            moved |= Collection.search([
                ("kind", "=", "income"), ("state", "=", "draft"), ("payer_cuit", "=", rule.cuit),
            ])._distri_set_aside(rule=rule, auto=True)
        return moved

    def action_undo(self):
        """«Deshacer la regla»: archived, and its payments still set aside
        (no payment exists for them) go back to the board."""
        _check_admin(self.env)
        Collection = self.env["distri.mp.collection"].sudo()
        back = Collection.browse()
        for rule in self:
            back |= Collection.search([("wholesale_rule_id", "=", rule.id),
                                       ("state", "=", APARTADO)])
            rule.message_post(body="Regla deshecha por %s." % self.env.user.name)
        back._distri_bring_back("Regla del CUIT deshecha")
        self.write({"active": False})
        return True


class DistriMpCollectionWholesale(models.Model):
    _inherit = "distri.mp.collection"

    state = fields.Selection(selection_add=[(APARTADO, APARTADO_LABEL)],
                             ondelete={APARTADO: "set default"})
    wholesale_rule_id = fields.Many2one("distri.mp.wholesale.rule", string="Regla del CUIT",
                                        readonly=True, ondelete="set null", index=True)
    wholesale_label = fields.Char(string="Cuenta mayorista", readonly=True)
    wholesale_auto = fields.Boolean(string="Apartado por la regla del CUIT", readonly=True)
    wholesale_by_id = fields.Many2one("res.users", string="Apartó", readonly=True)
    wholesale_at = fields.Datetime(string="Apartado el", readonly=True)

    # «Asignar cliente» only shows up when something is in it: by default the
    # unknown payers live grouped in «Sin identificar»
    column = fields.Selection(group_expand="_distri_expand_columns")

    # ------------------------------------------------------------ board

    @api.model
    def _distri_expand_columns(self, values, domain):
        keys = [key for key, _label in self._fields["column"].selection]
        return [key for key in keys if key != "unidentified" or key in values]

    def _distri_set_aside(self, rule=None, auto=False, label=False):
        """«Mayorista / fuera del sistema». Only pending incomes without a
        customer and without a receipt (a receipt or a customer is retail)."""
        todo = self.sudo().filtered(lambda c: c.kind == "income" and c.state == "draft")
        if auto:
            todo = todo.filtered(lambda c: not c.partner_id and not c.receipt_id
                                 and not c.promise_id)
        if not todo:
            return todo
        todo.write({
            "state": APARTADO,
            "wholesale_rule_id": rule.id if rule else False,
            "wholesale_label": (rule.label if rule else False) or label or False,
            "wholesale_auto": auto,
            "wholesale_by_id": False if auto else self.env.user.id,
            "wholesale_at": fields.Datetime.now(),
            "partner_id": False,
        })
        for card in todo:
            card._close_alerts("Apartado: %s" % APARTADO_LABEL)
            card.message_post(body="%s %s%s." % (
                "Apartado solo por la regla del CUIT" if auto else
                "Apartado por %s" % self.env.user.name, APARTADO_LABEL,
                " · %s" % card.wholesale_label if card.wholesale_label else ""))
        return todo

    def _distri_bring_back(self, why):
        """Back to the board, proposed again."""
        back = self.sudo().filtered(lambda c: c.state == APARTADO)
        if not back:
            return back
        back.write({"state": "draft", "wholesale_rule_id": False, "wholesale_label": False,
                    "wholesale_auto": False, "wholesale_by_id": False, "wholesale_at": False})
        for card in back:
            card.message_post(body="Vuelve al tablero (%s), por %s." % (why, self.env.user.name))
        back._propose()
        return back

    def action_mark_wholesale(self):
        """«Es de mayorista»: the wizard (one or several payments)."""
        _check_admin(self.env)
        todo = self.filtered(lambda c: c.kind == "income" and c.state == "draft")
        if not todo:
            raise UserError("Solo se apartan ingresos pendientes del reporte de Mercado Pago.")
        return {
            "type": "ir.actions.act_window",
            "name": "Es de mayorista",
            "res_model": "distri.mp.wholesale.mark",
            "view_mode": "form",
            "target": "new",
            "context": {"default_collection_ids": [(6, 0, todo.ids)]},
        }

    def action_unmark_wholesale(self):
        """«Volver al tablero»."""
        _check_admin(self.env)
        back = self._distri_bring_back("marcado a mano")
        if not back:
            raise UserError("Ninguno de los elegidos está apartado.")
        return True

    def _distri_apply_wholesale_rules(self):
        """On import: the payers remembered as mayoristas."""
        cuits = {c.payer_cuit for c in self if c.payer_cuit}
        if not cuits:
            return self.browse()
        rules = self.env["distri.mp.wholesale.rule"].sudo().search(
            [("cuit", "in", list(cuits)), ("active", "=", True)])
        rules._compute_conflict()
        moved = self.browse()
        for rule in rules.filtered(lambda r: not r.conflict):
            moved |= self.filtered(lambda c: c.payer_cuit == rule.cuit)._distri_set_aside(
                rule=rule, auto=True)
        return moved

    # ------------------------------------------------------------ counters

    @api.model
    def distri_board_counters(self):
        """The bar on top of the board (static/src/js/cobros_board.js),
        counted in SQL (read_group): what to act on, the unknown payers and
        the set-aside ones, in the default window."""
        _check_admin(self.env)
        since = fields.Date.context_today(self) - timedelta(days=BOARD_WINDOW_DAYS)
        pending = ["|", "|", ("state", "=", "draft"), ("report_review", "=", True),
                   "&", ("state", "=", "authorized"), ("income_id", "=", False)]

        def count(domain):
            [(n, total)] = self._read_group(domain, [], ["__count", "amount:sum"])
            return {"count": n, "total": total or 0.0, "total_label": _money(total or 0.0)}

        act = count(pending + [("column", "!=", "unidentified"), ("date_day", ">=", since)])
        older = count(pending + [("column", "!=", "unidentified"), ("date_day", "<", since)])
        unknown = count([("state", "=", "draft"), ("column", "=", "unidentified")])
        unknown["payers"] = self.env["distri.mp.payer.bucket"].search_count([])
        apart = count([("state", "=", APARTADO)])
        return {"window_days": BOARD_WINDOW_DAYS, "act": act, "older": older,
                "unknown": unknown, "apartados": apart}

    @api.model
    def action_open_unknown_payers(self):
        _check_admin(self.env)
        return self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_mp_payer_bucket")

    @api.model
    def action_open_apartados(self):
        _check_admin(self.env)
        return self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_mp_apartados")


class DistriMpPayerBucket(models.Model):
    """«Sin identificar»: one row per payer (CUIT, else the holder's name),
    grouped in SQL, never one card per payment."""
    _name = "distri.mp.payer.bucket"
    _description = "Pagador sin identificar"
    _auto = False
    _order = "total desc"
    _rec_name = "payer"
    _depends = {"distri.mp.collection": ["state", "column", "kind", "payer_cuit", "payer_name",
                                         "amount", "date", "company_id"],
                "distri.arca.name": ["cuit", "name"]}

    payer_key = fields.Char(string="Clave", readonly=True)
    payer = fields.Char(string="Pagador", readonly=True)
    payer_cuit = fields.Char(string="CUIT", readonly=True)
    payer_name = fields.Char(string="Titular", readonly=True)
    arca_name = fields.Char(string="Nombre en ARCA", readonly=True,
                            help="Nombre legal del padrón público de ARCA para ese CUIT "
                                 "(models/arca.py). Vacío si no figura.")
    payment_count = fields.Integer(string="Pagos", readonly=True)
    total = fields.Monetary(string="Total", readonly=True)
    first_date = fields.Datetime(string="Primer pago", readonly=True)
    last_date = fields.Datetime(string="Último pago", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    rule_conflict = fields.Char(string="Cliente con ese CUIT", compute="_compute_rule_conflict")

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                SELECT MIN(c.id) AS id,
                       k.payer_key AS payer_key,
                       COALESCE(MAX(c.payer_cuit), '') AS payer_cuit,
                       MAX(c.payer_name) AS payer_name,
                       MAX(a.name) AS arca_name,
                       COALESCE(MAX(NULLIF(c.payer_name, '')), MAX(c.payer_cuit),
                                'Sin datos del pagador') AS payer,
                       COUNT(*) AS payment_count,
                       SUM(c.amount) AS total,
                       MIN(c.date) AS first_date,
                       MAX(c.date) AS last_date,
                       MAX(c.company_id) AS company_id
                  FROM distri_mp_collection c
             LEFT JOIN distri_arca_name a ON a.cuit = c.payer_cuit,
                       LATERAL (SELECT CASE
                           WHEN COALESCE(c.payer_cuit, '') <> '' THEN 'cuit:' || c.payer_cuit
                           WHEN COALESCE(c.payer_name, '') <> '' THEN 'name:' || lower(c.payer_name)
                           ELSE 'none' END AS payer_key) k
                 WHERE c.kind = 'income' AND c.state = 'draft' AND c.column = 'unidentified'
              GROUP BY k.payer_key
            )""" % self._table)

    def _compute_rule_conflict(self):
        Partner = self.env["res.partner"].sudo()
        for row in self:
            partners = Partner._distri_partners_by_cuit(row.payer_cuit) if row.payer_cuit \
                else Partner
            row.rule_conflict = ", ".join(partners.mapped("display_name"))[:200] or False

    def _payments(self):
        Collection = self.env["distri.mp.collection"]
        domain = [("kind", "=", "income"), ("state", "=", "draft"),
                  ("column", "=", "unidentified")]
        result = Collection.browse()
        for row in self:
            if row.payer_key.startswith("cuit:"):
                extra = [("payer_cuit", "=", row.payer_cuit)]
            elif row.payer_key.startswith("name:"):
                extra = [("payer_cuit", "in", (False, "")),
                         ("payer_name", "=ilike", row.payer_key[5:])]
            else:
                extra = [("payer_cuit", "in", (False, "")), ("payer_name", "in", (False, ""))]
            result |= Collection.search(domain + extra)
        return result

    def action_view_payments(self):
        _check_admin(self.env)
        return {
            "type": "ir.actions.act_window",
            "name": "Pagos de %s" % (self[:1].payer or ""),
            "res_model": "distri.mp.collection",
            "view_mode": "list,form",
            "domain": [("id", "in", self._payments().ids)],
        }

    def action_mark_wholesale(self):
        return self._payments().action_mark_wholesale()


class DistriMpWholesaleMark(models.TransientModel):
    _name = "distri.mp.wholesale.mark"
    _description = "Apartar pagos de mayoristas"

    collection_ids = fields.Many2many("distri.mp.collection", string="Pagos", readonly=True)
    count = fields.Integer(string="Pagos", compute="_compute_summary")
    total = fields.Char(string="Total", compute="_compute_summary")
    cuits = fields.Char(string="CUIT", compute="_compute_summary")
    without_cuit = fields.Integer(string="Sin CUIT", compute="_compute_summary")
    remember = fields.Boolean(
        string="Recordar este CUIT",
        help="Los próximos pagos de este CUIT se apartan solos al importar el reporte. Un CUIT "
             "de un cliente del sistema nunca se aparta solo: la regla queda «En conflicto».")
    label = fields.Char(string="Cuenta mayorista",
                        help="Opcional: el revendedor o la distribuidora, para agruparlos.")
    note = fields.Char(string="Nota")
    conflict_note = fields.Char(string="Atención", compute="_compute_summary")

    @api.depends("collection_ids")
    def _compute_summary(self):
        Partner = self.env["res.partner"].sudo()
        for wizard in self:
            cards = wizard.collection_ids
            cuits = sorted({c.payer_cuit for c in cards if c.payer_cuit})
            wizard.count = len(cards)
            wizard.total = _money(sum(cards.mapped("amount")))
            wizard.cuits = ", ".join(cuits)
            wizard.without_cuit = len(cards.filtered(lambda c: not c.payer_cuit))
            clash = [c for c in cuits if Partner._distri_partners_by_cuit(c)]
            wizard.conflict_note = (
                "El CUIT %s es de un cliente del sistema: se puede apartar a mano, pero la regla "
                "no se va a aplicar sola y queda para revisar." % ", ".join(clash)) if clash \
                else False

    def action_apply(self):
        _check_admin(self.env)
        self.ensure_one()
        Rule = self.env["distri.mp.wholesale.rule"].sudo()
        cards = self.collection_ids.filtered(lambda c: c.kind == "income" and c.state == "draft")
        if not cards:
            raise UserError("Ya no hay pagos pendientes para apartar.")
        rules = Rule.browse()
        if self.remember:
            for cuit in {c.payer_cuit for c in cards if normalize_cuit(c.payer_cuit)}:
                rule = Rule.with_context(active_test=False).search([("cuit", "=", cuit)], limit=1)
                if rule:
                    rule.write({"active": True, "label": self.label or rule.label})
                else:
                    rule = Rule.create({"cuit": cuit, "label": self.label or False,
                                        "note": self.note or False})
                rules |= rule
        for card in cards:
            rule = rules.filtered(lambda r: r.cuit == card.payer_cuit)[:1]
            card._distri_set_aside(rule=rule or None, label=self.label)
            if self.note:
                card.message_post(body="Nota: %s" % self.note)
        # the other pending payments of the remembered CUITs follow
        rules._distri_apply()
        return {"type": "ir.actions.act_window_close"}


class DistriMpDayWholesale(models.Model):
    _inherit = "distri.mp.day"

    apartado_total = fields.Monetary(
        string="Mayoristas apartados", compute="_compute_totals",
        help="Transferencias del reporte apartadas como de mayoristas (fuera del sistema): no "
             "se registran ni cuentan como diferencia.")
    apartado_count = fields.Integer(string="Apartados", compute="_compute_totals")

    @api.depends("date")
    def _compute_totals(self):
        super()._compute_totals()
        Collection = self.env["distri.mp.collection"].sudo()
        for day in self:
            [(n, total)] = Collection._read_group(
                [("kind", "=", "income"), ("date_day", "=", day.date),
                 ("company_id", "=", day.company_id.id), ("state", "=", APARTADO)],
                [], ["__count", "amount:sum"])
            day.apartado_count = n
            day.apartado_total = total or 0.0
            # out of the system: neither money to register nor a difference
            day.difference -= day.apartado_total
            day.transfer_count -= n
