from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models import semaforo
from odoo.addons.distri_pedido.models.revisar import PARAM_ALERTA_ROJOS, RESUMEN_ALERTA
from odoo.addons.distri_pedido.models.sale_order import SOCIO_SIN_ASIGNAR
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_revisar")
class TestRevisar(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.armador = new_test_user(
            cls.env, login="armador_revisar", name="Armador Revisar",
            groups="base.group_user,distri_pedido.grupo_armador", **company)
        cls.vendedor = new_test_user(
            cls.env, login="vendedor_revisar", name="Vendedor Revisar",
            groups="base.group_user,distri_pedido.grupo_vendedor", **company)
        cls.gerente = new_test_user(
            cls.env, login="gerente_revisar", name="Gerente Revisar",
            groups="base.group_user,distri_pedido.grupo_gerencia", **company)
        cls._setup_reparto()
        cls.generico = cls.env["res.partner"].create({
            "name": SOCIO_SIN_ASIGNAR, "distri_generic_partner": True,
            "distri_generic_kind": "retail"})
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Revisar", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})
        cls.zona = cls.env["distri.zona"].sudo().create({"name": "Zona Test Revisar", "viaje": "1"})
        # a regular customer: old, with history and WhatsApp
        cls.habitual = cls.env["res.partner"].create({
            "name": "Kiosco Habitual", "city": "Zona Test Revisar",
            "distri_whatsapp": "+5492994222444"})
        cls._antiguo(cls.habitual)
        cls._historia(cls.habitual, [1000.0, 1000.0, 1000.0])

    # ------------------------------------------------------------ helpers

    @classmethod
    def _antiguo(cls, partner, dias=400):
        cls.env.cr.execute("UPDATE res_partner SET create_date = %s WHERE id = %s",
                           (fields.Datetime.now() - timedelta(days=dias), partner.id))
        partner.invalidate_recordset(["create_date"])

    @classmethod
    def _historia(cls, partner, montos):
        """Imported invoices (Odoo Online history) of the customer."""
        hoy = fields.Date.today()
        cls.env["distri.cuenta.movimiento"].sudo().create([{
            "partner_id": partner.id, "tipo": "factura", "debe": monto,
            "fecha": hoy - timedelta(days=10 + i), "comprobante": "FA-%s" % i,
        } for i, monto in enumerate(montos)])

    def _pedido(self, partner=None, precio=100.0, cantidad=10, confirmar=False):
        order = self.env["sale.order"].create({
            "partner_id": (partner or self.habitual).id,
            "pricelist_id": self.pricelist.id,
            "order_line": [Command.create({
                "product_id": self.product_a.id, "product_uom_qty": cantidad,
                "price_unit": precio, "tax_ids": [Command.clear()]})],
        })
        if confirmar:
            order.action_confirm()
        return order

    def _armar(self, order, terminar=True):
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        if terminar:
            self.assertEqual(pedido.distri_terminar(1), "")
        return order

    def _retener(self, order, motivo="Cliente nuevo, confirmar por teléfono"):
        wizard = self.env["distri.retener.wizard"].create({
            "order_id": order.id, "motivo": motivo})
        wizard.action_retener()

    def _notas(self, order):
        return " ".join(order.message_ids.mapped(lambda m: str(m.body or "")))

    def _facturas(self, order):
        return order.invoice_ids.filtered(lambda f: f.move_type == "out_invoice")

    # ------------------------------------------------------------ semaphore: pure rules

    def test_reglas_puras(self):
        self.assertEqual(semaforo.evaluar(sin_cliente=True), ("rojo", "Sin cliente asignado"))
        self.assertEqual(semaforo.evaluar(cliente_nuevo=True, sin_whatsapp=True),
                         ("rojo", "Cliente nuevo · Sin WhatsApp"))
        self.assertEqual(semaforo.evaluar(importe=2001, montos_previos=[1000, 1000]),
                         ("amarillo", "Importe alto"))
        self.assertEqual(semaforo.evaluar(importe=2000, montos_previos=[1000, 1000]),
                         ("verde", ""))
        # enough sale orders: they make the average; otherwise the invoices
        self.assertEqual(semaforo.promedio_base([5, 5, 5], [1]), [5, 5, 5])
        self.assertEqual(semaforo.promedio_base([5], [1, 2]), [1, 2])
        self.assertEqual(semaforo.promedio_base([5], []), [5])

    # ------------------------------------------------------------ semaphore: orders

    def test_semaforo_sin_cliente(self):
        order = self._pedido(self.generico)
        self.assertEqual(order.distri_semaforo, "rojo")
        self.assertEqual(order.distri_semaforo_motivos, "Sin cliente asignado")
        self.assertEqual(order.distri_semaforo_orden, 1)

    def test_semaforo_cliente_nuevo(self):
        nuevo = self.env["res.partner"].create({"name": "Cliente Recién Creado"})
        order = self._pedido(nuevo)
        self.assertEqual(order.distri_semaforo, "rojo")
        self.assertEqual(order.distri_semaforo_motivos,
                         "Cliente nuevo · Sin compras anteriores · Sin WhatsApp")

    def test_semaforo_sin_compras_anteriores(self):
        viejo = self.env["res.partner"].create({
            "name": "Cliente Sin Compras", "distri_whatsapp": "+5492994111222"})
        self._antiguo(viejo)
        order = self._pedido(viejo)
        self.assertEqual((order.distri_semaforo, order.distri_semaforo_motivos),
                         ("rojo", "Sin compras anteriores"))

    def test_semaforo_verde(self):
        order = self._pedido(precio=150.0)  # 1500 vs an average of 1000
        self.assertEqual(order.distri_semaforo, "verde")
        self.assertFalse(order.distri_semaforo_motivos)
        self.assertEqual(order.distri_semaforo_orden, 3)

    def test_semaforo_importe_alto(self):
        order = self._pedido(precio=201.0)  # 2010 > 2 x 1000
        self.assertEqual((order.distri_semaforo, order.distri_semaforo_motivos),
                         ("amarillo", "Importe alto"))
        # a lower price: the semaphore follows the amount
        order.order_line.price_unit = 100.0
        self.assertEqual(order.distri_semaforo, "verde")

    def test_semaforo_sin_whatsapp_y_se_corrige(self):
        cliente = self.env["res.partner"].create({"name": "Cliente Sin WA"})
        self._antiguo(cliente)
        self._historia(cliente, [500.0])
        order = self._pedido(cliente, precio=50.0)
        self.assertEqual((order.distri_semaforo, order.distri_semaforo_motivos),
                         ("amarillo", "Sin WhatsApp"))
        cliente.distri_whatsapp = "+5492994333444"
        self.assertEqual(order.distri_semaforo, "verde")

    def test_semaforo_promedio_con_facturas_de_odoo(self):
        cliente = self.env["res.partner"].create({
            "name": "Cliente Con Facturas", "distri_whatsapp": "+5492994555666"})
        self._antiguo(cliente)
        factura = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": cliente.id,
            "invoice_date": fields.Date.today() - timedelta(days=3),
            "invoice_line_ids": [Command.create({
                "product_id": self.product_a.id, "quantity": 1, "price_unit": 300.0,
                "tax_ids": [Command.clear()]})],
        })
        factura.action_post()
        self.assertEqual(self._pedido(cliente, precio=50.0).distri_semaforo, "verde")
        self.assertEqual(self._pedido(cliente, precio=70.0).distri_semaforo_motivos,
                         "Importe alto")

    def test_semaforo_usa_pedidos_confirmados_si_hay_suficientes(self):
        cliente = self.env["res.partner"].create({
            "name": "Cliente Con Pedidos", "distri_whatsapp": "+5492994777888"})
        self._antiguo(cliente)
        self._historia(cliente, [100.0])  # small history, overruled by the orders
        for _i in range(3):
            self._pedido(cliente, precio=100.0, confirmar=True)  # 1000 each
        self.assertEqual(self._pedido(cliente, precio=150.0).distri_semaforo, "verde")

    # ------------------------------------------------------------ hold: packing

    def test_retener_saca_de_la_cola_del_armador(self):
        order = self._pedido(confirmar=True)
        self.assertTrue(order._distri_para_armar())
        self._retener(order)
        self.assertTrue(order.distri_retenido)
        self.assertEqual(order.distri_retenido_por_id, self.env.user)
        self.assertEqual(order.distri_retenido_motivo, "Cliente nuevo, confirmar por teléfono")
        self.assertIn("retuvo el pedido: Cliente nuevo, confirmar por teléfono.",
                      self._notas(order))
        self.assertFalse(order._distri_armable())
        self.assertFalse(order._distri_para_armar())
        self.assertEqual(order.with_user(self.armador).sudo().distri_tomar(),
                         "Este pedido no se puede armar.")

        order.action_distri_liberar()
        self.assertFalse(order.distri_retenido)
        self.assertIn("liberó el pedido", self._notas(order))
        self.assertTrue(order._distri_para_armar())
        self._armar(order)
        self.assertEqual(order.distri_armado, "armado")

    def test_retener_presupuesto_web_no_se_arma_ni_se_aprueba(self):
        order = self._pedido(self.generico)
        order.distri_original = "Lo que mandó el cliente"
        self.assertTrue(order._distri_armable())
        self._retener(order)
        self.assertFalse(order._distri_armable())
        with self.assertRaisesRegex(UserError, "retenido"):
            order.action_confirm()
        order.action_distri_liberar()
        self.assertTrue(order._distri_armable())

    def test_retener_no_se_repite_ni_en_cancelado(self):
        order = self._pedido()
        self._retener(order)
        with self.assertRaisesRegex(UserError, "ya está retenido"):
            order.action_distri_retener()
        with self.assertRaisesRegex(UserError, "Escribí por qué"):
            order.distri_retener("   ")
        otro = self._pedido()
        otro.action_cancel()
        with self.assertRaisesRegex(UserError, "cancelado"):
            otro.action_distri_retener()
        with self.assertRaisesRegex(UserError, "no está retenido"):
            otro.action_distri_liberar()

    def test_cancelar_un_retenido(self):
        order = self._pedido(confirmar=True)
        self._retener(order)
        order.with_context(disable_cancel_warning=True).action_cancel()
        self.assertEqual(order.state, "cancel")
        self.assertNotIn(order, self.env["sale.order"].search(
            [("distri_revisar_en_lista", "=", True)]))

    # ------------------------------------------------------------ hold: invoicing

    def test_retenido_en_armado_termina_y_factura_al_liberar(self):
        order = self._pedido(confirmar=True)
        self._armar(order, terminar=False)
        self._retener(order)
        # already taken: the packer can still see and finish it
        self.assertTrue(order._distri_armable())
        self.assertIn("puede terminarlo", self._notas(order))
        self.assertEqual(order.with_user(self.armador).sudo().distri_terminar(2), "")
        self.assertEqual(order.distri_armado, "armado")
        self.assertFalse(self._facturas(order), "retenido: no sale la factura del armado")
        self.assertFalse(order.distri_factura_error)

        with self.assertRaisesRegex(UserError, "retenido: liberalo antes de facturar lo armado"):
            order.action_distri_facturar_armado()
        with self.assertRaisesRegex(UserError, "retenido"):
            order._create_invoices()

        order.action_distri_liberar()
        factura = self._facturas(order)
        self.assertEqual(len(factura), 1)
        self.assertEqual(order.distri_factura_auto_id, factura)
        self.assertEqual(factura.amount_total, 1000.0)

    def test_retenido_armado_no_factura_al_asignar_cliente(self):
        order = self._pedido(self.generico, confirmar=True)
        self._armar(order)
        self._retener(order)
        self.assertIn("Ya estaba armado", self._notas(order))
        order.with_context(distri_asignar_cliente=True).write({"partner_id": self.habitual.id})
        self.assertFalse(self._facturas(order))
        order.action_distri_liberar()
        self.assertEqual(len(self._facturas(order)), 1)

    def test_liberar_sin_armar_no_factura(self):
        order = self._pedido(confirmar=True)
        self._retener(order)
        order.action_distri_liberar()
        self.assertFalse(self._facturas(order))

    # ------------------------------------------------------------ hold: routing

    def test_retenido_no_entra_en_rutas(self):
        order = self._order(self.habitual, self.product_a)
        self._retener(order)
        route = self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "zone_ids": [Command.set(self.zona.ids)]})
        route.action_assign_orders()
        self.assertNotIn(order, route.stop_ids.order_id)
        with self.assertRaisesRegex(UserError, "retenido: liberalo antes de subirlo a una ruta"):
            route._add_orders(order)
        with self.assertRaisesRegex(UserError, "retenido"):
            order._distri_check_depot_delivery()

        order.action_distri_liberar()
        route.action_assign_orders()
        self.assertIn(order, route.stop_ids.order_id)

    def test_retener_lo_saca_de_la_ruta_que_no_salio(self):
        order = self._order(self.habitual, self.product_a)
        route = self._route(order)
        self.assertEqual(route.state, "draft")
        self._retener(order, motivo="Revisar el importe")
        self.assertFalse(route.stop_ids)
        left = route.left_order_ids
        self.assertEqual(left.order_id, order)
        self.assertEqual(left.reason, "Retenido: Revisar el importe")
        self.assertFalse(order.distri_retenido_rutas)
        order.action_distri_liberar()
        self.assertIn("había quedado fuera de la ruta", self._notas(order))

    def test_retener_en_ruta_que_ya_salio_avisa(self):
        order = self._order(self.habitual, self.product_a)
        route = self._route(order)
        route.sudo().state = "on_route"
        self._retener(order)
        self.assertEqual(route.stop_ids.order_id, order, "una ruta en la calle no se toca")
        self.assertEqual(order.distri_retenido_rutas, route.name)

    # ------------------------------------------------------------ access

    def test_solo_administracion_y_gerencia(self):
        order = self._pedido()
        como_vendedor = order.with_user(self.vendedor)
        with self.assertRaisesRegex(UserError, "Solo administración y gerencia"):
            como_vendedor.action_distri_retener()
        with self.assertRaisesRegex(UserError, "Solo administración y gerencia"):
            como_vendedor.distri_retener("Sospechoso")
        with self.assertRaises(AccessError):
            self.env["distri.retener.wizard"].with_user(self.vendedor).create({
                "order_id": order.id, "motivo": "x"})

        # gerencia (implies administración) holds and releases
        como_gerente = order.with_user(self.gerente)
        accion = como_gerente.action_distri_retener()
        self.assertEqual(accion["res_model"], "distri.retener.wizard")
        self.env["distri.retener.wizard"].with_user(self.gerente).create({
            "order_id": order.id, "motivo": "Lo miro"}).action_retener()
        self.assertEqual(order.distri_retenido_por_id, self.gerente)
        como_gerente.action_distri_liberar()
        self.assertFalse(order.distri_retenido)

        admin = self.env.ref("distri_pedido.grupo_administracion")
        accion = self.env.ref("distri_pedido.action_pedidos_revisar")
        for menu in ("menu_pedidos_revisar", "menu_app_pedidos_revisar"):
            self.assertEqual(self.env.ref("distri_pedido.%s" % menu).group_ids, admin)
        self.assertEqual(self.env.ref("distri_pedido.menu_pedidos_revisar").action, accion)

    def test_abrir_chat(self):
        order = self._pedido()
        accion = order.action_distri_abrir_chat()
        self.assertEqual(accion["url"], "https://wa.me/5492994222444")
        sin = self.env["res.partner"].create({"name": "Sin teléfono"})
        with self.assertRaisesRegex(UserError, "no tiene un WhatsApp"):
            self._pedido(sin).action_distri_abrir_chat()

    # ------------------------------------------------------------ the list

    def _fechar(self, order, cuando):
        self.env.cr.execute("UPDATE sale_order SET date_order = %s WHERE id = %s",
                            (cuando, order.id))
        order.invalidate_recordset(["date_order"])

    def test_dominio_de_la_lista(self):
        Order = self.env["sale.order"]
        hoy = self._pedido()
        ayer = self._pedido()
        self._fechar(ayer, semaforo.inicio_del_dia_utc(1) + timedelta(hours=1))
        viejo = self._pedido()
        self._fechar(viejo, semaforo.inicio_del_dia_utc(3))
        viejo_retenido = self._pedido()
        self._fechar(viejo_retenido, semaforo.inicio_del_dia_utc(5))
        self._retener(viejo_retenido)
        cancelado = self._pedido()
        cancelado.action_cancel()
        rojo = self._pedido(self.generico)

        todos = hoy | ayer | viejo | viejo_retenido | cancelado | rojo
        lista = Order.search([("distri_revisar_en_lista", "=", True), ("id", "in", todos.ids)],
                             order="distri_semaforo_orden, date_order desc, id desc")
        self.assertEqual(lista, rojo | hoy | ayer | viejo_retenido)
        self.assertEqual(lista[0], rojo, "los rojos primero")
        self.assertEqual(lista[1:].ids, (hoy | ayer | viejo_retenido).ids,
                         "dentro del color, el más nuevo primero")
        # the compute agrees with the search
        self.assertEqual(todos.filtered("distri_revisar_en_lista"), lista)

        fuera = Order.search([("distri_revisar_en_lista", "=", False), ("id", "in", todos.ids)])
        self.assertEqual(fuera, viejo | cancelado)
        de_hoy = Order.search([("distri_revisar_hoy", "=", True), ("id", "in", todos.ids)])
        self.assertEqual(de_hoy, hoy | rojo)
        rojos = Order.search([("distri_semaforo", "=", "rojo"), ("id", "in", todos.ids)])
        self.assertEqual(rojos, rojo)
        retenidos = Order.search([("distri_retenido", "=", True), ("id", "in", todos.ids)])
        self.assertEqual(retenidos, viejo_retenido)

    def test_origen(self):
        backend = self._pedido()
        self.assertEqual(backend.distri_revisar_origen, "backend")
        web = self._pedido(self.generico)
        web.sudo().distri_wa_state = "direct"
        self.assertEqual(web.distri_revisar_origen, "web")
        staff = self._pedido()
        staff.sudo().distri_wa_state = "staff"
        self.assertEqual(staff.distri_revisar_origen, "staff")

    # ------------------------------------------------------------ alerts

    def _alertas(self, order, usuario=None):
        return order.activity_ids.filtered(
            lambda a: a.summary == RESUMEN_ALERTA and (not usuario or a.user_id == usuario))

    def test_alerta_rojo_una_sola_vez(self):
        order = self._pedido(self.generico)
        self.assertTrue(order.distri_revisar_alertado)
        self.assertEqual(len(self._alertas(order, self.gerente)), 1)
        self.assertNotIn(self.env.ref("base.user_root"), self._alertas(order).user_id)
        # again: no new activity
        order._distri_revisar_alertar()
        self.assertEqual(len(self._alertas(order, self.gerente)), 1)
        self._alertas(order).action_feedback(feedback="visto")
        order._distri_revisar_alertar()
        self.assertFalse(self._alertas(order))

    def test_sin_alerta_si_no_es_rojo_o_esta_apagada(self):
        self.assertFalse(self._alertas(self._pedido()))
        self.env["ir.config_parameter"].sudo().set_param(PARAM_ALERTA_ROJOS, "0")
        order = self._pedido(self.generico)
        self.assertEqual(order.distri_semaforo, "rojo")
        self.assertFalse(self._alertas(order))
        self.assertFalse(order.distri_revisar_alertado)
