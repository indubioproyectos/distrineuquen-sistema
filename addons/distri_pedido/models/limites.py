"""Rate limits for public endpoints (19.0.1.29.0).

/pedido/enviar is public: anyone can post an order. The limits stop a
flood without bothering real customers. They count in the database (Odoo
runs 2 workers: a counter in memory would be per worker) with the visitor's
IP and session, hashed with the database secret (no IP stored in clear).

The IP is the real one: nginx takes it from CF-Connecting-IP only for
Cloudflare's addresses (snippets/cloudflare-real-ip.conf), overwrites
X-Forwarded-For with it, and Odoo runs with proxy_mode.

Many customers can share an IP (mobile carriers' CGNAT, a shop's wifi), so
the per-IP limit is generous; the per-session one is tighter. Values can be
changed with system parameters (see LIMITS).
"""
import hashlib
from datetime import timedelta

from odoo import api, fields, models

# (parameter, default, window): «at most N in the window»
LIMITS = {
    "ip_10min": ("distri_pedido.pedido_limite_ip_10min", 30, timedelta(minutes=10)),
    "ip_day": ("distri_pedido.pedido_limite_ip_dia", 300, timedelta(days=1)),
    "session_10min": ("distri_pedido.pedido_limite_sesion_10min", 8, timedelta(minutes=10)),
}
KEEP = timedelta(days=2)


class DistriRateHit(models.Model):
    _name = "distri.rate.hit"
    _description = "Uso de un formulario público (límite por IP y sesión)"
    _order = "id desc"
    _log_access = False

    key = fields.Char(required=True, index=True, readonly=True)
    at = fields.Datetime(required=True, index=True, readonly=True, default=fields.Datetime.now)

    @api.model
    def _hash(self, kind, value):
        secret = self.env["ir.config_parameter"].sudo().get_param("database.secret") or ""
        digest = hashlib.sha256(("%s|%s|%s" % (secret, kind, value)).encode()).hexdigest()
        return "%s:%s" % (kind, digest[:32])

    @api.model
    def _limit(self, name, limits=None):
        """(value, window) of one limit. `limits` (19.0.1.93.0) gives another
        scope its own parameters and defaults; a None window is the usual one."""
        param, default, window = (limits or LIMITS)[name]
        window = window or LIMITS[name][2]
        try:
            value = int(self.env["ir.config_parameter"].sudo().get_param(param) or default)
        except ValueError:
            value = default
        return value, window

    @api.model
    def _distri_check(self, scope, ip, session_id, limits=None):
        """Counts one use of `scope` by this IP and session. Returns the name
        of the limit it went over (and does not count it), or False."""
        Hit = self.sudo()
        now = fields.Datetime.now()
        keys = {"ip": Hit._hash(scope + ":ip", ip or "-")}
        if session_id:
            keys["session"] = Hit._hash(scope + ":sid", session_id)
        for name in ("ip_10min", "ip_day", "session_10min"):
            target = "session" if name.startswith("session") else "ip"
            if target not in keys:
                continue
            limit, window = self._limit(name, limits)
            if Hit.search_count([("key", "=", keys[target]), ("at", ">", now - window)]) >= limit:
                return name
        Hit.create([{"key": key, "at": now} for key in keys.values()])
        return False

    @api.model
    def _distri_cleanup(self):
        """Daily: hits older than two days are useless."""
        old = self.sudo().search([("at", "<", fields.Datetime.now() - KEEP)])
        count = len(old)
        old.unlink()
        return count
