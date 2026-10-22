"""«Deudas de empleados» (19.0.1.30.0): the charges to the driver read as
what each employee owes the company (decision of management, 22/10/2026).

A management report, read only, over the existing charges
(distri.route.case of kind «receipt», «unpaid» and «short»): no accounting
entry is made. A charge is «Adeudado» while it is open and nobody marked it
«Repuesto por el repartidor»; a charge administración closed, or one the
driver replenished, is «Saldado» with the reason. Only administración and
gerencia see it (security/ir.model.access.csv and security/reglas.xml).
"""
from odoo import api, fields, models, tools

from .comprobante_pendiente import document_of
from .reparto import _money

ORIGINS = [
    ("receipt", "Transferencia sin comprobante"),
    ("unpaid", "Pedido sin cobrar"),
    ("short", "Cobro incompleto"),
]
DEBT_STATES = [("owed", "Adeudado"), ("settled", "Saldado")]


class DistriEmployeeDebt(models.Model):
    _name = "distri.employee.debt"
    _description = "Deuda de un empleado con la empresa"
    _auto = False
    _order = "employee_id, delivered_at, id"
    _rec_name = "case_id"
    # the view reads these: Odoo flushes them before any query on it
    _depends = {
        "distri.route.case": ["amount", "state", "replenished", "kind", "driver_id", "date",
                              "route_id", "stop_id", "promise_id", "resolution"],
        "distri.route": ["company_id"],
        "distri.route.stop": ["order_id", "partner_id", "visit_captured_at"],
        "distri.promised.transfer": ["captured_at"],
    }

    case_id = fields.Many2one("distri.route.case", string="Cargo", readonly=True)
    employee_id = fields.Many2one("res.users", string="Empleado", readonly=True)
    route_id = fields.Many2one("distri.route", string="Ruta", readonly=True)
    origin = fields.Selection(ORIGINS, string="Origen", readonly=True)
    order_id = fields.Many2one("sale.order", string="Pedido", readonly=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True)
    delivered_at = fields.Datetime(string="Entregado", readonly=True)
    date = fields.Date(string="Fecha de la ruta", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    currency_id = fields.Many2one(related="company_id.currency_id")
    charge_amount = fields.Monetary(string="Importe del cargo", readonly=True,
                                    help="Lo que se le carga hoy al empleado por este caso.")
    amount_owed = fields.Monetary(string="Adeuda", readonly=True,
                                  help="Lo que todavía debe: cero si está saldado.")
    state = fields.Selection(DEBT_STATES, string="Estado", readonly=True)
    replenished = fields.Boolean(string="Repuesto por el repartidor", readonly=True)
    settled_reason = fields.Char(string="Cómo se saldó", compute="_compute_texts")
    document = fields.Char(string="Factura", compute="_compute_texts")
    amount_history_ids = fields.One2many(related="case_id.amount_history_ids",
                                         string="Historial del importe")

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW %s AS (
                SELECT c.id AS id,
                       c.id AS case_id,
                       c.driver_id AS employee_id,
                       c.route_id AS route_id,
                       c.kind AS origin,
                       s.order_id AS order_id,
                       s.partner_id AS partner_id,
                       COALESCE(p.captured_at, s.visit_captured_at) AS delivered_at,
                       c.date AS date,
                       r.company_id AS company_id,
                       -c.amount AS charge_amount,
                       CASE WHEN c.state = 'open' AND NOT COALESCE(c.replenished, FALSE)
                            THEN -c.amount ELSE 0 END AS amount_owed,
                       CASE WHEN c.state = 'open' AND NOT COALESCE(c.replenished, FALSE)
                            THEN 'owed' ELSE 'settled' END AS state,
                       COALESCE(c.replenished, FALSE) AS replenished
                  FROM distri_route_case c
                  JOIN distri_route r ON r.id = c.route_id
             LEFT JOIN distri_route_stop s ON s.id = c.stop_id
             LEFT JOIN distri_promised_transfer p ON p.id = c.promise_id
                 WHERE c.kind IN ('receipt', 'unpaid', 'short')
            )""" % self._table)

    @api.depends("case_id", "order_id", "state", "replenished")
    def _compute_texts(self):
        for debt in self:
            debt.document = document_of(debt.order_id)[0] if debt.order_id else ""
            if debt.state != "settled":
                debt.settled_reason = ""
            elif debt.replenished:
                debt.settled_reason = "Repuesto por el repartidor"
            else:
                debt.settled_reason = (debt.sudo().case_id.resolution or
                                       "Cerrado por administración")

    def action_open_case(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "res_model": "distri.route.case",
                "res_id": self.case_id.id, "view_mode": "form"}


class ReportEmployeeDebts(models.AbstractModel):
    """The PDF: one page per employee with what he owes and what was settled."""
    _name = "report.distri_pedido.deudas_empleado_documento"
    _description = "Resumen de deudas por empleado"

    @api.model
    def _get_report_values(self, docids, data=None):
        debts = self.env["distri.employee.debt"].browse(docids)
        employees = []
        for employee, items in debts.grouped("employee_id").items():
            items = items.sorted(lambda d: (d.delivered_at or fields.Datetime.now(), d.id))
            owed = items.filtered(lambda d: d.state == "owed")
            employees.append({
                "employee": employee,
                "owed": owed,
                "settled": items - owed,
                "total": sum(owed.mapped("amount_owed")),
            })
        return {
            "doc_ids": docids,
            "doc_model": "distri.employee.debt",
            "docs": debts,
            "employees": employees,
            "origins": dict(ORIGINS),
            "money": _money,
            "printed": fields.Datetime.context_timestamp(
                self, fields.Datetime.now()).strftime("%d/%m/%Y %H:%M"),
        }
