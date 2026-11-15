"""The customer's WhatsApp: canonical format, the confirmation wizard and the
«Clientes sin WhatsApp» list (models/whatsapp_cliente.py)."""
from odoo import Command
from odoo.exceptions import UserError, ValidationError
from odoo.tests import Form, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.pedido_whatsapp import (
    PARAM_REQUIRE, ar_mobile, ar_national, normalize_whatsapp, phone_key, wa_digits,
)

WIZARD = "distri.wa.confirm.wizard"


@tagged("post_install", "-at_install", "distri_pedido_whatsapp")
class TestWhatsappFormat(TransactionCase):

    def test_ar_mobile(self):
        self.assertEqual(ar_mobile("299", "4222444"), "+5492994222444")
        # the 0 of the area and the 15 of the number go away
        self.assertEqual(ar_mobile("0299", "15 422-2444"), "+5492994222444")
        self.assertEqual(ar_mobile("11", "15 1234-5678"), "+5491112345678")
        self.assertEqual(ar_mobile("2942", "123456"), "+5492942123456")
        for area, number in (("299", "42224"), ("", "4222444"), ("299", ""),
                             ("29942", "12345"), ("299", "42224449")):
            with self.assertRaises(ValueError):
                ar_mobile(area, number)

    def test_wa_digits_drops_the_15(self):
        self.assertEqual(wa_digits("299 555-1234"), "5492995551234")
        self.assertEqual(wa_digits("+542995350450"), "5492995350450")
        for value in ("0299 15 422-2444", "+54 9 299 15 4222444", "29915 4222444",
                      "+5492994222444", "5492994222444", "(0299) 422-2444"):
            self.assertEqual(wa_digits(value), "5492994222444", value)
            self.assertEqual(phone_key(value), "2994222444", value)
        # ambiguous: "15" after 2 and after 4 digits; left as it was
        self.assertEqual(ar_national("11 15 1515 1234"), "")
        self.assertEqual(wa_digits("11 15 1515 1234"), "549111515151234")
        # another country written with its +
        self.assertEqual(wa_digits("+56 9 1234 5678"), "56912345678")
        self.assertEqual(wa_digits("123"), "")

    def test_normalize(self):
        self.assertEqual(normalize_whatsapp("0299 15 422-2444"), "+5492994222444")
        self.assertEqual(normalize_whatsapp("+56 9 1234 5678"), "+56912345678")
        self.assertFalse(normalize_whatsapp("  "))
        for value in ("11 15 1515 1234", "4222444", "299 422"):
            with self.assertRaises(ValueError):
                normalize_whatsapp(value)

    def test_partner_stores_the_canonical_number(self):
        Partner = self.env["res.partner"]
        partner = Partner.create({"name": "Formato WA", "distri_whatsapp": "0299 15 422-2444"})
        self.assertEqual(partner.distri_whatsapp, "+5492994222444")
        partner.distri_whatsapp = "+54 9 299 15 5350450"
        self.assertEqual(partner.distri_whatsapp, "+5492995350450")
        partner.distri_whatsapp = False
        self.assertFalse(partner.distri_whatsapp)
        with self.assertRaisesRegex(ValidationError, "sin el 15"):
            partner.distri_whatsapp = "11 15 1515 1234"
        with self.assertRaises(ValidationError):
            Partner.create({"name": "Corto WA", "distri_whatsapp": "4222444"})


@tagged("post_install", "-at_install", "distri_pedido_whatsapp")
class TestWhatsappConfirmWizard(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product = cls.env["product.product"].create({
            "name": "Producto WA wizard", "list_price": 1000, "sale_ok": True,
            "type": "consu", "taxes_id": [Command.clear()]})
        cls.user = new_test_user(
            cls.env, login="admin_wa_wizard", name="Administración Wizard",
            groups="base.group_user,distri_pedido.grupo_administracion")
        cls.customer = cls.env["res.partner"].create({
            "name": "Almacén Sin WA", "company_type": "company", "phone": "0299 15 422-2444"})
        cls.params = cls.env["ir.config_parameter"].sudo()
        cls.params.set_param(PARAM_REQUIRE, "0")

    def _order(self, partner=None, state="staff"):
        order = self.env["sale.order"].create({
            "partner_id": (partner or self.customer).id,
            "order_line": [Command.create({"product_id": self.product.id,
                                           "product_uom_qty": 1, "price_unit": 1000})],
        })
        order.distri_wa_state = state
        return order.with_user(self.user)

    def _ask(self, order):
        return order.with_context(distri_wa_ask=True).action_confirm()

    def _wizard(self, action, **values):
        self.assertEqual(action["res_model"], WIZARD)
        return self.env[WIZARD].with_user(self.user).with_context(action["context"]).create(values)

    def test_only_interactive_confirmation_asks(self):
        order = self._order()
        action = self._ask(order)
        self.assertEqual(action["res_model"], WIZARD)
        self.assertEqual(order.state, "draft")
        # code that confirms (armado, counter, imports, crons) never gets it
        other = self._order()
        self.assertTrue(other.action_confirm())
        self.assertEqual(other.state, "sale")
        # backend quotations ask too
        self.assertEqual(self._ask(self._order(state=False))["res_model"], WIZARD)

    def test_save_and_confirm(self):
        order = self._order()
        action = self._ask(order)
        form = Form(self.env[WIZARD].with_user(self.user).with_context(action["context"]))
        # the phone on file is offered, already without 0 and 15
        self.assertEqual((form.area, form.number), ("299", "4222444"))
        self.assertEqual(form.preview, "+54 9 299 422-2444")
        self.assertTrue(form.allow_skip)
        form.area = "0298"
        form.number = "15 4111222"
        self.assertEqual((form.area, form.number), ("298", "4111222"))
        self.assertEqual(form.preview, "+54 9 298 411-1222")
        form.number = "41112"
        self.assertTrue(form.error)
        self.assertFalse(form.preview)
        form.number = "4111222"
        wizard = form.save()
        test = wizard.action_test()
        self.assertEqual(test["type"], "ir.actions.act_url")
        self.assertEqual(test["url"], "https://wa.me/5492984111222")
        self.assertEqual(test["target"], "new")
        self.assertFalse(test.get("close"), "the wizard stays open")
        self.assertEqual(order.state, "draft")
        result = wizard.action_save_confirm()
        self.assertEqual(result["type"], "ir.actions.act_window_close", "no loop")
        self.assertEqual(order.state, "sale")
        self.assertEqual(self.customer.distri_whatsapp, "+5492984111222")
        self.assertIn("WhatsApp cargado por Administración Wizard", self.customer.message_ids[0].body)
        # the next order of the same customer is not asked
        self.assertTrue(self._ask(self._order()))

    def test_save_rejects_a_wrong_number(self):
        order = self._order()
        wizard = self._wizard(self._ask(order), area="299", number="42224")
        with self.assertRaises(UserError):
            wizard.action_save_confirm()
        with self.assertRaises(UserError):
            wizard.action_test()
        self.assertEqual(order.state, "draft")

    def test_skip_confirms_and_leaves_a_note(self):
        order = self._order()
        wizard = self._wizard(self._ask(order))
        result = wizard.action_skip()
        self.assertEqual(result["type"], "ir.actions.act_window_close")
        self.assertEqual(order.state, "sale")
        self.assertFalse(self.customer.distri_whatsapp)
        self.assertIn("Confirmado sin WhatsApp del cliente por Administración Wizard",
                      "\n".join(order.message_ids.mapped("body")))

    def test_switch_on_has_no_skip(self):
        self.params.set_param(PARAM_REQUIRE, "1")
        order = self._order()
        wizard = self._wizard(self._ask(order))
        self.assertFalse(wizard.allow_skip)
        with self.assertRaises(UserError):
            wizard.action_skip()
        self.assertEqual(order.state, "draft")
        # code that confirms keeps today's hard requirement
        with self.assertRaisesRegex(UserError, "WhatsApp"):
            self._order(partner=self.env["res.partner"].create({"name": "Sin nada"})
                        ).action_confirm()
        wizard.write({"area": "299", "number": "4222444"})
        wizard.action_save_confirm()
        self.assertEqual(order.state, "sale")

    def test_web_customer_orders_are_not_asked(self):
        for state in ("direct", "received", "manual"):
            order = self._order(state=state)
            self.assertTrue(self._ask(order), state)
            self.assertEqual(order.state, "sale")
        waiting = self._order(state="waiting")
        with self.assertRaisesRegex(UserError, "espera el WhatsApp"):
            self._ask(waiting)

    def test_generic_order_saves_the_typed_phone(self):
        generic = self.env["res.partner"]._distri_generic_partner_for(False)
        order = self._order(partner=generic)
        order.sudo().write({"distri_nombre": "Kiosco Genérico", "distri_telefono": False})
        wizard = self._wizard(self._ask(order), area="299", number="15 4222444")
        self.assertEqual(wizard.customer_name, "Kiosco Genérico")
        wizard.action_save_confirm()
        self.assertEqual(order.distri_telefono, "+5492994222444")
        self.assertEqual(order.state, "sale")

    def test_several_orders_at_once(self):
        first, second = self._order(), self._order()
        both = first | second
        self.assertTrue(self._ask(both))
        self.assertEqual(set(both.mapped("state")), {"sale"})
        for order in both:
            self.assertIn("confirmación de varios pedidos", order.message_ids[0].body)
        # with the switch on staff orders without a number are refused
        self.params.set_param(PARAM_REQUIRE, "1")
        nobody = self.env["res.partner"].create({"name": "Lote sin WA"})
        with self.assertRaisesRegex(UserError, "WhatsApp"):
            self._ask(self._order(partner=nobody) | self._order(partner=nobody))

    def test_list_of_customers_without_whatsapp(self):
        self._order().action_confirm()
        self._order().action_confirm()
        rich = self.env["res.partner"].create({"name": "Con WA", "distri_whatsapp": "2994000000"})
        self._order(partner=rich).action_confirm()
        Rows = self.env["distri.cliente.sin.whatsapp"].with_user(self.user)
        rows = Rows.search([("distri_whatsapp", "=", False)])
        row = rows.filtered(lambda r: r.partner_id == self.customer)
        self.assertEqual(row.order_count, 2)
        self.assertEqual(row.amount_total, 2000)
        self.assertNotIn(rich, rows.partner_id)
        with self.assertRaises(ValidationError):
            row.distri_whatsapp = "11 15 1515 1234"
        row.distri_whatsapp = "0299 15 422-2444"
        self.assertEqual(self.customer.distri_whatsapp, "+5492994222444")
        self.assertNotIn(self.customer, Rows.search([("distri_whatsapp", "=", False)]).partner_id)
