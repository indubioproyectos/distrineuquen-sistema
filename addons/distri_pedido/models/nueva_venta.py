"""19.0.1.129.30: «Nueva venta», stage 1 of the owner-approved proposal
(«una pantalla, cuatro salidas»; plan unificado, Etapa 4).

One kit screen (controllers/nueva_venta.py, /nueva-venta) where staff pick
the customer, fill the cart and choose how the sale goes on. Every exit
reuses the path that already exists for it, so the records are the same:

    armar        «A armar / reparto»: the order of /pedido taken by staff
                 (controllers/main.py enviar, the same code) and confirmed:
                 it goes to /armado like any confirmed order.
    presupuesto  «Presupuesto»: the same /pedido order, left as a quotation.
    mostrador    «Mostrador»: a backend quotation and «Venta en mostrador»
                 (models/deposito.py action_distri_counter_sale): packed as
                 ordered, invoiced, then the depot delivery form with the
                 payment, as today.
    entregado    «Ya entregado»: a backend quotation, confirmed and packed as
                 ordered by the packer's own steps (the invoice comes out at
                 the end of the packing, as always), then flagged delivered
                 outside the system (19.0.1.129.25 distri_entregado_fuera)
                 with the date and the reason; its remito is dated that day.
                 It never enters route planning nor the depot delivery.

Who: «Mostrador» and «Ya entregado» are for administración (gerencia
implies it): Gonzalo, Denis and Ale. Sellers get «A armar» and
«Presupuesto»; an external seller only for his own customers (19.0.1.129.20).

Everything is behind the parameter distri_pedido.nueva_venta, OFF by
default: with it off there is no screen, no tile and nothing changes.
"""
import math
from datetime import datetime, time

import pytz
from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .deposito import ADMIN_GROUP, _check_admin

PARAM_NUEVA_VENTA = "distri_pedido.nueva_venta"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")

ARMAR = "armar"
MOSTRADOR = "mostrador"
ENTREGADO = "entregado"
PRESUPUESTO = "presupuesto"
# (key, label, help) in the order the screen shows them
SALIDAS = (
    (ARMAR, "A armar / reparto",
     "Se confirma y va a Armado, como un pedido tomado y confirmado."),
    (MOSTRADOR, "Mostrador",
     "El cliente está presente y se lo lleva ya: se arma, se factura y se registra el cobro."),
    (ENTREGADO, "Ya entregado",
     "La mercadería ya se entregó: se arma, se factura y queda entregada en la fecha que "
     "pongas."),
    (PRESUPUESTO, "Presupuesto",
     "Queda como presupuesto, sin confirmar todavía."),
)
SALIDAS_ADMIN = (MOSTRADOR, ENTREGADO)
# 19.0.1.129.30 (owner): the reason is mandatory and starts with this text
MOTIVO_ENTREGADO = "Ya entregado"
# 19.0.1.129.35 (#59): how many of the customer's last orders «Repetir» offers
REPETIR_ULTIMOS = 5
# the remito of «Ya entregado» is dated at noon (Argentina) of the chosen day
AR_TZ = pytz.timezone("America/Argentina/Buenos_Aires")
MEDIODIA = time(12, 0)


def nueva_venta_on(env):
    """19.0.1.129.30: the «Nueva venta» screen and its Inicio tile."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_NUEVA_VENTA) or "0"
    return str(value).strip().lower() in ON_VALUES


def puede_nueva_venta(user):
    """Staff who take orders (/pedido): sellers, administración, gerencia."""
    from .pedido_whatsapp import is_staff
    return is_staff(user)


def salidas_de(user):
    """The exits this user may choose, in screen order."""
    if not puede_nueva_venta(user):
        return []
    admin = user.has_group(ADMIN_GROUP)
    return [key for key, _label, _help in SALIDAS if admin or key not in SALIDAS_ADMIN]


# 19.0.1.129.59 (owner, 27/11/2026: «como podían en Odoo»): administración
# (gerencia implies it) and Sistemas change a line's price and create a
# product on the spot in «Nueva venta»; sellers see and may do neither
GRUPOS_PRECIOS = (ADMIN_GROUP, "base.group_system")
PRECIO_MAXIMO = 100_000_000
CATEGORIA_PRODUCTO_NUEVO = "Varios"


def puede_cambiar_precios(user):
    """«Cambiar el precio» of a line and «Producto nuevo» (19.0.1.129.59)."""
    return puede_nueva_venta(user) and any(user.has_group(g) for g in GRUPOS_PRECIOS)


def precio_valido(valor):
    """A typed unit price: a float above 0 (a comma is a decimal point), or
    None. «1.234,50» and «1234.5» both read as 1234.5."""
    if isinstance(valor, bool) or valor is None:
        return None
    if isinstance(valor, (int, float)):
        precio = float(valor)
    else:
        texto = str(valor).strip().replace("$", "").replace(" ", "")
        if "," in texto:
            texto = texto.replace(".", "").replace(",", ".")
        try:
            precio = float(texto)
        except ValueError:
            return None
    if not math.isfinite(precio) or precio <= 0 or precio > PRECIO_MAXIMO:
        return None
    return round(precio, 2)


def _plata(monto):
    return "$ %s" % ("{:,.2f}".format(monto).replace(",", "X").replace(".", ",").replace("X", "."))


def crear_producto_nueva_venta(env, nombre, precio):
    """19.0.1.129.59 «Producto nuevo»: a sellable product made on the spot by
    administración, created with sudo (Gonzalo has no product manager
    rights: the role is the gate, puede_cambiar_precios, checked by the
    caller) and kept out of /pedido's customer catalog (not published).
    Its type is the one every product of this database has (goods, stock
    tracked) and its category «Varios» when it exists. A chatter note says
    who made it. UserError when the name or the price cannot be used."""
    nombre = " ".join((nombre or "").split())[:120]
    if len(nombre) < 2:
        raise UserError("Escribí el nombre del producto.")
    precio = precio_valido(precio)
    if precio is None:
        raise UserError("Poné el precio del producto: un número mayor que cero.")
    Tmpl = env["product.template"].sudo()
    valores = {"name": nombre, "sale_ok": True, "type": "consu", "is_storable": True,
               "list_price": precio, "is_published": False,
               "distri_nueva_venta_creado": True}
    categoria = env["product.category"].sudo().search(
        [("name", "=ilike", CATEGORIA_PRODUCTO_NUEVO)], order="id", limit=1)
    if categoria:
        valores["categ_id"] = categoria.id
    # sudo keeps the uid: create_uid is the user
    plantilla = Tmpl.create(valores)
    plantilla.message_post(body="Producto creado por %s en «Nueva venta», a %s. No se publica "
                                "en el catálogo de /pedido." % (env.user.name, _plata(precio)))
    return plantilla


def unidad_de_venta(plantilla):
    """The unit the catalog sells in (controllers/main.py _unidad_de_venta)."""
    return plantilla.uom_ids[:1] or plantilla.uom_id


def juntar_renglones(renglones):
    """19.0.1.129.40: the same product twice in a posted cart becomes one
    line with the sum, in the order it first appeared (the phone already
    keeps one line per product; this covers any other poster). A line that
    is not {id, cantidad} is left as it is for /pedido's own check."""
    juntos, orden = {}, []
    for renglon in renglones:
        if not isinstance(renglon, dict):
            orden.append(renglon)
            continue
        try:
            clave = int(renglon.get("id"))
            cantidad = float(renglon.get("cantidad") or 0)
        except (TypeError, ValueError):
            orden.append(renglon)
            continue
        if clave in juntos:
            previo = juntos[clave]
            total = float(previo["cantidad"]) + cantidad
            previo["cantidad"] = int(total) if total.is_integer() else total
            continue
        juntos[clave] = dict(renglon, id=clave)
        orden.append(juntos[clave])
    return orden


class ProductTemplateNuevaVenta(models.Model):
    _inherit = "product.template"

    # 19.0.1.129.59: «Producto nuevo» of «Nueva venta» (to find and complete)
    distri_nueva_venta_creado = fields.Boolean(
        string="Creado en «Nueva venta»", readonly=True, copy=False,
        help="Producto creado por administración desde «Nueva venta» (botón «Producto nuevo»). "
             "No se publica en el catálogo de /pedido.")


class SaleOrderLineNuevaVenta(models.Model):
    _inherit = "sale.order.line"

    # 19.0.1.129.59: a price typed by administración in «Nueva venta»
    distri_precio_manual = fields.Boolean(
        string="Precio cambiado a mano", readonly=True, copy=False,
        help="Administración puso este precio en «Nueva venta»: «Actualizar precios» y los "
             "cambios de lista no lo pisan.")


class SaleOrderNuevaVenta(models.Model):
    _inherit = "sale.order"

    def _get_update_prices_lines(self):
        """19.0.1.129.59: a price typed in «Nueva venta» stays when the list
        is recomputed («Actualizar precios», a customer or list change)."""
        return super()._get_update_prices_lines().filtered(lambda l: not l.distri_precio_manual)

    def _distri_nueva_venta_precios(self, precios, nuevos=()):
        """19.0.1.129.59: the prices administración typed in «Nueva venta»
        ({product.template id: unit price in the unit of sale}) on this
        order's lines, before it is confirmed, packed or invoiced, and a
        chatter note with each overridden line (list price → new price) and
        the products created on the spot (`nuevos`, template ids). Written
        after the lines exist, so Odoo keeps them as manual prices
        (technical_price_unit stays the list's)."""
        self.ensure_one()
        if not precios and not nuevos:
            return
        user = self.env.user
        cambios, creados = [], []
        lineas = self.order_line.filtered(lambda l: not l.display_type)
        for linea in lineas:
            plantilla = linea.product_id.product_tmpl_id
            precio = precios.get(plantilla.id)
            if precio is None:
                continue
            antes = linea.price_unit
            moneda = linea.currency_id or self.currency_id
            if plantilla.id in nuevos:
                creados.append(escape("%s a %s" % (plantilla.name, _plata(precio))))
            elif moneda.compare_amounts(antes, precio) != 0:
                cambios.append(escape("%s: %s → %s" % (plantilla.name, _plata(antes),
                                                       _plata(precio))))
            if moneda.compare_amounts(antes, precio) != 0 or plantilla.id in nuevos:
                linea.sudo().write({"price_unit": precio, "distri_precio_manual": True})
        partes = []
        if cambios:
            partes.append(Markup("Precios cambiados por %s en «Nueva venta» (precio de lista → "
                                 "precio puesto):<br/>%s") % (user.name, Markup("<br/>").join(cambios)))
        if creados:
            partes.append(Markup("Productos creados en «Nueva venta», vendidos por %s:<br/>%s") % (
                user.name, Markup("<br/>").join(creados)))
        if partes:
            self.message_post(body=Markup("<br/><br/>").join(partes))

    # 19.0.1.129.30: «Ya entregado» of «Nueva venta» (who and when it was
    # marked are distri_entregado_fuera_por_id / _el, 19.0.1.129.25)
    distri_entregado_fuera_fecha = fields.Date(
        string="Entregado fuera del sistema el día", readonly=True, copy=False,
        help="El día en que la mercadería se entregó («Ya entregado» de «Nueva venta»).")
    distri_entregado_fuera_motivo = fields.Char(
        string="Motivo de la entrega fuera del sistema", readonly=True, copy=False)

    @api.model
    def _distri_nueva_venta_presupuesto(self, partner, renglones):
        """A quotation as «Ventas › Presupuesto» makes it, as the current
        user: the customer, its price list and salesperson by Odoo's own
        defaults, each line in the catalog's unit of sale and priced by the
        list. `renglones` = [(product.template, quantity)]."""
        lineas = []
        for plantilla, cantidad in renglones:
            lineas.append((0, 0, {
                "product_id": plantilla.product_variant_id.id,
                "product_uom_qty": float(cantidad),
                "product_uom_id": unidad_de_venta(plantilla).id,
            }))
        return self.create({"partner_id": partner.id, "order_line": lineas})

    def _distri_nueva_venta_mostrador(self):
        """«Mostrador»: the depot page's «Venta en mostrador» (controllers/
        deposito.py mostrador). Returns the delivery wizard's action."""
        self.ensure_one()
        action = self.action_distri_counter_sale()
        if (action or {}).get("res_model") != "distri.depot.delivery":
            raise UserError("No se pudo hacer la venta en mostrador: falta el WhatsApp del "
                            "cliente. Cargalo en el contacto y probá de nuevo.")
        return action

    @api.model
    def _distri_fecha_entregado(self, fecha):
        """The day of «Ya entregado»: a date, today or before (owner: any
        earlier date)."""
        from .reparto_cliente import today_ar
        if not fecha:
            raise UserError("Poné el día en que se entregó.")
        try:
            dia = fields.Date.to_date(fecha)
        except (TypeError, ValueError):
            raise UserError("Revisá el día en que se entregó.") from None
        if not dia:
            raise UserError("Poné el día en que se entregó.")
        if dia > today_ar():
            raise UserError("El día de la entrega no puede ser posterior a hoy.")
        return dia

    def _distri_nueva_venta_ya_entregado(self, fecha, motivo):
        """«Ya entregado»: confirm, pack as ordered (the packer's steps, as
        «Venta en mostrador»: remito validated, invoice and receipt code as
        usual), then delivered outside the system on `fecha` with `motivo`.
        No payment, collection or charge is created."""
        self.ensure_one()
        _check_admin(self.env)
        dia = self._distri_fecha_entregado(fecha)
        motivo = " ".join((motivo or "").split())[:250]
        if not motivo:
            raise UserError("Poné el motivo de la entrega fuera del sistema.")
        if self.state not in ("draft", "sent"):
            raise UserError("«Ya entregado» sale de un presupuesto sin confirmar.")
        if self._distri_sin_cliente():
            raise UserError("Asigná el cliente real antes de cargar la venta.")
        if not self.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty):
            raise UserError("La venta no tiene productos.")
        result = self.action_confirm()
        if isinstance(result, dict):
            raise UserError("No se pudo confirmar la venta: falta el WhatsApp del cliente.")
        error = self.distri_tomar() or self.distri_terminar(1)
        if error:
            raise UserError(error)
        if self.distri_factura_error:
            raise UserError("No salió la factura: %s" % self.distri_factura_error)
        # 19.0.1.129.32: invoicing at the end of packing may do nothing
        # without an error (held order, nothing to invoice): never marked
        # delivered without its invoice and its remito (everything rolls back)
        if not self.sudo().invoice_ids.filtered(lambda m: m.state != "cancel"):
            raise UserError("No salió la factura de la venta: no se cargó como entregada. "
                            "Revisala en Odoo.")
        if not self.sudo().picking_ids.filtered(lambda p: p.state == "done"):
            raise UserError("No quedó hecho el remito de la venta: no se cargó como entregada. "
                            "Revisala en Odoo.")
        user = self.env.user
        self.sudo().write({
            "distri_entregado_fuera": True,
            "distri_entregado_fuera_por_id": user.id,
            "distri_entregado_fuera_el": fields.Datetime.now(),
            "distri_entregado_fuera_fecha": dia,
            "distri_entregado_fuera_motivo": motivo,
        })
        self._distri_fechar_remito(dia)
        self.message_post(body="Venta cargada como «Ya entregado» por %s: entregada el %s. "
                               "Motivo: %s." % (user.name, dia.strftime("%d/%m/%Y"), motivo))
        return True

    def _distri_fechar_remito(self, dia):
        """The validated remito of «Ya entregado» carries the day of the
        delivery (noon, Argentina) instead of the moment it was loaded.
        Only the dates of the stock documents change: quantities, invoice
        and journal entries stay as they are."""
        from .reparto_cliente import today_ar
        if dia >= today_ar():
            return
        momento = AR_TZ.localize(datetime.combine(dia, MEDIODIA)).astimezone(
            pytz.utc).replace(tzinfo=None)
        pickings = self.sudo().picking_ids.filtered(lambda p: p.state == "done")
        # 19.0.1.129.32: the remito keeps a trace of the date it really had
        for picking in pickings:
            picking.message_post(body="Fecha del remito cambiada del %s al %s por «Ya entregado» "
                                      "(%s)." % (
                fields.Datetime.context_timestamp(picking, picking.date_done).strftime(
                    "%d/%m/%Y %H:%M") if picking.date_done else "-",
                dia.strftime("%d/%m/%Y"), self.env.user.name))
        pickings.write({"date_done": momento})
        pickings.move_ids.filtered(lambda m: m.state == "done").write({"date": momento})
        pickings.move_line_ids.write({"date": momento})

    # ------------------------------------------------------------ 19.0.1.129.35

    # #56: who placed the order at the shop («Nombre y apellido»), typed in
    # «Nueva venta»; the customer stays the company
    distri_contacto_nombre = fields.Char(
        string="Pidió", readonly=True, copy=False,
        help="Nombre y apellido de quien hizo el pedido en el comercio («Nueva venta»).")

    def _distri_nueva_venta_extras(self, contacto, lat, lng, accuracy):
        """#56: the name of who ordered and, when the seller tapped
        «Registrar la ubicación», the shop's GPS. Returns the warnings (a
        location that cannot be used is not an error: the sale stands)."""
        self.ensure_one()
        avisos = []
        contacto = " ".join((contacto or "").split())[:120]
        if contacto:
            self.sudo().distri_contacto_nombre = contacto
        if lat or lng:
            aviso = self._distri_nueva_venta_ubicacion(lat, lng, accuracy)
            if aviso:
                avisos.append(aviso)
        return avisos

    def _distri_nueva_venta_ubicacion(self, lat, lng, accuracy):
        """The phone's GPS at the shop, read by the seller. The customer's
        location is set only when he has none (the driver's or the one the
        customer sent are never replaced); otherwise it goes to the chatter
        to compare. Returns "" or why it was not used."""
        import math

        from .datos_entrega import MAX_ACCURACY_M, maps_url
        self.ensure_one()
        try:
            lat, lng, accuracy = float(lat), float(lng), float(accuracy or 0)
        except (TypeError, ValueError):
            return "No se pudo leer la ubicación: no se guardó."
        if not all(math.isfinite(v) for v in (lat, lng, accuracy)) or \
                not (-90 <= lat <= 90 and -180 <= lng <= 180) or (lat == 0 and lng == 0) \
                or accuracy <= 0:
            return "No se pudo leer la ubicación: no se guardó."
        if accuracy > MAX_ACCURACY_M:
            return "La ubicación no era precisa (±%d m): no se guardó." % round(accuracy)
        order = self.sudo()
        contact = order.partner_shipping_id or order.partner_id
        if contact.distri_generic_partner:
            return ""
        url = maps_url(lat, lng)
        main = not contact._distri_has_location()
        if main:
            contact.write({"partner_latitude": lat, "partner_longitude": lng,
                           "distri_location_user_id": self.env.user.id,
                           "distri_location_date": fields.Datetime.now()})
        from markupsafe import Markup
        contact.message_post(body=Markup(
            '%s registró la ubicación del comercio en «Nueva venta» (%s, ±%d m): '
            '<a href="%s" target="_blank" rel="noopener">ver en el mapa</a>. %s') % (
            self.env.user.name, order.name, round(accuracy), url,
            "No tenía ubicación: queda como la principal." if main else
            "La ubicación que ya tenía no se cambió: revisala con esta."))
        return ""

    def _distri_kit_bloqueo_aprobar(self):
        """Why this quotation cannot be approved from the kit now, or "".
        The same rules as /agilpedido's «Aprobar el pedido» and the
        backend's «Confirmar» (held, waiting for the WhatsApp, AgilPedido
        lines to correct, generic customer)."""
        self.ensure_one()
        o = self.sudo()
        if o.state not in ("draft", "sent"):
            return "El pedido ya está %s." % ("aprobado" if o.state == "sale" else "cancelado")
        if o.partner_id.distri_generic_partner:
            return "Asigná el cliente para aprobarlo."
        if o.distri_agil_por_corregir:
            return ("Tiene %s renglones de AgilPedido para corregir: corregilos antes de "
                    "aprobarlo." % o.distri_agil_por_corregir)
        if o.distri_retenido:
            return "Está retenido: liberalo antes de aprobarlo."
        if o.distri_wa_state == "waiting":
            return ("Espera el WhatsApp del cliente: confirmalo en «Pedidos sin WhatsApp» del "
                    "sistema.")
        return ""

    def _distri_kit_pide_whatsapp(self):
        """The approval asks the customer's WhatsApp (the backend wizard
        would open): models/pedido_whatsapp.py _distri_wa_asks_on_confirm."""
        self.ensure_one()
        return self.sudo()._distri_wa_asks_on_confirm()

    def distri_kit_aprobar(self, area="", numero="", sin_whatsapp=False):
        """«Aprobar» on the order screen (/pedidos/<id>), as the current user
        with his own rights: the backend's «Confirmar» (action_confirm with
        distri_wa_ask). When that would ask the customer's WhatsApp, the
        backend's own wizard (distri.wa.confirm.wizard) takes the number the
        screen asked («Guardar y confirmar») or, while the WhatsApp is not
        required, «Confirmar sin WhatsApp»."""
        self.ensure_one()
        self.check_access("write")
        # 19.0.1.129.39: a seller only on his own or his customers' orders
        # (the standard rule lets him write the ones without salesperson)
        if not self.env.su and not self.env.user._distri_may_reach_record(self):
            raise AccessError("Ese pedido no es de tus clientes.")
        bloqueo = self._distri_kit_bloqueo_aprobar()
        if bloqueo:
            raise UserError("No se aprobó: %s" % bloqueo)
        # 129.49 hotfix (audit of 26/11/2026: 7 refusals with no clear
        # reason): every refusal says what is missing and how to go on
        requerido = self.env["distri.pedido.whatsapp.settings"].requires_whatsapp()
        if self._distri_kit_pide_whatsapp():
            if not (area or numero) and not sin_whatsapp:
                raise UserError(falta_whatsapp_texto(requerido))
            if sin_whatsapp and requerido and not (area or numero):
                raise UserError(falta_whatsapp_texto(True))
            wizard = self.env["distri.wa.confirm.wizard"].create({
                "order_id": self.id, "then": "confirm",
                "area": area or False, "number": numero or False})
            if area or numero:
                try:
                    wizard._canonical()
                except UserError as exc:
                    raise UserError(WHATSAPP_INVALIDO % (
                        " ".join(x for x in (area, numero) if x),
                        exc.args[0] if exc.args else exc)) from None
                wizard.action_save_confirm()
            else:
                wizard.action_skip()
        else:
            result = self.with_context(distri_wa_ask=True).action_confirm()
            if isinstance(result, dict) and self.state != "sale":
                raise UserError(falta_whatsapp_texto(requerido))
        if self.state != "sale":
            raise UserError(NO_SE_APROBO % self.name)
        self.message_post(body="Aprobado por %s desde la pantalla del pedido." % self.env.user.name)
        return True


# 129.49 hotfix: «Aprobar» on the order screen (distri_kit_aprobar)
FALTA_WHATSAPP = ("No se aprobó. Falta el WhatsApp del cliente: escribí el código de área "
                  "(sin el 0) y el número (sin el 15) en «El cliente no tiene WhatsApp "
                  "cargado» y tocá «Aprobar» otra vez.")
FALTA_WHATSAPP_OPCIONAL = (" Si el cliente no tiene WhatsApp, tocá «Aprobar sin WhatsApp».")
FALTA_WHATSAPP_REQUERIDO = (" Hace falta para confirmar el pedido: sin el WhatsApp no se "
                            "puede aprobar.")
WHATSAPP_INVALIDO = ("No se aprobó: el WhatsApp «%s» no es válido (%s). Escribí el código de "
                     "área sin el 0 y el número sin el 15, por ejemplo 299 y 4222444, y tocá "
                     "«Aprobar» otra vez.")
NO_SE_APROBO = ("No se aprobó el pedido %s: sigue como presupuesto. Recargá la pantalla y "
                "probá otra vez; si vuelve a pasar, avisá a Sistemas.")


def falta_whatsapp_texto(requerido):
    return FALTA_WHATSAPP + (FALTA_WHATSAPP_REQUERIDO if requerido else FALTA_WHATSAPP_OPCIONAL)


def guardar_whatsapp_nueva_venta(env, cliente, area, numero, order):
    """19.0.1.129.35: the answer to «¿Cuál es el WhatsApp del cliente?» of
    «Nueva venta», stored as the backend wizard stores it («Guardar y
    confirmar»: +549 + 10 digits on the customer, with the chatter note).
    Raises UserError with the reason when it cannot be read."""
    from .ar_phone import ar_mobile
    try:
        canonical = ar_mobile(area, numero)
    except ValueError as error:
        raise UserError(str(error)) from None
    partner = cliente.sudo().commercial_partner_id
    partner.write({"distri_whatsapp": canonical})
    partner.message_post(body="WhatsApp cargado por %s en «Nueva venta» (%s): %s." % (
        env.user.name, order.name if order else "-", canonical))
    return canonical
