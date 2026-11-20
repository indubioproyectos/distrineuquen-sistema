"""19.0.1.121.0 (owner, from real use): the colour of «Pedidos para revisar»
is the order status, not the risk.

- green confirmed, yellow not confirmed, red not confirmed and without a
  real customer, grey cancelled (sale.order.distri_revisar_color, stored);
- what needs action first, the confirmed ones last (distri_revisar_orden);
- the risk reasons are chips of the orders not confirmed yet, and «Sin
  WhatsApp» is no longer one;
- the gerencia alert still follows the risk (distri_semaforo), never the
  status colour, and only on create.
"""
from datetime import timedelta

from odoo import Command, fields
from odoo.fields import Domain
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.revisar import card
from odoo.addons.distri_pedido.controllers.tablero import pendientes_domain, sin_cliente_domain
from odoo.addons.distri_pedido.models import semaforo
from odoo.addons.distri_pedido.models.revisar import RESUMEN_ALERTA
from odoo.addons.distri_pedido.models.sale_order import SOCIO_SIN_ASIGNAR


@tagged("post_install", "-at_install", "distri_v121")
class TestRevisarEstado(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.gerente = new_test_user(
            cls.env, login="gerente_v121", name="Gerente V121",
            groups="base.group_user,distri_pedido.grupo_gerencia", **company)
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        Partner = cls.env["res.partner"]
        cls.generico = Partner.create({
            "name": SOCIO_SIN_ASIGNAR, "distri_generic_partner": True,
            "distri_generic_kind": "retail"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto Revisar V121", "list_price": 100, "type": "consu",
            "invoice_policy": "order", "taxes_id": [Command.clear()]})
        # a regular customer with history and WhatsApp
        cls.habitual = Partner.create({"name": "Kiosco Habitual V121",
                                       "distri_whatsapp": "+5492994222121"})
        # a regular customer with history and no WhatsApp on file
        cls.sin_wa = Partner.create({"name": "Despensa Sin WA V121"})
        for partner in (cls.habitual, cls.sin_wa):
            cls.env.cr.execute("UPDATE res_partner SET create_date = %s WHERE id = %s",
                               (fields.Datetime.now() - timedelta(days=400), partner.id))
            partner.invalidate_recordset(["create_date"])
            cls.env["distri.cuenta.movimiento"].sudo().create([{
                "partner_id": partner.id, "tipo": "factura", "debe": 1000.0,
                "fecha": fields.Date.today() - timedelta(days=10 + i),
                "comprobante": "FA-V121-%s-%s" % (partner.id, i)} for i in range(3)])

    def _pedido(self, partner, precio=90.0):
        return self.env["sale.order"].create({
            "partner_id": partner.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 10, "price_unit": precio,
                "tax_ids": [Command.clear()]})],
        })

    def _alertas(self, order):
        return order.activity_ids.filtered(lambda a: a.summary == RESUMEN_ALERTA)

    # ------------------------------------------------------------ pure rules

    def test_reglas_puras(self):
        color = semaforo.color_estado
        self.assertEqual(color("draft"), "amarillo")
        self.assertEqual(color("sent"), "amarillo")
        self.assertEqual(color("draft", sin_cliente=True), "rojo")
        self.assertEqual(color("sale"), "verde")
        self.assertEqual(color("sale", sin_cliente=True), "verde", "confirmed is confirmed")
        self.assertEqual(color("cancel", sin_cliente=True), "gris")
        self.assertEqual(semaforo.chips("Cliente nuevo · Sin WhatsApp · Importe alto"),
                         ["Cliente nuevo", "Importe alto"], "a risk stored before 19.0.1.121.0")
        self.assertEqual(semaforo.chips("Sin cliente asignado · WhatsApp sin teléfono"),
                         ["WhatsApp sin teléfono"])
        self.assertEqual(semaforo.chips(False), [])
        # «Sin WhatsApp» is no risk any more
        self.assertNotIn("sin_whatsapp", semaforo.evaluar.__code__.co_varnames)

    # ------------------------------------------------------------ colour by state

    def test_color_por_estado(self):
        order = self._pedido(self.habitual)
        self.assertEqual((order.distri_revisar_color, order.distri_revisar_orden), ("amarillo", 2))
        order.action_quotation_sent()
        self.assertEqual(order.distri_revisar_color, "amarillo")
        order.action_confirm()
        # stored and repainted by the confirmation itself: the screen sees it
        self.assertEqual((order.distri_revisar_color, order.distri_revisar_orden), ("verde", 3))
        self.env.flush_all()
        self.assertEqual(self.env["sale.order"].search_count([
            ("id", "=", order.id), ("distri_revisar_color", "=", "verde")]), 1)

        web = self._pedido(self.generico)
        self.assertEqual((web.distri_revisar_color, web.distri_revisar_orden), ("rojo", 1))
        web.with_context(distri_asignar_cliente=True).partner_id = self.habitual
        self.assertEqual(web.distri_revisar_color, "amarillo", "a real customer now")
        web.action_cancel()
        self.assertEqual((web.distri_revisar_color, web.distri_revisar_orden), ("gris", 4))

    def test_orden_de_la_lista(self):
        confirmado = self._pedido(self.habitual)
        confirmado.action_confirm()
        amarillo = self._pedido(self.habitual)
        rojo = self._pedido(self.generico)
        todos = confirmado | amarillo | rojo
        lista = self.env["sale.order"].search(
            [("distri_revisar_en_lista", "=", True), ("id", "in", todos.ids)],
            order="distri_revisar_orden, date_order desc, id desc")
        self.assertEqual(lista.ids, [rojo.id, amarillo.id, confirmado.id])

        # Inicio: the badge counts what is missing confirmation, the banner
        # what is missing the customer
        Order = self.env["sale.order"]
        pendientes = Order.search(pendientes_domain(self.env) & Domain("id", "in", todos.ids))
        self.assertEqual(pendientes, rojo | amarillo)
        sin_cliente = Order.search(sin_cliente_domain(self.env) & Domain("id", "in", todos.ids))
        self.assertEqual(sin_cliente, rojo)

    # ------------------------------------------------------------ chips

    def test_sin_whatsapp_ya_no_es_una_etiqueta(self):
        order = self._pedido(self.sin_wa)
        self.assertEqual(order.distri_semaforo, "verde")
        self.assertFalse(order.distri_semaforo_motivos)
        c = card(order)
        self.assertEqual((c["color"], c["color_label"], c["chips"]),
                         ("amarillo", "Sin confirmar", []))
        # a risk stored before the upgrade still says it: the card drops it
        order.sudo().distri_semaforo_motivos = "Sin WhatsApp · Importe alto"
        self.assertEqual(card(order)["chips"], ["Importe alto"])

    def test_etiquetas_solo_sin_confirmar(self):
        nuevo = self.env["res.partner"].create({"name": "Almacén Recién Creado V121",
                                                "distri_whatsapp": "+5492994333121"})
        order = self._pedido(nuevo)
        c = card(order)
        self.assertEqual(c["color"], "amarillo")
        self.assertEqual(c["chips"], ["Cliente nuevo", "Sin compras anteriores"])
        self.assertTrue(c["pendiente"])
        self.assertFalse(c["compact"])
        order.action_confirm()
        c = card(order)
        self.assertEqual((c["color"], c["color_label"]), ("verde", "Confirmado"))
        self.assertEqual(c["chips"], [], "a confirmed order shows no risk chips")
        self.assertFalse(c["pendiente"])
        self.assertTrue(c["compact"])
        # the risk itself stays (the alert and the backend filter read it)
        self.assertEqual(order.distri_semaforo, "rojo")

    def test_sin_cliente_sin_etiqueta_repetida(self):
        c = card(self._pedido(self.generico))
        self.assertEqual((c["color"], c["color_label"]), ("rojo", "Sin cliente"))
        self.assertNotIn("Sin cliente asignado", c["chips"])

    # ------------------------------------------------------------ gerencia alert

    def test_alerta_sigue_al_riesgo(self):
        # a risky new order: gerencia gets the activity
        nuevo = self.env["res.partner"].create({"name": "Cliente Riesgo V121"})
        riesgoso = self._pedido(nuevo)
        self.assertEqual(riesgoso.distri_semaforo, "rojo")
        self.assertTrue(riesgoso.distri_revisar_alertado)
        self.assertIn(self.gerente, self._alertas(riesgoso).user_id)
        self.assertIn("Cliente nuevo", self._alertas(riesgoso)[0].note)
        # a plain order not confirmed yet (no WhatsApp on file either): nothing
        comun = self._pedido(self.sin_wa)
        self.assertEqual(comun.distri_revisar_color, "amarillo")
        self.assertFalse(self._alertas(comun))
        self.assertFalse(comun.distri_revisar_alertado)
        # confirming or repainting the status never alerts
        comun.action_confirm()
        self.env.add_to_compute(self.env["sale.order"]._fields["distri_revisar_color"], comun)
        self.env.flush_all()
        self.assertFalse(self._alertas(comun))
