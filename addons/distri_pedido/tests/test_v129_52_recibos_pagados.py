"""19.0.1.129.52 (owner, 26/11/2026): a pending receipt («done», delivered
with a transfer) whose order's issued invoices are already paid in full is
resolved by itself, at the visit sync and in the hourly cron, and its open
charge to the driver closes («Factura … ya pagada: no es un faltante del
repartidor»). Production had S00278, S00257, S00324, S00276 and S00235 fixed
by hand: the customer's Mercado Pago transfer was confirmed before the
driver's visit synced. A partial payment changes nothing. The resolution
itself never touches the ledger. Synthetic data only."""
from odoo.tests import tagged

from odoo.addons.distri_pedido.models.comprobante_pendiente import DONE_PAID_CLOSED_CASE
from odoo.addons.distri_pedido.tests import test_comprobantes as tc
from odoo.addons.distri_pedido.tests.test_v129_24_cargos import TestV12924Cargos


@tagged("post_install", "-at_install", "distri_v129_52")
class TestV12952RecibosPagados(TestV12924Cargos):

    _invoiced_order = tc.TestComprobantes._invoiced_order
    _pay = tc.TestComprobantes._pay

    # the inherited tests already run in their own class
    test_mp_confirmation_closes_the_charge_of_a_late_receipt = None
    test_one_off_closes_settled_charges_dry_run_then_apply = None

    def _delivered(self, captured="2026-09-24T18:00:00.000Z"):
        order, invoice = self._invoiced_order(price=5000.0)
        route = self._route(order)
        return order, invoice, route

    def test_paid_before_the_visit_is_resolved_at_once_without_charge(self):
        order, invoice, route = self._delivered()
        self._pay(invoice, invoice.amount_residual)
        self.assertTrue(invoice.currency_id.is_zero(invoice.amount_residual))
        promise = self._deliver(route, route.stop_ids, transfer_amount=invoice.amount_total)
        self.assertEqual((promise.state, promise.resolved_via), ("fulfilled", "payment"))
        self.assertFalse(promise.charge_case_id)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "fulfilled")
        self.assertFalse(promise.charge_case_id)

    def test_overdue_with_charge_closes_when_the_invoice_is_paid(self):
        order, invoice, route = self._delivered()
        promise = self._deliver(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z",
                                transfer_amount=invoice.amount_total)
        self.Promise._distri_cron_expire()
        case = promise.charge_case_id
        self.assertEqual((promise.state, case.state), ("overdue", "open"))
        self._pay(invoice, invoice.amount_residual)
        ledger = self.Promise._distri_ledger_fingerprint()
        self.Promise._distri_cron_expire()
        self.assertEqual(self.Promise._distri_ledger_fingerprint(), ledger)
        self.assertEqual((promise.state, promise.resolved_via), ("fulfilled", "payment"))
        self.assertEqual(case.state, "closed")
        self.assertEqual(case.resolution, DONE_PAID_CLOSED_CASE % invoice.name)
        # a second run changes nothing
        self.Promise._distri_cron_expire()
        self.assertEqual(case.resolution, DONE_PAID_CLOSED_CASE % invoice.name)

    def test_partial_payment_is_left_alone(self):
        order, invoice, route = self._delivered()
        self._pay(invoice, invoice.amount_residual / 2)
        promise = self._deliver(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z",
                                transfer_amount=invoice.amount_total)
        self.assertEqual(promise.state, "open")
        ledger = self.Promise._distri_ledger_fingerprint()
        self.Promise._distri_cron_expire()
        self.assertEqual(self.Promise._distri_ledger_fingerprint(), ledger)
        self.assertEqual(promise.state, "overdue")
        self.assertEqual(promise.charge_case_id.state, "open")

    def test_order_without_invoice_is_left_alone(self):
        order = self._order(self.customer, self.product_a, price=5000.0)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z",
                                transfer_amount=5000.0)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "overdue")
