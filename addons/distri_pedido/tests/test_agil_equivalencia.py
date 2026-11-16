"""AgilPedido name equivalences (models/agil_equivalencia.py): what staff
teach Odoo when they correct a flagged line, and how the next message uses
it. Also the pack rule of the catalog migration applied to AgilPedido names
(«Bulto DURACELL AAA X 120U.» = «Duracell AAA» sold in «Bulto x 120»).

Names and phones are made up («Pruebagil», 299 555-01xx)."""
from collections import Counter

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL, LISTA_OCULTA
from odoo.addons.distri_pedido.models.agil_tienda import PARAM_CATALOGO_AGIL
from odoo.addons.distri_pedido.models.agilpedido_wa import (
    MOTIVO_SIN_MAYORISTA, ORIGEN_AGILPEDIDO_WA, PARAM_AUTO,
)
from odoo.addons.distri_pedido.models.exportar_agilpedido import CENTAVOS
from odoo.addons.distri_pedido.models.importar_pedido import (
    catalogo_normalizado, importe_ar, leer_envase, leer_renglon, normalizar,
)
from odoo.addons.distri_pedido.models.pedido_whatsapp import PARAM_REQUIRE


def mensaje(*renglones, total):
    """An AgilPedido message in its fixed template (English amounts, as the
    real ones)."""
    return ("Hola Distri Neuquén! Soy Carla Prueba y quiero hacer el siguiente pedido hacia "
            "*Av. Siempreviva 742, Neuquén*:\n\n%s\n\n*Total: $ %s*\n\nMuchas gracias!") % (
        "\n".join(renglones), "{:,.2f}".format(total))


@tagged("post_install", "-at_install", "distri_pedido_agilpedido")
class TestAgilEquivalencias(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # the usual catalog: with the AgilPedido copy on (a copy of production
        # may have it), codes resolve to its temporary products instead
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_CATALOGO_AGIL, "0")
        Tmpl = cls.env["product.template"]
        Pricelist = cls.env["product.pricelist"]
        cls.params = cls.env["ir.config_parameter"].sudo()
        cls.params.set_param(PARAM_AUTO, "1")
        cls.params.set_param(PARAM_REQUIRE, "1")
        cls.unidad = cls.env.ref("uom.product_uom_unit")
        Uom = cls.env["uom.uom"]
        cls.bulto120 = Uom.search([("name", "=", "Bulto x 120")], limit=1) or Uom.create({
            "name": "Bulto x 120", "relative_factor": 120.0, "relative_uom_id": cls.unidad.id})
        cls.bulto24 = Uom.search([("name", "=", "Bulto x 24")], limit=1) or Uom.create({
            "name": "Bulto x 24", "relative_factor": 24.0, "relative_uom_id": cls.unidad.id})
        cls.bulto12 = Uom.search([("name", "=", "Bulto x 12")], limit=1) or Uom.create({
            "name": "Bulto x 12", "relative_factor": 12.0, "relative_uom_id": cls.unidad.id})
        cls.carton25 = Uom.search([("name", "=", "Cartón x 25")], limit=1) or Uom.create({
            "name": "Cartón x 25", "relative_factor": 25.0, "relative_uom_id": cls.unidad.id})
        base = {"type": "consu", "sale_ok": True, "taxes_id": [Command.clear()]}
        # as the catalog migration left it: stock in units, price per unit,
        # sold ONLY by the 120 pack
        cls.duracell = Tmpl.create(dict(base, name="Duracell AAA Pruebagil", list_price=1083.3333,
                                        uom_ids=[Command.set(cls.bulto120.ids)]))
        cls.encendedor = Tmpl.create(dict(base, name="Encendedor Pruebagil Chico", list_price=500))
        cls.actron = Tmpl.create(dict(base, name="Actron Pruebagil 400 x 10", list_price=5000))
        cls.ibu = Tmpl.create(dict(base, name="Ibupirac Pruebagil 400", list_price=3000))
        cls.yerba = Tmpl.create(dict(base, name="Yerba Pruebagil Suave 1kg", list_price=3000))
        # the hidden «May» list and the public one (controllers/main.py)
        cls.mayorista = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA})
        cls.minorista = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL})
        Item = cls.env["product.pricelist.item"]
        Item.create({"pricelist_id": cls.mayorista.id, "applied_on": "1_product",
                     "product_tmpl_id": cls.actron.id, "compute_price": "fixed",
                     "fixed_price": 4000})
        Item.create({"pricelist_id": cls.mayorista.id, "applied_on": "1_product",
                     "product_tmpl_id": cls.duracell.id, "compute_price": "fixed",
                     "fixed_price": 1000})
        cls.admin_distri = new_test_user(
            cls.env, login="admin_equiv_test", name="Administración Equivalencias",
            groups="base.group_user,distri_pedido.grupo_administracion,sales_team.group_sale_manager")
        cls.vendedor = new_test_user(
            cls.env, login="vendedor_equiv_test", name="Vendedor Equivalencias",
            groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Order = cls.env["sale.order"].sudo()
        cls.Equivalencia = cls.env["distri.agil.equivalencia"]
        cls.a_corregir = cls.Order._distri_agil_producto_a_corregir()

    _seq = 0

    def _text(self, body):
        TestAgilEquivalencias._seq += 1
        msg_id = "wamid.equiv.%s" % TestAgilEquivalencias._seq
        self.Message._wa_store_webhook({"object": "whatsapp_business_account", "entry": [{
            "id": "100000000000001", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551833591",
                             "phone_number_id": "100000000000002"},
                "contacts": [{"profile": {"name": "Carla Prueba"}, "wa_id": "5492995550101"}],
                "messages": [{"from": "5492995550101", "timestamp": "1790000000", "id": msg_id,
                              "type": "text", "text": {"body": body}}],
            }}]}]})
        msg = self.Message.search([("wa_message_id", "=", msg_id)])
        order = msg.distri_agil_order_id
        self.assertTrue(order, "the message did not create an AgilPedido order")
        self.assertEqual(order.distri_origen, ORIGEN_AGILPEDIDO_WA)
        return order

    def _corregir(self, line, producto, unidad=None, factor=None, user=None):
        wizard = Form(self.env["distri.agil.corregir.renglon"].with_user(
            user or self.admin_distri).with_context(default_linea_id=line.id))
        wizard.product_id = producto.product_variant_id
        if unidad:
            wizard.product_uom_id = unidad
        if factor:
            wizard.otra_cantidad = True
            wizard.factor = factor
        return wizard.save().action_corregir()

    # ------------------------------------------------------------ envase en el nombre

    def test_pack_rule_is_the_catalog_migration_rule(self):
        self.assertEqual(leer_envase("Bulto DURACELL AAA X 120U."),
                         {"base": "DURACELL AAA", "contiene": 120})
        self.assertEqual(leer_envase("Bulto x 30 u. Pitusas Chocolate 160 g."),
                         {"base": "Pitusas Chocolate 160 g", "contiene": 30})
        self.assertEqual(leer_envase("Agua Alvura 500 cc. pack 12u."),
                         {"base": "Agua Alvura 500 cc", "contiene": 12})
        self.assertEqual(leer_envase("Bulto KIEL BOX x 25 cartones"),
                         {"base": "KIEL BOX", "contiene": 25})
        # the presentation, the weight, the buy-by-ten and the five exceptions
        for nombre in ("Actron 400 x 10", "Pitusas Frutilla x 160g.", "Heineken Lata 473 x 24",
                       "10 bultos Heineken lata 473 x 24", "75 Bultos Fernet Branca 750cc",
                       "RED POINT ECO PACK 20", "Bi Caja Regaliz Frambueza x 12 x 18g.",
                       "Bi Caja Regaliz Sandia x 12 x 18g"):
            self.assertIsNone(leer_envase(nombre), nombre)

    def test_duracell_pack_imports_as_one_bulto_with_no_difference(self):
        order = self._text(mensaje(
            "- 1 x Bulto DURACELL AAA Pruebagil X 120U. ($ 130,000.00)", total=130000))
        line = order.order_line
        self.assertEqual(line.product_id, self.duracell.product_variant_id)
        self.assertEqual(line.product_uom_id, self.bulto120)
        self.assertEqual(line.product_uom_qty, 1)
        self.assertFalse(line.distri_agil_corregir)
        self.assertEqual(line.distri_agil_nota, "Por envase: Bulto x 120")
        # the price comes from Odoo: 1,083.3333 per unit x 120 = 129,999.996,
        # rounded to the cent per pack
        self.assertEqual(line.price_unit, 130000.0)
        self.assertEqual(line.price_subtotal, 130000.0)
        self.assertEqual(order.amount_total, 130000.0)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        # six packs, as in the real S00164
        order = self._text(mensaje(
            "- 6 x Bulto DURACELL AAA Pruebagil X 120U. ($ 780,000.00)", total=780000))
        self.assertEqual(order.order_line.product_uom_qty, 6)
        self.assertEqual(order.amount_total, 780000.0)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        # nothing was learned: the line was never corrected
        self.assertFalse(self.Equivalencia.with_context(active_test=False).search(
            [("product_id", "=", self.duracell.product_variant_id.id)]))

    def test_exact_name_of_a_pack_only_product_uses_its_pack(self):
        order = self._text(mensaje("- 2 x Duracell AAA Pruebagil ($ 260,000.00)", total=260000))
        line = order.order_line
        self.assertEqual(line.product_uom_id, self.bulto120)
        self.assertEqual(line.product_uom_qty, 2)
        self.assertFalse(line.distri_agil_corregir)
        self.assertEqual(order.distri_agil_diferencia, 0.0)

    def test_pack_quantity_that_is_not_a_unit_of_the_product_is_flagged(self):
        order = self._text(mensaje(
            "- 1 x Bulto DURACELL AAA Pruebagil X 24U. ($ 26,000.00)", total=26000))
        line = order.order_line
        self.assertEqual(line.product_id, self.duracell.product_variant_id)
        self.assertTrue(line.distri_agil_corregir)
        self.assertIn("envase de 24", line.distri_agil_motivo)

    def test_presentation_is_not_a_pack(self):
        # «x 10» without a pack word is ten tablets in the box, not a unit
        order = self._text(mensaje("- 2 x Ibupirac Pruebagil 400 x 10 ($ 6,000.00)", total=6000))
        line = order.order_line
        self.assertEqual(line.product_uom_id, self.unidad)
        self.assertEqual(line.product_uom_qty, 2)
        self.assertTrue(line.distri_agil_corregir)
        self.assertIn("Parecido", line.distri_agil_motivo)
        self.assertFalse(line.distri_agil_nota)
        # and the exact name keeps its unit, with no pack
        order = self._text(mensaje("- 2 x Actron Pruebagil 400 x 10 ($ 10,000.00)", total=10000))
        self.assertEqual(order.order_line.product_uom_id, self.unidad)
        self.assertFalse(order.order_line.distri_agil_corregir)

    # ------------------------------------------------------------ corregir y aprender

    def test_correct_by_pack_saves_the_equivalence_and_the_next_order_uses_it(self):
        order = self._text(mensaje("- 6 x Pilas Qwxz Zzyq Pruebagil ($ 780,000.00)", total=780000))
        line = order.order_line
        self.assertTrue(line.distri_agil_corregir)
        self.assertEqual(line.product_id, self.a_corregir)
        self.assertEqual(order.distri_agil_por_corregir, 1)
        # the wizard proposes the only sales unit of a pack-only product
        wizard = Form(self.env["distri.agil.corregir.renglon"].with_user(
            self.admin_distri).with_context(default_linea_id=line.id))
        self.assertEqual(wizard.cantidad_agil, 6)
        wizard.product_id = self.duracell.product_variant_id
        self.assertEqual(wizard.product_uom_id, self.bulto120)
        self.assertIn("6 Bulto x 120", wizard.resumen)
        wizard.save().action_corregir()
        self.assertEqual(line.product_id, self.duracell.product_variant_id)
        self.assertEqual(line.product_uom_id, self.bulto120)
        self.assertEqual(line.product_uom_qty, 6)
        self.assertEqual(line.price_unit, 130000.0)
        self.assertFalse(line.distri_agil_corregir)
        self.assertEqual(line.distri_agil_nota, "Corregido: 1 = Bulto x 120 (120 u.)")
        # AgilPedido's quantity stays visible in the original text
        self.assertEqual(line.distri_agil_texto, "- 6 x Pilas Qwxz Zzyq Pruebagil ($ 780,000.00)")
        self.assertEqual(order.distri_agil_por_corregir, 0)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        self.assertIn("Queda aprendido", " ".join(order.message_ids.mapped("body")))
        equiv = self.Equivalencia.search([("clave", "=", "pilas qwxz zzyq pruebagil")])
        self.assertEqual(equiv.nombre_agil, "Pilas Qwxz Zzyq Pruebagil")
        self.assertEqual(equiv.product_id, self.duracell.product_variant_id)
        self.assertEqual(equiv.product_uom_id, self.bulto120)
        self.assertEqual(equiv.factor, 1)
        self.assertEqual(equiv.pedido_id, order)
        self.assertEqual(equiv.create_uid, self.admin_distri)
        self.assertEqual(equiv.usos, 0)
        # the next message with the same name, written differently
        otro = self._text(mensaje("- 2 x PILAS qwxz-zzyq  Pruebagíl. ($ 260,000.00)", total=260000))
        nueva = otro.order_line
        self.assertEqual(nueva.product_id, self.duracell.product_variant_id)
        self.assertEqual(nueva.product_uom_id, self.bulto120)
        self.assertEqual(nueva.product_uom_qty, 2)
        self.assertFalse(nueva.distri_agil_corregir)
        self.assertEqual(nueva.distri_agil_nota, "Por equivalencia: 1 = Bulto x 120 (120 u.)")
        self.assertEqual(nueva.distri_agil_equivalencia_id, equiv)
        self.assertEqual(otro.distri_agil_diferencia, 0.0)
        self.assertEqual(equiv.usos, 1)
        self.assertTrue(equiv.ultimo_uso)
        self.assertIn("1 por equivalencia", " ".join(otro.message_ids.mapped("preview")))

    def test_correct_with_a_factor_when_the_unit_does_not_exist(self):
        # AgilPedido sells a box of 50 that Odoo does not have as a unit
        order = self._text(mensaje("- 3 x Caja Encendedores Zzyq 50 ($ 75,000.00)", total=75000))
        line = order.order_line
        self._corregir(line, self.encendedor, factor=50)
        self.assertEqual(line.product_uom_id, self.unidad)
        self.assertEqual(line.product_uom_qty, 150)
        self.assertEqual(line.price_unit, 500)
        self.assertFalse(line.distri_agil_corregir)
        self.assertEqual(line.distri_agil_nota, "Corregido: 1 = 50 u.")
        equiv = self.Equivalencia.search([("clave", "=", "caja encendedores zzyq 50")])
        self.assertEqual(equiv.factor, 50)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        otro = self._text(mensaje("- 2 x Caja Encendedores Zzyq 50 ($ 50,000.00)", total=50000))
        self.assertEqual(otro.order_line.product_uom_qty, 100)
        self.assertEqual(otro.order_line.distri_agil_nota, "Por equivalencia: 1 = 50 u.")
        self.assertFalse(otro.order_line.distri_agil_corregir)

    def test_may_line_with_an_equivalence_gets_the_wholesale_price(self):
        order = self._text(mensaje("- 3 x May Actron Qwxz Grande ($ 12,000.00)", total=12000))
        self._corregir(order.order_line, self.actron)
        self.assertEqual(order.order_line.price_unit, 4000)
        equiv = self.Equivalencia.search([("clave", "=", "actron qwxz grande")])
        self.assertEqual(equiv.nombre_agil, "Actron Qwxz Grande")
        # with or without «May»: the same name; «May» still means the wholesale list
        otro = self._text(mensaje("- 5 x May Actron Qwxz Grande ($ 20,000.00)",
                                  "- 1 x Actron Qwxz Grande ($ 5,000.00)", total=25000))
        may, comun = otro.order_line
        self.assertEqual(may.product_id, self.actron.product_variant_id)
        self.assertEqual(may.price_unit, 4000)
        self.assertEqual(may.product_uom_qty, 5)
        self.assertFalse(may.distri_agil_corregir)
        self.assertEqual(comun.price_unit, self.minorista._get_product_price(
            self.actron.product_variant_id, 1))
        self.assertEqual(equiv.usos, 2)
        # a «May» pack: the wholesale price per unit times the pack
        otro = self._text(mensaje(
            "- 1 x May Bulto DURACELL AAA Pruebagil X 120U. ($ 120,000.00)", total=120000))
        self.assertEqual(otro.order_line.product_uom_id, self.bulto120)
        self.assertEqual(otro.order_line.price_unit, 120000)
        self.assertEqual(otro.distri_agil_diferencia, 0.0)

    def test_confirming_a_fuzzy_match_learns_factor_one(self):
        order = self._text(mensaje("- 2 x Yerba Pruebagil Suave 1 kilo ($ 6,000.00)", total=6000))
        line = order.order_line
        self.assertEqual(line.product_id, self.yerba.product_variant_id)
        self.assertTrue(line.distri_agil_corregir)
        line.with_user(self.admin_distri).action_distri_agil_renglon_ok()
        self.assertFalse(line.distri_agil_corregir)
        equiv = self.Equivalencia.search([("clave", "=", "yerba pruebagil suave 1 kilo")])
        self.assertEqual(equiv.product_id, self.yerba.product_variant_id)
        self.assertEqual(equiv.product_uom_id, self.unidad)
        self.assertEqual(equiv.factor, 1)
        otro = self._text(mensaje("- 4 x Yerba Pruebagil Suave 1 kilo ($ 12,000.00)", total=12000))
        self.assertFalse(otro.order_line.distri_agil_corregir)
        self.assertEqual(otro.order_line.product_uom_qty, 4)

    def test_choosing_the_product_in_the_line_learns_it_in_its_pack(self):
        order = self._text(mensaje("- 1 x Pilas Tripla Qwxz ($ 130,000.00)", total=130000))
        line = order.order_line
        # what the web client sends: the product and the unit Odoo proposes (stock unit)
        line.with_user(self.admin_distri).write({
            "product_id": self.duracell.product_variant_id.id,
            "product_uom_id": self.unidad.id})
        self.assertFalse(line.distri_agil_corregir)
        # a pack-only product goes to its pack, priced by Odoo
        self.assertEqual(line.product_uom_id, self.bulto120)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        equiv = self.Equivalencia.search([("clave", "=", "pilas tripla qwxz")])
        self.assertEqual(equiv.product_uom_id, self.bulto120)
        self.assertEqual(equiv.factor, 1)

    def test_wholesale_price_only_flag_is_accepted_without_learning(self):
        order = self._text(mensaje("- 1 x May Yerba Pruebagil Suave 1kg ($ 2,500.00)", total=2500))
        line = order.order_line
        self.assertEqual(line.distri_agil_motivo, MOTIVO_SIN_MAYORISTA)
        line.with_user(self.admin_distri).action_distri_agil_renglon_ok()
        self.assertFalse(line.distri_agil_corregir)
        self.assertFalse(self.Equivalencia.search([("clave", "=", "yerba pruebagil suave 1kg")]))

    # ------------------------------------------------------------ cuando no sirve

    def test_archived_equivalence_or_unsellable_product_falls_back_to_flagging(self):
        equiv = self.Equivalencia.sudo()._distri_aprender(
            "Pilas Archivo Qwxz", self.duracell.product_variant_id, self.bulto120)
        equiv.active = False
        order = self._text(mensaje("- 1 x Pilas Archivo Qwxz ($ 130,000.00)", total=130000))
        self.assertTrue(order.order_line.distri_agil_corregir)
        self.assertEqual(order.order_line.product_id, self.a_corregir)
        self.assertEqual(equiv.usos, 0)
        # correcting it again brings it back: the new correction rules
        self._corregir(order.order_line, self.duracell)
        self.assertTrue(equiv.active)
        # the product is no longer sold: the equivalence is ignored and the line flagged
        equiv2 = self.Equivalencia.sudo()._distri_aprender(
            "Encendedor Viejo Qwxz", self.encendedor.product_variant_id, self.unidad)
        self.encendedor.sale_ok = False
        order = self._text(mensaje("- 1 x Encendedor Viejo Qwxz ($ 500.00)", total=500))
        line = order.order_line
        self.assertTrue(line.distri_agil_corregir)
        self.assertIn("ya no se puede vender", line.distri_agil_motivo)
        self.assertEqual(equiv2.usos, 0)
        # an archived product, too
        self.encendedor.sale_ok = True
        self.encendedor.active = False
        order = self._text(mensaje("- 1 x Encendedor Viejo Qwxz ($ 500.00)", total=500))
        self.assertTrue(order.order_line.distri_agil_corregir)

    def test_the_placeholder_product_is_never_learned(self):
        order = self._text(mensaje("- 1 x Qwxz Nunca Visto Zzyq ($ 500.00)", total=500))
        line = order.order_line
        self.assertEqual(line.product_id, self.a_corregir)
        # flagged and never corrected: nothing learned
        self.assertFalse(self.Equivalencia.search([("clave", "=", "qwxz nunca visto zzyq")]))
        self.assertFalse(self.Equivalencia.sudo()._distri_aprender(
            "Qwxz Nunca Visto Zzyq", self.a_corregir, self.unidad))
        with self.assertRaises(UserError):
            self.env["distri.agil.corregir.renglon"].with_user(self.admin_distri).create({
                "linea_id": line.id, "product_id": self.a_corregir.id,
                "product_uom_id": self.unidad.id}).action_corregir()
        with self.assertRaises(ValidationError):
            self.Equivalencia.sudo().create({
                "nombre_agil": "Qwxz Nunca Visto Zzyq", "product_id": self.a_corregir.id,
                "product_uom_id": self.unidad.id})
        # a line matched automatically and never touched is not learned either
        order = self._text(mensaje("- 1 x Yerba Pruebagil Suave 1kg ($ 3,000.00)", total=3000))
        self.assertFalse(order.order_line.distri_agil_corregir)
        self.assertFalse(self.Equivalencia.search([("clave", "=", "yerba pruebagil suave 1kg")]))

    def test_normalization_tolerates_case_accents_and_punctuation(self):
        equiv = self.Equivalencia.sudo()._distri_aprender(
            "Bulto DURACELL AAA X 120U. Qwxz", self.duracell.product_variant_id, self.bulto120)
        self.assertEqual(equiv.clave, "bulto duracell aaa x 120u qwxz")
        for nombre in ("bulto duracell aaa x 120u qwxz", "BULTO DURACELL AAA X 120U QWXZ",
                       "Bultó Duracéll AAA x 120U., Qwxz", "  Bulto  DURACELL-AAA X 120U  Qwxz  "):
            self.assertEqual(self.Equivalencia._distri_buscar(nombre), equiv, nombre)
        self.assertEqual(normalizar("Pañuelitos Élite"), "panuelitos elite")
        with mute_logger("odoo.sql_db"), self.assertRaises(Exception), self.env.cr.savepoint():
            self.Equivalencia.sudo().create({
                "nombre_agil": "bulto duracell aaa x 120u, qwxz",
                "product_id": self.duracell.product_variant_id.id,
                "product_uom_id": self.bulto120.id})
        # a unit the product is not sold in is refused
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.Equivalencia.sudo().create({
                "nombre_agil": "Otra Qwxz", "product_id": self.duracell.product_variant_id.id,
                "product_uom_id": self.unidad.id})

    # ------------------------------------------------------------ acceso

    def test_access_rights(self):
        equiv = self.Equivalencia.sudo()._distri_aprender(
            "Pilas Acceso Qwxz", self.duracell.product_variant_id, self.bulto120)
        # administration reads, edits, archives and creates
        as_admin = equiv.with_user(self.admin_distri)
        self.assertEqual(as_admin.nombre_agil, "Pilas Acceso Qwxz")
        as_admin.write({"nombre_agil": "Pilas Acceso Qwxz 2"})
        as_admin.action_archive()
        self.assertFalse(equiv.active)
        self.Equivalencia.with_user(self.admin_distri).create({
            "nombre_agil": "Otro Acceso Qwxz", "product_id": self.encendedor.product_variant_id.id,
            "product_uom_id": self.unidad.id})
        with self.assertRaises(AccessError):
            as_admin.unlink()
        # a salesperson has no access at all
        with self.assertRaises(AccessError):
            equiv.with_user(self.vendedor).read(["nombre_agil"])
        with self.assertRaises(AccessError):
            self.Equivalencia.with_user(self.vendedor).create({
                "nombre_agil": "Vendedor Qwxz", "product_id": self.encendedor.product_variant_id.id,
                "product_uom_id": self.unidad.id})
        with self.assertRaises(AccessError):
            self.env["distri.agil.corregir.renglon"].with_user(self.vendedor).create({})
        # the import runs as the webhook (sudo), whoever is logged in
        order = self._text(mensaje("- 1 x Otro Acceso Qwxz ($ 500.00)", total=500))
        self.assertFalse(order.order_line.distri_agil_corregir)

    # ------------------------------------------------------------ asistente de pegar

    def test_paste_wizard_uses_equivalences_and_packs(self):
        equiv = self.Equivalencia.sudo()._distri_aprender(
            "Pilas Pegar Qwxz", self.duracell.product_variant_id, self.bulto120)
        wizard = self.env["distri.importar.pedido"].with_user(self.admin_distri).create({
            "mensaje": mensaje("- 2 x Pilas Pegar Qwxz ($ 260,000.00)",
                               "- 1 x Bulto DURACELL AAA Pruebagil X 120U. ($ 130,000.00)",
                               "- 2 x Actron Pruebagil 400 x 10 ($ 10,000.00)", total=400000)})
        wizard.action_analizar()
        aprendida, envase, presentacion = wizard.linea_ids
        self.assertEqual(aprendida.equivalencia_id, equiv)
        self.assertEqual(aprendida.product_uom_id, self.bulto120)
        self.assertEqual(envase.product_tmpl_id, self.duracell)
        self.assertEqual(envase.product_uom_id, self.bulto120)
        self.assertEqual(presentacion.product_uom_id, self.unidad)
        self.assertIn("1 por equivalencia", wizard.resumen)
        wizard.partner_id = self.env["res.partner"].create({"name": "Kiosco Pegar Qwxz"})
        wizard.pricelist_id = self.minorista
        order = self.env["sale.order"].browse(wizard.action_crear_presupuesto()["res_id"])
        pilas = order.order_line.filtered(lambda l: l.product_id == self.duracell.product_variant_id)
        self.assertEqual(pilas.mapped("product_uom_id"), self.bulto120)
        self.assertEqual(sum(pilas.mapped("price_subtotal")), 390000.0)
        self.assertEqual(equiv.usos, 1)
        self.assertLessEqual(equiv.ultimo_uso, fields.Datetime.now())

    # ------------------------------------------------------------ catalogo exportado

    def test_leading_store_code_is_ignored(self):
        for linea in ("- 2 x 251269 Duracell AAA ($ 1,00)", "- 2 x 2512690120 - Duracell AAA ($ 1,00)",
                      "- 2 x [937] Duracell AAA ($ 1,00)", "- 2 x (937) Duracell AAA ($ 1,00)",
                      "- 2 x #251269: Duracell AAA ($ 1,00)", "- 2 x 251269 · Duracell AAA ($ 1,00)"):
            self.assertEqual(leer_renglon(linea)["nombre"], "Duracell AAA", linea)
        renglon = leer_renglon("- 1 x 2512695120 May Duracell AAA · Bulto x 120 u. ($ 1,00)")
        self.assertEqual(renglon["codigo"], "2512695120")
        self.assertTrue(renglon["es_mayorista"])
        self.assertEqual(renglon["nombre_limpio"], "Duracell AAA · Bulto x 120 u.")
        # a short number is part of the name
        self.assertEqual(leer_renglon("- 1 x 10 bultos Heineken ($ 1,00)")["nombre"], "10 bultos Heineken")

    def test_exported_suffix_is_read_back_by_quantity(self):
        self.assertEqual(leer_envase("Duracell AAA · Bulto x 120 u."),
                         {"base": "Duracell AAA", "contiene": 120})
        # the part before «·» is the Odoo name as it is, pack words and all
        self.assertEqual(leer_envase("Bulto Sky Cosmic 12 u. · Bulto x 12 u."),
                         {"base": "Bulto Sky Cosmic 12 u.", "contiene": 12})
        self.assertEqual(leer_envase("Master BOX x 25 cartones · Cartón x 25 u."),
                         {"base": "Master BOX x 25 cartones", "contiene": 25})
        for nombre in ("Master BOX x 25 cartones", "May Pañuelitos Elite Bulto 35",
                       "Pipas Clásicas 18g. Display", "Heineken Lata 473 x 24",
                       "Bulto Sky Cosmic 12 u."):
            self.assertIsNone(leer_envase(nombre), nombre)
        # the word does not matter, the quantity picks the unit: Kiel sells by the carton
        kiel = self.env["product.template"].create({
            "name": "Kiel Pruebagil BOX", "type": "consu", "sale_ok": True, "list_price": 470,
            "taxes_id": [Command.clear()], "uom_ids": [Command.set(self.carton25.ids)]})
        order = self._text(mensaje("- 2 x Kiel Pruebagil BOX · Bulto x 25 u. ($ 23,500.00)",
                                   total=23500))
        self.assertEqual(order.order_line.product_id, kiel.product_variant_id)
        self.assertEqual(order.order_line.product_uom_id, self.carton25)
        self.assertFalse(order.order_line.distri_agil_corregir)
        self.assertEqual(order.distri_agil_diferencia, 0.0)

    def _vuelta(self, fila, catalogo, repetidos, listas, con_codigo=False):
        """What the importer makes of one exported row sold once, by its name
        or, as the store sends it, with the code in front."""
        precio = fila[4] / CENTAVOS
        nombre = "%s - %s" % (fila[0], fila[1]) if con_codigo else fila[1]
        texto = "- 1 x %s ($ %s)" % (nombre, "{:,.2f}".format(precio))
        renglon = leer_renglon(texto)
        self.assertTrue(renglon, texto)
        renglon["importe_valor"] = importe_ar(renglon["importe"])
        valores = self.Order._distri_agil_renglon(renglon, catalogo, repetidos, *listas)
        linea = self.env["sale.order.line"].new(valores)
        return valores, precio, round(linea.price_unit * linea.product_uom_qty, 2)

    def test_exported_catalog_round_trip(self):
        Tmpl = self.env["product.template"]
        base = {"type": "consu", "sale_ok": True, "taxes_id": [Command.clear()],
                "is_published": True}
        # sold loose AND by the pack of 12: one row for each
        agua = Tmpl.create(dict(base, name="Agua Pruebagil 500 cc", list_price=1016.6667,
                                uom_ids=[Command.set((self.unidad | self.bulto12).ids)]))
        # names that already carry pack words or a number: names, not suffixes
        sky = Tmpl.create(dict(base, name="Bulto Sky Pruebagil 12 u.", list_price=9800,
                               uom_ids=[Command.set(self.bulto12.ids)]))
        panuelos = Tmpl.create(dict(base, name="May Pañuelitos Pruebagil Bulto 35",
                                    list_price=7583.3333,
                                    uom_ids=[Command.set(self.bulto12.ids)]))
        red_point = Tmpl.search([("name", "=ilike", "red point eco pack 20")], limit=1) or \
            Tmpl.create(dict(base, name="RED POINT ECO PACK 20", list_price=15500))
        plain = Tmpl.create(dict(base, name="Chicle Pruebagil Menta", list_price=333.33))
        # over AgilPedido's 50 characters: the Odoo name is shortened, never the pack
        largo = Tmpl.create(dict(base, name="Galletitas Pruebagil Surtidas Rellenas de Chocolate y "
                                             "Vainilla", list_price=1000,
                                 uom_ids=[Command.set(self.bulto12.ids)]))
        self.env["product.pricelist.item"].create({
            "pricelist_id": self.mayorista.id, "applied_on": "1_product",
            "product_tmpl_id": largo.id, "compute_price": "fixed", "fixed_price": 900})
        self.duracell.default_code = "AP-999001"
        exportar = self.env["distri.exportar.agilpedido"].with_user(self.admin_distri).create({})
        filas, cuenta = exportar._filas()
        self.assertEqual(filas[0], ["Codigo", "Nombre", "Descripción", "Visible", "Precio",
                                    "Precio salon", "Destacado"])
        nombres = {fila[1]: fila for fila in filas[1:]}
        esperadas = {
            "Duracell AAA Pruebagil · Bulto x 120 u.": (self.duracell, self.bulto120, 130000.0),
            "May Duracell AAA Pruebagil · Bulto x 120 u.": (self.duracell, self.bulto120, 120000.0),
            "Actron Pruebagil 400 x 10": (self.actron, self.unidad, 5000.0),
            "May Actron Pruebagil 400 x 10": (self.actron, self.unidad, 4000.0),
            "Agua Pruebagil 500 cc": (agua, self.unidad, 1016.67),
            "Agua Pruebagil 500 cc · Bulto x 12 u.": (agua, self.bulto12, 12200.0),
            "Bulto Sky Pruebagil 12 u. · Bulto x 12 u.": (sky, self.bulto12, 117600.0),
            "May Pañuelitos Pruebagil Bulto 35 · Bulto x 12 u.": (panuelos, self.bulto12, 91000.0),
            red_point.name: (red_point, self.unidad, None),
            "Chicle Pruebagil Menta": (plain, self.unidad, 333.33),
            "Galletitas Pruebagil Surtidas Rel… · Bulto x 12 u.": (largo, self.bulto12, 12000.0),
            "May Galletitas Pruebagil Surtidas… · Bulto x 12 u.": (largo, self.bulto12, 10800.0),
        }
        for nombre, (producto, unidad, precio) in esperadas.items():
            self.assertIn(nombre, nombres)
            if precio is not None:
                self.assertEqual(nombres[nombre][4], round(precio * CENTAVOS, 2), nombre)
            # «Destacado» is a badge text in AgilPedido, not yes/no
            self.assertEqual(nombres[nombre][6], "", nombre)
        self.assertNotIn("Duracell AAA Pruebagil", nombres)
        self.assertGreaterEqual(cuenta["envases"], 5)
        self.assertTrue(all(len(fila[1]) <= 50 for fila in filas[1:]))
        self.assertIn("Galletitas Pruebagil Surtidas Rel… · Bulto x 12 u.", cuenta["acortados"])
        # numeric codes, one per row, stable and decodable: n for the main row
        # (the pack of a pack-only product), n*10000 + N for another unit,
        # n*10000 + 5000 + N for «May»
        self.assertEqual(nombres["Duracell AAA Pruebagil · Bulto x 120 u."][0], 999001)
        self.assertEqual(nombres["May Duracell AAA Pruebagil · Bulto x 120 u."][0], 9990015120)
        n_agua = 9000000 + agua.id
        filas_agua = {agua.uom_ids[0]: n_agua}
        otra = agua.uom_ids[1]
        filas_agua[otra] = n_agua * 10000 + int(otra.relative_factor)
        self.assertEqual(nombres["Agua Pruebagil 500 cc"][0], filas_agua[self.unidad])
        self.assertEqual(nombres["Agua Pruebagil 500 cc · Bulto x 12 u."][0], filas_agua[self.bulto12])
        n_actron = 9000000 + self.actron.id
        self.assertEqual(nombres["May Actron Pruebagil 400 x 10"][0], n_actron * 10000 + 5001)
        codigos = [fila[0] for fila in filas[1:]]
        self.assertEqual(len(codigos), len(set(codigos)))
        self.assertTrue(all(isinstance(codigo, int) for codigo in codigos))
        # every row of these products comes back as the same product and unit, with difference 0
        catalogo = catalogo_normalizado(self.env)
        repetidos = Counter(catalogo.values())
        listas = self.Order._distri_agil_listas()
        propias = [fila for fila in filas[1:] if fila[1] in esperadas]
        self.assertEqual(len(propias), len(esperadas))
        for fila in propias:
            for con_codigo in (False, True):
                valores, precio, subtotal = self._vuelta(fila, catalogo, repetidos, listas,
                                                         con_codigo)
                producto, unidad, _precio = esperadas[fila[1]]
                self.assertEqual(valores["product_id"], producto.product_variant_id.id, fila[1])
                self.assertEqual(valores["product_uom_id"], unidad.id, fila[1])
                self.assertFalse(valores["distri_agil_corregir"], fila[1])
                self.assertEqual(subtotal, precio, fila[1])
                if con_codigo:
                    self.assertIn("Por código %s" % fila[0], valores["distri_agil_nota"])
                    self.assertNotIn("no es el de Odoo", valores["distri_agil_nota"], fila[1])
        # and a whole message with the exported names, with the store's code in
        # front in some of the forms it may take: difference 0
        order = self._text(mensaje(
            "- 6 x 9990010120 Duracell AAA Pruebagil · Bulto x 120 u. ($ 780,000.00)",
            "- 1 x [9990015120] May Duracell AAA Pruebagil · Bulto x 120 u. ($ 120,000.00)",
            "- 2 x Actron Pruebagil 400 x 10 ($ 10,000.00)",
            "- 1 x %s - Galletitas Pruebagil Surtidas Rel… · Bulto x 12 u. ($ 12,000.00)" % (
                nombres["Galletitas Pruebagil Surtidas Rel… · Bulto x 12 u."][0]),
            total=922000))
        self.assertFalse(order.distri_agil_por_corregir)
        self.assertEqual(order.order_line[1].price_unit, 120000)
        self.assertEqual(order.order_line[3].product_id, largo.product_variant_id)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        self.assertIn("Las fotos no van", exportar.action_exportar() and exportar.resumen)

    # ------------------------------------------------------------ el codigo primero

    MENSAJE_CON_CODIGOS = (
        "Hola Pedidos Fácil ! Soy *Prueba Odoo* y quiero hacer el siguiente pedido hacia "
        "*Calle Falsa 123*:\n\n"
        "- 1 x 242703 - Cerveza Andes Lata 473 cc · Bulto x 24 u. ($ 42.000,00)\n"
        "- 1 x 251269 - Duracell AAA · Bulto x 120 u. ($ 130.000,00)\n"
        "- 1 x 147277 - Pila Energizer AAA x 10 ($ 10.500,00)\n\n"
        "*Total: $ 182.500,00*\n\nMuchas gracias!")

    def _producto_con_codigo(self, codigo, nombre, precio, unidades=None, regla=None):
        """The real product of the test store message, or the same one on an
        empty database."""
        variante = self.env["product.product"].search([("default_code", "=", codigo)], limit=1)
        if variante:
            return variante.product_tmpl_id
        plantilla = self.env["product.template"].create({
            "name": nombre, "type": "consu", "sale_ok": True, "list_price": precio,
            "default_code": codigo, "taxes_id": [Command.clear()],
            "uom_ids": [Command.set(unidades.ids)] if unidades else False})
        if regla:
            self.env["product.pricelist.item"].create(dict(regla, product_tmpl_id=plantilla.id))
        return plantilla

    def test_real_test_store_message_with_codes(self):
        """mensaje-generado.txt, as AgilPedido wrote it (Argentine amounts)."""
        andes = self._producto_con_codigo("AP-242703", "Cerveza Andes Lata 473 cc", 1750,
                                          self.bulto24)
        duracell = self._producto_con_codigo("AP-251269", "Duracell AAA", 1083.3333,
                                             self.bulto120)
        energizer = self._producto_con_codigo("AP-147277", "Pila Energizer AAA x 10", 10500)
        order = self._text(self.MENSAJE_CON_CODIGOS)
        self.assertEqual(order.distri_nombre, "Prueba Odoo")
        self.assertEqual(order.distri_domicilio, "Calle Falsa 123")
        self.assertEqual(order.distri_agil_total, 182500.0)
        self.assertTrue(order.distri_agil_cierra)
        andes_l, duracell_l, energizer_l = order.order_line
        self.assertEqual(order.distri_agil_por_corregir, 0)
        self.assertEqual((andes_l.product_id, andes_l.product_uom_id, andes_l.product_uom_qty),
                         (andes.product_variant_id, self.bulto24, 1))
        self.assertEqual(andes_l.price_subtotal, 42000.0)
        self.assertEqual((duracell_l.product_id, duracell_l.product_uom_id),
                         (duracell.product_variant_id, self.bulto120))
        self.assertEqual(duracell_l.price_subtotal, 130000.0)
        self.assertEqual((energizer_l.product_id, energizer_l.product_uom_id),
                         (energizer.product_variant_id, energizer.uom_id))
        self.assertEqual(energizer_l.price_subtotal, 10500.0)
        self.assertEqual(duracell_l.distri_agil_nota, "Por código 251269")
        self.assertEqual(order.amount_total, 182500.0)
        self.assertEqual(order.distri_agil_diferencia, 0.0)

    def test_code_wins_over_the_name_and_may_still_means_wholesale(self):
        self.duracell.default_code = "AP-999002"
        # an old name in AgilPedido, but the code says which product it is
        order = self._text(mensaje("- 2 x 999002 - Pilas Viejas Qwxz ($ 260,000.00)", total=260000))
        line = order.order_line
        self.assertEqual(line.product_id, self.duracell.product_variant_id)
        self.assertEqual(line.product_uom_id, self.bulto120)
        self.assertFalse(line.distri_agil_corregir)
        self.assertIn("el nombre no es el de Odoo", line.distri_agil_nota)
        # «May» in the name sets the wholesale price even with the main row's code
        order = self._text(mensaje("- 1 x 999002 - May Duracell AAA Pruebagil ($ 120,000.00)",
                                   total=120000))
        self.assertEqual(order.order_line.price_unit, 120000)
        # and the «May» row's code, with the name in English amounts
        order = self._text(mensaje("- 1 x 9990025120 - Duracell AAA Pruebagil · Bulto x 120 u. "
                                   "($ 120,000.00)", total=120000))
        self.assertEqual(order.order_line.price_unit, 120000)
        self.assertEqual(order.distri_agil_diferencia, 0.0)
        # a pack the product is not sold in is flagged
        order = self._text(mensaje("- 1 x 999002 - Duracell AAA Pruebagil · Bulto x 24 u. "
                                   "($ 26,000.00)", total=26000))
        self.assertTrue(order.order_line.distri_agil_corregir)
        # an unknown code falls back to the name
        order = self._text(mensaje("- 1 x 123456789 - Duracell AAA Pruebagil ($ 130,000.00)",
                                   total=130000))
        self.assertEqual(order.order_line.product_id, self.duracell.product_variant_id)
        self.assertFalse(order.order_line.distri_agil_corregir)
        # an archived or unsellable product is flagged
        self.duracell.sale_ok = False
        order = self._text(mensaje("- 1 x 999002 - Duracell AAA Pruebagil ($ 130,000.00)",
                                   total=130000))
        self.assertTrue(order.order_line.distri_agil_corregir)
        self.assertIn("ya no se vende", order.order_line.distri_agil_motivo)
        self.duracell.sale_ok = True
        self.duracell.active = False
        order = self._text(mensaje("- 1 x 999002 - Duracell AAA Pruebagil ($ 130,000.00)",
                                   total=130000))
        self.assertTrue(order.order_line.distri_agil_corregir)
