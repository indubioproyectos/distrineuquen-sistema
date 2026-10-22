from odoo import api, fields, models

# Order matters: it is the track shown on /proyecto. "pending" sits before
# the track (nothing started yet).
STAGES = [
    ("pending", "Pendiente"),
    ("designed", "Diseñado"),
    ("programmed", "Programado"),
    ("lab", "Probado en el lab"),
    ("live", "En uso"),
]
TRACK = [key for key, _label in STAGES if key != "pending"]


class DistriProjectModule(models.Model):
    """One part of the project and how far along it is.

    Feeds the public page /proyecto and the stage strip at the top of each
    presentation. Public pages read it with sudo through
    ``project_rows``/``project_strip`` and only get the fields listed there.
    """

    _name = "distri.project.module"
    _description = "Parte del proyecto"
    _order = "sequence, id"

    code = fields.Char(
        string="Clave", required=True,
        help="Clave técnica. La usa cada presentación para mostrar su etapa.")
    name = fields.Char(string="Nombre", required=True, translate=False)
    sequence = fields.Integer(string="Orden", default=10)
    description = fields.Text(string="Qué hace")
    stage = fields.Selection(STAGES, string="Etapa", required=True, default="pending")
    next_step = fields.Text(string="Qué falta / próximo paso")
    presentation_url = fields.Char(
        string="Presentación",
        help="Dirección de la presentación pública, por ejemplo /presentacion/reparto.")
    depends_on_ids = fields.Many2many(
        "distri.project.module", "distri_project_module_dep_rel",
        "module_id", "depends_on_id", string="Depende de")
    active = fields.Boolean(default=True)

    _code_unique = models.Constraint("unique(code)", "La clave ya existe.")

    @api.model
    def project_rows(self):
        """Public-safe rows for /proyecto, in order."""
        rows = []
        for module in self.sudo().search([]):
            stage_index = TRACK.index(module.stage) if module.stage in TRACK else -1
            rows.append({
                "code": module.code,
                "name": module.name,
                "description": module.description or "",
                "stage": module.stage,
                "stage_label": dict(STAGES)[module.stage],
                "stage_index": stage_index,
                "next_step": module.next_step or "",
                "url": module.presentation_url or "",
                "depends_on": module.depends_on_ids.mapped("name"),
            })
        return rows

    @api.model
    def project_strip(self, code):
        """Stage of one module for the strip on top of a presentation."""
        module = self.sudo().search([("code", "=", code)], limit=1)
        if not module:
            return False
        return {
            "name": module.name,
            "stage": module.stage,
            "stage_label": dict(STAGES)[module.stage],
            "next_step": module.next_step or "",
        }

    @api.model
    def track_labels(self):
        return [dict(STAGES)[key] for key in TRACK]
