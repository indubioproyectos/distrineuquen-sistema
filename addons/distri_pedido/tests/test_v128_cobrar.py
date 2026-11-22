"""19.0.1.128.0: «Cobrar» in one step (models/cobrar.py, models/cobrar_wizard.py).

- Cash only: a cashier's cash goes to the caja journal and his open caja at
  once, reconciled with the invoice.
- Transfer only: the receipt is mandatory; with it, the v126 crediting runs
  (payment in «Mercado Pago», card «confirmed» and «a verificar»).
- Mixed: cash + transfer on one invoice, one distri.cobro with both payments;
  the payment opens its receipt card and the card the collection's payments.
- «Factura de Online»: unreconciled payments with the number in the memo and
  distri_history_invoice_id; the history row's red clears.
- No open caja: collected anyway, with the v124 notice; the next caja takes it.
- A cashier does not see «Pagar» nor «Banco»; gerencia does.
- /cobro by role; the OCR time cap.
- The backend wizard: questions come back in the wizard, then it collects.
Synthetic data only.
"""
import base64
import time
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models import ocr_comprobante
from odoo.addons.distri_pedido.models.acreditar import CreditWarning
from odoo.addons.distri_pedido.models.caja import PARAM_INVOICE_CASH_SINCE
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.caja_pago import NO_OPEN_CAJA_NOTICE
from odoo.addons.distri_pedido.models.cobrar import RECEIPT_MISSING, can_collect
from odoo.addons.distri_pedido.models.cobrar_wizard import (
    PARAM_ADMIN_CASHIER_KEEPS_BANK, native_pay_restricted,
)
from odoo.addons.distri_pedido.models.cobro_efectivo import OverDebtWarning
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.ocr_comprobante import (
    CONFIDENCE_HIGH, PARAM_OCR_SECONDS, ocr_deadline, ocr_gave_up, ocr_seconds,
)
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CSRF, CreditMixin, _local, _png


class CobrarMixin(CreditMixin):

    def _read_as(self, amount=None, **extra):
        """The reading patched: `amount`, or (None) what each receipt's
        uploader typed; a distinct moment per receipt (two receipts of the
        same amount and time are copies)."""
        Message = type(self.env["distri.whatsapp.message"])
        now = fields.Datetime.now()

        def reading(msg):
            values = {"amount": amount if amount is not None else msg.distri_amount_typed,
                      "amount_confidence": CONFIDENCE_HIGH,
                      "date": _local(now - timedelta(minutes=2, seconds=msg.id % 3000))}
            values.update(extra)
            return "texto leído", values
        return patch.object(Message, "_distri_file_reading", reading)

    def _file(self):
        CreditMixin.seq += 1
        return ("t%s.png" % CreditMixin.seq, _png(CreditMixin.seq + 5000))

    def _collect(self, user, destination="account", invoice=None, history=None, cash=0,
                 transfers=(), read=None, **flags):
        """transfers: amounts (each with a new file) or dicts for collect_receipts."""
        Cobro = self.env["distri.cobro"].with_user(user)
        slots = [t if isinstance(t, dict) else {"amount": "%d" % t, "file": self._file()}
                 for t in transfers]
        with self._read_as(read):
            stored = Cobro.collect_receipts(self.partner.id, slots)
        return Cobro.collect(self.partner.id, destination,
                             invoice_id=invoice.id if invoice else False,
                             history_id=history.id if history else False,
                             cash_amount=str(cash) if cash else "", transfers=stored, **flags)


@tagged("post_install", "-at_install", "distri_v128")
class TestV128Cobrar(CobrarMixin, CajaMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        Param = cls.env["ir.config_parameter"].sudo()
        Param.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.bank_journal = cls.company_data["default_journal_bank"]
        Param.set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        Param.set_param(PARAM_INVOICE_CASH_SINCE, "")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Cobrar V128",
                                                     "company_type": "company"})
        cls.mp_journal = cls.env["account.journal"]._distri_mp_journal(company)

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, company_id=company.id,
                                 company_ids=[Command.set(company.ids)])
        cls.packer = user("armador_v128", "distri_pedido.grupo_armador")
        # like the company's cashier: administración, caja and cobros
        cls.admin_cashier = user("admin_cajero_v128", "distri_pedido.grupo_administracion,"
                                                      "distri_pedido.grupo_cajero,"
                                                      "distri_pedido.grupo_cobros")
        cls.manager_cashier = user("gerente_cajero_v128", "distri_pedido.grupo_gerencia,"
                                                          "distri_pedido.grupo_cajero")

    def _invoice(self, amount):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create({"name": "mercadería", "quantity": 1,
                                                 "price_unit": amount,
                                                 "tax_ids": [Command.clear()]})]})
        invoice.action_post()
        return invoice

    def _history(self, number, amount):
        return self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": "2025-11-01", "comprobante": number,
            "tipo": "factura", "debe": amount})

    def _movements(self, payment):
        return self.env["distri.caja.movimiento"].sudo().search([("payment_id", "=", payment.id)])

    # ------------------------------------------------------------ cash

    def test_cash_only_enters_the_caja_and_pays_the_invoice(self):
        caja = self._caja().open_session("0")
        invoice = self._invoice(50000)
        cobro = self._collect(self.cashier, "lab", invoice=invoice, cash="50.000")
        payment = cobro.cash_payment_id
        self.assertEqual(payment.journal_id, self.cash_journal, "the caja journal (CSH1)")
        self.assertEqual(self._movements(payment).caja_id, caja, "in the open caja at once")
        self.assertEqual(cobro.caja_id, caja)
        self.assertFalse(cobro.caja_notice)
        self.assertEqual(invoice.payment_state in ("paid", "in_payment"), True)
        self.assertFalse(self.env["distri.route"].sudo().search(
            [("payment_ids", "in", payment.ids)]), "never in a depot box")
        self.assertEqual((cobro.cash_amount, cobro.transfer_amount, cobro.amount),
                         (50000, 0, 50000))
        self.assertIn("Efectivo $ 50.000 · factura %s" % invoice.name, cobro.memo)

    def test_no_open_caja_collects_with_the_notice(self):
        invoice = self._invoice(30000)
        cobro = self._collect(self.cashier, "lab", invoice=invoice, cash=30000)
        payment = cobro.cash_payment_id
        self.assertEqual(cobro.caja_notice, NO_OPEN_CAJA_NOTICE)
        self.assertFalse(self._movements(payment))
        self.assertTrue(any(NO_OPEN_CAJA_NOTICE in (m.body or "") for m in payment.message_ids))
        caja = self._caja().open_session("0")
        self.assertEqual(self._movements(payment).caja_id, caja, "the next caja takes it")

    def test_a_seller_keeps_his_depot_box(self):
        invoice = self._invoice(20000)
        cobro = self._collect(self.seller, "lab", invoice=invoice, cash=20000)
        self.assertEqual(cobro.cash_collection_id.payment_id, cobro.cash_payment_id)
        self.assertEqual(cobro.cash_payment_id.journal_id, self.depot_journal)
        self.assertFalse(cobro.caja_notice)

    # ------------------------------------------------------------ transfers

    def test_transfer_needs_its_receipt(self):
        with self.assertRaises(UserError) as caught:
            self.env["distri.cobro"].with_user(self.cashier).collect_receipts(
                self.partner.id, [{"amount": "1000", "file": None}])
        self.assertEqual(caught.exception.args[0], RECEIPT_MISSING % 1)

    def test_transfer_only_credits_through_v126(self):
        invoice = self._invoice(80000)
        cobro = self._collect(self.cashier, "lab", invoice=invoice, transfers=[80000])
        card = cobro.card_ids
        self.assertEqual(len(card), 1)
        self.assertEqual((card.state, card.audit_state), ("confirmed", "pendiente"))
        self.assertEqual(card.credited_by_id, self.cashier)
        self.assertEqual(card.payment_id.journal_id, self.mp_journal)
        self.assertEqual(cobro.payment_ids, card.payment_id)
        self.assertEqual(card.receipt_id.distri_receipt_state, "matched")
        self.assertIn(invoice.name, card.payment_id.memo)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        self.assertFalse(cobro.cash_payment_id)

    def test_mixed_one_invoice_and_links(self):
        self._caja().open_session("0")
        invoice = self._invoice(100000)
        cobro = self._collect(self.cashier, "lab", invoice=invoice, cash=40000,
                              transfers=[60000])
        self.assertEqual(len(cobro.payment_ids), 2)
        self.assertEqual(cobro.amount, 100000)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        card = cobro.card_ids
        cash = cobro.cash_payment_id
        # the payment opens its receipt card (the cash one too: same collection)
        for payment in cobro.payment_ids:
            self.assertEqual(payment.distri_cobro_id, cobro)
            self.assertEqual(payment.distri_receipt_card_ids, card)
            action = payment.action_distri_open_receipt()
            self.assertEqual((action["res_model"], action["res_id"]),
                             ("distri.mp.collection", card.id))
        # the card opens the payments of the collection
        self.assertEqual(card.distri_cobro_payment_count, 2)
        action = card.action_distri_open_payments()
        self.assertEqual(sorted(action["domain"][0][2]), sorted((cash | card.payment_id).ids))
        self.assertEqual(self._movements(cash).kind, "factura")
        self.assertFalse(self._movements(card.payment_id), "the transfer is not cash")

    def test_two_transfers_on_account(self):
        self._invoice(150000)
        cobro = self._collect(self.seller, "account", transfers=[50000, 30000])
        self.assertEqual(len(cobro.card_ids), 2)
        self.assertEqual(cobro.transfer_amount, 80000)
        self.assertEqual(round(self._balance(), 2), 70000)

    def test_over_the_invoice_is_refused(self):
        invoice = self._invoice(10000)
        with self.assertRaisesRegex(UserError, "supera lo que falta pagar"):
            self._collect(self.cashier, "lab", invoice=invoice, cash=5000, transfers=[6000])

    def test_over_the_debt_asks_first(self):
        with self.assertRaises(OverDebtWarning):
            self._collect(self.cashier, "account", cash=1000)
        cobro = self._collect(self.cashier, "account", cash=1000, over_ok=True)
        self.assertEqual(round(self._balance(), 2), -1000)
        self.assertTrue(cobro)

    def test_credit_warning_asks_first(self):
        invoice = self._invoice(70000)
        slot = {"amount": "70000", "file": self._file()}
        Cobro = self.env["distri.cobro"].with_user(self.cashier)
        # read 69.000: the amount differs from the one typed
        with self._read_as(69000):
            stored = Cobro.collect_receipts(self.partner.id, [slot])
        with self.assertRaises(CreditWarning):
            Cobro.collect(self.partner.id, "lab", invoice_id=invoice.id, transfers=stored)
        receipt = stored[0]["receipt"]
        self.assertEqual(receipt.distri_receipt_state, "pending", "kept for the confirmation")
        # confirmed, with the receipt stored before (not attached again)
        again = Cobro.collect_receipts(self.partner.id,
                                       [{"amount": "70000", "receipt_id": receipt.id}])
        cobro = Cobro.collect(self.partner.id, "lab", invoice_id=invoice.id, transfers=again,
                              credit_ok=True)
        self.assertEqual(cobro.card_ids.receipt_id, receipt)

    # ------------------------------------------------------------ Odoo Online

    def test_online_invoice_destination(self):
        self._caja().open_session("0")
        history = self._history("0001/2026/08128", 90000)
        cobro = self._collect(self.cashier, "online", history=history, cash=30000,
                              transfers=[40000])
        self.assertEqual(len(cobro.payment_ids), 2)
        for payment in cobro.payment_ids:
            self.assertEqual(payment.distri_history_invoice_id, history)
            self.assertTrue(payment.memo.endswith("· 0001/2026/08128"), payment.memo)
            receivable = payment.move_id.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable")
            self.assertFalse(receivable.reconciled, "never reconciled nor oldest first")
        row = next(r for r in self.partner._distri_statement_rows()
                   if r["history_id"] == history.id)
        self.assertEqual(row["owed"], 20000)
        data = self.partner._distri_statement()
        owed = sum(r.get("owed") or 0.0 for r in data["rows"])
        self.assertAlmostEqual(owed, max(data["balance"], 0.0), 2, "red = balance")

    # ------------------------------------------------------------ cashier without Pagar/Banco

    def test_cashier_loses_pay_and_bank(self):
        invoice = self._invoice(10000)
        for user, restricted in ((self.cashier, True), (self.admin_cashier, True),
                                 (self.manager_cashier, False), (self.seller, False),
                                 (self.env.user, False)):
            self.assertEqual(native_pay_restricted(user), restricted, user.login)
            self.assertEqual(invoice.with_user(user).sudo().distri_native_pay_allowed,
                             not restricted, user.login)
        self.assertTrue(invoice.with_user(self.cashier).sudo().distri_can_cobrar)
        self.assertFalse(invoice.with_user(self.packer).sudo().distri_can_cobrar)
        with self.assertRaises(AccessError):
            invoice.with_user(self.cashier).action_register_payment()
        with self.assertRaises(AccessError):
            self.env["account.payment.register"].with_user(self.cashier).with_context(
                active_model="account.move", active_ids=invoice.ids).create({})
        with self.assertRaisesRegex(AccessError, "diario de banco"):
            self.env["account.payment"].with_user(self.admin_cashier).create({
                "payment_type": "inbound", "partner_type": "customer",
                "partner_id": self.partner.id, "amount": 10,
                "journal_id": self.bank_journal.id})
        # gerencia keeps them; the parameter gives them back to administración + cajero
        action = invoice.with_user(self.manager_cashier).action_register_payment()
        self.assertEqual(action["res_model"], "account.payment.register")
        self.env["ir.config_parameter"].sudo().set_param(PARAM_ADMIN_CASHIER_KEEPS_BANK, "1")
        self.assertFalse(native_pay_restricted(self.admin_cashier))
        self.assertTrue(native_pay_restricted(self.cashier))

    def test_cashier_register_wizard_without_bank(self):
        invoice = self._invoice(10000)
        self.env["ir.config_parameter"].sudo().set_param(PARAM_ADMIN_CASHIER_KEEPS_BANK, "")
        wizard = self.env["account.payment.register"].with_user(self.manager_cashier).with_context(
            active_model="account.move", active_ids=invoice.ids).create({})
        self.assertIn(self.bank_journal, wizard.available_journal_ids)
        Register = type(self.env["account.payment.register"])
        batch = wizard.batches[0]
        journals = Register._get_batch_available_journals(
            wizard.with_user(self.admin_cashier), batch)
        self.assertNotIn(self.bank_journal, journals)
        self.assertIn(self.cash_journal, journals)

    # ------------------------------------------------------------ roles

    def test_who_collects(self):
        for user, allowed in ((self.cashier, True), (self.seller, True),
                              (self.manager, True), (self.packer, False)):
            self.assertEqual(can_collect(user), allowed, user.login)
        with self.assertRaises(AccessError):
            self._collect(self.packer, "account", cash=10, over_ok=True)

    # ------------------------------------------------------------ OCR cap

    def test_ocr_time_cap(self):
        Param = self.env["ir.config_parameter"].sudo()
        self.assertEqual(ocr_seconds(self.env), 7.0)
        Param.set_param(PARAM_OCR_SECONDS, "100")
        self.assertEqual(ocr_seconds(self.env), 30.0)
        Param.set_param(PARAM_OCR_SECONDS, "x")
        self.assertEqual(ocr_seconds(self.env), 7.0)
        calls = []

        def slow(image, lang="spa", psm=3, timeout=40):
            calls.append(timeout)
            time.sleep(0.4)
            return []

        with patch.object(ocr_comprobante, "_tsv_words", slow), \
                patch.object(ocr_comprobante, "ocr_available", lambda: True):
            start = time.monotonic()
            with ocr_deadline(0.5):
                text, _conf, reading = ocr_comprobante.read_image(_png(7))
            elapsed = time.monotonic() - start
            self.assertTrue(ocr_gave_up())
        self.assertFalse(reading.get("amount"))
        self.assertLess(elapsed, 3.0, "gave up instead of running every pass")
        self.assertTrue(all(t <= 0.5 for t in calls), calls)
        # without a deadline (background reading) every pass keeps its own timeout
        self.assertIsNone(ocr_comprobante.ocr_time_left())

    def test_preview_gives_up_with_a_notice(self):
        with patch.object(ocr_comprobante, "_tsv_words", lambda *a, **k: []), \
                patch.object(type(self.env["distri.whatsapp.message"]), "_distri_read_raw",
                             lambda self, raw, degrees=0: (
                                 ocr_comprobante._deadline.__setattr__("gave_up", True)
                                 or ("", {}))):
            preview = self.env["distri.whatsapp.message"]._distri_upload_preview(_png(9))
        self.assertTrue(preview["gave_up"])
        self.assertEqual(preview["notice"], ocr_comprobante.OCR_GAVE_UP)

    # ------------------------------------------------------------ backend wizard
    # (as the company's cashiers: administración + cajero + cobros; a plain
    # cajero does not open invoices in the backend)

    def test_backend_wizard_from_the_invoice(self):
        self._caja(self.admin_cashier).open_session("0")
        invoice = self._invoice(60000)
        action = invoice.with_user(self.admin_cashier).action_distri_cobrar()
        wizard = self.env["distri.cobrar.wizard"].with_user(self.admin_cashier).browse(
            action["res_id"])
        self.assertEqual((wizard.destination, wizard.invoice_id), ("lab", invoice))
        self.assertIn(invoice, wizard.invoice_choice_ids)
        name, raw = self._file()
        wizard.write({"cash_amount": 20000, "line_ids": [Command.create({
            "receipt_file": base64.b64encode(raw), "receipt_filename": name,
            "amount": 40000})]})
        self.assertEqual(wizard.total, 60000)
        with self._read_as(40000):
            result = wizard.action_collect()
        self.assertEqual(result["tag"], "display_notification", result)
        self.assertIn(invoice.payment_state, ("paid", "in_payment"))
        cobro = self.env["distri.cobro"].search([("invoice_id", "=", invoice.id)])
        self.assertEqual(len(cobro.payment_ids), 2)

    def test_backend_wizard_question_then_collect(self):
        invoice = self._invoice(5000)
        action = invoice.with_user(self.admin_cashier).action_distri_cobrar()
        wizard = self.env["distri.cobrar.wizard"].with_user(self.admin_cashier).browse(
            action["res_id"])
        wizard.write({"destination": "account", "invoice_id": False, "cash_amount": 9000})
        result = wizard.action_collect()
        self.assertEqual(result["res_id"], wizard.id, "back to the wizard")
        self.assertEqual(wizard.question_field, "deuda_ok")
        self.assertIn("supera la deuda", wizard.question)
        self.assertFalse(self.env["distri.cobro"].search([("partner_id", "=", self.partner.id)]))
        wizard.confirm = True
        result = wizard.action_collect()
        self.assertEqual(result["tag"], "display_notification")
        self.assertEqual(result["params"]["type"], "warning", "no open caja: the notice")
        self.assertIn(NO_OPEN_CAJA_NOTICE, result["params"]["message"])

    def test_backend_wizard_shows_a_refusal(self):
        invoice = self._invoice(5000)
        action = invoice.with_user(self.admin_cashier).action_distri_cobrar()
        wizard = self.env["distri.cobrar.wizard"].with_user(self.admin_cashier).browse(
            action["res_id"])
        wizard.write({"line_ids": [Command.create({"amount": 1000})]})
        result = wizard.action_collect()
        self.assertEqual(result["res_id"], wizard.id)
        self.assertEqual(wizard.error_text, RECEIPT_MISSING % 1)


@tagged("post_install", "-at_install", "distri_v128")
class TestV128CobroHttp(CobrarMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)
        cls.partner = cls.env["res.partner"].create({"name": "Kiosco Cobro Http V128",
                                                     "company_type": "company"})
        cls.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.partner.id, "fecha": "2026-01-10",
            "comprobante": "0001/2026/98128", "tipo": "factura", "debe": 100000.0})
        cls.env["distri.route"]._depot_journal(cls.env.company)
        new_test_user(cls.env, login="vendedor_http_v128", name="Vendedor Http V128",
                      groups="base.group_user,distri_pedido.grupo_vendedor")
        new_test_user(cls.env, login="armador_http_v128", name="Armador Http V128",
                      groups="base.group_user,distri_pedido.grupo_armador")
        new_test_user(cls.env, login="interno_http_v128", name="Interno Http V128",
                      groups="base.group_user")

    def test_cobro_by_role(self):
        for login in ("armador_http_v128", "interno_http_v128"):
            self.authenticate(login, login)
            res = self.url_open("/cobro")
            self.assertEqual(res.status_code, 403, login)
            self.assertIn("Esta pantalla no es para tu usuario", res.text)
            res = self.url_open("/cobro/registrar", data={"cliente": self.partner.id,
                                                          "importe": "10"})
            self.assertIn(res.status_code, (400, 403))
            res = self.url_open("/cobro/leer", data={"giro": "0"})
            self.assertIn(res.status_code, (400, 403), login)
        self.authenticate("vendedor_http_v128", "vendedor_http_v128")
        self.assertEqual(self.url_open("/cobro").status_code, 200)

    def test_mixed_from_the_page(self):
        self.authenticate("vendedor_http_v128", "vendedor_http_v128")
        page = self.url_open("/cobro?cliente=%d" % self.partner.id).text
        self.assertIn('name="comprobante_1"', page)
        self.assertIn("Factura del sistema o de Online", page)
        history = self.env["distri.cuenta.movimiento"].sudo().search(
            [("comprobante", "=", "0001/2026/98128")])
        name, raw = self._file()
        data = {"csrf_token": CSRF.search(page).group(1), "cliente": self.partner.id,
                "importe": "30.000", "factura": "online-%d" % history.id,
                "importe_transf_1": "20.000"}
        with self._read_as(20000):
            res = self.url_open("/cobro/registrar", data=data,
                                files={"comprobante_1": (name, raw, "image/png")},
                                allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:800])
        cobro = self.env["distri.cobro"].search([("partner_id", "=", self.partner.id)])
        self.assertEqual((cobro.cash_amount, cobro.transfer_amount), (30000, 20000))
        self.assertEqual(cobro.history_invoice_id, history)
        done = self.url_open(res.headers["Location"]).text
        self.assertIn("Cobro registrado", done)
        # a transfer without its receipt is refused, the form shown again
        page = self.url_open("/cobro?cliente=%d" % self.partner.id).text
        data["csrf_token"] = CSRF.search(page).group(1)
        res = self.url_open("/cobro/registrar", data=dict(data, importe=""))
        self.assertEqual(res.status_code, 400)
        self.assertIn("adjuntá la foto o el PDF del comprobante", res.text)
