"""19.0.1.129.30: «Nueva venta», stage 1 (controllers/nueva_venta.py,
models/nueva_venta.py).

Each exit is compared with the path that already exists for it: «A armar»
and «Presupuesto» with the order «Tomar un pedido» (/pedido/enviar) makes,
«Mostrador» with a backend quotation sold by /deposito/mostrador, «Ya
entregado» with the counter sale's packing plus 19.0.1.129.25's flag. With
distri_pedido.nueva_venta off (the default) nothing is reachable. Synthetic
data only.
"""
import json
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers import tablero
from odoo.addons.distri_pedido.controllers.deposito import deliverable_domain
from odoo.addons.distri_pedido.controllers.kit import screen_parents
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.nueva_venta import (
    MOTIVO_ENTREGADO, PARAM_NUEVA_VENTA, nueva_venta_on, salidas_de)
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio" value="([^"]+)"')
# what /pedido's order and «Nueva venta»'s must share (the rest are ids,
# names, tokens and dates of each record)
ORDER_FIELDS = ("state", "partner_id", "pricelist_id", "user_id", "distri_wa_state",
                "distri_entrega", "distri_nombre", "distri_telefono", "distri_domicilio",
                "distri_original", "distri_comentario", "distri_nota_deposito",
                "distri_vendedor_web_id", "distri_armado", "distri_retenido", "amount_total",
                "amount_untaxed", "company_id", "note")


@tagged("post_install", "-at_install", "distri_v129_30")
class TestV12930NuevaVenta(PedidoAbiertoMixin, RepartoMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        # price lists on, as in production (test_pedido_listas.py)
        cls.env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        # one company, as production: the website (/pedido) sells for it
        cls.env["website"].search([]).company_id = cls.env.company
        Pricelist = cls.env["product.pricelist"]
        if not Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1):
            Pricelist.create({"name": LISTA_GENERAL, "company_id": False,
                              "currency_id": cls.env.company.currency_id.id})
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups, **extra):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company, **extra)

        cls.admin = user("admin_nv130", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_nv130", "distri_pedido.grupo_gerencia")
        cls.vendedor = user("vendedor_nv130", "distri_pedido.grupo_vendedor")
        cls.externo = user("externo_nv130", "distri_pedido.grupo_vendedor")
        cls.externo.sudo().distri_tipo_vendedor = "externo"
        cls.armador = user("armador_nv130", "distri_pedido.grupo_armador")
        Tmpl = cls.env["product.template"]
        cls.p1 = Tmpl.create({"name": "Gaseosa NV130", "type": "consu", "list_price": 40000.0,
                              "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.p2 = Tmpl.create({"name": "Galletitas NV130", "type": "consu", "list_price": 15000.0,
                              "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.lista = cls.env["product.pricelist"].create({
            "name": "Lista cliente NV130", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id,
            "item_ids": [Command.create({"applied_on": "1_product", "product_tmpl_id": cls.p1.id,
                                         "compute_price": "fixed", "fixed_price": 36000.0})]})
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacen Nueva Venta NV130", "company_type": "company",
            "distri_whatsapp": "2995001130", "street": "Mitre 130", "city": "Neuquén",
            "property_product_pricelist": cls.lista.id})
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _tokens(self):
        page = self.url_open("/nueva-venta")
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1), ENVIO_RE.search(page.text).group(1)

    def _cart(self, q1=2, q2=3):
        return [{"id": self.p1.id, "cantidad": q1}, {"id": self.p2.id, "cantidad": q2}]

    def _post(self, salida, cart=None, envio=None, **extra):
        csrf, nuevo = self._tokens()
        data = {"csrf_token": csrf, "envio": envio or nuevo, "cliente_id": str(self.cliente.id),
                "renglones": json.dumps(cart or self._cart()), "salida": salida,
                "entrega": "domicilio", "telefono": "2995001130",
                "domicilio": "Mitre 130, Neuquén"}
        data.update(extra)
        res = self.url_open("/nueva-venta/crear", data=data, headers=FETCH, allow_redirects=False)
        self.env.invalidate_all()
        return res

    def _order_of(self, res):
        self.assertEqual(res.status_code, 200, res.text[:500])
        url = res.json()["url"]
        order_id = int(re.search(r"/(\d+)", url).group(1))
        return self.env["sale.order"].browse(order_id), url

    def _old_pedido(self, cart=None):
        """«Tomar un pedido» as the browser posts it for this customer."""
        before = self.env["sale.order"].search([]).ids
        res = post_pedido(self, {
            "renglones": json.dumps(cart or self._cart()), "cliente_id": str(self.cliente.id),
            "nombre": self.cliente.name, "telefono": "2995001130",
            "domicilio": "Mitre 130, Neuquén", "entrega": "domicilio"})
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.env.invalidate_all()
        return self.env["sale.order"].search([("id", "not in", before)])

    def _lines(self, order):
        return sorted((l.product_id.id, l.product_uom_qty, l.product_uom_id.id, l.price_unit,
                       l.price_subtotal, tuple(l.tax_ids.ids))
                      for l in order.order_line if not l.display_type)

    def _same_order(self, new, old):
        for name in ORDER_FIELDS:
            self.assertEqual(new[name], old[name], name)
        self.assertEqual(self._lines(new), self._lines(old))
        self.assertEqual(bool(new.distri_receipt_hash), bool(old.distri_receipt_hash),
                         "receipt code rules")
        self.assertTrue(new.distri_receipt_hash)
        self.assertEqual(bool(new.access_token), bool(old.access_token))

    def _invoice(self, order):
        return order.invoice_ids.filtered(lambda f: f.move_type == "out_invoice")

    def _ledger(self):
        self.env.flush_all()
        self.cliente.invalidate_recordset(["credit"])
        return self.cliente.commercial_partner_id.credit

    # ------------------------------------------------------------ flag off

    def test_flag_off_by_default_nothing_reachable(self):
        self.assertFalse(nueva_venta_on(self.env))
        for user in (self.admin, self.vendedor):
            self._login(user)
            self.assertEqual(self.url_open("/nueva-venta").status_code, 404)
            self.assertEqual(self.url_open(
                "/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).status_code, 404)
            self.assertNotIn("nuevaventa", [t["key"] for t in tablero.staff_tiles(
                self.env(user=user))])
            self.assertNotIn("/nueva-venta", self.url_open("/tablero").text)
        self.assertNotIn("nuevaventa", [t[0] for t in tablero.TILES])
        before = self.env["sale.order"].search_count([])
        res = self.url_open("/nueva-venta/crear", data={"salida": "armar"}, headers=FETCH)
        self.assertIn(res.status_code, (400, 404))
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    # ------------------------------------------------------------ access

    def test_tile_page_and_exits_per_group(self):
        self._flag()
        self.assertEqual(screen_parents("/nueva-venta"), ("inicio",))
        self.assertEqual(screen_parents("/nueva-venta/hecho/7"), ("/nueva-venta",))
        self.assertEqual(salidas_de(self.vendedor), ["armar", "presupuesto"])
        self.assertEqual(salidas_de(self.admin), ["armar", "mostrador", "entregado", "presupuesto"])
        self.assertEqual(salidas_de(self.gerente), salidas_de(self.admin))
        self.assertEqual(salidas_de(self.armador), [])
        for user in (self.admin, self.gerente, self.vendedor):
            tiles = tablero.staff_tiles(self.env(user=user))
            self.assertEqual(tiles[0]["key"], "nuevaventa", user.login)
            # 19.0.1.129.35: «Nueva venta» takes the place of «Tomar un pedido»
            self.assertNotIn("pedido", [t["key"] for t in tiles], "«Nueva venta» replaces it")
        self.assertNotIn("nuevaventa", [t["key"] for t in tablero.staff_tiles(
            self.env(user=self.armador))])
        self._login(self.vendedor)
        html = self.url_open("/nueva-venta").text
        body = re.search(r"<body[^>]*>", html).group(0)
        self.assertIn('data-page="nueva-venta"', body)
        self.assertRegex(body, r'data-parent="[^"]+"')
        self.assertIn("/dn/nueva_venta.js", html)
        self.assertIn('value="armar"', html)
        self.assertIn('value="presupuesto"', html)
        self.assertNotIn('value="mostrador"', html)
        self.assertNotIn('value="entregado"', html)
        self.assertNotIn("Pegar mensaje de AgilPedido", html)
        self._login(self.admin)
        self.assertIn('href="/nueva-venta"', self.url_open("/tablero").text, "the Inicio tile")
        html = self.url_open("/nueva-venta").text
        for salida in ("armar", "mostrador", "entregado", "presupuesto"):
            self.assertIn('value="%s"' % salida, html)
        self.assertIn("Pegar mensaje de AgilPedido", html)
        self.assertIn('value="%s"' % MOTIVO_ENTREGADO, html)
        self._login(self.armador)
        self.assertEqual(self.url_open("/nueva-venta").status_code, 403)

    def test_sellers_cannot_use_the_admin_exits(self):
        self._flag()
        self._login(self.vendedor)
        before = self.env["sale.order"].search_count([])
        for salida in ("mostrador", "entregado"):
            res = self._post(salida, fecha=str(today_ar()), motivo="x")
            self.assertEqual(res.status_code, 403, salida)
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_external_seller_only_his_customers(self):
        self._flag()
        self._login(self.externo)
        res = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id)
        self.assertEqual(res.status_code, 403)
        before = self.env["sale.order"].search_count([])
        res = self._post("presupuesto")
        self.assertEqual(res.status_code, 403)
        self.assertIn("no es tuyo", res.json()["error"])
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_catalog_priced_with_the_customers_list(self):
        self._flag()
        self._login(self.vendedor)
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
        self.assertEqual(data["cliente"]["nombre"], self.cliente.name)
        precios = {p["id"]: p["precio"] for p in data["productos"]}
        self.assertEqual(precios[self.p1.id], 36000.0, "the customer's list")
        self.assertEqual(precios[self.p2.id], 15000.0)
        self.assertEqual(self.url_open("/nueva-venta/catalogo").status_code, 400)

    # ------------------------------------------------------------ exits 1 and 4

    def test_presupuesto_is_the_order_of_tomar_un_pedido(self):
        self._flag()
        self._login(self.vendedor)
        old = self._old_pedido()
        new, url = self._order_of(self._post("presupuesto"))
        self.assertEqual(url, "/nueva-venta/hecho/%s" % new.id)
        self.assertNotEqual(new, old)
        self.assertEqual(new.state, "draft")
        self._same_order(new, old)
        self.assertEqual(new.user_id, self.vendedor)
        self.assertEqual(new.pricelist_id, self.lista)
        self.assertEqual(new.distri_wa_state, "staff")
        self.assertEqual(new.amount_untaxed, 2 * 36000 + 3 * 15000)
        self.assertFalse(new.picking_ids)
        self.assertFalse(new.invoice_ids)
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Presupuesto guardado", page.text)
        self.assertIn("Enviar al cliente por WhatsApp", page.text)

    def test_armar_is_the_order_of_tomar_un_pedido_confirmed(self):
        self._flag()
        self._login(self.vendedor)
        old = self._old_pedido()
        old.with_user(self.admin).action_confirm()
        new, url = self._order_of(self._post("armar"))
        self._same_order(new, old)
        self.assertEqual(new.state, "sale")
        self.assertEqual(new.distri_armado, "pendiente")
        self.assertTrue(new._distri_para_armar(), "it goes to the /armado queue")
        self.assertEqual(len(new.picking_ids), len(old.picking_ids))
        self.assertEqual(new.picking_ids.mapped("state"), old.picking_ids.mapped("state"))
        self.assertFalse(new.invoice_ids, "invoiced when its packing ends, as always")
        self.assertIn("Pedido confirmado", self.url_open(url).text)

    def test_double_post_gives_one_order(self):
        self._flag()
        self._login(self.admin)
        csrf, envio = self._tokens()
        first, _url = self._order_of(self._post("presupuesto", envio=envio))
        second, _url = self._order_of(self._post("presupuesto", envio=envio))
        self.assertEqual(first, second)
        first, _url = self._order_of(self._post("mostrador", envio=envio))
        second, _url = self._order_of(self._post("mostrador", envio=envio))
        self.assertEqual(first, second)
        self.assertEqual(len(self._invoice(first)), 1)

    def test_below_the_minimum_like_tomar_un_pedido(self):
        self._flag()
        self._login(self.vendedor)
        before = self.env["sale.order"].search_count([])
        res = self._post("armar", cart=[{"id": self.p2.id, "cantidad": 1}])
        self.assertEqual(res.status_code, 400)
        self.assertIn("La compra mínima", res.json()["error"])
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    def test_no_minimum_for_administracion(self):
        """25/11/2026: administración, caja and gerencia sell below the
        minimum (controllers/main.py COMPRA_MINIMA_EXENTOS)."""
        self._flag()
        self._login(self.admin)
        res = self._post("presupuesto", cart=[{"id": self.p2.id, "cantidad": 1}])
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertNotIn("La compra mínima", res.text)

    # ------------------------------------------------------------ exit 2

    def test_mostrador_is_the_counter_sale(self):
        self._flag()
        self._login(self.admin)
        # the old path: a backend quotation sold at /deposito/mostrador
        old = self.env["sale.order"].with_user(self.admin).create({
            "partner_id": self.cliente.id, "order_line": [
                Command.create({"product_id": self.p1.product_variant_id.id,
                                "product_uom_qty": 2}),
                Command.create({"product_id": self.p2.product_variant_id.id,
                                "product_uom_qty": 3})]})
        csrf = CSRF_RE.search(self.url_open("/nueva-venta").text).group(1)
        res = self.url_open("/deposito/mostrador/%s" % old.id, data={"csrf_token": csrf},
                            headers=FETCH, allow_redirects=False)
        self.assertTrue(res.json()["ok"], res.text)
        self.env.invalidate_all()
        new, url = self._order_of(self._post("mostrador", cart=self._cart()))
        self.assertEqual(url, "/deposito/entrega/%s?mostrador=1" % new.id)
        for name in ("state", "partner_id", "pricelist_id", "user_id", "distri_wa_state",
                     "distri_armado", "distri_bultos", "distri_entrega", "amount_total",
                     "distri_delivery_state", "distri_entregado_fuera"):
            self.assertEqual(new[name], old[name], name)
        self.assertEqual(self._lines(new), self._lines(old))
        self.assertEqual(new.distri_wa_state, "counter")
        self.assertEqual(new.distri_armado, "armado")
        self.assertTrue(new.distri_receipt_hash)
        inv_new, inv_old = self._invoice(new), self._invoice(old)
        self.assertEqual((inv_new.state, inv_new.journal_id, inv_new.amount_total),
                         (inv_old.state, inv_old.journal_id, inv_old.amount_total))
        self.assertEqual(new.distri_factura_auto_id, inv_new)
        self.assertEqual(new.picking_ids.mapped("state"), ["done"])
        # then the depot delivery and payment, as today
        form = self.url_open(url).text
        self.assertIn("El pedido ya quedó confirmado", form)
        csrf = CSRF_RE.search(self.url_open("/deposito").text) or re.search(
            r'name="csrf-token" content="([^"]+)"', self.url_open("/deposito").text)
        res = self.url_open(url.split("?")[0], data={
            "csrf_token": csrf.group(1), "mostrador": "1", "presente": "1", "bultos": "1",
            "devolvio": "none", "codigo_op": "without_code", "transferencia": "no",
            "efectivo": "117000"}, headers=FETCH, allow_redirects=False)
        self.assertTrue(res.json()["ok"], res.text)
        self.env.invalidate_all()
        self.assertEqual(new.distri_delivery_state, "delivered")

    # ------------------------------------------------------------ exit 3

    def test_ya_entregado(self):
        self._flag()
        # an earlier invoice of the customer: its balance must not move
        earlier = self.env["sale.order"].create({
            "partner_id": self.cliente.id, "order_line": [Command.create({
                "product_id": self.p2.product_variant_id.id, "product_uom_qty": 1})]})
        earlier.action_confirm()
        self.assertEqual(earlier.distri_tomar() or earlier.distri_terminar(1), "")
        earlier_invoice = self._invoice(earlier)
        residual = earlier_invoice.amount_residual
        self._login(self.admin)
        dia = today_ar() - timedelta(days=5)
        res = self._post("entregado", fecha=str(dia), motivo="Ya entregado por Gonzalo")
        new, url = self._order_of(res)
        self.assertEqual(url, "/nueva-venta/hecho/%s" % new.id)
        self.assertEqual(new.state, "sale")
        self.assertEqual(new.distri_armado, "armado")
        self.assertEqual(new.distri_bultos, 1)
        self.assertTrue(new.distri_receipt_hash)
        self.assertTrue(new.distri_entregado_fuera)
        self.assertEqual(new.distri_entregado_fuera_por_id, self.admin)
        self.assertEqual(new.distri_entregado_fuera_fecha, dia)
        self.assertEqual(new.distri_entregado_fuera_motivo, "Ya entregado por Gonzalo")
        self.assertEqual(new.distri_delivery_state, "delivered")
        self.assertEqual(new.pricelist_id, self.lista)
        self.assertEqual(new.amount_untaxed, 2 * 36000 + 3 * 15000)
        invoice = self._invoice(new)
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, earlier_invoice.state, "invoiced like the normal flow")
        self.assertEqual(invoice.journal_id, earlier_invoice.journal_id)
        self.assertEqual(invoice.amount_total, new.amount_total)
        picking = new.picking_ids
        self.assertEqual(picking.mapped("state"), ["done"])
        local = fields.Datetime.context_timestamp(new.with_context(
            tz="America/Argentina/Buenos_Aires"), picking.date_done)
        self.assertEqual(local.date(), dia, "the remito carries the day of the delivery")
        self.assertTrue(all(m.date == picking.date_done for m in picking.move_ids))
        # never in a delivery queue, no payment, no collection
        Order = self.env["sale.order"]
        self.assertFalse(Order.search(deliverable_domain() + [("id", "=", new.id)]))
        route = self.env["distri.route"].new({"company_id": self.env.company.id})
        self.assertFalse(Order.search(route._free_orders_domain() + [("id", "=", new.id)]))
        self.assertFalse(Order.search(self.env["distri.route"]._planning_free_domain()
                                      + [("id", "=", new.id)]))
        self.assertFalse(new.distri_stop_ids)
        self.assertFalse(self.env["account.payment"].search([("partner_id", "=", self.cliente.id)]))
        self.assertEqual(earlier_invoice.amount_residual, residual, "past invoices untouched")
        self.assertIn("Venta cargada como entregada", self.url_open(url).text)

    def test_ya_entregado_refuses_a_future_day_or_no_reason(self):
        self._flag()
        self._login(self.admin)
        before = self.env["sale.order"].search_count([])
        credit = self._ledger()
        res = self._post("entregado", fecha=str(today_ar() + timedelta(days=1)), motivo="x")
        self.assertEqual(res.status_code, 400)
        self.assertIn("posterior a hoy", res.json()["error"])
        res = self._post("entregado", fecha=str(today_ar()), motivo="   ")
        self.assertEqual(res.status_code, 400)
        self.assertIn("motivo", res.json()["error"])
        res = self._post("entregado", fecha="", motivo="x")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.env["sale.order"].search_count([]), before)
        self.assertEqual(self._ledger(), credit)

    # ------------------------------------------------------------ AgilPedido

    def test_pegar_mensaje_de_agilpedido(self):
        self._flag()
        self._login(self.admin)
        csrf, _envio = self._tokens()
        mensaje = ("Hola! Mi pedido:\n- 2 x Gaseosa NV130 ($ 80.000,00)\n"
                   "- 1 x Producto que no existe zzz ($ 1.000,00)\n")
        res = self.url_open("/nueva-venta/pegar", data={"csrf_token": csrf, "mensaje": mensaje},
                            headers=FETCH)
        self.assertEqual(res.status_code, 200, res.text[:300])
        data = res.json()
        self.assertEqual(data["renglones"], [{"id": self.p1.id, "cantidad": 2}])
        self.assertEqual(len(data["sin_identificar"]), 1)
        self.assertFalse(self.env["distri.importar.pedido"].search([]), "nothing kept")
        self._login(self.vendedor)
        csrf, _envio = self._tokens()
        res = self.url_open("/nueva-venta/pegar", data={"csrf_token": csrf, "mensaje": mensaje},
                            headers=FETCH)
        self.assertEqual(res.status_code, 403)
