"""19.0.1.115.0: go-live fixes of the owner-approved review.

1. Package labels: building them twice gives the same packages and the
   orders' rows are written first (concurrent requests serialize).
2. Driver app amounts (app3.js, tests/data/reparto_v115.js) and the server
   bound on what a visit collects.
3. A route reopened by administración opens again on the phone (node).
4. A box whose cash is already in another cashier's caja is not received.
5. A collection reset or cancelled leaves the open caja.
6. A route is settled only from a cashier's caja.
7. Remitos: only the roles that print them; wholesale only administración.
8. Every staff role uploads receipts.
9. Wholesale customers only for administración, gerencia and the admins.
10. Staff orders take the customer's phone on file.
11. One tap, one order.
12. «Anular ruta» only before departure (tests/test_v113_reparto.py).
13. A seller who also delivers lands on Inicio with his tiles.
14. «Salida de caja» ignores a twin post (another receipt is another one).
15. «Crear factura» of an order not packed yet asks «Facturar igual».
/proyecto: the new steps.
"""
import json
import os
import shutil
import subprocess
from unittest import SkipTest
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja import DuplicateOutflow
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, page_tokens, \
    post_pedido
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_v115")
class TestV115Caja(CajaMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.other_cashier = new_test_user(
            cls.env, login="cajero_v115", name="Otro Cajero V115",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        cls.partner = cls.partner_a
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})

    def _collect(self, amount, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cashier).cash_register(
            self.partner.id, str(amount), confirmed=True, route_ok=True, over_ok=True)

    # ------------------------------------------------------------ 1

    def test_1_labels_same_packages_and_orders_locked(self):
        order = self._order(self.partner, self.product_a)
        SaleOrder = type(self.env["sale.order"])
        original = SaleOrder._distri_lock_for_packages
        with patch.object(SaleOrder, "_distri_lock_for_packages", autospec=True,
                          side_effect=original) as lock:
            first = order._distri_packages()
        lock.assert_called_once()
        self.assertEqual(len(first), 2)
        # the second request (the double GET) renders the same labels
        second = order._distri_packages()
        self.assertEqual(first, second)
        self.assertEqual(self.env["distri.package"].search_count([("order_id", "=", order.id)]),
                         2)

    # ------------------------------------------------------------ 2 (server)

    def test_2_visit_amount_bound(self):
        order = self._order(self.partner, self.product_a)  # 10 x 100
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=1430760))])
        self.assertEqual(res[0]["status"], "error", res)
        self.assertIn("demasiado", res[0]["error"])
        self.assertIn("14.307,60", res[0]["error"])
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=150000))])
        self.assertEqual(res[0]["status"], "ok", res)

    # ------------------------------------------------------------ 4

    def test_4_box_already_in_another_caja_is_not_received(self):
        other = self._caja(self.other_cashier).open_session("0")
        collection = self._collect(8000, user=self.other_cashier)
        box = collection.route_id
        other.sudo()._sync_own_collections()
        self.assertEqual(other.total_cobros, 8000)
        caja = self._caja().open_session("0")
        _routes, boxes = self.env["distri.caja"].with_user(self.cashier).pending_receptions()
        self.assertNotIn(box, boxes)
        with self.assertRaisesRegex(UserError, "ya están en la caja de Otro Cajero V115"):
            caja.with_user(self.cashier).receive_route(box, "8000")
        self.assertFalse(caja.movement_ids)
        # a box of a user without a caja is still received
        plain = self._collect(500, user=self.env.user).route_id
        _routes, boxes = self.env["distri.caja"].with_user(self.cashier).pending_receptions()
        self.assertIn(plain, boxes)

    # ------------------------------------------------------------ 5

    def test_5_reset_collection_leaves_the_caja(self):
        caja = self._caja().open_session("0")
        kept = self._collect(3000)
        voided = self._collect(5000)
        caja.sudo()._sync_own_collections()
        self.assertEqual(caja.expected, 8000)
        payment = voided.payment_id.sudo()
        payment.action_draft()
        payment.action_cancel()
        caja.with_user(self.cashier).close_session(self._count(b100=30), left="0")
        self.assertEqual(caja.expected, 3000)
        self.assertFalse(caja.difference)
        self.assertFalse(caja.case_id)
        self.assertEqual(caja.movement_ids.payment_id, kept.payment_id)
        self.assertTrue(any("Se quitó de la caja el cobro" in (m.body or "")
                            for m in caja.message_ids))

    # ------------------------------------------------------------ 6

    def test_6_route_settled_only_from_the_caja(self):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        res = self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=1000))])
        self.assertEqual(res[0]["status"], "ok", res)
        route.sudo().state = "closed"
        route.sudo().write({"cash_counted": 1000, "journal_id": self.cash_journal.id})
        with self.assertRaisesRegex(UserError, "Caja del día"):
            route.with_user(self.manager).action_settle()
        self.assertEqual(route.state, "closed")
        arch = self.env.ref("distri_pedido.view_route_form").arch_db
        self.assertNotIn('name="action_settle"', arch)
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).receive_route(route, "1000")
        self.assertEqual(route.state, "settled")

    # ------------------------------------------------------------ 15

    def test_15_invoice_of_an_unpacked_order_asks_first(self):
        from odoo.addons.distri_pedido.models.factura_sin_armar import can_force
        user = self.env.user  # administración, sales manager, invoicing
        order = self._order(self.partner, self.product_a)
        order.sudo().write({"distri_armado": "pendiente", "distri_bultos": 0})
        with self.assertRaisesRegex(UserError, "todavía no está armado"):
            order.with_user(user)._create_invoices()
        Wizard = self.env["sale.advance.payment.inv"].with_user(user).with_context(
            active_model="sale.order", active_ids=order.ids, active_id=order.id)
        wizard = Wizard.create({"advance_payment_method": "delivered"})
        self.assertEqual(wizard.distri_sin_armar, order.name)
        self.assertTrue(wizard.distri_puede_forzar)
        with self.assertRaisesRegex(UserError, "Facturar igual"):
            wizard.create_invoices()
        self.assertFalse(order.invoice_ids)
        wizard.distri_facturar_sin_armar = True
        wizard.create_invoices()
        self.assertEqual(len(order.invoice_ids), 1)
        self.assertTrue(any("Facturado sin armar" in (m.body or "") for m in order.message_ids))
        self.assertFalse(can_force(self.seller))
        # a packed order: as always
        packed = self._order(self.partner, self.product_a)
        wizard = Wizard.with_context(active_ids=packed.ids, active_id=packed.id).create(
            {"advance_payment_method": "delivered"})
        self.assertFalse(wizard.distri_sin_armar)
        wizard.create_invoices()
        self.assertEqual(len(packed.invoice_ids), 1)

    # ------------------------------------------------------------ 14

    def test_14_twin_outflow_is_one(self):
        caja = self._caja().open_session("10000")
        first = caja.with_user(self.cashier).add_outflow("nafta", "500", who="YPF")
        with self.assertRaises(DuplicateOutflow) as error:
            caja.with_user(self.cashier).add_outflow("nafta", "500", who="YPF")
        self.assertEqual(error.exception.movement, first)
        self.assertIn("ya se guardó", str(error.exception))
        # another one (a different note) is saved
        caja.with_user(self.cashier).add_outflow("nafta", "500", who="YPF", note="segunda carga")
        self.assertEqual(len(caja.movement_ids), 2)
        self.assertEqual(caja.expected, 9000)


@tagged("post_install", "-at_install", "distri_v115")
class TestV115Http(PedidoAbiertoMixin, RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin = new_test_user(cls.env, login="admin_v115", name="Administracion V115",
                                  groups="base.group_user,distri_pedido.grupo_administracion",
                                  **company)
        cls.seller = new_test_user(cls.env, login="vendedor_v115", name="Vendedor V115",
                                   groups="base.group_user,distri_pedido.grupo_vendedor",
                                   **company)
        cls.packer = new_test_user(cls.env, login="armador_v115", name="Armador V115",
                                   groups="base.group_user,distri_pedido.grupo_armador",
                                   **company)
        cls.italo = new_test_user(
            cls.env, login="vende_reparte_v115", name="Vende y Reparte V115",
            groups="base.group_user,distri_pedido.grupo_vendedor,distri_pedido.grupo_reparto",
            **company)
        cls.plain = new_test_user(cls.env, login="interno_v115", name="Interno V115",
                                  groups="base.group_user", **company)
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.pedido_limite_sesion_10min", "100")
        params.set_param("distri_pedido.pedido_limite_ip_10min", "100")
        cls.product = cls.env["product.product"].create({
            "name": "Producto v115", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.plantilla = cls.env["product.template"].create({
            "name": "Producto Pedido V115", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.direct = cls.env["res.partner"].create({
            "name": "KIOSCO DIRECTO V115", "customer_rank": 1, "company_type": "company",
            "phone": "299 412 3456"})
        cls.wholesale = cls.env["res.partner"].create({
            "name": "KIOSCO MAYORISTA V115", "customer_rank": 1, "company_type": "company",
            "distri_es_mayorista": True})
        cls.no_phone = cls.env["res.partner"].create({
            "name": "KIOSCO SIN TELEFONO V115", "customer_rank": 1, "company_type": "company"})

    # ------------------------------------------------------------ 2, 3

    def test_2_3_app_amounts_and_reopen_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("Node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v115.js"), root],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        parse = out["parse"]
        self.assertEqual(parse["$ 14.307.60"], 14307.6)
        self.assertEqual(parse["$ 14.307,60"], 14307.6)
        self.assertEqual(parse["14,307.60"], 14307.6)
        self.assertEqual(parse["$ 25.00"], 25)
        self.assertEqual(parse["25.000"], 25000)
        self.assertEqual(parse["25,000"], 25000)
        self.assertEqual(parse["$ 1.234.567"], 1234567)
        self.assertEqual(parse["$ 12.345"], 12345)
        self.assertEqual(parse["0,50"], 0.5)
        self.assertEqual(out["money"], ["$ 0", "$ 25", "$ 14.307,60", "$ 1.234.567,89",
                                        "$ 0,50", "$ -5"])
        self.assertEqual(out["raw"], ["14307,6", "25", ""])
        self.assertTrue(out["roundTrip"])
        self.assertEqual(out["reopened"], {"closed": False, "route": 50})
        self.assertEqual(out["pendingClose"], {"closed": True, "outbox": 1})
        self.assertEqual(out["serverClosed"], {"closed": True})
        # the page loads app3.js
        self.authenticate(self.driver.login, self.driver.login)
        self.assertIn("app3.js?v=", self.url_open("/reparto").text)

    # ------------------------------------------------------------ 7

    def test_7_remito_only_for_who_prints_it(self):
        Report = self.env["report.distri_pedido.remito_pedido_documento"]
        order = self._order(self.direct, self.product)
        route = self._route(order)
        for user in (self.admin, self.packer, self.driver):
            values = Report.with_user(user)._get_report_values(order.ids)
            self.assertEqual(values["docs"], order, user.login)
        for user in (self.seller, self.other_driver, self.plain):
            with self.assertRaises(AccessError, msg=user.login):
                Report.with_user(user)._get_report_values(order.ids)
        self.assertTrue(route)
        wholesale = self._order(self.wholesale, self.product)
        with self.assertRaisesRegex(AccessError, "mayorista"):
            Report.with_user(self.packer)._get_report_values(wholesale.ids)
        self.assertEqual(Report.with_user(self.admin)._get_report_values(wholesale.ids)["docs"],
                         wholesale)

    # ------------------------------------------------------------ 8

    def test_8_every_staff_role_uploads_receipts(self):
        for login in (self.driver.login, self.packer.login, self.seller.login):
            self.authenticate(login, login)
            self.assertEqual(self.url_open("/comprobantes").status_code, 200, login)
        self.authenticate(self.plain.login, self.plain.login)
        self.assertEqual(self.url_open("/comprobantes").status_code, 403)

    # ------------------------------------------------------------ 9

    def test_9_wholesale_customers_only_for_administracion(self):
        Cash = self.env["distri.cash.collection"]
        for user in (self.driver, self.seller, self.packer):
            names = [r["name"] for r in Cash.with_user(user).cash_search("V115")]
            self.assertIn(self.direct.name, names, user.login)
            self.assertNotIn(self.wholesale.name, names, user.login)
            with self.assertRaises(UserError):
                Cash.with_user(user)._cash_partner(self.wholesale.id)
        names = [r["name"] for r in Cash.with_user(self.admin).cash_search("V115")]
        self.assertIn(self.wholesale.name, names)
        # /cuentas
        App = self.env["distri.account.app"]
        self.assertFalse(App.with_user(self.driver)._app_allowed(self.wholesale))
        self.assertFalse(App.with_user(self.seller)._app_allowed(self.wholesale))
        self.assertTrue(App.with_user(self.driver)._app_allowed(self.direct))
        self.assertTrue(App.with_user(self.admin)._app_allowed(self.wholesale))
        # /pedido customer search
        self.authenticate(self.seller.login, self.seller.login)
        names = [r["nombre"] for r in self.url_open("/pedido/clientes?q=V115").json()]
        self.assertIn(self.direct.name, names)
        self.assertNotIn(self.wholesale.name, names)
        self.authenticate(self.admin.login, self.admin.login)
        names = [r["nombre"] for r in self.url_open("/pedido/clientes?q=V115").json()]
        self.assertIn(self.wholesale.name, names)

    # ------------------------------------------------------------ 10, 11

    def _data(self, **extra):
        data = {"renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 1}]),
                "nombre": "Pedido V115", "telefono": "", "domicilio": "Calle V115 1",
                "entrega": "domicilio"}
        data.update(extra)
        return data

    def test_10_staff_order_takes_the_phone_on_file(self):
        self.authenticate(self.seller.login, self.seller.login)
        res = post_pedido(self, self._data(nombre="Con Telefono V115",
                                           cliente_id=str(self.direct.id)))
        self.assertEqual(res.status_code, 200, res.text[:500])
        order = self.env["sale.order"].sudo().search([("distri_nombre", "=", "Con Telefono V115")])
        self.assertEqual(order.partner_id, self.direct)
        self.assertTrue(order.distri_telefono)
        # without a phone on file, or a new customer: still asked
        res = post_pedido(self, self._data(nombre="Sin Telefono V115",
                                           cliente_id=str(self.no_phone.id)))
        self.assertEqual(res.status_code, 400)
        self.assertIn("teléfono", res.text)
        res = post_pedido(self, self._data(nombre="Nuevo V115"))
        self.assertEqual(res.status_code, 400)
        page = self.url_open("/pedido").text
        self.assertIn("!!fTel.dataset.guardado", page)

    def test_11_one_tap_one_order(self):
        self.authenticate(self.seller.login, self.seller.login)
        csrf, envio = page_tokens(self)
        data = self._data(nombre="Doble V115", telefono="299 412 3456", csrf_token=csrf,
                          envio=envio)
        for _attempt in range(2):
            res = self.url_open("/pedido/enviar", data=data)
            self.assertEqual(res.status_code, 200)
        self.assertEqual(self.env["sale.order"].sudo().search_count(
            [("distri_nombre", "=", "Doble V115")]), 1)
        page = self.url_open("/pedido").text
        self.assertIn("dataset.enviando", page)

    # ------------------------------------------------------------ 13

    def test_13_seller_who_delivers_lands_on_inicio(self):
        self.assertEqual(self.italo._distri_landing(), "")
        self.assertEqual(self.driver._distri_landing(), "/reparto")
        self.assertEqual(self.packer._distri_landing(), "/armado")
        self.authenticate(self.italo.login, self.italo.login)
        res = self.url_open("/tablero")
        self.assertEqual(res.status_code, 200)
        for title in ("Tomar un pedido", "Comprobantes", "Mi reparto"):
            self.assertIn(title, res.text)
        self.assertIn('href="/reparto"', res.text)
        self.assertEqual(self.url_open("/reparto").status_code, 200)

    # ------------------------------------------------------------ /proyecto

    def test_proyecto_new_steps(self):
        body = self.url_open("/proyecto").text
        self.assertIn("Confirmar los pedidos de los vendedores", body)
        self.assertIn("La app te pide el GPS", body)
        self.assertIn("Sin GPS – autorizado", body)
