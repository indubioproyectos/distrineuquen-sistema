"""19.0.1.129.59: «Nueva venta» (phone and PC layouts), administración
changes a line's unit price and creates a product on the spot
(controllers/nueva_venta.py, models/nueva_venta.py).

- the typed price is the order line's price_unit on every exit, set before
  anything is confirmed, packed or invoiced, with a chatter note;
- a «Producto nuevo» is created with sudo behind the role (gerencia without
  product manager rights too), unpublished, and sold;
- a seller cannot post a price nor create a product (403);
- with no price typed, nothing changes;
- a typed price survives a later price list recomputation.
Synthetic data only.
"""
import os
import re
from datetime import timedelta

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from odoo.addons.distri_pedido.models.nueva_venta import (
    precio_valido, puede_cambiar_precios)
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.common_pedido import abrir_pedidos
from odoo.addons.distri_pedido.tests.test_v129_53_planilla import FETCH, PlanillaCase

BODY_RE = re.compile(r"<body[^>]*>")


@tagged("post_install", "-at_install", "distri_v129_59")
class TestV12959NuevaVentaPrecio(PlanillaCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # a copy of production may show the AgilPedido catalog or be paused
        abrir_pedidos(cls.env)
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        # Gonzalo: gerencia, without product manager rights
        cls.gerente = new_test_user(cls.env, login="gerente_v159", name="Gerente V159",
                                    groups="base.group_user,distri_pedido.grupo_gerencia",
                                    **company)
        cls.sistemas = new_test_user(cls.env, login="sistemas_v159", name="Sistemas V159",
                                     groups="base.group_user,base.group_system", **company)
        Categ = cls.env["product.category"]
        cls.varios = Categ.search([("name", "=ilike", "Varios")], order="id", limit=1) or \
            Categ.create({"name": "Varios"})

    # ------------------------------------------------------------ helpers

    def _con_precio(self, *precios):
        """The cart of PlanillaCase._crear with typed prices (None = none)."""
        renglones = [{"id": self.p1.id, "cantidad": 2}, {"id": self.p2.id, "cantidad": 1}]
        for renglon, precio in zip(renglones, precios):
            if precio is not None:
                renglon["precio"] = precio
        return renglones

    def _linea(self, order, plantilla):
        return order.order_line.filtered(lambda l: l.product_id.product_tmpl_id == plantilla)

    def _notas(self, record):
        return " ".join(record.message_ids.mapped("body"))

    def _producto(self, nombre="Producto Raro V159", precio="1234,50"):
        return self._post("/nueva-venta/producto", {"nombre": nombre, "precio": precio})

    # ------------------------------------------------------------ the gate

    def test_who_may_change_prices(self):
        self._flags()
        self.assertTrue(puede_cambiar_precios(self.admin))
        self.assertTrue(puede_cambiar_precios(self.gerente))
        self.assertTrue(puede_cambiar_precios(self.sistemas), "base.group_system explicitly")
        self.assertFalse(puede_cambiar_precios(self.vendedor))
        self.assertFalse(puede_cambiar_precios(self.armador))
        self.assertFalse(self.gerente.has_group("product.group_product_manager"))
        for path in ("/nueva-venta", "/nueva-venta/planilla"):
            self._login(self.admin)
            html = self.url_open(path).text
            self.assertIn('data-precios="1"', BODY_RE.search(html).group(0), path)
            self.assertIn("Producto nuevo", html, path)
            self._login(self.vendedor)
            html = self.url_open(path).text
            self.assertIn('data-precios="0"', BODY_RE.search(html).group(0), path)
            self.assertNotIn("Producto nuevo", html, path)
        self._login(self.vendedor)
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
        self.assertFalse(data["precios"])

    def test_price_parsing(self):
        self.assertEqual(precio_valido("1.234,50"), 1234.5)
        self.assertEqual(precio_valido("1234.5"), 1234.5)
        self.assertEqual(precio_valido(" $ 900 "), 900.0)
        self.assertEqual(precio_valido(3000), 3000.0)
        for malo in ("0", "-5", "abc", "", None, True, "nan", "inf", float("nan"), 1e12):
            self.assertIsNone(precio_valido(malo), malo)

    # ------------------------------------------------------------ sellers

    def test_seller_cannot_post_prices_nor_create_products(self):
        self._flags()
        self._login(self.vendedor)
        before = self.env["sale.order"].search_count([])
        for salida in ("armar", "presupuesto"):
            res = self._crear(salida, renglones=self._con_precio(1.0))
            self.assertEqual(res.status_code, 403, salida)
            self.assertIn("Solo administración", res.json()["error"])
        self.assertEqual(self.env["sale.order"].search_count([]), before)
        productos = self.env["product.template"].search_count([])
        res = self._producto()
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.env["product.template"].search_count([]), productos)
        # a product outside the catalog is still refused to him
        oculto = self.env["product.template"].create({
            "name": "Oculto V159", "type": "consu", "sale_ok": True, "list_price": 10.0,
            "is_published": False})
        res = self._crear("presupuesto", renglones=[{"id": oculto.id, "cantidad": 1}])
        self.assertEqual(res.status_code, 400, res.text[:300])
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    # ------------------------------------------------------------ no price typed

    def test_without_prices_nothing_changes(self):
        self._flags()
        self._login(self.admin)
        for salida in ("presupuesto", "mostrador"):
            order = self._order_of(self._crear(salida))
            self.assertEqual(self._linea(order, self.p1).price_unit, 4000.0, salida)
            self.assertEqual(self._linea(order, self.p2).price_unit, 2500.0, salida)
            self.assertFalse(any(order.order_line.mapped("distri_precio_manual")), salida)
            self.assertNotIn("Precios cambiados", self._notas(order), salida)
        # the list price typed again is no change either (the screen sends none)
        order = self._order_of(self._crear("presupuesto", renglones=self._con_precio(4000)))
        self.assertFalse(any(order.order_line.mapped("distri_precio_manual")))
        self.assertNotIn("Precios cambiados", self._notas(order))
        # a product outside the catalog without a price: refused as before
        oculto = self.env["product.template"].create({
            "name": "Oculto Admin V159", "type": "consu", "sale_ok": True, "list_price": 10.0,
            "is_published": False})
        res = self._crear("mostrador", renglones=[{"id": oculto.id, "cantidad": 1}])
        self.assertEqual(res.status_code, 400)
        self.assertIn("ya no están en el catálogo", res.json()["error"])

    def test_invalid_prices_are_refused(self):
        self._flags()
        self._login(self.admin)
        before = self.env["sale.order"].search_count([])
        for malo in (0, -5, "abc"):
            res = self._crear("mostrador", renglones=self._con_precio(malo))
            self.assertEqual(res.status_code, 400, malo)
            self.assertIn("precios", res.json()["error"])
        self.assertEqual(self.env["sale.order"].search_count([]), before)

    # ------------------------------------------------------------ the four exits

    def test_mostrador_invoices_the_typed_price(self):
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("mostrador", renglones=self._con_precio("3.100,50")))
        linea = self._linea(order, self.p1)
        self.assertEqual(linea.price_unit, 3100.5)
        self.assertTrue(linea.distri_precio_manual)
        self.assertEqual(self._linea(order, self.p2).price_unit, 2500.0, "the list's")
        self.assertFalse(self._linea(order, self.p2).distri_precio_manual)
        self.assertEqual(order.state, "sale")
        invoice = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
        self.assertEqual(len(invoice), 1)
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.invoice_line_ids.filtered(
            lambda l: l.product_id == self.p1.product_variant_id).price_unit, 3100.5)
        self.assertAlmostEqual(invoice.amount_total, 2 * 3100.5 + 2500.0)
        nota = self._notas(order)
        self.assertIn("Precios cambiados por Admin V153", nota)
        self.assertIn("Gaseosa Planilla V153: $ 4.000,00 → $ 3.100,50", nota)
        self.assertNotIn("Galletitas", nota.split("Precios cambiados")[1])

    def test_armar_presupuesto_and_entregado_keep_the_typed_price(self):
        self._flags()
        self._login(self.admin)
        armar = self._order_of(self._crear("armar", renglones=self._con_precio(None, 1999)))
        self.assertEqual(armar.state, "sale")
        self.assertEqual(self._linea(armar, self.p2).price_unit, 1999.0)
        self.assertEqual(self._linea(armar, self.p1).price_unit, 4000.0)
        self.assertIn("$ 2.500,00 → $ 1.999,00", self._notas(armar))
        self.assertTrue(armar._distri_para_armar(), "it goes to the /armado queue")
        # the packing invoices it at that price
        self.assertEqual(armar.distri_tomar() or armar.distri_terminar(1), "")
        factura = armar.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
        self.assertAlmostEqual(factura.amount_total, 2 * 4000.0 + 1999.0)

        presupuesto = self._order_of(self._crear("presupuesto", renglones=self._con_precio(5000)))
        self.assertEqual(presupuesto.state, "draft")
        self.assertEqual(self._linea(presupuesto, self.p1).price_unit, 5000.0)
        self.assertAlmostEqual(presupuesto.amount_untaxed, 2 * 5000.0 + 2500.0)

        dia = today_ar() - timedelta(days=2)
        entregado = self._order_of(self._crear(
            "entregado", renglones=self._con_precio(3500, 2000), fecha=str(dia),
            motivo="Ya entregado V159"))
        self.assertTrue(entregado.distri_entregado_fuera)
        self.assertEqual(self._linea(entregado, self.p1).price_unit, 3500.0)
        self.assertEqual(self._linea(entregado, self.p2).price_unit, 2000.0)
        factura = entregado.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
        self.assertAlmostEqual(factura.amount_total, 2 * 3500.0 + 2000.0)

    def test_double_tap_with_prices_is_one_sale_one_note(self):
        self._flags()
        self._login(self.admin)
        envio = os.urandom(16).hex()
        for salida in ("armar", "mostrador"):
            first = self._order_of(self._crear(salida, renglones=self._con_precio(3000),
                                               envio=envio))
            second = self._order_of(self._crear(salida, renglones=self._con_precio(3000),
                                                envio=envio))
            self.assertEqual(first, second, salida)
            self.assertEqual(self._notas(first).count("Precios cambiados"), 1, salida)

    def test_price_list_recomputation_keeps_the_typed_price(self):
        """Odoo 19 keeps a price written after the line exists as manual
        (technical_price_unit stays the list's); «Actualizar precios» and a
        list change (_recompute_prices, forced) skip it too."""
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("presupuesto", renglones=self._con_precio(3333)))
        linea = self._linea(order, self.p1)
        self.assertEqual(linea.technical_price_unit, 4000.0)
        linea.product_uom_qty = 5
        self.assertEqual(linea.price_unit, 3333.0, "a quantity change keeps it")
        self.p2.list_price = 2700.0
        self.p1.list_price = 4100.0
        order._recompute_prices()
        self.assertEqual(linea.price_unit, 3333.0, "«Actualizar precios» keeps it")
        self.assertEqual(self._linea(order, self.p2).price_unit, 2700.0, "the rest is repriced")
        order.action_confirm()
        self.assertEqual(linea.price_unit, 3333.0)

    # ------------------------------------------------------------ «Producto nuevo»

    def test_admin_creates_a_product_and_sells_it(self):
        self._flags()
        self._login(self.admin)
        res = self._producto()
        self.assertEqual(res.status_code, 200, res.text[:300])
        fila = res.json()["producto"]
        nuevo = self.env["product.template"].browse(fila["id"])
        self.assertEqual(nuevo.name, "Producto Raro V159")
        self.assertTrue(nuevo.sale_ok)
        self.assertFalse(nuevo.is_published, "not in /pedido's customer catalog")
        self.assertEqual(nuevo.type, "consu")
        self.assertTrue(nuevo.is_storable)
        self.assertEqual(nuevo.list_price, 1234.5)
        self.assertEqual(nuevo.categ_id, self.varios)
        self.assertTrue(nuevo.distri_nueva_venta_creado)
        self.assertEqual(nuevo.create_uid, self.admin)
        self.assertIn("Producto creado por Admin V153", self._notas(nuevo))
        self.assertEqual(fila["precio"], 1234.5)
        # not offered to customers nor in the catalog of the screen
        catalogo = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
        self.assertNotIn(nuevo.id, [p["id"] for p in catalogo["productos"]])
        # sold at the counter (invoiced) and to pack
        for salida in ("mostrador", "armar"):
            order = self._order_of(self._crear(salida, renglones=[
                {"id": nuevo.id, "cantidad": 3, "precio": 1234.5},
                {"id": self.p2.id, "cantidad": 1}]))
            linea = self._linea(order, nuevo)
            self.assertEqual(linea.price_unit, 1234.5, salida)
            self.assertEqual(linea.product_uom_qty, 3, salida)
            self.assertTrue(linea.distri_precio_manual, salida)
            self.assertEqual(order.state, "sale", salida)
            self.assertIn("Productos creados en «Nueva venta»", self._notas(order), salida)
            self.assertIn("Producto Raro V159 a $ 1.234,50", self._notas(order), salida)
            if salida == "mostrador":
                factura = order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")
                self.assertAlmostEqual(factura.amount_total, 3 * 1234.5 + 2500.0)
        # a price is required, the name too
        for nombre, precio in (("Sin precio V159", ""), ("Cero V159", "0"), ("", "100")):
            res = self._producto(nombre, precio)
            self.assertEqual(res.status_code, 400, (nombre, precio))

    def test_gerencia_without_product_manager_creates_it(self):
        self._flags()
        with self.assertRaises(AccessError):
            self.env["product.template"].with_user(self.gerente).create({"name": "Directo V159"})
        self._login(self.gerente)
        res = self._producto("Producto Gerencia V159", "800")
        self.assertEqual(res.status_code, 200, res.text[:300])
        nuevo = self.env["product.template"].browse(res.json()["producto"]["id"])
        self.assertEqual(nuevo.create_uid, self.gerente)
        self.assertFalse(nuevo.is_published)
        order = self._order_of(self._crear("presupuesto", renglones=[
            {"id": nuevo.id, "cantidad": 2, "precio": 750}]))
        self.assertEqual(self._linea(order, nuevo).price_unit, 750.0)
        self.assertEqual(order.state, "draft")

    def test_corregir_brings_the_typed_prices_back(self):
        """«Corregir la venta» (PC layout) returns each changed price, and
        the row of a product outside the catalog, so the sale loaded again
        keeps them."""
        self._flags()
        self._login(self.admin)
        nuevo = self.env["product.template"].browse(self._producto().json()["producto"]["id"])
        order = self._order_of(self._crear("mostrador", renglones=[
            {"id": self.p1.id, "cantidad": 2, "precio": 3000},
            {"id": nuevo.id, "cantidad": 1, "precio": 1234.5},
            {"id": self.p2.id, "cantidad": 1}]))
        res = self._post("/nueva-venta/planilla/corregir/%s" % order.id, {})
        self.assertEqual(res.status_code, 200, res.text[:300])
        renglones = {r["id"]: r for r in res.json()["renglones"]}
        self.assertEqual(renglones[self.p1.id]["precio"], 3000.0)
        self.assertEqual(renglones[nuevo.id]["precio"], 1234.5)
        self.assertEqual(renglones[nuevo.id]["fila"]["nombre"], nuevo.name)
        self.assertNotIn("precio", renglones[self.p2.id])
        again = self._order_of(self._crear("mostrador", renglones=list(renglones.values())))
        self.assertEqual(self._linea(again, self.p1).price_unit, 3000.0)
        self.assertEqual(self._linea(again, nuevo).price_unit, 1234.5)
        self.assertEqual(self._linea(again, self.p2).price_unit, 2500.0)

    def test_product_route_needs_the_flag_and_a_post(self):
        self._login(self.admin)
        self._flags(nueva_venta=False)
        res = self.url_open("/nueva-venta/producto", data={"nombre": "x", "precio": "1"},
                            headers=FETCH)
        self.assertIn(res.status_code, (400, 404))
        self._flags()
        self.assertEqual(self.url_open("/nueva-venta/producto").status_code, 405)
