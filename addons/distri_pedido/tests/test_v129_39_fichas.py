"""19.0.1.129.39, finding 1 and 3 of the review of 129.33-129.38: the
standard «Personal Orders» / «Personal Invoices» record rules let any seller
read (and write) an order or an invoice without salesperson, as AgilPedido's
WhatsApp orders are. The kit's order and invoice screens, «Aprobar» and
«Anular venta» now also apply the external seller's rule
(res.users._distri_may_reach_record): an external seller gets 404 on an
order or invoice of a customer that is not his, with or without
salesperson, on every route and POST. His own customers' orders still open,
and nobody else changes. Nothing moves a balance."""
from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA

from .common_pedido import ensure_company_chart
from .test_v129_34_fichas import _ledger
from .test_v129_35_venta import CSRF_RE


@tagged("post_install", "-at_install", "distri_v129_39")
class TestV12939Fichas(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_company_chart(cls.env)
        company = cls.env.company

        def user(login, groups, **vals):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups="base.group_user," + groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if vals:
                new.write(vals)
            return new

        cls.admin = user("admin_f39", "distri_pedido.grupo_administracion")
        cls.interno = user("interno_f39", "distri_pedido.grupo_vendedor")
        cls.otro = user("otro_f39", "distri_pedido.grupo_vendedor")
        cls.externo = user("externo_f39", "distri_pedido.grupo_vendedor",
                           distri_tipo_vendedor="externo")
        Partner = cls.env["res.partner"]
        cls.propio = Partner.create({"name": "PROPIO EXTERNO F39", "customer_rank": 1})
        cls.ajeno = Partner.create({"name": "AJENO F39", "customer_rank": 1})
        cls.product = cls.env["product.product"].create({
            "name": "Yerba F39", "type": "consu", "invoice_policy": "order",
            "list_price": 100.0})
        # his customer: an order with him as salesperson
        cls.o_propio = cls._order(cls.propio, cls.externo, confirm=True)
        cls.q_propio = cls._order(cls.propio, cls.externo)
        # another seller's customer, and two orders of it WITHOUT salesperson
        # (AgilPedido's WhatsApp orders): a quotation and a confirmed one
        cls._order(cls.ajeno, cls.otro, confirm=True)
        cls.q_agil = cls._order(cls.ajeno, None)
        cls.o_agil = cls._order(cls.ajeno, None, confirm=True, invoice=True)
        cls.inv_agil = cls.o_agil.invoice_ids
        cls.inv_agil.sudo().invoice_user_id = False
        cls.inv_propio = cls._order(cls.propio, cls.externo, confirm=True,
                                    invoice=True).invoice_ids
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_UI_V2, "1")
        params.set_param(PARAM_NUEVA_VENTA, "1")

    @classmethod
    def _order(cls, partner, salesperson, confirm=False, invoice=False):
        order = cls.env["sale.order"].create({
            "partner_id": partner.id,
            "order_line": [Command.create({"product_id": cls.product.id,
                                           "product_uom_qty": 2, "price_unit": 100.0,
                                           "tax_ids": [Command.clear()]})]})
        order.user_id = salesperson.id if salesperson else False
        if not salesperson:
            order.sudo().distri_origen = "agilpedido_wa"
        if confirm:
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

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _get(self, path):
        return self.url_open(path, allow_redirects=False)

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, path)
        return CSRF_RE.search(page.text).group(1)

    def _post(self, path, csrf, **data):
        data["csrf_token"] = csrf
        return self.url_open(path, data=data, allow_redirects=False)

    # ------------------------------------------------------------ the backend lets him

    def test_the_standard_rule_lets_him_read_orders_without_salesperson(self):
        """The hole the review found: the backend alone lets him read and
        write the order and invoice without salesperson."""
        self.assertTrue(self.q_agil.with_user(self.externo).has_access("read"))
        self.assertTrue(self.q_agil.with_user(self.externo).has_access("write"))
        self.assertTrue(self.inv_agil.with_user(self.externo).has_access("read"))
        self.assertFalse(self.externo._distri_may_reach_record(self.q_agil))
        self.assertFalse(self.externo._distri_may_reach_record(self.inv_agil))
        self.assertTrue(self.externo._distri_may_reach_record(self.q_propio))
        self.assertTrue(self.externo._distri_may_reach_record(self.inv_propio))
        for user in (self.admin, self.interno, self.otro):
            self.assertTrue(user._distri_may_reach_record(self.q_agil), user.login)

    # ------------------------------------------------------------ every route and POST

    def test_external_seller_refused_on_every_route(self):
        self._login(self.externo)
        # his own: open, with «Aprobar» on his quotation
        self.assertEqual(self._get("/pedidos/%s" % self.o_propio.id).status_code, 200)
        html = self._get("/pedidos/%s" % self.q_propio.id).text
        self.assertIn('action="/pedidos/%s/aprobar"' % self.q_propio.id, html)
        self.assertEqual(self._get("/facturas/%s" % self.inv_propio.id).status_code, 200)
        csrf = self._csrf("/pedidos/%s" % self.o_propio.id)
        # another seller's customer, without salesperson: 404 everywhere
        for order in (self.q_agil, self.o_agil):
            res = self._get("/pedidos/%s" % order.id)
            self.assertEqual(res.status_code, 404, order.name)
            self.assertNotIn(self.ajeno.name, res.text)
            self.assertEqual(self._post("/pedidos/%s/aprobar" % order.id, csrf).status_code,
                             404, order.name)
            # «Anular venta» is administración's: refused before the order
            self.assertIn(self._get("/pedidos/%s/anular" % order.id).status_code, (403, 404))
            self.assertIn(self._post("/pedidos/%s/anular" % order.id, csrf,
                                     motivo="prueba", huella="x").status_code, (403, 404))
        res = self._get("/facturas/%s" % self.inv_agil.id)
        self.assertEqual(res.status_code, 404)
        self.assertNotIn(self.ajeno.name, res.text)
        self.assertEqual(self.q_agil.state, "draft", "not approved")
        self.assertEqual(self.o_agil.state, "sale", "not cancelled")
        # the customer screen was already refused (129.34)
        self.assertEqual(self._get("/clientes/%s" % self.ajeno.id).status_code, 404)

    def test_model_aprobar_refuses_him_too(self):
        with self.assertRaises(AccessError):
            self.q_agil.with_user(self.externo).distri_kit_aprobar(sin_whatsapp=True)
        self.assertEqual(self.q_agil.state, "draft")

    def test_others_unchanged(self):
        """Administración and internal sellers keep what the backend gives."""
        self._login(self.admin)
        for path in ("/pedidos/%s" % self.q_agil.id, "/pedidos/%s" % self.o_agil.id,
                     "/facturas/%s" % self.inv_agil.id, "/pedidos/%s/anular" % self.o_agil.id):
            self.assertEqual(self._get(path).status_code, 200, path)
        self.assertIn('action="/pedidos/%s/aprobar"' % self.q_agil.id,
                      self._get("/pedidos/%s" % self.q_agil.id).text)
        self._login(self.interno)
        self.assertEqual(self._get("/pedidos/%s" % self.q_agil.id).status_code, 200,
                         "an internal seller: the backend's rule, as in 129.34")

    def test_flags_off_still_404(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "0")
        self._login(self.admin)
        self.assertEqual(self._get("/pedidos/%s" % self.q_agil.id).status_code, 404)
