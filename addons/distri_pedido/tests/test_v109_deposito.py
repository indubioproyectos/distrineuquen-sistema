"""19.0.1.109.0: «Depósito» on the phone (/deposito, controllers/deposito.py),
the depot role's bottom nav, the «Depósito» tile, /caja/cierre on the depot
PC and the town prefixes «C -» and «S -».

Every depot action goes through HTTP as the page sends it (fetch,
X-Distri-Fetch: 1) and ends in the backend's own wizard and methods
(models/deposito.py, distri.route.action_close_depot), so the backend's
rules apply unchanged."""
import re
from datetime import timedelta

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import BaseCase, HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import depot_nav, staff_nav
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles, tile_badges
from odoo.addons.distri_pedido.models.deposito import COUNTER_REASON, WITHOUT_CODE_REASON
from odoo.addons.distri_pedido.models.etiquetas import PREFIX_OF_TOWN
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.models.caja_contabilidad import PARAM_JOURNAL
from odoo.addons.distri_pedido.models.reparto import DEPOT_JOURNAL_CODE, DEPOT_TO_CASHIER
from odoo.addons.distri_pedido.models.reparto_cliente import (
    CAPITAL, TOWN_PREFIXES, strip_town_prefix, town_from_name, town_prefix)
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v109")
class TestTownPrefixes(BaseCase):

    def test_every_prefix(self):
        cases = {
            "ZC - KIOSCO LA ESQUINA": "Cutral Có",
            "zc-kiosco": "Cutral Có",
            "C - AGUILERA MARIA - DNI 32951484": "Cutral Có",
            "C-ATENCIO MOISES DNI 42969371 - CHOS MALAL - PAGA EN DESTINO": "Cutral Có",
            "  c -  ROMERO GLADYS": "Cutral Có",
            "S - KAM": "Senillosa",
            "S-SUPER SENILLOSA": "Senillosa",
            "X - DESPENSA": "Centenario",
            "Y - KIOSCO GERENTE": "Plottier",
            "Z - ALMACEN": "Zapala",
            "Z-ALMACEN": "Zapala",
            "W - CATRIEL": "Catriel",
            "KIOSCO DIMAR": CAPITAL,
            "CUTRAL CO - KIOSCO": CAPITAL,
            "SA - ALGO": CAPITAL,
            "ZAPALA KIOSCO": CAPITAL,
            "": CAPITAL,
        }
        for name, town in cases.items():
            self.assertEqual(town_from_name(name), town, name)
        self.assertEqual(set(TOWN_PREFIXES), {"ZC", "C", "X", "Y", "Z", "W", "S"})
        # «ZC» is never read as «Z» nor as «C»
        self.assertEqual(town_prefix("ZC - X"), "ZC")
        self.assertEqual(town_prefix("C-ATENCIO"), "C")
        self.assertEqual(town_prefix("KIOSCO"), "")
        self.assertEqual(strip_town_prefix("C-ATENCIO MOISES"), "ATENCIO MOISES")
        self.assertEqual(strip_town_prefix("ZC - KIOSCO"), "KIOSCO")
        self.assertEqual(strip_town_prefix("S - KAM"), "KAM")

    def test_label_prefix_of_each_town(self):
        self.assertEqual(PREFIX_OF_TOWN["Cutral Có"], "C", "the owner's prefix for Cutral Có")
        self.assertEqual(PREFIX_OF_TOWN["Senillosa"], "S")
        self.assertEqual(PREFIX_OF_TOWN["Zapala"], "Z")
        self.assertEqual(PREFIX_OF_TOWN["Catriel"], "W")


@tagged("post_install", "-at_install", "distri_v109", "distri_v109_deposito")
class TestDepositoApp(RepartoMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_dp109", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_dp109", "distri_pedido.grupo_gerencia")
        cls.armador = user("armador_dp109", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_dp109", "distri_pedido.grupo_reparto")
        cls.vendedor = user("vendedor_dp109", "distri_pedido.grupo_vendedor")
        cls.cajero = user("cajero_dp109", "distri_pedido.grupo_cajero")
        cls.admin_cajero = user("admincaja_dp109",
                                "distri_pedido.grupo_administracion,distri_pedido.grupo_cajero")
        cls.partner = cls.partner_a
        cls.partner.write({"distri_whatsapp": "2995001234", "name": "Almacen Don Pepe DP109"})
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Deposito v109", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def _quotation(self, qty_a=10, qty_b=5):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id,
            "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": qty_a,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": qty_b,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ],
        })

    def _packed(self, pickup=True):
        """Confirmed and packed by the packer's steps: posted invoice of 1200."""
        order = self._quotation()
        if pickup:
            order.distri_entrega = "retira"
        order.action_confirm()
        self.assertEqual(order.distri_tomar(), "")
        self.assertEqual(order.distri_terminar(2), "")
        return order

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self, path="/deposito"):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1)

    def _post(self, path, data=None, fetch=True, page="/deposito"):
        data = dict(data or {}, csrf_token=self._csrf(page))
        res = self.url_open(path, data=data, headers=FETCH if fetch else None,
                            allow_redirects=False)
        self.env.invalidate_all()
        return res

    def _deliver(self, order, **values):
        data = {"devolvio": "none", "codigo_op": "without_code", "transferencia": "no"}
        data.update(values)
        return self._post("/deposito/entrega/%s" % order.id, data)

    def _stop(self, order):
        return order.distri_stop_ids.sorted("id")[-1:]

    # ------------------------------------------------------------ access

    def test_access_per_group(self):
        order = self._packed()
        for user in (self.armador, self.repartidor, self.vendedor, self.cajero):
            self._login(user)
            for path in ("/deposito", "/deposito/mostrador", "/deposito/caja",
                         "/deposito/entrega/%s" % order.id):
                self.assertEqual(self.url_open(path).status_code, 403, (user.login, path))
            res = self.url_open("/deposito/entrega/%s" % order.id, data={"csrf_token": "x"},
                                headers=FETCH, allow_redirects=False)
            self.assertIn(res.status_code, (400, 403), user.login)
        self.assertFalse(order.distri_stop_ids)
        for user in (self.admin, self.gerente):
            self._login(user)
            for path in ("/deposito", "/deposito/mostrador", "/deposito/caja",
                         "/deposito/entrega/%s" % order.id):
                self.assertEqual(self.url_open(path).status_code, 200, (user.login, path))

    # ------------------------------------------------------------ hub and delivery

    def test_hub_lists_the_pickup_and_delivers_with_code(self):
        order = self._packed()
        code = order._distri_generate_receipt_code()
        self._login(self.admin)
        hub = self.url_open("/deposito").text
        self.assertIn(order.name, hub)
        self.assertIn("Retira en depósito", hub)
        self.assertIn('href="/deposito/entrega/%s"' % order.id, hub)
        self.assertIn("La salida de viajes ahora se controla en", hub)
        self.assertIn('href="/despacho"', hub)
        page = self.url_open("/deposito/entrega/%s" % order.id).text
        self.assertIn("Saldo total", page)
        self.assertIn("Ingresar el código", page)
        self.assertIn("No tiene el código · Entregar igual", page)

        res = self._deliver(order, codigo_op="code", codigo=code, efectivo="1.300")
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["ok"], res.text)
        self.assertTrue(res.json()["url"].startswith("/deposito?ok="))
        stop = self._stop(order)
        self.assertEqual(stop.route_id.kind, "depot")
        self.assertEqual(stop.route_id.driver_id, self.admin)
        self.assertEqual(stop.delivery_state, "delivered_code")
        self.assertEqual(stop.state, "paid")
        self.assertEqual(order.distri_delivery_state, "delivered")
        payment = stop.route_id.payment_ids
        self.assertEqual(payment.amount, 1300)
        self.assertEqual(payment.journal_id.code, DEPOT_JOURNAL_CODE)
        hub = self.url_open("/deposito").text
        self.assertIn("Entregados hoy · 1", hub)
        self.assertNotIn('href="/deposito/entrega/%s"' % order.id, hub, "no longer to deliver")
        # delivered once: the form sends back to the hub with the reason
        again = self.url_open("/deposito/entrega/%s" % order.id, allow_redirects=False)
        self.assertEqual(again.status_code, 303)
        self.assertIn("aviso=", again.headers["Location"])

    def test_wrong_code_is_refused_then_without_code(self):
        order = self._packed()
        code = order._distri_generate_receipt_code()
        wrong = "%04d" % ((int(code) + 1) % 10000)
        self._login(self.admin)
        res = self._deliver(order, codigo_op="code", codigo=wrong, efectivo="1200")
        self.assertEqual(res.status_code, 400)
        self.assertIn("no coincide", res.json()["error"])
        self.assertFalse(order.distri_stop_ids)
        # without JavaScript: the page again with the error and the answers
        res = self._post("/deposito/entrega/%s" % order.id, {
            "devolvio": "none", "codigo_op": "code", "codigo": wrong, "transferencia": "no",
            "efectivo": "1200"}, fetch=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("no coincide", res.text)
        self.assertIn('value="1200"', res.text)
        res = self._deliver(order, efectivo="1200")
        self.assertTrue(res.json()["ok"], res.text)
        stop = self._stop(order)
        self.assertEqual(stop.delivery_state, "delivered_no_code")
        self.assertEqual(stop.no_code_reason, WITHOUT_CODE_REASON)

    def test_payment_answers_like_the_backend_wizard(self):
        order = self._packed()
        self._login(self.admin)
        res = self._deliver(order)
        self.assertEqual(res.status_code, 400)
        self.assertIn("No pagó nada", res.json()["error"])
        res = self._deliver(order, transferencia="yes", importe_transf="1200")
        self.assertEqual(res.status_code, 400)
        self.assertIn("comprobante", res.json()["error"])
        res = self._deliver(order, devolvio="", no_pago="1")
        self.assertEqual(res.status_code, 400)
        self.assertIn("devolvió", res.json()["error"])
        self.assertFalse(order.distri_stop_ids)
        res = self._deliver(order, efectivo="200", transferencia="yes", importe_transf="1.000",
                            comprobante="not_yet", comentario="paga el resto el viernes")
        self.assertTrue(res.json()["ok"], res.text)
        stop = self._stop(order)
        self.assertEqual(stop.cash_amount, 200)
        self.assertEqual(stop.transfer, "yes")
        self.assertEqual(stop.transfer_amount, 1000)
        self.assertEqual(stop.comment, "paga el resto el viernes")
        self.assertTrue(stop.route_id.promise_ids, "the transfer waits for its receipt")
        self.assertEqual(stop.route_id.payment_ids.amount, 200, "only the cash is a payment")

    def test_nothing_paid_and_returns(self):
        order = self._packed()
        line_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        self._login(self.admin)
        res = self._deliver(order, devolvio="yes", no_pago="1", **{"dev_%s" % line_a.id: "2"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("motivo", res.json()["error"])
        res = self._deliver(order, devolvio="yes", no_pago="1", recibido="1", **{
            "dev_%s" % line_a.id: "2", "motivo_%s" % line_a.id: "Vencido"})
        self.assertTrue(res.json()["ok"], res.text)
        stop = self._stop(order)
        self.assertEqual(stop.state, "not_paid")
        self.assertEqual(len(stop.return_ids), 1)
        ret = stop.return_ids
        self.assertEqual((ret.quantity, ret.reason, ret.settlement_state), (2, "Vencido", "received"))
        self.assertTrue(ret.credit_note_id, "received at the depot: the credit note comes out")
        self.assertFalse(stop.route_id.payment_ids)

    # ------------------------------------------------------------ counter sale

    def test_counter_sale(self):
        order = self._quotation(qty_a=3, qty_b=2)
        self._login(self.admin)
        page = self.url_open("/deposito/mostrador").text
        self.assertIn(order.name, page)
        self.assertIn("Se confirma el pedido, se arma completo", page)
        res = self._post("/deposito/mostrador/%s" % order.id, page="/deposito/mostrador")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertEqual(res.json()["url"], "/deposito/entrega/%s?mostrador=1" % order.id)
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.distri_factura_auto_id.state, "posted")
        form = self.url_open(res.json()["url"]).text
        self.assertIn(COUNTER_REASON, form)
        self.assertIn("El pedido ya quedó confirmado", form)
        res = self._deliver(order, mostrador="1", presente="1", bultos="2", efectivo="380")
        self.assertTrue(res.json()["ok"], res.text)
        stop = self._stop(order)
        self.assertEqual(stop.no_code_reason, COUNTER_REASON)
        self.assertEqual(order.distri_bultos, 2)
        self.assertEqual(stop.cash_amount, 380)
        # a packed order is no quotation any more
        res = self._post("/deposito/mostrador/%s" % order.id, page="/deposito/mostrador")
        self.assertEqual(res.status_code, 400)

    def test_counter_sale_refuses_a_generic_or_empty_quotation(self):
        empty = self.env["sale.order"].create({"partner_id": self.partner.id,
                                               "pricelist_id": self.pricelist.id})
        self._login(self.admin)
        self.assertNotIn(empty.name, self.url_open("/deposito/mostrador").text)
        res = self._post("/deposito/mostrador/%s" % empty.id, page="/deposito/mostrador")
        self.assertEqual(res.status_code, 400)
        self.assertIn("no tiene productos", res.json()["error"])
        self.assertEqual(empty.state, "draft")

    # ------------------------------------------------------------ closing

    def test_nobody_closes_a_depot_box_by_hand(self):
        """Owner (19.0.1.109.0): the depot cash always goes through a cashier.
        Neither /deposito nor the backend method closes a non-cashier's box,
        whoever asks (administración, gerencia)."""
        order = self._packed()
        self._login(self.admin)
        res = self._deliver(order, efectivo="1000")
        self.assertTrue(res.json()["ok"], res.text)
        box = self._stop(order).route_id
        page = self.url_open("/deposito/caja").text
        self.assertIn("Cierre de caja depósito", page)
        self.assertIn("Efectivo a rendir", page)
        self.assertIn(DEPOT_TO_CASHIER, page)
        self.assertNotIn("/deposito/caja/%s/cerrar" % box.id, page)
        self.assertNotIn("Recibirla en mi Caja del día", page, "he is no cashier")
        caja_page = "/deposito/caja/%s" % box.id
        for user in (self.admin, self.gerente):
            self._login(user)
            res = self._post("/deposito/caja/%s/cerrar" % box.id, {"contado": "1000"},
                             page=caja_page)
            self.assertEqual(res.status_code, 400, user.login)
            self.assertEqual(res.json()["error"], DEPOT_TO_CASHIER)
            with self.assertRaisesRegex(UserError, "la recibe un cajero"):
                box.with_user(user).action_close_depot()
        with self.assertRaisesRegex(UserError, "la recibe un cajero"):
            box.action_close_depot()
        self.assertEqual(box.state, "on_route")
        self.assertFalse(box.cash_count_done)
        form = self.env.ref("distri_pedido.view_depot_close_form").arch_db
        self.assertNotIn('name="action_close_depot"', form, "no button in the backend")

    def test_a_cashier_receives_the_depot_box(self):
        """«Recibir plata» in /caja: another user's depot box, with a pending
        return answered on the receiving page and a shortage, end to end."""
        self.env["ir.config_parameter"].sudo().set_param(
            PARAM_JOURNAL, self.company_data["default_journal_cash"].code)
        order = self._packed()
        line_a = order.order_line.filtered(lambda l: l.product_id == self.product_a)
        self._login(self.admin)
        res = self._deliver(order, devolvio="yes", efectivo="1000", **{
            "dev_%s" % line_a.id: "1", "motivo_%s" % line_a.id: "Roto"})
        self.assertTrue(res.json()["ok"], res.text)
        box = self._stop(order).route_id
        ret = box.return_ids
        self.assertEqual(ret.settlement_state, "pending")

        caja = self.env["distri.caja"].with_user(self.cajero).open_session("0")
        routes, boxes = self.env["distri.caja"].with_user(self.cajero).pending_receptions()
        self.assertIn(box, boxes)
        self._login(self.cajero)
        self.assertIn('href="/caja/rendicion/%s"' % box.id, self.url_open("/caja/rendicion").text)
        page = self.url_open("/caja/rendicion/%s" % box.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn("devolucion_%s" % ret.id, page.text, "the page asks the return")
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        data = {"csrf_token": token, "entrego": "900", "devolucion_%s" % ret.id: "received"}
        res = self.url_open("/caja/rendicion/%s" % box.id, data=data, headers=FETCH)
        self.assertEqual(res.status_code, 400, "a shortage is asked first")
        self.assertIn("Faltan", res.json()["error"])
        data["diferencia"] = "cargo"
        res = self.url_open("/caja/rendicion/%s" % box.id, data=data, headers=FETCH)
        self.assertTrue(res.json()["ok"], res.text)
        self.env.invalidate_all()
        self.assertEqual(box.state, "settled")
        self.assertEqual(box.cash_counted, 900)
        self.assertEqual(ret.settlement_state, "received")
        self.assertTrue(ret.credit_note_id, "received returns get their credit note")
        case = box._cash_case()
        self.assertTrue(case)
        self.assertEqual(case.amount, -100)
        movement = caja.movement_ids.filtered(lambda m: m.route_id == box)
        self.assertEqual((movement.kind, movement.amount), ("recepcion", 900))
        self.assertEqual(caja.expected, 900, "the amount lands in his caja")
        self._login(self.admin)
        page = self.url_open("/deposito/caja/%s" % box.id).text
        self.assertNotIn(DEPOT_TO_CASHIER, page)
        self.assertIn("Cerró", page)

    def test_cashier_admin_sees_receive_link(self):
        order = self._packed()
        self._login(self.admin)
        self.assertTrue(self._deliver(order, efectivo="1000").json()["ok"])
        box = self._stop(order).route_id
        self._login(self.admin_cajero)
        page = self.url_open("/deposito/caja/%s" % box.id).text
        self.assertIn('href="/caja/rendicion/%s"' % box.id, page)

    def test_box_of_a_day_before_is_flagged(self):
        Route = self.env["distri.route"]
        old = Route.with_user(self.admin)._depot_route_for(self.admin)
        old.sudo().write({"date": old.date - timedelta(days=2)})
        self._login(self.admin)
        hub = self.url_open("/deposito").text
        self.assertIn("sin cerrar", hub)
        self.assertIn('href="/deposito/caja/%s"' % old.id, hub)
        page = self.url_open("/deposito/caja/%s" % old.id).text
        self.assertIn("Esta caja es de un día anterior y sigue abierta", page)

    def test_a_cashiers_box_closes_with_his_caja(self):
        box = self.env["distri.route"].with_user(self.admin_cajero)._depot_route_for(
            self.admin_cajero)
        self._login(self.admin_cajero)
        page = self.url_open("/deposito/caja/%s" % box.id).text
        self.assertIn("se cierra sola cuando cerrás tu", " ".join(page.split()))
        self.assertNotIn("/deposito/caja/%s/cerrar" % box.id, page)
        res = self._post("/deposito/caja/%s/cerrar" % box.id, {"contado": "0"},
                         page="/deposito/caja/%s" % box.id)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Caja del día", res.json()["error"])
        self.assertNotEqual(box.state, "settled")

    # ------------------------------------------------------------ tablero and nav

    def test_tablero_tile_and_badge(self):
        self._packed()
        self._packed(pickup=False)

        def tiles(user):
            env = self.env(user=user)
            found = staff_tiles(env)
            tile_badges(env, found)
            return {t["key"]: t for t in found}

        for user in (self.admin, self.gerente):
            tile = tiles(user)["deposito"]
            self.assertEqual(tile["url"], "/deposito")
            self.assertEqual(tile["badge"]["text"], "1 para retirar", "only the pickups")
        for user in (self.armador, self.repartidor, self.vendedor, self.cajero):
            self.assertNotIn("deposito", tiles(user), user.login)
        self._login(self.gerente)
        text = self.url_open("/tablero").text
        self.assertIn('href="/deposito"', text)
        self.assertIn("1 para retirar", text)

    def test_bottom_nav_only_for_the_depot_role(self):
        keys = [i["key"] for i in depot_nav(self.env(user=self.admin), "entregas")]
        self.assertEqual(keys, ["armado", "despacho", "cobro", "entregas"])
        for user in (self.armador, self.repartidor, self.vendedor, self.cajero):
            self.assertEqual(depot_nav(self.env(user=user), "entregas"), [], user.login)
        self.assertNotIn("entregas", [i["key"] for i in staff_nav(self.env(user=self.admin),
                                                                     "armado")])
        self._packed()
        self._login(self.admin)
        hub = self.url_open("/deposito").text
        nav = re.search(r'<nav class="dn-nav".*?</nav>', hub, re.S).group(0)
        for href in ('href="/armado"', 'href="/despacho"', 'href="/cobro"', 'href="/deposito"'):
            self.assertIn(href, nav)
        self.assertRegex(nav, r'href="/deposito"[^>]*class="on"[^>]*aria-current="page"')
        self.assertIn('class="dn-nav__bd dn-num"', nav, "Armado carries its count")
        # the other screens keep their own nav
        armado = self.url_open("/armado").text
        self.assertNotIn('href="/deposito"', armado)
        self.assertNotIn('href="/deposito"', self.url_open("/revisar").text)

    def test_no_approve_for_driver_expenses(self):
        """Owner (batch 2): driver expenses are never «aprobados» by hand: the
        settlement approves the ones with paper."""
        views = self.env["ir.ui.view"].search([("model", "in", ("distri.route.expense",
                                                                 "distri.route"))])
        for view in views:
            arch = view.arch_db or ""
            self.assertNotIn('string="Aprobar"', arch, view.name)
            self.assertNotRegex(arch, r'name="action_approve\w*"', view.name)
        for xmlid in ("distri_pedido.caja_rendicion", "distri_pedido.deposito_caja"):
            self.assertNotIn("Aprobar", self.env.ref(xmlid).arch_db, xmlid)


@tagged("post_install", "-at_install", "distri_v109", "distri_caja")
class TestCajaEscritorio(CajaMixin, HttpCase):
    """/caja/cierre on the depot PC: the day, the count and the closing in
    one page; the phone keeps its layout (the desktop parts are hidden by
    .caja-pc-only below 1024 px)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)

    def test_desktop_markup_and_closing(self):
        self.authenticate("cajero_test", "cajero_test")
        caja = self.env["distri.caja"].with_user(self.cashier).open_session(1000)
        page = self.url_open("/caja/cierre")
        self.assertEqual(page.status_code, 200)
        text = page.text
        for piece in ('class="dn-main caja-pc"', "caja-pc-only caja-pc__dia", "Caja del día",
                      "Contar los billetes", "número y Enter pasa al siguiente billete",
                      "Total contado", "Contar la plata y cerrar", "Mis cajas cerradas",
                      "data-caja-enter", "data-caja-imprimir", "Cerrar e imprimir",
                      'href="/caja/rendicion"', "Recibir plata"):
            self.assertIn(piece, text)
        # the phone's footer button is still there
        self.assertIn('form="caja-cierre"', text)
        css = self.url_open("/dn/caja.css").text
        self.assertIn("@media (min-width:1024px)", css)
        self.assertIn(".caja-pc-only{display:none!important}", css)
        js = self.url_open("/dn/caja.js").text
        self.assertIn("data-caja-enter", js)
        self.assertIn("/etiqueta", js)
        # the same closing as before (v105 rules untouched)
        token = re.search(r'name="csrf_token" value="([^"]+)"', text).group(1)
        res = self.url_open("/caja/cierre", data={
            "csrf_token": token, "cant_%s" % self.bills[1000].id: "1", "queda": "0",
            "esperado": str(caja.expected)}, headers=FETCH)
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("cerrada=%s" % caja.id, res.json()["url"])
        self.env.invalidate_all()
        self.assertEqual(caja.state, "closed")
        # v105: «Reabrir mi caja» still works and the next count says it
        # replaces the first closing
        caja.with_user(self.cashier).action_reopen("Faltó cargar un gasto")
        self.assertEqual(caja.state, "open")
        text = self.url_open("/caja/cierre").text
        self.assertIn("Este cierre reemplaza al cierre", text)
