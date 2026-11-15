import json

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.res_partner import (
    GENERIC_LOCKED_MESSAGE, GENERIC_NAMES, GENERIC_UNLOCK_KEY,
)
from odoo.addons.distri_pedido.tests.common_pedido import post_pedido


class GenericMixin:

    @classmethod
    def _setup_generics(cls):
        Partner = cls.env["res.partner"]
        cls.retail = Partner._distri_generic_partner_for(False)
        cls.wholesale = Partner._distri_generic_partner_for(True)
        cls.admin_distri = new_test_user(
            cls.env, login="admin_generico", name="Administración Genérico",
            groups="base.group_user,distri_pedido.grupo_administracion",
        )


@tagged("post_install", "-at_install", "distri_socio_generico")
class TestSocioGenerico(GenericMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_generics()
        cls.cliente = cls.env["res.partner"].create({
            "name": "Z KIOSCO GENERICO TEST", "phone": "+54 299 411-2233",
            "vat": "20111222333", "customer_rank": 1,
        })
        cls.product = cls.env["product.product"].create({
            "name": "Producto genérico test", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()],
        })
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Genérico test", "currency_id": cls.env.company.currency_id.id})

    def _pedido_web(self, generic=None, nombre="Kiosco de la web"):
        return self.env["sale.order"].sudo().create({
            "partner_id": (generic or self.retail).id,
            "pricelist_id": self.pricelist.id,
            "distri_nombre": nombre,
            "distri_telefono": "299 411 2233",
            "distri_domicilio": "Belgrano 123",
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 2,
                "price_unit": 100.0, "tax_ids": [Command.clear()],
            })],
        })

    # ----------------------------------------------------------- lookup

    def test_lookup_by_flag_not_by_name(self):
        Partner = self.env["res.partner"]
        self.assertTrue(self.retail.distri_generic_partner)
        self.assertEqual(self.retail.distri_generic_kind, "retail")
        self.assertEqual(self.wholesale.distri_generic_kind, "wholesale")
        self.assertNotEqual(self.retail, self.wholesale)
        # a plain contact with the old name is not the generic
        impostor = Partner.create({"name": GENERIC_NAMES["retail"]})
        self.assertEqual(Partner._distri_generic_partner_for(False), self.retail)
        self.assertFalse(impostor.distri_generic_partner)
        # renamed on purpose by a system admin: still the same generic
        self.retail.with_context(**{GENERIC_UNLOCK_KEY: True}).write({"name": "Otro nombre"})
        self.assertEqual(Partner._distri_generic_partner_for(False), self.retail)
        order = self._pedido_web()
        self.assertTrue(order.distri_partner_generic)
        self.assertTrue(order._distri_sin_cliente())

    def test_fallback_creates_flagged_generic(self):
        Partner = self.env["res.partner"].sudo()
        Partner.search([("distri_generic_partner", "=", True)]).write(
            {"distri_generic_partner": False})
        nuevo = Partner._distri_generic_partner_for(True)
        self.assertTrue(nuevo.distri_generic_partner)
        self.assertEqual(nuevo.distri_generic_kind, "wholesale")
        self.assertEqual(nuevo.name, GENERIC_NAMES["wholesale"])
        self.assertEqual(Partner._distri_generic_partner_for(True), nuevo)

    # ----------------------------------------------------------- guard

    def test_generic_cannot_be_renamed_or_archived(self):
        generico = self.retail.with_user(self.admin_distri)
        for vals in ({"name": "CASTAÑEDA JUAN"}, {"city": "Loma Verde"},
                     {"phone": "2994000000"}, {"vat": "20111222333"},
                     {"company_type": "person"}, {"distri_generic_partner": False}):
            with self.subTest(vals=vals), self.assertRaises(UserError) as error:
                generico.write(vals)
            self.assertEqual(error.exception.args[0], GENERIC_LOCKED_MESSAGE)
        with self.assertRaises(UserError):
            generico.action_archive()
        with self.assertRaises(UserError):
            generico.unlink()
        self.assertEqual(self.retail.name, GENERIC_NAMES["retail"])
        self.assertTrue(self.retail.active)
        # a system admin in the regular form is blocked too...
        with self.assertRaises(UserError):
            self.retail.with_user(self.env.ref("base.user_admin")).write({"name": "X"})
        # ...harmless fields and unchanged values are fine
        generico.write({"comment": "nota interna", "name": self.retail.name})
        # other contacts are not affected
        self.cliente.with_user(self.admin_distri).write({"name": "Z KIOSCO RENOMBRADO"})

    def test_unlock_needs_system_admin(self):
        with self.assertRaises(UserError):
            self.retail.with_user(self.admin_distri).with_context(
                **{GENERIC_UNLOCK_KEY: True}).write({"name": "X"})

    # ----------------------------------------------------------- wizard

    def _asistente(self, order, **vals):
        accion = order.with_user(self.admin_distri).action_distri_asignar_cliente()
        Asistente = self.env[accion["res_model"]].with_user(self.admin_distri)
        return Asistente.with_context(accion["context"]).create(vals)

    def test_wizard_assigns_existing_customer(self):
        order = self._pedido_web(nombre="Z KIOSCO GENERICO")
        asistente = self._asistente(order)
        self.assertEqual(asistente.busqueda, "Z KIOSCO GENERICO")
        self.assertIn(self.cliente, asistente.candidato_ids)
        self.assertEqual(asistente.partner_id, self.cliente, "a single match is pre-selected")
        asistente.action_asignar()
        self.assertEqual(order.partner_id, self.cliente)
        self.assertEqual(order.state, "draft")
        self.assertFalse(order.distri_partner_generic)
        self.assertEqual(self.retail.name, GENERIC_NAMES["retail"])
        with self.assertRaises(UserError):
            order.with_user(self.admin_distri).action_distri_asignar_cliente()

    def test_customer_only_changes_through_the_wizard(self):
        order = self._pedido_web()
        with self.assertRaises(UserError):
            order.with_user(self.admin_distri).write({"partner_id": self.cliente.id})
        self.assertEqual(order.partner_id, self.retail)
        # moving between the two generics (web flow) is still allowed
        order.write({"partner_id": self.wholesale.id})
        self.assertEqual(order.partner_id, self.wholesale)

    def test_wizard_search_by_phone_and_cuit(self):
        Partner = self.env["res.partner"]
        self.assertIn(self.cliente, Partner._distri_buscar_clientes("2994112233"))
        self.assertIn(self.cliente, Partner._distri_buscar_clientes("20111222333"))
        self.assertNotIn(self.retail, Partner._distri_buscar_clientes("Pedidos web"))
        encontrados = Partner.with_context(distri_search_phone=True).name_search("299 411 2233")
        self.assertIn(self.cliente.id, [i for i, _n in encontrados])

    def test_wizard_creates_new_customer_and_approves(self):
        order = self._pedido_web(generic=self.wholesale, nombre="Almacén Nuevo Web")
        order.sudo().pricelist_id = self.pricelist
        asistente = self._asistente(order, modo="nuevo")
        self.assertEqual(asistente.nuevo_nombre, "Almacén Nuevo Web")
        self.assertEqual(asistente.nuevo_telefono, "299 411 2233")
        self.assertEqual(asistente.nuevo_lista_id, self.pricelist,
                         "a wholesale order keeps its list for the new customer")
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.pedido_requiere_whatsapp", "0")
        accion = asistente.action_asignar_y_aprobar()
        # the wizard starts from the phone typed for the new customer
        wa = self.env[accion["res_model"]].with_context(accion["context"]).create({})
        self.assertEqual((wa.area, wa.number), ("299", "4112233"))
        wa.action_save_confirm()
        self.assertEqual(order.partner_id.distri_whatsapp, "+5492994112233")
        cliente = order.partner_id
        self.assertEqual(cliente.name, "Almacén Nuevo Web")
        self.assertEqual(cliente.phone, "299 411 2233")
        self.assertEqual(cliente.street, "Belgrano 123")
        self.assertFalse(cliente.distri_generic_partner)
        self.assertEqual(cliente.property_product_pricelist, self.pricelist)
        self.assertEqual(order.pricelist_id, self.pricelist)
        self.assertEqual(order.state, "sale")

    def test_wizard_rejects_generic_and_empty(self):
        order = self._pedido_web()
        with self.assertRaises(UserError):
            self._asistente(order, partner_id=self.wholesale.id).action_asignar()
        with self.assertRaises(UserError):
            self._asistente(order, modo="nuevo", nuevo_nombre=" ").action_asignar()
        vendedor = new_test_user(self.env, login="vendedor_generico",
                                 groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(UserError):
            order.with_user(vendedor).action_distri_asignar_cliente()


@tagged("post_install", "-at_install", "distri_socio_generico")
class TestPedidoWebGenerico(GenericMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_generics()
        cls.plantilla = cls.env["product.template"].create({
            "name": "Producto web genérico", "list_price": 500, "sale_ok": True,
            "is_published": True, "type": "consu", "taxes_id": [Command.clear()],
        })

    def test_web_order_goes_under_the_flagged_generic(self):
        # the trap of the incident: a plain contact with the generic's name
        self.env["res.partner"].create({"name": GENERIC_NAMES["retail"]})
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.plantilla.id, "cantidad": 3}]),
            "nombre": "Kiosco Prueba Flag", "telefono": "2995550000",
            "domicilio": "Mitre 1", "entrega": "domicilio",
        })
        self.assertEqual(res.status_code, 200)
        order = self.env["sale.order"].search([("distri_nombre", "=", "Kiosco Prueba Flag")])
        self.assertEqual(len(order), 1)
        self.assertEqual(order.partner_id, self.retail)
        self.assertEqual(self.env["res.partner"].search_count(
            [("distri_generic_partner", "=", True), ("distri_generic_kind", "=", "retail")]), 1)


@tagged("post_install", "-at_install", "distri_socio_generico")
class TestAsignarComparacion(GenericMixin, TransactionCase):
    """The wizard shows the candidates side by side, best match first."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_generics()
        Partner = cls.env["res.partner"]
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Comparación test", "currency_id": cls.env.company.currency_id.id})
        # same phone and street, the name spelled differently
        cls.correcto = Partner.create({
            "name": "Y - PEREZ QUIQUE - DESPENSA LOS ALAMOS", "phone": "+54 9 299 555-1122",
            "street": "Los Alamos 450", "city": "Plottier", "vat": "20333444559",
            "customer_rank": 1,
        })
        # same surname, other phone and street
        cls.parecido = Partner.create({
            "name": "PEREZ QUIQUE ALBERTO", "phone": "+54 299 400-9999",
            "street": "Belgrano 100", "city": "Neuquén", "customer_rank": 1,
        })
        cls.product = cls.env["product.product"].create({
            "name": "Producto comparación", "list_price": 10, "type": "consu",
            "taxes_id": [Command.clear()],
        })

    def _pedido(self):
        return self.env["sale.order"].sudo().create({
            "partner_id": self.retail.id,
            "pricelist_id": self.pricelist.id,
            "distri_nombre": "Despensa Los Álamos (Quique)",
            "distri_telefono": "299 5551122",
            "distri_domicilio": "Los Alamos 450, Plottier",
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1,
                "price_unit": 10.0, "tax_ids": [Command.clear()],
            })],
        })

    def _asistente(self, order):
        accion = order.with_user(self.admin_distri).action_distri_asignar_cliente()
        return self.env[accion["res_model"]].with_user(self.admin_distri).with_context(
            accion["context"]).create({})

    def test_candidates_ranked_by_matches(self):
        asistente = self._asistente(self._pedido())
        socios = asistente.linea_ids.mapped("partner_id")
        self.assertIn(self.correcto, socios, "found by phone although the name differs")
        self.assertIn(self.parecido, socios, "found by a word of the name")
        primera = asistente.linea_ids[0]
        self.assertEqual(primera.partner_id, self.correcto)
        self.assertEqual((primera.m_telefono, primera.m_domicilio, primera.m_ciudad),
                         ("si", "si", "si"))
        self.assertTrue(primera.telefonos.startswith("✓ "))
        otra = asistente.linea_ids.filtered(lambda l: l.partner_id == self.parecido)
        self.assertEqual((otra.m_telefono, otra.m_domicilio, otra.m_ciudad), ("no", "no", "no"))
        self.assertTrue(otra.telefonos.startswith("✗ "))
        self.assertGreater(primera.puntaje, otra.puntaje)
        self.assertEqual(primera.cuit, "20333444559")
        self.assertEqual(primera.lista, self.correcto.property_product_pricelist.name or "")

    def test_row_pick_assigns_that_partner(self):
        order = self._pedido()
        asistente = self._asistente(order)
        otra = asistente.linea_ids.filtered(lambda l: l.partner_id == self.parecido)
        accion = otra.action_elegir()
        self.assertEqual(accion["res_id"], asistente.id, "the wizard reopens with the pick")
        self.assertEqual(asistente.partner_id, self.parecido)
        self.assertTrue(otra.elegido)
        self.assertEqual(asistente.sel_m_telefono, "no", "the card shows the mismatch")
        ficha = otra.action_ver_ficha()
        self.assertEqual(ficha["target"], "new")
        self.assertIn("/res.partner/%s" % self.parecido.id, ficha["url"])
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.pedido_requiere_whatsapp", "0")
        accion = asistente.action_asignar_y_aprobar()
        if accion.get("res_model") == "distri.wa.confirm.wizard":
            self.env[accion["res_model"]].with_context(accion["context"]).create({}).action_skip()
        self.assertEqual(order.partner_id, self.parecido)
        self.assertEqual(order.state, "sale")

    def test_name_marks(self):
        from odoo.addons.distri_pedido.models.asignar_cliente import comparar_nombre
        self.assertEqual(comparar_nombre("Almacén Carolina", "PRUEBA ANGELA CAROLINA - DESPENSA DEL SOL"),
                         "parcial", "one shared word is a resemblance")
        self.assertEqual(comparar_nombre("Kiosco 31 de Junio", "31 DE JUNIO"), "si")
        self.assertEqual(comparar_nombre("Facundo", "Y - FACUNDO"), "si")
        self.assertEqual(comparar_nombre("Kiosco Dimar", "LOPEZ JUAN"), "no")
        self.assertEqual(comparar_nombre("", "LOPEZ JUAN"), "falta")
