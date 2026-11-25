"""19.0.1.129.36 (owner's decision B, 25/11/2026; plan unificado, Etapa 4):
with «Nueva venta» on (distri_pedido.nueva_venta), a customer invoice that
does not come from a sale order is a «factura directa», and it is kept for
non-merchandise charges only.

- Who: the members of «Facturación directa de servicios»
  (grupo_facturacion_directa): Denis, Gonzalo and Ale. Nobody is put in the
  group by the module: scripts/correcciones/v129_36_facturacion_directa.sh
  adds them by login (`_distri_asignar_facturacion_directa`).
- What: only lines of a service product, lines without a product (a free
  text line with its account) and section/note lines. A line of goods is
  refused, for them too: merchandise is sold through «Nueva venta».
- Where: at posting (account.move._post), so it holds from every screen,
  import or action; the invoice form also drops a goods line with a
  message, and shows a banner.

Not a «factura directa», so never refused here:
- an invoice or credit note with a line linked to a sale order line
  (sale_line_ids): every sale path (Nueva venta, /pedido, the end of the
  packing, AgilPedido, Odoo's «Crear factura»), and the credit notes that
  copy those links (Odoo's reversal, «Anular venta», settled returns);
- a credit note of Odoo's reversal of an invoice (reversed_entry_id) for at
  most that invoice's total;
- vendor bills and journal entries;
- the module's own account tools, which pass DIRECTA_INTERNA in the
  context: «Ajuste de cuenta» (AJC, models/ajuste_cuenta.py) and «Venta sin
  registrar» (VSR, models/revision_cliente.py);
- code running as the superuser itself (odoo shell scripts, crons), except
  a scheduled («auto_post») invoice, which is judged by the user who made it.

With the flag off nothing here applies: everything is as in 129.35.
"""
from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import UserError

from .nueva_venta import nueva_venta_on

GRUPO_DIRECTA = "distri_pedido.grupo_facturacion_directa"
# the context key of the module's own account tools (AJC, VSR)
DIRECTA_INTERNA = "distri_factura_directa_interna"
# who the activation script puts in the group (owner, 25/11/2026): Ale
# («Ale · Sistemas», the admin user), Denis and Gonzalo
LOGINS_DIRECTA = ("admin", "denis", "gonzalo")

NO_PERMITIDA = ("Las facturas que no salen de un pedido ya no se cargan a mano: la venta se "
                "hace en «Nueva venta» («Mostrador» si el cliente se la lleva ahora, «Ya "
                "entregado» si ya la tiene). %s no se publicó.")
MERCADERIA = ("Una factura directa es solo para servicios o cargos que no son mercadería. "
              "%s tiene mercadería (%s): vendela con «Nueva venta» («Mostrador» o «Ya "
              "entregado»).")
MERCADERIA_FORM = ("Una factura directa es solo para servicios o cargos que no son mercadería. "
                   "«%s» es mercadería: vendela con «Nueva venta» («Mostrador» o «Ya "
                   "entregado»).")
AVISO_PERMITIDO = ("Factura directa (no sale de un pedido): solo servicios o cargos que no son "
                   "mercadería. La mercadería se vende con «Nueva venta».")
AVISO_NO_PERMITIDO = ("Factura directa (no sale de un pedido): no la vas a poder publicar. La venta "
                      "se hace en «Nueva venta» («Mostrador» o «Ya entregado»).")


def puede_facturar_directo(user):
    return bool(user) and not user.share and user.has_group(GRUPO_DIRECTA)


def es_mercaderia(line):
    """A goods line: a product that is not a service. A line without a
    product (free text with its account) and sections or notes are not."""
    return line.display_type == "product" and bool(line.product_id) \
        and line.product_id.type != "service"


class AccountMoveDirecta(models.Model):
    _inherit = "account.move"

    distri_aviso_directa = fields.Char(
        string="Aviso de factura directa", compute="_compute_distri_aviso_directa")

    def _distri_es_directa(self):
        """A customer invoice, credit note or receipt not linked to any sale
        order line, and not a reversal of an invoice for at most its total."""
        self.ensure_one()
        if not self.is_sale_document(include_receipts=True):
            return False
        # sudo: a user without access to the sale lines must not see none
        if self.sudo().invoice_line_ids.sale_line_ids:
            return False
        origin = self.reversed_entry_id
        if (self.move_type == "out_refund" and origin
                and origin.move_type in ("out_invoice", "out_receipt")
                and self.currency_id.compare_amounts(self.amount_total,
                                                     origin.amount_total) <= 0):
            return False
        return True

    @api.depends("move_type", "state", "reversed_entry_id", "amount_total",
                 "invoice_line_ids.sale_line_ids")
    @api.depends_context("uid")
    def _compute_distri_aviso_directa(self):
        on = nueva_venta_on(self.env)
        allowed = puede_facturar_directo(self.env.user)
        for move in self:
            show = on and move.state == "draft" and move._distri_es_directa()
            move.distri_aviso_directa = (
                (AVISO_PERMITIDO if allowed else AVISO_NO_PERMITIDO) if show else False)

    def _distri_check_directa(self):
        """Refuses posting a «factura directa» not allowed (module docstring)."""
        if self.env.context.get(DIRECTA_INTERNA) or not nueva_venta_on(self.env):
            return
        root = self.env.su and self.env.uid == SUPERUSER_ID
        for move in self:
            if not move._distri_es_directa():
                continue
            user = self.env.user
            if root:
                # a script or a cron as the superuser; a scheduled invoice is
                # its author's
                if move.auto_post == "no" or move.create_uid.id == SUPERUSER_ID:
                    continue
                user = move.create_uid
            name = move.name if move.name and move.name != "/" else "La factura"
            if not puede_facturar_directo(user):
                raise UserError(NO_PERMITIDA % name)
            goods = move.invoice_line_ids.filtered(es_mercaderia)
            if goods:
                raise UserError(MERCADERIA % (name, ", ".join(
                    goods.product_id.mapped("display_name"))))

    def _post(self, soft=True):
        self._distri_check_directa()
        return super()._post(soft=soft)


class AccountMoveLineDirecta(models.Model):
    _inherit = "account.move.line"

    @api.onchange("product_id")
    def _onchange_distri_factura_directa(self):
        """The invoice form drops a goods line of a «factura directa» at once,
        with the reason (the posting refuses it anyway)."""
        if not self.product_id or not es_mercaderia(self) or self.sudo().sale_line_ids:
            return None
        move = self.move_id
        if not move or not nueva_venta_on(self.env) or not move._distri_es_directa():
            return None
        name = self.product_id.display_name
        self.product_id = False
        return {"warning": {"title": "Factura directa", "message": MERCADERIA_FORM % name}}


class ResUsersDirecta(models.Model):
    _inherit = "res.users"

    @api.model
    def _distri_asignar_facturacion_directa(self, logins=LOGINS_DIRECTA, apply=False):
        """Puts the users of `logins` in «Facturación directa de servicios»
        (archived ones too: the group waits for them). Returns one line per
        login. Nobody is removed. Dry run unless `apply`."""
        group = self.env.ref(GRUPO_DIRECTA)
        Users = self.sudo().with_context(active_test=False)
        lines = []
        for login in logins:
            user = Users.search([("login", "=", login)], limit=1)
            if not user:
                lines.append("MISSING %s: no such user" % login)
                continue
            if user.share:
                lines.append("SKIP %s: not an internal user" % login)
                continue
            state = "active" if user.active else "archived"
            if group in user.group_ids:
                lines.append("ALREADY %s (%s, %s)" % (login, user.name, state))
                continue
            if apply:
                user.write({"group_ids": [(4, group.id)]})
            lines.append("%s %s (%s, %s)" % ("ADDED" if apply else "WOULD_ADD", login,
                                              user.name, state))
        return lines
