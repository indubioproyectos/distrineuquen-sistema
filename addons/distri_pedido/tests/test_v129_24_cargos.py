"""19.0.1.129.24: a driver charge closes when its transfer is verified in
Mercado Pago, even if the receipt had already arrived late.

Production, 24/11: 15 open «Cargos al repartidor» whose pending receipt was
already «Resuelta» (by a WhatsApp receipt after it expired, which by design
leaves the charge to administración); 13 of them were then confirmed in
Mercado Pago, which is the one thing that closes such a charge. The
confirmation only linked the transfer (`_distri_fulfil_for`, a promise
already fulfilled) and never closed the charge.

Also the one-off `_distri_close_settled_charges` (dry run and apply,
idempotent) for the charges left open by that bug. Synthetic data only.
"""
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.comprobante_pendiente import (
    MP_CLOSED_CASE, PAID_CLOSED_CASE, PARAM_RED)
from odoo.addons.distri_pedido.tests import test_comprobantes as tc
from odoo.addons.distri_pedido.tests.test_cobros import _fixture
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

# the fixture's transfer 900000000004: $ 51.234 on 24/09 15:00
SOURCE = "900000000004"


@tagged("post_install", "-at_install", "distri_v129_24")
class TestV12924Cargos(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    _deliver = tc.TestComprobantes._deliver
    _no_pay = tc.TestComprobantes._no_pay

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.seller = new_test_user(
            cls.env, login="vendedor_cargos_v12924", name="Vendedor Cargos",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cartera = cls.env["res.partner.category"].create({"name": "Cartera Cargos V12924"})
        cls.seller.sudo().etiqueta_cartera_id = cartera
        cls.customer = cls.env["res.partner"].create({
            "name": "Almacén Cargos V12924", "company_type": "company",
            "category_id": [Command.set(cartera.ids)]})
        cls.Promise = cls.env["distri.promised.transfer"].sudo()
        cls.product_a.invoice_policy = "order"
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_RED, "0")

    def _late_receipt_then_report(self):
        """Expired, the report proposes its transfer, then the receipt
        arrives late (marked by the seller): the charge stays open."""
        order = self._order(self.customer, self.product_a, qty=1, price=51234)
        route = self._route(order)
        promise = self._deliver(route, route.stop_ids, captured="2026-09-24T18:00:00.000Z",
                                transfer_amount=51234)
        self.Promise._distri_cron_expire()
        case = promise.charge_case_id
        self.assertEqual((promise.state, case.state), ("overdue", "open"))
        self.env["distri.mp.report"]._import_file("mp_report_25col.csv",
                                                  _fixture("mp_report_25col.csv"))
        card = self.env["distri.mp.collection"].search([("source_id", "=", SOURCE)])
        self.assertEqual(card.promise_id, promise)
        promise.with_user(self.seller).action_mark_arrived()
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(case.state, "open", "a late receipt alone leaves it to administración")
        return promise, case, card

    def test_mp_confirmation_closes_the_charge_of_a_late_receipt(self):
        promise, case, card = self._late_receipt_then_report()
        card.action_confirm()
        self.assertEqual(promise.fulfilled_by_id, card)
        self.assertEqual(case.state, "closed")
        self.assertEqual(case.resolution, MP_CLOSED_CASE % card.name)
        # undone: the verification is gone, the charge opens again
        card.action_undo()
        self.assertFalse(promise.fulfilled_by_id)
        self.assertEqual(promise.state, "fulfilled", "the late receipt still counts")
        self.assertEqual(case.state, "open")
        self.assertFalse(case.resolution)

    def test_one_off_closes_settled_charges_dry_run_then_apply(self):
        # left open by the old code: confirmed in Mercado Pago, charge open
        promise, case, card = self._late_receipt_then_report()
        card.action_confirm()
        case.write({"state": "open", "resolution": False})
        # a late receipt without Mercado Pago: administración decides
        order = self._order(self.customer, self.product_a, price=33)
        route = self._route(order)
        late = self._deliver(route, route.stop_ids, transfer_amount=330)
        self.Promise._distri_cron_expire()
        late.with_user(self.seller).action_mark_arrived()
        # an unpaid order whose payment closed the item but not its charge
        order = self._order(self.customer, self.product_a, price=44)
        route = self._route(order)
        unpaid = self._no_pay(route, route.stop_ids)
        self.Promise._distri_cron_expire()
        unpaid.write({"state": "fulfilled", "resolved_via": "payment",
                      "resolved_by_label": "Pago registrado"})
        cases = case | late.charge_case_id | unpaid.charge_case_id
        self.assertEqual(set(cases.mapped("state")), {"open"})

        ledger = self.Promise._distri_ledger_fingerprint()
        balance = self.customer._distri_account_balance()
        rows = self.Promise._distri_close_settled_charges()
        self.assertEqual(self.Promise._distri_ledger_fingerprint(), ledger)
        by_case = {r["case_id"]: r for r in rows}
        self.assertTrue(by_case[case.id]["close"])
        self.assertEqual(by_case[case.id]["resolution"], MP_CLOSED_CASE % card.name)
        self.assertTrue(by_case[unpaid.charge_case_id.id]["close"])
        self.assertFalse(by_case[late.charge_case_id.id]["close"])
        self.assertEqual(set(cases.mapped("state")), {"open"}, "a dry run writes nothing")

        # owner's rule: if anything would move a customer balance, nothing closes
        Promise = type(self.Promise)
        moved = iter([(1.0, 0, 0, 0), (2.0, 0, 0, 0)])
        with patch.object(Promise, "_distri_ledger_fingerprint", lambda self: next(moved)), \
                self.assertRaisesRegex(UserError, "saldos de clientes"), self.cr.savepoint():
            self.Promise._distri_close_settled_charges(apply=True)
        self.env.invalidate_all()
        self.assertEqual(case.state, "open")

        self.Promise._distri_close_settled_charges(apply=True)
        self.assertEqual(self.Promise._distri_ledger_fingerprint(), ledger,
                         "no entry, payment or reconciliation touched")
        self.assertEqual(self.customer._distri_account_balance(), balance)
        self.assertEqual(case.state, "closed")
        self.assertEqual(case.resolution, MP_CLOSED_CASE % card.name)
        self.assertIn("19.0.1.129.24", case.message_ids[:1].body)
        self.assertEqual(unpaid.charge_case_id.state, "closed")
        self.assertEqual(unpaid.charge_case_id.resolution, PAID_CLOSED_CASE % order.name)
        self.assertEqual(late.charge_case_id.state, "open")
        # idempotent: only the one left to administración is listed again
        again = self.Promise._distri_close_settled_charges(apply=True)
        self.assertEqual([r["case_id"] for r in again], [late.charge_case_id.id])
        self.assertFalse(again[0]["close"])
