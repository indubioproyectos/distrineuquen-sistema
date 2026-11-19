"""«Retener / Liberar» and «Pedidos para revisar» (19.0.1.85.0).

Decision for the phase-1 launch: every order (web /pedido, staff, backend)
keeps flowing to packing as before; nothing waits by default. The manager
reviews the day's orders from his phone, sorted by a risk semaphore
(models/semaforo.py), and holds the suspicious ones.

A held order (`distri_retenido`):
- is not packed: it leaves the packer's screen and queue while nobody took
  it (`_distri_armable`, the outermost override: this file loads last). An
  order already taken can be finished, so nothing breaks mid-packing.
- is not invoiced: neither the automatic invoice of the packing
  (`_distri_listo_para_facturar`) nor «Facturar lo armado» nor the standard
  invoice button (`_create_invoices`).
- is not routed: route assignment skips it, no route stop can be created
  for it (manual stops, «Deshacer» of the dispatch, depot delivery), and on
  hold it is taken off the routes that did not leave yet («Queda en el
  depósito», models/despacho.py), also after «Controlado» (19.0.1.93.0,
  E2E-C C4: with a note on the route). On a route already on the street the
  driver's app shows it held and refuses to deliver or collect it
  (models/reparto_sync.py): he marks it «No se entregó».
- cannot be confirmed (approved) until released.
Cancelling works as usual. «Liberar» restores the flow and, when the order
was packed meanwhile, the automatic invoice comes out then.

19.0.1.96.0 (audit F1): an order sent from /pedido by an anonymous visitor
(no session, so no known customer) is held on arrival with
`distri_retenido_sin_cliente`: it never reaches /armado until
administración assigns the customer and releases it. «Liberar» refuses while
it is still on the generic «Pedidos web» contact; «Asignar y aprobar»
releases it in the same step. Staff orders and logged-in customers' orders
are not held (unchanged).
"""
from datetime import timedelta

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain

from .semaforo import (
    DIAS_CLIENTE_NUEVO, RED, SEMAFORO, SEMAFORO_ORDEN, ULTIMOS, evaluar,
    inicio_del_dia_utc, link_whatsapp, promedio_base,
)

GRUPO_REVISAR = "distri_pedido.grupo_administracion"
GRUPO_GERENCIA = "distri_pedido.grupo_gerencia"

# «1» (default): an activity for gerencia when a red order arrives
PARAM_ALERTA_ROJOS = "distri_pedido.revisar_alerta_rojos"
RESUMEN_ALERTA = "Pedido para revisar (rojo)"

MOTIVO_MAX = 250

# the reason of the automatic hold of an anonymous /pedido order (F1)
MOTIVO_SIN_CLIENTE_WEB = "Pedido de la página sin cliente: asignale el cliente y liberalo"

ORIGENES_REVISAR = [
    ("web", "Página"),
    ("whatsapp", "WhatsApp (AgilPedido)"),
    ("staff", "Personal"),
    ("backend", "Backend"),
]


def puede_revisar(env):
    return env.su or env.user.has_group(GRUPO_REVISAR)


def _exigir_revisar(env):
    if not puede_revisar(env):
        raise UserError("Solo administración y gerencia retienen o liberan pedidos.")


def _valor_booleano(operator, value):
    """True/False for the `in` domains Odoo 19 hands to search methods,
    None when the value mixes both or is not a boolean set."""
    if operator != "in":
        return None
    valores = {bool(v) for v in value}
    return valores.pop() if len(valores) == 1 else None


class SaleOrderRevisar(models.Model):
    _inherit = "sale.order"

    # ---- hold
    distri_retenido = fields.Boolean(string="Retenido", copy=False, readonly=True, index=True)
    distri_retenido_motivo = fields.Char(string="Motivo de la retención", copy=False,
                                         readonly=True)
    distri_retenido_por_id = fields.Many2one("res.users", string="Retenido por", copy=False,
                                             readonly=True)
    distri_retenido_el = fields.Datetime(string="Retenido el", copy=False, readonly=True)
    # 19.0.1.96.0 (audit F1): held on arrival because nobody knows the
    # customer; released only once the customer is assigned
    distri_retenido_sin_cliente = fields.Boolean(
        string="Retenido hasta asignar el cliente", copy=False, readonly=True)
    distri_retenido_rutas = fields.Char(
        string="Sigue en la ruta", compute="_compute_distri_retenido_rutas",
        help="Rutas que ya salieron (o ya se controlaron) donde el pedido retenido sigue "
             "como visita pendiente.")

    # ---- semaphore: a snapshot of the order's risk, stored so the list
    # sorts and filters by it. It follows the customer, the amount and the
    # customer's WhatsApp; the customer's later purchases do not repaint it.
    distri_semaforo = fields.Selection(
        SEMAFORO, string="Semáforo", compute="_compute_distri_semaforo", store=True)
    distri_semaforo_orden = fields.Integer(
        string="Orden del semáforo", compute="_compute_distri_semaforo", store=True, index=True)
    distri_semaforo_motivos = fields.Char(
        string="Motivos", compute="_compute_distri_semaforo", store=True)

    # ---- the list
    distri_revisar_origen = fields.Selection(
        ORIGENES_REVISAR, string="Origen del pedido", compute="_compute_distri_revisar_origen")
    distri_revisar_wa_url = fields.Char(string="Abrir chat",
                                        compute="_compute_distri_revisar_wa_url")
    distri_revisar_en_lista = fields.Boolean(
        string="Para revisar", compute="_compute_distri_revisar_en_lista",
        search="_search_distri_revisar_en_lista",
        help="De hoy o de ayer y sin cancelar, más todos los retenidos.")
    distri_revisar_hoy = fields.Boolean(
        string="De hoy", compute="_compute_distri_revisar_en_lista",
        search="_search_distri_revisar_hoy")
    distri_revisar_alertado = fields.Boolean(
        string="Aviso a gerencia", copy=False, readonly=True,
        help="Ya se le avisó a gerencia que entró en rojo: no se vuelve a avisar.")

    # ------------------------------------------------------------ computes

    @api.depends("distri_retenido", "distri_stop_ids.state", "distri_stop_ids.route_id.state")
    def _compute_distri_retenido_rutas(self):
        for order in self:
            rutas = order.sudo().distri_stop_ids.filtered(
                lambda s: s.state == "pending").route_id if order.distri_retenido else ()
            order.distri_retenido_rutas = ", ".join(r.name or "" for r in rutas) or False

    @api.depends("partner_id", "partner_id.distri_generic_partner", "partner_id.distri_whatsapp",
                 "partner_id.commercial_partner_id.distri_whatsapp", "amount_total", "date_order",
                 "distri_wa_sin_telefono")
    def _compute_distri_semaforo(self):
        ahora = fields.Datetime.now()
        for order in self:
            color, motivos = evaluar(**order.sudo()._distri_semaforo_hechos(ahora))
            order.distri_semaforo = color
            order.distri_semaforo_orden = SEMAFORO_ORDEN[color]
            order.distri_semaforo_motivos = motivos or False

    def _distri_semaforo_hechos(self, ahora):
        """The facts the semaphore reads, as of the order's date."""
        self.ensure_one()
        partner = self.partner_id
        if not partner or partner.distri_generic_partner:
            return {"sin_cliente": True, "wa_sin_telefono": self.distri_wa_sin_telefono}
        cliente = partner.commercial_partner_id
        cuando = self.date_order or ahora
        origen = self._origin.id or 0

        pedidos = self.env["sale.order"].search([
            ("partner_id", "child_of", cliente.id), ("state", "=", "sale"),
            ("id", "!=", origen), ("date_order", "<=", cuando),
        ], order="date_order desc, id desc", limit=ULTIMOS)
        facturas = self.env["account.move"].search([
            ("commercial_partner_id", "=", cliente.id), ("move_type", "=", "out_invoice"),
            ("state", "=", "posted"), ("invoice_date", "<=", cuando.date()),
            ("id", "not in", self._origin.invoice_ids.ids),
        ], order="invoice_date desc, id desc", limit=ULTIMOS)
        # the imported history (Odoo Online): years of invoices the lab has
        # in the customer's account, not as account moves
        historia = self.env["distri.cuenta.movimiento"].search([
            ("partner_id", "child_of", cliente.id), ("tipo", "=", "factura"),
            ("fecha", "<=", cuando.date()),
        ], order="fecha desc, id desc", limit=ULTIMOS)

        montos_pedidos = pedidos.mapped("amount_total")
        con_fecha = [(f.invoice_date, f.amount_total) for f in facturas]
        con_fecha += [(m.fecha, m.debe) for m in historia]
        con_fecha.sort(key=lambda par: par[0], reverse=True)
        montos_facturas = [monto for _fecha, monto in con_fecha]

        creado = cliente.create_date or partner.create_date
        return {
            "cliente_nuevo": bool(creado and creado > cuando - timedelta(days=DIAS_CLIENTE_NUEVO)),
            "sin_compras": not (pedidos or facturas or historia),
            "sin_whatsapp": not (partner.distri_whatsapp or cliente.distri_whatsapp),
            "importe": self.amount_total,
            "montos_previos": promedio_base(montos_pedidos, montos_facturas),
            "wa_sin_telefono": self.distri_wa_sin_telefono,
        }

    @api.depends("distri_origen", "distri_wa_state", "distri_original")
    def _compute_distri_revisar_origen(self):
        for order in self:
            if order.distri_origen:
                order.distri_revisar_origen = "whatsapp"
            elif order.distri_wa_state in ("staff", "counter"):
                order.distri_revisar_origen = "staff"
            elif order.distri_wa_state or order.distri_original:
                order.distri_revisar_origen = "web"
            else:
                order.distri_revisar_origen = "backend"

    @api.depends("partner_id", "distri_telefono", "distri_wa_phone")
    def _compute_distri_revisar_wa_url(self):
        for order in self:
            digitos = order._distri_wa_customer_number() if order.partner_id else ""
            order.distri_revisar_wa_url = link_whatsapp(digitos) or False

    @api.depends("state", "date_order", "distri_retenido")
    def _compute_distri_revisar_en_lista(self):
        ayer, hoy = inicio_del_dia_utc(1), inicio_del_dia_utc(0)
        for order in self:
            fecha = order.date_order
            vivo = order.state != "cancel"
            order.distri_revisar_hoy = bool(vivo and fecha and fecha >= hoy)
            order.distri_revisar_en_lista = vivo and (
                order.distri_retenido or bool(fecha and fecha >= ayer))

    @api.model
    def _distri_revisar_dominio(self):
        """Today's and yesterday's orders (Argentine days) not cancelled, and
        every held order still alive."""
        return Domain("state", "!=", "cancel") & (
            Domain("distri_retenido", "=", True)
            | Domain("date_order", ">=", inicio_del_dia_utc(1)))

    def _search_distri_revisar_en_lista(self, operator, value):
        valor = _valor_booleano(operator, value)
        if valor is None:
            return NotImplemented
        dominio = self._distri_revisar_dominio()
        return dominio if valor else ~dominio

    def _search_distri_revisar_hoy(self, operator, value):
        valor = _valor_booleano(operator, value)
        if valor is None:
            return NotImplemented
        dominio = Domain("state", "!=", "cancel") & Domain(
            "date_order", ">=", inicio_del_dia_utc(0))
        return dominio if valor else ~dominio

    # ------------------------------------------------------------ hold rules

    def _distri_armable(self):
        # a held order nobody took leaves the packer's queue; one already
        # taken can be finished (the invoice and the route wait)
        if self.distri_retenido and self.distri_armado == "pendiente":
            return False
        return super()._distri_armable()

    def _distri_listo_para_facturar(self):
        return not self.distri_retenido and super()._distri_listo_para_facturar()

    def _distri_check_depot_delivery(self):
        self.ensure_one()
        if self.distri_retenido:
            raise UserError("El pedido %s está retenido: liberalo antes de entregarlo." % self.name)
        return super()._distri_check_depot_delivery()

    def _create_invoices(self, grouped=False, final=False, date=None):
        retenidos = self.filtered("distri_retenido")
        if retenidos:
            raise UserError("El pedido %s está retenido: liberalo antes de facturarlo."
                            % retenidos[0].name)
        return super()._create_invoices(grouped=grouped, final=final, date=date)

    def action_distri_facturar_armado(self):
        retenidos = self.filtered("distri_retenido")
        if retenidos:
            raise UserError("El pedido %s está retenido: liberalo antes de facturar lo armado."
                            % retenidos[0].name)
        return super().action_distri_facturar_armado()

    def action_confirm(self):
        retenidos = self.filtered("distri_retenido")
        if retenidos:
            raise UserError("El pedido %s está retenido: liberalo antes de confirmarlo."
                            % retenidos[0].name)
        return super().action_confirm()

    # ------------------------------------------------------------ hold actions

    def action_distri_retener(self):
        """«Retener»: asks the reason."""
        self.ensure_one()
        _exigir_revisar(self.env)
        self._distri_exigir_retenible()
        return {
            "type": "ir.actions.act_window",
            "name": "Retener el pedido",
            "res_model": "distri.retener.wizard",
            "view_mode": "form",
            "target": "new",
            "context": {"default_order_id": self.id},
        }

    def _distri_exigir_retenible(self):
        self.ensure_one()
        if self.state == "cancel":
            raise UserError("El pedido %s está cancelado." % self.name)
        if self.distri_retenido:
            raise UserError("El pedido %s ya está retenido." % self.name)

    def distri_retener(self, motivo):
        """Holds the order with the reason, in the chatter with the user."""
        _exigir_revisar(self.env)
        motivo = " ".join(str(motivo or "").split())[:MOTIVO_MAX]
        if not motivo:
            raise UserError("Escribí por qué retenés el pedido.")
        usuario = self.env.user
        for order in self:
            order._distri_bloquear()
            order._distri_exigir_retenible()
            order.sudo().write({
                "distri_retenido": True,
                "distri_retenido_motivo": motivo,
                "distri_retenido_por_id": usuario.id,
                "distri_retenido_el": fields.Datetime.now(),
            })
            nota = "%s retuvo el pedido: %s." % (usuario.name, motivo)
            if order.distri_armado == "en_armado":
                nota += (" El armador ya lo había tomado: puede terminarlo, pero no se factura "
                         "ni sale en una ruta hasta liberarlo.")
            elif order.distri_armado == "armado":
                nota += " Ya estaba armado: no se factura ni sale en una ruta hasta liberarlo."
            order.message_post(body=nota)
            order._distri_sacar_de_rutas(motivo)
        return True

    def _distri_retener_sin_cliente(self):
        """Automatic hold of an anonymous /pedido order (audit F1). Called by
        the controller right after creating it: no user to check, nothing to
        take off a route yet. A held order is never packed nor confirmed
        until released (_distri_armable, action_confirm)."""
        for order in self.sudo().filtered(
                lambda o: not o.distri_retenido and o.state != "cancel"
                and o._distri_sin_cliente()):
            order.write({
                "distri_retenido": True,
                "distri_retenido_sin_cliente": True,
                "distri_retenido_motivo": MOTIVO_SIN_CLIENTE_WEB,
                "distri_retenido_por_id": False,
                "distri_retenido_el": fields.Datetime.now(),
            })
            order.message_post(body=(
                "Pedido de la página sin cliente identificado: queda retenido y no pasa al "
                "armado. Asignale el cliente y liberalo (o «Asignar y aprobar»)."))
        return True

    def _distri_sacar_de_rutas(self, motivo):
        """Off the routes still at the depot, as «Queda en el depósito»
        (models/despacho.py). A route that already left keeps the stop: the
        form's banner says so, and the app refuses to deliver it."""
        self.ensure_one()
        usuario = self.env.user
        paradas = self.sudo().distri_stop_ids.filtered(
            lambda s: s.state == "pending" and s.route_id.kind == "route"
            and s.route_id.state == "draft")
        controladas = paradas.filtered(lambda s: s.route_id.departure_controlled_at)
        abiertas = (paradas - controladas).filtered(lambda s: s.route_id.driver_id != usuario)
        if abiertas:
            abiertas.action_leave_at_depot("Retenido: %s" % motivo)
        # 19.0.1.93.0 (C4): «Controlado» but still at the depot: it comes off
        # too, and the route says so (the load was checked with it)
        for parada in controladas:
            ruta = parada.route_id
            parada._distri_leave_at_depot("Retenido: %s" % motivo)
            ruta.message_post(body="%s retuvo %s después de «Controlado»: sale de la ruta y queda "
                                   "en el depósito. Bajalo del vehículo antes de salir." % (
                                       usuario.name, self.name))

    def action_distri_liberar(self):
        """«Liberar»: back to the normal flow. A packed order is invoiced now."""
        _exigir_revisar(self.env)
        usuario = self.env.user
        for order in self:
            order._distri_bloquear()
            if not order.distri_retenido:
                raise UserError("El pedido %s no está retenido." % order.name)
            if order.distri_retenido_sin_cliente and order._distri_sin_cliente():
                raise UserError(
                    "El pedido %s llegó de la página sin cliente: asignale el cliente antes de "
                    "liberarlo." % order.name)
            order.sudo().write({"distri_retenido": False, "distri_retenido_sin_cliente": False})
            nota = "%s liberó el pedido: sigue el circuito normal." % usuario.name
            afuera = self.env["distri.route.left.order"].sudo().search([
                ("order_id", "=", order.id), ("route_id.state", "=", "draft")])
            if afuera:
                nota += (" Al retenerlo había quedado fuera de la ruta %s: volvelo a subir con "
                         "«Deshacer» o asignalo a otra ruta." % ", ".join(
                             afuera.route_id.mapped("name")))
            order.message_post(body=nota)
            # the automatic invoice of the packing, which waited for the release
            order._distri_facturar_armado()
        return True

    def action_distri_abrir_chat(self):
        self.ensure_one()
        _exigir_revisar(self.env)
        if not self.distri_revisar_wa_url:
            raise UserError("El cliente no tiene un WhatsApp para escribirle.")
        return {"type": "ir.actions.act_url", "url": self.distri_revisar_wa_url,
                "target": "new"}

    @api.model
    def action_distri_revisar_asignar(self):
        """«Asignar cliente» opened from the phone's «Pedidos para revisar»
        (the v108 bridge: server action action_revisar_asignar_cliente,
        opened by URL, no active record). The wizard the page just prepared
        for this user, with the pasted number already searched.

        19.0.1.111.0 replaces the bridge with the kit screen
        (controllers/asignar.py) and drops the server action; this method
        stays only for a database still on 19.0.1.110.0 (the page uses the
        bridge until the module is updated). Remove it in the next version."""
        _exigir_revisar(self.env)
        wizard = self.env["distri.asignar.cliente"].search([
            ("create_uid", "=", self.env.uid),
            ("create_date", ">=", fields.Datetime.now() - timedelta(minutes=30)),
        ], order="id desc", limit=1)
        if not wizard or not wizard.order_id.distri_partner_generic:
            raise UserError("Volvé a «Pedidos para revisar» y tocá «Asignar cliente» en el pedido.")
        action = wizard._reabrir()
        action["context"] = dict(action["context"], default_order_id=wizard.order_id.id)
        return action

    # ------------------------------------------------------------ alerts

    @api.model_create_multi
    def create(self, vals_list):
        orders = super().create(vals_list)
        orders._distri_revisar_alertar()
        return orders

    @api.model
    def _distri_alerta_activa(self):
        valor = self.env["ir.config_parameter"].sudo().get_param(PARAM_ALERTA_ROJOS, "1")
        return (valor or "").strip().lower() not in ("0", "false", "no", "off", "")

    @api.model
    def _distri_usuarios_gerencia(self):
        grupo = self.env.ref(GRUPO_GERENCIA, raise_if_not_found=False)
        if not grupo:
            return self.env["res.users"]
        usuarios = self.env["res.users"].sudo().search([
            ("all_group_ids", "in", grupo.ids), ("share", "=", False),
        ], order="id")
        # the technical accounts only when nobody else is in gerencia
        tecnicos = self.env.ref("base.user_root") | self.env.ref("base.user_admin")
        return (usuarios - tecnicos) or usuarios

    def _distri_revisar_alertar(self):
        """One activity per gerencia user for a red order, once per order."""
        rojos = self.sudo().filtered(
            lambda o: o.distri_semaforo == RED and not o.distri_revisar_alertado
            and o.state != "cancel")
        if not rojos or not self._distri_alerta_activa():
            return
        usuarios = self._distri_usuarios_gerencia()
        # the activity alone (bell and «Pedidos para revisar»): no assignment
        # email for every red order
        rojos = rojos.with_context(mail_activity_quick_update=True)
        for order in rojos:
            for usuario in usuarios:
                order.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=RESUMEN_ALERTA,
                    note="Entró en rojo: %s. Revisalo en «Pedidos para revisar»."
                         % (order.distri_semaforo_motivos or ""),
                    user_id=usuario.id,
                )
            order.distri_revisar_alertado = True


class DistriRetenerWizard(models.TransientModel):
    """«Retener el pedido»: the short reason for the chatter."""
    _name = "distri.retener.wizard"
    _description = "Retener un pedido"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, readonly=True,
                               ondelete="cascade")
    motivo = fields.Char(string="Motivo", required=True, size=MOTIVO_MAX)

    def action_retener(self):
        self.ensure_one()
        self.order_id.distri_retener(self.motivo)
        return {"type": "ir.actions.act_window_close"}


class DistriRouteRevisar(models.Model):
    _inherit = "distri.route"

    def _free_orders_domain(self):
        return super()._free_orders_domain() + [("distri_retenido", "=", False)]


class DistriRouteStopRevisar(models.Model):
    _inherit = "distri.route.stop"

    @api.model_create_multi
    def create(self, vals_list):
        ids = [v["order_id"] for v in vals_list if v.get("order_id")]
        retenidos = self.env["sale.order"].sudo().browse(ids).filtered("distri_retenido")
        if retenidos:
            raise UserError("El pedido %s está retenido: liberalo antes de subirlo a una ruta."
                            % retenidos[0].name)
        return super().create(vals_list)
