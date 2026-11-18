"""19.0.1.103.0: «Pegar número» (what WhatsApp shows in the contact info, or
any pasted text with a number), the phone search of «Asignar cliente» on the
10 national digits, and «Copiar código». Synthetic data only: the numbers
(299 000-1031 …) are not real customers'."""
from lxml import etree

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import BaseCase, Form, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.ar_phone import (
    MSG_REVISAR_TELEFONO, MSG_SIN_NUMERO, read_pasted_phone,
)
from odoo.addons.distri_pedido.models.asignar_cliente import comparar_telefono

NATIONAL = "2990001031"
PASTED = "+54 9 299 000-1031"


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_pegar")
class TestNumeroPegado(BaseCase):

    def test_what_whatsapp_shows_and_other_pasted_texts(self):
        for pasted in (
                "+54 9 299 000-1031",
                # WhatsApp Web wraps it in direction marks; non-breaking
                # spaces and dashes
                "‪+54 9 299 000-1031‬",
                "‎+54 9 299 000‑1031",
                "Juan Pérez ~ +54 9 299 000-1031",
                "mi cel es 0299 15 000 1031, gracias",
                "5492990001031",
                "+5492990001031",
                "299 000 1031",
                "(0299) 15-000-1031",
                "Pedido S00164 · código 7K3P\n+54 9 299 000-1031"):
            self.assertEqual(read_pasted_phone(pasted), (NATIONAL, ""), repr(pasted))

    def test_texts_without_a_readable_number(self):
        self.assertEqual(read_pasted_phone(""), ("", ""))
        self.assertEqual(read_pasted_phone("   "), ("", ""))
        self.assertEqual(read_pasted_phone("Juan Pérez"), ("", MSG_SIN_NUMERO))
        self.assertEqual(read_pasted_phone("+1 555 123 4567"), ("", MSG_REVISAR_TELEFONO))
        self.assertEqual(read_pasted_phone("299 000 103"), ("", MSG_REVISAR_TELEFONO))

    def test_match_marks_compare_the_national_number(self):
        self.assertEqual(comparar_telefono("0299 15 000-1031", ["+54 9 299 000 1031"]), "si")
        self.assertEqual(comparar_telefono(PASTED, ["299 000-1031"]), "si")
        self.assertEqual(comparar_telefono(PASTED, ["299 000-9999"]), "no")


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_pegar")
class TestBuscarPorTelefono(TransactionCase):
    """Reproduces the miss of the audit: «+54 9 …» pasted in «Asignar
    cliente» did not find a customer stored without 54 9."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        cls.stored = {}
        for key, vals in {
            "spaces": {"phone": "299 000-1031"},
            "plus54": {"phone": "+54 299 000 1031"},
            "digits": {"phone": "2990001031"},
            "with15": {"phone": "0299 15 000-1031"},
            "whatsapp": {"distri_whatsapp": "+5492990001031"},
            "no_area": {"phone": "000-1031"},
        }.items():
            cls.stored[key] = Partner.create(dict(vals, name="Kiosco Pegado V103 %s" % key,
                                                  customer_rank=1))
        cls.other_line = Partner.create({"name": "Kiosco Otra Linea V103",
                                         "phone": "2942 00-1031", "customer_rank": 1})
        cls.learned = Partner.create({"name": "Kiosco Aprendido V103", "customer_rank": 1})
        cls.env["distri.payer.identity"].sudo().create(
            {"kind": "phone", "value": "5492990001031", "partner_id": cls.learned.id})
        cls.generic = Partner._distri_generic_partner_for(False)

    def test_the_old_search_missed_it(self):
        Partner = self.env["res.partner"]
        old = Partner.search([("phone_mobile_search", "ilike", "5492990001031")])
        self.assertNotIn(self.stored["spaces"], old)
        self.assertNotIn(self.stored["digits"], old)

    def test_pasted_number_finds_every_format(self):
        found = self.env["res.partner"]._distri_buscar_clientes(PASTED, limit=20)
        for key, partner in self.stored.items():
            self.assertIn(partner, found, key)
        self.assertIn(self.learned, found, "learned payer phone")
        self.assertNotIn(self.other_line, found, "same last digits, another area")
        self.assertNotIn(self.generic, found)
        # the same number first, the stored one without area code last
        self.assertEqual(found[-1:], self.stored["no_area"])

    def test_typed_and_name_search_formats(self):
        Partner = self.env["res.partner"]
        for text in ("2990001031", "0299 15 000 1031", "5492990001031"):
            self.assertIn(self.stored["spaces"], Partner._distri_buscar_clientes(text, limit=20),
                          text)
        found = Partner.with_context(distri_search_phone=True).name_search(PASTED, limit=50)
        self.assertIn(self.stored["spaces"].id, [pid for pid, _name in found])

    def test_candidates_of_a_web_order_with_the_phone_typed_with_15(self):
        Partner = self.env["res.partner"]
        found = Partner._distri_candidatos_pedido("", "", "0299 15 000-1031")
        self.assertIn(self.stored["plus54"], found)
        self.assertIn(self.stored["whatsapp"], found)


class PegarMixin:

    @classmethod
    def _setup_pegar(cls):
        cls.admin = new_test_user(cls.env, login="admin_pegar_v103", name="Admin Pegar V103",
                                  groups="base.group_user,distri_pedido.grupo_administracion")
        Partner = cls.env["res.partner"]
        cls.customer = Partner.create({"name": "Almacen Pegar V103", "company_type": "company",
                                       "customer_rank": 1})
        cls.holder = Partner.create({"name": "Kiosco Ya Lo Tiene V103", "phone": "299 000-1031",
                                     "city": "Neuquén", "customer_rank": 1})
        cls.product = cls.env["product.product"].create({
            "name": "Producto Pegar V103", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.generic = Partner._distri_generic_partner_for(False)

    def _order(self, partner=None, telefono="299 000-2222"):
        return self.env["sale.order"].sudo().create({
            "partner_id": (partner or self.customer).id,
            "distri_nombre": "Kiosco de la web V103",
            "distri_telefono": telefono,
            "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": 1,
                                           "price_unit": 100.0, "tax_ids": [Command.clear()]})],
        })


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_pegar")
class TestPegarEnElDialogo(PegarMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pegar()

    def _dialog(self, order):
        action = order.with_user(self.admin).action_distri_pegar_numero()
        Wizard = self.env[action["res_model"]].with_user(self.admin).with_context(action["context"])
        return Form(Wizard)

    def test_paste_fills_area_and_number_and_warns(self):
        order = self._order()
        form = self._dialog(order)
        self.assertFalse(form.area, "«Pegar número» starts empty, not with the typed phone")
        form.pegado = "‪" + PASTED + "‬"
        self.assertEqual((form.area, form.number), ("299", "0001031"))
        self.assertEqual(form.preview, "+54 9 299 000-1031")
        self.assertIn("No es el teléfono que escribió el cliente en el pedido", form.pegado_aviso)
        self.assertIn("299 000-2222", form.pegado_aviso)
        self.assertIn(self.holder, form.pegado_cliente_ids, "«Ya tienen este número»")
        self.assertNotIn(self.customer, form.pegado_cliente_ids)
        wizard = form.save()
        wizard.action_save_confirm()
        self.assertEqual(self.customer.distri_whatsapp, "+5492990001031")
        self.assertTrue(self.customer.distri_whatsapp_confirmed)
        self.assertEqual(self.customer.distri_whatsapp_user_id, self.admin)

    def test_same_phone_as_typed_has_no_warning(self):
        order = self._order(telefono="0299 15 000-1031")
        form = self._dialog(order)
        form.pegado = PASTED
        self.assertFalse(form.pegado_aviso)

    def test_unreadable_paste_shows_the_reason(self):
        form = self._dialog(self._order())
        form.pegado = "Juan Pérez"
        self.assertEqual(form.pegado_error, MSG_SIN_NUMERO)
        self.assertFalse(form.area)
        with self.assertRaises(UserError):
            form.save().action_save_confirm()

    def test_old_area_and_number_boxes_still_work(self):
        form = self._dialog(self._order())
        form.area = "0299"
        form.number = "15 0001031"
        self.assertEqual(form.preview, "+54 9 299 000-1031")
        self.assertIn(self.holder, form.pegado_cliente_ids, "the list also for typed boxes")
        form.save().action_save_confirm()
        self.assertEqual(self.customer.distri_whatsapp, "+5492990001031")

    def test_paste_without_onchange_is_still_stored(self):
        order = self._order()
        wizard = self.env["distri.wa.confirm.wizard"].with_user(self.admin).with_context(
            distri_wa_pegar=True).create({"order_id": order.id, "then": "verify",
                                          "pegado": "0299 15 000 1031"})
        wizard.action_save_confirm()
        self.assertEqual(self.customer.distri_whatsapp, "+5492990001031")

    def test_confirmation_dialog_has_the_box(self):
        order = self._order()
        form = Form(self.env["distri.wa.confirm.wizard"].with_user(self.admin).with_context(
            default_order_id=order.id, default_then="confirm"))
        form.pegado = PASTED
        self.assertEqual(form.preview, "+54 9 299 000-1031")

    def test_only_administracion_pastes(self):
        seller = new_test_user(self.env, login="vend_pegar_v103", name="Vend Pegar V103",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(UserError):
            self._order().with_user(seller).action_distri_pegar_numero()

    def test_generic_order_opens_asignar_cliente_with_the_number(self):
        order = self._order(partner=self.generic)
        form = self._dialog(order)
        form.pegado = PASTED
        wizard = form.save()
        action = wizard.action_pegado_asignar()
        asignar = self.env["distri.asignar.cliente"].browse(action["res_id"])
        self.assertEqual(asignar.busqueda, "+54 9 299 000-1031")
        self.assertIn(self.holder, asignar.linea_ids.partner_id)


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_pegar")
class TestPegarEnAsignarCliente(PegarMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pegar()

    def _asignar(self, order):
        action = order.with_user(self.admin).action_distri_asignar_cliente()
        return self.env[action["res_model"]].with_user(self.admin).browse(action["res_id"])

    def test_pasted_number_finds_and_stores_it(self):
        order = self._order(partner=self.generic, telefono="299 000-2222")
        wizard = self._asignar(order)
        with Form(wizard) as form:
            form.pegado = "Kiosco ~ " + PASTED
            self.assertEqual(form.busqueda, "+54 9 299 000-1031")
            self.assertEqual(form.pegado_preview, "+54 9 299 000-1031")
            self.assertIn("No es el teléfono que escribió", form.pegado_aviso)
        wizard.action_pegado_buscar()
        self.assertIn(self.holder, wizard.linea_ids.partner_id)
        self.assertEqual(wizard.partner_id, self.holder, "the only customer with it is chosen")
        wizard.action_asignar()
        self.assertEqual(order.partner_id, self.holder)
        self.assertEqual(self.holder.distri_whatsapp, "+5492990001031")
        self.assertTrue(self.holder.distri_whatsapp_confirmed)

    def test_not_stored_when_unticked(self):
        order = self._order(partner=self.generic)
        wizard = self._asignar(order)
        wizard.write({"pegado": PASTED, "pegado_guardar": False})
        wizard.action_pegado_buscar()
        wizard.action_asignar()
        self.assertEqual(order.partner_id, self.holder)
        self.assertFalse(self.holder.distri_whatsapp)


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_copiar")
class TestCopiarCodigo(PegarMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_pegar()

    def test_code_line_value(self):
        order = self._order()
        self.assertFalse(order.distri_wa_code_line)
        order.distri_wa_code = "7K3P"
        self.assertEqual(order.distri_wa_code_line, "Pedido %s · código 7K3P" % order.name)

    def _arch(self, xmlid, view_type):
        view = self.env.ref(xmlid)
        arch = self.env["sale.order"].with_user(self.admin).get_view(view.id, view_type)["arch"]
        return etree.fromstring(arch)

    def test_copy_button_where_administracion_reviews(self):
        for xmlid, view_type in (("distri_pedido.view_order_kanban_revisar", "kanban"),
                                 ("distri_pedido.view_order_list_revisar", "list"),
                                 ("sale.view_order_form", "form")):
            arch = self._arch(xmlid, view_type)
            fields_ = arch.xpath("//field[@name='distri_wa_code_line'][@widget='distri_copiar']")
            self.assertTrue(fields_, xmlid)
            buttons = arch.xpath("//button[@name='action_distri_pegar_numero']")
            self.assertTrue(buttons, xmlid)

    def test_widget_is_in_the_backend_bundle(self):
        from odoo.modules.module import get_manifest
        assets = get_manifest("distri_pedido")["assets"]["web.assets_backend"]
        self.assertIn("distri_pedido/static/src/js/copiar_codigo.js", assets)
        self.assertIn("distri_pedido/static/src/js/copiar_codigo.xml", assets)
        from odoo.tools.misc import file_open
        with file_open("distri_pedido/static/src/js/copiar_codigo.xml") as handle:
            template = handle.read()
        self.assertIn("Copiar código", template)
        self.assertIn("user-select-all", template, "visible fallback: the text selects whole")
        with file_open("distri_pedido/static/src/js/copiar_codigo.js") as handle:
            script = handle.read()
        self.assertIn('registry.category("fields").add("distri_copiar"', script)
        self.assertIn("execCommand", script)
