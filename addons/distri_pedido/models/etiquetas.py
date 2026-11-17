"""Package labels (19.0.1.33.0).

One label per package of a packed order, for the depot's thermal printer
(Nexuspos Z-NX 461/418, 203 dpi, rolls of 100 x 50 mm). The PDF has one page
per package, exactly 100 x 50 mm with no margins, and is printed from the
depot PC's browser through the Windows driver («tamaño real», no scaling).

The package count is the one the packer gives when finishing
(`distri_bultos`). Each package gets a small record (`distri.package`) with a
unique code («S00231-02») that the label carries as a Code128 barcode, to be
scanned later at the departure control. The departure control itself still
counts packages per order (despacho.py): nothing changes there.

Never a price or an amount on a label. Direct TSPL printing is a documented
future option, not built (docs: armado.md «Etiquetas de bultos»).
"""
import base64
import re

from odoo import api, fields, models
from odoo.exceptions import UserError

from .encomienda import PAYER_LABELS
from .reparto_cliente import TOWN_PREFIXES, town_from_name

LABEL_REPORT = "distri_pedido.report_etiquetas_bultos"
TEST_REPORT = "distri_pedido.report_etiqueta_prueba"
PREFIX_OF_TOWN = {town: prefix for prefix, town in TOWN_PREFIXES.items()}
# 19.0.1.94.0: the company's public phone on every label (never in the code)
PARAM_PHONE = "distri_pedido.telefono_empresa"


def format_phone(raw):
    """2995797248 -> «299 579-7248» (area code + local number); anything
    that is not 10 digits is shown as it was loaded."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return "%s %s-%s" % (digits[:3], digits[3:6], digits[6:])
    return (raw or "").strip()


def big_number_size(text):
    """Font size (pt) of the «3/5» block, 32 mm wide: as big as fits."""
    return {1: 34, 2: 34, 3: 34, 4: 30}.get(len(text), 25)


class DistriPackage(models.Model):
    _name = "distri.package"
    _description = "Bulto de un pedido"
    _order = "order_id, number"
    _rec_name = "code"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, ondelete="cascade",
                               index=True)
    number = fields.Integer(string="Bulto", required=True)
    code = fields.Char(string="Código", required=True, index=True, readonly=True)
    active = fields.Boolean(default=True)
    printed_count = fields.Integer(string="Veces impreso", readonly=True)
    last_printed_at = fields.Datetime(string="Última impresión", readonly=True)

    _code_unique = models.Constraint("unique(code)", "Ese código de bulto ya existe.")

    @api.model
    def _code_for(self, order, number):
        return "%s-%02d" % (order.name, number)

    def _label_values(self):
        """Everything one label shows. No prices, no amounts."""
        self.ensure_one()
        order = self.order_id.sudo()
        partner = order.partner_id
        generic = partner.distri_generic_partner
        name = (order.distri_nombre or partner.name) if generic else partner._distri_app_name()
        town = partner._distri_town() if not generic else (
            order.distri_zona_id.name or "")
        town = town or town_from_name(partner.name) or ""
        prefix = PREFIX_OF_TOWN.get(town, "")
        total = order.distri_bultos or len(order.distri_package_ids) or 1
        stop = order.distri_stop_ids.filtered(
            lambda s: not (s.state == "not_delivered" and s.unloaded)).sorted("id")[-1:]
        if order.distri_encomienda:
            payer = PAYER_LABELS.get(order.distri_envio_paga or "distri", "")
            destination = "Encomienda a %s" % (order._distri_parcel_town() or town)
            detail = payer
        elif order.distri_entrega == "retira":
            destination, detail = "Retira en depósito", ""
        elif stop:
            route = stop.route_id
            destination = "Reparto · %s" % (route.driver_id.name or "").split(" ")[0]
            detail = "Viaje %s · %s" % (route.trip or "1", stop.zone_id.name or town)
        else:
            destination, detail = "Reparto", order.distri_zona_id.name or town
        when = stop.route_id.date if stop else (
            order.commitment_date.date() if order.commitment_date else False)
        # 19.0.1.94.0: the store's address (the order's delivery address);
        # a web order without a customer yet carries the address it typed
        ship = order.partner_shipping_id or partner
        if generic:
            street, locality = order.distri_domicilio or "", town
        else:
            street = ", ".join(p.strip() for p in (ship.street, ship.street2) if p and p.strip())
            locality = (ship.city or "").strip() or town
        number_text = "%s/%s" % (self.number, total)
        from ..controllers.main import FICHA
        phone = format_phone(self.env["ir.config_parameter"].sudo().get_param(PARAM_PHONE))
        return {
            "name": name or "",
            "street": street,
            "locality": locality,
            "company": " · ".join(x for x in (FICHA["nombre"], phone) if x),
            "number_text": number_text,
            "number_size": big_number_size(number_text),
            "town": town,
            "prefix": prefix,
            "order": order.name,
            "remito": order._distri_remitos_texto() or "",
            "number": self.number,
            "total": total,
            "destination": destination,
            "detail": detail or "",
            "date": when.strftime("%d/%m/%Y") if when else "a confirmar",
            "code": self.code,
            "barcode": self._barcode_png(),
        }

    def _barcode_png(self):
        """Code128 of the package code as a base64 PNG, embedded in the
        page (no HTTP fetch from the PDF engine)."""
        self.ensure_one()
        png = self.env["ir.actions.report"].barcode(
            "Code128", self.code, width=800, height=140, humanreadable=0, quiet=0)
        return base64.b64encode(png).decode()

    def _mark_printed(self):
        now = fields.Datetime.now()
        for package in self.sudo():
            package.write({"printed_count": package.printed_count + 1, "last_printed_at": now})


class SaleOrderPackages(models.Model):
    _inherit = "sale.order"

    distri_package_ids = fields.One2many("distri.package", "order_id", string="Bultos (etiquetas)")

    def _distri_packages(self):
        """The packages 1..N of the packed order (N = distri_bultos): the
        missing ones are created, the extra ones archived. Returns them."""
        Package = self.env["distri.package"].sudo().with_context(active_test=False)
        result = Package.browse()
        for order in self.sudo():
            if order.distri_armado != "armado" or not order.distri_bultos:
                raise UserError("%s todavía no está armado con sus bultos: las etiquetas salen "
                                "al terminar el armado." % order.name)
            existing = Package.search([("order_id", "=", order.id)])
            by_number = {p.number: p for p in existing}
            for number in range(1, order.distri_bultos + 1):
                package = by_number.get(number)
                if not package:
                    package = Package.create({"order_id": order.id, "number": number,
                                              "code": Package._code_for(order, number)})
                elif not package.active:
                    package.active = True
                result |= package
            existing.filtered(lambda p: p.number > order.distri_bultos and p.active).write(
                {"active": False})
        return result.with_context(active_test=True)

    def action_distri_print_labels(self):
        """«Imprimir etiquetas» (order form): one page per package."""
        packages = self._distri_packages()
        packages._mark_printed()
        return self.env.ref(LABEL_REPORT).report_action(packages, config=False)


class DistriRoutePackages(models.Model):
    _inherit = "distri.route"

    def action_distri_print_labels(self):
        """«Imprimir etiquetas» of the whole trip (Salida de viajes)."""
        self.ensure_one()
        orders = self.stop_ids.filtered(
            lambda s: not (s.state == "not_delivered" and s.unloaded)).order_id.filtered(
            lambda o: o.distri_armado == "armado" and o.distri_bultos)
        if not orders:
            raise UserError("La ruta no tiene pedidos armados con bultos.")
        packages = orders._distri_packages()
        packages._mark_printed()
        return self.env.ref(LABEL_REPORT).report_action(packages, config=False)


class ReportPackageLabels(models.AbstractModel):
    _name = "report.distri_pedido.etiquetas_bultos_documento"
    _description = "Etiquetas de bultos"

    @api.model
    def _get_report_values(self, docids, data=None):
        packages = self.env["distri.package"].sudo().browse(docids)
        return {"doc_ids": docids, "doc_model": "distri.package", "docs": packages,
                "labels": [p._label_values() for p in packages]}
