from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import freeze_time, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.factura_armado import RESUMEN_ACTIVIDAD
from odoo.addons.distri_pedido.models.sale_order import SOCIO_SIN_ASIGNAR
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid


@tagged("post_install", "-at_install", "distri_factura_armado")
class TestFacturaArmado(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls.armador = new_test_user(
            cls.env, login="armador_factura", name="Armador Factura",
            groups="base.group_user,distri_pedido.grupo_armador",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)],
        )
        cls._setup_reparto()
        cls.partner = cls.partner_a
        cls.generico = cls.env["res.partner"].create({
            "name": SOCIO_SIN_ASIGNAR, "distri_generic_partner": True,
            "distri_generic_kind": "retail"})
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Factura armado", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})

    def _pedido(self, partner=None, confirmar=True):
        order = self.env["sale.order"].create({
            "partner_id": (partner or self.partner).id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": 10,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": 5,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ],
        })
        if confirmar:
            order.action_confirm()
        return order

    def _armar(self, order, faltantes=None, bultos=2):
        """El recorrido del armador, como en controllers/armado.py (sudo)."""
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        for producto, falta in (faltantes or {}).items():
            linea = pedido.order_line.filtered(lambda l: l.product_id == producto)
            pedido.distri_marcar(linea, falta)
        self.assertEqual(pedido.distri_terminar(bultos), "")
        return order

    def _facturas(self, order):
        return order.invoice_ids.filtered(lambda f: f.move_type == "out_invoice")

    # ----------------------------------------------------------------

    def test_boton_armar_abre_la_pantalla_del_armador(self):
        order = self._pedido()
        accion = order.action_distri_abrir_armado()
        self.assertEqual(accion["type"], "ir.actions.act_url")
        self.assertEqual(accion["url"], "/armado/pedido/%s" % order.id)
        menu = self.env.ref("distri_pedido.menu_armado_app")
        self.assertIn(self.env.ref("distri_pedido.grupo_armador"), menu.group_ids)

    # ------------------------------------------ dejar para mas tarde

    def _otro_armador(self):
        return new_test_user(
            self.env, login="armador_otro", name="Otro Armador",
            groups="base.group_user,distri_pedido.grupo_armador",
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)],
        )

    def _notas(self, order):
        return order.message_ids.mapped(lambda m: m.body and str(m.body) or "")

    def test_dejar_para_mas_tarde_el_armador(self):
        order = self._pedido()
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        self.assertFalse(order._distri_editable(), "tomado: el cliente ya no lo cambia")
        linea_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        linea_b = order.order_line.filtered(lambda l: l.product_id == self.product_b)
        pedido.distri_marcar(linea_a, 3)
        pedido.distri_marcar(linea_b, 5)
        telefono = pedido.distri_agregar(self.product_b, 2, self.product_b.uom_id, clave="k1")

        self.assertEqual(pedido.distri_dejar_para_mas_tarde(), "")
        self.assertEqual(order.distri_armado, "pendiente")
        self.assertFalse(order.distri_armador_id)
        self.assertFalse(order.distri_armado_inicio)
        # 19.0.1.98.0: lo marcado queda (faltantes y lo agregado por telefono)
        self.assertEqual((linea_a.distri_faltante, linea_a.product_uom_qty), (3, 7))
        self.assertEqual((linea_b.distri_faltante, linea_b.product_uom_qty), (5, 0))
        self.assertTrue(telefono.exists())
        self.assertEqual((telefono.product_uom_qty, telefono.distri_por_telefono), (2, True))
        # el cliente lo puede volver a cambiar
        self.assertTrue(order._distri_editable())
        self.assertIn("Armador Factura dejó el pedido para más tarde; vuelve a pendiente. "
                      "Queda lo marcado: 2 con faltante.", " ".join(self._notas(order)))

        # doble toque: el primero ya lo solto
        self.assertEqual(pedido.distri_dejar_para_mas_tarde(), "")
        self.assertEqual(order.distri_armado, "pendiente")

        # libre para otro armador
        otro = self._otro_armador()
        self.assertEqual(order.with_user(otro).sudo().distri_tomar(), "")
        self.assertEqual(order.distri_armador_id, otro)
        self.assertEqual(order.distri_armado, "en_armado")

    def test_agregar_por_unidad_solo_enteras(self):
        """19.0.1.93.0 (E2E-C C10): a product sold by units takes whole units,
        like /pedido; a product by weight may be fractional."""
        order = self._pedido()
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        unidad = self.env.ref("uom.product_uom_unit")
        with self.assertRaisesRegex(UserError, "se vende por unidad"):
            pedido.distri_agregar(self.product_b, 0.5, unidad, clave="media")
        docena = self.env.ref("uom.product_uom_dozen", raise_if_not_found=False)
        if docena:
            with self.assertRaisesRegex(UserError, "se vende por unidad"):
                pedido.distri_agregar(self.product_b, 1.5, docena, clave="docena")
        self.assertTrue(pedido.distri_agregar(self.product_b, 2.0, unidad, clave="dos"))
        from odoo.addons.distri_pedido.models.armado import _por_unidades
        self.assertFalse(_por_unidades(self.env, self.env.ref("uom.product_uom_kgm")))

    def test_dejar_para_mas_tarde_sin_faltantes(self):
        order = self._pedido()
        pedido = order.with_user(self.armador).sudo()
        pedido.distri_tomar()
        self.assertEqual(pedido.distri_dejar_para_mas_tarde(), "")
        notas = " ".join(self._notas(order))
        self.assertIn("dejó el pedido para más tarde; vuelve a pendiente.", notas)
        self.assertNotIn("faltante", notas)

    def test_dejar_para_mas_tarde_administracion(self):
        order = self._pedido()
        pedido = order.with_user(self.armador).sudo()
        pedido.distri_tomar()
        linea_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        pedido.distri_marcar(linea_a, 4)
        # el boton del backend, como administracion (sin sudo)
        self.assertTrue(order.action_distri_dejar_para_mas_tarde())
        self.assertEqual(order.distri_armado, "pendiente")
        self.assertFalse(order.distri_armador_id)
        # 19.0.1.98.0: the shortage stays
        self.assertEqual((linea_a.distri_faltante, linea_a.product_uom_qty), (4, 6))
        self.assertIn("%s dejó el pedido para más tarde; vuelve a pendiente. "
                      "Queda lo marcado: 1 con faltante." % self.env.user.name,
                      " ".join(self._notas(order)))
        # doble toque en el backend tampoco es un error
        self.assertTrue(order.action_distri_dejar_para_mas_tarde())

    def test_dejar_para_mas_tarde_otro_armador_no(self):
        order = self._pedido()
        order.with_user(self.armador).sudo().distri_tomar()
        linea_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        order.with_user(self.armador).sudo().distri_marcar(linea_a, 2)
        otro = order.with_user(self._otro_armador()).sudo()
        self.assertEqual(
            otro.distri_dejar_para_mas_tarde(),
            "Este pedido lo está armando Armador Factura: solo esa persona o "
            "administración lo pueden dejar para más tarde.")
        self.assertEqual(order.distri_armado, "en_armado")
        self.assertEqual(order.distri_armador_id, self.armador)
        self.assertEqual(linea_a.distri_faltante, 2)
        with self.assertRaises(UserError):
            otro.action_distri_dejar_para_mas_tarde()

    def test_dejar_para_mas_tarde_armado_o_cancelado_no(self):
        armado = self._armar(self._pedido())
        pedido = armado.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_dejar_para_mas_tarde(), "Este pedido ya está armado.")
        self.assertEqual(armado.distri_armado, "armado")

        cancelado = self._pedido()
        self.assertEqual(cancelado.with_user(self.armador).sudo().distri_tomar(), "")
        cancelado.sudo().write({"state": "cancel"})
        self.assertEqual(cancelado.with_user(self.armador).sudo().distri_dejar_para_mas_tarde(),
                         "Este pedido se canceló.")
        self.assertEqual(cancelado.distri_armado, "en_armado")
        with self.assertRaises(UserError):
            cancelado.action_distri_dejar_para_mas_tarde()

    def test_factura_una_vez_por_lo_armado(self):
        order = self._armar(self._pedido(), {self.product_a: 3, self.product_b: 5})
        factura = self._facturas(order)
        self.assertEqual(len(factura), 1)
        self.assertEqual(factura.state, "posted")
        self.assertEqual(order.distri_factura_auto_id, factura)
        self.assertFalse(order.distri_factura_error)
        lineas = factura.invoice_line_ids.filtered(lambda l: l.display_type == "product")
        self.assertEqual(lineas.product_id, self.product_a, "lo que faltó todo no se factura")
        self.assertEqual(lineas.quantity, 7)
        linea_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        self.assertEqual(factura.amount_total, 7 * linea_a.price_unit)
        self.assertEqual(factura.amount_total, order.amount_total)
        # la entrega del deposito sale por lo armado
        self.assertTrue(order.picking_ids)
        self.assertTrue(all(p.state == "done" for p in order.picking_ids))
        linea_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        self.assertEqual(linea_a.qty_delivered, 7)
        self.assertEqual(order.invoice_status, "invoiced")

        # doble toque, reasignacion del cliente, reintento: nunca otra
        self.assertEqual(order.with_user(self.armador).sudo().distri_terminar(2), "")
        order.partner_id = self.partner_b
        order._distri_facturar_armado()
        self.assertEqual(len(self._facturas(order)), 1)
        with self.assertRaises(UserError):
            order.action_distri_facturar_armado()

    def test_factura_con_la_fecha_argentina(self):
        # 22:30 in Neuquén is already the next day in UTC (the server)
        with freeze_time("2026-09-27 01:30:00"):
            order = self._armar(self._pedido())
        self.assertEqual(str(self._facturas(order).invoice_date), "2026-09-26")

    def test_como_pagar_descuenta_el_saldo_a_favor(self):
        order = self._armar(self._pedido())
        factura = self._facturas(order)
        self.assertEqual(order._distri_importe_a_pagar(), factura.amount_residual)
        # a credit from the imported Online history covers part of it
        Movimiento = self.env["distri.cuenta.movimiento"].sudo()
        Movimiento.create({"partner_id": self.partner.id, "fecha": "2026-09-01",
                           "comprobante": "REC-1", "tipo": "pago", "haber": 1000})
        self.assertEqual(order._distri_importe_a_pagar(), factura.amount_residual - 1000)
        # ...or all of it: nothing to transfer, as the account page says
        Movimiento.create({"partner_id": self.partner.id, "fecha": "2026-09-02",
                           "comprobante": "REC-2", "tipo": "pago", "haber": 5000})
        self.assertEqual(order._distri_importe_a_pagar(), 0.0)

    def test_sin_cliente_espera_la_asignacion(self):
        order = self._pedido(partner=self.generico, confirmar=False)
        self._armar(order, {self.product_a: 4})
        self.assertEqual(order.distri_armado, "armado")
        self.assertFalse(order.invoice_ids)
        self.assertFalse(order.distri_factura_error)

        order.sudo().with_context(distri_asignar_cliente=True).partner_id = self.partner
        self.assertFalse(order.invoice_ids, "sin confirmar todavia no se factura")
        order.action_confirm()
        factura = self._facturas(order)
        self.assertEqual(len(factura), 1)
        self.assertEqual(factura.state, "posted")
        self.assertEqual(factura.partner_id, self.partner)
        cantidades = {l.product_id: l.quantity for l in factura.invoice_line_ids
                      if l.display_type == "product"}
        self.assertEqual(cantidades, {self.product_a: 6, self.product_b: 5})

    def test_confirmado_sin_cliente_factura_al_asignar(self):
        order = self._pedido(partner=self.generico)
        self._armar(order)
        self.assertFalse(order.invoice_ids)
        order.sudo().with_context(distri_asignar_cliente=True).partner_id = self.partner
        self.assertEqual(len(self._facturas(order)), 1)
        self.assertEqual(self._facturas(order).amount_total, order.amount_total)

    def test_asistente_asignar_y_aprobar_factura_lo_armado(self):
        """Packing finished before the customer was known: the wizard's
        «Asignar y aprobar» assigns, confirms and the invoice comes out."""
        order = self._pedido(partner=self.generico, confirmar=False)
        self._armar(order, {self.product_a: 4})
        self.assertFalse(order.invoice_ids)
        cliente = self.env["res.partner"].create({"name": "Cliente del asistente"})
        accion = order.action_distri_asignar_cliente()
        asistente = self.env[accion["res_model"]].with_context(accion["context"]).create({
            "modo": "existente", "partner_id": cliente.id})
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.pedido_requiere_whatsapp", "0")
        accion = asistente.action_asignar_y_aprobar()
        # the customer has no WhatsApp: «Asignar y aprobar» asks for it (19.0.1.84.0)
        self.assertEqual(accion["res_model"], "distri.wa.confirm.wizard")
        self.env[accion["res_model"]].with_context(accion["context"]).create({}).action_skip()
        self.assertEqual(order.partner_id, cliente)
        self.assertEqual(order.state, "sale")
        factura = self._facturas(order)
        self.assertEqual(len(factura), 1)
        self.assertEqual(factura.partner_id, cliente)
        self.assertEqual(factura.state, "posted")

    def test_respeta_la_factura_hecha_a_mano(self):
        # un pedido armado antes de la factura automatica, facturado a mano
        order = self._pedido()
        order.sudo().write({"distri_armado": "armado", "distri_bultos": 1})
        manual = order._create_invoices()
        order.partner_id = self.partner_b
        order._distri_facturar_armado()
        self.assertEqual(order.invoice_ids, manual)
        self.assertFalse(order.distri_factura_auto_id)
        self.assertEqual(manual.state, "draft")
        with self.assertRaises(UserError):
            order.action_distri_facturar_armado()

    def test_factura_a_mano_despues_de_un_error(self):
        order = self._pedido()
        SaleOrder = type(self.env["sale.order"])
        with patch.object(SaleOrder, "_create_invoices", side_effect=UserError("falla")), \
                self.assertLogs("odoo.addons.distri_pedido.models.factura_armado", "WARNING"):
            self._armar(order)
        manual = order._create_invoices()
        order.sudo().partner_id = self.partner_b
        self.assertEqual(order.invoice_ids, manual)

    def test_parametro_deja_en_borrador(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.factura_auto_emitir", "0")
        order = self._armar(self._pedido())
        self.assertEqual(self._facturas(order).state, "draft")

    def test_error_no_frena_al_armador(self):
        order = self._pedido()
        SaleOrder = type(self.env["sale.order"])
        with patch.object(SaleOrder, "_create_invoices", side_effect=ValueError("sin diario")), \
                self.assertLogs("odoo.addons.distri_pedido.models.factura_armado", "WARNING"):
            self._armar(order, {self.product_a: 1})
        self.assertEqual(order.distri_armado, "armado")
        self.assertTrue(order.sudo().distri_receipt_hash)
        self.assertFalse(order.invoice_ids)
        self.assertIn("sin diario", order.distri_factura_error)
        actividad = order.activity_ids.filtered(lambda a: a.summary == RESUMEN_ACTIVIDAD)
        # 19.0.1.116.0: one for each person of administración/gerencia
        avisados = order._distri_avisar_factura_a()
        self.assertTrue(avisados)
        self.assertEqual(actividad.user_id, avisados)
        self.assertEqual(len(actividad), len(avisados))
        self.assertFalse(any(p.state == "done" for p in order.picking_ids),
                         "la entrega se deshace junto con la factura")

        # otro intento automatico no duplica la actividad
        with patch.object(SaleOrder, "_create_invoices", side_effect=UserError("otra vez")):
            order._distri_facturar_armado()
        self.assertEqual(len(order.activity_ids.filtered(
            lambda a: a.summary == RESUMEN_ACTIVIDAD)), len(avisados))

        # el administrador reintenta desde el pedido
        order.action_distri_facturar_armado()
        self.assertEqual(len(self._facturas(order)), 1)
        self.assertEqual(self._facturas(order).amount_total, order.amount_total)
        self.assertFalse(order.distri_factura_error)
        self.assertFalse(order.activity_ids.filtered(lambda a: a.summary == RESUMEN_ACTIVIDAD))

    def test_reparto_con_la_factura_del_armado(self):
        receivable = self.company_data["default_account_receivable"]
        vieja = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "invoice_date": "2026-01-10",
            "invoice_line_ids": [Command.create({
                "name": "deuda vieja", "quantity": 1, "price_unit": 500,
                "tax_ids": [Command.clear()]})],
        })
        vieja.action_post()
        order = self._armar(self._pedido(), {self.product_b: 5})
        factura = order.distri_factura_auto_id
        self.assertEqual(factura.amount_residual, 1000)

        route = self._route(order)
        stop = route.stop_ids
        self.assertEqual(stop.order_amount, 1000)
        self.assertEqual(stop.previous_debt, 500)

        linea = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        self._sync([
            self._event(route, "visit", self._visit(stop, returns_answer="yes", returns=[
                {"uuid": _uuid(), "line_id": linea.id, "qty": 2, "reason": "Vencido"}],
                cash=900)),
            self._event(route, "close", {}),
        ])
        self.assertEqual(route.state, "closed")
        self.assertEqual(stop.expected_balance, 1300)
        route.cash_counted = 900
        stop.return_ids.action_received()
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()

        self.assertEqual(route.state, "settled")
        self.assertEqual(vieja.amount_residual, 0, "primero la factura mas vieja")
        # la nota de crédito se emite y se aplica antes que el efectivo
        self.assertEqual(factura.amount_residual, 400)
        self.assertTrue(route.payment_ids.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable).reconciled)
        nota = stop.return_ids.credit_note_id
        self.assertEqual(nota.move_type, "out_refund")
        self.assertEqual(nota.reversed_entry_id, factura)
        self.assertEqual(nota.amount_total, 200)
        self.assertEqual(nota.state, "posted")
        self.assertEqual(factura.amount_residual, 400)
