"""19.0.1.129.32: fixes from the independent review of 129.27-129.31.

- Blocking (129.29, no flag): after the phone's «Atrás», «Salir a repartir»
  works again (Node harness tests/data/navegacion_v129_29.js); a double tap
  on «Guardar» steps back once.
- High (129.28, reparto_v2 on): the driver's own photo (or a receipt he
  forwarded) never closes his overdue charge; it resolves the pending
  receipt and waits for administración or the Mercado Pago report.
"""
import json
import os
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import TransactionCase, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.tests.test_comprobante_app import _png, photo_url
from odoo.addons.distri_pedido.tests.test_reparto import _uuid
from odoo.addons.distri_pedido.tests.test_v129_28_reparto_v2 import V12928Mixin


@tagged("post_install", "-at_install", "distri_v129_32")
class TestNavegacionV12932(TransactionCase):

    def test_depart_after_phone_back_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/navegacion_v129_29.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # «Control de carga» -> «Salir a repartir» -> phone back -> «Salir a repartir»
        dep = out["reparto_depart_after_phone_back"]
        self.assertEqual(dep["first"], {"hash": "#salida", "entries": 2})
        self.assertEqual(dep["afterBack"], {"hash": "", "dnFrom": None},
                         "the loaded entry keeps no parent")
        self.assertEqual((dep["hash"], dep["entries"], dep["index"]), ("#salida", 2, 1))
        self.assertEqual(out["reparto_depart_stale_state"], {"hash": "#salida", "entries": 2})
        self.assertEqual(out["reparto_forward_never_back"], {"hash": "#salida", "entries": 2})
        tap = out["reparto_double_tap_save"]
        self.assertEqual((tap["backCalls"], tap["hash"], tap["leave"]), (1, "#visitas", 1))
        # the 129.29 levels still go up
        for key in ("reparto_back_from_visit", "reparto_two_levels", "reparto_go_after_saving",
                    "reparto_home_is_the_list", "despacho_back_from_opened_route",
                    "cuentas_row_then_debtors"):
            self.assertEqual(out[key]["leave"], 1, key)


@tagged("post_install", "-at_install", "distri_v129_32")
class TestDriverPhotoKeepsCharge(V12928Mixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_v129_28()
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Foto V12932",
                                                     "company_type": "company"})
        cls.Promise = cls.env["distri.promised.transfer"].sudo()
        cls.Message = cls.env["distri.whatsapp.message"].sudo()

    def _overdue(self):
        order = self._order(self.partner, self.product, qty=1, price=5000)
        route = self._route(order)
        res = self._sync([self._event(route, "visit", self._visit(
            route.stop_ids, transfer="yes", transfer_amount=5000, receipt_answer="not_yet"),
            captured="2026-09-24T18:00:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        promise = self.Promise.search([("stop_id", "=", route.stop_ids.id)])
        self.Promise._distri_cron_expire()
        case = promise.charge_case_id
        self.assertEqual((promise.state, case.state), ("overdue", "open"))
        return route, promise, case

    def _ledger(self):
        self.env.flush_all()
        self.partner.invalidate_recordset(["credit"])
        return self.Promise._distri_ledger_fingerprint(), self.partner.credit

    def test_driver_photo_does_not_close_the_charge(self):
        self._v2()
        route, promise, case = self._overdue()
        before = self._ledger()
        stop = route.stop_ids
        res = self._sync([self._event(route, "receipt", {
            "photo_uuid": _uuid(), "stop_id": stop.id, "visit_uuid": stop.visit_uuid or _uuid(),
            "transfer_amount": 5000, "photo": photo_url(_png())},
            captured="2026-09-26T15:01:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        photo = self.Message.search([("distri_app_stop_id", "=", stop.id),
                                     ("distri_from_app", "=", True)])
        self.assertEqual(len(photo), 1)
        # the receipt is attached and goes to verification
        self.assertTrue(photo.attachment_id)
        self.assertTrue(photo.distri_is_receipt)
        self.assertEqual(photo.distri_receipt_state, "pending")
        self.assertEqual((promise.state, promise.receipt_id), ("fulfilled", photo))
        # the charge stays for administración
        self.assertEqual(case.state, "open")
        self.assertFalse(case.resolution)
        self.assertTrue(any("Administración decide" in (m.body or "") for m in case.message_ids))
        self.assertEqual(self._ledger(), before)
        # Mercado Pago verifying the transfer still closes it (129.24)
        promise._distri_close_charge_mp()
        self.assertEqual(case.state, "closed")

    def test_short_charge_closes_once_the_order_is_paid(self):
        """Medium (129.28): a partial cash payment's charge closes by itself
        when the customer pays the rest (hourly cron)."""
        order = self._order(self.partner, self.product, qty=10, price=100)
        invoice = order._create_invoices()
        invoice.action_post()
        route = self._route(order)
        stop = route.stop_ids
        self._v2()
        route.with_user(self.driver).sudo()._app_payload()
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=600))])
        self.assertEqual(res[0]["status"], "ok", res)
        case = self.env["distri.route.case"].sudo().search([("stop_id", "=", stop.id),
                                                              ("kind", "=", "short")])
        self.assertEqual((case.amount, case.state), (-400, "open"))
        self.env["distri.route.stop"]._distri_cron_paid_to_confirm()
        self.assertEqual(case.state, "open", "the order still owes")
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create(
            {"amount": invoice.amount_residual}).action_create_payments()
        self.assertEqual(invoice.amount_residual, 0)
        # with the flag off it stays as in 129.27
        self._v2(False)
        self.env["distri.route.stop"]._distri_cron_paid_to_confirm()
        self.assertEqual(case.state, "open")
        self._v2()
        self.env["distri.route.stop"]._distri_cron_paid_to_confirm()
        self.assertEqual(case.state, "closed")
        self.assertIn("quedó pagado", case.resolution)

    def test_no_snapshot_no_cash_charge(self):
        """Medium (129.28): a route downloaded before the flag (no «Le toca
        pagar» snapshot) is not charged for a partial cash payment."""
        order = self._order(self.partner, self.product, qty=10, price=100)
        order._create_invoices().action_post()
        route = self._route(order)
        stop = route.stop_ids
        self._v2()
        self.assertFalse(stop.v2_due_known)
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=600))])
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertFalse(self.env["distri.route.case"].sudo().search(
            [("stop_id", "=", stop.id), ("kind", "=", "short")]))

    def test_receipt_forwarded_by_the_driver_does_not_close_it(self):
        self._v2()
        route, promise, case = self._overdue()
        promise._distri_resolve("whatsapp", user=route.driver_id)
        self.assertEqual((promise.state, case.state), ("fulfilled", "open"))

    def test_someone_else_still_closes_it(self):
        self._v2()
        _route, promise, case = self._overdue()
        promise.action_mark_arrived()
        self.assertEqual((promise.state, case.state), ("fulfilled", "closed"))
        self.assertIn("se cerró solo", case.resolution)


@tagged("post_install", "-at_install", "distri_v129_32")
class TestBoardFlagOff(TransactionCase):

    def _flag(self, on):
        from odoo.addons.distri_pedido.models.verificacion import PARAM_VERIFICACION
        self.env["ir.config_parameter"].sudo().set_param(PARAM_VERIFICACION, "1" if on else "0")

    def test_board_action_is_129_26_with_the_flag_off(self):
        from odoo.tools.safe_eval import safe_eval
        self._flag(False)
        action = self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_mp_board")
        self.assertEqual(safe_eval(action["context"]),
                         {"search_default_to_act": 1, "search_default_last_60": 1})
        self._flag(True)
        action = self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_mp_board")
        self.assertEqual(safe_eval(action["context"]), {
            "search_default_to_act": 1, "search_default_last_60": 1,
            "search_default_not_historic": 1, "distri_verificacion_v2": True})

    def test_new_filters_are_invisible_without_the_flag(self):
        from lxml import etree
        arch = self.env["distri.mp.collection"].get_view(
            self.env.ref("distri_pedido.view_mp_collection_search").id, "search")["arch"]
        root = etree.fromstring(arch)
        for name in ("not_historic", "historic", "auto_credited_check"):
            node = root.xpath("//filter[@name='%s']" % name)[0]
            self.assertEqual(node.get("invisible"), "not context.get('distri_verificacion_v2')")


from odoo.addons.distri_pedido.tests import test_v129_31_verificacion as v31  # noqa: E402


@tagged("post_install", "-at_install", "distri_v129_32")
class TestTwinAlreadyCredited(v31.TestV12931Verificacion):
    """A held twin of a receipt that already has its money is an error for
    anyone who may not approve credits (verificacion_v2 on). The Odoo
    loader runs only the methods defined here, not the inherited ones."""

    def test_credited_twin_is_an_error_for_a_non_approver(self):
        self._flag()
        _op, amount = self._next()
        first, second = self._pair(amount)
        self.assertTrue(second.distri_twin_review)
        card = self._card(second)
        # the twin is still pending: a warning, as in 129.31
        errors, warnings = card.with_user(self.seller).sudo()._distri_credit_review(
            self.partner, amount)
        self.assertFalse([e for e in errors if "mismo pago" in e], errors)
        self.assertTrue(any("mismo pago" in w for w in warnings), warnings)
        # the twin was credited by a person before the report (its card
        # confirmed, the receipt still waiting): an error for the seller, a
        # warning for Denis
        self._card(first).sudo().write({"state": "confirmed"})
        self.assertEqual(first.distri_receipt_state, "pending")
        errors, _warnings = card.with_user(self.seller).sudo()._distri_credit_review(
            self.partner, amount)
        self.assertTrue(any("Son dos pagos" in e for e in errors), errors)
        errors, warnings = card.with_user(self.denis).sudo()._distri_credit_review(
            self.partner, amount)
        self.assertFalse(errors)
        self.assertTrue(any("mismo pago" in w for w in warnings), warnings)
        # with the flag off nothing of it applies
        self._flag(False)
        errors, warnings = card.with_user(self.seller).sudo()._distri_credit_review(
            self.partner, amount)
        self.assertFalse(any("mismo pago" in t for t in errors + warnings), errors + warnings)


    def test_auto_credit_never_pays_another_customer(self):
        """Medium (129.31 tests): the income without a customer or with
        another one; the receipt's customer is the only one ever paid."""
        self._flag()
        other = self.env["res.partner"].create({"name": "Otro Cliente V12932",
                                                "company_type": "company"})
        for income_partner in (False, other.id):
            operation, amount = self._next()
            income = self._income(operation, amount, partner_id=income_partner, proposal="none")
            self._customer(amount, operation=operation)
            income.invalidate_recordset()
            if income.payment_id:
                self.assertEqual(income.payment_id.partner_id.commercial_partner_id,
                                 self.partner, "only the receipt's customer")
            self.assertFalse(self.env["account.payment"].sudo().search(
                [("partner_id", "=", other.id)]), "never the other customer")


from unittest.mock import patch  # noqa: E402

from odoo.exceptions import UserError  # noqa: E402

from odoo.addons.distri_pedido.tests import test_v129_30_nueva_venta as v30  # noqa: E402


@tagged("post_install", "-at_install", "distri_v129_32")
class TestYaEntregadoNeedsInvoice(v30.TestV12930NuevaVenta):
    """«Ya entregado» is refused (and rolls back) when packing left no
    invoice or no validated remito (nueva_venta on)."""

    def test_no_invoice_no_delivered(self):
        self._flag()
        order = self.env["sale.order"].create({
            "partner_id": self.cliente.id, "order_line": [Command.create({
                "product_id": self.p2.product_variant_id.id, "product_uom_qty": 1})]})
        with patch.object(type(self.env["sale.order"]), "distri_terminar",
                          lambda self, bultos: ""):
            with self.assertRaisesRegex(UserError, "factura"):
                order.with_user(self.admin)._distri_nueva_venta_ya_entregado(
                    str(fields.Date.today()), "Entregado por Gonzalo")
        self.assertFalse(order.distri_entregado_fuera)


from odoo.addons.distri_pedido.tests import test_v129_27_caja as v27  # noqa: E402


@tagged("post_install", "-at_install", "distri_v129_32")
class TestAdvanceInTheApp(v27.TestCajaV2):
    """Medium (129.27, caja_v2 on): the phone knows the advance."""

    def test_payload_carries_the_advance(self):
        route = self._road_route()
        data = route.with_user(self.driver).sudo()._app_payload()
        self.assertNotIn("advance_total", data, "no advance: 129.26's payload")
        self._v2()
        caja = self._caja().open_session("300000")
        caja.with_user(self.cashier).add_advance(route, "200.000", note="cambio")
        data = route.with_user(self.driver).sudo()._app_payload()
        self.assertEqual(data["advance_total"], 200000)
