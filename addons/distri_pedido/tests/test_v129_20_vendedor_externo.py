"""19.0.1.129.20: internal and external sellers (res.users
distri_tipo_vendedor). An external seller reaches only his customers
(res.users._distri_chosen_customers: the customers of the sale orders
mis_ventas.de_vendedor counts for him or that he created, and the customers
he created) in «Cuentas (celular)» and through the partner record rule; an
internal seller keeps today's behavior; nobody changes his own type."""
import base64
import json
import re

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import MENSAJE_CLIENTE_AJENO
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido

BIG = 960_000_000.0
# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")


@tagged("post_install", "-at_install", "distri_v129_20")
class TestVendedorExterno(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.tag = cls.env["res.partner.category"].create({"name": "Cartera Externo V20"})

        def user(login, groups, **vals):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups="base.group_user," + groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if vals:
                new.write(vals)
            return new

        cls.externo = user("externo_v20", "distri_pedido.grupo_vendedor",
                           distri_tipo_vendedor="externo")
        cls.interno = user("interno_v20", "distri_pedido.grupo_vendedor",
                           etiqueta_cartera_id=cls.tag.id)
        cls.otro = user("otro_v20", "distri_pedido.grupo_vendedor")
        cls.admin_u = user("admin_v20", "distri_pedido.grupo_administracion")
        cls.system_u = user("sistema_v20", "base.group_system")
        cls.rights_u = user("permisos_v20", "base.group_erp_manager")

        Partner = cls.env["res.partner"]
        # chose the external seller in «Mi vendedor es»
        cls.elegido = Partner.create({"name": "ELEGIDO V20", "customer_rank": 1,
                                      "is_company": True})
        cls.contacto = Partner.create({"name": "CONTACTO ELEGIDO V20",
                                       "parent_id": cls.elegido.id})
        # no choice: the external seller is the salesperson of the order
        cls.propio = Partner.create({"name": "PROPIO V20", "customer_rank": 1})
        # another seller's customer, untagged (what `category_id in [False]`
        # exposed to a seller without portfolio)
        cls.ajeno = Partner.create({"name": "AJENO V20", "customer_rank": 1})
        # chose the external seller, but the order's salesperson is another
        cls.ganado = Partner.create({"name": "GANADO V20", "customer_rank": 1})
        # internal portfolio
        cls.cartera = Partner.create({"name": "CARTERA V20", "customer_rank": 1,
                                      "category_id": [Command.link(cls.tag.id)]})

        web = cls.env["distri.vendedor.web"].sudo().create(
            {"name": "Externo V20", "user_id": cls.externo.id})
        cls._order(cls.elegido, cls.otro, web)
        cls._order(cls.propio, cls.externo)
        cls._order(cls.ajeno, cls.otro)
        # the customer gave the order to the external seller: it counts for
        # him only, not for the salesperson
        cls._order(cls.ganado, cls.otro, web)

        Mov = cls.env["distri.cuenta.movimiento"].sudo()
        Mov.create([
            dict(partner_id=partner.id, fecha="2025-06-10", comprobante="0001/2025/%d" % base,
                 concepto="Factura vieja", tipo="factura", debe=BIG + base, haber=0.0,
                 origen_id=base)
            for partner, base in ((cls.elegido, 99610), (cls.propio, 99620),
                                  (cls.ajeno, 99630), (cls.cartera, 99640))])

    @classmethod
    def _order(cls, partner, salesperson, web=None, state=None):
        order = cls.env["sale.order"].sudo().create({
            "partner_id": partner.id, "user_id": salesperson.id,
            "distri_vendedor_web_id": web.id if web else False})
        if state:
            order.write({"state": state})
        return order

    def _app(self, user):
        return self.env["distri.account.app"].with_user(user)

    def _listed(self, user):
        data = self._app(user).app_debtors(settled=True)
        return {d["id"] for d in data["debtors"] + data["settled"] + data["uncollectible"]}

    def _readable(self, user, partners):
        return self.env["res.partner"].with_user(user).search([("id", "in", partners.ids)])

    # ------------------------------------------------------------ the set

    def test_chosen_customers(self):
        chosen = self.externo._distri_chosen_customers()
        self.assertEqual(chosen, self.elegido | self.propio | self.ganado)
        self.assertTrue(self.externo._distri_is_external_seller())
        self.assertFalse(self.interno._distri_is_external_seller())
        self.assertEqual(self.elegido.distri_vendedor_elegido_ids, self.externo)
        self.assertEqual(self.contacto.distri_vendedor_elegido_ids, self.externo)
        self.assertFalse(self.ajeno.distri_vendedor_elegido_ids, "only external sellers")
        # administración never counts as external, whatever its type
        self.admin_u.sudo().distri_tipo_vendedor = "externo"
        self.assertFalse(self.admin_u._distri_is_external_seller())

    def test_orders_he_took_and_customers_he_created(self):
        """(b) an order he creates from his session (/pedido creates it with
        sudo, which keeps his uid) gives him its customer, even when the
        customer picked another seller; (c) so does a customer he creates
        («Cliente nuevo» of /cobro, also with sudo). Neither a contact he
        adds to another company nor the generic partner does."""
        Partner = self.env["res.partner"]
        web_otro = self.env["distri.vendedor.web"].sudo().create(
            {"name": "Otro V20", "user_id": self.otro.id})
        tomado = Partner.create({"name": "TOMADO V20", "customer_rank": 1})
        order = self.env["sale.order"].with_user(self.externo).sudo().create({
            "partner_id": tomado.id, "user_id": self.otro.id,
            "distri_vendedor_web_id": web_otro.id})
        self.assertEqual(order.create_uid, self.externo)
        nuevo = Partner.with_user(self.externo).sudo().create(
            {"name": "NUEVO V20", "customer_rank": 1})
        self.assertEqual(nuevo.create_uid, self.externo)
        Partner.with_user(self.externo).sudo().create(
            {"name": "CONTACTO AJENO V20", "parent_id": self.ajeno.id})
        generico = Partner.create({"name": "GENERICO V20", "distri_generic_partner": True})
        self.env["sale.order"].with_user(self.externo).sudo().create(
            {"partner_id": generico.id})

        chosen = self.externo._distri_chosen_customers()
        self.assertIn(tomado, chosen)
        self.assertIn(nuevo, chosen)
        self.assertNotIn(self.ajeno, chosen)
        self.assertNotIn(generico, chosen)
        listed = self._listed(self.externo)
        self.assertTrue({tomado.id, nuevo.id} <= listed)
        self.assertNotIn(self.ajeno.id, listed)
        self.assertTrue(self._app(self.externo).app_account(nuevo.id))
        with self.assertRaises(AccessError):
            self._app(self.externo).app_account(self.ajeno.id)
        readable = self._readable(self.externo, tomado | nuevo | self.ajeno | generico)
        self.assertEqual(readable, tomado | nuevo)

    # ------------------------------------------------------------ /cuentas

    def test_external_seller_lists_only_his_customers(self):
        listed = self._listed(self.externo)
        self.assertTrue({self.elegido.id, self.propio.id} <= listed)
        self.assertNotIn(self.ajeno.id, listed)
        self.assertNotIn(self.cartera.id, listed)
        # a cancelled order still records the choice
        nuevo = self.env["res.partner"].create({"name": "CANCELADO V20", "customer_rank": 1})
        self._order(nuevo, self.externo, state="cancel")
        self.assertIn(nuevo.id, self._listed(self.externo))

    def test_external_seller_denied_on_another_account(self):
        app = self._app(self.externo)
        account = app.app_account(self.elegido.id)
        self.assertEqual(account["partner"]["name"], "ELEGIDO V20")
        self.assertTrue(account["share"]["pdf"], "the PDF goes through the partner rule")
        # a contact of a chosen customer opens the customer's account
        self.assertEqual(app.app_account(self.contacto.id)["partner"]["id"], self.elegido.id)
        for partner in (self.ajeno, self.cartera):
            with self.assertRaises(AccessError):
                app.app_account(partner.id)
            with self.assertRaises(AccessError):
                app.app_line(partner.id, "online:1")
            with self.assertRaises(AccessError):
                app.app_statement_pdf(partner.id)
            with self.assertRaises(AccessError):
                app.app_add_comment(partner.id, "no")
            with self.assertRaises(AccessError):
                app.app_receipt(partner.id, 1)
        # a comment on another customer's account cannot be deleted either
        comment = self._app(self.admin_u).app_add_comment(self.ajeno.id, "de admin")
        with self.assertRaises(AccessError):
            app.app_delete_comment(comment["id"])
        own = app.app_add_comment(self.elegido.id, "mío")
        self.assertTrue(app.app_delete_comment(own["id"]))

        self.authenticate(self.externo.login, self.externo.login)
        res = self.url_open("/cuentas/api/account?id=%d" % self.ajeno.id, allow_redirects=False)
        self.assertEqual(res.status_code, 403)
        res = self.url_open("/cuentas/api/account?id=%d" % self.elegido.id, allow_redirects=False)
        self.assertTrue(res.json()["ok"])
        res = self.url_open("/cuentas/api/comment", data=json.dumps(
            {"partner_id": self.ajeno.id, "body": "no"}), headers={"Content-Type": "application/json"})
        self.assertEqual(res.status_code, 403)

    def test_external_seller_cash_sheet_hides_other_customers(self):
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": self.elegido.id,
            "amount": 700.0, "date": "2026-11-20", "memo": "Efectivo V20",
            "journal_id": self.env["account.journal"].search(
                [("type", "=", "cash"), ("company_id", "=", self.env.company.id)],
                limit=1).id})
        payment.action_post()
        self.env["distri.cash.sheet"].sudo().create({
            "name": "PLANILLA V20", "collector": "SIMON", "date": "2026-11-20",
            "source_key": "xlsx:test:v20", "line_ids": [
                Command.create({"sequence": 10, "client_raw": "ELEGIDO", "amount_paid": 700.0,
                                "partner_id": self.elegido.id, "payment_id": payment.id}),
                Command.create({"sequence": 20, "client_raw": "AJENO", "amount_paid": 900.0,
                                "partner_id": self.ajeno.id}),
            ]})
        key = next(r["line_key"] for r in self.elegido._distri_statement_rows()
                   if r["res_model"] == "account.move" and r["res_id"] == payment.move_id.id)
        sheet = self._app(self.externo).app_line(self.elegido.id, key)["cash"]["sheet"]
        self.assertEqual([r["client"] for r in sheet["rows"]], ["ELEGIDO"])
        self.assertEqual(sheet["hidden_count"], 1)
        self.assertNotIn("AJENO", str(sheet))
        # an internal seller still sees the whole sheet
        sheet = self._app(self.interno).app_line(self.elegido.id, key)["cash"]["sheet"]
        self.assertEqual(sheet["hidden_count"], 0)

    def test_internal_seller_unchanged(self):
        listed = self._listed(self.interno)
        self.assertTrue({self.elegido.id, self.propio.id, self.ajeno.id, self.cartera.id}
                        <= listed)
        app = self._app(self.interno)
        for partner in (self.ajeno, self.elegido, self.cartera):
            self.assertTrue(app.app_account(partner.id))
        # backend: his portfolio, as before
        everyone = self.elegido | self.contacto | self.propio | self.ajeno | self.ganado \
            | self.cartera
        self.assertEqual(self._readable(self.interno, everyone), self.cartera)

    # ------------------------------------------------------------ backend rule

    def test_partner_rule_of_external_seller(self):
        everyone = self.elegido | self.contacto | self.propio | self.ajeno | self.ganado \
            | self.cartera
        self.assertEqual(self._readable(self.externo, everyone),
                         self.elegido | self.contacto | self.propio | self.ganado)
        with self.assertRaises(AccessError):
            self.ajeno.with_user(self.externo).read(["name"])
        # a new order counts at once (the rule's domain is cached, not the set)
        self._order(self.ajeno, self.externo)
        self.assertIn(self.ajeno, self._readable(self.externo, everyone))
        # administración keeps every customer
        self.assertEqual(self._readable(self.admin_u, everyone), everyone)

    def test_switching_type_applies_at_once(self):
        everyone = self.elegido | self.propio | self.ajeno | self.cartera
        self.assertEqual(self._readable(self.interno, everyone), self.cartera)
        self.interno.with_user(self.system_u).write({"distri_tipo_vendedor": "externo"})
        self.assertFalse(self._readable(self.interno, everyone),
                         "none of these is his: he reads none")
        self.assertFalse(self._listed(self.interno))

    # ------------------------------------------------------------ who changes it

    def test_seller_cannot_change_his_own_type(self):
        for seller, value in ((self.externo, "interno"), (self.interno, "externo")):
            with self.assertRaises(AccessError):
                seller.with_user(seller).write({"distri_tipo_vendedor": value})
        self.assertNotIn("distri_tipo_vendedor", self.env["res.users"].SELF_WRITEABLE_FIELDS)
        # «Permisos de acceso» may edit users, but not this field
        with self.assertRaises(AccessError):
            self.externo.with_user(self.rights_u).write({"distri_tipo_vendedor": "interno"})
        self.assertEqual(self.externo.distri_tipo_vendedor, "externo")
        self.assertEqual(self.interno.distri_tipo_vendedor, "interno")
        # system administrators may (administración too, through the guard)
        self.externo.with_user(self.system_u).write({"distri_tipo_vendedor": "interno"})
        self.assertEqual(self.externo.distri_tipo_vendedor, "interno")
        self.assertEqual(self.env["res.users"].new({}).distri_tipo_vendedor, "interno")
        view = self.env.ref("distri_pedido.view_users_form_vendedor")
        self.assertIn('name="distri_tipo_vendedor"', view.arch_db)

    # ------------------------------------------------------------ other backend rules

    RULE_MODELS = ("distri.cuenta.movimiento", "distri.payer.identity", "distri.account.comment",
                   "distri.owed.allocation", "distri.cash.sheet.line",
                   "distri.promised.transfer", "distri.promised.transfer.history")

    def test_portfolio_rules_follow_the_seller_type(self):
        """Every rule keyed on the portfolio tag: an external seller gets
        his customers, an internal one his tag, as before."""
        Rule = self.env["ir.rule"]
        for model in self.RULE_MODELS:
            external = str(Rule.with_user(self.externo)._compute_domain(model, "read"))
            internal = str(Rule.with_user(self.interno)._compute_domain(model, "read"))
            self.assertIn("distri_vendedor_elegido_ids", external, model)
            self.assertNotIn("category_id", external, model)
            self.assertIn("category_id", internal, model)
            self.assertNotIn("distri_vendedor_elegido_ids", internal, model)

        def readable(user, model, records):
            return self.env[model].with_user(user).search([("id", "in", records.ids)])

        Mov = self.env["distri.cuenta.movimiento"].sudo()
        movs = Mov.search([("origen_id", "in", (99610, 99630, 99640))])
        self.assertEqual(readable(self.externo, Mov._name, movs).partner_id, self.elegido)
        self.assertEqual(readable(self.interno, Mov._name, movs).partner_id, self.cartera)

        Identity = self.env["distri.payer.identity"].sudo()
        ids = Identity.create([
            {"partner_id": self.contacto.id, "kind": "cuit", "value": "20999000611"},
            {"partner_id": self.ajeno.id, "kind": "cuit", "value": "20999000631"},
            {"partner_id": self.cartera.id, "kind": "cuit", "value": "20999000641"}])
        # stored on the commercial partner
        self.assertEqual(readable(self.externo, Identity._name, ids).partner_id, self.elegido)
        self.assertEqual(readable(self.interno, Identity._name, ids).partner_id, self.cartera)

        Comment = self.env["distri.account.comment"].sudo()
        comments = Comment.create([{"partner_id": p.id, "body": "nota %s" % p.name}
                                   for p in (self.elegido, self.ajeno, self.cartera)])
        self.assertEqual(readable(self.externo, Comment._name, comments).partner_id,
                         self.elegido)
        self.assertEqual(readable(self.interno, Comment._name, comments).partner_id,
                         self.cartera)

    # ------------------------------------------------------------ /cobro, /comprobantes

    def test_cash_screens_only_his_customers(self):
        Cash = self.env["distri.cash.collection"]
        found = {r["id"] for r in Cash.with_user(self.externo).cash_search("V20")}
        self.assertIn(self.elegido.id, found)
        self.assertTrue(found <= {self.elegido.id, self.propio.id, self.ganado.id}, found)
        self.assertIn(self.ajeno.id, {r["id"] for r in Cash.with_user(self.interno).cash_search(
            "V20")}, "an internal seller finds every customer, as before")
        self.assertTrue(Cash.with_user(self.externo).cash_account(self.elegido.id))
        for partner in (self.ajeno, self.cartera):
            with self.assertRaises(AccessError):
                Cash.with_user(self.externo).cash_account(partner.id)
        self.assertTrue(Cash.with_user(self.interno).cash_account(self.ajeno.id))

        self.authenticate(self.externo.login, self.externo.login)
        # the page shows the refusal (AccessError is a UserError) and nothing of him
        page = self.url_open("/cobro?cliente=%d" % self.ajeno.id)
        self.assertIn("Ese cliente no es tuyo", page.text)
        self.assertNotIn(self.ajeno.name, page.text)
        self.assertIn(self.elegido.name, self.url_open("/cobro?cliente=%d" % self.elegido.id).text)
        res = self.url_open("/comprobantes/clientes?q=V20").json()
        self.assertTrue({r["id"] for r in res["results"]}
                        <= {self.elegido.id, self.propio.id, self.ganado.id})
        page = self.url_open("/comprobantes")
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        before = self.env["distri.whatsapp.message"].sudo().search_count([])
        res = self.url_open("/comprobantes/subir", data={
            "csrf_token": csrf, "cliente_id": self.ajeno.id, "importes": "1.000"},
            files=[("archivos", ("v20.png", PNG, "image/png"))])
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.env["distri.whatsapp.message"].sudo().search_count([]), before)


@tagged("post_install", "-at_install", "distri_v129_20")
class TestVendedorExternoPedido(PedidoAbiertoMixin, HttpCase):
    """/pedido: an external seller searches and takes orders only for his
    customers; without a customer (the generic one) he still can."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company

        def user(login, groups, **vals):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups="base.group_user," + groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if vals:
                new.write(vals)
            return new

        cls.externo = user("externo_pv20", "distri_pedido.grupo_vendedor",
                           distri_tipo_vendedor="externo")
        cls.interno = user("interno_pv20", "distri_pedido.grupo_vendedor")
        Partner = cls.env["res.partner"]
        cls.suyo = Partner.create({"name": "Almacen Suyo PV20", "customer_rank": 1,
                                   "phone": "2995550120"})
        cls.ajeno = Partner.create({"name": "Almacen Ajeno PV20", "customer_rank": 1,
                                    "phone": "2995550121"})
        cls.env["sale.order"].sudo().create({"partner_id": cls.suyo.id,
                                             "user_id": cls.externo.id})
        cls.env["sale.order"].sudo().create({"partner_id": cls.ajeno.id,
                                             "user_id": cls.interno.id})
        cls.producto = cls.env["product.template"].create({
            "name": "Yerba Pruebapv20 x 1 kg", "type": "consu", "sale_ok": True,
            "is_published": True, "list_price": 5000.0, "taxes_id": [Command.clear()]})

    def _search(self, login, q="PV20"):
        self.authenticate(login, login)
        return {r["nombre"] for r in self.url_open("/pedido/clientes?q=%s" % q).json()}

    def _post(self, cliente):
        return post_pedido(self, {
            "renglones": json.dumps([{"id": self.producto.id, "cantidad": 20}]),
            "nombre": "Comprador PV20", "telefono": "2995550130",
            "domicilio": "Calle Falsa 123", "entrega": "domicilio",
            "cliente_id": str(cliente.id) if cliente else ""})

    def test_search_only_his_customers(self):
        self.assertEqual(self._search("externo_pv20"), {self.suyo.name})
        self.assertEqual(self._search("externo_pv20", q=""), {self.suyo.name})
        self.assertTrue({self.suyo.name, self.ajeno.name} <= self._search("interno_pv20"))

    def test_order_only_for_his_customers(self):
        self.authenticate("externo_pv20", "externo_pv20")
        Order = self.env["sale.order"].sudo()
        before = Order.search_count([])
        res = self._post(self.ajeno)
        self.assertEqual(res.status_code, 403)
        self.assertIn(MENSAJE_CLIENTE_AJENO, res.text)
        self.assertEqual(Order.search_count([]), before)
        self.assertEqual(self._post(self.suyo).status_code, 200)
        self.assertEqual(Order.search([], order="id desc", limit=1).partner_id, self.suyo)
        # no customer: the generic one, as for any staff order
        self.assertEqual(self._post(None).status_code, 200)
        last = Order.search([], order="id desc", limit=1)
        self.assertTrue(last.partner_id.distri_generic_partner)
        self.assertEqual(last.create_uid, self.externo)
        # an internal seller is unchanged
        self.authenticate("interno_pv20", "interno_pv20")
        self.assertEqual(self._post(self.ajeno).status_code, 200)


@tagged("post_install", "-at_install", "distri_v129_20")
class TestVendedorBloqueadoInicio(HttpCase):
    """19.0.1.129.20 (owner): a seller locked to the accounts app
    (distri_pedido.vendedor_solo_cuentas «1»), internal or external, lands
    on Inicio with «Inicio» on every staff screen and only the tiles his
    groups allow; the backend stays locked (it sends him to Inicio)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.vendedor_solo_cuentas", "1")
        company = cls.env.company
        cls.sellers = cls.env["res.users"]
        for login, tipo in (("interno_iv20", "interno"), ("externo_iv20", "externo")):
            user = new_test_user(cls.env, login=login, name=login.title(),
                                 groups="base.group_user,distri_pedido.grupo_vendedor",
                                 company_id=company.id, company_ids=[Command.set(company.ids)])
            user.distri_tipo_vendedor = tipo
            cls.sellers |= user

    def test_locked_sellers_land_on_inicio(self):
        for seller in self.sellers:
            self.assertTrue(seller._distri_seller_only_accounts(), seller.login)
            self.assertEqual(seller._distri_landing(), "", seller.login)
            self.assertEqual(seller._distri_ajustes()["inicio"], "/tablero", seller.login)
            self.assertTrue(seller._distri_ajustes()["mis_ventas"], seller.login)
            self.authenticate(seller.login, seller.login)
            for url in ("/odoo", "/odoo/action-base.action_res_users"):
                res = self.url_open(url, allow_redirects=False)
                self.assertIn(res.status_code, (301, 302, 303), url)
                self.assertTrue(res.headers["Location"].endswith("/tablero"), url)
            page = self.url_open("/tablero", allow_redirects=False)
            self.assertEqual(page.status_code, 200, seller.login)
            for href in ('href="/cuentas"', 'href="/mis-ventas"', 'href="/pedido"',
                         'href="/cobro"', 'href="/comprobantes"'):
                self.assertIn(href, page.text, (seller.login, href))
            for href in ('href="/odoo', 'href="/armado"', 'href="/reparto"', 'href="/revisar"',
                         'href="/caja"', 'href="/rutas"', 'href="/deposito"'):
                self.assertNotIn(href, page.text, (seller.login, href))
            self.assertNotIn("Todas las aplicaciones", page.text)
            # «Inicio» on the accounts app (dn_ajustes)
            cuentas = self.url_open("/cuentas")
            self.assertEqual(cuentas.status_code, 200)
            self.assertIn('href="/tablero"', cuentas.text)
            self.assertEqual(self.url_open("/mis-ventas").status_code, 200)


@tagged("post_install", "-at_install", "distri_v129_20")
class TestVentasTodas(TransactionCase):
    """19.0.1.129.20 (owner): the backend sales lists open on every order
    (data/ventas_todas.xml); «Pedidos» keeps its state filter."""

    def test_quotation_lists_without_my_filter(self):
        for xmlid in ("sale.action_quotations_with_onboarding", "sale.action_quotations"):
            action = self.env.ref(xmlid)
            self.assertNotIn("search_default_my_quotation", action.context or "", xmlid)
        self.assertIn("search_default_sales", self.env.ref("sale.action_orders").context)
