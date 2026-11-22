"""19.0.1.126.0: receipts credited at once (D11 replaced).

- A receipt the staff uploaded is credited when validated: the payment is
  registered in «Mercado Pago» at once, the balance goes down and the card
  waits in «Acreditado · a verificar» (audit «pendiente»).
- «Acreditar comprobantes»: cajero, reparto, vendedor, administración,
  gerencia and admin; nobody else. A receipt the customer sent waits in
  «Para acreditar» for administración, gerencia or admin.
- The drivers' receipts follow distri_pedido.acreditar_repartidor:
  «inmediato» (default), «rendicion», «coincide»; a user both driver and
  seller follows the driver rule only for what he registers as a driver.
- Defenses: an operation already credited, the same image, another
  destination account, an old receipt (approvers only, after a warning), an
  amount not read or different (a warning), a credit balance over the
  threshold (a warning).
- The report ties a later income to the credited payment (no second
  payment, audit «verificado»): exact operation, operation with one OCR slip,
  amount and time; a tie between several or another amount goes to review.
- «Pago confirmado» (✓) on the statements only once the report tied it.
- The one-time notice on /tablero.
Synthetic data only: no real customer, receipt or account.
"""
import base64
import io
import re
from datetime import timedelta, timezone
from unittest.mock import patch

from PIL import Image

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.acreditar import (
    CREDIT_NOTICE, CREDIT_REMINDER, PARAM_CREDIT_WARNING, PARAM_DRIVER_RULE, CreditWarning,
    can_approve, can_credit,
)
from odoo.addons.distri_pedido.models.cobros import COLUMNS
from odoo.addons.distri_pedido.models.ocr_comprobante import CONFIDENCE_HIGH
from odoo.addons.distri_pedido.models.reparto_cliente import AR_TZ
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


def _png(seed):
    buffer = io.BytesIO()
    Image.new("RGB", (30 + seed % 50, 20 + seed // 50 % 50), (seed % 255, 40, 90)).save(
        buffer, format="PNG")
    return buffer.getvalue()


def _local(moment):
    """Naive UTC -> naive Argentine time (what the reading returns)."""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).replace(tzinfo=None)


class CreditMixin:
    """Receipts as the pipeline leaves them, with the reading patched."""

    seq = 0

    def _reading(self, reading):
        Message = type(self.env["distri.whatsapp.message"])
        return patch.object(Message, "_distri_file_reading",
                            lambda msg: ("texto leído", dict(reading)))

    def _receipt(self, amount, partner=None, user=None, operation=None, when=None, read=None,
                 confidence=CONFIDENCE_HIGH, raw=None, extra=None, customer=False):
        """A receipt read with `read` (default `amount`): uploaded by `user`
        (staff), or sent by the customer (`customer=True`)."""
        CreditMixin.seq += 1
        # distinct moments: two receipts of the same amount and time are copies
        when = when or (fields.Datetime.now() - timedelta(minutes=3, seconds=CreditMixin.seq))
        partner = partner or self.partner
        vals = {
            "wa_message_id": "v126.%s.%s" % (CreditMixin.seq, _uuid()),
            "direction": "in", "message_type": "image", "body": "",
            "timestamp": fields.Datetime.now() - timedelta(minutes=1),
            "media_mime_type": "image/png",
            "distri_partner_id": partner.id, "distri_partner_source": "Elegido en la prueba",
            "distri_is_receipt": True, "distri_receipt_state": "pending",
        }
        if customer:
            vals.update(wa_id="549299%07d" % CreditMixin.seq, profile_name="Cliente")
        else:
            vals.update(distri_forwarded_by_id=(user or self.seller).id, distri_upload=True,
                        profile_name="Subido por el personal")
        Message = self.env["distri.whatsapp.message"].sudo().with_context(
            distri_defer_receipt_read=True)
        msg = Message.create(vals)
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "c%s.png" % CreditMixin.seq, "raw": raw or _png(CreditMixin.seq + 1000),
            "mimetype": "image/png", "res_model": msg._name, "res_id": msg.id})
        msg = msg.with_context(distri_defer_receipt_read=False)
        msg.with_context(distri_receipt_matching=True).write({"attachment_id": attachment.id})
        reading = {"amount": amount if read is None else read, "amount_confidence": confidence,
                   "date": _local(when)}
        if operation:
            reading["operation"] = operation
        reading.update(extra or {})
        with self._reading(reading):
            msg.write({"media_state": "done"})
        return msg

    def _card(self, msg):
        return self.env["distri.mp.collection"].sudo().search(
            [("kind", "=", "verify"), ("receipt_id", "=", msg.id)], order="id desc", limit=1)

    def _credit(self, msg, user=None, amount=None, partner=None, confirmed=False):
        card = self._card(msg)
        return self.env["distri.mp.collection"].with_user(user or self.seller).browse(
            card.id)._distri_credit(partner or card.partner_id, amount or card.amount,
                                    confirmed=confirmed)

    def _balance(self, partner=None):
        return (partner or self.partner)._distri_statement()["balance"]

    def _mp_payments(self, partner=None):
        journal = self.env["account.journal"]._distri_mp_journal()
        return self.env["account.payment"].sudo().search([
            ("journal_id", "=", journal.id), ("partner_id", "=", (partner or self.partner).id),
            ("state", "not in", ("draft", "canceled"))])


@tagged("post_install", "-at_install", "distri_v126")
class TestV126Acreditar(CreditMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_DRIVER_RULE, "")
        cls.env.user.sudo().write({"group_ids": [
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
        cls.seller = user("vendedor_v126", "distri_pedido.grupo_vendedor")
        cls.cashier = user("cajero_v126", "distri_pedido.grupo_cajero")
        cls.admin_user = user("admin_v126", "distri_pedido.grupo_administracion")
        cls.manager = user("gerente_v126", "distri_pedido.grupo_gerencia")
        cls.sysadmin = user("sistemas_v126", "base.group_system")
        cls.packer = user("armador_v126", "distri_pedido.grupo_armador")
        # Italo / Simón: driver and seller
        cls.dual = user("italo_v126", "distri_pedido.grupo_reparto,distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V126",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Prueba V126",
                                                           "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Report = cls.env["distri.mp.report"]
        cls.product_a.invoice_policy = "order"

    # ------------------------------------------------------------ helpers

    def _invoice(self, amount, partner=None, day=None):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": (partner or self.partner).id,
            "invoice_date": day or fields.Date.today(),
            "invoice_line_ids": [Command.create({
                "name": "mercadería", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def _report(self, *rows):
        header = ("TRANSACTION_DATE;SOURCE_ID;PAYMENT_METHOD_TYPE;TRANSACTION_TYPE;"
                  "TRANSACTION_AMOUNT;PAYER_NAME;PAYMENT_METHOD;PAYER_ID_TYPE;PAYER_ID_NUMBER")
        lines = [header]
        for source, when, amount, cuit in rows:
            local = _local(when).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
            lines.append(";".join([local, source, "bank_transfer", "SETTLEMENT",
                                   "%.2f" % amount, "PAGADOR PRUEBA", "bank_transfer",
                                   "CUIT" if cuit else "", cuit or ""]))
        CreditMixin.seq += 1
        return self.Report._import_file("v126-%s.csv" % CreditMixin.seq,
                                        ("\n".join(lines) + "\n").encode())

    def _income(self, source):
        return self.Collection.sudo().search([("source_id", "=", source)])

    # ------------------------------------------------------------ crediting

    def test_columns_and_reminder(self):
        self.assertIn(("verify", "Para acreditar"), COLUMNS)
        self.assertIn(("credited", "Acreditado · a verificar"), COLUMNS)
        self.assertEqual(CREDIT_REMINDER, "Se acredita en el momento: revisá importe, cliente y "
                                          "que el comprobante sea real.")

    def test_staff_credit_creates_the_payment_at_once(self):
        self._invoice(50000)
        msg = self._receipt(30000, operation="126000000001")
        card = self._card(msg)
        self.assertEqual(card.column, "verify")
        self.assertEqual(card.credit_flow, "staff")
        self.assertEqual(self._balance(), 50000)
        payment = self._credit(msg)
        self.assertEqual(payment.journal_id, self.env["account.journal"]._distri_mp_journal())
        self.assertIn(payment.state, ("in_process", "paid"))
        self.assertEqual(payment.amount, 30000)
        self.assertIn("126000000001", payment.memo)
        self.assertIn("acreditado", payment.memo)
        self.assertEqual(self._balance(), 20000, "the balance goes down at once")
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.payment_id, payment)
        self.assertEqual(card.credited_by_id, self.seller)
        self.assertTrue(card.credited_at)
        self.assertEqual(card.audit_state, "pendiente")
        self.assertEqual(card.column, "credited")
        self.assertEqual(msg.distri_receipt_state, "matched")
        self.assertEqual(payment.distri_audit_state, "pendiente")
        self.assertIn(payment, self.env["account.payment"].search(
            [("distri_audit_state", "=", "pendiente")]))
        # the staff statement marks it, the customer sees a plain payment
        html = self.partner.distri_statement_html
        self.assertIn("a verificar con MP", html)
        self.assertNotIn("Pago confirmado", html)

    def test_who_credits(self):
        for user in (self.seller, self.cashier, self.admin_user, self.manager, self.sysadmin,
                     self.driver, self.dual):
            self.assertTrue(can_credit(user), user.login)
        self.assertFalse(can_credit(self.packer))
        for user in (self.admin_user, self.manager, self.sysadmin):
            self.assertTrue(can_approve(user), user.login)
        for user in (self.seller, self.cashier, self.driver, self.packer):
            self.assertFalse(can_approve(user), user.login)
        for n, user in enumerate((self.seller, self.cashier, self.admin_user, self.manager,
                                  self.sysadmin, self.driver)):
            msg = self._receipt(1000 + 10 * n, user=user)
            self._credit(msg, user=user)
            self.assertEqual(self._card(msg).credited_by_id, user)
        msg = self._receipt(2000, user=self.packer)
        with self.assertRaises(UserError):
            self._credit(msg, user=self.packer)
        self.assertEqual(self._card(msg).state, "draft")

    def test_customer_receipt_waits_for_an_approver(self):
        msgs = [self._receipt(3000 + 10 * n, customer=True) for n in range(4)]
        card = self._card(msgs[0])
        self.assertEqual(card.credit_flow, "customer")
        self.assertEqual(card.column, "verify", "«Para acreditar»")
        for user in (self.cashier, self.seller, self.driver):
            with self.assertRaisesRegex(UserError, "lo mandó el cliente"):
                self._credit(msgs[0], user=user)
            self.assertFalse(card.with_user(user).sudo().can_credit)
        self.assertEqual(card.state, "draft")
        for msg, user in zip(msgs[1:], (self.admin_user, self.manager, self.sysadmin)):
            self._credit(msg, user=user)
            self.assertEqual(self._card(msg).state, "confirmed")

    def test_credit_balance_warning_and_threshold(self):
        msg = self._receipt(150000)
        with self.assertRaises(CreditWarning) as caught:
            self._credit(msg)
        self.assertIn("El cliente va a quedar con $ 150.000 a favor. ¿Acreditar igual?",
                      caught.exception.warnings)
        self.assertEqual(self._card(msg).state, "draft")
        self._credit(msg, confirmed=True)
        self.assertEqual(self._card(msg).state, "confirmed", "approved anyway: no cap")
        self.env["ir.config_parameter"].sudo().set_param(PARAM_CREDIT_WARNING, "500000")
        msg = self._receipt(200000, partner=self.other_partner)
        self._credit(msg)
        self.assertEqual(self._card(msg).state, "confirmed", "under the threshold")

    def test_amount_not_read_or_different_needs_confirmation(self):
        unread = self._receipt(0, read=0)
        with self.assertRaisesRegex(CreditWarning, "No se pudo leer el importe"):
            self._credit(unread, amount=4500)
        self._credit(unread, amount=4500, confirmed=True)
        self.assertEqual(self._card(unread).amount, 4500)
        self.assertEqual(unread.distri_amount, 4500)
        other = self._receipt(7000)
        with self.assertRaisesRegex(CreditWarning, "no es el leído"):
            self._credit(other, amount=7700)

    def test_operation_already_credited_is_refused(self):
        first = self._receipt(9000, operation="126000000011")
        self._credit(first)
        # the exact copy is closed as «Ya recibido»; one OCR slip away, refused
        copy = self._receipt(9000, operation="126000000011")
        self.assertEqual(copy.distri_receipt_state, "duplicate")
        slip = self._receipt(9000, operation="126000000019")
        with self.assertRaisesRegex(UserError, "ya se acreditó"):
            self._credit(slip)

    def test_same_image_is_refused(self):
        raw = _png(7)
        first = self._receipt(9100, raw=raw)
        second = self._receipt(9200, raw=raw, when=fields.Datetime.now() - timedelta(minutes=9))
        self._credit(first)
        with self.assertRaisesRegex(UserError, "misma imagen"):
            self._credit(second)

    def test_other_destination_is_refused(self):
        msg = self._receipt(9300, extra={"destination": "otra.cuenta.prueba",
                                         "dest_name": "Proveedor Prueba"})
        with self.assertRaisesRegex(UserError, "otra cuenta"):
            self._credit(msg)

    def test_old_receipt_needs_an_approver(self):
        msg = self._receipt(9400, when=fields.Datetime.now() - timedelta(days=5))
        with self.assertRaisesRegex(UserError, "más viejo que ayer"):
            self._credit(msg, user=self.seller)
        with self.assertRaisesRegex(CreditWarning, "más viejo que ayer"):
            self._credit(msg, user=self.admin_user)
        payment = self._credit(msg, user=self.admin_user, confirmed=True)
        self.assertEqual(payment.date, _local(fields.Datetime.now() - timedelta(days=5)).date(),
                         "dated on the receipt's day")

    def test_undo_cancels_the_payment(self):
        msg = self._receipt(9500)
        payment = self._credit(msg)
        card = self._card(msg)
        card.action_undo()
        self.assertEqual(payment.state, "canceled")
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.credited_at)
        self.assertFalse(card.audit_state)
        self.assertEqual(card.column, "verify")
        self.assertEqual(msg.distri_receipt_state, "pending")
        self.assertEqual(self._balance(), 0)

    def test_online_history_invoice_keeps_the_v125_way(self):
        history = self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": "2025-11-01", "comprobante": "0001/2025/00777",
            "tipo": "factura", "debe": 40000})
        lab = self._invoice(25000, day="2026-01-10")
        msg = self._receipt(40000)
        card = self._card(msg)
        card.pay_invoice_id = history
        payment = self._credit(msg, confirmed=True)
        self.assertEqual(payment.distri_history_invoice_id, history)
        self.assertTrue(payment.memo.endswith("0001/2025/00777"))
        self.assertEqual(lab.amount_residual, 25000, "never oldest first")
        row = next(r for r in self.partner._distri_statement_rows()
                   if r.get("history_id") == history.id)
        self.assertFalse(row["owed"], "the red clears on that row")
        self.assertEqual(self._balance(), 25000)

    def test_bank_twin_warning_and_v124_guard(self):
        bank = self.company_data["default_journal_bank"]
        manual = self.env["account.payment"].sudo().create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": self.partner.id,
            "amount": 12345, "journal_id": bank.id, "date": fields.Date.today()})
        manual.action_post()
        msg = self._receipt(12345)
        with self.assertRaisesRegex(CreditWarning, "cargado a mano"):
            self._credit(msg)
        # the v124 guard: an income of the report ties to the manual payment
        income = self.Collection.sudo().create({
            "source_id": "126999000001", "date": fields.Datetime.now(), "amount": 12345,
            "method_type": "bank_transfer", "payer_name": "PAGADOR PRUEBA",
            "partner_id": self.partner.id, "proposal": "manual"})
        # 19.0.1.129.0: proposed, a person confirms the tie
        with self.assertRaises(UserError):
            income.action_confirm()
        income.action_tie_bank_twin()
        self.assertEqual(income.payment_id, manual)
        self.assertFalse(self._mp_payments())

    # ------------------------------------------------------------ the report

    def test_report_exact_operation_ties_and_confirms(self):
        msg = self._receipt(30000, operation="126000000021")
        card = self._card(msg)
        payment = self._credit(msg)
        row = next(r for r in self.partner._distri_statement_rows() if r.get("card_id"))
        self.assertFalse(row["confirmed"], "no legend before the report")
        self._report(("126000000021", fields.Datetime.now() - timedelta(minutes=4), 30000, ""))
        income = self._income("126000000021")
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, payment)
        self.assertEqual(card.income_id, income)
        self.assertEqual(card.audit_state, "verificado")
        self.assertEqual(self._mp_payments(), payment, "no second payment")
        self.assertEqual(self._balance(), -30000)
        row = next(r for r in self.partner._distri_statement_rows() if r.get("card_id"))
        self.assertTrue(row["confirmed"])
        self.assertIn("Pago confirmado", self.partner.distri_statement_html)
        self.assertNotIn("a verificar con MP", self.partner.distri_statement_html)
        app_row = self.env["distri.account.app"]._app_row(row, {})
        self.assertIs(app_row["confirmed"], True)
        with self.assertRaises(UserError):
            income.action_undo()

    def test_report_operation_with_one_slip_ties(self):
        msg = self._receipt(31000, operation="126000000031")
        payment = self._credit(msg)
        self._report(("126000000039", fields.Datetime.now() - timedelta(minutes=4), 31000, ""))
        income = self._income("126000000039")
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, payment)
        self.assertEqual(income.proposal, "credited")
        self.assertEqual(self._card(msg).audit_state, "verificado")

    def test_report_same_amount_and_time_ties(self):
        when = fields.Datetime.now() - timedelta(minutes=30)
        msg = self._receipt(32000, when=when)
        payment = self._credit(msg)
        self._report(("126000000041", when + timedelta(minutes=1), 32000, ""))
        income = self._income("126000000041")
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, payment)
        self.assertEqual(self._mp_payments(), payment)

    def test_report_ambiguous_goes_to_review(self):
        when = fields.Datetime.now() - timedelta(minutes=30)
        first = self._receipt(33000, when=when)
        second = self._receipt(33000, partner=self.other_partner,
                               when=when + timedelta(seconds=40))
        self._credit(first)
        self._credit(second)
        self._report(("126000000051", when + timedelta(seconds=20), 33000, ""))
        income = self._income("126000000051")
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.column, "review")
        self.assertEqual(income.proposal, "credited")
        self.assertEqual(income.credited_candidate_count, 2)
        income.partner_id = self.partner
        with self.assertRaisesRegex(UserError, "ya acreditado"):
            income.action_confirm()
        # «Confirmar todos» never takes it
        self.assertIn("acreditado", income._bulk_problem())
        action = income.action_open_tie_credited()
        wizard = self.env["distri.mp.tie.credited"].browse(action["res_id"])
        wizard.card_id = self._card(first)
        wizard.action_apply()
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, self._card(first).payment_id)
        self.assertEqual(self._card(first).audit_state, "verificado")
        self.assertEqual(self._card(second).audit_state, "pendiente")

    def test_report_other_amount_goes_to_review(self):
        msg = self._receipt(34000, operation="126000000061")
        payment = self._credit(msg)
        self._report(("126000000061", fields.Datetime.now() - timedelta(minutes=4), 34500, ""))
        income = self._income("126000000061")
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.column, "review")
        with self.assertRaises(UserError):
            income.action_confirm()
        self.assertEqual(self._mp_payments(), payment)
        # 19.0.1.129.0: the audit marks the other amount «diferencia»
        self.assertEqual(self._card(msg).audit_state, "diferencia")

    def test_income_already_in_the_report_refuses_crediting(self):
        when = fields.Datetime.now() - timedelta(minutes=30)
        self._report(("126000000071", when, 35000, ""))
        msg = self._receipt(35000, operation="126000000071", when=when)
        self.assertNotEqual(msg.distri_receipt_state, "pending", "the income took it")
        # an income loaded without taking the receipt (same operation, or the
        # same amount within 2 minutes): the person works from that card
        for source, operation, amount in (("126000000072", "126000000072", 35500),
                                          ("126000000073", None, 35600)):
            pending = self._receipt(amount, operation=operation, when=when)
            self.Collection.sudo().create({
                "source_id": source, "date": when + timedelta(seconds=30), "amount": amount,
                "method_type": "bank_transfer", "payer_name": "PAGADOR PRUEBA"})
            with self.assertRaisesRegex(UserError, "ya está en el reporte"):
                self._credit(pending)
            self.assertEqual(self._card(pending).state, "draft")

    def test_cash_payment_never_shows_the_legend(self):
        cash = self.company_data["default_journal_cash"]
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": self.partner.id,
            "amount": 5000, "journal_id": cash.id, "date": fields.Date.today()})
        payment.action_post()
        rows = self.partner._distri_statement_rows()
        self.assertTrue(rows)
        self.assertFalse(any(r["confirmed"] for r in rows))
        self.assertNotIn("Pago confirmado", self.partner.distri_statement_html)

    # ------------------------------------------------------------ drivers

    def _app_receipt(self, amount, read, driver=None, partner=None):
        driver = driver or self.driver
        order = self._order(partner or self.partner, self.product_a, qty=1, price=amount)
        route = self._route(order, driver=driver)
        CreditMixin.seq += 1
        Message = self.env["distri.whatsapp.message"].sudo()
        msg = Message._distri_app_receipt_create(
            route.stop_ids, driver, base64.b64encode(_png(CreditMixin.seq + 3000)).decode(),
            _uuid(), fields.Datetime.now() - timedelta(minutes=2), amount)
        reading = {"amount": read, "amount_confidence": CONFIDENCE_HIGH,
                   "date": _local(fields.Datetime.now() - timedelta(minutes=3))}
        with self._reading(reading):
            Message._distri_read_app_receipts()
        msg.invalidate_recordset()
        return msg

    def test_driver_receipt_credits_at_once_by_default(self):
        msg = self._app_receipt(8000, 8000)
        card = self._card(msg)
        self.assertEqual(card.credit_flow, "driver")
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.credited_by_id, self.driver)
        self.assertEqual(card.audit_state, "pendiente")
        # a reading that differs waits in «Para acreditar» for anyone who credits
        other = self._app_receipt(8100, 8900, partner=self.other_partner)
        card = self._card(other)
        self.assertEqual(card.state, "draft")
        self.assertEqual(card.column, "verify")
        self.assertTrue(card.with_user(self.seller).sudo()._distri_user_can_credit(self.seller))

    def test_driver_rule_rendicion(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_DRIVER_RULE, "rendicion")
        msg = self._app_receipt(8200, 8200, driver=self.dual)
        card = self._card(msg)
        self.assertEqual(card.state, "draft", "never by itself")
        # Italo is a seller too, but this is his driver's flow
        with self.assertRaisesRegex(UserError, "repartidor"):
            self._credit(msg, user=self.dual)
        with self.assertRaises(UserError):
            self._credit(msg, user=self.cashier)
        # outside the driver's flow he credits as a seller
        upload = self._receipt(8300, user=self.dual)
        self._credit(upload, user=self.dual)
        self.assertEqual(self._card(upload).state, "confirmed")
        self._credit(msg, user=self.admin_user)
        self.assertEqual(card.state, "confirmed")

    def test_driver_rule_coincide(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_DRIVER_RULE, "coincide")
        same = self._app_receipt(8400, 8400, driver=self.dual)
        self.assertEqual(self._card(same).state, "confirmed")
        self.assertEqual(self._card(same).credited_by_id, self.dual)
        differs = self._app_receipt(8500, 8000, driver=self.dual, partner=self.other_partner)
        card = self._card(differs)
        self.assertEqual(card.state, "draft")
        with self.assertRaises(UserError):
            self._credit(differs, user=self.dual)
        self._credit(differs, user=self.manager, amount=8000, confirmed=True)
        self.assertEqual(card.state, "confirmed")

    # ------------------------------------------------------------ notice

    def test_notice_for_who_credits_until_acknowledged(self):
        self.assertTrue(self.seller._distri_credit_notice_pending())
        self.assertTrue(self.driver._distri_credit_notice_pending())
        self.assertFalse(self.packer._distri_credit_notice_pending())
        self.assertTrue(self.seller.with_user(self.seller)._distri_credit_notice_seen())
        self.assertTrue(self.seller.distri_aviso_acreditar_visto)
        self.assertFalse(self.seller._distri_credit_notice_pending())
        self.assertFalse(self.packer._distri_credit_notice_seen())
        self.assertFalse(self.packer.distri_aviso_acreditar_visto)
        self.assertIn("distri_aviso_acreditar_visto", self.seller.SELF_WRITEABLE_FIELDS)


@tagged("post_install", "-at_install", "distri_v126")
class TestV126AcreditarPaginas(CreditMixin, HttpCase):
    """The upload screen's «Acreditar» and the notice on /tablero."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = new_test_user(cls.env, login="vendedor_v126_web", name="Vendedor V126 Web",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.packer = new_test_user(cls.env, login="armador_v126_web", name="Armador V126 Web",
                                   groups="base.group_user,distri_pedido.grupo_armador")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V126 Web",
                                                     "company_type": "company"})

    def test_upload_screen_credits(self):
        msg = self._receipt(15000, user=self.seller)
        self.authenticate("vendedor_v126_web", "vendedor_v126_web")
        page = self.url_open("/comprobantes/%d" % msg.id)
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("Acreditar", page.text)
        self.assertIn(CREDIT_REMINDER, page.text)
        csrf = CSRF.search(page.text).group(1)
        res = self.url_open("/comprobantes/%d/acreditar" % msg.id, data={
            "csrf_token": csrf, "importe": "15.000", "cliente_id": str(self.partner.id)})
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertIn("Acreditado", res.text)
        card = self._card(msg)
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.credited_by_id, self.seller)

    def test_upload_screen_asks_before_a_big_credit(self):
        msg = self._receipt(250000, user=self.seller)
        self.authenticate("vendedor_v126_web", "vendedor_v126_web")
        page = self.url_open("/comprobantes/%d" % msg.id)
        csrf = CSRF.search(page.text).group(1)
        data = {"csrf_token": csrf, "importe": "250.000", "cliente_id": str(self.partner.id)}
        res = self.url_open("/comprobantes/%d/acreditar" % msg.id, data=data)
        self.assertIn("a favor", res.text)
        self.assertIn("Acreditar igual", res.text)
        self.assertEqual(self._card(msg).state, "draft")
        res = self.url_open("/comprobantes/%d/acreditar" % msg.id,
                            data=dict(data, confirmado="1"))
        self.assertEqual(self._card(msg).state, "confirmed")

    def test_tablero_notice(self):
        self.authenticate("admin", "admin")
        admin = self.env.ref("base.user_admin")
        admin.sudo().distri_aviso_acreditar_visto = False
        if admin._distri_landing():
            self.skipTest("admin lands elsewhere in this database")
        page = self.url_open("/tablero")
        self.assertIn(CREDIT_NOTICE, page.text)
        csrf = CSRF.search(page.text).group(1)
        self.url_open("/tablero/aviso-acreditar", data={"csrf_token": csrf, "volver": "/tablero"})
        admin.invalidate_recordset()
        self.assertTrue(admin.distri_aviso_acreditar_visto)
        self.assertNotIn(CREDIT_NOTICE, self.url_open("/tablero").text)

    def test_notice_never_for_who_does_not_credit(self):
        self.assertFalse(self.packer._distri_credit_notice_pending())
        self.authenticate("armador_v126_web", "armador_v126_web")
        page = self.url_open("/tablero")
        self.assertNotIn(CREDIT_NOTICE, page.text)
