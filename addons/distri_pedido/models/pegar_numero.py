"""«Pegar número» and «Copiar código» (19.0.1.103.0, audit WhatsApp Web
2026-11-18, «Lo que falta» 1 and 3).

Administración reads the customers' chats on the PC (WhatsApp Web or the
Windows app). The number shows in the contact info as «+54 9 299 555-1234»;
typing it again in the two boxes of «WhatsApp del cliente» (area and
number) was the error source the audit wants gone.

- `distri.pegar.numero.mixin`: one «Pegar número» box that takes whatever
  was copied (the contact info, a chat line, a name and a number). It is
  read with the rules of the order page (ar_phone.read_pasted_phone /
  check_ar_phone) and shows «Queda así», a warning when it is not the phone
  the customer typed on the order (or replaces the WhatsApp the customer
  has on file), and the customers that already have that number in any
  format (res.partner._distri_clientes_por_telefono).
- «WhatsApp del cliente» (distri.wa.confirm.wizard) gets the box above the
  old area/number boxes, which keep working: pasting fills them. The same
  dialog opens from «Pegar número» on the order and on «Pedidos para
  revisar» (`pegar_modo`): it stores the number as the customer's verified
  WhatsApp (sale.order._distri_wa_mark_verified, which notes who did it and
  the number it replaces). On an order without customer, «Asignar cliente»
  opens with the pasted number already searched.
- «Asignar cliente» (distri.asignar.cliente) gets the box too: «Buscar este
  número» lists the customers that have it, and assigning stores it as the
  customer's verified WhatsApp (box «Guardar como WhatsApp verificado»).
- «Copiar código»: `distri_wa_code_line` («Pedido S00164 · código 7K3P»,
  the line the customer's message carries) with the `distri_copiar`
  widget (static/src/js/copiar_codigo.js) on the order and on «Pedidos
  para revisar», to search it in WhatsApp Web when the customer wrote from
  another number.

Only pure helpers are imported (ar_phone): this file loads last and adds
fields and methods, it overrides nothing of sale.order.
"""
from odoo import Command, api, fields, models
from odoo.exceptions import UserError

from .ar_phone import (
    AR_PREFIX, ar_mobile, ar_national, format_ar_national, phone_match_key,
    read_pasted_phone, split_ar_national,
)

GROUP_ADMIN = "distri_pedido.grupo_administracion"
MAX_YA_LO_TIENEN = 20


def _can_verify(env):
    return env.su or env.user.has_group(GROUP_ADMIN)


class DistriPegarNumeroMixin(models.AbstractModel):
    """The «Pegar número» box of the dialogs that record a customer's
    WhatsApp. The model needs an `order_id` (sale.order)."""
    _name = "distri.pegar.numero.mixin"
    _description = "Pegar número de WhatsApp"

    pegado = fields.Char(
        string="Pegar número",
        help="Pegá lo que copiaste de WhatsApp («Info. del contacto»: +54 9 299 555-1234) o "
             "cualquier texto con el número: se lee solo.")
    pegado_nacional = fields.Char(compute="_compute_pegado")
    pegado_error = fields.Char(compute="_compute_pegado")
    pegado_preview = fields.Char(string="Número leído", compute="_compute_pegado")
    pegado_aviso = fields.Char(compute="_compute_pegado")
    pegado_del_cliente = fields.Boolean(compute="_compute_pegado")
    pegado_cliente_ids = fields.Many2many(
        "res.partner", string="Ya tienen este número", compute="_compute_pegado")

    def _distri_pegado_valor(self):
        """(national, error) the box works on."""
        self.ensure_one()
        return read_pasted_phone(self.pegado)

    def _distri_pegado_cliente(self):
        """The order's real customer (commercial), or an empty recordset."""
        self.ensure_one()
        partner = self.order_id.sudo().partner_id
        if not partner or partner.distri_generic_partner:
            return self.env["res.partner"]
        return partner.commercial_partner_id

    @api.depends("pegado", "order_id")
    def _compute_pegado(self):
        Partner = self.env["res.partner"]
        for wizard in self:
            national, error = wizard._distri_pegado_valor()
            wizard.pegado_nacional = national or False
            wizard.pegado_error = error or False
            wizard.pegado_preview = format_ar_national(national) if national else False
            avisos = []
            own = wizard._distri_pegado_cliente()
            found = Partner.browse()
            if national:
                order = wizard.order_id.sudo()
                typed = (order.distri_telefono or "").strip()
                typed_key = phone_match_key(typed) if typed else ""
                if typed and typed_key != national:
                    avisos.append("No es el teléfono que escribió el cliente en el pedido (%s)." % (
                        format_ar_national(typed_key) if len(typed_key) == 10 else typed))
                current = own.distri_whatsapp
                if current and phone_match_key(current) != national:
                    avisos.append("Reemplaza al WhatsApp que tiene cargado el cliente (%s)." %
                                  current)
                found = Partner._distri_clientes_por_telefono(national, limit=MAX_YA_LO_TIENEN)
            wizard.pegado_aviso = " ".join(avisos) or False
            wizard.pegado_del_cliente = bool(own and own in found.commercial_partner_id)
            others = found.filtered(lambda p: not own or p.commercial_partner_id != own)
            wizard.pegado_cliente_ids = [Command.set(others.ids)]

    def _distri_pegado_canonical(self):
        """+549 + 10 digits of the box, or a UserError with the reason."""
        self.ensure_one()
        national, error = self._distri_pegado_valor()
        if not national:
            raise UserError(error or "Pegá el número que copiaste del chat.")
        return AR_PREFIX + national


class DistriWaConfirmWizardPegar(models.TransientModel):
    _name = "distri.wa.confirm.wizard"
    _inherit = ["distri.wa.confirm.wizard", "distri.pegar.numero.mixin"]

    pegar_modo = fields.Boolean(
        default=lambda self: bool(self.env.context.get("distri_wa_pegar")),
        help="Abierto con «Pegar número»: guarda el número como WhatsApp verificado.")

    @api.model
    def default_get(self, fields_list):
        values = super().default_get(fields_list)
        if self.env.context.get("distri_wa_pegar") and not (
                self.env.context.get("default_area") or self.env.context.get("default_number")):
            # «Pegar número»: the box starts empty, not with the typed phone
            values.pop("area", None)
            values.pop("number", None)
        return values

    def _distri_pegado_valor(self):
        """The pasted text; without one, what the area and number boxes say
        (their own error is shown apart)."""
        self.ensure_one()
        if (self.pegado or "").strip():
            return read_pasted_phone(self.pegado)
        if self.area or self.number:
            try:
                return ar_national(ar_mobile(self.area, self.number)), ""
            except ValueError:
                return "", ""
        return "", ""

    @api.depends("pegado", "area", "number", "order_id")
    def _compute_pegado(self):
        return super()._compute_pegado()

    @api.onchange("pegado")
    def _onchange_pegado(self):
        national, _error = read_pasted_phone(self.pegado)
        if national:
            self.area, self.number = split_ar_national(national)

    def _canonical(self):
        """The area and number boxes win (pasting fills them and they can
        be corrected by hand); with both empty, the pasted text."""
        self.ensure_one()
        if not (self.area or self.number) and (self.pegado or "").strip():
            return self._distri_pegado_canonical()
        return super()._canonical()

    def action_pegado_asignar(self):
        """«Asignar cliente» with the pasted number already searched (an
        order still under the generic contact)."""
        self.ensure_one()
        national, _error = self._distri_pegado_valor()
        action = self.order_id.action_distri_asignar_cliente()
        wizard = self.env["distri.asignar.cliente"].browse(action["res_id"])
        if national:
            wizard.write({"pegado": format_ar_national(national)})
            wizard._distri_pegado_buscar()
        return action


class DistriAsignarClientePegar(models.TransientModel):
    _name = "distri.asignar.cliente"
    _inherit = ["distri.asignar.cliente", "distri.pegar.numero.mixin"]

    pegado_guardar = fields.Boolean(
        string="Guardar como WhatsApp verificado", default=True,
        help="Al asignar, el número pegado queda como WhatsApp verificado del cliente.")

    @api.onchange("pegado")
    def _onchange_pegado(self):
        national, _error = read_pasted_phone(self.pegado)
        if not national:
            return
        self.busqueda = format_ar_national(national)
        if not (self.nuevo_telefono or "").strip() or (
                phone_match_key(self.nuevo_telefono) == phone_match_key(self.web_telefono or "")):
            self.nuevo_telefono = format_ar_national(national)

    def _distri_pegado_buscar(self):
        """The candidates of the pasted number; one alone is chosen."""
        for wizard in self:
            national, _error = read_pasted_phone(wizard.pegado)
            if not national:
                continue
            wizard.busqueda = format_ar_national(national)
            wizard._cargar_candidatos()
            found = self.env["res.partner"]._distri_clientes_por_telefono(national)
            if len(found) == 1 and not wizard.partner_id:
                wizard.write({"modo": "existente", "partner_id": found.id})

    def action_pegado_buscar(self):
        """«Buscar este número»."""
        self.ensure_one()
        self._distri_pegado_canonical()
        self._distri_pegado_buscar()
        return self._reabrir()

    def _despues_de_asignar(self, pedido, cliente):
        result = super()._despues_de_asignar(pedido, cliente)
        national, _error = read_pasted_phone(self.pegado)
        if national and self.pegado_guardar:
            number = AR_PREFIX + national
            if _can_verify(self.env):
                pedido._distri_wa_mark_verified(number)
            else:
                pedido.message_post(body="%s pegó el WhatsApp del chat al asignar el cliente: %s. "
                                         "Falta que administración lo verifique." % (
                                             self.env.user.name, number))
        return result


class SaleOrderPegarNumero(models.Model):
    _inherit = "sale.order"

    distri_wa_code_line = fields.Char(
        string="Código para buscar en WhatsApp", compute="_compute_distri_wa_code_line",
        help="La línea que trae el mensaje del cliente: copiala y buscala en WhatsApp Web "
             "cuando el cliente escribió desde otro número.")

    @api.depends("name", "distri_wa_code")
    def _compute_distri_wa_code_line(self):
        for order in self:
            order.distri_wa_code_line = (order._distri_wa_code_line()
                                         if order.distri_wa_code else False)

    def action_distri_pegar_numero(self):
        """«Pegar número»: the WhatsApp copied from the chat becomes the
        customer's verified WhatsApp (or the order's, under the generic
        contact)."""
        self.ensure_one()
        if not _can_verify(self.env):
            raise UserError("Solo administración y gerencia cargan el WhatsApp del cliente.")
        if self.state == "cancel":
            raise UserError("El pedido %s está cancelado." % self.name)
        return {
            "type": "ir.actions.act_window",
            "name": "Pegar número",
            "res_model": "distri.wa.confirm.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_order_id": self.id, "default_then": "verify",
                        "distri_wa_pegar": True},
        }
