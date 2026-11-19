"""«Hoja de ruta y pagos» and «Remitos» (19.0.1.110.0, owner-approved designs
of 18/11/2026): QWeb PDF reports in views/hoja_ruta.xml.

- Hoja de ruta: one A4 per route (more pages only with many stops): the
  stops in visit order with what to collect (the remito plus what the
  customer owed before) and blank columns Código, Hora, Pagado and Medio
  that the driver fills by hand when the phone fails. The receipt code is
  NEVER printed (the customer dictates it), nor the departure time, nor
  signature lines.
- Remitos: the delivered picking (R-NNNNN, models/remito.py) of each order
  of the route, or of one order: what was packed, its prices, the previous
  debt and the total to pay. Never titled «factura» (owner's rule of
  26/09/2026: the customer never sees the invoice), never the receipt code.

Both print the transfer alias big (customers always ask the driver for it):
distri_pedido.alias_transferencia, the one the order page shows, and its
holder distri_pedido.alias_titular (data/parametros.xml).

wkhtmltopdf (old QtWebKit) renders them: tables and floats only, no CSS
variables, flex or grid.
"""
from zoneinfo import ZoneInfo

from odoo import api, models
from odoo.exceptions import AccessError

from .ar_phone import ar_national, format_local, split_ar_national
from .despacho import AR_TZ, first_name
from .planificacion import day_text, parcels_text
from .remito import numero_de_remito
from .reparto import _money
from .reparto_cliente import today_ar

PARAM_ALIAS = "distri_pedido.alias_transferencia"  # the key of controllers/main.py
PARAM_HOLDER = "distri_pedido.alias_titular"
PARAM_PHONE = "distri_pedido.telefono_empresa"
PARAM_WHATSAPP = "distri_pedido.whatsapp_empresa"
DEFAULT_ALIAS = "sepiko"
DEFAULT_HOLDER = "Gonzalo Granero"
COMPANY = "Distri Neuquén"
UTC = ZoneInfo("UTC")
# 19.0.1.115.0: besides administración/gerencia and the route's driver
REMITO_GROUPS = ("distri_pedido.grupo_cajero", "distri_pedido.grupo_armador")
REMITO_DENIED = ("El remito lo imprimen administración, el cajero, el armador o el repartidor "
                 "de la ruta.")

# «Otros cobros»: blank rows that fill the rest of the first page (measured
# on the real PDF): about BLANK_ROWS_PAGE fit under the fixed parts, and a
# stop (customer, address and notes on two or three lines) takes about
# STOP_ROW_WEIGHT of them. A long route gets the minimum and more pages.
BLANK_ROWS_PAGE = 22
BLANK_ROWS_MIN = 3
STOP_ROW_WEIGHT = 2.5


def phone_text(value):
    """«299 579-7248» for any way of writing an Argentine phone."""
    national = ar_national(value)
    if not national:
        return (value or "").strip()
    area, number = split_ar_national(national)
    return "%s %s" % (area, format_local(number))


def transfer_account(env):
    get = env["ir.config_parameter"].sudo().get_param
    return {
        "alias": (get(PARAM_ALIAS) or "").strip() or DEFAULT_ALIAS,
        "holder": "%s · %s" % ((get(PARAM_HOLDER) or "").strip() or DEFAULT_HOLDER, COMPANY),
    }


def company_lines(env):
    get = env["ir.config_parameter"].sudo().get_param
    phone = phone_text(get(PARAM_PHONE) or "")
    whatsapp = phone_text(get(PARAM_WHATSAPP) or "")
    return {
        "name": COMPANY,
        "sheet": " · ".join(x for x in (phone, "WhatsApp %s" % whatsapp if whatsapp else "") if x),
        "remito": " · ".join(x for x in ("Distribuidora mayorista", "Neuquén", phone) if x),
    }


def _stop_amounts(stop):
    """(remito, previous debt): the snapshot the phone got once the stop
    was visited; for a pending stop the values it would download now
    (stop._refresh_amounts), computed without writing."""
    if stop.state != "pending":
        return stop.order_amount, stop.previous_debt
    order = stop.order_id.sudo()
    return order._distri_amount_to_collect(), order._distri_previous_debt()


def _shipping(order):
    order = order.sudo()
    partner = order.partner_id
    ship = order.partner_shipping_id or partner
    return partner, ship


def _address(order):
    partner, ship = _shipping(order)
    if order.sudo().distri_encomienda:
        return "Encomienda · %s" % (order.sudo()._distri_parcel_town() or "")
    parts = [p for p in (ship.street, ship.street2) if p]
    town = ship.city or partner._distri_town()
    if town and town not in parts:
        parts.append(town)
    return ", ".join(parts)


def _notes(order):
    """What the driver must know: when the customer receives, the notes of
    the address, the notice from the order link, the customer's comment and
    the depot note. Never the receipt code."""
    order = order.sudo()
    partner, ship = _shipping(order)
    raw = [ship.distri_delivery_text, ship.distri_driver_note, order.distri_customer_notice_text,
           order.distri_comentario, order.distri_nota_deposito]
    out = []
    for text in raw:
        text = " ".join(str(text or "").split())
        if text and text not in out:
            out.append(text)
    return out


def _phone(order):
    partner, ship = _shipping(order)
    return phone_text(partner._distri_whatsapp_shown() or ship.phone or "")


class DistriRouteSheet(models.Model):
    _inherit = "distri.route"

    def _sheet_values(self):
        """The «Hoja de ruta y pagos» of one route."""
        self.ensure_one()
        rows = []
        total = 0.0
        for index, stop in enumerate(self.stop_ids, start=1):
            order = stop.order_id.sudo()
            remito, debt = _stop_amounts(stop)
            amount = remito + debt
            total += amount
            breakdown = ""
            if debt > 0.004:
                breakdown = "remito %s + debía %s" % (_money(remito), _money(debt))
            elif debt < -0.004:
                breakdown = "remito %s − saldo a favor %s" % (_money(remito), _money(-debt))
            detail = [_address(order)] + _notes(order) + [_phone(order)]
            rows.append({
                "n": index,
                "client": stop.partner_id._distri_app_name(),
                "detail": " · ".join(x for x in detail if x),
                "order": order.name,
                "remito": order._distri_remitos_texto(),
                "parcels": stop.parcels,
                "amount": _money(amount),
                "breakdown": breakdown,
            })
        dispatcher = self._planning_dispatcher()
        return {
            "route": self,
            "title": self._planning_title(),
            "printed": today_ar().strftime("%d/%m/%Y"),
            "driver": self.driver_id.name or "",
            "date": day_text(self.date),
            "zones": ", ".join(self._planning_zones().mapped("name")),
            "parcels": self.parcel_count,
            "dispatcher": first_name(dispatcher) if dispatcher else "",
            "rows": rows,
            "total": _money(total),
            "blank_rows": max(BLANK_ROWS_MIN, int(BLANK_ROWS_PAGE - STOP_ROW_WEIGHT * len(rows))),
            "account": transfer_account(self.env),
            "company": company_lines(self.env),
        }

    def _remito_values(self):
        """The remitos of the route, in visit order."""
        self.ensure_one()
        return [stop.order_id._distri_remito_values(stop) for stop in self.stop_ids]

    # config=False: never the «configure your document layout» wizard, these
    # documents do not use the company layout
    def action_print_route_sheet(self):
        return self.env.ref("distri_pedido.report_hoja_ruta").report_action(self, config=False)

    def action_print_remitos(self):
        self._distri_check_remitos_access()
        return self.env.ref("distri_pedido.report_remitos_ruta").report_action(self, config=False)

    def _distri_check_remitos_access(self):
        """19.0.1.116.0: the route's remitos follow the rule of the single
        remito (SaleOrderRemitoPrint._distri_check_remito_access):
        administración and gerencia, the cashier, the packer and the route's
        driver; a route with a wholesale customer's order, only those who
        see the wholesale customers."""
        from .lista_oculta import ve_lista_oculta
        if self.env.su:
            return
        user = self.env.user
        if user.share:
            raise AccessError(REMITO_DENIED)
        sees_wholesale = ve_lista_oculta(user)
        by_role = sees_wholesale or any(user.has_group(g) for g in REMITO_GROUPS)
        for route in self.sudo():
            if not (by_role or user == route.driver_id):
                raise AccessError(REMITO_DENIED)
            if sees_wholesale:
                continue
            for order in route.stop_ids.order_id:
                if order.partner_id._distri_es_distribuidora() or order._distri_de_distribuidora():
                    raise AccessError("La %s tiene el remito de un cliente mayorista (%s): los "
                                      "remitos de la ruta los imprime administración."
                                      % (route.name, order.name))


class SaleOrderRemitoPrint(models.Model):
    _inherit = "sale.order"

    def _distri_active_stop(self):
        """The order's stop on a route that is not settled (or the last one)."""
        self.ensure_one()
        stops = self.sudo().distri_stop_ids.filtered(lambda s: s.route_id.kind == "route")
        live = stops.filtered(lambda s: s.route_id.state != "settled"
                              and not (s.state == "not_delivered" and s.unloaded))
        return (live or stops).sorted("id")[-1:]

    def _distri_remito_values(self, stop=None):
        """One printed remito: the delivered picking (what was packed)."""
        self.ensure_one()
        order = self.sudo()
        stop = stop if stop is not None else order._distri_active_stop()
        pickings = order._distri_remitos()
        lines = []
        for move in pickings.move_ids.filtered(lambda m: m.state == "done" and m.quantity):
            sale_line = move.sale_line_id
            unit = (sale_line.price_total / sale_line.product_uom_qty) \
                if sale_line and sale_line.product_uom_qty else 0.0
            lines.append({
                "qty": move.quantity,
                "product": move.product_id.with_context(display_default_code=False).display_name,
                "price": unit,
                "subtotal": unit * move.quantity,
            })
        if not pickings:
            # not packed yet: the order as it was asked
            for line in order.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty):
                lines.append({
                    "qty": line.product_uom_qty,
                    "product": line.product_id.with_context(
                        display_default_code=False).display_name,
                    "price": line.price_total / line.product_uom_qty,
                    "subtotal": line.price_total,
                })
        total = sum(l["subtotal"] for l in lines)
        if stop:
            to_collect, debt = _stop_amounts(stop)
        else:
            to_collect, debt = order._distri_amount_to_collect(), order._distri_previous_debt()
        done = pickings.filtered("date_done")[-1:]
        moment = done.date_done or order.date_order
        day = _ar_date(moment) if moment else today_ar()
        partner = order.partner_id
        phone = _phone(order)
        client = [_address(order), "Tel. %s" % phone if phone else ""] + _notes(order)
        return {
            "number": ", ".join(numero_de_remito(p) for p in pickings),
            "order": order.name,
            "date": day.strftime("%d/%m/%Y"),
            "client_name": partner._distri_app_name(),
            "client": " · ".join(x for x in client if x),
            "delivery": order._distri_delivery_line(stop),
            "lines": [dict(l, qty_text=_qty(l["qty"]), price_text=_money(l["price"]),
                           subtotal_text=_money(l["subtotal"])) for l in lines],
            "total": _money(total),
            "debt": _money(debt) if debt >= 0 else "%s a favor" % _money(-debt),
            "paid": _money(total - to_collect) if total - to_collect > 0.004 else "",
            "to_pay": _money(to_collect + debt),
            "account": transfer_account(self.env),
            "company": company_lines(self.env),
        }

    def _distri_delivery_line(self, stop):
        """«Reparto · Neuquén · Ruta 1 · Viaje 1 · Simón · 2 bultos» (never
        the departure time)."""
        self.ensure_one()
        order = self.sudo()
        parcels = parcels_text(order.distri_bultos)
        if order.distri_entrega == "retira":
            return "Retira en el depósito · %s" % parcels
        bits = []
        if order.distri_encomienda:
            bits.append("Encomienda · %s" % (order._distri_parcel_town() or ""))
            if order.distri_carrier_id:
                bits.append(order.distri_carrier_id.name)
        else:
            bits.append("Reparto")
            zone = (stop.zone_id if stop else None) or order._distri_route_zone()
            if zone:
                bits.append(zone.name)
        if stop and stop.route_id.kind == "route":
            route = stop.route_id
            bits.append(route._planning_title())
            if route.driver_id:
                bits.append(route.driver_id.name)
        bits.append(parcels)
        return " · ".join(b for b in bits if b)

    def action_distri_print_remito(self):
        self._distri_check_remito_access()
        return self.env.ref("distri_pedido.report_remito_pedido").report_action(self, config=False)

    def _distri_check_remito_access(self):
        """19.0.1.115.0: who prints an order's remito: administración and
        gerencia, the cashier, the packer and the driver of the order's
        route; a wholesale customer's (lista oculta, «Es mayorista») only
        administración, gerencia and the system administrators."""
        from .lista_oculta import ve_lista_oculta
        if self.env.su:
            return
        user = self.env.user
        if user.share:
            raise AccessError(REMITO_DENIED)
        sees_wholesale = ve_lista_oculta(user)
        by_role = sees_wholesale or any(user.has_group(g) for g in REMITO_GROUPS)
        for order in self.sudo():
            driver = user in order.distri_stop_ids.route_id.driver_id
            if not (by_role or driver):
                raise AccessError(REMITO_DENIED)
            if not sees_wholesale and (order.partner_id._distri_es_distribuidora()
                                       or order._distri_de_distribuidora()):
                raise AccessError("El remito de %s es de un cliente mayorista: lo imprime "
                                  "administración." % order.name)


class ReportRemitoPedido(models.AbstractModel):
    """The order's remito checks who prints it (/report/pdf included)."""
    _name = "report.distri_pedido.remito_pedido_documento"
    _description = "Remito del pedido"

    @api.model
    def _get_report_values(self, docids, data=None):
        orders = self.env["sale.order"].browse(docids)
        orders._distri_check_remito_access()
        return {"doc_ids": docids, "doc_model": "sale.order", "docs": orders.sudo()}


class ReportRemitosRuta(models.AbstractModel):
    """19.0.1.116.0: the route's remitos check who prints them too
    (/report/pdf included)."""
    _name = "report.distri_pedido.remitos_ruta_documento"
    _description = "Remitos de la ruta"

    @api.model
    def _get_report_values(self, docids, data=None):
        routes = self.env["distri.route"].browse(docids)
        routes._distri_check_remitos_access()
        return {"doc_ids": docids, "doc_model": "distri.route", "docs": routes.sudo()}


def _ar_date(moment):
    return moment.replace(tzinfo=UTC).astimezone(AR_TZ).date()


def _qty(value):
    """2 / 1,5 like the app."""
    text = ("%.3f" % value).rstrip("0").rstrip(".")
    return text.replace(".", ",")
