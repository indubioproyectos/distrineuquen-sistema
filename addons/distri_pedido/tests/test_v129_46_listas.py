"""19.0.1.129.46 (owner, 26/11/2026): the «lista 3» / «lista 4» products
(models/lista_personal.py).

Administración, Gerencia and Sistemas see two sections «Lista 3» and «Lista
4» on /pedido, «Nueva venta»'s picker and its embedded catalog, and can order
those products at their own sales price, whatever the customer's price list
says. Sellers, drivers, packers, customers (with or without the hidden list)
and visitors never see them, and a crafted post with their ids is refused.
The migration marks exactly the products named «… - lista 3/4». Nothing
touches the ledger. Synthetic data only."""
import importlib.util
import json
import os
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import (
    CATEGORIAS_A_PRESUPUESTAR, LISTA_GENERAL,
)
from odoo.addons.distri_pedido.models.lista_personal import (
    lista_por_nombre, marcar_por_nombre, ve_listas_personales,
)
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido

PRECIO_L3 = 7310.0
PRECIO_L3B = 7320.0
PRECIO_L4 = 8420.0
PRECIO_GENERAL_REGLA = 1111.0     # what the General list would give a lista 3 product
PRECIO_CLIENTE_REGLA = 2222.0     # what the customer's list would give it
CANTIDAD = 10                     # 73.100: over the minimum purchase for Sistemas
FETCH = {"X-Distri-Fetch": "1"}
# a Sistemas user (website designer) gets data-oe-* attributes in between
CSRF_RE = re.compile(r'name="csrf_token"[^>]*? value="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio"[^>]*? value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_46")
class TestV12946Listas(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        env["website"].search([]).company_id = env.company
        currency = env.company.currency_id.id
        Pricelist = env["product.pricelist"]
        cls.general = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL, "currency_id": currency})
        Categ = env["product.category"]
        categ_cig = Categ.search([("name", "=", CATEGORIAS_A_PRESUPUESTAR[0])], limit=1) or \
            Categ.create({"name": CATEGORIAS_A_PRESUPUESTAR[0]})
        base = {"type": "consu", "sale_ok": True, "taxes_id": [Command.clear()],
                "invoice_policy": "order"}
        Tmpl = env["product.template"]
        cls.comun = Tmpl.create(dict(base, name="Gaseosa Pruebal46", list_price=5000.0,
                                     is_published=True))
        # a cigarette, as the real ones («May BOLD 20 - lista 3»): it still has a price
        cls.l3 = Tmpl.create(dict(base, name="May Pruebal46 B - lista 3", list_price=PRECIO_L3,
                                  is_published=False, categ_id=categ_cig.id,
                                  distri_lista_personal="lista_3"))
        cls.l3b = Tmpl.create(dict(base, name="May Pruebal46 A - lista 3",
                                   list_price=PRECIO_L3B, is_published=False,
                                   distri_lista_personal="lista_3"))
        cls.l4 = Tmpl.create(dict(base, name="May Pruebal46 C - Lista 4", list_price=PRECIO_L4,
                                  is_published=False, distri_lista_personal="lista_4"))
        cls.personales = cls.l3 | cls.l3b | cls.l4
        # price lists that would reprice them if they were allowed to
        env["product.pricelist.item"].create({
            "pricelist_id": cls.general.id, "applied_on": "1_product",
            "product_tmpl_id": cls.l3.id, "compute_price": "fixed",
            "fixed_price": PRECIO_GENERAL_REGLA})
        cls.lista_cliente = Pricelist.create({
            "name": "Lista cliente Pruebal46", "currency_id": currency,
            "item_ids": [
                Command.create({"applied_on": "1_product", "product_tmpl_id": cls.l3.id,
                                "compute_price": "fixed", "fixed_price": PRECIO_CLIENTE_REGLA}),
                Command.create({"applied_on": "3_global", "compute_price": "percentage",
                                "percent_price": 10}),
            ]})
        cls.cartera = env["res.partner.category"].create({"name": "Cartera Pruebal46"})
        Partner = env["res.partner"]
        cls.cliente = Partner.create({
            "name": "Almacen Pruebal46", "company_type": "company",
            "distri_whatsapp": "2995004601", "street": "Roca 46", "city": "Neuquén",
            "category_id": [Command.link(cls.cartera.id)],
            "property_product_pricelist": cls.lista_cliente.id})
        company = {"company_id": env.company.id,
                   "company_ids": [Command.set(env.company.ids)]}

        def user(login, groups, **extra):
            return new_test_user(env, login=login, name=login.replace("_", " ").title(),
                                 groups=groups, **company, **extra)

        cls.admin = user("admin_l46", "base.group_user,distri_pedido.grupo_administracion")
        cls.gerencia = user("gerencia_l46", "base.group_user,distri_pedido.grupo_gerencia")
        cls.sistemas = user("sistemas_l46", "base.group_user,base.group_system")
        cls.vendedor = user("vendedor_l46", "base.group_user,distri_pedido.grupo_vendedor")
        cls.vendedor.etiqueta_cartera_id = cls.cartera
        cls.chofer = user("chofer_l46", "base.group_user,distri_pedido.grupo_reparto")
        cls.armador = user("armador_l46", "base.group_user,distri_pedido.grupo_armador")
        distribuidora = Partner.create({"name": "Distribuidora Pruebal46",
                                        "company_type": "company", "distri_lista_oculta": True})
        cls.distribuidor = user("distri_l46", "base.group_portal", partner_id=Partner.create({
            "name": "Contacto Distri Pruebal46", "parent_id": distribuidora.id}).id)
        kiosco = Partner.create({"name": "Kiosco Pruebal46", "company_type": "company"})
        cls.kiosquero = user("kiosco_l46", "base.group_portal", partner_id=Partner.create({
            "name": "Contacto Kiosco Pruebal46", "parent_id": kiosco.id}).id)

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "1")

    # ------------------------------------------------------------ helpers

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _login(self, user):
        if user:
            self.authenticate(user.login, user.login)
        else:
            self.authenticate(None, None)

    def _lineas_personales(self):
        return self.env["sale.order.line"].sudo().search(
            [("product_id.product_tmpl_id", "in", self.personales.ids)])

    def _enviar(self, renglones, **extra):
        before = self.env["sale.order"].sudo().search([]).ids
        data = {"renglones": json.dumps(renglones), "nombre": "Comprador Pruebal46",
                "telefono": "2995004602", "domicilio": "Roca 46", "entrega": "domicilio"}
        data.update(extra)
        html = self.url_open("/pedido").text
        for campo, patron in (("csrf_token", CSRF_RE), ("envio", ENVIO_RE)):
            encontrado = patron.search(html)
            data.setdefault(campo, encontrado.group(1) if encontrado else "")
        res = post_pedido(self, data)
        self.env.invalidate_all()
        return res, self.env["sale.order"].sudo().search([("id", "not in", before)])

    def _precio(self, pedido, plantilla):
        return pedido.order_line.filtered(
            lambda l: l.product_id.product_tmpl_id == plantilla).price_unit

    def _assert_ve(self, html):
        self.assertIn("Lista 3</span>", html)
        self.assertIn("Lista 4</span>", html)
        for plantilla, precio in ((self.l3, PRECIO_L3), (self.l3b, PRECIO_L3B),
                                  (self.l4, PRECIO_L4)):
            self.assertIn('data-id="%s" data-precio="%s"' % (plantilla.id, precio), html)
        # Lista 3 before Lista 4, and by name inside it
        pos = [html.index('data-id="%s"' % p.id) for p in (self.l3b, self.l3, self.l4)]
        self.assertEqual(pos, sorted(pos))
        self.assertLess(html.index("Lista 3</span>"), pos[0])
        self.assertLess(pos[1], html.index("Lista 4</span>"))

    def _assert_no_ve(self, html):
        self.assertNotIn("Lista 3</span>", html)
        self.assertNotIn("Lista 4</span>", html)
        for plantilla in self.personales:
            self.assertNotIn('data-id="%s"' % plantilla.id, html)
            self.assertNotIn(plantilla.name, html)

    def _nv_tokens(self):
        page = self.url_open("/nueva-venta")
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1), ENVIO_RE.search(page.text).group(1)

    def _nv_crear(self, renglones, salida="presupuesto"):
        csrf, envio = self._nv_tokens()
        res = self.url_open("/nueva-venta/crear", headers=FETCH, allow_redirects=False, data={
            "csrf_token": csrf, "envio": envio, "cliente_id": str(self.cliente.id),
            "renglones": json.dumps(renglones), "salida": salida, "entrega": "domicilio",
            "telefono": "2995004601", "domicilio": "Roca 46, Neuquén"})
        self.env.invalidate_all()
        return res

    # -------------------------------------------------------------- tests

    def test_who_sees_them(self):
        for user in (self.admin, self.gerencia, self.sistemas):
            self.assertTrue(ve_listas_personales(user), user.login)
        for user in (self.vendedor, self.chofer, self.armador, self.distribuidor,
                     self.kiosquero, self.env.ref("base.public_user")):
            self.assertFalse(ve_listas_personales(user), user.login)

    def test_staff_with_access_see_both_sections_on_pedido(self):
        for user in (self.admin, self.gerencia, self.sistemas):
            self._login(user)
            res = self.url_open("/pedido")
            self.assertEqual(res.status_code, 200, user.login)
            self._assert_ve(res.text)

    def test_everyone_else_does_not_see_them_on_pedido(self):
        for user in (self.vendedor, self.chofer, self.armador, self.distribuidor,
                     self.kiosquero, None):
            self._login(user)
            res = self.url_open("/pedido")
            self._assert_no_ve(res.text)
            # the usual catalog is still there
            if res.status_code == 200 and "data-id" in res.text:
                self.assertIn('data-id="%s"' % self.comun.id, res.text)

    def test_published_by_mistake_still_hidden(self):
        self.l4.is_published = True
        self._login(None)
        self._assert_no_ve(self.url_open("/pedido").text)
        res, pedido = self._enviar([{"id": self.l4.id, "cantidad": CANTIDAD}])
        self.assertFalse(pedido)
        self.assertFalse(self._lineas_personales())

    def test_admin_gerencia_sistemas_order_at_the_lista_price(self):
        before = self._ledger()
        for user in (self.admin, self.gerencia, self.sistemas):
            self._login(user)
            res, pedido = self._enviar([{"id": self.l3.id, "cantidad": CANTIDAD},
                                        {"id": self.l4.id, "cantidad": 1}])
            self.assertEqual(res.status_code, 200, (user.login, res.text[:300]))
            self.assertEqual(len(pedido), 1, user.login)
            self.assertEqual(self._precio(pedido, self.l3), PRECIO_L3, user.login)
            self.assertEqual(self._precio(pedido, self.l4), PRECIO_L4, user.login)
            # never «a confirmar», though it is a cigarette
            self.assertNotIn("SIN PRECIO", pedido.note or "")
            # assigning the customer and «Actualizar precios» keep the price
            pedido.with_context(distri_asignar_cliente=True).write(
                {"partner_id": self.cliente.id})
            self.assertEqual(pedido.pricelist_id, self.lista_cliente)
            pedido._recompute_prices()
            self.assertEqual(self._precio(pedido, self.l3), PRECIO_L3, user.login)
            self.assertEqual(self._precio(pedido, self.l4), PRECIO_L4, user.login)
            self.assertEqual(pedido.order_line.mapped("discount"), [0.0, 0.0], user.login)
        self.assertEqual(self._ledger(), before)

    def test_admin_order_for_a_customer_keeps_the_lista_price(self):
        self._login(self.admin)
        res, pedido = self._enviar([{"id": self.l3.id, "cantidad": CANTIDAD},
                                    {"id": self.comun.id, "cantidad": 1}],
                                   cliente_id=str(self.cliente.id))
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertEqual(pedido.partner_id, self.cliente)
        # the customer's list reprices the rest (10 % off), not lista 3
        self.assertEqual(self._precio(pedido, self.l3), PRECIO_L3)
        self.assertEqual(self._precio(pedido, self.comun), 4500.0)
        self.assertEqual(pedido.order_line.filtered(
            lambda l: l.product_id.product_tmpl_id == self.l3).discount, 0.0)

    def test_crafted_post_is_refused_for_everyone_else(self):
        before = self._ledger()
        for user in (self.vendedor, self.chofer, self.armador, self.distribuidor,
                     self.kiosquero, None):
            self._login(user)
            # only lista products: nothing is created
            res, pedido = self._enviar([{"id": self.l3.id, "cantidad": CANTIDAD},
                                        {"id": self.l4.id, "cantidad": CANTIDAD, "may": 1}])
            self.assertFalse(pedido, user and user.login)
            self.assertNotEqual(res.status_code, 200, user and user.login)
        self.assertFalse(self._lineas_personales())
        # mixed with a catalog product: the order has only that one
        self._login(self.vendedor)
        res, pedido = self._enviar([{"id": self.comun.id, "cantidad": 20},
                                    {"id": self.l3.id, "cantidad": CANTIDAD}],
                                   cliente_id=str(self.cliente.id))
        self.assertEqual(len(pedido), 1, res.text[:300])
        self.assertEqual(pedido.order_line.product_id.product_tmpl_id, self.comun)
        self.assertFalse(self._lineas_personales())
        self.assertEqual(self._ledger(), before)

    def test_nueva_venta_catalog_and_picker(self):
        for user in (self.admin, self.gerencia, self.sistemas):
            self._login(user)
            data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
            self.assertTrue(data["ok"], user.login)
            por_id = {p["id"]: p for p in data["productos"]}
            self.assertEqual(por_id[self.l3.id]["seccion"], "Lista 3")
            self.assertEqual(por_id[self.l3.id]["precio"], PRECIO_L3)
            self.assertEqual(por_id[self.l4.id]["seccion"], "Lista 4")
            self.assertEqual(por_id[self.l4.id]["precio"], PRECIO_L4)
            # the rest priced with the customer's list, as before
            self.assertEqual(por_id[self.comun.id]["precio"], 4500.0)
            res = self.url_open("/nueva-venta/productos?cliente_id=%s" % self.cliente.id)
            self.assertEqual(res.status_code, 200, user.login)
            self._assert_ve(res.text)
        self._login(self.vendedor)
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
        self.assertTrue(data["ok"])
        ids = {p["id"] for p in data["productos"]}
        self.assertIn(self.comun.id, ids)
        self.assertFalse(ids & set(self.personales.ids))
        self.assertNotIn("Lista 3", {p["seccion"] for p in data["productos"]})
        res = self.url_open("/nueva-venta/productos?cliente_id=%s" % self.cliente.id)
        self.assertEqual(res.status_code, 200)
        self._assert_no_ve(res.text)

    def test_nueva_venta_orders(self):
        before = self._ledger()
        self._login(self.admin)
        for salida in ("presupuesto",):
            res = self._nv_crear([{"id": self.l3.id, "cantidad": CANTIDAD}], salida)
            self.assertEqual(res.status_code, 200, (salida, res.text[:500]))
            order_id = int(re.search(r"/(\d+)", res.json()["url"]).group(1))
            pedido = self.env["sale.order"].sudo().browse(order_id)
            self.assertEqual(pedido.partner_id, self.cliente, salida)
            self.assertEqual(self._precio(pedido, self.l3), PRECIO_L3, salida)
        # a seller cannot, even crafting the post
        self._login(self.vendedor)
        lineas = self._lineas_personales()
        res = self._nv_crear([{"id": self.l3.id, "cantidad": CANTIDAD}])
        self.assertFalse(res.json().get("ok"), res.text[:300])
        self.assertEqual(self._lineas_personales(), lineas)
        self.assertEqual(self._ledger(), before)
        # «Mostrador» sells at once (its invoice is the usual one): the price
        self._login(self.admin)
        res = self._nv_crear([{"id": self.l3.id, "cantidad": CANTIDAD}], "mostrador")
        self.assertEqual(res.status_code, 200, res.text[:500])
        pedido = self.env["sale.order"].sudo().search(
            [("order_line.product_id", "=", self.l3.product_variant_id.id)],
            order="id desc", limit=1)
        self.assertEqual(self._precio(pedido, self.l3), PRECIO_L3)

    def test_backend_line_takes_the_product_price(self):
        pedido = self.env["sale.order"].create({
            "partner_id": self.cliente.id,
            "order_line": [Command.create({"product_id": self.l3.product_variant_id.id,
                                           "product_uom_qty": 3}),
                           Command.create({"product_id": self.comun.product_variant_id.id,
                                           "product_uom_qty": 1})]})
        self.assertEqual(pedido.pricelist_id, self.lista_cliente)
        self.assertEqual(self._precio(pedido, self.l3), PRECIO_L3)
        self.assertEqual(self._precio(pedido, self.comun), 4500.0)
        linea = pedido.order_line.filtered(lambda l: l.product_id.product_tmpl_id == self.l3)
        linea.product_uom_qty = 5
        self.assertEqual(linea.price_unit, PRECIO_L3)
        pedido.pricelist_id = self.general
        pedido._recompute_prices()
        self.assertEqual(linea.price_unit, PRECIO_L3)
        self.assertEqual(linea.discount, 0.0)

    def test_migration_marks_exactly_the_name_pattern(self):
        Tmpl = self.env["product.template"]
        # as the products made before this version: no value yet
        base = {"type": "consu", "sale_ok": True, "list_price": 100.0,
                "distri_lista_personal": False}
        si = {
            Tmpl.create(dict(base, name="May Migra46 BOLD 20 - lista 3")): "lista_3",
            Tmpl.create(dict(base, name="May Migra46 LUCKY - Lista 4")): "lista_4",
            Tmpl.create(dict(base, name="Migra46 Otro - LISTA 3 ")): "lista_3",
            Tmpl.create(dict(base, name="Migra46 Archivado - lista 4", active=False)): "lista_4",
        }
        no = (Tmpl.create(dict(base, name="Migra46 Lista 3 de precios"))
              | Tmpl.create(dict(base, name="Migra46 sin guion lista 3"))
              | Tmpl.create(dict(base, name="Migra46 - lista 34"))
              | Tmpl.create(dict(base, name="Migra46 - lista 5"))
              | Tmpl.create(dict(base, name="May Migra46 BOLD 20")))
        # edited by hand on the form: kept
        a_mano = Tmpl.create(dict(base, name="Migra46 Manual - lista 3"))
        a_mano.distri_lista_personal = "lista_4"
        publicado = {p.id: p.is_published for p in si}
        precios = {p.id: p.list_price for p in si}
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)),
                            "migrations", "19.0.1.129.46", "post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v129_46_migra", path)
        modulo = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(modulo)
        modulo.migrate(self.env.cr, "19.0.1.129.45")
        self.env.invalidate_all()
        for plantilla, valor in si.items():
            self.assertEqual(plantilla.distri_lista_personal, valor, plantilla.name)
            self.assertEqual(plantilla.is_published, publicado[plantilla.id])
            self.assertEqual(plantilla.list_price, precios[plantilla.id])
        for plantilla in no:
            self.assertFalse(plantilla.distri_lista_personal, plantilla.name)
        self.assertEqual(a_mano.distri_lista_personal, "lista_4")
        # a fresh install does nothing, and a second run changes nothing
        modulo.migrate(self.env.cr, False)
        self.assertEqual(marcar_por_nombre(self.env), {})
        self.assertEqual(lista_por_nombre("May BOLD 20 - lista 3"), "lista_3")
        self.assertEqual(lista_por_nombre("May BOLD 20 - Lista 4"), "lista_4")
        self.assertFalse(lista_por_nombre("May BOLD 20"))

    def test_new_product_is_marked_from_its_name(self):
        Tmpl = self.env["product.template"]
        nuevo = Tmpl.create({"name": "May Nuevo46 RED 20 - Lista 3", "type": "consu"})
        self.assertEqual(nuevo.distri_lista_personal, "lista_3")
        self.assertFalse(Tmpl.create({"name": "Nuevo46 Comun", "type": "consu"})
                         .distri_lista_personal)
        dado = Tmpl.create({"name": "Nuevo46 Dado - lista 4", "type": "consu",
                            "distri_lista_personal": False})
        self.assertFalse(dado.distri_lista_personal)
        # renaming does not change it: the form decides
        nuevo.name = "May Nuevo46 RED 20"
        self.assertEqual(nuevo.distri_lista_personal, "lista_3")

