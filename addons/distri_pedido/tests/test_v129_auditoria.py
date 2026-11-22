"""19.0.1.129.0: full audit of the collections on every Mercado Pago report.

- The cascade per credited receipt: exact operation, operation with one OCR
  slip, amount and time, amount, customer and day; a tie goes to «Revisar».
- audit_state: pendiente, verificado, diferencia, sin_transferencia,
  revertido, anulado; «sin comprobante» on electronic payments.
- Duplicates: the same operation credited twice; same customer and amount
  within ±2 days. A report income that may be a credited receipt is never
  confirmed with a second payment.
- The 3-day alert (by report coverage) and the daily summary per person.
- Bank twins are proposed and confirmed by a person.
- «Pago a proveedor de Distri»: permissions, journal, out of the audit.
- The unique index of credited operations, also under concurrency.
- The SFTP fetcher.
Synthetic data only.
"""
import base64
import os
import shutil
import tempfile
import threading
import time
from contextlib import closing
from datetime import timedelta
from unittest.mock import patch

import psycopg2

from odoo import SUPERUSER_ID, Command, fields, sql_db
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
from odoo.addons.distri_pedido.models.auditoria import ALERT_SUMMARY, DAILY_SUMMARY
from odoo.addons.distri_pedido.models.cobros import PARAM_FOLDER
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin, _local, _png


@tagged("post_install", "-at_install", "distri_v129")
class TestV129Auditoria(CreditMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        params.set_param(PARAM_DRIVER_RULE, "")
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
        cls.seller = user("vendedor_v129", "distri_pedido.grupo_vendedor")
        cls.admin_user = user("admin_v129", "distri_pedido.grupo_administracion")
        cls.manager = user("gerente_v129", "distri_pedido.grupo_gerencia")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V129",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Prueba V129",
                                                           "company_type": "company"})
        cls.supplier = cls.env["res.partner"].create({"name": "Proveedor Prueba V129",
                                                      "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Report = cls.env["distri.mp.report"]
        cls.Anomaly = cls.env["distri.mp.anomaly"]
        cls.Audit = cls.env["distri.mp.audit"]
        cls.bank = cls.company_data["default_journal_bank"]

    # ------------------------------------------------------------ helpers

    def _invoice(self, amount, partner=None):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": (partner or self.partner).id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create({
                "name": "mercadería", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def _csv(self, *rows):
        header = ("TRANSACTION_DATE;SOURCE_ID;PAYMENT_METHOD_TYPE;TRANSACTION_TYPE;"
                  "TRANSACTION_AMOUNT;PAYER_NAME;PAYMENT_METHOD;PAYER_ID_TYPE;PAYER_ID_NUMBER")
        lines = [header]
        for row in rows:
            source, when, amount, cuit = row[:4]
            kind = row[4] if len(row) > 4 else "SETTLEMENT"
            local = _local(when).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
            lines.append(";".join([local, source, "bank_transfer", kind, "%.2f" % amount,
                                   "PAGADOR PRUEBA", "bank_transfer",
                                   "CUIT" if cuit else "", cuit or ""]))
        return ("\n".join(lines) + "\n").encode()

    def _report(self, *rows):
        CreditMixin.seq += 1
        return self.Report._import_file("v129-%s.csv" % CreditMixin.seq, self._csv(*rows))

    def _filler(self, when=None, amount=1.11):
        """A report with an unrelated income: its coverage reaches `when`."""
        CreditMixin.seq += 1
        return self._report(("129%09d" % (900000 + CreditMixin.seq),
                             when or fields.Datetime.now(), amount, ""))

    def _income(self, source):
        return self.Collection.sudo().search([("source_id", "=", source)])

    def _anomalies(self, kind, **where):
        domain = [("kind", "=", kind), ("company_id", "=", self.env.company.id)]
        domain += [(k, "=", v.id if hasattr(v, "id") else v) for k, v in where.items()]
        return self.Anomaly.sudo().search(domain)

    def _credited(self, amount, operation=None, when=None, partner=None):
        msg = self._receipt(amount, operation=operation, when=when, partner=partner)
        self._credit(msg, confirmed=True)
        return self._card(msg)

    def _mp_payment(self, amount, partner=None, day=None):
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": (partner or self.partner).id, "amount": amount,
            "journal_id": self.env["account.journal"]._distri_mp_journal().id,
            "date": day or today_ar()})
        payment.action_post()
        return payment

    def _bank_payment(self, amount, partner=None, day=None):
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": (partner or self.partner).id, "amount": amount,
            "journal_id": self.bank.id, "date": day or today_ar()})
        payment.action_post()
        return payment

    # ------------------------------------------------------------ cascade

    def test_cascade_exact_operation_verifies(self):
        card = self._credited(30000, operation="129000000001")
        payment = card.payment_id
        report = self._report(("129000000001", fields.Datetime.now() - timedelta(minutes=2),
                               30000, ""))
        self.assertEqual(card.audit_state, "verificado")
        self.assertEqual(card.income_id, self._income("129000000001"))
        self.assertEqual(self._mp_payments(), payment, "no second payment")
        run = report.audit_id
        self.assertTrue(run)
        self.assertGreaterEqual(run.verified_count, 1)
        self.assertIn("Auditoría hasta el", report.summary)

    def test_cascade_operation_slip_and_time(self):
        slip = self._credited(31000, operation="129000000011")
        timed = self._credited(32000)
        read = timed.receipt_id.distri_receipt_date
        self._report(("129000000012", fields.Datetime.now() - timedelta(minutes=1), 31000, ""),
                     ("129000000099", read + timedelta(minutes=1), 32000, ""))
        self.assertEqual(slip.audit_state, "verificado")
        self.assertEqual(timed.audit_state, "verificado")
        self.assertEqual(len(self._mp_payments()), 2)

    def test_cascade_customer_and_day_on_the_audit(self):
        card = self._credited(33000, when=fields.Datetime.now() - timedelta(minutes=12))
        report = self._report(("129000000021", fields.Datetime.now(), 33000, ""))
        income = self._income("129000000021")
        self.assertEqual(income.state, "draft", "nothing identifies the customer yet")
        self.assertEqual(card.audit_state, "sin_transferencia")
        # a person gives the income its customer: the next audit ties it
        income.sudo().write({"partner_id": self.partner.id, "proposal": "manual"})
        run = self.Audit._run(company=self.env.company)
        self.assertEqual(card.income_id, income)
        self.assertEqual(card.audit_state, "verificado")
        self.assertEqual(income.payment_id, card.payment_id)
        self.assertGreaterEqual(run.tied_count, 1)
        self.assertTrue(report.audit_id)
        self.assertFalse(self._anomalies("sin_transferencia", card_id=card).filtered(
            lambda a: a.state == "open"), "resolved by itself")

    def test_tie_goes_to_review(self):
        when = fields.Datetime.now() - timedelta(minutes=3)
        first = self._credited(34000, when=when)
        second = self._credited(34000, when=when + timedelta(seconds=20))
        self._report(("129000000031", when + timedelta(seconds=30), 34000, ""))
        income = self._income("129000000031")
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.proposal, "credited")
        self.assertEqual((first | second).mapped("audit_state"), ["pendiente", "pendiente"])
        self.assertTrue(self._anomalies("revisar", card_id=first))
        self.assertEqual(len(self._mp_payments()), 2)

    # ------------------------------------------------------------ audit states

    def test_states_diferencia_sin_transferencia_pendiente(self):
        other = self._credited(35000, operation="129000000041")
        missing = self._credited(36000, operation="129000000042")
        self._report(("129000000041", fields.Datetime.now() - timedelta(minutes=1), 35500, ""))
        self.assertEqual(other.audit_state, "diferencia")
        self.assertTrue(self._anomalies("diferencia", card_id=other))
        self.assertEqual(missing.audit_state, "sin_transferencia")
        anomaly = self._anomalies("sin_transferencia", card_id=missing)
        self.assertEqual(anomaly.credited_by_id, self.seller)
        self.assertEqual(anomaly.payment_id, missing.payment_id)
        # a receipt later than the coverage waits
        late = self._credited(37000, operation="129000000043",
                              when=fields.Datetime.now() + timedelta(minutes=1))
        self.Audit._run(company=self.env.company)
        self.assertEqual(late.audit_state, "pendiente")

    def test_state_revertido(self):
        card = self._credited(38000, operation="129000000051")
        when = fields.Datetime.now() - timedelta(minutes=1)
        self._report(("129000000051", when, 38000, ""),
                     ("129000000051", when + timedelta(seconds=30), -38000, "", "REFUND"))
        self.assertEqual(card.audit_state, "revertido")
        self.assertTrue(self._anomalies("revertido", card_id=card))

    def test_state_anulado_by_annul(self):
        self._invoice(50000)
        card = self._credited(39000, operation="129000000061")
        self.assertEqual(self._balance(), 11000)
        self._filler()
        anomaly = self._anomalies("sin_transferencia", card_id=card)
        payment = card.payment_id
        with self.assertRaises(AccessError):
            anomaly.with_user(self.seller).action_open_annul()
        wizard_action = anomaly.with_user(self.manager).action_open_annul()
        wizard = self.env["distri.mp.anomaly.review"].with_user(self.manager).browse(
            wizard_action["res_id"])
        wizard.note = "Comprobante falso: el cliente no transfirió"
        wizard.action_apply()
        self.assertEqual(payment.state, "canceled")
        self.assertEqual((card.state, card.audit_state), ("ignored", "anulado"))
        self.assertEqual(anomaly.state, "annulled")
        self.assertEqual(self._balance(), 50000, "the account is updated")
        run = self.Audit._run(company=self.env.company)
        self.assertGreaterEqual(run.annulled_count, 1)

    def test_mark_reviewed(self):
        card = self._credited(39500, operation="129000000062")
        self._filler()
        anomaly = self._anomalies("sin_transferencia", card_id=card)
        with self.assertRaises(UserError):
            anomaly.with_user(self.admin_user)._mark_reviewed("")
        anomaly.with_user(self.admin_user)._mark_reviewed("Hablado con el cliente")
        self.assertEqual((anomaly.state, anomaly.reviewed_by_id), ("reviewed", self.admin_user))
        self.Audit._run(company=self.env.company)
        self.assertEqual(anomaly.state, "reviewed", "a reviewed anomaly stays reviewed")

    def test_sin_comprobante_and_manual_payment(self):
        manual = self._mp_payment(41000)
        card = self._credited(42000, operation="129000000071")
        self._filler()
        self.assertTrue(self._anomalies("sin_comprobante", payment_id=manual))
        self.assertTrue(self._anomalies("manual_sin_transferencia", payment_id=manual))
        self.assertTrue(manual.distri_without_receipt)
        self.assertFalse(self._anomalies("sin_comprobante", payment_id=card.payment_id))
        self.assertIn(manual, self.env["account.payment"].search(
            [("distri_without_receipt", "=", True)]))

    # ------------------------------------------------------------ duplicates

    def test_duplicate_operation(self):
        first = self._credited(43000, operation="129000000081")
        second = self._credited(43500, operation="129555000082")
        # a legacy pair the index could not see (the same digits)
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET operation = %s WHERE id = %s",
                            ["129000000081 ", second.id])
        self.env.invalidate_all()
        self._filler()
        anomaly = self._anomalies("operacion_repetida", card_id=second)
        self.assertEqual(anomaly.related_card_id, first)

    def test_duplicate_customer_and_amount(self):
        bank = self._bank_payment(44000, day=today_ar() - timedelta(days=1))
        card = self._credited(44000.5, operation="129000000091")
        self._filler()
        anomaly = self._anomalies("cliente_importe", payment_id=card.payment_id)
        self.assertEqual(anomaly.related_payment_id, bank)
        # two distinct transfers of the report are not a duplicate
        one = self._credited(45000, operation="129111000092")
        two = self._credited(45000, operation="129222000093")
        self._report(("129111000092", fields.Datetime.now(), 45000, ""),
                     ("129222000093", fields.Datetime.now(), 45000, ""))
        self.assertEqual((one | two).mapped("audit_state"), ["verificado", "verificado"])
        self.assertFalse(self._anomalies("cliente_importe", payment_id=two.payment_id).filtered(
            lambda a: a.state == "open" and a.related_payment_id == one.payment_id))

    def test_report_income_never_a_second_payment(self):
        card = self._credited(46000, when=fields.Datetime.now() - timedelta(days=1))
        payment = card.payment_id
        self._report(("129000000101", fields.Datetime.now(), 46000, ""))
        income = self._income("129000000101")
        income.sudo().write({"partner_id": self.partner.id, "proposal": "manual"})
        with self.assertRaisesRegex(UserError, "ya acreditado"):
            income.action_confirm_board()
        self.assertEqual(income._bulk_problem(), "puede ser un comprobante ya acreditado")
        self.assertEqual(self._mp_payments(), payment)
        # the person ties it: no second payment
        action = income.action_open_tie_credited()
        wizard = self.env["distri.mp.tie.credited"].browse(action["res_id"])
        wizard.card_id = card
        wizard.action_apply()
        self.assertEqual(income.payment_id, payment)
        self.assertEqual(card.audit_state, "verificado")
        self.assertEqual(self._mp_payments(), payment)

    def test_other_payment_is_flagged_as_duplicate(self):
        card = self._credited(47000, when=fields.Datetime.now() - timedelta(days=1))
        self._report(("129000000111", fields.Datetime.now(), 47000, ""))
        income = self._income("129000000111")
        income.sudo().write({"partner_id": self.partner.id, "proposal": "manual"})
        income.with_context(distri_vouched_ids=income.ids).action_confirm_other_payment()
        if income.state == "draft":
            self.env["distri.mp.confirm.notice"].create({
                "mode": "selected", "collection_ids": [Command.set(income.ids)]}).with_context(
                distri_not_credited=True).action_apply()
        self.assertEqual(income.state, "confirmed")
        self.assertNotEqual(income.payment_id, card.payment_id)
        self.Audit._run(company=self.env.company)
        pair = self._anomalies("cliente_importe", payment_id=income.payment_id) | \
            self._anomalies("cliente_importe", payment_id=card.payment_id)
        self.assertTrue(pair)

    # ------------------------------------------------------------ alerts and summary

    def test_three_day_alert_by_coverage(self):
        recent = self._credited(48000, operation="129000000121")
        old = self._credited(49000, operation="129000000122")
        old.sudo().credited_at = fields.Datetime.now() - timedelta(days=4)
        self._filler()
        alert = self._anomalies("sin_transferencia", card_id=old)
        self.assertTrue(alert.overdue)
        activities = alert.activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY)
        self.assertIn(self.manager, activities.user_id)
        self.assertTrue(any(ALERT_SUMMARY in (m.body or "") for m in old.message_ids))
        self.assertFalse(self._anomalies("sin_transferencia", card_id=recent).overdue)
        # coverage, not wall clock: without a newer report nothing new
        self.Audit._run(company=self.env.company)
        self.assertEqual(len(alert.activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY)),
                         len(activities), "never alerted twice")

    def test_daily_summary_per_person(self):
        verified = self._credited(50000, operation="129000000131")
        self._credited(51000, operation="129000000132")
        self._report(("129000000131", fields.Datetime.now(), 50000, ""))
        self.assertEqual(verified.audit_state, "verificado")
        summary = self.env["distri.credit.summary"]._build(today_ar(), self.env.company)
        line = summary.line_ids.filtered(lambda l: l.user_id == self.seller)
        # the other one is covered by the report and missing: an anomaly
        self.assertEqual((line.count, line.amount, line.verified_count, line.pending_count,
                          line.anomaly_count), (2, 101000, 1, 0, 1))
        self.env["distri.credit.summary"]._cron_daily()
        summary = self.env["distri.credit.summary"].search(
            [("date", "=", today_ar()), ("company_id", "=", self.env.company.id)])
        self.assertIn(self.manager, summary.activity_ids.filtered(
            lambda a: a.summary == DAILY_SUMMARY).user_id)
        self.assertIn(self.seller.name, summary.text)

    # ------------------------------------------------------------ bank twins

    def test_bank_twin_proposed_and_confirmed(self):
        self.env["distri.payer.identity"].sudo().create(
            {"partner_id": self.partner.id, "kind": "cuit", "value": "20291290001"})
        bank = self._bank_payment(52000)
        self._report(("129000000141", fields.Datetime.now(), 52000, "20291290001"))
        income = self._income("129000000141")
        self.assertEqual(income.partner_id, self.partner)
        self.assertEqual(income.bank_twin_payment_id, bank, "proposed")
        self.assertEqual(income.column, "review")
        self.assertEqual(income.state, "draft", "never tied by itself")
        self.assertTrue(self._anomalies("mellizo_banco", income_id=income))
        with self.assertRaisesRegex(UserError, "Es el pago cargado a mano"):
            income.action_confirm()
        income.action_confirm_not_bank_twin()
        if income.state == "draft":
            self.env["distri.mp.confirm.notice"].create({
                "mode": "selected", "collection_ids": [Command.set(income.ids)]}).with_context(
                distri_not_bank_twin=True).action_apply()
        self.assertEqual(income.state, "confirmed")
        self.assertNotEqual(income.payment_id, bank)
        self.assertFalse(income.bank_twin_payment_id)

    def test_bank_twin_tied_by_a_person(self):
        bank = self._bank_payment(53000)
        income = self.Collection.sudo().create({
            "source_id": "129000000151", "date": fields.Datetime.now(), "amount": 53000,
            "method_type": "bank_transfer", "partner_id": self.partner.id, "proposal": "manual"})
        with self.assertRaises(AccessError):
            income.with_user(self.seller).action_tie_bank_twin()
        income.action_tie_bank_twin()
        self.assertEqual((income.state, income.payment_id), ("confirmed", bank))

    # ------------------------------------------------------------ supplier payment

    def _supplier_vals(self, **extra):
        return dict({"partner_id": self.partner.id, "supplier_id": self.supplier.id,
                     "amount": 20000, "authorized_by_id": self.manager.id,
                     "receipt_file": base64.b64encode(_png(7)), "receipt_filename": "c.png"},
                    **extra)

    def test_supplier_payment_permissions_and_flow(self):
        Supplier = self.env["distri.supplier.payment"]
        for user in (self.seller, self.admin_user):
            with self.assertRaises(AccessError):
                Supplier.with_user(user).create(self._supplier_vals())
        self.assertTrue(self.env.ref("base.user_admin").has_group(
            "distri_pedido.grupo_pago_proveedor"))
        self._invoice(50000)
        bad = Supplier.with_user(self.manager).create(
            self._supplier_vals(authorized_by_id=self.seller.id))
        with self.assertRaisesRegex(UserError, "Gerencia"):
            bad.action_post()
        record = Supplier.with_user(self.manager).create(self._supplier_vals())
        self.assertEqual(record.loaded_by_id, self.manager)
        record.action_post()
        payment = record.payment_id
        journal = self.env["account.journal"]._distri_supplier_journal()
        self.assertEqual(payment.journal_id, journal)
        self.assertEqual(journal.name, "Pagos a proveedores por cuenta de clientes")
        self.assertEqual(self._balance(), 30000)
        self._filler()
        self.assertFalse(self.Anomaly.sudo().search([("payment_id", "=", payment.id)]),
                         "never matched against the report")
        with self.assertRaises(AccessError):
            record.with_user(self.seller).action_cancel()
        record.with_user(self.manager).action_cancel()
        self.assertEqual(payment.state, "canceled")
        self.assertEqual(self._balance(), 50000)

    # ------------------------------------------------------------ unique index

    def test_unique_index_of_credited_operations(self):
        self.env.cr.execute("SELECT indexdef FROM pg_indexes WHERE indexname = %s",
                            ["distri_mp_collection_credited_operation_uniq"])
        self.assertIn("UNIQUE", self.env.cr.fetchone()[0])
        self._credited(54000, operation="129000000161")
        second = self._credited(54500, operation="129555000162")
        with self.assertRaises(psycopg2.IntegrityError), mute_logger("odoo.sql_db"), \
                self.env.cr.savepoint():
            second.sudo().write({"operation": "129000000161"})
            second.flush_recordset()

    def test_concurrent_credits_one_wins(self):
        """Two transactions credit two receipts of the same operation at the
        same time: the unique index lets exactly one commit. Real separate
        connections; the rows are committed and removed at the end."""
        db = self.env.cr.dbname
        company = self.env.ref("base.main_company").id
        operation = "1299%08d" % (int(time.time()) % 10 ** 8)
        with closing(sql_db.db_connect(db).cursor()) as cr:
            cr.execute("""
                INSERT INTO distri_mp_collection (kind, state, company_id, operation, amount,
                                                  proposal, create_date, write_date)
                VALUES ('verify', 'draft', %s, %s, 55000, 'none', now(), now()),
                       ('verify', 'draft', %s, %s, 55000, 'none', now(), now())
                RETURNING id""", [company, operation, company, operation])
            ids = [row[0] for row in cr.fetchall()]
            cr.commit()
        barrier = threading.Barrier(2, timeout=20)
        results = {}

        def credit(card_id):
            # raw SQL: the ORM would wait on the registry lock the test
            # runner holds; this is the UPDATE the flush of `_distri_credit`
            # sends for the crediting
            with closing(sql_db.db_connect(db).cursor()) as cr:
                try:
                    cr.execute("SELECT id FROM distri_mp_collection WHERE id = %s FOR UPDATE",
                               [card_id])
                    barrier.wait()
                    cr.execute("""
                        UPDATE distri_mp_collection
                           SET state = 'confirmed', credited_at = now() at time zone 'utc',
                               credited_by_id = %s, audit_state = 'pendiente'
                         WHERE id = %s""", [SUPERUSER_ID, card_id])
                    time.sleep(0.5)
                    cr.commit()
                    results[card_id] = "credited"
                except psycopg2.IntegrityError:
                    cr.rollback()
                    results[card_id] = "refused"
                except Exception as exc:  # reported by the assertion below
                    cr.rollback()
                    results[card_id] = repr(exc)

        try:
            with mute_logger("odoo.sql_db"):
                threads = [threading.Thread(target=credit, args=(i,)) for i in ids]
                for thread in threads:
                    thread.start()
                for thread in threads:
                    thread.join(60)
            self.assertEqual(sorted(results.values()), ["credited", "refused"], results)
        finally:
            with closing(sql_db.db_connect(db).cursor()) as cr:
                cr.execute("DELETE FROM mail_message WHERE model = 'distri.mp.collection' "
                           "AND res_id = ANY(%s)", [ids])
                cr.execute("DELETE FROM distri_mp_collection WHERE id = ANY(%s)", [ids])
                cr.commit()

    # ------------------------------------------------------------ SFTP

    def test_sftp_fetch(self):
        folder = tempfile.mkdtemp(prefix="v129-sftp-")
        self.addCleanup(shutil.rmtree, folder, True)
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_FOLDER, folder)
        self.assertEqual(self.Report._cron_sftp_fetch(), [], "not configured: nothing")
        for key, value in (("host", "sftp.example.invalid"), ("user", "mp"),
                           ("path", "/reportes"), ("key_file", "/nonexistent/key")):
            params.set_param("distri_pedido.mp_sftp_" + key, value)
        name = "settlement-v129-%s.csv" % CreditMixin.seq
        content = self._csv(("129000000171", fields.Datetime.now(), 1234.5, ""))
        asked = []

        class Fake:
            def listdir(self, path):
                asked.append(path)
                return [(name, len(content)), ("notas.txt", 3)]

            def get(self, remote, local):
                asked.append(remote)
                with open(local, "wb") as handle:
                    handle.write(content)

            def close(self):
                pass

        Report = type(self.Report)
        with patch.object(Report, "_distri_sftp_client", lambda self, config: Fake()):
            self.assertEqual(self.Report._cron_sftp_fetch(), [name])
            self.assertEqual(self.Report._cron_sftp_fetch(), [], "fetched once")
        self.assertEqual(asked[:2], ["/reportes", "/reportes/%s" % name])
        report = self.Report.sudo().search([("name", "=", name)])
        self.assertEqual(report.source, "sftp")
        self.assertTrue(report.audit_id, "the audit runs on the SFTP import too")
        self.assertTrue(os.path.exists(os.path.join(folder, "procesados", name)))
