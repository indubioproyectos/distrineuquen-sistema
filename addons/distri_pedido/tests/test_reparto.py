import json
import uuid

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.reparto_cliente import (
    receipt_code_hash, strip_town_prefix, town_from_name,
)


def _uuid():
    return str(uuid.uuid4())


class RepartoMixin:

    @classmethod
    def _setup_reparto(cls):
        cls.driver = new_test_user(
            cls.env, login="repartidor_test", name="Repartidor Test",
            groups="base.group_user,distri_pedido.grupo_reparto",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)],
        )
        cls.other_driver = new_test_user(
            cls.env, login="repartidor_otro", name="Otro Repartidor",
            groups="base.group_user,distri_pedido.grupo_reparto",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)],
        )

    @classmethod
    def _order(cls, partner, product, qty=10, price=100.0):
        company = cls.env.company
        pricelist = cls.env["product.pricelist"].search(
            [("company_id", "=", company.id), ("name", "=", "Reparto test")], limit=1
        ) or cls.env["product.pricelist"].create({
            "name": "Reparto test", "company_id": company.id,
            "currency_id": company.currency_id.id})
        order = cls.env["sale.order"].create({
            "partner_id": partner.id,
            "pricelist_id": pricelist.id,
            "order_line": [Command.create({
                "product_id": product.id,
                "product_uom_qty": qty,
                "price_unit": price,
                "tax_ids": [Command.clear()],
            })],
        })
        order.action_confirm()
        order.sudo().write({"distri_armado": "armado", "distri_bultos": 2})
        return order

    # 19.0.1.91.0: visits, expenses, receipts, parcels and the close need the
    # route on the road. Classes that sync those events right away set this,
    # and _route leaves the route «En reparto» (as after «Salir a repartir»)
    ROUTE_ON_ROAD = False

    def _route(self, orders, driver=None, on_road=None):
        route = self.env["distri.route"].sudo().create({
            "driver_id": (driver or self.driver).id,
            "date": fields.Date.today(),
        })
        route._add_orders(orders)
        if self.ROUTE_ON_ROAD if on_road is None else on_road:
            self._on_road(route)
        return route

    def _on_road(self, route):
        """The route as it is after «Salir a repartir», without the load
        ticks and the double check (see _depart for the real flow)."""
        route.sudo().write({"state": "on_route", "departed_at": fields.Datetime.now()})
        return route

    def _event(self, route, kind, payload, captured="2026-09-26T15:00:00.000Z"):
        return {"uuid": _uuid(), "type": kind, "route_id": route.id,
                "captured_at": captured, "payload": payload}

    def _visit(self, stop, **kw):
        payload = {
            "visit_uuid": _uuid(), "stop_id": stop.id, "outcome": "delivered",
            "returns_answer": "none", "returns": [], "code": "", "without_code": True,
            "cash": 0, "transfer": "no", "sent_to_vendor": None, "no_payment": False,
            "comment": "",
        }
        payload.update(kw)
        return payload

    def _sync(self, events, user=None):
        return self.env["distri.sync.event"].sudo().process_batch(user or self.driver, events)

    def _depart(self, route, **payload):
        """«Salir a repartir» the way it works now (despacho.py): every order
        ticked by the driver and by the depot, then the departure."""
        driver = route.driver_id
        self._sync([self._event(route, "load", {"stop_id": s.id, "loaded": True})
                    for s in route.stop_ids], user=driver)
        route.stop_ids.sudo().action_dispatch_check()
        res = self._sync([self._event(route, "depart", payload)], user=driver)
        self.assertEqual(res[0]["status"], "ok", res)
        return res


@tagged("post_install", "-at_install", "distri_reparto")
class TestReparto(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.product_a.invoice_policy = "order"
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.partner = cls.partner_a
        cls.order = cls._order(cls.partner, cls.product_a)

    # ----------------------------------------------------------- names

    def test_town_prefix(self):
        self.assertEqual(strip_town_prefix("X - LUCIA PRUEBA- KIOSCO EL REY"), "LUCIA PRUEBA- KIOSCO EL REY")
        self.assertEqual(town_from_name("ZC - ALGO"), "Cutral Có")
        self.assertEqual(town_from_name("Z - ALGO"), "Zapala")
        self.assertEqual(town_from_name("KIOSCO DIMAR"), "Neuquén")
        self.partner.name = "Y - KIOSCO DIMAR"
        self.assertEqual(self.partner._distri_app_name(), "Kiosco Dimar")

    # ---------------------------------------------------- receipt code

    def test_receipt_code_hash_only(self):
        code = self.order._distri_generate_receipt_code()
        self.assertRegex(code, r"^\d{4}$")
        order = self.order.sudo()
        self.assertNotIn(code, (order.distri_receipt_hash, order.distri_receipt_salt))
        self.assertEqual(order.distri_receipt_hash, receipt_code_hash(order.distri_receipt_salt, code))
        self.assertTrue(self.order._distri_check_receipt_code(code))
        wrong = "%04d" % ((int(code) + 1) % 10000)
        self.assertFalse(self.order._distri_check_receipt_code(wrong))
        self.assertFalse(self.order._distri_check_receipt_code(""))

        route = self._route(self.order)
        stop_data = route._app_payload()["stops"][0]
        self.assertEqual(stop_data["code_hash"], order.distri_receipt_hash)
        self.assertNotIn("code", stop_data)
        flat = json.dumps(stop_data)
        self.assertNotIn('"%s"' % code, flat)

    def test_visit_with_code(self):
        code = self.order._distri_generate_receipt_code()
        route = self._route(self.order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            stop, code=code, without_code=False, cash=1000))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(stop.delivery_state, "delivered_code")
        self.assertFalse(stop.code_mismatch)

        # a code that no longer matches (regenerated after the download)
        other = self._order(self.partner, self.product_a)
        other_code = other._distri_generate_receipt_code()
        route._add_orders(other)
        stop2 = route.stop_ids.filtered(lambda s: s.order_id == other)
        wrong = "%04d" % ((int(other_code) + 1) % 10000)
        self._sync([self._event(route, "visit", self._visit(
            stop2, code=wrong, without_code=False, no_payment=True))])
        self.assertEqual(stop2.delivery_state, "delivered_no_code")
        self.assertTrue(stop2.code_mismatch)

    # ------------------------------------------------ idempotent sync

    def test_sync_idempotent_and_upsert(self):
        route = self._route(self.order)
        stop = route.stop_ids
        visit = self._visit(stop, cash=500)
        event = self._event(route, "visit", visit)

        first = self._sync([event])
        again = self._sync([event])
        self.assertEqual(first[0]["status"], "ok", first)
        self.assertEqual(again[0]["status"], "duplicate")
        self.assertEqual(self.env["distri.sync.event"].sudo().search_count(
            [("uuid", "=", event["uuid"])]), 1)
        self.assertEqual(stop.state, "paid")
        self.assertEqual(stop.cash_amount, 500)
        self.assertTrue(stop.visit_received_at)
        self.assertEqual(stop.visit_captured_at, fields.Datetime.to_datetime("2026-09-26 15:00:00"))

        # the driver edits the same visit: new event, same visit uuid
        edit = dict(visit, cash=0, transfer="yes", transfer_amount=1000,
                    receipt_answer="not_yet")
        self._sync([self._event(route, "visit", edit, captured="2026-09-26T15:05:00Z")])
        self.assertEqual(stop.state, "paid")
        self.assertEqual(stop.cash_amount, 0)
        self.assertEqual(len(stop.env["distri.promised.transfer"].sudo().search(
            [("stop_id", "=", stop.id)])), 1)

        # an older edit arriving late does not overwrite the newer one
        old = dict(visit, cash=900)
        self._sync([self._event(route, "visit", old, captured="2026-09-26T14:00:00Z")])
        self.assertEqual(stop.cash_amount, 0)

        # a transfer needs the driver's answer about the receipt
        res = self._sync([self._event(route, "visit", dict(visit, transfer="yes",
                                                           transfer_amount=1000),
                                      captured="2026-09-26T15:10:00Z")])
        self.assertEqual(res[0]["status"], "error")
        self.assertFalse(res[0]["retry"])

    def test_route_written_only_by_its_driver(self):
        route = self._route(self.order)
        res = self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=10))],
                         user=self.other_driver)
        self.assertEqual(res[0]["status"], "error")
        self.assertEqual(route.stop_ids.state, "pending")

    def test_close_requires_states_and_reasons(self):
        other = self._order(self.partner, self.product_a)
        route = self._route(self.order | other)
        s1, s2 = route.stop_ids
        res = self._sync([self._event(route, "close", {})])
        self.assertEqual(res[0]["status"], "error")
        self._sync([
            self._event(route, "visit", self._visit(s1, no_payment=True)),
            self._event(route, "visit", {"visit_uuid": _uuid(), "stop_id": s2.id,
                                         "outcome": "not_delivered", "reason": ""}),
        ])
        res = self._sync([self._event(route, "close", {})])
        self.assertEqual(res[0]["status"], "error", "missing reason must block the close")
        self._sync([self._event(route, "visit", {"visit_uuid": s2.visit_uuid, "stop_id": s2.id,
                                                 "outcome": "not_delivered", "reason": "Cerrado"},
                                captured="2026-09-26T16:00:00Z")])
        res = self._sync([self._event(route, "close", {})])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(route.state, "closed")
        self.assertEqual(self.order.distri_delivery_state, "delivered")
        self.assertEqual(other.distri_delivery_state, "pending")

    def test_nothing_before_departing(self):
        """19.0.1.91.0 (E2E-A A5): visits, customer data, expenses, receipts,
        parcels and the close need the route on the road; the rejection goes
        back to the app (and to «Cambios rechazados») like any other."""
        route = self._route(self.order, on_road=False)
        stop = route.stop_ids
        events = [
            self._event(route, "visit", self._visit(stop, cash=6000)),
            self._event(route, "customer", {"partner_id": stop.shipping_partner_id.id,
                                            "note": "portón verde"}),
            self._event(route, "expense", {"expense_uuid": _uuid(), "kind": "fuel",
                                           "amount": 100}),
            self._event(route, "receipt", {"stop_id": stop.id, "photo_uuid": _uuid()}),
            self._event(route, "parcel", {"visit_uuid": _uuid(), "stop_id": stop.id}),
            self._event(route, "close", {}),
        ]
        results = self._sync(events)
        for event, result in zip(events, results):
            self.assertEqual(result["status"], "error", (event["type"], result))
            self.assertFalse(result["retry"])
            self.assertIn("Salir a repartir", result["error"])
        self.assertEqual((route.state, stop.state, route.cash_collected), ("draft", "pending", 0))
        self.assertFalse(route.departed_at)
        self.assertEqual(self.env["distri.sync.rejection"].sudo().search_count(
            [("route_id", "=", route.id), ("state", "=", "open")]), len(events))

    def test_offline_batch_departs_then_visits(self):
        """An offline batch still applies in order: the depart first, then
        the visits and the close of the same batch."""
        route = self._route(self.order, on_road=False)
        stop = route.stop_ids
        self._sync([self._event(route, "load", {"stop_id": stop.id, "loaded": True})])
        stop.sudo().action_dispatch_check()
        results = self._sync([
            self._event(route, "depart", {}),
            self._event(route, "visit", self._visit(stop, cash=1000)),
            self._event(route, "close", {}),
        ])
        self.assertEqual([r["status"] for r in results], ["ok"] * 3, results)
        self.assertEqual((route.state, stop.state), ("closed", "paid"))
        self.assertTrue(route.departed_at)

    # ------------------------------------------ cash expected with returns

    def test_cash_expected_with_returns(self):
        route = self._route(self.order)
        stop = route.stop_ids
        self.assertEqual(stop.order_amount, 1000)
        line = self.order.order_line
        visit = self._visit(stop, returns_answer="yes", returns=[
            {"uuid": _uuid(), "line_id": line.id, "qty": 2, "reason": "Envase golpeado"}],
            cash=800)
        self.assertEqual(self._sync([self._event(route, "visit", visit)])[0]["status"], "ok")
        self.assertEqual(stop.returns_amount, 200)
        self.assertEqual(stop.expected_balance, stop.previous_debt + 800)
        self.assertEqual(route.cash_expected, 800)

        # a retry of the same visit does not duplicate the return
        self._sync([self._event(route, "visit", visit, captured="2026-09-26T15:01:00Z")])
        self.assertEqual(len(stop.return_ids), 1)

        # returning more than delivered is rejected
        bad = dict(visit, returns=[{"uuid": _uuid(), "line_id": line.id, "qty": 11, "reason": "x"}])
        res = self._sync([self._event(route, "visit", bad, captured="2026-09-26T15:02:00Z")])
        self.assertEqual(res[0]["status"], "error")

        self._sync([self._event(route, "close", {})])
        route.cash_counted = 800
        self.assertEqual(route.cash_difference, 0)

    # ------------------------------------------- settlement -> payments

    def test_settlement_registers_cash_oldest_first(self):
        receivable = self.company_data["default_account_receivable"]
        old_invoice = self.env["account.move"].create({
            "move_type": "out_invoice",
            "partner_id": self.partner.id,
            "invoice_date": "2026-01-10",
            "invoice_line_ids": [Command.create({
                "name": "deuda vieja", "quantity": 1, "price_unit": 500, "tax_ids": [Command.clear()]})],
        })
        old_invoice.action_post()
        invoice = self.order._create_invoices()
        invoice.invoice_date = "2026-09-25"
        invoice.action_post()

        other = self._order(self.partner, self.product_a)
        route = self._route(self.order | other)
        stop, stop2 = route.stop_ids
        self.assertEqual(stop.order_amount, 1000)
        self.assertEqual(stop.previous_debt, 500)

        line = self.order.order_line
        ret_uuid = _uuid()
        self._sync([
            self._event(route, "visit", self._visit(stop, returns_answer="yes", returns=[
                {"uuid": ret_uuid, "line_id": line.id, "qty": 2, "reason": "Vencido"}], cash=700)),
            self._event(route, "visit", {"visit_uuid": _uuid(), "stop_id": stop2.id,
                                         "outcome": "not_delivered", "reason": "Cerrado"}),
            self._event(route, "close", {}),
        ])
        self.assertEqual(route.state, "closed")
        self.assertEqual(stop.expected_balance, 1300)

        route.cash_counted = 650
        self.assertEqual(route.cash_difference, -50)
        with self.assertRaises(UserError):
            route.action_settle()  # the return is not checked yet
        stop.return_ids.action_received()
        with self.assertRaises(UserError):
            route.action_settle()  # undelivered order not unloaded
        stop2.unloaded = True
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()

        self.assertEqual(route.state, "settled")
        self.assertEqual(len(route.payment_ids), 1)
        payment = route.payment_ids
        self.assertEqual(payment.amount, 700)
        self.assertEqual(payment.partner_id, self.partner.commercial_partner_id)
        self.assertEqual(old_invoice.amount_residual, 0, "oldest invoice is paid first")
        # the return's credit note is posted and applied before the cash
        self.assertEqual(invoice.amount_residual, 600)
        self.assertTrue(payment.move_id.line_ids.filtered(
            lambda l: l.account_id == receivable).reconciled)

        refund = stop.return_ids.credit_note_id
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.state, "posted")
        self.assertEqual(refund.amount_total, 200)
        self.assertEqual(refund.amount_residual, 0)

        cash_case = route.case_ids.filtered(lambda c: c.kind == "cash")
        self.assertEqual(len(cash_case), 1)
        self.assertEqual(cash_case.amount, -50)

        # the undelivered order can go on another route
        self.assertEqual(other.distri_delivery_state, "pending")
        next_route = self._route(self.env["sale.order"])
        stop2.reassign_route_id = next_route
        stop2.action_reassign()
        self.assertIn(other, next_route.stop_ids.order_id)
        self.assertEqual(other.distri_delivery_state, "on_route")

    def test_transfers_never_create_payments(self):
        route = self._route(self.order)
        stop = route.stop_ids
        self._sync([
            self._event(route, "visit", self._visit(stop, transfer="yes", transfer_amount=1000,
                                                    receipt_answer="forwarded")),
            self._event(route, "close", {}),
        ])
        promise = self.env["distri.promised.transfer"].sudo().search([("stop_id", "=", stop.id)])
        self.assertEqual(promise.amount, 1000)
        self.assertEqual(promise.receipt_answer, "forwarded")
        route.cash_counted = 0
        route.journal_id = self.company_data["default_journal_cash"]
        route.action_settle()
        self.assertFalse(route.payment_ids)

    def test_missing_return_opens_case(self):
        route = self._route(self.order)
        stop = route.stop_ids
        self._sync([self._event(route, "visit", self._visit(stop, returns_answer="yes", returns=[
            {"uuid": _uuid(), "line_id": self.order.order_line.id, "qty": 1, "reason": "Roto"}],
            cash=900))])
        stop.return_ids.action_missing()
        case = stop.return_ids.case_id
        self.assertEqual(case.kind, "return")
        self.assertEqual(case.amount, -100)

    def test_assign_by_zone_oldest_first(self):
        zone = self.env["distri.zona"].sudo().create({"name": "Zona Test Reparto", "viaje": "1"})
        self.partner.city = "Zona Test Reparto"
        newer = self._order(self.partner, self.product_a)
        route = self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "zone_ids": [Command.set(zone.ids)]})
        route.action_assign_orders()
        self.assertEqual(route.stop_ids.order_id[:2], self.order | newer)
        self.assertEqual(route.stop_ids[0].order_id, self.order)
        # already on a route: not assigned twice
        route2 = self.env["distri.route"].sudo().create({
            "driver_id": self.other_driver.id, "zone_ids": [Command.set(zone.ids)]})
        route2.action_assign_orders()
        self.assertFalse(route2.stop_ids & route.stop_ids)
        self.assertFalse(set(route2.stop_ids.order_id.ids) & {self.order.id, newer.id})

    def test_route_cash_journal_is_not_the_depot_one(self):
        depot = self.env["distri.route"]._depot_journal()
        route = self.env["distri.route"].sudo().create({"driver_id": self.driver.id})
        self.assertTrue(route.journal_id)
        self.assertNotEqual(route.journal_id, depot)
        self.assertEqual(route.journal_id.type, "cash")

    def test_pickup_orders_stay_off_the_route(self):
        zone = self.env["distri.zona"].sudo().create({"name": "Zona Test Retira", "viaje": "1"})
        self.partner.city = "Zona Test Retira"
        retira = self._order(self.partner, self.product_a)
        retira.distri_entrega = "retira"
        route = self.env["distri.route"].sudo().create({
            "driver_id": self.driver.id, "zone_ids": [Command.set(zone.ids)]})
        route.action_assign_orders()
        self.assertIn(self.order, route.stop_ids.order_id)
        self.assertNotIn(retira, route.stop_ids.order_id)


@tagged("post_install", "-at_install", "distri_reparto")
class TestRepartoHttp(RepartoMixin, HttpCase):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        partner = cls.env["res.partner"].create({"name": "X - KIOSCO PRUEBA REPARTO", "city": "Centenario"})
        product = cls.env["product.product"].create({
            "name": "Producto reparto", "list_price": 100, "type": "consu", "taxes_id": [Command.clear()]})
        cls.order = cls._order(partner, product, qty=3, price=100)

    def test_app_manifest_and_sync(self):
        route = self._route(self.order)
        self.authenticate("repartidor_test", "repartidor_test")

        res = self.url_open("/reparto")
        self.assertEqual(res.status_code, 200)
        self.assertIn("manifest.webmanifest", res.text)

        res = self.url_open("/reparto/manifest.webmanifest")
        self.assertEqual(res.json()["start_url"], "/reparto")

        res = self.url_open("/reparto/sw.js")
        self.assertEqual(res.headers.get("Service-Worker-Allowed"), "/reparto")
        self.assertNotIn("__VERSION__", res.text)
        self.assertIn("/distri_pedido/static/src/css/distri_ui.css?v=", res.text)

        res = self.url_open("/reparto/api/route")
        data = res.json()
        self.assertEqual(data["route"]["id"], route.id)
        self.assertEqual(data["route"]["stops"][0]["name"], "Kiosco Prueba Reparto")

        stop = route.stop_ids
        body = json.dumps({"events": [self._event(route, "visit", self._visit(stop, cash=300))]})
        headers = {"Content-Type": "application/json"}
        first = self.url_open("/reparto/api/sync", data=body, headers=headers).json()
        second = self.url_open("/reparto/api/sync", data=body, headers=headers).json()
        self.assertEqual(first["results"][0]["status"], "ok", first)
        self.assertEqual(second["results"][0]["status"], "duplicate")

        res = self.url_open("/reparto/api/sync", data="events=1",
                            headers={"Content-Type": "application/x-www-form-urlencoded"})
        self.assertEqual(res.status_code, 415)

    def test_other_users_are_refused(self):
        new_test_user(self.env, login="vendedor_test_rep", groups="base.group_user")
        self.authenticate("vendedor_test_rep", "vendedor_test_rep")
        res = self.url_open("/reparto/api/route")
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.url_open("/reparto/api/account?stop=1").status_code, 403)

    def test_driver_sees_the_account_of_his_own_stops(self):
        """19.0.1.92.0: «Ver cuenta» of a stop of any of the driver's own
        routes; another driver's stop is refused (no IDOR). Any customer's
        account is in /cuentas."""
        route = self._route(self.order)
        stop = route.stop_ids
        partner = stop.partner_id.commercial_partner_id
        self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": partner.id, "fecha": "2026-01-05", "comprobante": "0001/2026/77001",
            "tipo": "factura", "debe": 1234.0})
        other_partner = self.env["res.partner"].create({"name": "X - KIOSCO DE OTRO REPARTO"})
        other_order = self._order(other_partner, self.order.order_line.product_id, qty=1)
        other_route = self._route(other_order, driver=self.other_driver)

        self.authenticate("repartidor_test", "repartidor_test")
        data = self.url_open("/reparto/api/account?stop=%d" % stop.id).json()
        self.assertTrue(data["ok"], data)
        account = data["account"]
        self.assertEqual(account["partner_id"], partner.id)
        self.assertEqual(account["balance"], partner._distri_statement()["balance"])
        self.assertEqual(account["rows"][0]["ref"], "0001/2026/77001")
        self.assertEqual(account["rows"][0]["debit"], 1234.0)

        for wanted in (other_route.stop_ids.id, "abc", 0, 999999999):
            res = self.url_open("/reparto/api/account?stop=%s" % wanted)
            self.assertEqual(res.status_code, 404, wanted)
            self.assertFalse(res.json()["ok"])
        # an old, settled route of his: still his stop
        route.sudo().write({"state": "settled",
                            "date": fields.Date.subtract(fields.Date.today(), days=90)})
        self.assertEqual(self.url_open("/reparto/api/account?stop=%d" % stop.id).status_code, 200)
        # and the driver opens /cuentas, linked from the app
        self.assertEqual(self.url_open("/cuentas").status_code, 200)

    def test_app_links_to_the_account_and_the_cash_page(self):
        source = self.url_open("/distri_pedido/static/src/reparto/app.js").text
        self.assertIn("/reparto/api/account", source)
        self.assertIn('"/cobro"', source)
        self.assertIn('"/cuentas"', source)
