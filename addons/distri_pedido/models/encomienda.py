"""Parcels to the interior («encomiendas»).

Orders of customers in the interior (Zapala, Cutral Có) come in like any
other order. The driver takes them to the Neuquén bus terminal and ships
them with a carrier (usually «El Petróleo S.A.» to Zapala); the customer
picks the parcel up at the terminal of their town with the guide number.

- Order: «Encomienda» is set by the zone of the customer (distri.zona
  field «Se envía por encomienda»), the carrier comes from the zone and
  «Paga el envío» follows the rule below. Administración and gerencia can
  change all three on the order; changing them back to what the rule says
  puts the order back on the rule.
- Who pays the shipping: Distrineuquén, except orders with a wholesale
  price list (product.pricelist «Lista de distribuidoras») or under the small order
  amount (parameter distri_pedido.encomienda_pedido_chico): those are paid
  by the customer at destination.
- Driver app: the parcel stop is dispatched with a mandatory photo of the
  carrier's guide, the guide number and, when Distrineuquén pays, the
  shipping cost, which becomes an «Encomienda» expense of the day's cash
  (models/gasto.py). No receipt code: the customer is not there.
- As soon as the guide reaches the server, the salesperson gets an activity
  on the order and administración a message, both with the guide photo and
  a ready WhatsApp text for the customer (distri.parcel).
"""
import base64
import re
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

from .reparto_cliente import town_from_name

PAYERS = [
    ("distri", "Distrineuquén"),
    ("customer", "Cliente en destino"),
]
PAYER_LABELS = {
    "distri": "Envío pago por Distrineuquén",
    "customer": "Envío a pagar por el cliente en destino",
}
# Orders under this total go «Cliente en destino» (parameter).
SMALL_ORDER_PARAM = "distri_pedido.encomienda_pedido_chico"
SMALL_ORDER_DEFAULT = 70000.0

# «Guía N° 000123-4», «GUIA NRO: A-55821», «Nro. de guía 123456»
_GUIDE_RES = [
    # the OCR reads «N°» as «N*», «Nº», «No» or «N»
    re.compile(r"gu[ií]a\s*(?:de\s+\w+\s*)?(?:n(?:[uú]mero|ro|um|o)?)?[\s.:#°º*]*"
               r"([A-Z]{0,3}-?\d[\d-]{3,18})", re.IGNORECASE),
    re.compile(r"n(?:[uú]mero|ro|o)?[\s.°º*]*(?:de\s+)?gu[ií]a[\s.:#]*([A-Z]{0,3}-?\d[\d-]{3,18})",
               re.IGNORECASE),
]


def read_guide_number(text):
    """The guide number printed on a carrier's ticket, or ""."""
    for regex in _GUIDE_RES:
        match = regex.search(text or "")
        if match:
            return re.sub(r"\s+", "", match.group(1)).strip("-")
    return ""


def normalize_dni(value):
    """The DNI as digits (7 or 8), or "" when it is not one."""
    digits = re.sub(r"[\s.\-]", "", value or "")
    return digits if re.fullmatch(r"\d{7,8}", digits) else ""


def normalize_guide(value):
    return re.sub(r"[^0-9A-Za-z]", "", value or "").upper().lstrip("0")


class DistriCarrier(models.Model):
    _name = "distri.carrier"
    _description = "Transportista de encomiendas"
    _order = "sequence, name"

    name = fields.Char(string="Transportista", required=True)
    sequence = fields.Integer(default=10)
    phone = fields.Char(string="Teléfono")
    note = fields.Text(string="Nota", help="Horarios de la boletería, destinos, etc.")
    active = fields.Boolean(default=True)


class DistriZonaEncomienda(models.Model):
    _inherit = "distri.zona"

    encomienda = fields.Boolean(
        string="Se envía por encomienda",
        help="Los pedidos de esta zona salen por encomienda desde la terminal de ómnibus de "
             "Neuquén. Se puede cambiar en cada pedido.")
    carrier_id = fields.Many2one("distri.carrier", string="Transportista habitual")

    def write(self, vals):
        result = super().write(vals)
        if {"encomienda", "carrier_id"} & set(vals):
            # open orders follow the zone's new rule (a choice made by hand stays)
            orders = self.env["sale.order"].sudo().search([
                ("state", "in", ("draft", "sent", "sale")),
                ("distri_parcel_dispatched", "=", False),
            ])
            orders = orders.filtered(lambda o: o._distri_route_zone() in self)
            orders._distri_recompute_parcel_rule()
        return result


class ProductPricelistEncomienda(models.Model):
    _inherit = "product.pricelist"

    distri_mayorista = fields.Boolean(
        string="Lista de distribuidoras",
        help="En las encomiendas de esta lista el envío lo paga el cliente en destino.")


class SaleOrderEncomienda(models.Model):
    _inherit = "sale.order"

    distri_encomienda = fields.Boolean(
        string="Encomienda", compute="_compute_distri_encomienda", store=True, readonly=False,
        help="Sale por encomienda desde la terminal de ómnibus. Automático según la zona del "
             "cliente; administración lo puede cambiar.")
    distri_encomienda_manual = fields.Boolean(copy=False, readonly=True)
    # «Soy del interior»: what the customer typed on /pedido for the guide
    distri_encomienda_pedida = fields.Boolean(
        string="Encomienda pedida por el cliente", copy=False,
        help="El cliente marcó «Soy del interior» en la página del pedido.")
    distri_encomienda_nombre = fields.Char(string="Retira (nombre completo)", copy=False)
    distri_encomienda_dni = fields.Char(string="DNI de quien retira", copy=False)
    distri_encomienda_localidad = fields.Char(string="Terminal de destino", copy=False,
                                              help="La localidad donde retira la encomienda.")
    distri_encomienda_retira = fields.Char(string="Retira", compute="_compute_distri_encomienda_retira")
    distri_encomienda_resumen = fields.Char(string="Encomienda", compute="_compute_distri_encomienda_resumen")
    distri_carrier_id = fields.Many2one(
        "distri.carrier", string="Transportista", compute="_compute_distri_carrier",
        store=True, readonly=False)
    distri_envio_paga = fields.Selection(
        PAYERS, string="Paga el envío", compute="_compute_distri_envio_paga", store=True,
        readonly=False,
        help="Distrineuquén, salvo lista de distribuidoras o pedido chico: cliente en destino. "
             "Administración lo puede cambiar.")
    distri_envio_paga_manual = fields.Boolean(copy=False, readonly=True)
    distri_envio_paga_label = fields.Char(string="Envío", compute="_compute_distri_envio_paga_label")
    distri_parcel_ids = fields.One2many("distri.parcel", "order_id", string="Encomiendas despachadas",
                                        readonly=True)
    distri_parcel_dispatched = fields.Boolean(
        string="Encomienda despachada", compute="_compute_distri_parcel_dispatched", store=True)

    # ------------------------------------------------------------ rules

    def _distri_encomienda_rule(self):
        self.ensure_one()
        if self.distri_encomienda_pedida:
            return True
        if self.distri_entrega == "retira":
            return False
        return bool(self._distri_route_zone().encomienda)

    @api.depends("distri_encomienda", "distri_carrier_id", "distri_envio_paga",
                 "distri_encomienda_retira", "distri_encomienda_localidad")
    def _compute_distri_encomienda_resumen(self):
        for order in self:
            if not order.distri_encomienda:
                order.distri_encomienda_resumen = False
                continue
            parts = ["Encomienda a la terminal de %s" % order._distri_parcel_town(),
                     order.distri_carrier_id.name, PAYER_LABELS.get(order.distri_envio_paga),
                     "Retira: %s" % order.distri_encomienda_retira
                     if order.distri_encomienda_retira else ""]
            order.distri_encomienda_resumen = " · ".join(p for p in parts if p)

    @api.depends("distri_encomienda_nombre", "distri_encomienda_dni")
    def _compute_distri_encomienda_retira(self):
        for order in self:
            parts = [order.distri_encomienda_nombre or ""]
            if order.distri_encomienda_dni:
                parts.append("DNI %s" % order.distri_encomienda_dni)
            order.distri_encomienda_retira = " · ".join(p for p in parts if p)

    def _distri_small_order_amount(self):
        value = self.env["ir.config_parameter"].sudo().get_param(SMALL_ORDER_PARAM)
        try:
            return float(value) if value not in (None, False, "") else SMALL_ORDER_DEFAULT
        except ValueError:
            return SMALL_ORDER_DEFAULT

    def _distri_envio_paga_rule(self):
        """Distrineuquén pays, except wholesale lists and small orders."""
        self.ensure_one()
        if not self.distri_encomienda:
            return False
        if self.pricelist_id.distri_mayorista:
            return "customer"
        if self.amount_total < self._distri_small_order_amount():
            return "customer"
        return "distri"

    @api.depends("partner_id", "partner_id.name", "partner_id.city", "partner_shipping_id",
                 "partner_shipping_id.city", "distri_domicilio", "distri_entrega",
                 "distri_encomienda_pedida")
    def _compute_distri_encomienda(self):
        for order in self:
            if order.distri_encomienda_manual:
                order.distri_encomienda = order.distri_encomienda
            else:
                order.distri_encomienda = order._distri_encomienda_rule()

    @api.depends("distri_encomienda", "distri_encomienda_localidad")
    def _compute_distri_carrier(self):
        for order in self:
            if not order.distri_encomienda:
                order.distri_carrier_id = False
            elif not order.distri_carrier_id:
                order.distri_carrier_id = order._distri_parcel_zone().carrier_id
            else:
                order.distri_carrier_id = order.distri_carrier_id

    def _distri_parcel_zone(self):
        """The zone of the destination terminal: the town the customer
        typed, else the customer's zone."""
        self.ensure_one()
        Zona = self.env["distri.zona"].sudo()
        return (Zona.zona_de_ciudad(self.distri_encomienda_localidad)
                if self.distri_encomienda_localidad else Zona) or self._distri_route_zone()

    @api.depends("distri_encomienda", "pricelist_id", "pricelist_id.distri_mayorista",
                 "amount_total")
    def _compute_distri_envio_paga(self):
        for order in self:
            if not order.distri_encomienda:
                order.distri_envio_paga = False
            elif order.distri_envio_paga_manual and order.distri_envio_paga:
                order.distri_envio_paga = order.distri_envio_paga
            else:
                order.distri_envio_paga = order._distri_envio_paga_rule()

    @api.depends("distri_envio_paga")
    def _compute_distri_envio_paga_label(self):
        for order in self:
            order.distri_envio_paga_label = PAYER_LABELS.get(order.distri_envio_paga, "")

    @api.depends("distri_parcel_ids")
    def _compute_distri_parcel_dispatched(self):
        for order in self:
            order.distri_parcel_dispatched = bool(order.distri_parcel_ids)

    def write(self, vals):
        """A value typed on the order that differs from the rule stays; the
        same value as the rule puts the order back on the rule."""
        result = super().write(vals)
        if self.env.context.get("distri_parcel_rule"):
            return result
        for order in self:
            if "distri_encomienda" in vals:
                manual = bool(vals["distri_encomienda"]) != order._distri_encomienda_rule()
                if manual and not order.distri_encomienda_manual:
                    order._distri_check_parcel_editor()
                if manual != order.distri_encomienda_manual:
                    super(SaleOrderEncomienda, order).write({"distri_encomienda_manual": manual})
            if "distri_envio_paga" in vals:
                manual = (vals["distri_envio_paga"] or False) != order._distri_envio_paga_rule()
                if manual and not order.distri_envio_paga_manual:
                    order._distri_check_parcel_editor()
                if manual != order.distri_envio_paga_manual:
                    super(SaleOrderEncomienda, order).write({"distri_envio_paga_manual": manual})
        return result

    def _distri_recompute_parcel_rule(self):
        """Parcel, carrier and who pays again by the rule (a choice made by
        hand stays)."""
        names = ["distri_encomienda", "distri_carrier_id", "distri_envio_paga"]
        for name in names:
            self.env.add_to_compute(self._fields[name], self)
        self._recompute_recordset(names)

    def _distri_check_parcel_editor(self):
        if not self.env.su and not self.env.user.has_group("distri_pedido.grupo_administracion"):
            raise UserError("Solo administración y gerencia cambian la encomienda y quién paga "
                            "el envío.")

    def action_distri_toggle_envio_paga(self):
        """«Cambiar quién paga el envío» (administración y gerencia)."""
        self._distri_check_parcel_editor()
        for order in self:
            if not order.distri_encomienda:
                raise UserError("El pedido no es una encomienda.")
            if order.distri_parcel_dispatched:
                raise UserError("La encomienda ya se despachó.")
            new = "customer" if order.distri_envio_paga == "distri" else "distri"
            order.write({"distri_envio_paga": new})
            order.message_post(body="%s cambió quién paga el envío: %s." % (
                self.env.user.name, PAYER_LABELS[new]))
        return True

    def action_distri_parcel_whatsapp(self):
        """«Avisar al cliente por WhatsApp» from the order (salesperson)."""
        self.ensure_one()
        self.check_access("read")
        parcel = self.sudo().distri_parcel_ids[-1:]
        if not parcel:
            raise UserError("La encomienda todavía no se despachó.")
        return parcel.action_notify_whatsapp()

    def _distri_parcel_town(self):
        """The customer's town: the zone, else the name prefix."""
        self.ensure_one()
        if self.distri_encomienda_localidad:
            return self.distri_encomienda_localidad
        partner = self.partner_shipping_id or self.partner_id
        return (self._distri_route_zone().name or partner.city
                or town_from_name(self.partner_id.name))


class DistriParcel(models.Model):
    """One dispatched parcel: the carrier's guide the driver photographed."""

    _name = "distri.parcel"
    _description = "Encomienda despachada"
    _inherit = ["mail.thread"]
    _order = "dispatched_at desc, id desc"
    _rec_name = "guide_number"

    stop_id = fields.Many2one("distri.route.stop", string="Visita", required=True,
                              ondelete="restrict", index=True, readonly=True)
    route_id = fields.Many2one(related="stop_id.route_id", store=True, string="Ruta")
    driver_id = fields.Many2one(related="stop_id.driver_id", store=True, string="Repartidor")
    order_id = fields.Many2one(related="stop_id.order_id", store=True, string="Pedido")
    partner_id = fields.Many2one(related="stop_id.partner_id", store=True, string="Cliente")
    salesperson_id = fields.Many2one(related="order_id.user_id", store=True, string="Vendedor")
    company_id = fields.Many2one(related="route_id.company_id", store=True)
    currency_id = fields.Many2one(related="route_id.currency_id")
    town = fields.Char(string="Ciudad de destino", readonly=True)
    recipient = fields.Char(string="Retira", readonly=True,
                            help="Nombre completo y DNI de quien retira en la terminal.")
    remito = fields.Char(string="Remito", readonly=True)
    carrier_id = fields.Many2one("distri.carrier", string="Transportista")
    payer = fields.Selection(PAYERS, string="Paga el envío", readonly=True)
    guide_number = fields.Char(string="N° de guía", tracking=True)
    guide_number_read = fields.Char(string="N° leído en la foto", readonly=True)
    guide_review = fields.Boolean(
        string="Revisar guía", compute="_compute_guide_review", store=True,
        help="El número cargado no coincide con el leído en la foto, o no hay número.")
    ocr_state = fields.Selection([("pending", "Leyendo la foto"), ("done", "Leída")],
                                 string="Lectura", default="pending", readonly=True)
    ocr_text = fields.Text(string="Texto leído", readonly=True)
    cost = fields.Monetary(string="Costo del envío", readonly=True)
    packages = fields.Integer(string="Bultos", readonly=True)
    comment = fields.Char(string="Comentario", readonly=True)
    photo = fields.Image(string="Foto de la guía", max_width=2000, max_height=2000,
                         attachment=True, readonly=True)
    order_attachment_id = fields.Many2one("ir.attachment", string="Foto en el pedido",
                                          readonly=True)
    route_attachment_id = fields.Many2one("ir.attachment", string="Foto en la ruta",
                                          readonly=True)
    dispatched_at = fields.Datetime(string="Despachada", readonly=True)
    received_at = fields.Datetime(string="Recibida en el servidor", readonly=True)
    expense_id = fields.Many2one("distri.route.expense", string="Gasto del envío", readonly=True)
    notified = fields.Boolean(string="Cliente avisado", readonly=True, tracking=True)
    notified_at = fields.Datetime(string="Avisado el", readonly=True)
    notified_by_id = fields.Many2one("res.users", string="Avisó", readonly=True)
    message_text = fields.Text(string="Mensaje para el cliente", compute="_compute_message_text")
    whatsapp_url = fields.Char(string="WhatsApp", compute="_compute_message_text")

    _stop_unique = models.Constraint("unique(stop_id)", "La visita ya tiene su encomienda.")

    @api.depends("guide_number", "guide_number_read", "ocr_state")
    def _compute_guide_review(self):
        for parcel in self:
            typed = normalize_guide(parcel.guide_number)
            read = normalize_guide(parcel.guide_number_read)
            parcel.guide_review = (not typed) or (
                parcel.ocr_state == "done" and bool(read) and read != typed)

    @api.depends("guide_number", "carrier_id", "town", "payer", "remito", "partner_id",
                 "recipient")
    def _compute_message_text(self):
        for parcel in self:
            name = parcel.partner_id._distri_app_name() if parcel.partner_id else ""
            payer = ("Envío pago." if parcel.payer == "distri"
                     else "Envío a pagar en destino.")
            parcel.message_text = (
                "Hola %s, tu pedido %s salió hoy por %s. Guía N° %s. Lo retirás en la terminal "
                "de %s con esta guía. %s" % (
                    name, parcel.remito or parcel.order_id.name,
                    parcel.carrier_id.name or "encomienda",
                    parcel.guide_number or "(ver la foto)", parcel.town or "tu ciudad", payer))
            if parcel.recipient:
                parcel.message_text += " Retira: %s." % parcel.recipient
            digits = parcel._whatsapp_digits()
            parcel.whatsapp_url = ("https://wa.me/%s?text=%s" % (digits, quote(parcel.message_text))
                                   if digits else False)

    def _whatsapp_digits(self):
        self.ensure_one()
        if not self.partner_id:
            return ""
        digits = re.sub(r"\D", "", self.partner_id._distri_whatsapp_shown()).lstrip("0")
        if digits and not digits.startswith("54"):
            digits = "549" + digits
        return digits

    def action_notify_whatsapp(self):
        """Opens the customer's chat with the message written and marks the
        parcel as notified."""
        self.ensure_one()
        parcel = self.sudo()
        if not parcel.whatsapp_url:
            raise UserError("El cliente no tiene WhatsApp cargado. Mandale el mensaje por otro "
                            "medio y tocá «Marcar avisado».")
        parcel._mark_notified()
        return {"type": "ir.actions.act_url", "url": parcel.whatsapp_url, "target": "new"}

    def action_mark_notified(self):
        self.sudo()._mark_notified()
        return True

    def _mark_notified(self):
        for parcel in self:
            if not parcel.notified:
                parcel.write({"notified": True, "notified_at": fields.Datetime.now(),
                              "notified_by_id": self.env.uid})
            activities = parcel.order_id.activity_ids.filtered(
                lambda a: a.summary == ACTIVITY_SUMMARY)
            if activities:
                activities.action_feedback(feedback="Cliente avisado.")

    # ------------------------------------------------------------ from the phone

    @api.model
    def _distri_record(self, stop, values, photo, captured):
        """Creates or updates the parcel of a stop (reparto_sync.py)."""
        parcel = self.sudo().search([("stop_id", "=", stop.id)], limit=1)
        order = stop.order_id.sudo()
        vals = {
            "town": order._distri_parcel_town(),
            "recipient": order.distri_encomienda_retira or False,
            "remito": order._distri_remitos_texto() or order.name,
            "carrier_id": order.distri_carrier_id.id,
            "payer": order.distri_envio_paga or "distri",
            "guide_number": values.get("guide_number") or False,
            "cost": values.get("cost") or 0.0,
            "packages": values.get("packages") or order.distri_bultos,
            "comment": values.get("comment") or False,
            "dispatched_at": captured,
            "received_at": fields.Datetime.now(),
        }
        new_photo = photo and (not parcel or parcel.photo != photo)
        if new_photo:
            vals.update(photo=photo, ocr_state="pending", guide_number_read=False, ocr_text=False)
        if parcel:
            parcel.write(vals)
        else:
            parcel = self.sudo().create(dict(vals, stop_id=stop.id))
        if new_photo:
            parcel._attach_photo()
        parcel._sync_expense()
        if not parcel.message_ids.filtered(lambda m: m.body and "Encomienda despachada" in m.body):
            parcel._notify_dispatch()
        return parcel

    def _attach_photo(self):
        """The guide photo as an attachment of the order and of the route:
        the receipt that the customer needs never lives only in the phone."""
        self.ensure_one()
        Attachment = self.env["ir.attachment"].sudo()
        name = "Guía %s %s.jpg" % (self.order_id.name, self.guide_number or "")
        datas = self.photo
        vals = {}
        for field, model, res_id in (("order_attachment_id", "sale.order", self.order_id.id),
                                     ("route_attachment_id", "distri.route", self.route_id.id)):
            att = self[field]
            if att:
                att.write({"datas": datas, "name": name})
            else:
                vals[field] = Attachment.create({
                    "name": name, "datas": datas, "res_model": model, "res_id": res_id,
                    "mimetype": "image/jpeg"}).id
        if vals:
            self.write(vals)

    def _sync_expense(self):
        """Shipping paid by Distrineuquén: an «Encomienda» expense of the
        day's cash, with the guide photo as its receipt."""
        self.ensure_one()
        Expense = self.env["distri.route.expense"].sudo()
        expense = self.expense_id
        if self.payer == "distri" and self.cost > 0:
            vals = {"kind": "parcel", "amount": self.cost, "photo": self.photo,
                    "comment": "Envío %s · guía %s" % (self.order_id.name, self.guide_number or "-"),
                    "captured_at": self.dispatched_at, "state": "reported"}
            if expense:
                if expense.amount != self.cost or expense.photo != self.photo:
                    vals.update(ocr_state="pending", ocr_amount=0.0)
                expense.write(vals)
            else:
                expense = Expense.create(dict(vals, route_id=self.route_id.id,
                                              stop_id=self.stop_id.id,
                                              uuid="parcel-%s" % self.stop_id.id,
                                              ocr_state="pending"))
                self.expense_id = expense
        elif expense and expense.state == "reported":
            expense.state = "cancelled"

    def _notify_dispatch(self):
        """Message with the guide on the order for administración and an
        activity for the salesperson with the WhatsApp text."""
        self.ensure_one()
        order = self.order_id
        url = self.whatsapp_url
        link = ('<a href="%s" target="_blank">Abrir WhatsApp con el mensaje</a>' % url
                if url else "El cliente no tiene WhatsApp cargado.")
        body = (
            "<p><b>Encomienda despachada</b> por %s: %s · Guía N° %s · %s · %s bultos.</p>"
            "<p>Mensaje para el cliente:<br/><i>%s</i></p><p>%s. Después tocá «Marcar avisado» "
            "en Reparto › Encomiendas despachadas, o «Avisar encomienda» en el pedido.</p>" % (
                self.driver_id.name, self.carrier_id.name or "-", self.guide_number or "(ver la foto)",
                PAYER_LABELS.get(self.payer, ""), self.packages, _html(self.message_text), link))
        admin = self.env.ref("distri_pedido.grupo_administracion", raise_if_not_found=False)
        users = self.env["res.users"].sudo().search([
            ("all_group_ids", "in", admin.ids), ("share", "=", False)]) if admin else \
            self.env["res.users"]
        users = (users | order.user_id).filtered(lambda u: u.active and not u.share)
        attachments = self.order_attachment_id.ids
        order.sudo().message_post(
            body=_markup(body), message_type="comment", subtype_xmlid="mail.mt_comment",
            partner_ids=users.partner_id.ids, attachment_ids=attachments)
        self.message_post(body=_markup(body))
        responsible = order.user_id if order.user_id and not order.user_id.share else users[:1]
        if responsible:
            order.sudo().activity_schedule(
                "mail.mail_activity_data_todo", summary=ACTIVITY_SUMMARY,
                note=_markup(body), user_id=responsible.id)

    # ------------------------------------------------------------ OCR

    def _distri_read_guide(self):
        from .ocr_comprobante import ocr_image
        for parcel in self.sudo().filtered(lambda p: p.ocr_state == "pending"):
            text, _confidence = ocr_image(base64.b64decode(parcel.photo)) if parcel.photo else ("", 0)
            number = read_guide_number(text)
            vals = {"ocr_state": "done", "ocr_text": (text or "")[:20000],
                    "guide_number_read": number or False}
            if number and not parcel.guide_number:
                vals["guide_number"] = number
            parcel.write(vals)


ACTIVITY_SUMMARY = "Avisar al cliente: encomienda despachada"


def _html(text):
    return (text or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _markup(html):
    from markupsafe import Markup
    return Markup(html)
