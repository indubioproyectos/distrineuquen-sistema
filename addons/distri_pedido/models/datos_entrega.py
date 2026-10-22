"""«Datos para la entrega»: what the customer tells us from the order link.

- Days and hours: the same contact fields the drivers edit on the phone
  (reparto_cliente.py), on the shipping contact, as the stop payload uses it.
- Location: the phone's GPS reading is kept apart (distri_customer_*): a
  location saved by a driver on the spot is never overwritten from the web.
  Only a contact without any location gets it as its main one.
- A notice for this one order: «Hoy cierro antes», «Traémelo otro día» or
  «Otro», with a short text.

Nothing is sent to the customer: Odoo only receives WhatsApp (decision 9 in
docs/distrineuquen-modulo/cobranza/README.md, Meta bills every send). What
the customer sees is on the public pages. Every change leaves an internal
note on the order and a To-Do for its salesperson, so «Traémelo otro día»
is acted upon by a person: the order is never taken off a route here.
"""
import math
import re
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models

from .reparto_cliente import WEEKDAYS, normalize_days, normalize_hours

NOTICE_KINDS = [
    ("closes_early", "Hoy cierro antes"),
    ("other_day", "Traémelo otro día"),
    ("other", "Otro"),
]
NOTICE_MAX = 300
# A phone reading worse than this could put the pin on another block.
MAX_ACCURACY_M = 150
# Each change posts a note and touches an activity: a link cannot flood them.
MAX_CHANGES_PER_HOUR = 20
MAX_RANGES = 4
CHATTER_TITLE = "Datos para la entrega"
ACTIVITY_SUMMARY = "Revisar lo que avisó el cliente para la entrega"

_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


def maps_url(lat, lng):
    return "https://www.google.com/maps?q=%.6f,%.6f" % (lat, lng)


def weekday_date(day):
    """"martes 29/09", like the route names."""
    return "%s %s" % (WEEKDAYS[day.weekday()], day.strftime("%d/%m")) if day else ""


class ResPartnerCustomerData(models.Model):
    _inherit = "res.partner"

    distri_customer_latitude = fields.Float(
        string="Latitud enviada por el cliente", digits=(10, 7), copy=False, readonly=True)
    distri_customer_longitude = fields.Float(
        string="Longitud enviada por el cliente", digits=(10, 7), copy=False, readonly=True)
    distri_customer_location_accuracy = fields.Float(
        string="Precisión (m)", digits=(6, 0), copy=False, readonly=True)
    distri_customer_location_date = fields.Datetime(
        string="Ubicación enviada por el cliente el", copy=False, readonly=True)
    distri_customer_location_order_id = fields.Many2one(
        "sale.order", string="Enviada desde el pedido", copy=False, readonly=True,
        ondelete="set null")
    distri_customer_location_url = fields.Char(
        string="Ubicación enviada por el cliente", compute="_compute_distri_customer_location_url")
    distri_customer_hours_date = fields.Datetime(
        string="Horarios cambiados por el cliente el", copy=False, readonly=True)

    @api.depends("distri_customer_latitude", "distri_customer_longitude",
                 "distri_customer_location_date")
    def _compute_distri_customer_location_url(self):
        for partner in self:
            partner.distri_customer_location_url = maps_url(
                partner.distri_customer_latitude, partner.distri_customer_longitude,
            ) if partner.distri_customer_location_date else False

    def _distri_has_location(self):
        self.ensure_one()
        return bool(self.partner_latitude or self.partner_longitude)


class SaleOrderCustomerNotice(models.Model):
    _inherit = "sale.order"

    distri_customer_notice_kind = fields.Selection(
        NOTICE_KINDS, string="Aviso del cliente", copy=False, readonly=True)
    distri_customer_notice = fields.Char(
        string="Texto del aviso", size=NOTICE_MAX, copy=False, readonly=True)
    distri_customer_notice_date = fields.Datetime(
        string="Aviso enviado el", copy=False, readonly=True)
    distri_customer_notice_text = fields.Char(
        string="Aviso del cliente para la entrega", compute="_compute_distri_customer_notice_text")

    @api.depends("distri_customer_notice_kind", "distri_customer_notice")
    def _compute_distri_customer_notice_text(self):
        labels = dict(NOTICE_KINDS)
        for order in self:
            kind = labels.get(order.distri_customer_notice_kind)
            parts = [p for p in (kind, order.distri_customer_notice) if p]
            if order.distri_customer_notice_kind == "other" and order.distri_customer_notice:
                parts = [order.distri_customer_notice]
            order.distri_customer_notice_text = ": ".join(parts) if parts else False

    # ------------------------------------------------------- route status

    def _distri_route_status(self):
        """Where the order is on the delivery routes, for «Tu pedido».

        The last stop decides (a reassigned order has a newer one). Only
        truck routes: the depot deliveries have their own texts. Returns
        False when the other texts of the page apply (delivered, dispatched,
        packed...).
        """
        self.ensure_one()
        last = self.sudo().distri_stop_ids.sorted("id")[-1:]
        if not last or last.route_id.kind != "route":
            return False
        if last.delivery_state == "not_delivered":
            return {"kind": "not_delivered"}
        if last.delivery_state != "pending":
            return False
        if last.route_id.state == "draft":
            return {"kind": "assigned", "date": last.route_id.date,
                    "day": weekday_date(last.route_id.date)}
        if last.route_id.state == "on_route":
            return {"kind": "on_route"}
        return False

    # ------------------------------------------------- «Datos para la entrega»

    def _distri_delivery_contact(self):
        """The contact the driver sees for this order (the stop payload uses
        the shipping contact), or an empty recordset while the order is under
        a generic «Pedidos web» contact."""
        self.ensure_one()
        order = self.sudo()
        partner = order.partner_id
        ship = order.partner_shipping_id or partner
        if not partner or partner.distri_generic_partner or ship.distri_generic_partner:
            return self.env["res.partner"]
        return ship

    def _distri_delivery_data_open(self):
        self.ensure_one()
        return self.state != "cancel" and bool(self._distri_delivery_contact())

    def _distri_notice_open(self):
        """A notice only makes sense until the order is delivered."""
        self.ensure_one()
        return self._distri_delivery_data_open() and self.distri_delivery_state not in (
            "delivered", "dispatched")

    def _distri_customer_changes_left(self):
        self.ensure_one()
        since = fields.Datetime.now() - timedelta(hours=1)
        done = self.env["mail.message"].sudo().search_count([
            ("model", "=", "sale.order"), ("res_id", "=", self.id),
            ("create_date", ">=", since), ("body", "ilike", CHATTER_TITLE),
        ])
        return done < MAX_CHANGES_PER_HOUR

    def _distri_check_customer_change(self):
        if not self._distri_delivery_data_open():
            return "Estos datos ya no se pueden cambiar desde este link."
        if not self._distri_customer_changes_left():
            return "Hiciste muchos cambios seguidos. Probá de nuevo en un rato."
        return ""

    def _distri_customer_set_hours(self, days, ranges):
        """days: iterable of 0..6 (Monday = 0); ranges: [(from, to)] "HH:MM".

        Returns "" or the error to show. Writes the same fields as the
        driver's «Ubicación y horarios».
        """
        self.ensure_one()
        error = self._distri_check_customer_change()
        if error:
            return error
        day_list = normalize_days(",".join(str(d) for d in days or []))
        if not day_list:
            return "Marcá al menos un día en que recibís."
        clean = []
        for start, end in list(ranges or [])[:MAX_RANGES]:
            start, end = (start or "").strip(), (end or "").strip()
            if not start and not end:
                continue
            if not start or not end:
                return "Completá el horario: poné desde qué hora y hasta qué hora."
            if not (_TIME_RE.match(start) and _TIME_RE.match(end)):
                return "Revisá el horario: la hora va así, 08:30."
            if not normalize_hours("%s-%s" % (start, end)):
                return "El horario tiene que terminar después de empezar (%s a %s)." % (start, end)
            clean.append(normalize_hours("%s-%s" % (start, end)))
        if not clean:
            return "Poné al menos un horario (desde y hasta)."
        clean.sort()
        for before, after in zip(clean, clean[1:]):
            if after.split("-")[0] < before.split("-")[1]:
                return "Los horarios se superponen. Revisalos."
        hours = ";".join(clean)

        contact = self._distri_delivery_contact()
        before = contact.distri_delivery_text or "sin datos"
        contact.write({
            "distri_delivery_days": day_list,
            "distri_delivery_hours": hours,
            "distri_customer_hours_date": fields.Datetime.now(),
        })
        after = contact.distri_delivery_text
        self._distri_customer_log(Markup(
            "Cambió los días y horarios en que recibe: antes «%s», ahora «%s».") % (before, after))
        return ""

    def _distri_customer_set_location(self, lat, lng, accuracy):
        """The customer's phone GPS, read at the shop. Returns "" or the error."""
        self.ensure_one()
        error = self._distri_check_customer_change()
        if error:
            return error
        try:
            lat, lng, accuracy = float(lat), float(lng), float(accuracy)
        except (TypeError, ValueError):
            return "No pudimos leer tu ubicación. Probá de nuevo."
        if not all(math.isfinite(v) for v in (lat, lng, accuracy)) or \
                not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0) \
                or accuracy <= 0:
            return "No pudimos leer tu ubicación. Probá de nuevo."
        if accuracy > MAX_ACCURACY_M:
            return ("La ubicación no es precisa (±%d m). Salí a la vereda o prendé el GPS "
                    "del celular y probá de nuevo." % round(accuracy))

        contact = self._distri_delivery_contact()
        now = fields.Datetime.now()
        vals = {
            "distri_customer_latitude": lat,
            "distri_customer_longitude": lng,
            "distri_customer_location_accuracy": accuracy,
            "distri_customer_location_date": now,
            "distri_customer_location_order_id": self.id,
        }
        main = not contact._distri_has_location()
        if main:
            # nothing to protect: without a location the driver has nothing
            vals.update({
                "partner_latitude": lat,
                "partner_longitude": lng,
                "distri_location_user_id": False,
                "distri_location_date": now,
            })
        contact.write(vals)
        url = maps_url(lat, lng)
        self._distri_customer_log(Markup(
            'Mandó la ubicación de su negocio desde el celular (±%d m): '
            '<a href="%s" target="_blank" rel="noopener">ver en el mapa</a>. %s') % (
            round(accuracy), url,
            "El contacto no tenía ubicación: queda como la principal." if main else
            "La ubicación guardada por el repartidor no se cambió: revisala con esta."))
        return ""

    def _distri_customer_set_notice(self, kind, text):
        """The notice for this order. Returns "" or the error."""
        self.ensure_one()
        error = self._distri_check_customer_change()
        if error:
            return error
        if not self._distri_notice_open():
            return "Tu pedido ya fue entregado."
        if kind not in dict(NOTICE_KINDS):
            return "Elegí qué nos querés avisar."
        text = " ".join(str(text or "").split())[:NOTICE_MAX]
        if kind == "other" and not text:
            return "Contanos qué pasa en el texto."
        self.sudo().write({
            "distri_customer_notice_kind": kind,
            "distri_customer_notice": text or False,
            "distri_customer_notice_date": fields.Datetime.now(),
        })
        self._distri_customer_log(Markup("Dejó un aviso para la entrega: «%s».%s") % (
            self.distri_customer_notice_text,
            Markup(" <b>Pide otra fecha: coordiná la entrega y, si hace falta, sacalo de la "
                   "ruta a mano.</b>") if kind == "other_day" else ""))
        return ""

    def _distri_customer_clear_notice(self):
        self.ensure_one()
        error = self._distri_check_customer_change()
        if error:
            return error
        if not self.distri_customer_notice_kind:
            return ""
        old = self.distri_customer_notice_text
        self.sudo().write({"distri_customer_notice_kind": False, "distri_customer_notice": False,
                           "distri_customer_notice_date": fields.Datetime.now()})
        self._distri_customer_log(Markup("Borró su aviso para la entrega («%s»).") % old)
        return ""

    def _distri_customer_log(self, what):
        """Internal note on the order and one open To-Do for its salesperson
        (or administración). Notes notify nobody outside the company."""
        self.ensure_one()
        order = self.sudo()
        contact = order._distri_delivery_contact()
        body = Markup("<p><b>%s</b> · %s, desde el link del pedido: %s</p>") % (
            CHATTER_TITLE, contact._distri_app_name() if contact else order.partner_id.name, what)
        order.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note",
                           author_id=order.partner_id.id)
        user = order.user_id if order.user_id and order.user_id.active and \
            not order.user_id.share else order._distri_usuario_administracion()
        # no e-mail to the assignee either: the activity shows up in Odoo
        order = order.with_context(mail_activity_quick_update=True)
        open_ones = order.activity_ids.filtered(lambda a: a.summary == ACTIVITY_SUMMARY)
        if open_ones:
            open_ones[:1].write({"note": body, "date_deadline": fields.Date.context_today(order)})
        else:
            order.activity_schedule("mail.mail_activity_data_todo", summary=ACTIVITY_SUMMARY,
                                    note=body, user_id=user.id)

    def _distri_notice_payload(self):
        """The notice as the driver's phone shows it."""
        self.ensure_one()
        order = self.sudo()
        if not order.distri_customer_notice_kind:
            return None
        return {
            "kind": order.distri_customer_notice_kind,
            "text": order.distri_customer_notice_text or "",
            "date": fields.Datetime.to_string(order.distri_customer_notice_date)
            if order.distri_customer_notice_date else "",
        }


def contact_hours_form(contact):
    """Days (set of ints) and at least two [from, to] rows for the form."""
    days = {int(d) for d in (contact.distri_delivery_days or "").split(",") if d.isdigit()}
    ranges = [r.split("-") for r in (contact.distri_delivery_hours or "").split(";") if "-" in r]
    while len(ranges) < 2:
        ranges.append(["", ""])
    return days, ranges[:MAX_RANGES]
