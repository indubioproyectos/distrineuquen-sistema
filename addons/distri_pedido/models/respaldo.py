"""«Respaldos»: status card of the hourly backup (19.0.1.90.0).

scripts/respaldo-hora.sh runs on the host, outside Odoo. After each run it
writes what happened straight into this table with psql (one row, the
lowest id): last good local backup, last good copy to Cloudflare R2, the R2
error if the last copy failed, and the daily size check of the R2
repository. Odoo only reads it. Not ir.config_parameter: get_param is cached
per worker, so a raw SQL update would show stale values.

The state is computed on every read, against the current time:
- error: the last copy to R2 failed (r2_error is set);
- stale: no good copy to R2 in the last 26 hours, or none recorded yet;
- ok: otherwise.
"""
from datetime import timedelta
from zoneinfo import ZoneInfo

from odoo import api, fields, models

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
UTC = ZoneInfo("UTC")
# A good copy to R2 older than this turns the card red.
R2_MAX_AGE = timedelta(hours=26)
# Cloudflare R2 free tier: 10 GB of storage (decimal gigabytes, as Cloudflare
# bills them).
R2_FREE_BYTES = 10 * 1000 ** 3
NO_DATA = "Sin datos"


def human_size(size):
    """1234567890 -> '1,2 GB' (decimal units, decimal comma)."""
    if size is None or size is False:
        return NO_DATA
    for unit in ("B", "kB", "MB", "GB"):
        if abs(size) < 1000 or unit == "GB":
            break
        size /= 1000.0
    if unit == "B":
        return "%d B" % size
    return ("%.1f %s" % (size, unit)).replace(".", ",")


def human_age(delta):
    """timedelta -> 'hace 5 min' / 'hace 3 h' / 'hace 2 días'."""
    minutes = int(delta.total_seconds() // 60)
    if minutes < 1:
        return "recién"
    if minutes < 60:
        return "hace %d min" % minutes
    hours = minutes // 60
    if hours < 48:
        return "hace %d h" % hours
    return "hace %d días" % (hours // 24)


def human_time(value, now):
    """UTC naive datetime -> '16/11/2026 10:15 (hace 2 h)' in Buenos Aires time."""
    if not value:
        return NO_DATA
    local = value.replace(tzinfo=UTC).astimezone(AR_TZ)
    return "%s (%s)" % (local.strftime("%d/%m/%Y %H:%M"), human_age(now - value))


class BackupStatus(models.Model):
    _name = "distri.backup.status"
    _description = "Estado de los respaldos"
    _order = "id"

    # Written by scripts/respaldo-hora.sh (column names = keys of
    # run/respaldo-estado.env). Read-only in Odoo.
    local_ok = fields.Datetime(string="Último respaldo local", readonly=True)
    r2_ok = fields.Datetime(string="Última copia a R2", readonly=True)
    r2_error = fields.Char(string="Error de la copia a R2", readonly=True)
    r2_bytes = fields.Float(string="Espacio usado en R2 (bytes)", readonly=True, digits=(16, 0))
    r2_snapshots = fields.Integer(string="Copias guardadas en R2", readonly=True)
    r2_checked = fields.Datetime(string="Espacio medido", readonly=True)

    state = fields.Selection(
        [("ok", "Al día"), ("stale", "Atrasado"), ("error", "Con error")],
        string="Estado", compute="_compute_display")
    state_message = fields.Char(string="Resumen", compute="_compute_display")
    local_ok_display = fields.Char(string="Último respaldo local", compute="_compute_display")
    r2_ok_display = fields.Char(string="Última copia a R2", compute="_compute_display")
    r2_checked_display = fields.Char(string="Espacio medido", compute="_compute_display")
    r2_size_display = fields.Char(string="Espacio usado en R2", compute="_compute_display")
    r2_usage_percent = fields.Float(string="Uso del plan gratis (10 GB)",
                                    compute="_compute_display")

    def _compute_display(self):
        now = fields.Datetime.now()
        for record in self:
            if record.r2_error:
                record.state = "error"
                record.state_message = "Falló la última copia a R2: %s" % record.r2_error
            elif not record.r2_ok:
                record.state = "stale"
                record.state_message = "Todavía no hay ninguna copia a R2 registrada."
            elif now - record.r2_ok > R2_MAX_AGE:
                record.state = "stale"
                record.state_message = "La última copia a R2 buena es de %s: más de 26 horas." % (
                    human_age(now - record.r2_ok))
            else:
                record.state = "ok"
                record.state_message = "La copia fuera del servidor (R2) está al día."
            record.local_ok_display = human_time(record.local_ok, now)
            record.r2_ok_display = human_time(record.r2_ok, now)
            record.r2_checked_display = human_time(record.r2_checked, now)
            if record.r2_checked or record.r2_bytes:
                record.r2_size_display = "%s de %s" % (human_size(record.r2_bytes),
                                                        human_size(R2_FREE_BYTES))
                record.r2_usage_percent = 100.0 * (record.r2_bytes or 0.0) / R2_FREE_BYTES
            else:
                record.r2_size_display = NO_DATA
                record.r2_usage_percent = 0.0

    @api.model
    def _get_status(self):
        """The status row (lowest id), created empty when the script has not
        written one yet. Callers must have checked read access."""
        status = self.sudo().search([], order="id", limit=1)
        return status or self.sudo().create({})

    @api.model
    def action_open_status(self):
        self.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "name": "Respaldos",
            "res_model": self._name,
            "res_id": self._get_status().id,
            "view_mode": "form",
            "target": "current",
        }
