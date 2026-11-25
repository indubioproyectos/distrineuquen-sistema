"""19.0.1.129.24 (#106): voiding a payment keeps its journal entry and number.

`action_draft()` + `action_cancel()` deleted the draft entry, and Odoo only
lets an account manager (in production, only admin) delete a numbered entry
that is not the last of its journal sequence: a driver's settlement failed
on 24/11. Now the entry is cancelled (`account.move.button_cancel()`), keeps
its name, and the payment ends «canceled».

One test per flow, each with the payment to void NOT the last of its
journal and run by the role that uses it in real life (never an account
manager): the driver correcting a visit, the user of «Pago a proveedor»
cancelling it, administración with cobros annulling from the audit and
undoing a confirmed card. Synthetic data only.
"""
import base64

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.tests import test_v129_2_efectivo_a_rendir as t1292
from odoo.addons.distri_pedido.tests import test_v129_auditoria as t129
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin, _png

ACCOUNT_MANAGER = "account.group_account_manager"


class VoidAssertsMixin:

    def _assert_not_last(self, payment, later):
        """`later` is a newer numbered entry of the same journal."""
        self.assertTrue(payment.move_id.name and payment.move_id.name != "/")
        self.assertEqual(later.journal_id, payment.journal_id)
        self.assertGreater(later.move_id.id, payment.move_id.id)
        self.assertTrue(later.move_id.name and later.move_id.name != "/")

    def _assert_voided(self, payment, name):
        move = payment.move_id
        self.assertTrue(move.exists(), "the entry is kept, never deleted")
        self.assertEqual(move.state, "cancel")
        self.assertEqual(move.name, name, "it keeps its number")
        self.assertEqual(payment.state, "canceled")


@tagged("post_install", "-at_install", "distri_v129_24")
class TestV12924VoidDriverCash(VoidAssertsMixin, CajaMixin, AccountTestInvoicingCommon):
    """The driver corrects a visit: its «a rendir» payment is voided."""

    ROUTE_ON_ROAD = True

    # the helpers of the 19.0.1.129.2 tests (module import: its class is not
    # collected twice)
    _invoiced_order = t1292.TestV1292DriverCash._invoiced_order
    _visit_sync = t1292.TestV1292DriverCash._visit_sync
    _live = t1292.TestV1292DriverCash._live

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Anular V12924",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Anular V12924",
                                                           "company_type": "company"})
        cls.third_partner = cls.env["res.partner"].create({"name": "Despensa Anular V12924",
                                                           "company_type": "company"})
        cls.settler = new_test_user(
            cls.env, login="rinde_v12924", name="Recibe Rutas V12924",
            groups="base.group_user,distri_pedido.grupo_administracion,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.Payment = cls.env["account.payment"]

    def test_driver_corrects_a_visit_whose_payment_is_not_the_last(self):
        self.assertFalse(self.driver.has_group(ACCOUNT_MANAGER))
        order, invoice = self._invoiced_order()
        other_order, _other_invoice = self._invoiced_order(partner=self.other_partner)
        third_order, _third_invoice = self._invoiced_order(partner=self.third_partner)
        route = self._route(order | other_order | third_order)
        stop = route.stop_ids.filtered(lambda s: s.order_id == order)
        other_stop = route.stop_ids.filtered(lambda s: s.order_id == other_order)
        third_stop = route.stop_ids.filtered(lambda s: s.order_id == third_order)
        payload = self._visit_sync(route, stop, cash=600)
        first = self._live(stop)
        self._visit_sync(route, other_stop, captured="2026-11-22T15:02:00.000Z", cash=300)
        self._assert_not_last(first, self._live(other_stop))
        name = first.move_id.name
        # the old way (delete the draft entry) is refused to the driver
        with self.assertRaisesRegex(UserError, "not the last one in the chain"):
            first.move_id.with_user(self.driver)._unlink_forbid_parts_of_chain()
        self.assertEqual(invoice.amount_residual, 400)
        # voided as the driver (a corrected visit: the app's sync runs it
        # sudo as the driver), never an account manager
        stop.with_user(self.driver).sudo()._distri_void_driver_cash(first, "prueba de anulación")
        self._assert_voided(first, name)
        self.assertFalse(self._live(stop))
        self.assertEqual(invoice.amount_residual, 1000, "the invoice owes again")
        # the driver fixes the amount: a new payment
        res = self._sync([self._event(route, "visit", dict(payload, cash=800),
                                      captured="2026-11-22T15:05:00.000Z")])
        self.assertEqual(res[0]["status"], "ok", res)
        self._assert_voided(first, name)
        live = self._live(stop)
        self.assertEqual(len(live), 1, "the unique live payment per stop still holds")
        self.assertEqual(live.amount, 800)
        self.assertEqual(invoice.amount_residual, 200)
        self.assertIn("Se anuló el cobro a rendir", "".join(route.message_ids.mapped("body")))
        # the settlement voids the new one as the cashier who receives the
        # route (the path that failed on 24/11: administración and cajero, as
        # in production); it is not the last either
        self.assertFalse(self.settler.has_group(ACCOUNT_MANAGER))
        self._visit_sync(route, third_stop, captured="2026-11-22T15:07:00.000Z", cash=100)
        self._assert_not_last(live, self._live(third_stop))
        stop.with_user(self.settler)._distri_void_driver_cash(live, "prueba de rendición")
        self.assertEqual(live.move_id.state, "cancel")
        self.assertEqual(live.state, "canceled")
        self.assertFalse(self._live(stop))
        self.assertEqual(invoice.amount_residual, 1000, "the invoice owes again")
        # the balance the old code (entry deleted) gave: the full debt
        self.assertEqual(self.partner._distri_account_balance(), 1000)
        self.assertEqual(self.other_partner._distri_account_balance(), 700)
        self.assertEqual(self.third_partner._distri_account_balance(), 900)


@tagged("post_install", "-at_install", "distri_v129_24")
class TestV12924VoidOthers(VoidAssertsMixin, CreditMixin, RepartoMixin,
                           AccountTestInvoicingCommon):
    """Pago a proveedor, the audit's «Anular pago» and «Deshacer» on a card."""

    ROUTE_ON_ROAD = True

    _invoice = t129.TestV129Auditoria._invoice
    _csv = t129.TestV129Auditoria._csv
    _report = t129.TestV129Auditoria._report
    _filler = t129.TestV129Auditoria._filler
    _anomalies = t129.TestV129Auditoria._anomalies
    _credited = t129.TestV129Auditoria._credited
    _mp_payment = t129.TestV129Auditoria._mp_payment

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        params.set_param(PARAM_DRIVER_RULE, "")
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        cls.seller = user("vendedor_v12924", "distri_pedido.grupo_vendedor")
        cls.manager = user("gerente_v12924", "distri_pedido.grupo_gerencia")
        # Denis: administración, authorizes money
        cls.denis = user("administracion_v12924", "distri_pedido.grupo_administracion,"
                                                  "distri_pedido.grupo_cobros,"
                                                  "distri_pedido.grupo_ingresos_reporte")
        # Gonzalo: cajero with «Cobros: pago a proveedor de Distri»
        cls.gonzalo = user("gonzalo_v12924", "distri_pedido.grupo_cajero,"
                                             "distri_pedido.grupo_pago_proveedor")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V12924",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Prueba V12924",
                                                           "company_type": "company"})
        cls.supplier = cls.env["res.partner"].create({"name": "Proveedor Prueba V12924",
                                                      "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Report = cls.env["distri.mp.report"]
        cls.Anomaly = cls.env["distri.mp.anomaly"]
        for who in (cls.denis, cls.gonzalo):
            assert not who.has_group(ACCOUNT_MANAGER)

    # ------------------------------------------------------------ pago a proveedor

    def _supplier(self, amount):
        record = self.env["distri.supplier.payment"].with_user(self.gonzalo).create({
            "partner_id": self.partner.id, "supplier_id": self.supplier.id, "amount": amount,
            "authorized_by_id": self.manager.id, "receipt_file": base64.b64encode(_png(24)),
            "receipt_filename": "c.png"})
        record.action_post()
        return record

    def test_supplier_payment_cancelled_when_not_the_last(self):
        invoice = self._invoice(50000)
        record = self._supplier(20000)
        payment = record.payment_id
        later = self._supplier(5000)
        self._assert_not_last(payment, later.payment_id)
        name = payment.move_id.name
        self.assertEqual(invoice.amount_residual, 25000)
        record.with_user(self.gonzalo).action_cancel()
        self._assert_voided(payment, name)
        self.assertEqual(record.state, "cancelled")
        self.assertIn("pago %s anulado" % payment.name, record.message_ids[:1].body)
        self.assertEqual(invoice.amount_residual, 45000, "the invoice owes it again")
        # what the old code (entry deleted) left: only the later payment counts
        self.assertEqual(self._balance(), 45000)
        self.assertEqual(later.payment_id.state, "paid")

    # ------------------------------------------------------------ audit

    def test_audit_annul_when_not_the_last(self):
        invoice = self._invoice(50000)
        card = self._credited(39000, operation="129240000001")
        payment = card.payment_id
        later = self._mp_payment(1000, partner=self.other_partner)
        self._assert_not_last(payment, later)
        name = payment.move_id.name
        self.assertEqual(invoice.amount_residual, 11000)
        self._filler()
        anomaly = self._anomalies("sin_transferencia", card_id=card)
        action = anomaly.with_user(self.denis).action_open_annul()
        wizard = self.env["distri.mp.anomaly.review"].with_user(self.denis).browse(
            action["res_id"])
        wizard.note = "Comprobante falso: el cliente no transfirió"
        wizard.action_apply()
        self._assert_voided(payment, name)
        self.assertEqual((card.state, card.audit_state), ("ignored", "anulado"))
        self.assertEqual(anomaly.state, "annulled")
        self.assertEqual(invoice.amount_residual, 50000, "the invoice owes again")
        self.assertEqual(self._balance(), 50000, "as the old code left it")
        self.assertEqual(self._balance(self.other_partner), -1000, "the later one untouched")
        self.assertNotEqual(later.state, "canceled")

    # ------------------------------------------------------------ cobros

    def test_undo_confirmed_card_when_not_the_last(self):
        invoice = self._invoice(5000)
        card = self.Collection.sudo().create({
            "source_id": "129240000101", "amount": 1500.0, "date": fields.Datetime.now(),
            "method_type": "bank_transfer", "partner_id": self.partner.id,
            "proposal": "cuit"}).with_user(self.denis)
        card.action_confirm()
        payment = card.payment_id
        other = self.Collection.sudo().create({
            "source_id": "129240000102", "amount": 700.0, "date": fields.Datetime.now(),
            "method_type": "bank_transfer", "partner_id": self.other_partner.id,
            "proposal": "cuit"}).with_user(self.denis)
        other.action_confirm()
        self._assert_not_last(payment, other.payment_id)
        name = payment.move_id.name
        self.assertEqual(invoice.amount_residual, 3500)
        card.action_undo()
        self._assert_voided(payment, name)
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.payment_id)
        self.assertIn("pago %s anulado" % payment.name, card.message_ids[:1].body)
        self.assertEqual(invoice.amount_residual, 5000, "the invoice owes again")
        self.assertEqual(self._balance(), 5000, "as the old code left it")
        self.assertEqual(other.state, "confirmed")
