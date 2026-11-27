"""19.0.1.122.0 (owner):

- the tiles of «Inicio» follow the daily work (tablero.DEFAULT_TILE_ORDER),
  read row by row; each user still sees only the tiles of his groups;

- the home page «/» no longer shows the «Farmacia · Venta libre» card;
  /pedido and its products are unchanged;
- «Rutas anteriores» (controllers/reparto_anteriores.py): the driver's own
  routes of the last days, read only, without any customer balance;
- «Rearmar» (models/rearmar.py): administración sends an invoiced order back
  to /armado, as the script of 20/11/2026 did: the invoice and the order are
  cancelled and a confirmed copy goes back to the packing queue.
"""
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import file_path

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers import sitio, tablero
from odoo.addons.distri_pedido.models import rearmar
from odoo.addons.distri_pedido.models.agil_tienda import catalogo_agil_activo
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FARMACIA = "Farmacia · Venta libre"


def texto(html):
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


@tagged("post_install", "-at_install", "distri_v122", "distri_sitio")
class TestInicioSinFarmacia(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        categ = cls.env["product.category"].create({"name": "FARMACIA V122"})
        cls.env["product.template"].create({
            "name": "Ibuprofeno Prueba V122", "type": "consu", "sale_ok": True,
            "is_published": True, "list_price": 500, "categ_id": categ.id,
            # /pedido lists the temporary AgilPedido copy while its switch is on
            "distri_agil_temporal": catalogo_agil_activo(cls.env)})

    def test_home_hides_farmacia_only(self):
        res = self.url_open("/")
        self.assertEqual(res.status_code, 200)
        page = texto(res.text)
        self.assertNotIn(FARMACIA, page)
        self.assertNotIn("cat-farmacia", res.text, "no card image either")
        for key, titulo, _ejemplos in sitio.CATEGORIAS:
            if key != "farmacia":
                self.assertIn(titulo, page)
        # still a section of the catalog, hidden by its key on the home only
        self.assertIn("farmacia", [c[0] for c in sitio.CATEGORIAS])
        self.assertEqual(sitio.CATEGORIAS_OCULTAS_INICIO, frozenset({"farmacia"}))

    def test_pedido_still_shows_farmacia(self):
        res = self.url_open("/pedido")
        self.assertEqual(res.status_code, 200)
        self.assertIn("Ibuprofeno Prueba V122", res.text)


@tagged("post_install", "-at_install", "distri_v122")
class TestRearmar(RepartoMixin, AccountTestInvoicingCommon):

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
        cls.admin = new_test_user(
            cls.env, login="admin_v122", name="Administración V122",
            groups="base.group_user,distri_pedido.grupo_administracion", **company)
        cls.armador = new_test_user(
            cls.env, login="armador_v122", name="Armador V122",
            groups="base.group_user,distri_pedido.grupo_armador", **company)
        cls.vendedor = new_test_user(
            cls.env, login="vendedor_v122", name="Vendedor V122",
            groups="base.group_user,distri_pedido.grupo_vendedor,sales_team.group_sale_manager",
            **company)
        cls._setup_reparto()
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Rearmar V122", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})

    def _facturado(self):
        """A confirmed order, packed with one missing item and invoiced by
        the end of packing (posted invoice, done depot delivery)."""
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "pricelist_id": self.pricelist.id,
            "note": "Tocar timbre V122",
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": 10,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": 5,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ],
        })
        order.action_confirm()
        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        linea = pedido.order_line.filtered(lambda l: l.product_id == self.product_b)
        pedido.distri_marcar(linea, 2)
        self.assertEqual(pedido.distri_terminar(2), "")
        factura = order.invoice_ids
        self.assertEqual(factura.state, "posted")
        self.assertTrue(order.picking_ids.filtered(lambda p: p.state == "done"))
        self.assertFalse(order._distri_sin_entregar())
        return order

    def _rearmar(self, order, user=None):
        return order.with_user(user or self.admin).action_distri_rearmar()

    def _sin_cambios(self, order):
        self.assertEqual(order.state, "sale")
        self.assertTrue(order.invoice_ids.filtered(lambda f: f.state == "posted"))

    # ------------------------------------------------------------- happy path

    def test_rearmar_vuelve_al_armado(self):
        order = self._facturado()
        factura = order.invoice_ids
        accion = self._rearmar(order)

        self.assertEqual(order.state, "cancel")
        self.assertEqual(factura.state, "cancel")
        nuevo = self.env["sale.order"].browse(accion["res_id"])
        self.assertEqual(accion["res_model"], "sale.order")
        self.assertNotEqual(nuevo, order)
        self.assertEqual(nuevo.state, "sale")
        self.assertEqual(nuevo.partner_id, order.partner_id)
        self.assertEqual(nuevo.distri_armado, "pendiente")
        self.assertFalse(nuevo.invoice_ids)
        self.assertTrue(nuevo._distri_sin_entregar())
        self.assertTrue(nuevo._distri_para_armar(), "back in the /armado queue")
        self.assertIn("Tocar timbre V122", str(nuevo.note))
        # the lines as packed (the missing item already discounted)
        self.assertEqual(
            sorted(nuevo.order_line.mapped(lambda l: (l.product_id.id, l.product_uom_qty))),
            sorted(order.order_line.mapped(lambda l: (l.product_id.id, l.product_uom_qty))))
        # each chatter links the other order
        nuevo_msg = " ".join(str(m.body) for m in nuevo.message_ids)
        viejo_msg = " ".join(str(m.body) for m in order.message_ids)
        self.assertIn("Pedido rearmado", nuevo_msg)
        self.assertIn('data-oe-id="%s"' % order.id, nuevo_msg)
        self.assertIn("Rearmado", viejo_msg)
        self.assertIn('data-oe-id="%s"' % nuevo.id, viejo_msg)

    def test_gerencia_tambien_rearma(self):
        gerente = new_test_user(
            self.env, login="gerente_v122", name="Gerente V122",
            groups="base.group_user,distri_pedido.grupo_gerencia",
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)])
        order = self._facturado()
        self._rearmar(order, gerente)
        self.assertEqual(order.state, "cancel")

    # --------------------------------------------------------------- refusals

    def test_solo_administracion(self):
        for user in (self.armador, self.vendedor):
            order = self._facturado()
            with self.assertRaisesRegex(UserError, "Solo administración"):
                self._rearmar(order, user)
            self._sin_cambios(order)

    def test_rechaza_no_confirmado(self):
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [Command.create({"product_id": self.product_a.id,
                                           "product_uom_qty": 1, "price_unit": 10,
                                           "tax_ids": [Command.clear()]})]})
        with self.assertRaisesRegex(UserError, "confirmado"):
            self._rearmar(order)
        self.assertEqual(order.state, "draft")

    def test_rechaza_factura_con_pago(self):
        order = self._facturado()
        factura = order.invoice_ids
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=factura.ids).create({}) \
            ._create_payments()
        self.assertNotEqual(factura.payment_state, "not_paid")
        with self.assertRaisesRegex(UserError, "pagos o conciliaciones"):
            self._rearmar(order)
        self._sin_cambios(order)

    def test_rechaza_entregado(self):
        order = self._facturado()
        route = self._route(order)
        route.stop_ids.filtered(lambda s: s.order_id == order).sudo().write(
            {"delivery_state": "delivered_code", "state": "paid"})
        with self.assertRaisesRegex(UserError, "ya fue entregado"):
            self._rearmar(order)
        self._sin_cambios(order)

    def test_rechaza_en_ruta(self):
        order = self._facturado()
        self._route(order)
        with self.assertRaisesRegex(UserError, "está en la ruta"):
            self._rearmar(order)
        self._sin_cambios(order)

    def test_rechaza_cae(self):
        """An invoice authorized by ARCA is never cancelled."""
        order = self._facturado()
        with patch.object(rearmar, "_has_cae", return_value=True), \
                self.assertRaisesRegex(UserError, "CAE de ARCA"):
            self._rearmar(order)
        self._sin_cambios(order)

    def test_sin_cae_en_produccion(self):
        """Today no invoice has a CAE: no l10n_ar_edi field, no ARCA journal."""
        order = self._facturado()
        self.assertFalse(rearmar._has_cae(order.invoice_ids))


@tagged("post_install", "-at_install", "distri_v122")
class TestInicioOrden(TransactionCase):

    ORDEN = ["Tomar un pedido", "Mis ventas", "AgilPedido", "Pedidos para revisar", "Armado", "Depósito",
             "Planificación de rutas", "Despacho", "Mi reparto", "Rendiciones y casos",
             "Cobrar", "Caja del día", "Comprobantes", "Cuentas",
             "Historial de cajas", "Cobros", "Novedades", "Capacitación"]

    def _user(self, login, groups):
        return new_test_user(
            self.env, login=login, name=login, groups="base.group_user," + groups,
            company_id=self.env.company.id, company_ids=[Command.set(self.env.company.ids)])

    def test_default_order_covers_every_tile(self):
        self.assertEqual(sorted(tablero.DEFAULT_TILE_ORDER), sorted(t[0] for t in tablero.TILES))
        self.assertEqual([t[2] for t in tablero.ordered_tiles()], self.ORDEN)

    def test_each_user_sees_his_tiles_in_that_order(self):
        users = [
            self._user("todo_v122", "distri_pedido.grupo_gerencia,distri_pedido.grupo_cajero,"
                                    "distri_pedido.grupo_cobros"),
            self._user("admin_orden_v122", "distri_pedido.grupo_administracion"),
            self._user("armador_orden_v122", "distri_pedido.grupo_armador"),
            self._user("vendedor_orden_v122", "distri_pedido.grupo_vendedor"),
        ]
        vistos = []
        for user in users:
            titulos = [t["title"] for t in tablero.staff_tiles(self.env(user=user))]
            self.assertEqual(titulos, [t for t in self.ORDEN if t in titulos], user.login)
            self.assertEqual(titulos[-1], "Capacitación", user.login)
            vistos.append(titulos)
        self.assertEqual(vistos[0][0], "Tomar un pedido")
        self.assertLess(len(vistos[2]), len(vistos[0]), "fewer groups, fewer tiles")

    def test_tiles_are_one_row_major_grid(self):
        """One grid of tiles filled row by row, at every width: no CSS
        columns, no column-first flow, no sections splitting the list."""
        with open(file_path("distri_pedido/static/src/tablero/tablero.css")) as f:
            css = f.read()
        regla = re.search(r"\.tab-tiles\{[^}]*\}", css).group(0)
        self.assertIn("display:grid", regla)
        self.assertNotRegex(css, r"column-count|[{;\s]columns:|grid-auto-flow:\s*column")
        self.assertNotRegex(css, r"\.tab-tile[^{]*\{[^}]*\border:")


@tagged("post_install", "-at_install", "distri_v122")
class TestRutasAnteriores(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        company = {"company_id": env.company.id, "company_ids": [Command.set(env.company.ids)]}
        cls._setup_reparto()
        cls.packer = new_test_user(env, login="armador_ant_v122", name="Armador Ant V122",
                                   groups="base.group_user,distri_pedido.grupo_armador",
                                   **company)
        cls.product = env["product.product"].create({
            "name": "Producto Ant V122", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.wholesale = env["res.partner"].create({
            "name": "PEPE MAYORISTA V122", "customer_rank": 1, "company_type": "company",
            "distri_es_mayorista": True, "street": "Calle Mayorista 122", "city": "Neuquén"})
        cls.direct = env["res.partner"].create({
            "name": "KIOSCO DIRECTO V122", "customer_rank": 1, "company_type": "company",
            "street": "Calle Directa 122", "city": "Neuquén"})
        env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.wholesale.id, "fecha": "2026-11-01",
            "comprobante": "0001/2026/12201", "tipo": "factura", "debe": 98765.0})

    def _past(self, driver=None, days=2):
        orders = self._order(self.wholesale, self.product, qty=3) \
            | self._order(self.direct, self.product, qty=2)
        route = self._route(orders, driver=driver).sudo()
        stops = route.stop_ids
        delivered = stops.filtered(lambda s: s.partner_id == self.wholesale)
        missed = stops - delivered
        delivered.write({"delivery_state": "delivered_code", "state": "paid",
                         "cash_amount": 300.0})
        missed.write({"delivery_state": "not_delivered", "state": "not_delivered",
                      "not_delivered_reason": "Cerrado V122"})
        route.write({"date": today_ar() - timedelta(days=days), "state": "settled"})
        return route

    def test_own_past_routes_listed_newest_first(self):
        older = self._past(days=5)
        newer = self._past(days=1)
        too_old = self._past(days=40)
        today = self._past(days=0)
        # 19.0.1.129.23: today's route still open stays on the main screen only
        today_open = self._past(days=0)
        today_open.state = "closed"
        other = self._past(driver=self.other_driver, days=1)
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/anteriores")
        self.assertEqual(res.status_code, 200)
        html = res.text
        links = re.findall(r'href="/reparto/anteriores/(\d+)"', html)
        # 19.0.1.129.23: today's settled route is listed first
        self.assertEqual(links, [str(today.id), str(newer.id), str(older.id)])
        for route in (too_old, today_open, other):
            self.assertNotIn("/reparto/anteriores/%s\"" % route.id, html)
        page = texto(html)
        self.assertIn("2 visitas · 1 entregada · 1 sin entregar", page)
        self.assertIn("Efectivo cobrado: $ 300,00", page)
        # the app links to it
        with open(file_path("distri_pedido/static/src/reparto/app3.js")) as f:
            self.assertIn('"/reparto/anteriores"', f.read())

    def test_route_detail_read_only_and_no_balance(self):
        route = self._past()
        self.assertTrue(route.stop_ids.filtered(lambda s: s.partner_id == self.wholesale)
                        .previous_debt, "the route had the debt the day it went out")
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/anteriores/%s" % route.id)
        self.assertEqual(res.status_code, 200)
        html, page = res.text, texto(res.text)
        self.assertIn(self.wholesale._distri_app_name(), page)
        self.assertIn("Calle Mayorista 122", page)
        self.assertIn("Entregado con código", page)
        self.assertIn("Efectivo: $ 300,00", page)
        self.assertIn("No se entregó", page)
        self.assertIn("Motivo: Cerrado V122", page)
        # visits in their order
        self.assertEqual(len(re.findall(r'data-stop=""', html)), 2)
        # no balance, no account, nothing that changes state
        self.assertNotIn("98.765", html)
        self.assertNotIn("98765", html)
        self.assertNotIn("Ver cuenta", html)
        self.assertNotIn("/reparto/api/", html)
        self.assertNotRegex(html, r"(?i)<form|<button|<input|<textarea|data-a=")
        # only the shared navigation script of the legacy pages
        self.assertLessEqual(len(re.findall(r"(?i)<script", html)), 1)
        # GET only
        res = self.url_open("/reparto/anteriores/%s" % route.id, data={"x": "1"})
        self.assertNotEqual(res.status_code, 200)

    def test_today_settled_route_opens_open_one_refused(self):
        """19.0.1.129.23: the driver sees today's route as soon as it is settled."""
        settled = self._past(days=0)
        still_open = self._past(days=0)
        still_open.state = "closed"
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/anteriores/%s" % settled.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Calle Mayorista 122", texto(res.text))
        self.assertEqual(self.url_open("/reparto/anteriores/%s" % still_open.id).status_code, 404)

    def test_another_drivers_route_refused(self):
        other = self._past(driver=self.other_driver)
        self.authenticate(self.driver.login, self.driver.login)
        res = self.url_open("/reparto/anteriores/%s" % other.id)
        self.assertEqual(res.status_code, 404)
        self.assertNotIn("Calle Mayorista 122", res.text)
        self.assertEqual(self.url_open("/reparto/anteriores/999999999").status_code, 404)

    def test_packer_has_no_access(self):
        route = self._past()
        self.authenticate(self.packer.login, self.packer.login)
        self.assertEqual(self.url_open("/reparto/anteriores").status_code, 403)
        self.assertEqual(self.url_open("/reparto/anteriores/%s" % route.id).status_code, 403)
