"""Location of the driver's app at the key moments of a route (19.0.1.35.0).

Privacy (Ley 25.326): the phone asks for one fresh position only when the
driver taps one of these actions of an active route, never continuously and
never outside work:

- «Salir a repartir» (start of the route),
- «Llegué» (arrival at a customer),
- closing a visit (delivered, not delivered, parcel dispatched),
- «Cerrar la ruta».

The position travels inside the outbox event (lat, lng, accuracy in meters
and the phone's time), so it works without signal. Before the first capture
the driver accepts a notice once; its version and date stay on the user.

The coordinates are kept for a retention period (12 months by default) and a
daily cron clears them; the times stay, so «Tiempos de reparto» keeps
working on old routes. The event log never stores the coordinates
(reparto_sync._without_photo).

A customer's saved location is never overwritten by these points: only
administración can copy a good point with «Guardar como ubicación del
cliente».
"""
import math
from datetime import datetime, timezone

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .reparto_cliente import AR_TZ
from .reparto_sync import parse_phone_time

ADMIN_GROUP = "distri_pedido.grupo_administracion"
DRIVER_GROUP = "distri_pedido.grupo_reparto"

PARAM_REQUIRED = "distri_pedido.gps_obligatorio"
PARAM_ACCURACY = "distri_pedido.gps_precision_m"
PARAM_DISTANCE = "distri_pedido.gps_distancia_m"
PARAM_RETENTION = "distri_pedido.gps_retencion_meses"
DEFAULT_ACCURACY = 100
DEFAULT_DISTANCE = 300
DEFAULT_RETENTION = 12

# Bump it when the wording of the notice in the app changes: every driver
# accepts the new one before the next capture.
NOTICE_VERSION = "1"

GPS_KINDS = [
    ("start", "Inicio de ruta"),
    ("arrive", "Llegada"),
    ("close_visit", "Cierre de visita"),
    ("end", "Cierre de ruta"),
]
# event of the app -> moment of the route
EVENT_KIND = {
    "depart": "start",
    "arrive": "arrive",
    "visit": "close_visit",
    "parcel": "close_visit",
    "close": "end",
}
MISSING_REASONS = [
    ("denied", "Ubicación no permitida en el celular"),
    ("unavailable", "Sin señal de GPS"),
    ("timeout", "El GPS no respondió a tiempo"),
    ("unsupported", "El celular no da la ubicación"),
    ("not_required", "Sin ubicación (GPS no obligatorio)"),
]
OVERRIDE_REASON = "Sin GPS – autorizado"


def _int_param(env, key, default, low, high):
    raw = env["ir.config_parameter"].sudo().get_param(key)
    try:
        value = int(float(raw)) if raw not in (None, False, "") else default
    except (TypeError, ValueError):
        value = default
    return min(max(value, low), high)


def gps_settings(env):
    """The four settings of «Reparto › Ajustes › Ubicación (GPS)»."""
    raw = env["ir.config_parameter"].sudo().get_param(PARAM_REQUIRED, "1")
    return {
        "required": str(raw).strip() not in ("0", "False", "false", ""),
        "accuracy": _int_param(env, PARAM_ACCURACY, DEFAULT_ACCURACY, 5, 5000),
        "distance": _int_param(env, PARAM_DISTANCE, DEFAULT_DISTANCE, 20, 50000),
        "retention": _int_param(env, PARAM_RETENTION, DEFAULT_RETENTION, 1, 120),
    }


def haversine(lat1, lng1, lat2, lng2):
    """Meters between two points (the same formula as the app)."""
    radius, rad = 6371000.0, math.pi / 180
    d_lat, d_lng = (lat2 - lat1) * rad, (lng2 - lng1) * rad
    x = (math.sin(d_lat / 2) ** 2
         + math.cos(lat1 * rad) * math.cos(lat2 * rad) * math.sin(d_lng / 2) ** 2)
    return 2 * radius * math.asin(math.sqrt(x))


def map_url(lat, lng):
    """A plain https link: nothing from the map is embedded in Odoo."""
    return "https://www.google.com/maps/search/?api=1&query=%.6f,%.6f" % (lat, lng)


def _coord(value, limit):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or abs(number) > limit:
        return None
    return number


def local_hhmm(moment):
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%H:%M")


def duration_text(seconds):
    if seconds is None:
        return ""
    minutes = int(round(max(seconds, 0) / 60.0))
    if minutes < 60:
        return "%s min" % minutes
    return "%s h %02d min" % (minutes // 60, minutes % 60)


class DistriRouteGps(models.Model):
    _name = "distri.route.gps"
    _description = "Ubicación registrada por la app de reparto"
    _order = "device_time, id"
    _rec_name = "kind"

    route_id = fields.Many2one("distri.route", string="Ruta", required=True,
                               ondelete="cascade", index=True, readonly=True)
    driver_id = fields.Many2one("res.users", string="Repartidor", readonly=True, index=True)
    stop_id = fields.Many2one("distri.route.stop", string="Visita", ondelete="set null",
                              readonly=True, index=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True)
    shipping_partner_id = fields.Many2one("res.partner", string="Dirección de entrega",
                                          readonly=True)
    kind = fields.Selection(GPS_KINDS, string="Momento", required=True, readonly=True)
    event_uuid = fields.Char(string="Evento", readonly=True, index=True)
    latitude = fields.Float(string="Latitud", digits=(10, 7), readonly=True)
    longitude = fields.Float(string="Longitud", digits=(10, 7), readonly=True)
    accuracy = fields.Float(string="Precisión (m)", digits=(10, 0), readonly=True)
    device_time = fields.Datetime(string="Hora en el celular", readonly=True)
    received_at = fields.Datetime(string="Recibido", readonly=True,
                                  default=fields.Datetime.now)
    missing = fields.Boolean(string="Sin ubicación", readonly=True)
    missing_reason = fields.Selection(MISSING_REASONS, string="Por qué no hay ubicación",
                                      readonly=True)
    low_accuracy = fields.Boolean(string="Precisión baja", readonly=True)
    distance_m = fields.Float(string="Distancia al cliente (m)", digits=(10, 0), readonly=True)
    far = fields.Boolean(string="Lejos del cliente", readonly=True)
    purged = fields.Boolean(string="Coordenadas borradas", readonly=True,
                            help="Pasó el plazo de conservación: se borraron las coordenadas y "
                                 "quedan los horarios.")
    purged_at = fields.Datetime(string="Borradas el", readonly=True)
    flags = fields.Char(string="Avisos", compute="_compute_flags")
    map_url = fields.Char(string="Mapa", compute="_compute_map_url")
    can_save_location = fields.Boolean(compute="_compute_can_save_location")

    @api.depends("missing", "missing_reason", "low_accuracy", "accuracy", "far", "distance_m",
                 "purged")
    def _compute_flags(self):
        reasons = dict(MISSING_REASONS)
        for rec in self:
            bits = []
            if rec.missing:
                bits.append(reasons.get(rec.missing_reason) or "Sin ubicación")
            if rec.low_accuracy:
                bits.append("Precisión baja (±%d m)" % round(rec.accuracy or 0))
            if rec.far:
                bits.append("Lejos del cliente (%d m)" % round(rec.distance_m or 0))
            rec.flags = " · ".join(bits)

    @api.depends("latitude", "longitude", "missing", "purged")
    def _compute_map_url(self):
        for rec in self:
            ok = not rec.missing and not rec.purged and (rec.latitude or rec.longitude)
            rec.map_url = map_url(rec.latitude, rec.longitude) if ok else False

    @api.depends("missing", "purged", "low_accuracy", "stop_id", "kind")
    def _compute_can_save_location(self):
        for rec in self:
            rec.can_save_location = bool(
                rec.stop_id and rec.kind in ("arrive", "close_visit") and not rec.missing
                and not rec.purged and not rec.low_accuracy)

    # ---------------------------------------------------------- record

    @api.model
    def _distri_record(self, user, route, event_type, payload, captured, event_uuid):
        """One point per capture. An event without "gps" is not a capture
        (an older app, or a reason edited afterwards): nothing to record."""
        kind = EVENT_KIND.get(event_type)
        if not kind or not isinstance(payload, dict) or "gps" not in payload:
            return self.browse()
        gps = payload.get("gps") if isinstance(payload.get("gps"), dict) else {}
        settings = gps_settings(self.env)
        stop = self.env["distri.route.stop"]
        if kind in ("arrive", "close_visit"):
            try:
                stop_id = int(payload.get("stop_id") or 0)
            except (TypeError, ValueError):
                stop_id = 0
            stop = route.stop_ids.filtered(lambda s: s.id == stop_id)[:1]
        ship = (stop.shipping_partner_id or stop.partner_id) if stop else self.env["res.partner"]
        lat = _coord(gps.get("lat"), 90)
        lng = _coord(gps.get("lng"), 180)
        acc = _coord(gps.get("acc"), 1e6)
        vals = {
            "route_id": route.id,
            "driver_id": user.id,
            "stop_id": stop.id or False,
            "partner_id": stop.partner_id.id if stop else False,
            "shipping_partner_id": ship.id or False,
            "kind": kind,
            "event_uuid": event_uuid,
            "device_time": parse_phone_time(gps.get("at")) or captured,
        }
        if lat is None or lng is None or (lat == 0 and lng == 0):
            reason = gps.get("error")
            vals.update({"missing": True,
                         "missing_reason": reason if reason in dict(MISSING_REASONS)
                         else "unavailable"})
        else:
            acc = abs(acc) if acc is not None else 0.0
            vals.update({"latitude": lat, "longitude": lng, "accuracy": acc,
                         "low_accuracy": acc > settings["accuracy"]})
            if ship and (ship.partner_latitude or ship.partner_longitude):
                meters = haversine(ship.partner_latitude, ship.partner_longitude, lat, lng)
                vals.update({"distance_m": meters, "far": meters > settings["distance"]})
        return self.sudo().create(vals)

    # ---------------------------------------------------------- actions

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError("Solo administración o gerencia.")

    def action_save_customer_location(self):
        """«Guardar como ubicación del cliente»: by hand, from a good point."""
        self._check_admin()
        self.ensure_one()
        if not self.can_save_location:
            raise UserError("Ese punto no sirve como ubicación del cliente: tiene que ser de una "
                            "visita, con coordenadas y con buena precisión.")
        ship = self.shipping_partner_id or self.partner_id
        if not ship:
            raise UserError("El punto no tiene cliente.")
        ship.sudo().write({
            "partner_latitude": self.latitude,
            "partner_longitude": self.longitude,
            "distri_location_user_id": self.env.user.id,
            "distri_location_date": fields.Datetime.now(),
        })
        self.route_id.message_post(
            body="%s guardó la ubicación de %s desde la visita (precisión ±%d m)." % (
                self.env.user.name, ship.display_name, round(self.accuracy or 0)))
        return True

    # ---------------------------------------------------------- retention

    @api.model
    def _cron_purge(self):
        """Clears the coordinates older than the retention period. The
        times, the moments and the flags stay."""
        months = gps_settings(self.env)["retention"]
        cutoff = fields.Datetime.now() - relativedelta(months=months)
        old = self.sudo().search([("purged", "=", False), "|",
                                  ("device_time", "<", cutoff),
                                  "&", ("device_time", "=", False), ("received_at", "<", cutoff)])
        old.write({"latitude": 0.0, "longitude": 0.0, "accuracy": 0.0, "distance_m": 0.0,
                   "purged": True, "purged_at": fields.Datetime.now()})
        return len(old)


class ResUsersGps(models.Model):
    _inherit = "res.users"

    distri_gps_notice_version = fields.Char(string="Aviso de ubicación aceptado (versión)",
                                            readonly=True)
    distri_gps_notice_date = fields.Datetime(string="Aviso de ubicación aceptado el",
                                             readonly=True)


class DistriRouteGpsFields(models.Model):
    _inherit = "distri.route"

    gps_point_ids = fields.One2many("distri.route.gps", "route_id", string="Ubicaciones")
    gps_flag_count = fields.Integer(string="Avisos de ubicación", compute="_compute_gps_flags")
    gps_override = fields.Boolean(string=OVERRIDE_REASON, readonly=True, copy=False,
                                  tracking=True)
    gps_override_reason = fields.Text(string="Motivo (sin GPS)", readonly=True, copy=False)
    gps_override_user_id = fields.Many2one("res.users", string="Autorizó sin GPS",
                                           readonly=True, copy=False)
    gps_override_date = fields.Datetime(string="Autorizado sin GPS el", readonly=True,
                                        copy=False)

    @api.depends("gps_point_ids.missing", "gps_point_ids.low_accuracy", "gps_point_ids.far")
    def _compute_gps_flags(self):
        for route in self:
            route.gps_flag_count = len(route.gps_point_ids.filtered(
                lambda p: p.missing or p.low_accuracy or p.far))

    def _app_payload(self):
        payload = super()._app_payload()
        settings = gps_settings(self.env)
        driver = self.driver_id
        payload["gps"] = {
            # «Sin GPS – autorizado» lets this route go on without a position
            "required": settings["required"] and not self.gps_override,
            "accuracy_m": settings["accuracy"],
            "retention_months": settings["retention"],
            "notice_version": NOTICE_VERSION,
            "notice_accepted": driver.distri_gps_notice_version == NOTICE_VERSION,
        }
        return payload

    def action_gps_override(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError("Solo administración o gerencia autorizan una ruta sin GPS.")
        return {
            "type": "ir.actions.act_window",
            "name": OVERRIDE_REASON,
            "res_model": "distri.route.gps.override",
            "view_mode": "form",
            "target": "new",
            "context": {"default_route_id": self.id},
        }


class DistriRouteGpsOverride(models.TransientModel):
    """«Sin GPS – autorizado»: the route goes on without the position, with
    a reason that stays visible in «Tiempos de reparto»."""
    _name = "distri.route.gps.override"
    _description = "Autorizar una ruta sin GPS"

    route_id = fields.Many2one("distri.route", string="Ruta", required=True)
    reason = fields.Text(string="Motivo", required=True)

    def action_confirm(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError("Solo administración o gerencia autorizan una ruta sin GPS.")
        reason = " ".join((self.reason or "").split())
        if len(reason) < 5:
            raise UserError("Escribí el motivo.")
        self.route_id.sudo().write({
            "gps_override": True,
            "gps_override_reason": reason,
            "gps_override_user_id": self.env.user.id,
            "gps_override_date": fields.Datetime.now(),
        })
        self.route_id.message_post(body="%s: %s (autorizó %s)." % (
            OVERRIDE_REASON, reason, self.env.user.name))
        return {"type": "ir.actions.act_window_close"}


class DistriSyncEventGps(models.Model):
    _inherit = "distri.sync.event"

    def _apply(self, user, event):
        note = super()._apply(user, event)
        payload = event.get("payload") or {}
        if event.get("type") in EVENT_KIND and isinstance(payload, dict) and "gps" in payload:
            route = self._route_for(user, event)
            captured = parse_phone_time(event.get("captured_at")) or fields.Datetime.now()
            self.env["distri.route.gps"]._distri_record(
                user, route, event.get("type"), payload, captured, str(event.get("uuid")))
        return note

    def _apply_arrive(self, user, route, payload, captured):
        """«Llegué»: only the moment (and its position) matters."""
        if route.state != "on_route":
            raise UserError("La ruta no está en reparto.")
        self._stop_for(route, payload)

    def _apply_gps_notice(self, user, route, payload, captured):
        version = str(payload.get("version") or "")[:16]
        if not version:
            raise UserError("Falta la versión del aviso.")
        if user.distri_gps_notice_version != version:
            user.sudo().write({"distri_gps_notice_version": version,
                               "distri_gps_notice_date": captured})
        return "aviso %s" % version


class DistriGpsConfig(models.TransientModel):
    """«Reparto › Ajustes › Ubicación (GPS)»."""
    _name = "distri.gps.config"
    _description = "Ajustes de ubicación del reparto"

    gps_required = fields.Boolean(
        string="Ubicación obligatoria",
        default=lambda self: gps_settings(self.env)["required"],
        help="Activado: sin ubicación la app no deja salir, marcar «Llegué», cerrar visitas ni "
             "cerrar la ruta. Administración puede autorizar una ruta «Sin GPS».")
    accuracy_m = fields.Integer(
        string="Precisión baja desde (m)",
        default=lambda self: gps_settings(self.env)["accuracy"],
        help="Un punto con una precisión peor queda marcado. No bloquea.")
    distance_m = fields.Integer(
        string="Lejos del cliente desde (m)",
        default=lambda self: gps_settings(self.env)["distance"],
        help="Con la ubicación del cliente guardada, una visita más lejos queda marcada.")
    retention_months = fields.Integer(
        string="Conservar coordenadas (meses)",
        default=lambda self: gps_settings(self.env)["retention"],
        help="Después se borran las coordenadas y quedan los horarios.")

    def action_save(self):
        self.ensure_one()
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError("Solo administración o gerencia cambian estos ajustes.")
        if not 5 <= self.accuracy_m <= 5000:
            raise UserError("La precisión va de 5 a 5000 m.")
        if not 20 <= self.distance_m <= 50000:
            raise UserError("La distancia va de 20 a 50000 m.")
        if not 1 <= self.retention_months <= 120:
            raise UserError("La conservación va de 1 a 120 meses.")
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_REQUIRED, "1" if self.gps_required else "0")
        params.set_param(PARAM_ACCURACY, str(self.accuracy_m))
        params.set_param(PARAM_DISTANCE, str(self.distance_m))
        params.set_param(PARAM_RETENTION, str(self.retention_months))
        return {"type": "ir.actions.act_window_close"}


class DistriRepartoTiempos(models.TransientModel):
    """«Tiempos de reparto»: filters of the report."""
    _name = "distri.reparto.tiempos"
    _description = "Tiempos de reparto"

    date_from = fields.Date(string="Desde", required=True,
                            default=lambda self: fields.Date.context_today(self))
    date_to = fields.Date(string="Hasta", required=True,
                          default=lambda self: fields.Date.context_today(self))
    driver_id = fields.Many2one("res.users", string="Repartidor",
                                domain=lambda self: [("all_group_ids", "in", self.env.ref(
                                    DRIVER_GROUP).ids)],
                                help="Vacío: todos los repartidores.")

    def _check_admin(self):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError("«Tiempos de reparto» es solo para administración y gerencia.")

    def action_view(self):
        self.ensure_one()
        self._check_admin()
        return self.env.ref("distri_pedido.report_tiempos_reparto_html").report_action(
            self, config=False)

    def action_pdf(self):
        self.ensure_one()
        self._check_admin()
        return self.env.ref("distri_pedido.report_tiempos_reparto").report_action(
            self, config=False)

    def _distri_routes(self):
        self.ensure_one()
        if self.date_to < self.date_from:
            raise UserError("«Hasta» va después de «Desde».")
        domain = [("kind", "=", "route"), ("date", ">=", self.date_from),
                  ("date", "<=", self.date_to)]
        if self.driver_id:
            domain.append(("driver_id", "=", self.driver_id.id))
        return self.env["distri.route"].sudo().search(domain, order="date, driver_id, trip, id")

    def _distri_data(self):
        """One block per route: start, end, total and one row per visit."""
        self._check_admin()
        return [self._distri_route_data(route) for route in self._distri_routes()]

    @staticmethod
    def _point_view(point):
        return {"time": local_hhmm(point.device_time), "flags": point.flags or "",
                "map": point.map_url or "", "kind": dict(GPS_KINDS)[point.kind],
                "accuracy": round(point.accuracy) if not point.missing and not point.purged
                else None}

    def _distri_route_data(self, route):
        points = route.gps_point_ids.sorted(lambda p: (p.device_time or datetime.min, p.id))
        starts = points.filtered(lambda p: p.kind == "start")
        ends = points.filtered(lambda p: p.kind == "end")
        start = starts[:1].device_time or route.departed_at
        end = ends[-1:].device_time or route.closed_at
        visits = []
        for stop in route.stop_ids:
            mine = points.filtered(lambda p, s=stop: p.stop_id == s)
            arrive = mine.filtered(lambda p: p.kind == "arrive")[:1]
            closes = mine.filtered(lambda p: p.kind == "close_visit")
            if arrive:
                closes = closes.filtered(lambda p, a=arrive: p.device_time >= a.device_time) \
                    or closes
            close = closes[:1]
            if not arrive and not close:
                continue
            visits.append({
                "stop": stop, "arrive": arrive, "close": close,
                "sort": (arrive or close).device_time or datetime.min,
            })
        visits.sort(key=lambda v: v["sort"])
        rows, previous = [], start
        for visit in visits:
            arrive, close = visit["arrive"], visit["close"]
            at_customer = ((close.device_time - arrive.device_time).total_seconds()
                           if arrive and close else None)
            first = arrive or close
            gap = ((first.device_time - previous).total_seconds()
                   if previous and first.device_time else None)
            previous = close.device_time if close else first.device_time
            # a parcel goes to the bus terminal: no «Llegué» there
            no_arrive = "" if visit["stop"].order_id.distri_encomienda else "Sin «Llegué»"
            flags = [f for f in (arrive.flags if arrive else no_arrive,
                                 close.flags if close else "Sin cierre") if f]
            rows.append({
                "name": visit["stop"].partner_id.display_name or "",
                "order": visit["stop"].order_id.name or "",
                "state": dict(visit["stop"]._fields["state"].selection).get(
                    visit["stop"].state, ""),
                "arrive": self._point_view(arrive) if arrive else None,
                "close": self._point_view(close) if close else None,
                "at_customer": duration_text(at_customer),
                "gap": duration_text(gap),
                "flags": " · ".join(flags),
            })
        driver = route.driver_id
        return {
            "route": route.name,
            "driver": driver.name or "",
            "date": route.date.strftime("%d/%m/%Y") if route.date else "",
            "start": local_hhmm(start),
            "end": local_hhmm(end),
            "start_point": self._point_view(starts[:1]) if starts else None,
            "end_point": self._point_view(ends[-1:]) if ends else None,
            "total": duration_text((end - start).total_seconds()) if start and end else "",
            "rows": rows,
            "flag_count": len(points.filtered(lambda p: p.missing or p.low_accuracy or p.far)),
            "override": route.gps_override,
            "override_reason": route.gps_override_reason or "",
            "override_by": route.gps_override_user_id.name or "",
            "notice": (driver.distri_gps_notice_date.strftime("%d/%m/%Y")
                       if driver.distri_gps_notice_date else ""),
            "purged": any(points.mapped("purged")),
        }


class ReportTiemposReparto(models.AbstractModel):
    _name = "report.distri_pedido.tiempos_reparto_documento"
    _description = "Tiempos de reparto (reporte)"

    @api.model
    def _get_report_values(self, docids, data=None):
        if not self.env.user.has_group(ADMIN_GROUP):
            raise AccessError("«Tiempos de reparto» es solo para administración y gerencia.")
        docs = self.env["distri.reparto.tiempos"].browse(docids)
        return {
            "doc_ids": docids,
            "doc_model": "distri.reparto.tiempos",
            "docs": docs,
            "data_of": lambda doc: doc._distri_data(),
            "date_label": lambda d: d.strftime("%d/%m/%Y") if d else "",
            "printed_at": datetime.now(timezone.utc).astimezone(AR_TZ).strftime(
                "%d/%m/%Y %H:%M"),
        }


class ReportTiemposRepartoHtml(models.AbstractModel):
    _name = "report.distri_pedido.tiempos_reparto_html"
    _inherit = "report.distri_pedido.tiempos_reparto_documento"
    _description = "Tiempos de reparto (pantalla)"
