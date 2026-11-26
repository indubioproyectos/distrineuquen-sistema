"""19.0.1.116.0: fixes from the 60-order stress test on the practice copy.

1. Pasted AgilPedido message: a «May …» line whose full name is a temporary
   AgilPedido product takes that product at its own price (the 11 cases of
   the stress test's catalogo_autoconsistencia.json); «Corregir producto» to
   a «May …» product clears «May»; the equivalence learned keeps it clear.
2. «Corregir producto» with the same product a «Parecido al 94 %» line
   already proposed resolves the line (nothing learned).
3. /rutas says the customer is missing when the order is still on the
   generic web contact.
4. Cash amounts up to $ 10.000.000.000, with a message when exceeded.
5. The route's remitos follow the single remito's access rule.
6. A failed auto-invoice at packing warns every person of administración
   and gerencia (not the technical admin) and shows on /tablero.
7. (19.0.1.117.0) A remito longer than one page starts on a new page in the
   route's remitos, and so does the next one.
Synthetic data only.
"""
import re

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import LISTA_OCULTA
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles, tile_badges
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_agil_equivalencia import mensaje
from odoo.addons.distri_pedido.tests.test_catalogo_agil import AgilMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar

# the 11 temporary «May …» products of the stress test that were taken as
# the normal product + «May» (practica/estres/out/catalogo_autoconsistencia.json)
MAY_NOMBRES = [
    "May Amoxidal 500",
    "May amoxixilina ( FABAMOX) 500 mg. x 8",
    "May Bicarbonato",
    "May Caramelo garganta x10",
    "May Encendedor Neoclick x 25 u.",
    "May Ibuevanol x 10",
    "May Ocb premium",
    "May Parche 3",
    "May Termómetro digital",
    "May Té Vick Forte",
    "May Té Vivita Forte x 24u",
]


@tagged("post_install", "-at_install", "distri_v116")
class TestV116Importar(AgilMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Pricelist = cls.env["product.pricelist"]
        currency = cls.env.company.currency_id.id
        cls.oculta = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA, "currency_id": currency,
                              "distri_mayorista": True})
        cls.lista = Pricelist.create({"name": "Lista V116", "currency_id": currency})
        cls.partner = cls.env["res.partner"].create({"name": "KIOSCO V116"})
        cls.unidad = cls.env.ref("uom.product_uom_unit")

    def _producto(self, nombre, precio, temporal=False, numero=0):
        valores = {"name": nombre, "list_price": precio, "type": "consu", "sale_ok": True,
                   "uom_id": self.unidad.id, "taxes_id": [Command.clear()]}
        if temporal:
            valores.update(distri_agil_temporal=True, distri_agil_id=str(99116000 + numero))
        return self.env["product.template"].create(valores)

    def _asistente(self, *renglones):
        wizard = self.env["distri.importar.pedido"].create({
            "mensaje": mensaje(*renglones, total=1000), "partner_id": self.partner.id,
            "pricelist_id": self.lista.id})
        wizard.action_analizar()
        return wizard

    def test_1_may_temporal_por_su_nombre_completo(self):
        """The 11 cases: with the AgilPedido catalog on, the line takes the
        temporary «May …» product at its own price, without «May»."""
        temporales = {}
        for numero, nombre in enumerate(MAY_NOMBRES):
            # the normal product (no wholesale price) and the temporary copy
            self._producto(nombre[4:], 100.0 + numero)
            temporales[nombre] = self._producto(nombre, 1000.0 + numero, temporal=True,
                                                numero=numero)
        self._switch("1")
        wizard = self._asistente(*["- 1 x %s ($ 1,000.00)" % n for n in MAY_NOMBRES])
        self.assertEqual(len(wizard.linea_ids), len(MAY_NOMBRES))
        for linea in wizard.linea_ids:
            self.assertEqual(linea.product_tmpl_id, temporales[linea.nombre_mensaje],
                             linea.nombre_mensaje)
            self.assertFalse(linea.es_mayorista, linea.nombre_mensaje)
            self.assertEqual(linea.parecido, 100, linea.nombre_mensaje)
            self.assertFalse(linea._distri_para_corregir(), linea.nombre_mensaje)
        accion = wizard.action_crear_presupuesto()
        pedido = self.env["sale.order"].browse(accion["res_id"])
        precios = {l.product_id.product_tmpl_id: l.price_unit for l in pedido.order_line}
        for nombre, plantilla in temporales.items():
            self.assertEqual(precios[plantilla], plantilla.list_price, nombre)

    def test_1_corregir_a_un_may_y_aprender(self):
        normal = self._producto("Yerba Pruebav116 1 kg", 100.0)
        temporal = self._producto("May Yerba Pruebav116 1 kg", 900.0, temporal=True, numero=50)
        # catalog off: the temporary copy does not count, «May» of the normal one
        self._switch("0")
        wizard = self._asistente("- 2 x May Yerba Pruebav116 1 kg ($ 1,800.00)")
        linea = wizard.linea_ids
        self.assertEqual((linea.product_tmpl_id, linea.es_mayorista), (normal, True))
        with self.assertRaisesRegex(UserError, "precio mayorista"):
            wizard.action_crear_presupuesto()
        # «Corregir producto» to the «May …» product: no «May» on top
        linea._distri_corregir(temporal.product_variant_id, self.unidad)
        self.assertEqual((linea.product_tmpl_id, linea.es_mayorista), (temporal, False))
        self.assertFalse(linea._distri_para_corregir())
        accion = wizard.action_crear_presupuesto()
        pedido = self.env["sale.order"].browse(accion["res_id"])
        self.assertEqual(pedido.order_line.product_id.product_tmpl_id, temporal)
        self.assertEqual(pedido.order_line.price_unit, 900.0)
        # the learned equivalence: same product, without «May» either
        wizard = self._asistente("- 1 x May Yerba Pruebav116 1 kg ($ 900.00)")
        linea = wizard.linea_ids
        self.assertTrue(linea.equivalencia_id)
        self.assertEqual((linea.product_tmpl_id, linea.es_mayorista), (temporal, False))
        wizard.action_crear_presupuesto()

    def test_1_may_sin_precio_mayorista_prefiere_el_nombre_completo(self):
        """Only the name prefix says «May»: when the normal product has no
        wholesale price and a «May …» product exists, that one."""
        normal = self._producto("Mate Pruebav116 Calabaza", 100.0)
        may = self._producto("May Mate Pruebav116 Calabaza", 80.0)
        self._switch("0")
        linea = self._asistente("- 1 x May Mate Pruebav116 Calabaza ($ 80.00)").linea_ids
        self.assertEqual((linea.product_tmpl_id, linea.es_mayorista), (may, False))
        # with a wholesale price, «May» is the normal product's wholesale price
        self.oculta.write({"item_ids": [Command.create({
            "applied_on": "1_product", "product_tmpl_id": normal.id,
            "compute_price": "fixed", "fixed_price": 70.0})]})
        linea = self._asistente("- 1 x May Mate Pruebav116 Calabaza ($ 70.00)").linea_ids
        self.assertEqual((linea.product_tmpl_id, linea.es_mayorista), (normal, True))

    def test_2_corregir_con_el_mismo_producto_resuelve(self):
        Equivalencia = self.env["distri.agil.equivalencia"].sudo().with_context(
            active_test=False)
        alfajor = self._producto("Alfajor Pruebav116 Triple Chocolate Blanco", 100.0)
        self._switch("0")
        wizard = self._asistente("- 2 x Alfajor Pruebav116 Triple Chocolate Blanc ($ 200.00)")
        linea = wizard.linea_ids
        self.assertEqual(linea.product_tmpl_id, alfajor)
        self.assertLess(linea.parecido, 100)
        self.assertTrue(linea._distri_para_corregir())
        antes = Equivalencia.search_count([])
        linea._distri_corregir(alfajor.product_variant_id, linea.product_uom_id)
        self.assertEqual(Equivalencia.search_count([]), antes, "nothing to learn")
        self.assertFalse(linea.equivalencia_id)
        self.assertFalse(linea._distri_para_corregir())
        self.assertEqual(linea._distri_motivo(), "")
        wizard.action_crear_presupuesto()


@tagged("post_install", "-at_install", "distri_v116")
class TestV116Http(PedidoAbiertoMixin, RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin = new_test_user(cls.env, login="admin_v116", name="Administracion V116",
                                  groups="base.group_user,distri_pedido.grupo_administracion",
                                  **company)
        cls.gerente = new_test_user(cls.env, login="gerente_v116", name="Gerencia V116",
                                    groups="base.group_user,distri_pedido.grupo_gerencia",
                                    **company)
        cls.tecnico = new_test_user(
            cls.env, login="tecnico_v116", name="Tecnico V116",
            groups="base.group_user,base.group_system,distri_pedido.grupo_administracion",
            **company)
        cls.baja = new_test_user(cls.env, login="baja_v116", name="Baja V116",
                                 groups="base.group_user,distri_pedido.grupo_administracion",
                                 **company)
        cls.baja.active = False
        cls.seller = new_test_user(cls.env, login="vendedor_v116", name="Vendedor V116",
                                   groups="base.group_user,distri_pedido.grupo_vendedor",
                                   **company)
        cls.packer = new_test_user(cls.env, login="armador_v116", name="Armador V116",
                                   groups="base.group_user,distri_pedido.grupo_armador",
                                   **company)
        cls.product = cls.env["product.product"].create({
            "name": "Producto v116", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.direct = cls.env["res.partner"].create({
            "name": "KIOSCO DIRECTO V116", "customer_rank": 1, "company_type": "company",
            "phone": "299 412 3456"})
        cls.wholesale = cls.env["res.partner"].create({
            "name": "KIOSCO MAYORISTA V116", "customer_rank": 1, "company_type": "company",
            "distri_es_mayorista": True})

    def test_3_rutas_dice_que_falta_el_cliente(self):
        generico = self.env["res.partner"]._distri_generic_partner_for(False)
        order = self._order(generico, self.product)
        route = self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "date": today_ar()})
        with self.assertRaisesRegex(UserError, "no tiene cliente"):
            self.env["distri.route"].with_user(self.admin).planning_move(
                order.with_user(self.admin), route.with_user(self.admin))

    def test_4_importes_grandes(self):
        Cash = self.env["distri.cash.collection"]
        self.assertEqual(Cash._cash_amount("150.000.000"), 150000000)
        self.assertEqual(Cash._cash_amount("9.999.999.999,99"), 9999999999.99)
        with self.assertRaisesRegex(UserError, "demasiado grande"):
            Cash._cash_amount("20.000.000.000")
        with self.assertRaisesRegex(UserError, "mayor que cero"):
            Cash._cash_amount("0")

    def test_5_remitos_de_la_ruta(self):
        Report = self.env["report.distri_pedido.remitos_ruta_documento"]
        order = self._order(self.direct, self.product)
        route = self._route(order)
        for user in (self.admin, self.packer, self.driver):
            values = Report.with_user(user)._get_report_values(route.ids)
            self.assertEqual(values["docs"], route, user.login)
        for user in (self.seller, self.other_driver):
            with self.assertRaises(AccessError, msg=user.login):
                Report.with_user(user)._get_report_values(route.ids)
            with self.assertRaises(AccessError, msg=user.login):
                route.with_user(user).action_print_remitos()
        wholesale = self._order(self.wholesale, self.product)
        route_may = self._route(wholesale)
        for user in (self.packer, self.driver):
            with self.assertRaisesRegex(AccessError, "mayorista", msg=user.login):
                Report.with_user(user)._get_report_values(route_may.ids)
        self.assertEqual(Report.with_user(self.admin)._get_report_values(route_may.ids)["docs"],
                         route_may)

    def test_7_remito_largo_empieza_en_hoja_nueva(self):
        """A remito over one A4 page starts on a new page, and so does the
        next one; short remitos keep sharing the sheet with a cut line."""
        short_a = self._order(self.direct, self.product)
        long_order = self._order(self.direct, self.product)
        extra = self.env["product.product"].create([{
            "name": "Renglon largo v117 %02d" % i, "list_price": 10, "type": "consu",
            "taxes_id": [Command.clear()]} for i in range(44)])
        long_order.write({"order_line": [Command.create({
            "product_id": p.id, "product_uom_qty": 1, "price_unit": 10}) for p in extra]})
        short_b = self._order(self.direct, self.product)
        short_c = self._order(self.direct, self.product)
        orders = short_a | long_order | short_b | short_c
        route = self._route(orders)
        html = self.env["ir.actions.report"].with_user(self.admin)._render_qweb_html(
            "distri_pedido.report_remitos_ruta", route.ids)[0].decode()
        body = html.split('class="dr dr-remitos"', 1)[1]
        seq = re.findall(r'class="(rm-hoja|dr-cut)"|Pedido (S\d+)', body)
        names = [n for _sep, n in seq if n]
        seps = [s for s, _n in seq if s]
        self.assertEqual(sorted(names), sorted(orders.mapped("name")))
        self.assertEqual(len(seps), len(names) - 1)
        for i, sep in enumerate(seps):
            pair = {names[i], names[i + 1]}
            expected = "rm-hoja" if long_order.name in pair else "dr-cut"
            self.assertEqual(sep, expected, (names, seps))

    def test_6_factura_fallida_avisa_a_las_personas(self):
        order = self._order(self.direct, self.product)
        avisados = order._distri_avisar_factura_a()
        self.assertIn(self.admin, avisados)
        self.assertIn(self.gerente, avisados)
        self.assertNotIn(self.tecnico, avisados)
        self.assertNotIn(self.baja, avisados)
        self.assertNotIn(self.env.ref("base.user_admin"), avisados)
        order._distri_avisar_error_factura(UserError("sin diario V116"))
        actividades = order.activity_ids.filtered(
            lambda a: a.summary == "Revisar la factura del armado")
        self.assertEqual(actividades.user_id, avisados)
        # a second failure does not repeat them
        order._distri_avisar_error_factura(UserError("otra vez"))
        self.assertEqual(len(order.activity_ids.filtered(
            lambda a: a.summary == "Revisar la factura del armado")), len(avisados))
        # /tablero: the red count on «Pedidos para revisar»
        env = self.env(user=self.gerente)
        tiles = staff_tiles(env)
        tile_badges(env, tiles)
        badge = {t["key"]: t for t in tiles}["revisar"]["badge"]
        self.assertTrue(badge and badge["kind"] == "err")
        self.assertGreaterEqual(badge["sin_factura"], 1)
        self.assertIn("sin factura", badge["text"])
