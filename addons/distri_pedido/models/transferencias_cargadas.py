"""«Transferencias cargadas»: every transfer loaded, newest first (19.0.1.129.7).

Owner's request: «Auditoría de cobros» lists only the anomalies and
«Resumen diario por persona» only totals; this is the chronological control
list of every transfer receipt the staff credited or loaded, for
Administración, Gerencia and Sistemas (`AUDIT_GROUPS`).

Inclusion rule (one row per `distri.mp.collection` card):

- every card credited by a person (`credited_at` set): kind «verify», the
  only kind `_distri_credit` credits, whatever path loaded it (the board,
  «Subir y acreditar», «Cobrar» / /cobro, the driver app, a customer's
  receipt approved by administración). It stays when the payment is
  annulled in the audit (`audit_state` «anulado»); a credit undone with
  «Deshacer» clears `credited_at` and leaves the list (the card is not
  credited any more);
- every receipt an employee uploaded and nobody credited yet: kind «verify»
  or «receipt» (the «Esperando reporte MP» card a report moved to «Foto sin
  transferencia»), state «draft» or «authorized», whose WhatsApp message
  was uploaded by the staff (`distri_forwarded_by_id`: /comprobantes/subir,
  «Cobrar», a seller's number) or taken in the driver app
  (`distri_from_app`, `distri_app_stop_id`). A cancelled upload is
  «ignored» and leaves.

Left out: the incomes of the Mercado Pago report (kind «income»), with or
without a receipt: the report loaded them, not a person, and the income a
credited receipt tied to shows in «Cruzado con el reporte»; a receipt the
customer sent that nobody credited (it waits in «Para acreditar», it was not
loaded by the staff); the cards confirmed by hand before 19.0.1.126.0
(no `credited_at`).

Who loaded it: who credited it, else who uploaded it, else the driver of
the route of the app photo. When: the crediting, else the upload.

Speed: a read-only SQL model (`_table_query`) over the card table and its
indexed links; the anomaly of each row is one LATERAL index lookup. Every
column of the list is a plain column: no compute per row (the thumbnail
reads the receipts' attachments in one batch).
"""
from odoo import api, fields, models
from odoo.tools import SQL

from .acreditar import AUDIT_STATES
from .auditoria import _check_audit
from .reparto import _money

LOAD_STATES = [("credited", "Acreditada"), ("pending", "Subida sin acreditar")]
DESTINATION_KINDS = [("lab", "Factura del sistema"), ("online", "Factura de Online"),
                     ("account", "A cuenta")]
# the audit states that count as an anomaly in «Resumen diario por persona»
ANOMALY_AUDIT_STATES = ("diferencia", "sin_transferencia", "revertido", "anulado")
LOCAL_TZ = "America/Argentina/Buenos_Aires"


class DistriLoadedTransfer(models.Model):
    _name = "distri.loaded.transfer"
    _description = "Transferencia cargada"
    _auto = False
    _order = "loaded_at desc, id desc"
    _rec_name = "card_id"
    # pending writes on these are flushed before reading the list
    _depends = {
        "distri.mp.collection": [
            "kind", "state", "company_id", "partner_id", "amount", "operation", "payment_id",
            "receipt_id", "income_id", "credited_at", "credited_by_id", "audit_state",
            "pay_move_id", "pay_invoice_id", "date", "report_id"],
        "distri.whatsapp.message": [
            "distri_forwarded_by_id", "distri_from_app", "distri_app_stop_id",
            "distri_operation", "attachment_id"],
        "distri.mp.anomaly": ["card_id", "payment_id", "kind", "state"],
        "distri.route.stop": ["route_id"],
        "distri.route": ["driver_id"],
        "res.partner": ["commercial_partner_id"],
        "account.move": ["name"],
        "distri.cuenta.movimiento": ["comprobante"],
    }

    card_id = fields.Many2one("distri.mp.collection", string="Comprobante", readonly=True)
    company_id = fields.Many2one("res.company", string="Empresa", readonly=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    loaded_at = fields.Datetime(string="Cargada o acreditada el", readonly=True)
    loaded_day = fields.Date(string="Día", readonly=True)
    loaded_by_id = fields.Many2one("res.users", string="Cargó o acreditó", readonly=True)
    uploaded_by_id = fields.Many2one("res.users", string="Subió el comprobante", readonly=True)
    load_state = fields.Selection(LOAD_STATES, string="Carga", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    operation = fields.Char(string="Número de operación", readonly=True)
    destination_kind = fields.Selection(DESTINATION_KINDS, string="Destino", readonly=True)
    destination = fields.Char(string="Paga", readonly=True)
    pay_move_id = fields.Many2one("account.move", string="Factura del sistema", readonly=True)
    pay_invoice_id = fields.Many2one("distri.cuenta.movimiento", string="Factura de Online",
                                     readonly=True)
    payment_id = fields.Many2one("account.payment", string="Pago", readonly=True)
    audit_state = fields.Selection(AUDIT_STATES, string="Control con MP", readonly=True)
    without_receipt = fields.Boolean(string="Sin comprobante", readonly=True)
    receipt_flag = fields.Char(string="Marca de comprobante", readonly=True)
    income_id = fields.Many2one("distri.mp.collection", string="Cruzado con el reporte",
                                readonly=True)
    income_date = fields.Datetime(string="Fecha en el reporte", readonly=True)
    matched = fields.Boolean(string="Cruzada con el reporte", readonly=True)
    confirmed = fields.Boolean(string="Pago confirmado", readonly=True)
    confirmed_mark = fields.Char(string="Pago confirmado ✓", readonly=True)
    anomaly_id = fields.Many2one("distri.mp.anomaly", string="Anomalía", readonly=True)
    with_anomaly = fields.Boolean(string="Con anomalía", readonly=True)
    receipt_id = fields.Many2one("distri.whatsapp.message", string="Mensaje del comprobante",
                                 readonly=True)
    receipt_image = fields.Binary(related="receipt_id.media_data", string="Foto")
    receipt_name = fields.Char(related="receipt_id.media_name", string="Archivo")
    name = fields.Char(string="Transferencia", compute="_compute_name")

    @property
    def _table_query(self):
        return SQL("""
            SELECT c.id AS id,
                   c.id AS card_id,
                   c.company_id AS company_id,
                   COALESCE(c.credited_at, m.create_date, c.create_date) AS loaded_at,
                   (COALESCE(c.credited_at, m.create_date, c.create_date)
                        AT TIME ZONE 'UTC' AT TIME ZONE %(tz)s)::date AS loaded_day,
                   COALESCE(c.credited_by_id, m.distri_forwarded_by_id, r.driver_id)
                       AS loaded_by_id,
                   COALESCE(m.distri_forwarded_by_id, r.driver_id) AS uploaded_by_id,
                   CASE WHEN c.credited_at IS NOT NULL THEN 'credited' ELSE 'pending' END
                       AS load_state,
                   COALESCE(p.commercial_partner_id, c.partner_id) AS partner_id,
                   c.amount AS amount,
                   COALESCE(NULLIF(c.operation, ''), NULLIF(m.distri_operation, ''))
                       AS operation,
                   CASE WHEN c.pay_move_id IS NOT NULL THEN 'lab'
                        WHEN c.pay_invoice_id IS NOT NULL THEN 'online'
                        ELSE 'account' END AS destination_kind,
                   CASE WHEN c.pay_move_id IS NOT NULL
                            THEN 'Factura ' || COALESCE(am.name, '')
                        WHEN c.pay_invoice_id IS NOT NULL
                            THEN 'Factura de Online ' || COALESCE(cm.comprobante, '')
                        ELSE 'A cuenta' END AS destination,
                   c.pay_move_id AS pay_move_id,
                   c.pay_invoice_id AS pay_invoice_id,
                   c.payment_id AS payment_id,
                   c.audit_state AS audit_state,
                   flags.without_receipt AS without_receipt,
                   CASE WHEN flags.without_receipt THEN 'Sin comprobante' END AS receipt_flag,
                   c.income_id AS income_id,
                   i.date AS income_date,
                   c.income_id IS NOT NULL AS matched,
                   COALESCE(c.audit_state = 'verificado', FALSE) AS confirmed,
                   CASE WHEN c.audit_state = 'verificado' THEN '✓' END AS confirmed_mark,
                   an.id AS anomaly_id,
                   ((an.id IS NOT NULL AND an.kind <> 'sin_comprobante')
                    OR COALESCE(c.audit_state IN %(bad)s, FALSE)) AS with_anomaly,
                   c.receipt_id AS receipt_id
              FROM distri_mp_collection c
              LEFT JOIN distri_whatsapp_message m ON m.id = c.receipt_id
              LEFT JOIN distri_route_stop s ON s.id = m.distri_app_stop_id
              LEFT JOIN distri_route r ON r.id = s.route_id
              LEFT JOIN res_partner p ON p.id = c.partner_id
              LEFT JOIN account_move am ON am.id = c.pay_move_id
              LEFT JOIN distri_cuenta_movimiento cm ON cm.id = c.pay_invoice_id
              LEFT JOIN distri_mp_collection i ON i.id = c.income_id
              LEFT JOIN LATERAL (
                    SELECT a.id, a.kind
                      FROM distri_mp_anomaly a
                     WHERE (a.card_id = c.id
                            OR (c.payment_id IS NOT NULL AND a.payment_id = c.payment_id))
                       AND a.state IN ('open', 'annulled')
                     ORDER BY a.kind = 'sin_comprobante', a.state <> 'open', a.id DESC
                     LIMIT 1) an ON TRUE
              CROSS JOIN LATERAL (
                    SELECT (m.id IS NULL OR m.attachment_id IS NULL
                            OR (an.kind = 'sin_comprobante')) AS without_receipt) flags
             WHERE c.kind IN ('verify', 'receipt')
               AND (c.credited_at IS NOT NULL
                    OR (c.state IN ('draft', 'authorized')
                        AND (m.distri_forwarded_by_id IS NOT NULL
                             OR COALESCE(m.distri_from_app, FALSE)
                             OR m.distri_app_stop_id IS NOT NULL)))
        """, tz=LOCAL_TZ, bad=ANOMALY_AUDIT_STATES)

    def _compute_name(self):
        for rec in self:
            rec.name = "%s · %s" % (_money(rec.amount or 0.0),
                                    rec.partner_id.name or "sin cliente")

    # ------------------------------------------------------------ links

    def action_view_card(self):
        self.ensure_one()
        _check_audit(self.env)
        return {"type": "ir.actions.act_window", "res_model": "distri.mp.collection",
                "name": "Comprobante", "view_mode": "form", "res_id": self.card_id.id,
                "views": [(self.env.ref("distri_pedido.view_mp_collection_form").id, "form")]}

    def action_view_payment(self):
        self.ensure_one()
        _check_audit(self.env)
        return {"type": "ir.actions.act_window", "res_model": "account.payment",
                "name": "Pago", "view_mode": "form", "res_id": self.payment_id.id}

    def action_view_anomalies(self):
        """The anomalies of the card or of its payment, open ones first."""
        self.ensure_one()
        _check_audit(self.env)
        domain = [("card_id", "=", self.card_id.id)]
        if self.payment_id:
            domain = ["|", ("payment_id", "=", self.payment_id.id)] + domain
        anomalies = self.env["distri.mp.anomaly"].search(domain)
        if len(anomalies) == 1:
            return {"type": "ir.actions.act_window", "res_model": "distri.mp.anomaly",
                    "name": "Anomalía", "view_mode": "form", "res_id": anomalies.id}
        return {"type": "ir.actions.act_window", "res_model": "distri.mp.anomaly",
                "name": "Anomalías", "view_mode": "list,form",
                "domain": [("id", "in", anomalies.ids)]}

    def action_view_statement(self):
        self.ensure_one()
        _check_audit(self.env)
        return self.partner_id.action_distri_estado_cuenta()

    @api.model
    def _action_for(self, user, day, company=None):
        """The list filtered by who loaded it and the day (the entry point
        of «Resumen diario por persona»)."""
        action = self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_loaded_transfer")
        company = company or self.env.company
        action.update({
            "name": "Transferencias de %s el %s" % (user.name or "sin usuario",
                                                     day.strftime("%d/%m/%Y")),
            "domain": [("loaded_by_id", "=", user.id or False), ("loaded_day", "=", day),
                       ("load_state", "=", "credited"), ("company_id", "=", company.id)],
            "context": {},
        })
        return action


class DistriCreditSummaryLineTransfers(models.Model):
    _inherit = "distri.credit.summary.line"

    def action_view_transfers(self):
        """«Ver transferencias»: what this person credited that day."""
        self.ensure_one()
        _check_audit(self.env)
        return self.env["distri.loaded.transfer"]._action_for(
            self.user_id, self.date, self.summary_id.company_id)
