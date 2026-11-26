"""19.0.1.129.52 (owner, 26/11/2026): «Listas y accesos» (models/listas_distri.py,
controllers/listas.py) and the restructured price lists.

- Structure: «Distribuidoras» renamed «May farmacia y varios» (same record),
  keys, the chain cigarette list -> May -> General, «Lista 2 + May farmacia»
  created, Lista 3/4 reactivated; idempotent.
- Prices per category for a FARMACIA item, a General-only item (wine), a
  cigarette with and without a price in the list.
- Category changes (chatter, open orders opt-in, packed orders untouched),
  cigarette grid, «usar precio May», CSV initial load (idempotent).
- Web access by CUIT with conflicts; group-only on every public method,
  also through call_kw; /listas 200 / 403; Inicio tile.
- /pedido: the logged-in customer sees his list, «a presupuestar» for a
  cigarette without a price; visitors and sellers see General.
- New internal users get inbox notifications; portal users keep e-mail.
- The ledger fingerprint never moves. Synthetic data only."""
import json
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import (
    CATEGORIAS_A_PRESUPUESTAR, LISTA_GENERAL, LISTA_OCULTA, LISTA_OCULTA_ANTERIOR, lista_general,
    lista_oculta,
)
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
from odoo.addons.distri_pedido.models.listas_distri import (
    GRUPO_LISTAS, MENSAJE_SIN_PERMISO, puede_listas,
)
from odoo.addons.distri_pedido.models.notificaciones import poner_bandeja
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin

P_LISTA_FARMACIA = 1000.0
P_MAY_FARMACIA = 700.0
P_LISTA_VINO = 2000.0
P_GENERAL_VINO = 1800.0
P_LISTA_CIG = 9000.0
P_LISTA3_CIG = 8500.0
P_MAY_PRODUCTO_L3 = 8400.0
CUIT = "30-71234567-9"
CUIT_DIGITOS = "30712345679"


@tagged("post_install", "-at_install", "distri_v129_52")
class TestV12952Listas(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        env["website"].search([]).company_id = env.company
        currency = env.company.currency_id.id
        Pl = env["product.pricelist"].with_context(active_test=False)
        Categ = env["product.category"]
        cls.categ_cig = Categ.search([("name", "=", CATEGORIAS_A_PRESUPUESTAR[0])], limit=1) or \
            Categ.create({"name": CATEGORIAS_A_PRESUPUESTAR[0]})
        # production's AgilPedido section category (26/11/2026)
        cls.categ_agil = Categ.search([("name", "=", "CIGARRILLOS y relacionados")], limit=1) or \
            Categ.create({"name": "CIGARRILLOS y relacionados"})
        base = {"type": "consu", "sale_ok": True, "is_published": True,
                "taxes_id": [Command.clear()], "invoice_policy": "order"}
        Tmpl = env["product.template"]
        cls.farmacia = Tmpl.create(dict(base, name="Ibuprofeno Prueba152",
                                        list_price=P_LISTA_FARMACIA))
        cls.vino = Tmpl.create(dict(base, name="Vino Prueba152", list_price=P_LISTA_VINO))
        cls.cig = Tmpl.create(dict(base, name="Cigarrillo Prueba152 A 20",
                                   list_price=P_LISTA_CIG, categ_id=cls.categ_cig.id))
        cls.cig_sin = Tmpl.create(dict(base, name="Cigarrillo Prueba152 B 20",
                                       list_price=9500.0, categ_id=cls.categ_cig.id))
        cls.may_l3 = Tmpl.create(dict(base, name="May Cigarrillo Prueba152 A 20 - lista 3",
                                      list_price=P_MAY_PRODUCTO_L3, is_published=False,
                                      categ_id=cls.categ_cig.id))
        # production as it is on 26/11/2026
        cls.general = Pl.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pl.create({"name": LISTA_GENERAL, "currency_id": currency})
        env["product.pricelist.item"].create({
            "pricelist_id": cls.general.id, "applied_on": "1_product",
            "product_tmpl_id": cls.vino.id, "compute_price": "fixed",
            "fixed_price": P_GENERAL_VINO})
        cls.may = Pl.create({
            "name": LISTA_OCULTA_ANTERIOR, "currency_id": currency, "distri_mayorista": True,
            "item_ids": [Command.create({
                "applied_on": "1_product", "product_tmpl_id": cls.farmacia.id,
                "compute_price": "fixed", "fixed_price": P_MAY_FARMACIA})]})
        cls.lista3 = Pl.create({
            "name": "Lista 3", "currency_id": currency, "distri_mayorista": True, "active": False,
            "item_ids": [
                Command.create({"applied_on": "1_product", "product_tmpl_id": cls.cig.id,
                                "compute_price": "fixed", "fixed_price": P_LISTA3_CIG}),
                Command.create({"applied_on": "3_global", "compute_price": "formula",
                                "base": "pricelist", "base_pricelist_id": cls.may.id})]})
        cls.lista4 = Pl.create({"name": "Lista 4", "currency_id": currency,
                                "distri_mayorista": True, "active": False})
        cls.revendedor = Pl.create({
            "name": "Revendedor Prueba152", "currency_id": currency, "distri_mayorista": True,
            "item_ids": [Command.create({"applied_on": "3_global", "compute_price": "formula",
                                         "base": "pricelist", "base_pricelist_id": cls.may.id})]})
        cls.listas, cls.hecho = env["distri.listas"]._asegurar_estructura()
        cls.lista2 = cls.listas["lista2"]

        def user(login, groups, **kw):
            return new_test_user(env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **kw)

        cls.gonzalo = user("gonzalo_152", "distri_pedido.grupo_gerencia," + GRUPO_LISTAS)
        cls.gerente = user("gerente_152", "distri_pedido.grupo_gerencia")
        cls.administracion = user("admin_152", "distri_pedido.grupo_administracion")
        cls.vendedor = user("vendedor_152", "distri_pedido.grupo_vendedor")
        Partner = env["res.partner"]
        cls.cliente = Partner.create({"name": "Kiosco Prueba152", "company_type": "company",
                                      "customer_rank": 1, "vat": CUIT,
                                      "property_product_pricelist": cls.general.id})
        cls.cliente_l3 = Partner.create({
            "name": "Distribuidora Prueba152", "company_type": "company", "customer_rank": 1,
            "distri_lista_oculta": True, "property_product_pricelist": cls.lista3.id})
        contacto = Partner.create({"name": "Contacto Prueba152", "parent_id": cls.cliente_l3.id,
                                   "type": "contact"})
        new_test_user(env, login="portal_152", groups="base.group_portal",
                      partner_id=contacto.id)

    # ------------------------------------------------------------ helpers

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _precio(self, lista, plantilla):
        return lista._get_product_price(plantilla.product_variant_id, 1.0)

    def _listas(self):
        return self.env["distri.listas"].with_user(self.gonzalo)

    # ------------------------------------------------------------ structure

    def test_structure_renamed_chained_and_idempotent(self):
        may = self.listas["may"]
        self.assertEqual(may, self.may, "the same record, renamed")
        self.assertEqual(may.name, LISTA_OCULTA)
        self.assertEqual(may.distri_clave, "may")
        self.assertEqual(lista_oculta(self.env), may)
        self.assertEqual(lista_general(self.env), self.general)
        self.assertEqual(self.general.distri_clave, "general")
        globales = may.item_ids.filtered(lambda i: i.applied_on == "3_global")
        self.assertEqual(globales.base_pricelist_id, self.general)
        cig_rule = may.item_ids.filtered(lambda i: i.applied_on == "2_product_category")
        self.assertEqual(cig_rule.categ_id, self.categ_cig | self.categ_agil)
        self.assertEqual(set(cig_rule.mapped("fixed_price")), {0.0})
        for clave, lista in (("lista2", self.lista2), ("lista3", self.lista3),
                             ("lista4", self.lista4)):
            self.assertEqual(self.listas[clave], lista)
            self.assertTrue(lista.active and lista.distri_mayorista)
            self.assertTrue(lista._distri_es_oculta())
            self.assertEqual(lista.item_ids.filtered(
                lambda i: i.applied_on == "3_global").base_pricelist_id, may)
        self.assertEqual(self.lista2.name, "Lista 2 + May farmacia")
        # a rename cannot break the lookup any more
        may.name = "Otro nombre"
        self.assertEqual(lista_oculta(self.env), may)
        may.name = LISTA_OCULTA
        cuenta = self.env["product.pricelist.item"].search_count([])
        listas, hecho = self.env["distri.listas"]._asegurar_estructura()
        self.assertEqual(hecho, [])
        self.assertEqual(self.env["product.pricelist.item"].search_count([]), cuenta)
        self.assertEqual(listas, self.listas)

    def test_prices_per_category(self):
        """FARMACIA: May price on every hidden list. Wine: General price.
        Cigarette with a Lista 3 price: only there; elsewhere 0 (a
        presupuestar). Cigarette without price: 0 everywhere hidden."""
        esperado = {
            self.general: (P_LISTA_FARMACIA, P_GENERAL_VINO, P_LISTA_CIG, 9500.0),
            self.may: (P_MAY_FARMACIA, P_GENERAL_VINO, 0.0, 0.0),
            self.lista2: (P_MAY_FARMACIA, P_GENERAL_VINO, 0.0, 0.0),
            self.lista3: (P_MAY_FARMACIA, P_GENERAL_VINO, P_LISTA3_CIG, 0.0),
            self.lista4: (P_MAY_FARMACIA, P_GENERAL_VINO, 0.0, 0.0),
            self.revendedor: (P_MAY_FARMACIA, P_GENERAL_VINO, 0.0, 0.0),
        }
        for lista, precios in esperado.items():
            got = tuple(self._precio(lista, p) for p in (self.farmacia, self.vino, self.cig,
                                                          self.cig_sin))
            self.assertEqual(got, precios, lista.name)

    def test_seller_cannot_read_hidden_lists(self):
        visibles = self.env["product.pricelist"].with_user(self.vendedor).search([])
        self.assertIn(self.general, visibles)
        for lista in (self.may, self.lista2, self.lista3, self.lista4):
            self.assertNotIn(lista, visibles)
        items = self.env["product.pricelist.item"].with_user(self.vendedor).search([])
        self.assertFalse(items.filtered(lambda i: i.pricelist_id == self.lista2))

    # ------------------------------------------------------------ categories

    def test_apply_category_posts_chatter_and_reprices_opt_in_orders(self):
        ledger = self._ledger()
        Order = self.env["sale.order"]
        abierto = Order.create({"partner_id": self.cliente.id, "pricelist_id": self.general.id,
                                "order_line": [Command.create({"product_id":
                                                               self.farmacia.product_variant_id.id,
                                                               "product_uom_qty": 2})]})
        otro = Order.create({"partner_id": self.cliente.id, "pricelist_id": self.general.id,
                             "order_line": [Command.create({"product_id":
                                                            self.farmacia.product_variant_id.id,
                                                            "product_uom_qty": 1})]})
        armado = Order.create({"partner_id": self.cliente.id, "pricelist_id": self.general.id,
                               "order_line": [Command.create({"product_id":
                                                              self.farmacia.product_variant_id.id,
                                                              "product_uom_qty": 1})]})
        armado.sudo().write({"distri_armado": "armado"})
        self.assertEqual(abierto.amount_untaxed, 2 * P_LISTA_FARMACIA)
        previa = self._listas()._vista_previa(self.cliente, "3")
        self.assertEqual(previa["categoria_antes"], "1")
        self.assertEqual(previa["lista"], self.lista2.name)
        self.assertTrue(previa["oculta"] and previa["cambia"])
        self.assertEqual({p["id"] for p in previa["pedidos"]}, {abierto.id, otro.id})
        self.assertEqual(self.cliente.property_product_pricelist, self.general, "preview writes nothing")
        resultado = self._listas()._aplicar_categoria(self.cliente, "3",
                                                      pedido_ids=[abierto.id, armado.id])
        self.assertTrue(resultado["cambio"])
        self.assertEqual(self.cliente.property_product_pricelist, self.lista2)
        self.assertTrue(self.cliente.distri_lista_oculta)
        self.assertEqual(self.env["distri.listas"]._categoria(self.cliente)[0], "3")
        self.assertEqual(abierto.pricelist_id, self.lista2)
        self.assertEqual(abierto.amount_untaxed, 2 * P_MAY_FARMACIA)
        self.assertEqual(otro.pricelist_id, self.general, "not opted in")
        self.assertEqual(otro.amount_untaxed, P_LISTA_FARMACIA)
        self.assertEqual(armado.pricelist_id, self.general, "packed orders are never touched")
        mensaje = self.cliente.message_ids[:1]
        self.assertIn("Lista 2 + May farmacia", mensaje.body)
        self.assertIn("General", mensaje.body)
        self.assertIn(self.gonzalo.name, mensaje.body)
        self.assertEqual(mensaje.author_id, self.gonzalo.partner_id)
        self.assertIn(abierto.name, mensaje.body)
        # back to category 1: General, not hidden
        self._listas()._aplicar_categoria(self.cliente, "1")
        self.assertEqual(self.cliente.property_product_pricelist, self.general)
        self.assertFalse(self.cliente.distri_lista_oculta)
        # category 6 needs a hidden list that is not a system one
        with self.assertRaises(UserError):
            self._listas()._aplicar_categoria(self.cliente, "6", self.general.id)
        with self.assertRaises(UserError):
            self._listas()._aplicar_categoria(self.cliente, "6", self.lista3.id)
        self._listas()._aplicar_categoria(self.cliente, "6", self.revendedor.id)
        self.assertEqual(self.env["distri.listas"]._categoria(self.cliente)[0], "6")
        with self.assertRaises(UserError):
            self._listas()._aplicar_categoria(self.cliente, "9")
        self.assertEqual(self._ledger(), ledger)

    def test_legacy_general_with_flag_is_a_revisar(self):
        socio = self.env["res.partner"].create({
            "name": "Legado Prueba152", "company_type": "company", "distri_lista_oculta": True,
            "property_product_pricelist": self.general.id})
        self.assertEqual(self.env["distri.listas"]._categoria(socio)[0], "0")

    # ------------------------------------------------------------ cigarette grid

    def test_grid_save_clear_and_use_may_price(self):
        Listas = self._listas()
        n = Listas._guardar_precios({("lista2", self.cig_sin.id): 9100.0})
        self.assertEqual(n, 1)
        self.assertEqual(self._precio(self.lista2, self.cig_sin), 9100.0)
        self.assertIn("9.100", self.lista2.message_ids[:1].body)
        self.assertEqual(Listas._guardar_precios({("lista2", self.cig_sin.id): 9100.0}), 0)
        Listas._guardar_precios({("lista2", self.cig_sin.id): None})
        self.assertEqual(self._precio(self.lista2, self.cig_sin), 0.0, "a presupuestar again")
        with self.assertRaises(UserError):
            Listas._guardar_precios({("lista2", self.farmacia.id): 1.0})
        # «May X - lista 3» linked to its cigarette
        Listas._vincular(self.may_l3, self.cig)
        fila = [f for f in Listas._grilla()["filas"] if f["id"] == self.cig.id][0]
        self.assertEqual(fila["lista3"], P_LISTA3_CIG)
        self.assertEqual(fila["may_lista3"]["precio"], P_MAY_PRODUCTO_L3)
        self.assertIsNone(fila["lista2"])
        self.assertNotIn(self.may_l3.id, [f["id"] for f in Listas._grilla()["filas"]])
        self.assertEqual(Listas._usar_precio_may("lista3"), 1)
        self.assertEqual(self._precio(self.lista3, self.cig), P_MAY_PRODUCTO_L3)
        with self.assertRaises(UserError):
            Listas._usar_precio_may("lista2")

    def test_migration_links_may_products_by_price(self):
        self.may_l3.list_price = P_LISTA3_CIG
        hechos = self.env["distri.listas"]._sugerir_vinculos()
        self.assertIn((self.may_l3, self.cig), hechos)
        self.assertEqual(self.may_l3.distri_cigarrillo_ids, self.cig)
        self.assertEqual(self.env["distri.listas"]._sugerir_vinculos(), [])

    def test_one_may_product_for_two_cigarettes_red_and_green(self):
        """Owner, 26/11/2026: «May Milenio RED / GREEN 20 - lista 3» is the
        price of two cigarettes, RED and GREEN («Milenio Mint»). The migration
        links both and «usar precio del producto May» fills both rows;
        idempotent. The new AgilPedido section category counts as cigarettes."""
        base = {"type": "consu", "sale_ok": True, "is_published": True,
                "categ_id": self.categ_agil.id}
        self.assertEqual(self.may.item_ids.filtered(
            lambda i: i.categ_id == self.categ_agil).fixed_price, 0.0)
        Tmpl = self.env["product.template"]
        # production names: the GREEN one is called «Milenio Mint» (owner)
        red = Tmpl.create(dict(base, name="MILENIO RED Box X 20", list_price=16900.0))
        green = Tmpl.create(dict(base, name="Milenio Mint", list_price=16900.0))
        Tmpl.create(dict(base, name="Milenio Gold", list_price=16900.0))
        Tmpl.create(dict(base, name="MILENIO ICERGY x 20", list_price=19400.0))
        may_rg = Tmpl.create(dict(base, name="May Milenio RED / GREEN 20 - lista 3",
                                  list_price=16000.0, is_published=False))
        self.assertEqual(may_rg.distri_lista_personal, "lista_3")
        self.assertEqual(self.env["distri.listas"]._variantes_nombre(may_rg.name),
                         ["milenio red 20", "milenio green 20"])
        hechos = self.env["distri.listas"]._sugerir_vinculos()
        self.assertIn((may_rg, red), hechos)
        self.assertIn((may_rg, green), hechos)
        self.assertEqual(may_rg.distri_cigarrillo_ids, red | green)
        self.assertEqual(self.env["distri.listas"]._sugerir_vinculos(), [], "idempotent")
        Listas = self._listas()
        filas = {f["id"]: f for f in Listas._grilla()["filas"]}
        self.assertEqual(filas[red.id]["may_lista3"]["id"], may_rg.id)
        self.assertEqual(filas[green.id]["may_lista3"]["id"], may_rg.id)
        self.assertEqual(Listas._usar_precio_may("lista3", {red.id, green.id}), 2)
        self.assertEqual(self._precio(self.lista3, red), 16000.0)
        self.assertEqual(self._precio(self.lista3, green), 16000.0)
        self.assertEqual(Listas._usar_precio_may("lista3", {red.id, green.id}), 0, "idempotent")
        self.assertEqual(self._precio(self.lista4, red), 0.0, "Lista 4 untouched")
        # a cigarette taken by another product of the same list leaves this one
        otro = Tmpl.create(dict(base, name="May Otro Prueba152 - lista 3", list_price=1.0,
                                is_published=False))
        Listas._vincular(otro, green)
        self.assertEqual(may_rg.distri_cigarrillo_ids, red)
        self.assertEqual(otro.distri_cigarrillo_ids, green)

    def test_link_screen_accepts_several_cigarettes(self):
        base = {"type": "consu", "sale_ok": True, "is_published": True,
                "categ_id": self.categ_cig.id}
        red = self.env["product.template"].create(dict(base, name="Rojo Prueba152 20"))
        self.authenticate("gonzalo_152", "gonzalo_152")
        html = self.url_open("/listas/cigarrillos/vincular").text
        token = re.search(r'name="csrf_token"[^>]*? value="([^"]+)"', html).group(1)
        campo = "v_%s" % self.may_l3.id
        res = self.url_open("/listas/cigarrillos/vincular", data=[
            ("csrf_token", token), (campo, "#%s %s" % (self.cig.id, self.cig.name)),
            (campo, "#%s %s" % (red.id, red.name)), (campo, "")])
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.may_l3.distri_cigarrillo_ids, self.cig | red)
        res = self.url_open("/listas/cigarrillos/vincular", data=[
            ("csrf_token", token), (campo, "#%s %s" % (red.id, red.name))])
        self.assertEqual(self.may_l3.distri_cigarrillo_ids, red)

    # ------------------------------------------------------------ web access

    def test_access_with_cuit_conflicts_and_without(self):
        self.cliente.distri_lista_oculta = True
        otro = self.env["res.partner"].create({"name": "Mismo CUIT Prueba152",
                                               "company_type": "company", "vat": CUIT_DIGITOS})
        socio = self.cliente.with_user(self.gonzalo)
        usuarios = self.env["res.users"].with_context(active_test=False).search_count([])
        with self.assertRaisesRegex(UserError, "Mismo CUIT Prueba152"):
            socio._distri_crear_acceso()
        self.assertEqual(self.env["res.users"].with_context(active_test=False).search_count([]),
                         usuarios, "nothing created")
        otro.vat = False
        usuario, clave = socio._distri_crear_acceso()
        self.assertEqual(usuario.login, CUIT_DIGITOS)
        self.assertTrue(usuario.share and usuario.distri_debe_cambiar_clave)
        self.assertEqual(self.cliente.distri_usuario_id, usuario)
        self.assertEqual(len(clave), 10)
        self.assertIn(CUIT_DIGITOS, self.cliente.message_ids[:1].body)
        self.assertEqual(usuario.notification_type, "email", "portal users keep e-mail")
        # a second customer with the same CUIT: the login is taken
        tercero = self.env["res.partner"].create({"name": "Tercero Prueba152",
                                                  "company_type": "company", "vat": CUIT})
        with self.assertRaisesRegex(UserError, "ya es el usuario"):
            tercero.with_user(self.gonzalo)._distri_crear_acceso()
        # no CUIT: generated login
        sin = self.env["res.partner"].create({"name": "Sin Cuit Prueba152", "ref": "SC152",
                                              "company_type": "company"})
        usuario2, _clave = sin.with_user(self.gonzalo)._distri_crear_acceso()
        self.assertEqual(usuario2.login, "sc152")
        # regenerate, deactivate, reactivate
        _u, clave2 = socio._distri_regenerar_clave()
        self.assertNotEqual(clave2, clave)
        self.assertTrue(socio._distri_desactivar_acceso())
        self.assertFalse(usuario.active)
        socio._distri_regenerar_clave()
        self.assertTrue(usuario.active)
        acciones = self.cliente.message_ids.mapped("body")
        self.assertTrue(any("desactivado" in b for b in acciones))
        filas = self._listas()._accesos()
        fila = [f for f in filas if f["socio"] == self.cliente][0]
        self.assertEqual((fila["login"], fila["activo"]), (CUIT_DIGITOS, True))

    def test_only_the_group_may_call_the_methods(self):
        socio = self.cliente
        for usuario in (self.gerente, self.administracion, self.vendedor):
            self.assertFalse(puede_listas(usuario))
            for metodo in ("action_crear_acceso_web", "action_regenerar_clave_web",
                           "action_desactivar_acceso_web"):
                with self.assertRaises(AccessError, msg="%s %s" % (usuario.login, metodo)):
                    getattr(socio.with_user(usuario), metodo)()
            Listas = self.env["distri.listas"].with_user(usuario)
            for llamada in (lambda: Listas._aplicar_categoria(socio, "2"),
                            lambda: Listas._vista_previa(socio, "2"),
                            lambda: Listas._grilla(),
                            lambda: Listas._guardar_precios({("lista2", self.cig.id): 1.0}),
                            lambda: Listas._accesos(),
                            lambda: Listas._leer_csv(b"%s,2" % str(socio.id).encode())):
                with self.assertRaises(AccessError):
                    llamada()
        self.assertFalse(socio.distri_usuario_id)
        self.assertEqual(socio.property_product_pricelist, self.general)
        self.assertTrue(puede_listas(self.gonzalo))

    def _call_kw(self, model, method, args):
        res = self.url_open("/web/dataset/call_kw/%s/%s" % (model, method), data=json.dumps({
            "jsonrpc": "2.0", "method": "call", "id": 1,
            "params": {"model": model, "method": method, "args": args, "kwargs": {}}}),
            headers={"Content-Type": "application/json"})
        return res.json()

    def test_rpc_call_kw_is_refused_outside_the_group(self):
        for login in ("gerente_152", "admin_152", "vendedor_152"):
            self.authenticate(login, login)
            for metodo in ("action_crear_acceso_web", "action_regenerar_clave_web",
                           "action_desactivar_acceso_web"):
                respuesta = self._call_kw("res.partner", metodo, [[self.cliente.id]])
                self.assertIn("error", respuesta, (login, metodo))
                self.assertEqual(respuesta["error"]["data"]["name"],
                                 "odoo.exceptions.AccessError", (login, metodo))
            # private helpers are never callable remotely
            respuesta = self._call_kw("res.partner", "_distri_crear_acceso", [[self.cliente.id]])
            self.assertIn("error", respuesta)
            respuesta = self._call_kw("distri.listas", "_aplicar_categoria",
                                      [self.cliente.id, "2"])
            self.assertIn("error", respuesta)
        self.authenticate("gerente_152", "gerente_152")
        gerente_err = self._call_kw("res.partner", "action_crear_acceso_web", [[self.cliente.id]])
        self.assertIn(MENSAJE_SIN_PERMISO, gerente_err["error"]["data"]["message"])
        self.assertFalse(self.cliente.distri_usuario_id)
        # the member can
        self.authenticate("gonzalo_152", "gonzalo_152")
        respuesta = self._call_kw("res.partner", "action_crear_acceso_web", [[self.cliente.id]])
        self.assertNotIn("error", respuesta)
        self.assertEqual(self.cliente.distri_usuario_id.login, CUIT_DIGITOS)

    # ------------------------------------------------------------ screens

    def test_listas_pages_200_for_the_group_403_for_others(self):
        paginas = ("/listas", "/listas/cliente/%s" % self.cliente.id,
                   "/listas/cliente/%s?categoria=2" % self.cliente.id, "/listas/cigarrillos",
                   "/listas/cigarrillos/vincular", "/listas/accesos", "/listas/carga")
        self.authenticate("gonzalo_152", "gonzalo_152")
        for url in paginas:
            res = self.url_open(url)
            self.assertEqual(res.status_code, 200, url)
        html = self.url_open("/listas?q=Prueba152").text
        self.assertIn("Kiosco Prueba152", html)
        self.assertIn("Distribuidora Prueba152", html)
        html = self.url_open("/listas/cigarrillos").text
        self.assertIn(self.cig.name, html)
        self.assertNotIn(self.farmacia.name, html)
        self.assertIn("a presupuestar", html)
        self.assertIn("listas", [t["key"] for t in staff_tiles(self.env(user=self.gonzalo))])
        for login in ("gerente_152", "admin_152", "vendedor_152"):
            self.authenticate(login, login)
            for url in paginas:
                self.assertEqual(self.url_open(url).status_code, 403, (login, url))
            user = self.env["res.users"].search([("login", "=", login)])
            self.assertNotIn("listas", [t["key"] for t in staff_tiles(self.env(user=user))])
        self.authenticate("gerente_152", "gerente_152")
        res = self.url_open("/listas/cliente/%s" % self.cliente.id,
                            data={"categoria": "2", "csrf_token": http_csrf(self)})
        self.assertEqual(res.status_code, 403)
        self.assertFalse(self.cliente.distri_lista_oculta)

    def test_apply_from_the_screen(self):
        self.authenticate("gonzalo_152", "gonzalo_152")
        html = self.url_open("/listas/cliente/%s?categoria=4" % self.cliente.id).text
        self.assertIn("Lista 3", html)
        token = re.search(r'name="csrf_token"[^>]*? value="([^"]+)"', html).group(1)
        res = self.url_open("/listas/cliente/%s" % self.cliente.id,
                            data={"categoria": "4", "lista": "", "csrf_token": token})
        self.assertEqual(res.status_code, 200)
        self.assertIn("categoría aplicada", res.text)
        self.assertEqual(self.cliente.property_product_pricelist, self.lista3)
        self.assertTrue(self.cliente.distri_lista_oculta)
        # the access screen creates the user and shows the password once
        res = self.url_open("/listas/accesos/%s" % self.cliente.id,
                            data={"accion": "crear", "csrf_token": token})
        self.assertEqual(res.status_code, 200)
        self.assertIn(CUIT_DIGITOS, res.text)
        self.assertEqual(self.cliente.distri_usuario_id.login, CUIT_DIGITOS)

    # ------------------------------------------------------------ CSV

    def test_csv_preview_apply_idempotent(self):
        ledger = self._ledger()
        archivado = self.env["res.partner"].create({"name": "Archivado Prueba152",
                                                    "active": False})
        contenido = "\n".join([
            "id;categoria;lista",
            "%s;2;" % self.cliente.id,
            "%s;6;%s" % (self.cliente_l3.id, self.revendedor.name),
            "999999;2;",
            "%s;7;" % self.cliente.id,
            "%s;3;" % self.cliente.id,
            "%s;2;" % archivado.id,
            "abc;1;",
        ]).encode()
        filas = self._listas()._leer_csv(contenido)
        estados = [(f["linea"], f["estado"]) for f in filas]
        self.assertEqual(estados, [(2, "ok"), (3, "ok"), (4, "error"), (5, "error"),
                                   (6, "error"), (7, "error"), (8, "error")])
        self.assertIn("desconocido", filas[2]["mensaje"])
        self.assertIn("repetido", filas[3]["mensaje"])
        self.assertIn("repetido", filas[4]["mensaje"])
        self.assertIn("archivado", filas[5]["mensaje"])
        self.assertEqual(self.cliente.property_product_pricelist, self.general, "preview only")
        aplicadas, _filas = self._listas()._aplicar_csv(contenido)
        self.assertEqual(aplicadas, 2)
        self.assertEqual(self.cliente.property_product_pricelist, self.may)
        self.assertEqual(self.cliente_l3.property_product_pricelist, self.revendedor)
        self.assertIn("carga inicial", self.cliente.message_ids[:1].body)
        aplicadas, filas = self._listas()._aplicar_csv(contenido)
        self.assertEqual(aplicadas, 0)
        self.assertEqual(filas[0]["estado"], "igual")
        bad = self._listas()._leer_csv(b"%s,6,No existe" % str(self.cliente.id).encode())
        self.assertIn("Lista desconocida", bad[0]["mensaje"])
        self.assertEqual(self._ledger(), ledger)

    # ------------------------------------------------------------ /pedido

    def _fila(self, html, plantilla):
        m = re.search(r'data-id="%s"([^>]*)>' % plantilla.id, html)
        return m.group(1) if m else None

    def test_pedido_prices_for_customer_visitor_and_seller(self):
        self.authenticate("portal_152", "portal_152")
        html = self.url_open("/pedido").text
        self.assertIn('data-precio="%s"' % P_MAY_FARMACIA, self._fila(html, self.farmacia))
        self.assertIn('data-precio="%s"' % P_LISTA3_CIG, self._fila(html, self.cig))
        self.assertIn('data-sinprecio="1"', self._fila(html, self.cig_sin))
        self.assertIn('data-precio="%s"' % P_GENERAL_VINO, self._fila(html, self.vino))
        for login in (None, "vendedor_152"):
            self.authenticate(login, login)
            html = self.url_open("/pedido").text
            self.assertIn('data-precio="%s"' % P_LISTA_FARMACIA, self._fila(html, self.farmacia))
            self.assertIn('data-sinprecio="1"', self._fila(html, self.cig))
            self.assertNotIn("%s" % P_LISTA3_CIG, html)

    # ------------------------------------------------------------ notifications

    def test_new_internal_users_get_inbox_notifications(self):
        interno = new_test_user(self.env, login="nuevo_152", groups="base.group_user")
        self.assertEqual(interno.notification_type, "inbox")
        explicito = self.env["res.users"].create({"name": "Mail 152", "login": "mail_152",
                                                  "notification_type": "email",
                                                  "group_ids": [Command.set(
                                                      [self.env.ref("base.group_user").id])]})
        self.assertEqual(explicito.notification_type, "inbox")
        portal = new_test_user(self.env, login="portalnuevo_152", groups="base.group_portal")
        self.assertEqual(portal.notification_type, "email")
        self.env.cr.execute("UPDATE res_users SET notification_type = 'email' WHERE id = %s",
                            (interno.id,))
        interno.invalidate_recordset(["notification_type"])
        self.assertEqual(poner_bandeja(interno | portal), interno)
        self.assertEqual(interno.notification_type, "inbox")
        self.assertFalse(poner_bandeja(interno | portal), "idempotent")


def http_csrf(case):
    html = case.url_open("/mi-correo").text
    m = re.search(r'name="csrf_token"[^>]*? value="([^"]+)"', html)
    return m.group(1) if m else "x"
