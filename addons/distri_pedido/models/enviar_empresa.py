"""19.0.1.129.5: the order's WhatsApp text and the «Enviar a la empresa» step.

The company number (distri_pedido.whatsapp_empresa) is a plain phone, not on
the Cloud API: the server cannot send anything to it by itself. Every order
source therefore offers a wa.me link to that number with the text below:

- the customer's own page order («Enviar por WhatsApp», controllers/main.py);
- the seller's order (/pedido as staff): «Enviar a la empresa» next to
  «Enviar al cliente», shown as a pending step until it is tapped;
- the backend order form: a header button.

Tapping any of them sets `distri_enviado_empresa` (and its date), so the flag
means the same everywhere; «Pedidos para revisar» filters the ones still
missing. The text (`_distri_company_message`) reads:

    *Cliente:* Kiosco Ana
    *Dirección:* Mitre 1
    *Teléfono:* 299 …

    - 2 x Producto ($ 2.000)

    *Total: $ 2.000*

    *Comentario:* …

    <stock notice>

    Pedido S00224 · código 8BMH
    Tu pedido (link personal, no lo compartas):
    https://…/p/<token>

19.0.1.129.40 (owner): the number appears once. «*Pedido:* S00224» goes at
the top only when there is no code line at the bottom (no receipt code yet).

The code line and the /p/ link keep the webhook matching
(models/pedido_whatsapp.py find_web_codes) and keep the AgilPedido
auto-import away from it (models/agilpedido_wa.py es_mensaje_de_la_pagina).
"""
import json

from odoo import fields, models
from odoo.exceptions import UserError

from . import ar_phone
from .agilpedido_wa import ORIGEN_AGILPEDIDO_WA

WHOLESALE_WARNING = "⚠️ *LISTA DISTRIBUIDORAS*"
UNPRICED_NOTE = ("(los renglones a confirmar se valorizan con tu lista y te "
                 "confirmamos el total)")
MODIFIED_MARK = "_modificado por el cliente_"


def money(amount):
    """«$ 4.240», «$ 4.980.000»: no cents, always the thousands dot."""
    return "$ %s" % "{:,.0f}".format(amount or 0).replace(",", ".")


class SaleOrderEnviarEmpresa(models.Model):
    _inherit = "sale.order"

    distri_enviado_empresa = fields.Boolean(
        string="Enviado a la empresa", copy=False, readonly=True, index=True,
        help="Se tocó «Enviar a la empresa» (o «Enviar por WhatsApp» en la página del "
             "cliente): se abrió el WhatsApp de la empresa con el pedido escrito.")
    distri_enviado_empresa_el = fields.Datetime(
        string="Enviado a la empresa el", copy=False, readonly=True)

    # ------------------------------------------------------------ the text

    def _distri_company_message(self, nombre, telefono, domicilio, entrega="domicilio",
                                mayorista=False, link_label=None):
        """The order as text for the company's WhatsApp, one blank line
        between blocks."""
        self.ensure_one()
        order = self.sudo()
        # 19.0.1.129.40 (owner): the number once; the top «*Pedido:*» line
        # only when the bottom «Pedido … · código …» line is not there
        bloques = [order._distri_company_fields(nombre, telefono, domicilio, entrega,
                                                con_numero=not order.distri_wa_code)]
        if mayorista:
            # it qualifies every price below
            bloques.append([WHOLESALE_WARNING])
        renglones, sin_precio = order._distri_company_lines()
        if renglones:
            bloques.append(renglones)
        bloques.append(order._distri_company_total(sin_precio))
        comentario = (order.distri_comentario or "").strip()
        if comentario:
            bloques.append(["*Comentario:* %s" % comentario])
        bloques.append([order._distri_stock_notice()])
        cola = []
        if order.distri_wa_code:
            # this line stays exactly as is: the webhook matcher reads it
            # (models/pedido_whatsapp.py find_codes, _WEB_CODE_RE)
            cola.append(order._distri_wa_code_line())
        cola.extend(order._distri_link_line(link_label).split("\n"))
        bloques.append(cola)
        return "\n\n".join("\n".join(bloque) for bloque in bloques)

    def _distri_company_fields(self, nombre, telefono, domicilio, entrega, con_numero=True):
        lineas = [MODIFIED_MARK] if self.distri_modificado else []
        # 19.0.1.129.6: the order number first (owner); 19.0.1.129.40: not
        # when the code line at the bottom already names it
        if con_numero:
            lineas.append("*Pedido:* %s" % self.name)
        lineas.append("*Cliente:* %s" % ((nombre or "").strip() or "-"))
        if self.distri_encomienda_pedida:
            lineas.append("*Encomienda:* terminal de %s" % (
                self.distri_encomienda_localidad or "-"))
            lineas.append("*Retira:* %s, DNI %s" % (
                self.distri_encomienda_nombre or "-", self.distri_encomienda_dni or "-"))
        elif entrega == "retira":
            lineas.append("*Retira:* En el depósito")
        else:
            lineas.append("*Dirección:* %s" % ((domicilio or "").strip() or "-"))
        if (telefono or "").strip():
            lineas.append("*Teléfono:* %s" % telefono.strip())
        return lineas

    def _distri_company_lines(self):
        """(product lines, whether any line has no price yet)."""
        lineas, sin_precio = [], False
        for linea in self.order_line:
            if linea.display_type:
                continue
            uom = linea.product_uom_id
            # the unit only when it is a pack («Bulto x 12»), as in 19.0.1.129.3
            detalle = " · %s" % (uom.name or "") if uom.relative_factor > 1 else ""
            if linea.price_unit:
                importe = money(linea.price_subtotal)
            else:
                importe = "$ a confirmar"
                sin_precio = True
            lineas.append("- %s x %s%s (%s)" % (
                int(linea.product_uom_qty), linea.product_id.name, detalle, importe))
        return lineas, sin_precio

    def _distri_company_total(self, sin_precio):
        if sin_precio and not self.amount_total:
            total = "*Total: a confirmar*"
        elif sin_precio:
            total = "*Total: %s + a confirmar*" % money(self.amount_total)
        else:
            total = "*Total: %s*" % money(self.amount_total)
        return [total, UNPRICED_NOTE] if sin_precio else [total]

    # ------------------------------------------------- the text, from the order

    def _distri_company_message_default(self, link_label=None):
        """The text built from the order alone: the backend button, the
        seller's confirmation and the customer's order page."""
        self.ensure_one()
        order = self.sudo()
        socio = order.partner_id
        web = order._distri_nombre_web()
        nombre = web if socio.distri_generic_partner else socio.name
        return order._distri_company_message(
            nombre or web or socio.name,
            order._distri_company_phone(), order._distri_company_address(),
            order.distri_entrega or "domicilio", mayorista=order._distri_company_wholesale(),
            link_label=link_label)

    def _distri_company_address(self):
        """The typed address, else the street on file of a real customer."""
        socio = self.partner_id
        if self.distri_domicilio or socio.distri_generic_partner:
            return self.distri_domicilio or ""
        return socio.street or socio.commercial_partner_id.street or ""

    def _distri_company_phone(self):
        """The order's phone as people read it («+54 9 299 412-3456»)."""
        socio = self.partner_id
        valor = self.distri_telefono or ("" if socio.distri_generic_partner else (
            socio._distri_whatsapp_shown()
            or socio.commercial_partner_id._distri_whatsapp_shown()))
        nacional = ar_phone.ar_national(valor)
        return ar_phone.format_ar_national(nacional) if nacional else (valor or "")

    def _distri_company_wholesale(self):
        """Wholesale prices: a line of the DISTRIBUIDORAS section (the same
        rule as when saving the order), the wholesale generic contact or a
        hidden list (models/lista_oculta.py)."""
        try:
            original = json.loads(self.distri_original or "[]")
        except ValueError:
            original = []
        return bool(any(isinstance(r, dict) and r.get("may") for r in original)
                    or self.partner_id.distri_generic_kind == "wholesale"
                    or self._distri_de_distribuidora())

    def _distri_company_url(self, text=None):
        self.ensure_one()
        if text is None:
            text = self._distri_company_message_default()
        return self.env["distri.pedido.whatsapp.settings"].sudo().company_url(text)

    # ------------------------------------------------------------ the flag

    def _distri_mark_sent_to_company(self):
        """«Enviar a la empresa» was tapped: the first date stays."""
        for order in self.sudo().filtered(lambda o: not o.distri_enviado_empresa):
            order.write({"distri_enviado_empresa": True,
                         "distri_enviado_empresa_el": fields.Datetime.now()})
        return True

    def _distri_company_send_missing(self):
        """Still to be sent: not cancelled and not an order that already
        came through the company's WhatsApp (AgilPedido auto-import)."""
        self.ensure_one()
        return (not self.distri_enviado_empresa and self.state != "cancel"
                and self.distri_origen != ORIGEN_AGILPEDIDO_WA)

    def action_distri_enviar_empresa(self):
        """Backend «Enviar a la empresa»: opens the company's chat with the
        order's text and marks the order as sent."""
        self.ensure_one()
        if self.state == "cancel":
            raise UserError("El pedido %s está cancelado." % self.name)
        if not self.env["distri.pedido.whatsapp.settings"].sudo().company_number():
            raise UserError("Falta el número de WhatsApp de la empresa: cargalo en "
                            "«WhatsApp de pedidos».")
        url = self._distri_company_url()
        self._distri_mark_sent_to_company()
        return {"type": "ir.actions.act_url", "url": url, "target": "new"}
