"""Parcels to the interior (models/encomienda.py) and the customer's
«Soy del interior» form (/pedido)."""
import base64
import json
import unittest

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.encomienda import (
    ACTIVITY_SUMMARY, normalize_dni, read_guide_number,
)
from odoo.addons.distri_pedido.models.ocr_comprobante import ocr_available
from odoo.addons.distri_pedido.tests import receipt_images
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid
from odoo.addons.distri_pedido.tests.common_pedido import post_pedido


def photo_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


class EncomiendaMixin(RepartoMixin):

    @classmethod
    def _setup_encomiendas(cls):
        cls._setup_reparto()
        cls.carrier = cls.env.ref("distri_pedido.carrier_el_petroleo")
        cls.zapala = cls.env.ref("distri_pedido.zona_zapala")
        cls.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.encomienda_pedido_chico", "70000")
        cls.seller = new_test_user(
            cls.env, login="vendedor_encomienda", name="Vendedora Encomienda",
            groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.customer = cls.env["res.partner"].create({
            "name": "Z - ALMACEN LA MESETA", "phone": "2942 401234"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto encomienda", "default_code": "AP-103241", "list_price": 100,
            "type": "consu", "taxes_id": [Command.clear()]})

    @classmethod
    def _parcel_order(cls, qty=1000, price=100.0, partner=None):
        order = cls._order(partner or cls.customer, cls.product, qty=qty, price=price)
        order.user_id = cls.seller
        return order

    def _dispatch(self, route, stop, **kw):
        payload = {"visit_uuid": _uuid(), "stop_id": stop.id, "outcome": "dispatched",
                   "guide_number": "000123-4", "cost": 8500, "packages": 2, "comment": "",
                   "photo": photo_url(receipt_images.carrier_guide("000123-4", 8500))}
        payload.update(kw)
        return self._sync([self._event(route, "parcel", payload)])


@tagged("post_install", "-at_install", "distri_encomiendas")
class TestEncomiendas(EncomiendaMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_encomiendas()

    # ------------------------------------------------------------ the rule

    def test_interior_zone_is_a_parcel_paid_by_distrineuquen(self):
        order = self._parcel_order()
        self.assertEqual(order._distri_route_zone(), self.zapala, "the «Z -» prefix is Zapala")
        self.assertTrue(order.distri_encomienda)
        self.assertEqual(order.distri_carrier_id, self.carrier)
        self.assertEqual(order.distri_envio_paga, "distri")
        # a Neuquén customer is not a parcel
        city = self._order(self.partner_a, self.product)
        self.assertFalse(city.distri_encomienda)
        self.assertFalse(city.distri_envio_paga)

    def test_wholesale_and_small_orders_pay_at_destination(self):
        small = self._parcel_order(qty=10)
        self.assertEqual(small.amount_total, 1000)
        self.assertEqual(small.distri_envio_paga, "customer")
        Pricelist = self.env["product.pricelist"]
        retail_list = Pricelist.create({"name": "General test", "company_id": self.env.company.id})
        wholesale_list = Pricelist.create({
            "name": "Distribuidoras test", "distri_mayorista": True, "company_id": self.env.company.id})
        big = self.env["sale.order"].create({
            "partner_id": self.customer.id, "pricelist_id": retail_list.id,
            "order_line": [Command.create({
                "product_id": self.product.id, "product_uom_qty": 1000, "price_unit": 100.0,
                "tax_ids": [Command.clear()]})]})
        self.assertEqual(big.distri_envio_paga, "distri")
        big.pricelist_id = wholesale_list
        big.order_line.price_unit = 100.0
        self.assertEqual(big.distri_envio_paga, "customer", "a wholesale list pays at destination")

    def test_admin_override_stays_and_goes_back_to_the_rule(self):
        order = self._parcel_order()
        order.action_distri_toggle_envio_paga()
        self.assertEqual(order.distri_envio_paga, "customer")
        self.assertTrue(order.distri_envio_paga_manual)
        # a change of the total does not undo the choice made by hand
        order.order_line.product_uom_qty = 1500
        self.assertEqual(order.distri_envio_paga, "customer")
        # choosing what the rule says puts the order back on the rule
        order.action_distri_toggle_envio_paga()
        self.assertEqual(order.distri_envio_paga, "distri")
        self.assertFalse(order.distri_envio_paga_manual)
        order.order_line.product_uom_qty = 10
        self.assertEqual(order.distri_envio_paga, "customer", "back on the small-order rule")
        # the parcel flag itself, by hand
        order.distri_encomienda = False
        self.assertTrue(order.distri_encomienda_manual)
        self.assertFalse(order.distri_envio_paga)

    def test_only_administration_changes_who_pays(self):
        order = self._parcel_order()
        with self.assertRaises(UserError):
            order.with_user(self.seller).sudo(False).action_distri_toggle_envio_paga()
        self.assertEqual(order.distri_envio_paga, "distri")

    def test_zone_change_follows_open_orders(self):
        order = self._parcel_order()
        self.zapala.carrier_id = False
        self.zapala.encomienda = False
        self.assertFalse(order.distri_encomienda)
        self.zapala.encomienda = True
        self.assertTrue(order.distri_encomienda)

    # ------------------------------------------------------------ the phone

    def test_driver_app_payload(self):
        order = self._parcel_order()
        order.write({"distri_encomienda_nombre": "Rosa Quiroga", "distri_encomienda_dni": "30111222"})
        route = self._route(order)
        stop = route._app_payload()["stops"][0]
        self.assertEqual(stop["parcel"]["carrier"], "El Petróleo S.A.")
        self.assertEqual(stop["parcel"]["payer"], "distri")
        self.assertEqual(stop["parcel"]["payer_label"], "Envío pago por Distrineuquén")
        self.assertEqual(stop["parcel"]["town"], "Zapala")
        self.assertEqual(stop["parcel"]["recipient"], "Rosa Quiroga")
        self.assertEqual(stop["parcel"]["dni"], "30111222")
        self.assertEqual(stop["lines"][0]["product"], "Producto encomienda",
                         "no internal code like [AP-103241] for the driver")

    def test_close_blocked_until_the_guide_is_in(self):
        order = self._parcel_order()
        route = self._route(order)
        stop = route.stop_ids
        # the receipt code flow does not apply to a parcel
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=10))])
        self.assertEqual(res[0]["status"], "error")
        res = self._sync([self._event(route, "close", {})])
        self.assertEqual(res[0]["status"], "error", "a parcel without its guide blocks the close")
        # no photo, no dispatch
        res = self._dispatch(route, stop, photo="")
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("foto de la guía", res[0]["error"])
        # Distrineuquén pays: the cost is mandatory
        res = self._dispatch(route, stop, cost=0)
        self.assertEqual(res[0]["status"], "error")
        res = self._dispatch(route, stop)
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.state, "dispatched")
        self.assertEqual(order.distri_delivery_state, "dispatched")
        res = self._sync([self._event(route, "close", {})])
        self.assertEqual(res[0]["status"], "ok", res)

    def test_guide_photo_missing_blocks_the_close(self):
        order = self._parcel_order()
        route = self._route(order)
        self._dispatch(route, route.stop_ids)
        route.stop_ids.parcel_id.sudo().photo = False
        res = self._sync([self._event(route, "close", {})])
        self.assertEqual(res[0]["status"], "error")
        self.assertIn("Falta la foto de la guía", res[0]["error"])

    def test_shipping_paid_by_distrineuquen_is_an_expense(self):
        order = self._parcel_order()
        other = self._order(self.partner_a, self.product, qty=2)
        route = self._route(order | other)
        s_parcel = route.stop_ids.filtered(lambda s: s.order_id == order)
        s_city = route.stop_ids - s_parcel
        self._sync([self._event(route, "visit", self._visit(s_city, cash=20000))])
        self._dispatch(route, s_parcel, cost=8500)
        parcel = s_parcel.parcel_id
        expense = parcel.expense_id
        self.assertEqual(expense.kind, "parcel")
        self.assertEqual(expense.amount, 8500)
        self.assertTrue(expense.photo, "the guide is the receipt of the expense")
        self.assertEqual(route.expense_total, 8500)
        self.assertEqual(route.cash_expected, 20000 - 8500)
        # an edit of the dispatch updates the same expense
        self._dispatch(route, s_parcel, visit_uuid=s_parcel.visit_uuid, cost=9000, photo="")
        self.assertEqual(parcel.expense_id, expense)
        self.assertEqual(expense.amount, 9000)
        self.assertEqual(len(route.expense_ids), 1)

    def test_customer_pays_at_destination_no_expense(self):
        order = self._parcel_order(qty=10)
        self.assertEqual(order.distri_envio_paga, "customer")
        route = self._route(order)
        res = self._dispatch(route, route.stop_ids, cost=5000)
        self.assertEqual(res[0]["status"], "ok", res)
        parcel = route.stop_ids.parcel_id
        self.assertEqual(parcel.cost, 0, "the driver pays nothing")
        self.assertFalse(parcel.expense_id)
        self.assertFalse(route.expense_ids)
        self.assertIn("Envío a pagar en destino", parcel.message_text)

    def test_seller_notified_with_the_guide(self):
        order = self._parcel_order()
        order.write({"distri_encomienda_nombre": "Rosa Quiroga", "distri_encomienda_dni": "30111222"})
        route = self._route(order)
        self._dispatch(route, route.stop_ids)
        parcel = route.stop_ids.parcel_id
        self.assertEqual(parcel.guide_number, "000123-4")
        self.assertEqual(parcel.carrier_id, self.carrier)
        self.assertEqual(parcel.packages, 2)
        # the guide is kept on the order and on the route
        self.assertEqual(parcel.order_attachment_id.res_model, "sale.order")
        self.assertEqual(parcel.order_attachment_id.res_id, order.id)
        self.assertEqual(parcel.route_attachment_id.res_model, "distri.route")
        self.assertEqual(parcel.route_attachment_id.res_id, route.id)
        # activity for the salesperson with the WhatsApp text
        activity = order.activity_ids.filtered(lambda a: a.summary == ACTIVITY_SUMMARY)
        self.assertEqual(activity.user_id, self.seller)
        self.assertIn("wa.me/549", str(activity.note))
        message = order.message_ids.filtered(lambda m: "Encomienda despachada" in (m.body or ""))
        self.assertEqual(len(message), 1)
        self.assertIn(parcel.order_attachment_id, message.attachment_ids)
        self.assertIn(self.seller.partner_id, message.partner_ids)
        text = parcel.message_text
        self.assertIn("Hola Almacen La Meseta", text)
        self.assertIn("salió hoy por El Petróleo S.A.", text)
        self.assertIn("Guía N° 000123-4", text)
        self.assertIn("terminal de Zapala", text)
        self.assertIn("Envío pago.", text)
        self.assertIn("Retira: Rosa Quiroga · DNI 30111222.", text)
        # re-sending the dispatch does not notify twice
        self._dispatch(route, route.stop_ids, visit_uuid=route.stop_ids.visit_uuid, photo="")
        self.assertEqual(len(order.message_ids.filtered(
            lambda m: "Encomienda despachada" in (m.body or ""))), 1)
        # the salesperson opens WhatsApp from the order: notified
        action = order.with_user(self.seller).action_distri_parcel_whatsapp()
        self.assertTrue(action["url"].startswith("https://wa.me/5492942401234?text="))
        self.assertTrue(parcel.notified)
        self.assertEqual(parcel.notified_by_id, self.seller)
        self.assertFalse(order.activity_ids.filtered(lambda a: a.summary == ACTIVITY_SUMMARY))

    def test_read_guide_number(self):
        self.assertEqual(read_guide_number("EL PETROLEO\nGUIA N° 000123-4\nDestino Zapala"), "000123-4")
        self.assertEqual(read_guide_number("Nro. de guía: 55821"), "55821")
        self.assertEqual(read_guide_number("sin numero"), "")
        self.assertEqual(normalize_dni("30.111.222"), "30111222")
        self.assertEqual(normalize_dni("1234567"), "1234567")
        self.assertEqual(normalize_dni("123456"), "")
        self.assertEqual(normalize_dni("123456789"), "")

    @unittest.skipUnless(ocr_available(), "tesseract is not installed")
    def test_guide_number_read_from_the_photo(self):
        order = self._parcel_order()
        route = self._route(order)
        self._dispatch(route, route.stop_ids, guide_number="",
                       photo=photo_url(receipt_images.carrier_guide("558210", 8500)))
        parcel = route.stop_ids.parcel_id
        self.assertTrue(parcel.guide_review, "no number typed")
        self.env["distri.route.expense"]._cron_read_photos()
        self.assertEqual(parcel.guide_number_read, "558210")
        self.assertEqual(parcel.guide_number, "558210", "the number read fills the empty one")
        self.assertFalse(parcel.guide_review)


@tagged("post_install", "-at_install", "distri_encomiendas")
class TestEncomiendaCliente(EncomiendaMixin, HttpCase):
    """«Soy del interior» on /pedido and the parcel on the order page."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_encomiendas()
        cls.template = cls.env["product.template"].create({
            "name": "Producto web encomienda", "list_price": 1000, "type": "consu",
            "is_published": True, "taxes_id": [Command.clear()]})

    def _post(self, **extra):
        data = {"renglones": json.dumps([{"id": self.template.id, "cantidad": 80}]),
                "nombre": "Almacén del Sur", "telefono": "2942 555111", "domicilio": "",
                "entrega": "domicilio"}
        data.update(extra)
        return post_pedido(self, data)

    def test_interior_form_stores_the_recipient(self):
        res = self._post(encomienda="1", enc_nombre="  Rosa   Quiroga ", enc_dni="30.111.222",
                         enc_localidad="Zapala")
        self.assertEqual(res.status_code, 200)
        order = self.env["sale.order"].search([("distri_nombre", "=", "Almacén del Sur")],
                                              order="id desc", limit=1)
        self.assertTrue(order.distri_encomienda_pedida)
        self.assertEqual(order.distri_encomienda_nombre, "Rosa Quiroga")
        self.assertEqual(order.distri_encomienda_dni, "30111222")
        self.assertEqual(order.distri_encomienda_localidad, "Zapala")
        # the customer's choice makes it a parcel even without an interior zone
        self.assertTrue(order.distri_encomienda)
        self.assertEqual(order.distri_carrier_id, self.carrier, "carrier of the Zapala zone")
        self.assertEqual(order.distri_envio_paga, "distri", "80.000: Distrineuquén pays")
        self.assertIn("Retira en la terminal: Rosa Quiroga, DNI 30111222", order.note)

    def test_interior_form_validates_the_dni(self):
        for dni in ("123456", "123456789", "abc"):
            res = self._post(encomienda="1", enc_nombre="Rosa Quiroga", enc_dni=dni,
                             enc_localidad="Zapala")
            self.assertEqual(res.status_code, 400)
            self.assertIn("7 u 8", res.text)
        res = self._post(encomienda="1", enc_nombre="", enc_dni="30111222", enc_localidad="Zapala")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.env["sale.order"].search_count([("distri_nombre", "=", "Almacén del Sur")]))

    def test_form_shows_the_interior_box(self):
        res = self.url_open("/pedido")
        self.assertIn("Soy del interior", res.text)
        self.assertIn('<option value="Zapala"', res.text)

    def test_order_page_dispatch_and_guide_download(self):
        order = self._parcel_order()
        order._portal_ensure_token()
        token = order.access_token
        order.write({"distri_encomienda_nombre": "Rosa Quiroga", "distri_encomienda_dni": "30111222"})
        page = self.url_open("/pedido/%s" % token)
        self.assertIn("Sale por encomienda", page.text)
        self.assertNotIn("Descargar comprobante de guía", page.text)
        self.assertEqual(self.url_open("/pedido/%s/guia" % token).status_code, 404,
                         "no guide before the dispatch")

        route = self._route(order)
        self._dispatch(route, route.stop_ids)
        page = self.url_open("/pedido/%s" % token)
        self.assertIn("Despachado por encomienda", page.text)
        self.assertIn("El Petróleo S.A.", page.text)
        self.assertIn("000123-4", page.text)
        self.assertIn("Envío pago", page.text)
        self.assertIn("Descargar comprobante de guía", page.text)
        self.assertNotIn("0001/", page.text, "never an invoice number")

        guide = self.url_open("/pedido/%s/guia" % token)
        self.assertEqual(guide.status_code, 200)
        self.assertTrue(guide.content.startswith(b"\x89PNG") or guide.content.startswith(b"\xff\xd8"))
        self.assertIn("attachment", guide.headers.get("Content-Disposition"))
        # another token, or none, gets nothing
        self.assertEqual(self.url_open("/pedido/%s/guia" % ("x" * len(token))).status_code, 404)
        other = self._parcel_order()
        other._portal_ensure_token()
        self.assertEqual(self.url_open("/pedido/%s/guia" % other.access_token).status_code, 404)
