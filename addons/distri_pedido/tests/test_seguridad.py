"""The three fixes of 19.0.1.29.0: rejected app changes are never lost,
/pedido/enviar is limited, validated and token-checked, and the Mercado
Pago webhook needs its secret. Synthetic data only."""
import hashlib
import hmac
import json
import uuid
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.mercado_pago import (
    PARAM_ENABLED, PARAM_TOKEN, PARAM_WEBHOOK_SECRET,
)
from odoo.addons.distri_pedido.models.agil_tienda import PARAM_CATALOGO_AGIL
from odoo.addons.distri_pedido.tests.common_pedido import (
    PedidoAbiertoMixin, page_token, page_tokens, post_pedido,
)
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


def _uuid():
    return str(uuid.uuid4())


@tagged("post_install", "-at_install", "distri_seguridad")
class TestRejectedChanges(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.Rejection = cls.env["distri.sync.rejection"].sudo()

    def _rejected_visit(self):
        order = self._order(self.partner_a, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        # «Transfiere después» no longer exists: rejected for good, with cash in it
        visit = self._visit(stop, cash=500, transfer="later")
        event = self._event(route, "visit", visit)
        res = self._sync([event])
        self.assertEqual(res[0]["status"], "error")
        self.assertFalse(res[0]["retry"])
        return route, stop, visit, event

    def test_rejection_is_kept_with_its_payload(self):
        route, stop, visit, event = self._rejected_visit()
        rec = self.Rejection.search([("uuid", "=", event["uuid"])])
        self.assertEqual(rec.state, "open")
        self.assertEqual(rec.route_id, route)
        self.assertEqual(rec.stop_id, stop)
        self.assertEqual(rec.user_id, self.driver)
        self.assertEqual(rec.cash, 500)
        self.assertIn("ya no existe", rec.error)
        self.assertEqual(json.loads(rec.payload)["visit_uuid"], visit["visit_uuid"])
        self.assertEqual(route.rejection_open_count, 1)
        self.assertEqual(stop.cash_amount, 0, "nothing was applied")
        self.assertIn(rec.uuid, [r["uuid"] for r in route._app_payload()["rejections"]])
        # the same POST again: still one record
        self._sync([event])
        self.assertEqual(self.Rejection.search_count([("uuid", "=", event["uuid"])]), 1)

    def test_settlement_waits_for_rejected_changes(self):
        route, stop, visit, event = self._rejected_visit()
        self._sync([self._event(route, "visit", dict(visit, transfer="no"),
                                captured="2026-09-26T15:05:00.000Z")])
        self._sync([self._event(route, "close", {})])
        route.cash_counted = 500
        route.journal_id = self.company_data["default_journal_cash"]
        with self.assertRaises(UserError) as error:
            route.action_settle()
        self.assertIn("Cambios rechazados", str(error.exception))
        rec = self.Rejection.search([("uuid", "=", event["uuid"])])
        with self.assertRaises(UserError):
            rec.action_resolve()  # without a resolution
        rec.resolution = "El repartidor lo volvió a cargar a mano en la visita."
        rec.action_resolve()
        self.assertEqual(rec.state, "resolved")
        route.action_settle()
        self.assertEqual(route.state, "settled")

    def test_corrected_resend_closes_it_and_counts_once(self):
        route, stop, visit, event = self._rejected_visit()
        corrected = self._event(route, "visit", dict(visit, transfer="no"),
                                captured="2026-09-26T15:05:00.000Z")
        corrected["resends"] = event["uuid"]
        res = self._sync([corrected])
        self.assertEqual(res[0]["status"], "ok", res)
        rec = self.Rejection.search([("uuid", "=", event["uuid"])])
        self.assertEqual(rec.state, "resent")
        self.assertEqual(rec.last_uuid, corrected["uuid"])
        self.assertEqual(stop.cash_amount, 500)
        # the phone retries the same resend: a duplicate, nothing changes
        again = self._sync([corrected])
        self.assertEqual(again[0]["status"], "duplicate")
        self.assertEqual(route.cash_collected, 500)
        self.assertEqual(route.rejection_open_count, 0)

    def test_rejected_resend_updates_the_same_record(self):
        route, stop, visit, event = self._rejected_visit()
        retry = self._event(route, "visit", dict(visit, transfer="later"),
                            captured="2026-09-26T15:05:00.000Z")
        retry["resends"] = event["uuid"]
        self._sync([retry])
        recs = self.Rejection.search([("route_id", "=", route.id)])
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs.attempts, 2)
        self.assertEqual(recs.last_uuid, retry["uuid"])
        self.assertEqual(recs.state, "open")

    def test_a_close_that_goes_through_closes_the_rejected_close(self):
        order = self._order(self.partner_a, self.product_a)
        route = self._route(order)
        res = self._sync([self._event(route, "close", {})])  # a visit without state
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual(route.rejection_open_count, 1)
        self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=1000))])
        self._sync([self._event(route, "close", {})])
        self.assertEqual(route.state, "closed")
        self.assertEqual(route.rejection_open_count, 0)

    def test_only_administration_resolves(self):
        route, stop, visit, event = self._rejected_visit()
        rec = self.Rejection.search([("uuid", "=", event["uuid"])])
        rec.resolution = "x"
        with self.assertRaises(UserError):
            rec.with_user(self.driver).action_resolve()


@tagged("post_install", "-at_install", "distri_seguridad")
class TestPedidoEnviar(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 19.0.1.91.0: one unit reaches the minimum purchase (COMPRA_MINIMA)
        Tmpl = cls.env["product.template"]
        cls.plantilla = Tmpl.create({
            "name": "Producto Prueba Seguridad", "list_price": 70000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.barata = Tmpl.create({
            "name": "Producto Prueba Barato", "list_price": 1000.0, "is_published": True,
            "sale_ok": True, "type": "consu", "taxes_id": [Command.clear()]})
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_CATALOGO_AGIL, "0")

    def _post(self, data, token=True):
        data = dict(data)
        if token:
            data["csrf_token"] = page_token(self)
        return self.url_open("/pedido/enviar", data=data)

    def _data(self, renglones):
        return {"renglones": renglones if isinstance(renglones, str) else json.dumps(renglones),
                "nombre": "Kiosco Prueba Seguridad", "telefono": "2995550000",
                "domicilio": "Calle de ejemplo 1", "entrega": "domicilio"}

    def _orders(self):
        return self.env["sale.order"].sudo().search_count(
            [("distri_nombre", "=", "Kiosco Prueba Seguridad")])

    def test_malformed_input_is_a_clean_400(self):
        for bad in ("{no es json", '{"id": 1}', '[1, 2]', '[{"id": "x", "cantidad": 1}]',
                    '[{"id": 5, "cantidad": "NaN"}]', '[{"id": 5, "cantidad": -1}]'):
            res = self._post(self._data(bad))
            self.assertEqual(res.status_code, 400, bad)
        self.assertEqual(self._orders(), 0)

    def test_caps_on_lines_and_quantities(self):
        many = [{"id": self.plantilla.id, "cantidad": 1}] * 301
        self.assertEqual(self._post(self._data(many)).status_code, 400)
        huge = [{"id": self.plantilla.id, "cantidad": 10001}]
        res = self._post(self._data(huge))
        self.assertEqual(res.status_code, 400)
        self.assertIn("demasiado grande", res.text)
        oversized = dict(self._data([{"id": self.plantilla.id, "cantidad": 1}]),
                         relleno="x" * (300 * 1024))
        self.assertEqual(self._post(oversized).status_code, 413)
        self.assertEqual(self._orders(), 0)

    def test_without_the_page_token_the_cart_comes_back(self):
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 3}]), token=False)
        self.assertEqual(res.status_code, 403)
        self.assertIn("La página había vencido", res.text)
        self.assertIn("Kiosco Prueba Seguridad", res.text, "what was typed is kept")
        self.assertEqual(self._orders(), 0)
        bad = dict(self._data([{"id": self.plantilla.id, "cantidad": 3}]), csrf_token="falso")
        self.assertEqual(self.url_open("/pedido/enviar", data=bad).status_code, 403)
        # with the token of the page it goes through
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 3}]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._orders(), 1)

    def _data_retira(self, renglones):
        return dict(self._data(renglones), entrega="retira", domicilio="")

    def _last_order(self):
        return self.env["sale.order"].sudo().search(
            [("distri_nombre", "=", "Kiosco Prueba Seguridad")], order="id desc", limit=1)

    def test_minimum_purchase_for_delivery_and_pickup(self):
        """19.0.1.91.0 (E2E-A A1): the minimum is checked by the server, for
        home delivery and for pickup alike."""
        for data in (self._data([{"id": self.barata.id, "cantidad": 6}]),
                     self._data_retira([{"id": self.barata.id, "cantidad": 6}])):
            res = self._post(data)
            self.assertEqual(res.status_code, 400)
            self.assertIn("La compra mínima por la página es de $ 70.000", res.text)
            self.assertIn("este pedido suma $ 6.000", res.text)
            self.assertIn("en el local", res.text)
        self.assertEqual(self._orders(), 0)
        res = self._post(self._data_retira([{"id": self.barata.id, "cantidad": 70}]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._orders(), 1)
        self.assertEqual(self._last_order().distri_entrega, "retira")

    def test_minimum_purchase_on_the_token_change(self):
        """19.0.1.91.0 (E2E-A A1): changing an order by its link can't take
        it below the minimum either; the order stays as it was."""
        self.assertEqual(self._post(self._data(
            [{"id": self.barata.id, "cantidad": 80}])).status_code, 200)
        order = self._last_order()
        order._portal_ensure_token()
        before = order.amount_untaxed
        res = self._post(dict(self._data([{"id": self.barata.id, "cantidad": 28}]),
                              token=order.access_token))
        self.assertEqual(res.status_code, 400)
        self.assertIn("La compra mínima", res.text)
        order.invalidate_recordset()
        self.assertEqual(order.amount_untaxed, before)
        self.assertEqual(order.order_line.product_uom_qty, 80)
        res = self._post(dict(self._data([{"id": self.barata.id, "cantidad": 75}]),
                              token=order.access_token))
        self.assertEqual(res.status_code, 200)
        order.invalidate_recordset()
        self.assertEqual(order.order_line.product_uom_qty, 75)

    def test_backend_quotation_below_the_minimum_is_allowed(self):
        """The minimum is for /pedido only: administración still loads a small
        purchase as a quotation from the backend."""
        order = self.env["sale.order"].create({
            "partner_id": self.env["res.partner"].create({"name": "Mostrador Prueba"}).id,
            "order_line": [Command.create({"product_id": self.barata.product_variant_id.id,
                                           "product_uom_qty": 1})]})
        self.assertEqual(order.amount_untaxed, 1000.0)

    def test_only_the_products_of_the_catalog(self):
        """19.0.1.91.0 (E2E-A A2): services, unsellable, unpublished, archived
        products and the ones of the other catalog are never ordered."""
        Tmpl = self.env["product.template"]
        base = {"list_price": 90000.0, "is_published": True, "sale_ok": True, "type": "consu"}
        rejected = Tmpl.create([
            dict(base, name="Envío Prueba Seguridad", type="service"),
            dict(base, name="No Vendible Prueba Seguridad", sale_ok=False),
            dict(base, name="No Publicado Prueba Seguridad", is_published=False),
            dict(base, name="Archivado Prueba Seguridad", active=False),
            dict(base, name="Temporal Prueba Seguridad", distri_agil_temporal=True),
        ])
        for tmpl in rejected:
            res = self._post(self._data([{"id": tmpl.id, "cantidad": 1}]))
            # 19.0.1.93.0 (C11): told, not a silent redirect
            self.assertEqual(res.status_code, 400, tmpl.name)
            self.assertIn("ya no están en el catálogo", res.text, tmpl.name)
        self.assertEqual(self._orders(), 0)
        # the valid lines go through, the others are left out
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 1}]
                                    + [{"id": t.id, "cantidad": 1} for t in rejected]))
        self.assertEqual(res.status_code, 200)
        self.assertIn("No agregamos", res.text)
        self.assertIn("Archivado Prueba Seguridad", res.text)
        self.assertEqual(self._last_order().order_line.product_id.product_tmpl_id,
                         self.plantilla)
        # the other way round with the AgilPedido catalog on
        self.env["ir.config_parameter"].sudo().set_param(PARAM_CATALOGO_AGIL, "1")
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 1}]))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self._orders(), 1)

    def test_quantities_are_whole_units(self):
        """19.0.1.91.0 (E2E-A A3): a fraction is rounded to whole units and a
        line that ends at 0 is dropped: an order of $0 is impossible."""
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 0.001}]))
        self.assertTrue(res.url.endswith("/pedido"), res.url)
        self.assertEqual(self._orders(), 0)
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 1.6},
                                     {"id": self.barata.id, "cantidad": 0.4}]))
        self.assertEqual(res.status_code, 200)
        order = self._last_order()
        self.assertEqual(order.order_line.mapped("product_uom_qty"), [2.0])
        self.assertEqual(order.amount_untaxed, 140000.0)

    def test_same_form_posted_twice_gives_one_order(self):
        """19.0.1.93.0 (E2E-C C8): a double tap or a browser retry of the same
        form returns the first order; a new page is a new order."""
        csrf, envio = page_tokens(self)
        self.assertTrue(envio)
        data = dict(self._data([{"id": self.plantilla.id, "cantidad": 1}]),
                    csrf_token=csrf, envio=envio)
        first = self.url_open("/pedido/enviar", data=data)
        second = self.url_open("/pedido/enviar", data=data)
        self.assertEqual((first.status_code, second.status_code), (200, 200))
        self.assertEqual(self._orders(), 1)
        order = self._last_order()
        self.assertIn(order.name, second.text)
        self.assertTrue(order.distri_envio_clave)
        res = post_pedido(self, self._data([{"id": self.plantilla.id, "cantidad": 1}]))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self._orders(), 2)

    def test_unknown_delivery_is_a_400(self):
        """19.0.1.93.0 (E2E-C C9): it was a 500."""
        res = self._post(dict(self._data([{"id": self.plantilla.id, "cantidad": 1}]),
                              entrega="helicoptero"))
        self.assertEqual(res.status_code, 400)
        self.assertIn("No entendimos cómo querés recibir el pedido", res.text)
        self.assertEqual(self._orders(), 0)

    def test_change_by_link_tells_the_dropped_products(self):
        """19.0.1.93.0 (E2E-C C11): «Tu pedido» says how many lines were left out."""
        self.assertEqual(self._post(self._data(
            [{"id": self.plantilla.id, "cantidad": 1}])).status_code, 200)
        order = self._last_order()
        order._portal_ensure_token()
        gone = self.env["product.template"].create({
            "name": "Archivado Cambio Seguridad", "list_price": 5.0, "is_published": True,
            "sale_ok": True, "type": "consu", "active": False})
        res = self._post(dict(self._data([{"id": self.plantilla.id, "cantidad": 2},
                                          {"id": gone.id, "cantidad": 1}]),
                              token=order.access_token))
        self.assertEqual(res.status_code, 200)
        self.assertIn("descartados=1", res.url)
        self.assertIn("Un producto ya no está en el catálogo y no se agregó.", res.text)

    def test_rate_limit_per_session_and_ip(self):
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.pedido_limite_sesion_10min", "2")
        for _n in range(2):
            self.assertEqual(self._post(self._data(
                [{"id": self.plantilla.id, "cantidad": 1}])).status_code, 200)
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 1}]))
        self.assertEqual(res.status_code, 429)
        self.assertIn("Esperá unos minutos", res.text)
        self.assertEqual(self._orders(), 2)
        # per IP, whatever the session
        params.set_param("distri_pedido.pedido_limite_sesion_10min", "100")
        params.set_param("distri_pedido.pedido_limite_ip_10min", "3")
        self.opener.cookies.clear()
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 1}]))
        self.assertEqual(res.status_code, 200)
        self.opener.cookies.clear()
        res = self._post(self._data([{"id": self.plantilla.id, "cantidad": 1}]))
        self.assertEqual(res.status_code, 429)
        hits = self.env["distri.rate.hit"].sudo().search([])
        self.assertTrue(hits)
        self.assertFalse(any("127.0.0.1" in h.key for h in hits), "no IP in clear")


@tagged("post_install", "-at_install", "distri_seguridad")
class TestMercadoPagoWebhook(PedidoAbiertoMixin, HttpCase):

    SECRET = "secreto-de-prueba"

    def setUp(self):
        super().setUp()
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_ENABLED, "1")
        params.set_param(PARAM_TOKEN, "TEST-token-de-prueba")
        params.set_param(PARAM_WEBHOOK_SECRET, "")
        self.synced = []
        Payment = type(self.env["distri.mp.payment"])
        test = self

        def fake_sync(model, payment_id, source):
            test.synced.append((payment_id, source))
            return model.browse()
        patcher = patch.object(Payment, "_mp_sync_payment", fake_sync)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _notify(self, payment_id="123456789", signature=None, request_id="req-1", ts="1700000000"):
        headers = {"Content-Type": "application/json", "x-request-id": request_id}
        if signature is not None:
            headers["x-signature"] = "ts=%s,v1=%s" % (ts, signature)
        body = json.dumps({"type": "payment", "data": {"id": payment_id}})
        return self.url_open("/pedido/mp/webhook?type=payment&data.id=%s" % payment_id,
                             data=body, headers=headers)

    def _sign(self, payment_id="123456789", request_id="req-1", ts="1700000000"):
        manifest = "id:%s;request-id:%s;ts:%s;" % (payment_id, request_id, ts)
        return hmac.new(self.SECRET.encode(), manifest.encode(), hashlib.sha256).hexdigest()

    def test_without_secret_it_is_rejected(self):
        res = self._notify(signature=self._sign())
        self.assertEqual(res.status_code, 401)
        self.assertFalse(self.synced)

    def test_bad_signature_is_rejected(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_WEBHOOK_SECRET, self.SECRET)
        self.assertEqual(self._notify(signature="0" * 64).status_code, 401)
        self.assertEqual(self._notify(signature=None).status_code, 401)
        self.assertFalse(self.synced)

    def test_valid_signature_is_processed_from_the_api(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_WEBHOOK_SECRET, self.SECRET)
        res = self._notify(signature=self._sign())
        self.assertEqual(res.status_code, 200)
        # only the id travels: the payment is fetched again from the API
        self.assertEqual(self.synced, [("123456789", "webhook")])
