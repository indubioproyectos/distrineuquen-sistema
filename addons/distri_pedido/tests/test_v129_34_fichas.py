"""19.0.1.129.34 («Etapa 3b», part 2). Everything behind distri_pedido.ui_v2.

- The order, invoice and customer screens (/pedidos/<id>, /facturas/<id>,
  /clientes/<id>): 404 with the flag off; with it on, each role opens
  exactly the records the backend lets it read (an external seller only his
  customers and their orders, an internal seller his portfolio and his
  orders, administración and gerencia everything), packers and drivers are
  refused, and «Ver en Odoo» is only for administración and Sistemas.
- The kit links that opened the Odoo form (/revisar, /agilpedido, «Asignar
  cliente», /deposito/mostrador) open those screens with the flag on and the
  form with it off; «Mis ventas» links each order only with the flag on.
- The Odoo menus only Sistemas uses leave the menu bar of everybody else
  with the flag on, come back with it off, and the Inicio tiles and kit bars
  (_visible_menu_ids) do not change.
- No path creates, edits or deletes an account move, payment or
  reconciliation: counts and the residual sum are checked around every test.
"""
import re

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.fichas import (ODOO_FORMS, ficha_url, may_use,
                                                          record_link, sees_odoo)
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, screen_parents
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
from odoo.addons.distri_pedido.models.menus_sistemas import (SISTEMAS_ONLY_MENUS,
                                                             sistemas_only_menu_ids)

from .common_pedido import ensure_company_chart

PARENT_RE = re.compile(r'<body\b[^>]*\bdata-parent="([^"]*)"', re.S)


def _ledger(env):
    """Counts and residual sum of everything that makes a customer balance."""
    env = env(su=True)
    lines = env["account.move.line"].search([])
    return (env["account.move"].search_count([]), env["account.payment"].search_count([]),
            env["account.partial.reconcile"].search_count([]),
            env["account.full.reconcile"].search_count([]), len(lines),
            round(sum(lines.mapped("amount_residual")), 2))


@tagged("post_install", "-at_install", "distri_v129_34")
class TestFichas(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_company_chart(cls.env)
        company = cls.env.company
        cls.tag = cls.env["res.partner.category"].create({"name": "Cartera F34"})

        def user(login, groups, **vals):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups="base.group_user," + groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if vals:
                new.write(vals)
            return new

        cls.gerente = user("gerente_f34", "distri_pedido.grupo_gerencia")
        cls.admin = user("admin_f34", "distri_pedido.grupo_administracion")
        cls.cajero = user("cajero_f34", "distri_pedido.grupo_administracion,"
                                        "distri_pedido.grupo_cajero")
        cls.vendedor = user("vendedor_f34", "distri_pedido.grupo_vendedor",
                            etiqueta_cartera_id=cls.tag.id)
        cls.otro = user("otro_f34", "distri_pedido.grupo_vendedor")
        cls.externo = user("externo_f34", "distri_pedido.grupo_vendedor",
                           distri_tipo_vendedor="externo")
        cls.armador = user("armador_f34", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_f34", "distri_pedido.grupo_reparto")
        cls.sistemas = user("sistemas_f34", "base.group_system")

        Partner = cls.env["res.partner"]
        cls.cartera = Partner.create({"name": "CARTERA F34", "customer_rank": 1,
                                      "is_company": True, "vat": "20111111112",
                                      "category_id": [Command.link(cls.tag.id)]})
        cls.contacto = Partner.create({"name": "CONTACTO CARTERA F34",
                                       "parent_id": cls.cartera.id})
        cls.ajeno = Partner.create({"name": "AJENO F34", "customer_rank": 1})
        cls.propio = Partner.create({"name": "PROPIO EXTERNO F34", "customer_rank": 1})

        cls.product = cls.env["product.product"].create({
            "name": "Yerba F34", "type": "consu", "invoice_policy": "order",
            "list_price": 100.0})
        cls.o_cartera = cls._order(cls.cartera, cls.vendedor, invoice=True)
        cls.o_ajeno = cls._order(cls.ajeno, cls.otro, invoice=True)
        cls.o_propio = cls._order(cls.propio, cls.externo)
        cls.invoice = cls.o_cartera.invoice_ids
        cls.invoice_ajeno = cls.o_ajeno.invoice_ids
        # a partial payment, so the invoice shows a payment and a balance
        journal = cls.env["account.journal"].search(
            [("type", "=", "cash"), ("company_id", "=", company.id)], limit=1)
        cls.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=cls.invoice.ids).create({
                "journal_id": journal.id, "amount": 300.0,
                "payment_date": fields.Date.today()}).action_create_payments()
        # a quotation of today, held: it shows on /revisar with «Liberar»
        cls.presupuesto = cls.env["sale.order"].create({
            "partner_id": cls.cartera.id, "user_id": cls.vendedor.id,
            "order_line": [Command.create({"product_id": cls.product.id,
                                           "product_uom_qty": 2, "price_unit": 100.0,
                                           "tax_ids": [Command.clear()]})]})
        cls.presupuesto.sudo().write({"distri_retenido": True,
                                      "distri_retenido_motivo": "Prueba F34"})
        cls.daily = [cls.gerente, cls.admin, cls.cajero, cls.vendedor, cls.otro, cls.externo,
                     cls.sistemas]

    @classmethod
    def _order(cls, partner, salesperson, invoice=False):
        order = cls.env["sale.order"].create({
            "partner_id": partner.id, "user_id": salesperson.id,
            "order_line": [Command.create({"product_id": cls.product.id,
                                           "product_uom_qty": 10, "price_unit": 100.0,
                                           "tax_ids": [Command.clear()]})]})
        order.action_confirm()
        order.sudo().write({"distri_armado": "armado", "distri_bultos": 1})
        if invoice:
            order._create_invoices().action_post()
        return order

    def setUp(self):
        super().setUp()
        self.before = _ledger(self.env)

    def tearDown(self):
        self.assertEqual(_ledger(self.env), self.before, "no customer balance moved")
        super().tearDown()

    def _flag(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _get(self, path):
        return self.url_open(path, allow_redirects=False)

    def _paths(self):
        return {
            ("sale.order", self.o_cartera.id): "/pedidos/%s" % self.o_cartera.id,
            ("sale.order", self.o_ajeno.id): "/pedidos/%s" % self.o_ajeno.id,
            ("sale.order", self.o_propio.id): "/pedidos/%s" % self.o_propio.id,
            ("account.move", self.invoice.id): "/facturas/%s" % self.invoice.id,
            ("account.move", self.invoice_ajeno.id): "/facturas/%s" % self.invoice_ajeno.id,
            ("res.partner", self.cartera.id): "/clientes/%s" % self.cartera.id,
            ("res.partner", self.ajeno.id): "/clientes/%s" % self.ajeno.id,
            ("res.partner", self.propio.id): "/clientes/%s" % self.propio.id,
        }

    def _backend_reads(self, user, model, rec_id):
        """What the backend lets the user read (ACLs and record rules), plus
        the kit's external-seller check for a customer."""
        record = self.env[model].with_user(user).browse(rec_id)
        if not record.has_access("read"):
            return False
        if model == "res.partner":
            return user._distri_may_reach_customer(record.sudo())
        return True

    # ------------------------------------------------------------ flag off

    def test_flag_off_screens_404_and_links_to_the_form(self):
        self._flag(False)
        self._login(self.admin)
        for path in self._paths().values():
            self.assertEqual(self._get(path).status_code, 404, path)
        env = self.env(user=self.admin)
        self.assertEqual(record_link(env, "pedido", 7, "/revisar"), "/odoo/sale.order/7")
        self.assertEqual(record_link(env, "cliente", 7), "/odoo/res.partner/7")
        html = self._get("/revisar").text
        self.assertIn('href="/odoo/sale.order/%s"' % self.presupuesto.id, html)
        self.assertNotIn("/pedidos/", html)
        self.assertIn("Abrir en el sistema", html)
        html = self._get("/agilpedido/creado/%s" % self.o_cartera.id).text
        self.assertIn('href="/odoo/sale.order/%s"' % self.o_cartera.id, html)
        self._login(self.vendedor)
        html = self._get("/mis-ventas?periodo=mes").text
        self.assertIn(self.o_cartera.name, html)
        self.assertNotIn("/pedidos/", html)
        self.assertNotIn("mv-name--link", html)

    # ------------------------------------------------------------ permissions

    def test_each_role_sees_what_the_backend_shows(self):
        self._flag(True)
        for user in self.daily:
            self._login(user)
            for (model, rec_id), path in self._paths().items():
                res = self._get(path)
                expected = 200 if self._backend_reads(user, model, rec_id) else 404
                self.assertEqual(res.status_code, expected, (user.login, path))

    def test_external_seller_only_his_customers(self):
        self._flag(True)
        self._login(self.externo)
        self.assertEqual(self._get("/clientes/%s" % self.propio.id).status_code, 200)
        self.assertEqual(self._get("/pedidos/%s" % self.o_propio.id).status_code, 200)
        for path in ("/clientes/%s" % self.ajeno.id, "/pedidos/%s" % self.o_ajeno.id,
                     "/clientes/%s" % self.cartera.id, "/pedidos/%s" % self.o_cartera.id,
                     "/facturas/%s" % self.invoice_ajeno.id, "/facturas/%s" % self.invoice.id):
            res = self._get(path)
            self.assertEqual(res.status_code, 404, path)
            self.assertNotIn("AJENO F34", res.text)
            self.assertNotIn("CARTERA F34", res.text)

    def test_internal_seller_portfolio_and_own_orders(self):
        self._flag(True)
        self._login(self.vendedor)
        res = self._get("/clientes/%s" % self.cartera.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn("CARTERA F34", res.text)
        self.assertIn(self.o_cartera.name, res.text)
        self.assertEqual(self._get("/pedidos/%s" % self.o_cartera.id).status_code, 200)
        self.assertEqual(self._get("/clientes/%s" % self.ajeno.id).status_code, 404)
        self.assertEqual(self._get("/pedidos/%s" % self.o_ajeno.id).status_code, 404)
        # another seller without portfolio: neither
        self._login(self.otro)
        self.assertEqual(self._get("/clientes/%s" % self.cartera.id).status_code, 404)
        self.assertEqual(self._get("/pedidos/%s" % self.o_cartera.id).status_code, 404)

    def test_packers_drivers_and_customers_refused(self):
        self._flag(True)
        for user in (self.armador, self.repartidor):
            self.assertFalse(may_use(user))
            self._login(user)
            for path in self._paths().values():
                self.assertEqual(self._get(path).status_code, 403, (user.login, path))
        self.authenticate(None, None)
        res = self._get("/pedidos/%s" % self.o_cartera.id)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/web/login", res.headers.get("Location", ""))

    def test_missing_record_and_contact(self):
        self._flag(True)
        self._login(self.admin)
        self.assertEqual(self._get("/pedidos/999999999").status_code, 404)
        self.assertEqual(self._get("/facturas/999999999").status_code, 404)
        # a contact goes to its company's screen
        res = self._get("/clientes/%s" % self.contacto.id)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/clientes/%s" % self.cartera.id))
        # a move that is not a customer invoice is not shown here
        entry = self.env["account.move"].search([("move_type", "=", "entry")], limit=1)
        if entry:
            self.assertEqual(self._get("/facturas/%s" % entry.id).status_code, 404)

    # ------------------------------------------------------------ content

    def test_order_screen(self):
        self._flag(True)
        self._login(self.admin)
        res = self._get("/pedidos/%s?desde=/revisar" % self.o_cartera.id)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertEqual(PARENT_RE.search(html).group(1), "/revisar")
        for text in (self.o_cartera.name, "CARTERA F34", "Yerba F34", "Confirmado",
                     "Armado", "/dn/fichas.css?v=", "dn-bottom--rol"):
            self.assertIn(text, html)
        self.assertIn('href="/clientes/%s"' % self.cartera.id, html)
        self.assertIn('href="/facturas/%s?desde=/pedidos/%s"' % (self.invoice.id,
                                                               self.o_cartera.id), html)
        self.assertIn('href="/odoo/sale.order/%s"' % self.o_cartera.id, html)
        self.assertIn("Ver en Odoo", html)
        self.assertIn('href="/cuentas#cuenta/%s"' % self.cartera.id, html)
        self.assertIn('href="/cobro?cliente=%s"' % self.cartera.id, html)
        # a held quotation: «Liberar» posts to /revisar's own route
        html = self._get("/pedidos/%s" % self.presupuesto.id).text
        self.assertIn('action="/revisar/%s/liberar"' % self.presupuesto.id, html)
        self.assertIn("Es un presupuesto", html)
        self.assertEqual(PARENT_RE.search(html).group(1), "/tablero")

    def test_seller_has_no_odoo_link_nor_admin_actions(self):
        self._flag(True)
        self.assertFalse(sees_odoo(self.vendedor))
        self.assertTrue(sees_odoo(self.admin) and sees_odoo(self.sistemas))
        self._login(self.vendedor)
        html = self._get("/pedidos/%s" % self.presupuesto.id).text
        self.assertNotIn("Ver en Odoo", html)
        self.assertNotIn("/odoo/", html)
        self.assertNotIn("/liberar", html)
        self.assertNotIn("/revisar", html)

    def test_invoice_screen(self):
        self._flag(True)
        self._login(self.admin)
        res = self._get("/facturas/%s?desde=/pedidos/%s" % (self.invoice.id, self.o_cartera.id))
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertEqual(PARENT_RE.search(html).group(1), "/pedidos/%s" % self.o_cartera.id)
        self.assertIn(self.invoice.name, html)
        self.assertIn("Pago parcial", html)
        self.assertIn("$ 700", html)  # 1.000 minus the 300 paid
        self.assertIn("$ 300", html)
        self.assertIn('href="/pedidos/%s?desde=/facturas/%s"' % (self.o_cartera.id,
                                                               self.invoice.id), html)
        self.assertIn('href="/odoo/account.move/%s"' % self.invoice.id, html)
        self.assertIn('href="/cobro?cliente=%s"' % self.cartera.id, html)

    def test_customer_screen(self):
        self._flag(True)
        self._login(self.cajero)
        res = self._get("/clientes/%s?desde=/cuentas" % self.cartera.id)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertEqual(PARENT_RE.search(html).group(1), "/cuentas")
        for text in ("CARTERA F34", "20111111112", "Saldo", self.o_cartera.name,
                     self.invoice.name, "Cartera F34"):
            self.assertIn(text, html)
        self.assertIn('href="/facturas/%s?desde=/clientes/%s"' % (self.invoice.id,
                                                                self.cartera.id), html)
        self.assertIn('href="/odoo/res.partner/%s"' % self.cartera.id, html)

    def test_desde_only_accepts_declared_parents(self):
        self._flag(True)
        self._login(self.admin)
        for desde in ("//evil.example/x", "https://evil.example", "/web/login", "/caja",
                      "/pedidos/abc", "javascript:alert(1)"):
            html = self._get("/pedidos/%s?desde=%s" % (self.o_cartera.id, desde)).text
            self.assertEqual(PARENT_RE.search(html).group(1), "/tablero", desde)
        html = self._get("/pedidos/%s?desde=/clientes/%s" % (self.o_cartera.id,
                                                             self.cartera.id)).text
        self.assertEqual(PARENT_RE.search(html).group(1), "/clientes/%s" % self.cartera.id)
        self.assertEqual(screen_parents("/facturas/3"), ("/pedidos/<id>", "/clientes/<id>",
                                                        "inicio"))

    # ------------------------------------------------------------ links switch

    def test_flag_on_kit_links_open_the_screens(self):
        self._flag(True)
        env = self.env(user=self.admin)
        self.assertEqual(record_link(env, "pedido", 7, "/revisar"), "/pedidos/7?desde=/revisar")
        self.assertEqual(record_link(env, "cliente", 7), "/clientes/7")
        self.assertEqual(ficha_url("factura", 5, "/pedidos/4"), "/facturas/5?desde=/pedidos/4")
        self._login(self.admin)
        html = self._get("/revisar").text
        self.assertIn('href="/pedidos/%s?desde=/revisar"' % self.presupuesto.id, html)
        self.assertIn("Ver el pedido", html)
        self.assertNotIn('href="/odoo/sale.order/', html)
        html = self._get("/agilpedido/creado/%s" % self.o_cartera.id).text
        self.assertIn('href="/pedidos/%s?desde=/agilpedido/creado/%s"' % (
            self.o_cartera.id, self.o_cartera.id), html)
        self.assertNotIn("/odoo/sale.order/", html)
        # «Mis ventas»: each order the seller may open links to its screen
        self._login(self.vendedor)
        html = self._get("/mis-ventas?periodo=mes").text
        self.assertIn('href="/pedidos/%s?desde=/mis-ventas"' % self.o_cartera.id, html)
        self.assertEqual(self._get("/pedidos/%s?desde=/mis-ventas" % self.o_cartera.id)
                         .status_code, 200)

    # ------------------------------------------------------------ menus

    def _menus(self, user):
        return set(self.env["ir.ui.menu"].with_user(user).load_menus(False))

    def test_sistemas_menus_hidden_by_the_flag(self):
        hidden = set(sistemas_only_menu_ids(self.env))
        self.assertEqual(len(hidden), len(SISTEMAS_ONLY_MENUS), "every listed menu exists")
        # the menus the kit links to or reads stay
        for xmlid in ("distri_pedido.menu_reparto_despacho", "distri_pedido.menu_reparto_settle",
                      "distri_pedido.menu_reparto_cases", "distri_pedido.menu_reparto_charges",
                      "distri_pedido.menu_reparto_promises", "distri_pedido.menu_reparto_zones",
                      "distri_pedido.menu_cobros_root", "distri_pedido.menu_caja_app",
                      "distri_pedido.menu_armado_app", "distri_pedido.menu_transicion_app",
                      "distri_pedido.menu_caja_historial"):
            self.assertNotIn(self.env.ref(xmlid).id, hidden, xmlid)
        transicion = self.env.ref("distri_pedido.menu_transicion").id
        importar = self.env.ref("distri_pedido.menu_importar_pedido").id
        app_importar = self.env.ref("distri_pedido.menu_app_importar").id
        Menu = self.env["ir.ui.menu"]
        users = (self.gerente, self.admin, self.cajero, self.vendedor, self.armador)
        visible = {u: Menu.with_user(u)._visible_menu_ids() for u in users}
        tiles = {u: [t["key"] for t in staff_tiles(self.env(user=u)) if t["key"] != "equipo"]
                 for u in users}

        self._flag(False)
        before = {u: self._menus(u) for u in users}
        self.assertIn(transicion, before[self.admin])
        self.assertIn(importar, before[self.admin])

        self._flag(True)
        for user in users:
            menus = self._menus(user)
            self.assertFalse(menus & hidden, user.login)
            # nothing else changed: the daily menus stay
            self.assertEqual(menus, before[user] - hidden - {importar} - set(
                Menu.sudo().search([("id", "child_of", list(hidden))]).ids), user.login)
            self.assertEqual(Menu.with_user(user)._visible_menu_ids(), visible[user], user.login)
            # the tiles too («Equipo» is the only tile ui_v2 itself adds)
            self.assertEqual([t["key"] for t in staff_tiles(self.env(user=user))
                              if t["key"] != "equipo"], tiles[user], user.login)
        # the same actions stay under the «Transición AgilPedido» app
        self.assertIn(app_importar, self._menus(self.admin))
        # Sistemas keeps every menu
        self.assertIn(transicion, self._menus(self.sistemas))
        # and turning the flag off brings them back at once
        self._flag(False)
        self.assertEqual(self._menus(self.admin), before[self.admin])
        # no access right was touched: the action still opens
        self.assertTrue(self.env["distri.agil.equivalencia"].with_user(self.admin)
                        .has_access("read"))

    def test_odoo_forms_map(self):
        self.assertEqual(set(ODOO_FORMS), {"pedido", "factura", "cliente"})
        for kind in ODOO_FORMS:
            self.assertIsNotNone(screen_parents(ficha_url(kind, 1)), kind)
