"""Collections board with Mercado Pago reports.

The fixtures in tests/data keep the exact columns of the real reports
(default 12 columns, 25 columns with the payer, all 69 columns) with
synthetic operations: the real samples carry payers' names and CUITs.
"""
import os
import shutil
import tempfile
from datetime import datetime, timedelta
from unittest.mock import patch

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools.misc import file_path
from odoo.tools.safe_eval import safe_eval

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.cobros import (
    ALERT_SUMMARY, AUTHORIZED_LABEL, CANDIDATE_LABEL, WITHOUT_RECEIPT_NOTICE, AUTHORIZED_MISSING, AUTHORIZED_WAITING, COLUMN_HELP,
    COLUMNS, HAND_VERIFIED_MISSING, PARAM_ALERT_HOURS, MP_JOURNAL_CODE, REVERSAL_SUMMARY,
    TWO_CUSTOMERS, local_when, operation_close, own_account, parse_amount_ar,
    parse_mp_datetime, parse_operation, read_report, receipt_caption_amount,
)
from odoo.addons.distri_pedido.models.ocr_comprobante import parse_receipt
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

DATA = "distri_pedido/tests/data/%s"


def _fixture(name):
    with open(file_path(DATA % name), "rb") as handle:
        return handle.read()


@tagged("post_install", "-at_install", "distri_cobros")
class TestCobros(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # fictitious own account: the real one is a lab parameter, never in the code
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)
        cls.env.user.sudo().write({"group_ids": [
            # 19.0.1.129.11: confirming a report income needs grupo_ingresos_reporte
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        Partner = cls.env["res.partner"]
        # CUIT 20-99900001-1 is the customer's own
        cls.known = Partner.create({"name": "Almacén Prueba Conocido", "vat": "20-99900001-1",
                                    "company_type": "company"})
        cls.photo = Partner.create({"name": "Kiosco Prueba Foto", "phone": "299 5000111",
                                    "company_type": "company"})
        cls.other = Partner.create({"name": "Despensa Prueba Otra", "company_type": "company"})
        cls.Report = cls.env["distri.mp.report"]
        cls.Collection = cls.env["distri.mp.collection"]

    # ------------------------------------------------------------ helpers

    def _invoice(self, partner, amount, date):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": date,
            "invoice_line_ids": [Command.create({
                "name": "mercadería", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    def _import(self, name):
        return self.Report._import_file(name, _fixture(name))

    def _card(self, source):
        return self.Collection.search([("source_id", "=", source)])

    def _receipt(self, partner_phone, when, caption="", kind="image", mime="image/jpeg"):
        return self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.test.%s" % when.isoformat(),
            "direction": "in",
            "wa_id": partner_phone,
            "message_type": kind,
            "body": caption,
            "timestamp": when,
            "media_mime_type": mime,
            "media_id": "123",
            "media_state": "pending",
        })

    def _apply_notice(self, result):
        """«Confirmar igual» on the notice the board shows before confirming
        a payment without its WhatsApp receipt (19.0.1.42.0)."""
        self.assertEqual(result.get("res_model"), "distri.mp.confirm.notice", result)
        return self.env["distri.mp.confirm.notice"].browse(result["res_id"]).action_apply()

    def _promise(self, partner, amount, captured):
        order = self._order(partner, self.product_a, qty=1, price=amount)
        route = self._route(order)
        return self.env["distri.promised.transfer"].sudo().create({
            "stop_id": route.stop_ids.id, "kind": "done", "amount": amount,
            "captured_at": captured,
        })

    # ------------------------------------------------------------ parsing

    def test_board_columns_are_named_by_the_task(self):
        # from the most likely valid to the one that needs most attention.
        # 19.0.1.126.0 (D11 replaced): «Esperando reporte MP» became «Para
        # acreditar» (same key) and «Acreditado · a verificar» was added
        self.assertEqual(COLUMNS, [
            ("confirm", "Confirmado contra reporte MP"),
            ("verify", "Para acreditar"),
            ("credited", "Acreditado · a verificar"),
            ("review", "Revisar"),
            ("unidentified", "Asignar cliente"),
            ("no_money", "Sin transferencia"),
        ])
        labels = dict(self.Collection._fields["column"].selection)
        self.assertEqual(labels, dict(COLUMNS))
        self.assertEqual(set(COLUMN_HELP), set(labels), "one help text per column")
        self.assertTrue(all(len(text) > 60 for text in COLUMN_HELP.values()))
        # the «i» of each column header reads the same texts, in board order
        board = self.Collection.distri_board_columns()
        self.assertEqual([c["key"] for c in board], [key for key, _label in COLUMNS])
        for column in board:
            self.assertEqual(column["label"], labels[column["key"]])
            self.assertEqual(column["help"], COLUMN_HELP[column["key"]])
        # 19.0.1.126.0: «Visto en MP» is no longer offered; the help still
        # explains the cards already authorized
        self.assertIn("«Visto en MP»", COLUMN_HELP["verify"])
        self.assertIn(AUTHORIZED_LABEL, COLUMN_HELP["verify"])
        self.assertIn("«Acreditar»", COLUMN_HELP["verify"])
        # the transfers of the report register a payment when confirmed;
        # 19.0.1.126.0: a receipt registers it when credited («Acreditar»)
        for key in ("confirm", "review", "unidentified"):
            self.assertIn("se registra el pago", COLUMN_HELP[key])
        self.assertIn("no se registra ningún pago", COLUMN_HELP["no_money"])
        self.assertIn("se registra el pago en el momento", COLUMN_HELP["verify"])
        self.assertIn("sin registrar otro", COLUMN_HELP["credited"])
        self.assertIn("confirmar todas juntas", COLUMN_HELP["confirm"])
        # the kanban shows the columns in the order of the selection
        self.assertEqual(self.Collection._fields["column"].selection, COLUMNS)
        self.assertEqual([c["key"] for c in self.Collection.distri_board_columns()
                          if c["priority"]], ["unidentified", "no_money"])
        # no old label is left on the board, its help or its wizards
        # («Sin identificar» was an old column label; since 19.0.1.31.0 it names
        # the bucket of unknown payers grouped by payer, on purpose)
        old = ("Comprobantes a verificar", "Para confirmar", "Listo para confirmar",
               "Para revisar", "Foto sin plata", "Verificar en MP",
               "Verificado en MP")
        View = self.env["ir.ui.view"]
        texts = [View.browse(self.env.ref(xmlid).id).arch_db for xmlid in (
            "distri_pedido.view_mp_collection_kanban", "distri_pedido.view_mp_collection_search",
            "distri_pedido.view_receipt_search", "distri_pedido.view_mp_ignore_form")]
        texts.append(str(self.env.ref("distri_pedido.action_mp_board").help))
        for text in texts:
            for label in old:
                self.assertNotIn(label, text)

    def test_board_kanban_uses_the_board_js_class(self):
        arch = self.env.ref("distri_pedido.view_mp_collection_kanban").arch_db
        node = etree.fromstring(arch.encode())
        self.assertEqual(node.get("js_class"), "distri_cobros_kanban")

    def _aged(self, cards, hours):
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET create_date = %s WHERE id IN %s",
                            (fields.Datetime.now() - timedelta(hours=hours), tuple(cards.ids)))
        cards.invalidate_recordset(["create_date"])

    def _alerts(self, card):
        card.invalidate_recordset(["activity_ids"])
        return card.activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY)

    def test_board_alerts_for_cards_waiting_too_long(self):
        admin_group = self.env.ref("distri_pedido.grupo_administracion")
        common = {"date": fields.Datetime.now(), "method_type": "bank_transfer"}
        stale = self.Collection.create(dict(common, source_id="700000000001", amount=1000))
        ready = self.Collection.create(dict(common, source_id="700000000003", amount=3000,
                                            partner_id=self.known.id, proposal="cuit"))
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 18, 0), caption="$ 5.000")
        no_money = self.Collection.create({"kind": "receipt", "receipt_id": receipt.id,
                                           "amount": 5000, "date": receipt.timestamp})
        fresh_receipt = self._receipt("5492995000112", datetime(2026, 9, 24, 19, 0),
                                      caption="$ 2.000")
        fresh = self.Collection.create({"kind": "receipt", "receipt_id": fresh_receipt.id,
                                        "amount": 2000, "date": fresh_receipt.timestamp})
        self.assertEqual((stale.column, fresh.column, ready.column, no_money.column),
                         ("unidentified", "no_money", "confirm", "no_money"))
        self._aged(stale | ready | no_money, 30)
        self._aged(fresh, 3)
        mails = self.env["mail.mail"].search_count([])
        # only this test's cards: the database may hold cards of its own
        mine = stale | fresh | ready | no_money
        alerted = self.Collection._cron_alerts() & mine
        # 19.0.1.129.16: an income of the report («Asignar cliente») is never alerted
        self.assertEqual(alerted, no_money)
        activity = self._alerts(no_money)
        self.assertEqual(len(activity), 1)
        self.assertIn(activity.user_id, admin_group.user_ids)
        self.assertEqual(activity.activity_type_id, self.env.ref("mail.mail_activity_data_todo"))
        self.assertFalse(self._alerts(stale), "no alert for the incomes of the report")
        self.assertFalse(self._alerts(fresh), "not yet 24 hours")
        self.assertFalse(self._alerts(ready), "only the columns that need a person")
        self.assertEqual(self.env["mail.mail"].search_count([]), mails, "no email leaves")
        # the next day: nothing new while the activity is open
        self.assertFalse(self.Collection._cron_alerts() & mine)
        self.assertEqual(len(self._alerts(no_money)), 1)
        # the parameter moves the limit
        self.env["ir.config_parameter"].sudo().set_param(PARAM_ALERT_HOURS, "2")
        self.assertEqual(self.Collection._cron_alerts() & mine, fresh)
        # the button does the same check, for administration only
        action = self.Collection.action_check_alerts()
        self.assertEqual(action["tag"], "display_notification")
        with self.assertRaises(UserError):
            self.Collection.with_user(self.driver).action_check_alerts()

    def test_parsers(self):
        self.assertEqual(parse_amount_ar("Te transferí $ 45.000 recién"), 45000)
        self.assertEqual(parse_amount_ar("$45.000,50"), 45000.5)
        self.assertEqual(parse_amount_ar("sin monto"), 0)
        # a cash note next to the photo is another payment (card 50534, 31/10/2026)
        self.assertEqual(receipt_caption_amount("Chino Lanin\nRecibí efectivo $ 600.000 Ivo Laurenti"), 0)
        self.assertEqual(receipt_caption_amount("Te transferí $ 45.000\nEfectivo $ 10.000"), 45000)
        self.assertEqual(parse_operation("Número de operación de Mercado Pago 180819825054"),
                         "180819825054")
        self.assertEqual(parse_mp_datetime("2026-09-24T10:15:00.000-03:00"),
                         datetime(2026, 9, 24, 13, 15))
        for name, columns in (("mp_report_12col.csv", 12), ("mp_report_25col.csv", 25),
                              ("mp_report_69col.csv", 69)):
            rows = read_report(_fixture(name))
            self.assertEqual(len(rows[0]), columns)
        with self.assertRaises(UserError):
            read_report(b"fecha,importe\n2026-09-24,100\n")

    def test_import_skips_yields_and_own_payments_and_loads_once(self):
        report = self._import("mp_report_25col.csv")
        self.assertEqual(report.row_count, 7)
        self.assertEqual(report.new_count, 5, "4 settlements and 1 credit coming in")
        self.assertEqual(report.skipped_count, 2, "a yield and an own payment")
        self.assertAlmostEqual(report.incoming_total, 167677.5)
        self.assertEqual(report.period_end, datetime(2026, 9, 25, 4, 3, 40))
        # the overlapping report: the repeated operation is not loaded again
        second = self._import("mp_report_69col.csv")
        self.assertEqual(second.new_count, 2)
        self.assertEqual(second.duplicate_count, 1)
        self.assertEqual(second.reversal_count, 1)
        self.assertEqual(len(self._card("900000000002")), 1)
        self.assertIn("Devolución", self._card("900000000003").reversal)
        # default columns: no payer, still one card per transfer
        third = self._import("mp_report_12col.csv")
        self.assertEqual(third.new_count, 1)
        self.assertEqual(self._card("900000000008").column, "unidentified")

    # ------------------------------------------------------------ proposals

    def test_known_cuit_goes_to_confirm_and_pays_oldest_first(self):
        old = self._invoice(self.known, 30000, "2026-08-01")
        new = self._invoice(self.known, 40000, "2026-09-20")
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        self.assertEqual(card.partner_id, self.known)
        self.assertEqual(card.proposal, "cuit")
        self.assertEqual(card.column, "confirm")
        self.assertEqual(card.proposal_badge, "CUIT")
        # everything to double-check it by hand, the report included
        self.assertIn("CUIT 20-99900001-1", card.payer_label)
        self.assertEqual(card.report_label, "Reporte N° %s del %s" % (
            card.report_id.id, local_when(card.report_id.generated_at
                                          or card.report_id.covers_until
                                          or card.report_id.imported_at)))
        action = card.action_view_report()
        self.assertEqual((action["res_model"], action["res_id"]),
                         ("distri.mp.report", card.report_id.id))
        self.assertFalse(card.review_reason, "only «Revisar» says why")
        self.assertIn("CUIT 20-99900001-1", card.detail)
        self.assertEqual(card.date_day, fields.Date.to_date("2026-09-24"))

        card.action_confirm()
        payment = card.payment_id
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(payment.journal_id.code, MP_JOURNAL_CODE)
        self.assertEqual(payment.journal_id.type, "bank")
        self.assertEqual(payment.amount, 45321)
        self.assertEqual(payment.partner_id, self.known)
        self.assertEqual(payment.date, fields.Date.to_date("2026-09-24"))
        self.assertEqual(payment.state, "paid", "straight into the Mercado Pago account")
        self.assertIn(payment.journal_id.default_account_id, payment.move_id.line_ids.account_id)
        self.assertEqual(card.confirmed_by_id, self.env.user)
        self.assertEqual(old.amount_residual, 0, "oldest invoice first")
        self.assertAlmostEqual(new.amount_residual, 40000 - 15321)
        with self.assertRaises(UserError):
            card.action_confirm()

    def test_unknown_cuit_is_assigned_once_and_learned(self):
        self._import("mp_report_25col.csv")
        self._import("mp_report_69col.csv")
        later = self._card("900000000010")
        self.assertEqual(later.column, "unidentified")
        card = self._card("900000000003")
        # the second report refunds it: a person looks at it first
        self.assertEqual(card.column, "review")
        self.assertIn("Devolución", card.review_reason)
        self.assertFalse(card.partner_id)
        wizard = self.env["distri.mp.assign"].create({
            "collection_id": card.id, "partner_id": self.other.id})
        # no WhatsApp receipt: the notice first, then it is confirmed and flagged
        self._apply_notice(wizard.action_assign_confirm())
        self.assertEqual(card.state, "confirmed")
        self.assertTrue(card.confirmed_without_receipt)
        # 19.0.1.69.0: confirmed without its receipt in the WhatsApp group, the
        # payer's CUIT is NOT learned (01/11/2026: hundreds of learned CUITs
        # assigned other people's transfers); the other pending transfer from
        # that account stays unidentified
        self.assertFalse(self.env["distri.payer.identity"].search([("value", "=", "20999000029")]))
        self.assertFalse(later.partner_id)
        self.assertEqual(later.column, "unidentified")
        self.assertFalse(self.env["res.partner"]._distri_partners_by_cuit("20-99900002-9"))
        # undo: the payment is cancelled and the card goes back
        card.action_undo()
        self.assertEqual(card.state, "draft")
        self.assertEqual(card.payment_id.id, False)

    def test_cuit_typed_on_the_customer_moves_pending_cards(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000003")
        self.assertEqual(card.column, "unidentified")
        self.env["distri.payer.identity"].create({
            "partner_id": self.other.id, "kind": "cuit", "value": "20-99900002-9"})
        self.assertEqual(card.partner_id, self.other)
        self.assertEqual(card.proposal, "cuit")
        self.assertEqual(card.column, "confirm")

    def test_customer_found_by_payer_holder_or_cuit(self):
        self.env["distri.payer.identity"].create({
            "partner_id": self.other.id, "kind": "cuit", "value": "20-99900002-9",
            "holder": "Juana Pagadora Titular"})
        arch = self.env["res.partner"].get_views([(False, "search")])["views"]["search"]["arch"]
        node = etree.fromstring(arch).xpath("//field[@name='distri_payer_ids']")
        self.assertTrue(node, "admin sees the payer filter in the customer search")
        domain = node[0].get("filter_domain")
        for text in ("pagadora titular", "99900002"):
            found = self.env["res.partner"].search(safe_eval(domain, {"self": text}))
            self.assertIn(self.other, found, text)

    def test_photo_proposes_the_customer_for_review(self):
        # the bank transfer of 38.777 at 11:34 (14:34 UTC) has no CUIT
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Te pasé $ 38.777")
        self.assertTrue(receipt.distri_is_receipt)
        self.assertEqual(receipt.distri_partner_id, self.photo)
        self.assertEqual(receipt.distri_amount, 38777)
        self._import("mp_report_25col.csv")
        card = self._card("900000000002")
        self.assertEqual(card.column, "review")
        self.assertEqual(card.proposal, "photo")
        self.assertEqual(card.partner_id, self.photo)
        self.assertEqual(card.receipt_id, receipt)
        self.assertEqual(receipt.distri_receipt_state, "matched")
        self.assertIn("11:40", card.proposal_note)
        self.assertEqual(card.proposal_badge, "Teléfono")
        self.assertEqual(card.review_reason, "Cliente sacado solo de la foto")
        self.assertFalse(card.amount_mismatch)
        card.action_confirm()
        self.assertEqual(card.payment_id.partner_id, self.photo)

    def test_operation_number_of_the_receipt(self):
        receipt = self._receipt("5491100000000", datetime(2026, 9, 24, 20, 0),
                                caption="Comprobante del pedido. Operación 900000000004")
        self.assertEqual(receipt.distri_operation, "900000000004")
        receipt.distri_partner_id = self.other
        self._import("mp_report_25col.csv")
        card = self._card("900000000004")
        self.assertEqual(card.proposal, "operation")
        self.assertEqual(card.partner_id, self.other)
        self.assertEqual(card.column, "review")
        card.action_confirm()
        # the unknown phone is learned on the customer
        self.assertEqual(self.env["res.partner"]._distri_partners_by_phone("5491100000000"),
                         self.other)

    def test_photo_without_money_and_late_money(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 18, 0),
                                caption="comprobante $ 77.001")
        report = self._import("mp_report_25col.csv")
        self.assertEqual(report.no_money_count, 1)
        card = self.Collection.search([("kind", "=", "receipt"), ("receipt_id", "=", receipt.id)])
        self.assertEqual(card.column, "no_money")
        self.assertEqual(card.partner_id, self.photo)
        self.assertEqual(card.period_label, "%s a %s" % (
            local_when(report.period_start), local_when(report.period_end)))
        self.assertEqual(receipt.distri_receipt_state, "no_money")
        with self.assertRaises(UserError):
            card.action_confirm()
        # the administrator corrects the amount read from the photo: 38.777
        receipt.distri_amount = 38777
        income = self._card("900000000002")
        self.assertEqual(income.receipt_id, receipt)
        self.assertEqual(income.column, "review")
        self.assertEqual(card.state, "ignored")
        self.assertEqual(card.ignore_reason, "receipt_late")

    def _same_amount_report(self):
        """Two synthetic transfers of $ 400.000, 13 hours apart."""
        header = _fixture("mp_report_25col.csv").decode().splitlines()[0]
        row = ("2026-09-24T{t}.000-03:00;2026-09-24T{t}.000-03:00;2026-09-24T{t}.000-03:00;"
               "{op};bank_transfer;SETTLEMENT;400000.00;0.00;400000.00;0.00;;;;cvu;;;;;;"
               "registered;;;;100000001;")
        rows = [row.format(t="08:00:00", op="900000000101"),
                row.format(t="21:00:00", op="900000000102")]
        return ("\n".join([header] + rows) + "\n").encode()

    def test_operation_number_wins_over_amount_and_time(self):
        # the photo of the 21:00 transfer: 21:05 local is 00:05 UTC
        receipt = self._receipt("5492995000111", datetime(2026, 9, 25, 0, 5),
                                caption="$ 400.000 Operación 900000000102")
        self.assertEqual(receipt.distri_operation, "900000000102")
        self.Report._import_file("same_amount.csv", self._same_amount_report())
        early, late = self._card("900000000101"), self._card("900000000102")
        self.assertEqual(late.receipt_id, receipt)
        self.assertEqual(late.proposal, "operation")
        self.assertEqual(late.partner_id, self.photo)
        self.assertFalse(early.receipt_id)
        self.assertEqual(receipt.distri_collection_id, late)

    def test_receipt_after_the_report_keeps_to_its_operation(self):
        self.Report._import_file("same_amount.csv", self._same_amount_report())
        early, late = self._card("900000000101"), self._card("900000000102")
        # the 21:00 transfer already has its photo
        first = self._receipt("5492995000111", datetime(2026, 9, 25, 0, 5),
                              caption="$ 400.000 Operación 900000000102")
        self.assertEqual(late.receipt_id, first)
        # a second photo of the same operation never falls on the 08:00 one
        again = self._receipt("5492995000111", datetime(2026, 9, 25, 0, 10),
                              caption="$ 400.000 Operación 900000000102")
        self.assertFalse(early.receipt_id)
        self.assertNotEqual(again.distri_receipt_state, "matched")

    def test_promise_matches_amount(self):
        promise = self._promise(self.other, 51234, datetime(2026, 9, 24, 15, 0))
        self._import("mp_report_25col.csv")
        card = self._card("900000000004")
        self.assertEqual(card.proposal, "promise")
        self.assertEqual(card.promise_id, promise)
        self.assertEqual(card.partner_id, self.other)
        card.action_confirm()
        self.assertEqual(promise.state, "fulfilled")
        self.assertIn(promise, card.fulfilled_promise_ids)

    # ------------------------------------------------------------ promises

    def test_report_no_longer_expires_pending_receipts(self):
        """«Prometidas vencidas» is gone: the report never expires a pending
        receipt (the settlement of the next day does, comprobante_pendiente.py)."""
        old = self._promise(self.photo, 4444, datetime(2026, 9, 24, 12, 0))
        report = self._import("mp_report_25col.csv")
        self.assertEqual(old.state, "open")
        self.assertNotIn("vencidas", (report.summary or "").lower())
        self.assertIn("S0", old._reminder_text())
        self.assertIn("comprobante", old._reminder_text())

    def test_known_cuit_fulfils_promise_on_confirm(self):
        promise = self._promise(self.known, 45321, datetime(2026, 9, 24, 11, 0))
        self._import("mp_report_25col.csv")
        self.assertEqual(promise.state, "open", "a pending card of the customer keeps it open")
        self.Collection.action_confirm_ready()
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.fulfilled_by_id, self._card("900000000001"))
        self.assertEqual(self._card("900000000006").state, "confirmed",
                         "the CREDIT of a known CUIT is confirmed too")

    def test_promise_fulfilled_only_when_the_amount_covers_it(self):
        # the known customer promised 30.000 and then 99.999; the transfer is 45.321
        first = self._promise(self.known, 30000, datetime(2026, 9, 24, 9, 0))
        second = self._promise(self.known, 99999, datetime(2026, 9, 24, 11, 0))
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        card.action_confirm()
        # the statement links the payment to its card (31/10/2026)
        row = [r for r in self.known._distri_statement_rows()
               if r["res_model"] == "account.move" and r["res_id"] == card.payment_id.move_id.id]
        self.assertEqual(row[0].get("card_id"), card.id)
        self.assertIn("/odoo/distri.mp.collection/%d" % card.id, str(self.known.distri_statement_html))
        self.assertEqual(first.state, "fulfilled", "the oldest one, covered")
        self.assertEqual(second.state, "open", "15.321 left: a partial payment")
        self.assertIn("no cubre", second.partial_note)
        self.assertEqual(card.fulfilled_promise_ids, first)
        # undone: the promise it fulfilled opens again
        card.action_undo()
        self.assertEqual(first.state, "open")
        self.assertFalse(first.fulfilled_by_id)

    # ------------------------------------------------------------ bulk and close

    def test_bulk_confirm_and_daily_close(self):
        self._import("mp_report_25col.csv")
        result = self.Collection.action_confirm_ready()
        self.assertEqual(result["params"]["type"], "success")
        confirmed = self.Collection.search([("state", "=", "confirmed"),
                                            ("source_id", "like", "90000000000")])
        self.assertEqual(set(confirmed.mapped("source_id")), {"900000000001", "900000000006"})

        day = self.env["distri.mp.day"].search([("date", "=", "2026-09-24"),
                                                ("company_id", "=", self.env.company.id)])
        self.assertTrue(day.covered)
        self.assertAlmostEqual(day.report_total, 167677.5)
        self.assertAlmostEqual(day.confirmed_total, 65321)
        self.assertEqual(day.pending_count, 3)
        with self.assertRaises(UserError):
            day.action_close()

        self._apply_notice(self.env["distri.mp.assign"].create({
            "collection_id": self._card("900000000002").id,
            "partner_id": self.photo.id}).action_assign_confirm())
        self._apply_notice(self.env["distri.mp.assign"].create({
            "collection_id": self._card("900000000003").id,
            "partner_id": self.other.id}).action_assign_confirm())
        self.env["distri.mp.ignore"].create({
            "collection_id": self._card("900000000004").id,
            "reason": "not_customer"}).action_ignore()
        day.invalidate_recordset()
        self.assertEqual(day.pending_count, 0)
        self.assertAlmostEqual(day.difference, 0)
        day.action_close()
        self.assertEqual(day.state, "closed")

    def test_daily_close_difference_alerts(self):
        self._import("mp_report_25col.csv")
        self.Collection.search([("source_id", "in", (
            "900000000002", "900000000003", "900000000004"))]).write(
            {"state": "ignored", "ignore_reason": "other"})
        self.Collection.action_confirm_ready()
        # a payment typed by hand in the Mercado Pago journal, twice the same
        journal = self.env["account.journal"]._distri_mp_journal()
        manual = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": self.known.id, "amount": 500, "journal_id": journal.id,
            "date": "2026-09-24"})
        manual.action_post()
        day = self.env["distri.mp.day"].search([("date", "=", "2026-09-24"),
                                                ("company_id", "=", self.env.company.id)])
        self.assertAlmostEqual(day.difference, -500)
        with self.assertRaises(UserError):
            day.action_close()
        day.note = "Pago cargado a mano dos veces"
        day.action_close()
        self.assertEqual(day.state, "alert")
        self.assertEqual(day.closed_difference, -500)
        self.assertTrue(any("diferencia" in (m.body or "") for m in day.message_ids))

    # ------------------------------------------------------------ SFTP folder

    def test_sftp_folder_pickup(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.mp_carpeta_reportes", folder)
        with open(os.path.join(folder, "settlement_v2-100000001-2026-09-25.csv"), "wb") as f:
            f.write(_fixture("mp_report_25col.csv"))
        with open(os.path.join(folder, "roto.csv"), "wb") as f:
            f.write(b"no es un reporte\n")
        with open(os.path.join(folder, "notas.txt"), "wb") as f:
            f.write(b"ignorado")
        self.Report._cron_import_folder()
        report = self.Report.search([("name", "=", "settlement_v2-100000001-2026-09-25.csv")])
        self.assertEqual(report.source, "sftp")
        self.assertEqual(report.new_count, 5)
        self.assertTrue(os.path.exists(os.path.join(
            folder, "procesados", "settlement_v2-100000001-2026-09-25.csv")))
        self.assertTrue(os.path.exists(os.path.join(folder, "errores", "roto.csv")))
        self.assertTrue(os.path.exists(os.path.join(folder, "notas.txt")))
        # a missing folder does nothing
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.mp_carpeta_reportes", os.path.join(folder, "no-existe"))
        self.Report._cron_import_folder()

    def test_manual_upload_wizard(self):
        import base64
        wizard = self.env["distri.mp.import"].create({
            "file": base64.b64encode(_fixture("mp_report_25col.csv")),
            "filename": "reporte.csv"})
        wizard.action_import()
        self.assertEqual(wizard.report_id.new_count, 5)
        self.assertIn("Confirmado contra reporte MP", wizard.summary)
        bad = self.env["distri.mp.import"].create({
            "file": base64.b64encode(b"x"), "filename": "reporte.xlsx"})
        with self.assertRaises(UserError):
            bad.action_import()

    def test_only_administration_confirms(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        with self.assertRaises(UserError):
            card.with_user(self.driver).action_confirm()

    # ------------------------------------------------------------ «Verificar en MP»

    def _verify_card(self, receipt):
        return self.Collection.search([("kind", "=", "verify"), ("receipt_id", "=", receipt.id)])

    def _board(self):
        """What the board shows with its default filter «Pendientes»."""
        return self.Collection.search(["|", "|", ("state", "=", "draft"),
                                       ("report_review", "=", True),
                                       "&", ("state", "=", "authorized"),
                                       ("income_id", "=", False)])

    def test_board_filter_matches_the_view(self):
        arch = self.env.ref("distri_pedido.view_mp_collection_search").arch_db
        node = etree.fromstring(arch.encode()).find(".//filter[@name='pending']")
        # 19.0.1.126.0: plus the credited receipts waiting for the report
        self.assertEqual(safe_eval(node.get("domain")), [
            "|", "|", "|", ("state", "=", "draft"), ("report_review", "=", True),
            "&", ("state", "=", "authorized"), ("income_id", "=", False),
            "&", ("column", "=", "credited"), ("income_id", "=", False)])

    def _mp_payments(self, operation):
        journal = self.env["account.journal"]._distri_mp_journal()
        return self.env["account.payment"].search([
            ("journal_id", "=", journal.id), ("memo", "ilike", operation),
            ("state", "not in", ("draft", "canceled"))])

    def _partner_payments(self, partner):
        return self.env["account.payment"].search([("partner_id", "=", partner.id)])

    def _authorized_receipt(self, amount_text="38.777", operation="900000000002",
                            when=datetime(2026, 9, 24, 14, 40)):
        """«Visto en MP: autorizar entrega» on a receipt of the photo customer."""
        receipt = self._receipt("5492995000111", when, caption="Comprobante $ %s operación %s" % (
            amount_text, operation))
        card = self._verify_card(receipt)
        card.action_verify()
        return receipt, card

    def _legacy_verified_receipt(self, amount_text="38.777", operation="900000000002",
                                 when=datetime(2026, 9, 24, 14, 40)):
        """A receipt verified by hand before 19.0.1.21.0: «Verificado en MP»
        registered the payment at once (the data left by the old flow)."""
        receipt = self._receipt("5492995000111", when, caption="Comprobante $ %s operación %s" % (
            amount_text, operation))
        card = self._verify_card(receipt)
        # the payment the old flow registered, made directly: `_confirm_one`
        # only pays incomes of the report now
        journal = self.env["account.journal"]._distri_mp_journal()
        payment = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": card.partner_id.id, "amount": card.amount, "journal_id": journal.id,
            "date": card.date_day, "memo": card._payment_memo()})
        payment.action_post()
        card.sudo().write({"state": "confirmed", "payment_id": payment.id,
                           "confirmed_by_id": self.env.user.id,
                           "confirmed_at": fields.Datetime.now()})
        receipt.with_context(distri_receipt_matching=True).write({
            "distri_receipt_state": "matched", "distri_collection_id": card.id})
        return receipt, card

    def test_pending_receipt_gets_one_verify_card(self):
        # 19.0.1.126.0: the same column, now «Para acreditar»
        self.assertEqual(dict(COLUMNS)["verify"], "Para acreditar")
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000002")
        self.assertEqual(receipt.distri_receipt_state, "pending")
        card = self._verify_card(receipt)
        self.assertEqual(len(card), 1)
        self.assertEqual(card.column, "verify")
        self.assertEqual(card.state, "draft")
        self.assertEqual(card.partner_id, self.photo)
        self.assertEqual(card.amount, 38777)
        self.assertEqual(card.operation, "900000000002")
        self.assertIn("Teléfono", card.proposal_note)
        self.assertIn("900000000002", card.detail)
        self.assertTrue(card.waiting_label.startswith("Esperando hace "))
        self.assertEqual(receipt.distri_collection_id, card)
        self.assertIn(card, self._board())
        # processed again, edited, marked again, backfilled: still one card
        receipt._distri_read_receipt()
        receipt.write({"distri_amount": 38800})
        receipt.action_distri_mark_receipt()
        self.env["distri.whatsapp.message"]._distri_backfill_verify_cards()
        cards = self.Collection.search([("receipt_id", "=", receipt.id)])
        self.assertEqual(cards, card)
        self.assertEqual(card.amount, 38800, "the card follows the receipt")
        # a customer typed on the receipt reaches the card
        receipt.distri_partner_id = self.other
        self.assertEqual(card.partner_id, self.other)
        self.assertIn("a mano", card.proposal_note)

    def test_verify_card_moves_on_when_the_report_brings_the_money(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000002")
        card = self._verify_card(receipt)
        self.assertEqual(card.column, "verify")
        known = self._receipt("5491100000009", datetime(2026, 9, 24, 13, 0),
                              caption="Comprobante $ 45.321 operación 900000000001")
        known_card = self._verify_card(known)
        self._import("mp_report_25col.csv")
        # nobody touched the cards: both leave «Verificar en MP» by themselves
        for waiting in (card, known_card):
            self.assertEqual(waiting.state, "ignored")
            self.assertEqual(waiting.ignore_reason, "receipt_late")
            self.assertNotIn(waiting, self._board())
        # the money of a known payer is ready to confirm, with its receipt
        income = self._card("900000000001")
        self.assertEqual(income.column, "confirm")
        self.assertEqual(income.receipt_id, known)
        self.assertEqual(income.receipt_operation, "900000000001")
        # a payer not known yet goes to «Revisar», saying why
        income = self._card("900000000002")
        self.assertEqual(income.column, "review")
        self.assertEqual(income.receipt_id, receipt)
        self.assertTrue(income.review_reason)

    def test_authorize_delivery_registers_nothing(self):
        invoice = self._invoice(self.photo, 50000, "2026-09-20")
        receipt, card = self._authorized_receipt()
        self.assertEqual(card.state, "authorized")
        self.assertEqual(card.authorized_by_id, self.env.user)
        self.assertTrue(card.authorized_at)
        self.assertIn(self.env.user.name, card.authorized_label)
        self.assertEqual(card.column, "verify", "it keeps waiting for the report")
        self.assertIn(card, self._board())
        self.assertTrue(card.waiting_label)
        self.assertEqual(receipt.distri_receipt_state, "matched",
                         "no other transfer takes the receipt by its amount")
        # nothing reaches the customer's account
        self.assertFalse(card.payment_id)
        self.assertFalse(card.confirmed_by_id)
        self.assertFalse(self._mp_payments("900000000002"))
        self.assertFalse(self._partner_payments(self.photo))
        self.assertEqual(invoice.amount_residual, 50000)
        self.assertTrue(any("No se registró ningún pago" in (m.body or "")
                            for m in card.message_ids))
        with self.assertRaises(UserError):
            card.action_verify()
        # the same receipt sent twice: the copy is «Ya recibido», never a card
        twin = self._receipt("5492995000111", datetime(2026, 9, 24, 15, 0),
                             caption="Comprobante $ 38.777 operación 900000000002")
        self.assertEqual(twin.distri_receipt_state, "duplicate")
        self.assertFalse(self._verify_card(twin))
        # the receipt read again never undoes the authorization
        receipt._distri_read_receipt()
        self.assertEqual(card.state, "authorized")
        self.assertEqual(self.Collection.search([("receipt_id", "=", receipt.id)]), card)
        # the board card says it: «Autorizado para entregar · falta el reporte»
        arch = self.env.ref("distri_pedido.view_mp_collection_kanban").arch_db
        self.assertIn(AUTHORIZED_LABEL, arch)
        # 19.0.1.126.0: no new authorizations are offered: «Acreditar» instead
        self.assertNotIn('name="action_verify"', arch)
        self.assertIn('name="action_credit"', arch)

    def test_undo_authorization_before_the_report(self):
        receipt, card = self._authorized_receipt()
        with self.assertRaises(UserError):
            card.with_user(self.driver).action_undo()
        card.action_undo()
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.authorized_by_id)
        self.assertFalse(card.authorized_at)
        self.assertEqual(card.column, "verify")
        self.assertEqual(receipt.distri_receipt_state, "pending")
        self.assertEqual(self.Collection.search([("receipt_id", "=", receipt.id)]), card)
        # the report brings it: the ordinary flow of a waiting receipt
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.proposal, "operation")
        self.assertEqual(income.receipt_id, receipt)
        self.assertEqual(card.state, "ignored")
        self.assertFalse(self._mp_payments("900000000002"))

    def test_verify_needs_customer_amount_and_operation(self):
        receipt = self._receipt("5491100000001", datetime(2026, 9, 26, 12, 0),
                                caption="Comprobante $ 5.000 operación 900000000091")
        card = self._verify_card(receipt)
        self.assertFalse(card.partner_id)
        self.assertEqual(card.proposal, "none")
        with self.assertRaises(UserError):
            card.action_verify()
        self.env["distri.mp.assign"].create({
            "collection_id": card.id, "partner_id": self.other.id}).action_assign()
        self.assertEqual(card.partner_id, self.other)
        self.assertEqual(receipt.distri_partner_id, self.other)
        self.assertEqual(card.column, "verify")

        no_amount = self._receipt("5492995000111", datetime(2026, 9, 26, 13, 0),
                                  caption="Comprobante operación 900000000092")
        with self.assertRaises(UserError):
            self._verify_card(no_amount).action_verify()
        no_operation = self._receipt("5492995000111", datetime(2026, 9, 26, 14, 0),
                                     caption="Comprobante $ 6.000")
        with self.assertRaises(UserError):
            self._verify_card(no_operation).action_verify()
        self.assertFalse(self._mp_payments("90000000009"))
        with self.assertRaises(UserError):
            card.with_user(self.driver).action_verify()
        card.action_verify()
        self.assertEqual(card.state, "authorized")
        self.assertEqual(card.partner_id, self.other)
        self.assertFalse(card.payment_id)
        self.assertFalse(self._partner_payments(self.other))

    def test_report_brings_the_authorized_operation(self):
        invoice = self._invoice(self.photo, 50000, "2026-09-20")
        receipt, card = self._authorized_receipt()
        report = self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        # proposed with the card's customer, ready to confirm
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.column, "confirm")
        self.assertEqual(income.proposal, "authorized")
        self.assertEqual(income.partner_id, self.photo)
        self.assertEqual(income.receipt_id, receipt)
        self.assertIn("Autorizado a mano por %s el" % self.env.user.name, income.proposal_note)
        self.assertEqual(income.proposal_badge, "Entrega autorizada")
        self.assertEqual(card.income_id, income)
        self.assertFalse(card.report_review)
        self.assertNotIn(card, self._board(), "the income carries it now")
        self.assertEqual(report.no_money_count, 0)
        self.assertFalse(self.Collection.search([("kind", "=", "receipt"),
                                                 ("receipt_id", "=", receipt.id)]))
        self.assertFalse(self._mp_payments("900000000002"), "still nothing registered")
        self.assertEqual(invoice.amount_residual, 50000)
        # an authorization is not undone once the report brought it and it
        # was confirmed; before that it still can
        self.Collection.action_confirm_ready()
        self.assertEqual(income.state, "confirmed")
        payments = self._mp_payments("900000000002")
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments, income.payment_id)
        self.assertEqual(payments.partner_id, self.photo)
        self.assertEqual(payments.amount, 38777)
        self.assertEqual(invoice.amount_residual, 50000 - 38777)
        self.assertEqual(self._partner_payments(self.photo), payments)
        with self.assertRaises(UserError):
            card.action_undo()
        self.assertEqual(card.state, "authorized")
        # the same operation in an overlapping report changes nothing
        self._import("mp_report_69col.csv")
        self.assertEqual(self._mp_payments("900000000002"), payments)

    def test_undo_authorization_after_the_report_ties_it(self):
        receipt, card = self._authorized_receipt()
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.proposal, "authorized")
        card.action_undo()
        self.assertFalse(card.income_id)
        self.assertEqual(card.state, "ignored", "the photo now lives on the income")
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.proposal, "operation")
        self.assertEqual(income.receipt_id, receipt)
        self.assertFalse(self._mp_payments("900000000002"))
        income.action_confirm()
        self.assertEqual(len(self._mp_payments("900000000002")), 1)

    def test_authorized_other_amount_goes_to_review(self):
        receipt, card = self._authorized_receipt(amount_text="38.000")
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.column, "review")
        self.assertEqual(income.proposal, "authorized")
        self.assertEqual(income.review_reason, "Entrega autorizada a mano con otro importe")
        self.assertIn("revisá", income.proposal_note)
        self.Collection.action_confirm_ready()
        self.assertEqual(income.state, "draft", "«Confirmar todos» never takes it")
        # nothing was registered before: confirming it registers the only payment
        income.action_confirm()
        payments = self._mp_payments("900000000002")
        self.assertEqual(len(payments), 1)
        self.assertEqual(payments.amount, 38777)

    def test_authorized_for_another_customer_than_the_cuit_goes_to_review(self):
        # the report's payer CUIT is the known customer's own
        receipt, card = self._authorized_receipt(amount_text="45.321", operation="900000000001")
        self.assertEqual(card.partner_id, self.photo)
        self._import("mp_report_25col.csv")
        income = self._card("900000000001")
        self.assertEqual(card.income_id, income)
        self.assertEqual(income.proposal, "authorized")
        self.assertEqual(income.partner_id, self.photo)
        self.assertEqual(income.column, "review")
        self.assertEqual(income.authorized_conflict, self.known.name)
        self.assertIn(self.photo.name, income.proposal_note)
        self.assertIn(self.known.name, income.proposal_note)
        self.assertEqual(income.review_reason,
                         "Entrega autorizada a mano, pero el CUIT es de otro cliente")
        self.Collection.action_confirm_ready()
        self.assertEqual(income.state, "draft", "«Confirmar todos» never takes it")
        self.assertFalse(self._mp_payments("900000000001"))

    def test_mark_receipt_again_refused_while_authorized_or_paid(self):
        receipt, card = self._authorized_receipt()
        with self.assertRaises(UserError):
            receipt.action_distri_mark_receipt()
        self.assertEqual(receipt.distri_receipt_state, "matched")
        self.assertEqual(card.state, "authorized")
        card.action_undo()
        receipt.action_distri_mark_receipt()
        self.assertEqual(card.state, "draft")
        self.assertEqual(receipt.distri_receipt_state, "pending")
        # a card paid by hand before 19.0.1.21.0: the same
        legacy, legacy_card = self._legacy_verified_receipt(
            amount_text="77.001", operation="900000000077", when=datetime(2026, 9, 24, 18, 0))
        with self.assertRaises(UserError):
            legacy.action_distri_mark_receipt()
        self.assertEqual(legacy.distri_receipt_state, "matched")
        self.assertEqual(legacy_card.state, "confirmed")

    def test_authorized_missing_from_the_report_goes_to_no_money(self):
        receipt, card = self._authorized_receipt(amount_text="77.001", operation="900000000077",
                                                 when=datetime(2026, 9, 24, 18, 0))
        waiting = self._receipt("5492995000111", datetime(2026, 9, 24, 19, 0),
                                caption="Comprobante $ 66.001 operación 900000000066")
        waiting_card = self._verify_card(waiting)
        report = self._import("mp_report_25col.csv")
        self.assertTrue(card.report_review)
        self.assertEqual(card.state, "authorized")
        self.assertEqual(card.column, "no_money")
        self.assertEqual(card.proposal_note, AUTHORIZED_MISSING)
        self.assertIn(card, self._board())
        self.assertIn(AUTHORIZED_MISSING, report.summary)
        self.assertFalse(self._mp_payments("900000000077"))
        self.assertFalse(self._partner_payments(self.photo))
        # the unauthorized receipt without money moves to «Sin transferencia»: same card
        self.assertEqual(report.no_money_count, 1)
        self.assertEqual(waiting_card.kind, "receipt")
        self.assertEqual(waiting_card.column, "no_money")
        # «Revisado» does not hide it
        card.action_clear_review()
        self.assertTrue(card.report_review)
        # priority: alerted like the other cards of «Sin transferencia»
        self._aged(card, 30)
        alerted = self.Collection._cron_alerts()
        self.assertIn(card, alerted)
        self.assertIn(AUTHORIZED_MISSING, self._alerts(card).note)
        # never a second alert while the first is open
        self.assertNotIn(card, self.Collection._cron_alerts())
        self.assertEqual(len(self._alerts(card)), 1)

    def test_authorized_missing_arrives_in_a_later_report(self):
        receipt, card = self._authorized_receipt()
        # a report of the same moment that does not bring the operation
        csv = _fixture("mp_report_25col.csv").decode().splitlines()
        partial = "\n".join([csv[0]] + [l for l in csv[1:] if ";900000000002;" not in l])
        self.Report._import_file("parcial.csv", partial.encode())
        self.assertEqual(card.column, "no_money")
        self._aged(card, 30)
        self.Collection._cron_alerts()
        self.assertTrue(self._alerts(card))
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(card.income_id, income)
        self.assertFalse(card.report_review)
        self.assertFalse(self._alerts(card), "the alert is done")
        self.assertEqual(income.proposal, "authorized")
        self.assertEqual(income.partner_id, self.photo)
        self.assertEqual(income.column, "confirm")

    def test_undo_authorization_missing_from_the_report(self):
        receipt, card = self._authorized_receipt(amount_text="77.001", operation="900000000077",
                                                 when=datetime(2026, 9, 24, 18, 0))
        self._import("mp_report_25col.csv")
        self._aged(card, 30)
        self.Collection._cron_alerts()
        self.assertTrue(self._alerts(card))
        card.action_undo()
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.report_review)
        self.assertEqual(card.column, "verify")
        self.assertEqual(receipt.distri_receipt_state, "pending")
        self.assertFalse(self._alerts(card))
        self.assertFalse(self._partner_payments(self.photo))

    # before 19.0.1.21.0 «Verificado en MP» registered the payment at once:
    # those cards keep working

    def test_legacy_report_after_hand_verification_does_not_pay_twice(self):
        receipt, card = self._legacy_verified_receipt()
        self.assertEqual(card.state, "confirmed")
        self.assertTrue(card.payment_id)
        report = self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, card.payment_id)
        self.assertEqual(income.proposal, "verified")
        self.assertEqual(income.partner_id, self.photo)
        self.assertEqual(income.receipt_id, receipt)
        self.assertIn("Verificado a mano el", income.proposal_note)
        self.assertEqual(card.income_id, income)
        self.assertFalse(card.report_review)
        self.assertEqual(self._mp_payments("900000000002"), card.payment_id)
        self.assertEqual(receipt.distri_receipt_state, "matched")
        self.assertFalse(self.Collection.search([("kind", "=", "receipt"),
                                                 ("receipt_id", "=", receipt.id)]))
        self.assertEqual(report.no_money_count, 0)
        with self.assertRaises(UserError):
            income.action_undo()
        # the same operation in an overlapping report changes nothing
        self._import("mp_report_69col.csv")
        self.assertEqual(self._mp_payments("900000000002"), card.payment_id)
        # nor can it be authorized again: the copy is «Ya recibido»
        twin = self._receipt("5492995000111", datetime(2026, 9, 24, 15, 0),
                             caption="Comprobante $ 38.777 operación 900000000002")
        self.assertEqual(twin.distri_receipt_state, "duplicate")
        self.assertFalse(self._verify_card(twin))

    def test_legacy_hand_verified_other_amount_goes_to_review(self):
        receipt, card = self._legacy_verified_receipt(amount_text="38.000")
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.column, "review")
        self.assertEqual(income.proposal, "verified")
        self.assertIn("Verificado a mano", income.proposal_note)
        self.assertIn("revisá", income.proposal_note)
        # the card shows both flags
        self.assertTrue(income.hand_verified)
        self.assertTrue(income.amount_mismatch)
        self.assertEqual(income.review_reason, "Verificado a mano con otro importe")
        self.assertEqual(card.income_id, income)
        with self.assertRaises(UserError):
            income.action_confirm()
        self.Collection.action_confirm_ready()
        self.assertEqual(income.state, "draft", "«Confirmar todos» never takes it")
        self.assertEqual(len(self._mp_payments("900000000002")), 1)
        # the hand verification was wrong: undone, the income is paid by itself
        card.action_undo()
        self.assertEqual(card.payment_id.id, False)
        self.assertFalse(card.income_id)
        self.assertEqual(income.receipt_id, receipt)
        self.assertEqual(receipt.distri_receipt_state, "matched")
        self.assertEqual(card.state, "ignored", "the photo now lives on the income")
        income.action_confirm()
        payments = self._mp_payments("900000000002")
        self.assertEqual(payments, income.payment_id)
        self.assertEqual(payments.amount, 38777)

    def test_legacy_hand_verified_missing_goes_to_no_money(self):
        receipt, card = self._legacy_verified_receipt(
            amount_text="77.001", operation="900000000077", when=datetime(2026, 9, 24, 18, 0))
        self._import("mp_report_25col.csv")
        self.assertTrue(card.report_review)
        self.assertEqual(card.column, "no_money")
        self.assertEqual(card.proposal_note, HAND_VERIFIED_MISSING)
        self.assertIn("sistema anterior", card.proposal_note)
        self.assertTrue(card.hand_verified)
        self.assertIn(card, self._board())
        self.assertEqual(card.state, "confirmed", "the payment stays until someone decides")
        self.assertFalse(self.Collection.search([("kind", "=", "receipt"),
                                                 ("receipt_id", "=", receipt.id)]))
        self._aged(card, 30)
        self.assertIn(card, self.Collection._cron_alerts())
        self.assertIn("sistema anterior", self._alerts(card).note)
        card.action_clear_review()
        self.assertFalse(card.report_review)
        self.assertNotIn(card, self._board())

    def test_discard_verify_card(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 26, 12, 0),
                                caption="Comprobante $ 9.999 operación 900000000093")
        card = self._verify_card(receipt)
        self.env["distri.mp.ignore"].create({
            "collection_id": card.id, "reason": "receipt_fake"}).action_ignore()
        self.assertEqual(card.state, "ignored")
        self.assertEqual(receipt.distri_receipt_state, "dismissed")
        self.assertFalse(card.payment_id)
        self.assertNotIn(card, self._board())
        card.action_undo()
        self.assertEqual(card.state, "draft")
        self.assertEqual(receipt.distri_receipt_state, "pending")
        self.assertEqual(self.Collection.search([("receipt_id", "=", receipt.id)]), card)
        # «No es comprobante» in the receipts list closes the card too
        receipt.action_distri_dismiss_receipt()
        self.assertEqual(card.state, "ignored")
        receipt.action_distri_mark_receipt()
        self.assertEqual(card.state, "draft")
        self.assertEqual(self.Collection.search([("receipt_id", "=", receipt.id)]), card)

    def test_verify_card_closes_when_the_report_brings_the_money(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000002")
        card = self._verify_card(receipt)
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.receipt_id, receipt)
        self.assertEqual(income.proposal, "operation")
        self.assertEqual(card.state, "ignored")
        self.assertNotIn(card, self._board())
        # a receipt of an operation the report already brought is not verified by hand
        self._card("900000000001").action_confirm()
        late = self._receipt("5492995000111", datetime(2026, 9, 24, 23, 0),
                             caption="Comprobante $ 45.321 operación 900000000001")
        self.assertEqual(late.distri_receipt_state, "registered")
        self.assertIn("Ya registrado", late.distri_receipt_note)
        self.assertFalse(self._verify_card(late), "no card: nothing to verify by hand")
        self.assertEqual(len(self._mp_payments("900000000001")), 1)

    # ------------------------------------------------------------ review 2026-09-29

    def _report(self, *rows):
        """A synthetic report with the 25 columns of the real one."""
        header = _fixture("mp_report_25col.csv").decode().splitlines()[0]
        columns = header.split(";")
        lines = [header] + [";".join(str(row.get(c, "")) for c in columns) for row in rows]
        return ("\n".join(lines) + "\n").encode()

    def _row(self, source, when, amount, kind="SETTLEMENT", method="bank_transfer", cuit=""):
        return {"TRANSACTION_DATE": when, "SOURCE_ID": source, "PAYMENT_METHOD_TYPE": method,
                "TRANSACTION_TYPE": kind, "TRANSACTION_AMOUNT": "%.2f" % amount,
                "PAYMENT_METHOD": method, "PAYER_ID_TYPE": "CUIT" if cuit else "",
                "PAYER_ID_NUMBER": cuit}

    def test_refund_of_a_pending_transfer_goes_to_review(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        self.assertEqual(card.column, "confirm")
        self.Report._import_file("devolucion.csv", self._report(self._row(
            "900000000001", "2026-09-25T10:00:00.000-03:00", -45321, kind="REFUND",
            method="available_money")))
        self.assertIn("Devolución", card.reversal)
        self.assertEqual(card.column, "review")
        self.assertIn("Devolución", card.review_reason)
        self.Collection.action_confirm_ready()
        self.assertEqual(card.state, "draft", "«Confirmar todos» never takes it")
        self.assertFalse(self._mp_payments("900000000001"))

    def test_refund_before_its_transfer_in_the_same_file(self):
        self.Report._import_file("mismo.csv", self._report(
            self._row("900000000201", "2026-09-24T10:05:00.000-03:00", -1000, kind="REFUND"),
            self._row("900000000201", "2026-09-24T10:00:00.000-03:00", 1000,
                      cuit="20999000011")))
        card = self._card("900000000201")
        self.assertEqual(card.partner_id, self.known)
        self.assertIn("Devolución", card.reversal)
        self.assertEqual(card.column, "review")

    def test_chargeback_of_a_confirmed_transfer_alerts_administration(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        card.action_confirm()
        chargeback = self._report(self._row(
            "900000000001", "2026-09-26T10:00:00.000-03:00", -45321, kind="CHARGEBACK",
            method="available_money"))
        self.Report._import_file("contracargo.csv", chargeback)
        card.invalidate_recordset(["activity_ids"])
        activity = card.activity_ids.filtered(lambda a: a.summary == REVERSAL_SUMMARY)
        self.assertEqual(len(activity), 1)
        self.assertIn("Contracargo", activity.note)
        self.assertIn(activity.user_id, self.env.ref("distri_pedido.grupo_administracion").user_ids)
        self.assertEqual(card.state, "confirmed", "the payment stays until a person decides")
        # the same report again: no second activity
        self.Report._import_file("contracargo-2.csv", chargeback)
        card.invalidate_recordset(["activity_ids"])
        self.assertEqual(len(card.activity_ids.filtered(
            lambda a: a.summary == REVERSAL_SUMMARY)), 1)

    def test_receipt_by_amount_goes_to_the_closest_transfer(self):
        # no operation number: the 21:05 photo is the 21:00 transfer's, not
        # the 08:00 one's that comes first in the report
        receipt = self._receipt("5492995000111", datetime(2026, 9, 25, 0, 5),
                                caption="Te pasé $ 400.000")
        self.assertFalse(receipt.distri_operation)
        self.Report._import_file("same_amount.csv", self._same_amount_report())
        early, late = self._card("900000000101"), self._card("900000000102")
        self.assertEqual(late.receipt_id, receipt)
        self.assertFalse(early.receipt_id)

    def test_receipt_by_amount_only_never_teaches_the_phone(self):
        # the known CUIT proposes the customer; a photo of an unknown number
        # with the same amount and time is tied to it by amount only
        receipt = self._receipt("5491100000077", datetime(2026, 9, 24, 13, 20),
                                caption="Te pasé $ 45.321")
        self.assertFalse(receipt.distri_partner_id)
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        self.assertEqual(card.receipt_id, receipt)
        self.assertEqual(card.proposal, "cuit")
        self.assertTrue(card._receipt_by_amount_only())
        card.action_confirm()
        self.assertFalse(self.env["res.partner"]._distri_partners_by_phone("5491100000077"),
                         "the phone is not learned from an amount-only link")
        self.assertFalse(receipt.distri_partner_id)

    def test_photo_by_amount_only_never_teaches_the_cuit(self):
        # the payer's CUIT is new; the customer came from a photo tied by amount
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 15, 25),
                                caption="Te pasé $ 12.345,50")
        self._import("mp_report_25col.csv")
        card = self._card("900000000003")
        self.assertEqual(card.receipt_id, receipt)
        self.assertEqual(card.proposal, "photo")
        self.assertEqual(card.column, "review")
        card.action_confirm()
        self.assertFalse(self.env["distri.payer.identity"].search(
            [("value", "=", "20999000029")]), "a photo by amount never teaches the CUIT")

    def test_report_covers_until_it_was_generated(self):
        # the report was generated at 23:59:46; its last transfer is 10:00
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 23, 0),
                                caption="comprobante $ 7.777")
        receipt_auth, card = self._authorized_receipt(
            amount_text="8.888", operation="900000000301", when=datetime(2026, 9, 24, 22, 0))
        report = self.Report._import_file(
            "settlement_v2-100000001-manual-2026-09-24-235946.csv",
            self._report(self._row("900000000300", "2026-09-24T10:00:00.000-03:00", 1000)))
        self.assertEqual(report.covers_until, datetime(2026, 9, 25, 2, 59, 46))
        self.assertEqual(receipt.distri_receipt_state, "no_money")
        self.assertEqual(report.no_money_count, 1)
        self.assertTrue(card.report_review)
        self.assertEqual(card.column, "no_money")

    def test_authorized_card_still_waiting_is_alerted(self):
        receipt, card = self._authorized_receipt()
        fresh_receipt, fresh = self._authorized_receipt(
            amount_text="5.555", operation="900000000055", when=datetime(2026, 9, 24, 16, 0))
        self.assertEqual((card.column, fresh.column), ("verify", "verify"))
        card.sudo().authorized_at = fields.Datetime.now() - timedelta(hours=30)
        alerted = self.Collection._cron_alerts()
        self.assertIn(card, alerted)
        self.assertNotIn(fresh, alerted)
        self.assertIn(AUTHORIZED_WAITING, self._alerts(card).note)
        self.assertNotIn(card, self.Collection._cron_alerts(), "never a second one")
        # the alert runs every hour, so 24 h means about 24 h
        cron = self.env.ref("distri_pedido.cron_mp_board_alerts")
        self.assertEqual((cron.interval_number, cron.interval_type), (1, "hours"))

    def test_confirm_one_pays_only_pending_incomes_of_the_report(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000002")
        card = self._verify_card(receipt)
        with self.assertRaises(UserError):
            card._confirm_one()
        self.assertFalse(card.payment_id)
        self.assertFalse(self._partner_payments(self.photo))
        self._import("mp_report_25col.csv")
        income = self._card("900000000001")
        income.action_confirm()
        with self.assertRaises(UserError):
            income._confirm_one()
        self.assertEqual(len(self._mp_payments("900000000001")), 1)

    def test_assign_keeps_the_gate_of_an_authorized_delivery(self):
        receipt, card = self._authorized_receipt(amount_text="45.321", operation="900000000001")
        self._import("mp_report_25col.csv")
        income = self._card("900000000001")
        self.assertEqual(income.column, "review")
        with self.assertRaises(UserError):
            self.env["distri.mp.assign"].create({
                "collection_id": income.id, "partner_id": self.known.id}).action_assign()
        self.env["distri.mp.assign"].create({
            "collection_id": income.id, "partner_id": self.photo.id}).action_assign()
        self.assertEqual(income.proposal, "authorized")
        self.assertEqual(income.column, "review", "the CUIT conflict still holds it")
        self.assertFalse(self.env["distri.payer.identity"].search(
            [("partner_id", "=", self.photo.id), ("kind", "=", "cuit")]))
        self.Collection.action_confirm_ready()
        self.assertEqual(income.state, "draft")

    def test_hand_verified_income_never_stops_confirm_all(self):
        receipt, card = self._legacy_verified_receipt(amount_text="38.000")
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        with self.assertRaises(UserError):
            self.env["distri.mp.assign"].create({
                "collection_id": income.id, "partner_id": self.photo.id}).action_assign()
        # data left in «Confirmado» by the old wizard: skipped, the others go on
        income.sudo().write({"proposal": "manual"})
        self.assertEqual(income.column, "confirm")
        result = self.Collection.action_confirm_ready()
        self.assertEqual(result["params"]["type"], "warning")
        self.assertIn(income.name, result["params"]["message"])
        self.assertEqual(income.state, "draft")
        self.assertEqual(self._card("900000000001").state, "confirmed")
        self.assertEqual(len(self._mp_payments("900000000002")), 1)

    def test_card_name_without_receipt(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000002")
        card = self._verify_card(receipt)
        card.sudo().receipt_id = False
        self.assertEqual(card.name, "Foto %s" % str(card.id)[-8:])

    def test_sftp_file_already_imported_is_only_moved(self):
        folder = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, folder, True)
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.mp_carpeta_reportes", folder)
        name = "settlement_v2-100000001-2026-09-26.csv"
        self.Report.sudo().create({"name": name, "source": "sftp"})
        with open(os.path.join(folder, name), "wb") as f:
            f.write(_fixture("mp_report_25col.csv"))
        self.Report._cron_import_folder()
        self.assertEqual(self.Report.search_count([("name", "=", name)]), 1)
        self.assertFalse(self._card("900000000001"))
        self.assertTrue(os.path.exists(os.path.join(folder, "procesados", name)))

    # ------------------------------------------------------------ reading and linking 19.0.1.23.0

    def _read_receipt(self, phone, when, amount_text, read_at=None, operation="", partner=None):
        """A receipt whose photo was read: amount, operation number and the
        date and time stamped on it (naive UTC, as Odoo stores it), known
        before the first matching, as the OCR leaves them."""
        caption = "Comprobante $ %s" % amount_text
        if operation:
            caption += " operación %s" % operation
        values = {
            "wa_message_id": "wamid.test.%s" % when.isoformat(), "direction": "in",
            "wa_id": phone, "message_type": "image", "body": caption, "timestamp": when,
            "media_mime_type": "image/jpeg", "media_id": "123", "media_state": "pending",
        }
        if read_at:
            values["distri_receipt_date"] = read_at
        if partner:
            values["distri_partner_id"] = partner.id
        return self.env["distri.whatsapp.message"].sudo().create(values)

    def test_operation_close_allows_one_ocr_slip(self):
        self.assertTrue(operation_close("900000000002", "900000000002"))
        self.assertTrue(operation_close("900000000092", "900000000002"), "one digit wrong")
        self.assertTrue(operation_close("90000000000", "900000000002"), "one digit dropped")
        self.assertTrue(operation_close("9000000000021", "900000000002"), "one digit added")
        self.assertFalse(operation_close("900000000012", "900000000021"), "two digits")
        self.assertFalse(operation_close("123456", "1234567"), "too short to be a Mercado Pago id")
        self.assertFalse(operation_close("", "900000000002"))

    def test_own_account_comes_from_the_parameter(self):
        self.assertIn("23999999999", own_account(self.env))
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.cuenta_propia", "30-71234567-0, Distri Prueba SRL, mi.alias.mp")
        own = own_account(self.env)
        self.assertEqual(own, {"30712345670", "distri prueba srl", "mi.alias.mp"})
        self.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia", "")
        self.assertEqual(own_account(self.env), set(), "empty: no own account in the code")

    def test_old_receipt_finds_its_transfer_by_the_date_read_on_it(self):
        # the bank transfer of 38.777 was at 11:34:47 of the 24th (14:34 UTC);
        # the customer sends the receipt sixteen days later
        self._import("mp_report_25col.csv")
        card = self._card("900000000002")
        self.assertEqual(card.column, "unidentified")
        receipt = self._read_receipt("5492995000111", datetime(2026, 10, 10, 18, 0), "38.777",
                                     read_at=datetime(2026, 9, 24, 14, 35))
        self.assertEqual(receipt.distri_receipt_state, "matched")
        self.assertEqual(card.receipt_id, receipt)
        self.assertEqual(card.partner_id, self.photo)
        self.assertEqual(card.column, "review")
        self.assertFalse(card.date_mismatch)

    def test_receipt_stamped_within_two_minutes_beats_the_arrival_time(self):
        # two transfers of 400.000: 08:00 and 21:00 local; the photo of the
        # 08:00 one arrives at 21:03, when the arrival time would point to
        # the 21:00 transfer
        self.Report._import_file("same_amount.csv", self._same_amount_report())
        early, late = self._card("900000000101"), self._card("900000000102")
        receipt = self._read_receipt("5492995000111", datetime(2026, 9, 25, 0, 3), "400.000",
                                     read_at=datetime(2026, 9, 24, 11, 1))
        self.assertEqual(early.receipt_id, receipt)
        self.assertFalse(late.receipt_id)

    def test_receipt_date_far_from_the_transfer_is_flagged(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000002")
        receipt = self._read_receipt("5492995000111", datetime(2026, 9, 24, 14, 40), "38.777",
                                     read_at=datetime(2026, 9, 1, 10, 0))
        self.assertEqual(card.receipt_id, receipt, "the arrival window still pairs it")
        self.assertTrue(card.date_mismatch)
        self.assertIn("fecha leída", card.review_reason)

    def test_receipt_of_a_confirmed_transfer_is_already_registered(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        card.action_confirm()
        self.assertEqual(len(self._mp_payments("900000000001")), 1)
        # weeks later the customer sends the receipt of that transfer
        receipt = self._receipt("5491100000000", datetime(2026, 10, 20, 12, 0),
                                caption="Comprobante $ 45.321 operación 900000000001")
        self.assertEqual(receipt.distri_receipt_state, "registered")
        self.assertIn("Ya registrado", receipt.distri_receipt_note)
        self.assertIn(card.name, receipt.distri_receipt_note)
        self.assertFalse(self._verify_card(receipt), "no card waits for it")
        self.assertEqual(len(self._mp_payments("900000000001")), 1, "never a second payment")
        # the same by amount and time when the photo has no operation number
        by_time = self._read_receipt("5491100000000", datetime(2026, 10, 21, 12, 0), "45.321",
                                     read_at=datetime(2026, 9, 24, 13, 16))
        self.assertEqual(by_time.distri_receipt_state, "registered")
        # the report that covers the arrival does not make them «Sin transferencia»
        report = self.Report._import_file("octubre.csv", self._report(self._row(
            "900000000901", "2026-10-21T15:00:00.000-03:00", 1000)))
        self.assertEqual(report.no_money_count, 0)
        self.assertEqual(receipt.distri_receipt_state, "registered")

    def test_operation_one_digit_off_with_the_same_amount_is_the_operation(self):
        # the OCR read 900000000092 for 900000000002 (38.777)
        receipt = self._receipt("5491100000000", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000092")
        receipt.distri_partner_id = self.other
        self._import("mp_report_25col.csv")
        card = self._card("900000000002")
        self.assertEqual(card.receipt_id, receipt)
        self.assertEqual(card.proposal, "operation")
        self.assertTrue(card._receipt_by_operation())
        self.assertFalse(card._receipt_by_amount_only())
        # another amount: never the operation
        other = self._receipt("5491100000001", datetime(2026, 9, 24, 15, 40),
                              caption="Comprobante $ 12.345,50 operación 900000000092")
        self.assertNotEqual(other.distri_receipt_state, "matched")
        # after the report too: one digit dropped
        late = self._receipt("5491100000002", datetime(2026, 9, 24, 16, 0),
                             caption="Comprobante $ 12.345,50 operación 90000000000")
        self.assertEqual(self._card("900000000003").receipt_id, late)

    def test_same_receipt_sent_again_is_already_received(self):
        first = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                              caption="Comprobante $ 38.777 operación 900000000002")
        again = self._receipt("5492995000111", datetime(2026, 9, 24, 15, 0),
                              caption="Comprobante $ 38.777 operación 900000000002")
        self.assertEqual(first.distri_receipt_state, "pending")
        self.assertEqual(again.distri_receipt_state, "duplicate")
        self.assertIn("Ya recibido el 24/09 11:40", again.distri_receipt_note)
        self.assertFalse(self._verify_card(again), "no card for the copy")
        self._import("mp_report_25col.csv")
        self.assertEqual(self._card("900000000002").receipt_id, first)
        # a bank receipt without a number: same amount, date and time
        one = self._read_receipt("5491100000000", datetime(2026, 9, 24, 16, 0), "77.001",
                                 read_at=datetime(2026, 9, 24, 15, 58))
        two = self._read_receipt("5491100000000", datetime(2026, 9, 24, 16, 5), "77.001",
                                 read_at=datetime(2026, 9, 24, 15, 58))
        self.assertEqual(one.distri_receipt_state, "pending")
        self.assertEqual(two.distri_receipt_state, "duplicate")
        # marked as a new receipt by hand: it waits again
        two.action_distri_mark_receipt()
        self.assertEqual(two.distri_receipt_state, "pending")
        self.assertFalse(two.distri_receipt_note)

    def test_same_operation_for_two_customers_goes_to_review(self):
        first = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                              caption="Comprobante $ 38.777 operación 900000000002")
        self.assertEqual(first.distri_partner_id, self.photo)
        second = self._receipt("5491100000000", datetime(2026, 9, 24, 15, 0),
                               caption="Comprobante $ 38.777 operación 900000000002")
        self.assertEqual(second.distri_receipt_state, "duplicate", "no customer: a copy")
        second.action_distri_mark_receipt()
        second.distri_partner_id = self.other
        note = TWO_CUSTOMERS % (self.photo.name, self.other.name)
        self.assertEqual(first.distri_conflict, note)
        self.assertEqual(second.distri_conflict, note)
        self.assertEqual(second.distri_receipt_state, "pending", "not a copy: a person decides")
        for receipt in (first, second):
            card = self._verify_card(receipt).filtered(lambda c: c.state == "draft")
            self.assertEqual(card.column, "review")
            self.assertEqual(card.review_reason, note)
        self._import("mp_report_25col.csv")
        income = self._card("900000000002")
        self.assertEqual(income.receipt_id, first)
        self.assertEqual(income.conflict_note, note)
        self.assertEqual(income.column, "review")
        self.assertIn("dos clientes", income.review_reason)
        self.Collection.action_confirm_ready()
        self.assertEqual(income.state, "draft", "never confirmed on sight")

    def test_receipt_of_an_outgoing_transfer_is_never_a_payment(self):
        # the report brings the company's own transfer 900000000005 (-7.600)
        self._import("mp_report_25col.csv")
        outgoing = self.env["distri.mp.outgoing"].search([("source_id", "=", "900000000005")])
        self.assertEqual(len(outgoing), 1)
        self.assertEqual(outgoing.amount, -7600)
        self.assertFalse(self._card("900000000005"))
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 21, 0),
                                caption="Comprobante $ 7.600 operación 900000000005")
        self.assertEqual(receipt.distri_receipt_state, "outgoing")
        self.assertIn("egreso", receipt.distri_receipt_note)
        self.assertFalse(self._verify_card(receipt), "no card for the company's own transfer")
        # the payer read on the photo is the company itself
        refund = self._receipt("5492995000111", datetime(2026, 9, 24, 21, 10),
                               caption="Comprobante $ 5.000")
        refund.with_context(distri_receipt_matching=True).write({"distri_payer_own": True})
        refund._distri_classify()
        self.assertEqual(refund.distri_receipt_state, "outgoing")
        self.assertIn("cuenta propia", refund.distri_receipt_note)
        self.assertEqual(len(self._mp_payments("900000000005")), 0)

    def test_operation_of_an_income_is_never_outgoing(self):
        # the photo says the company paid (a misread), but the operation
        # number is an income of the report: a customer's payment
        self._import("mp_report_25col.csv")
        receipt = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.test.own", "direction": "in", "wa_id": "5492995000111",
            "message_type": "image", "body": "Comprobante $ 38.777 operación 900000000002",
            "timestamp": datetime(2026, 9, 24, 14, 40), "media_mime_type": "image/jpeg",
            "media_id": "123", "media_state": "pending", "distri_payer_own": True,
        })
        self.assertEqual(receipt.distri_receipt_state, "matched")
        self.assertEqual(self._card("900000000002").receipt_id, receipt)
        # without the income it is a refund
        refund = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.test.own2", "direction": "in", "wa_id": "5492995000111",
            "message_type": "image", "body": "Comprobante $ 3.000 operación 900000000777",
            "timestamp": datetime(2026, 9, 24, 14, 50), "media_mime_type": "image/jpeg",
            "media_id": "123", "media_state": "pending", "distri_payer_own": True,
        })
        self.assertEqual(refund.distri_receipt_state, "outgoing")

    def test_destination_to_another_account_is_visible_on_the_card(self):
        receipt = self._receipt("5492995000111", datetime(2026, 9, 24, 14, 40),
                                caption="Comprobante $ 38.777 operación 900000000002")
        card = self._verify_card(receipt)
        self.assertFalse(card.receipt_destination)
        self.assertFalse(card.receipt_other_account)
        receipt.with_context(distri_receipt_matching=True).write({
            "distri_destination": "otro.negocio.mp", "distri_dest_name": "Otro Negocio"})
        self.assertTrue(receipt.distri_other_account)
        self.assertTrue(card.receipt_other_account)
        self.assertEqual(card.receipt_destination, "Alias otro.negocio.mp · Otro Negocio")
        receipt.with_context(distri_receipt_matching=True).write({
            "distri_destination": "0000003100090000000018", "distri_dest_cuit": "23999999999"})
        self.assertFalse(receipt.distri_other_account)
        self.assertFalse(card.receipt_other_account)
        self.assertIn("CVU/CBU 0000003100090000000018", card.receipt_destination)

    # ------------------------------------------------------------ «Confirmado sin comprobante»

    def test_confirm_without_receipt_is_allowed_after_a_notice_and_flagged(self):
        flagged_before = self.Collection.distri_board_counters()["without_receipt"]["count"]
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        self.assertFalse(card.receipt_id)
        result = card.action_confirm_board()
        self.assertEqual(card.state, "draft", "nothing registered before the notice")
        notice = self.env["distri.mp.confirm.notice"].browse(result["res_id"])
        self.assertEqual(notice.notice, WITHOUT_RECEIPT_NOTICE)
        self.assertEqual(notice.missing_count, 1)
        done = notice.action_apply()
        self.assertEqual(done["params"]["next"], {"type": "ir.actions.act_window_close"})
        self.assertIn("Quedó marcado «Confirmado sin comprobante»", done["params"]["message"])
        self.assertEqual(card.state, "confirmed")
        self.assertTrue(card.payment_id)
        self.assertTrue(card.confirmed_without_receipt)
        self.assertEqual(card.confirmed_by_id, self.env.user)
        self.assertIn("Confirmado sin comprobante", card.message_ids[:1].body)
        # the board filter and the bar on top of the board find it
        # (the suite also runs on a copy of the lab: count only what the test made)
        self.assertIn(card, self.Collection.search([("confirmed_without_receipt", "=", True)]))
        self.assertEqual(self.Collection.distri_board_counters()["without_receipt"]["count"],
                         flagged_before + 1)
        arch = self.env.ref("distri_pedido.view_mp_collection_search").arch_db
        self.assertIn('name="confirmed_without_receipt"', arch)
        # undone: the flag goes with the confirmation
        card.action_undo()
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.confirmed_without_receipt)

    def test_confirm_with_receipt_is_not_flagged(self):
        self._receipt("5491100000000", datetime(2026, 9, 24, 20, 0),
                      caption="Comprobante del pedido. Operación 900000000004"
                      ).distri_partner_id = self.other
        self._import("mp_report_25col.csv")
        card = self._card("900000000004")
        self.assertTrue(card.receipt_id)
        self.assertIs(card.action_confirm_board(), True, "no notice: straight confirmed")
        self.assertEqual(card.state, "confirmed")
        self.assertFalse(card.confirmed_without_receipt)

    def test_receipt_joining_later_clears_the_flag(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        card.action_confirm()
        self.assertTrue(card.confirmed_without_receipt)
        # days later the customer sends the receipt of that transfer to the group
        receipt = self._receipt("5491100000000", datetime(2026, 9, 26, 12, 0),
                                caption="Comprobante $ 45.321 operación 900000000001")
        self.assertEqual(receipt.distri_receipt_state, "registered", "never a second payment")
        self.assertEqual(card.receipt_id, receipt)
        self.assertFalse(card.confirmed_without_receipt)
        self.assertIn("se quita la marca «Confirmado sin comprobante»", card.message_ids[:1].body)
        self.assertEqual(len(self._mp_payments("900000000001")), 1)
        # any other path that joins a receipt clears it too
        other = self._card("900000000006")
        other.action_confirm()
        self.assertTrue(other.confirmed_without_receipt)
        other.sudo().receipt_id = self._receipt("5491100000001", datetime(2026, 9, 26, 13, 0))
        self.assertFalse(other.confirmed_without_receipt)

    def test_late_receipt_of_another_customer_does_not_join(self):
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        card.action_confirm()
        # the photo comes from the phone of another customer
        receipt = self._receipt("5492995000111", datetime(2026, 9, 26, 12, 0),
                                caption="Comprobante $ 45.321 operación 900000000001")
        self.assertEqual(receipt.distri_partner_id, self.photo)
        self.assertEqual(receipt.distri_receipt_state, "registered")
        self.assertFalse(card.receipt_id)
        self.assertTrue(card.confirmed_without_receipt)

    def test_migration_flags_the_past_confirmed_without_receipt(self):
        self._receipt("5491100000000", datetime(2026, 9, 24, 20, 0),
                      caption="Comprobante del pedido. Operación 900000000004"
                      ).distri_partner_id = self.other
        self._import("mp_report_25col.csv")
        without, with_receipt = self._card("900000000001"), self._card("900000000004")
        (without | with_receipt).action_confirm()
        # as before 19.0.1.42.0: no flag on any card
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET confirmed_without_receipt = FALSE "
                            "WHERE id IN %s", [tuple((without | with_receipt).ids)])
        self.Collection.invalidate_model(["confirmed_without_receipt"])
        count, amount = self.Collection._flag_confirmed_without_receipt()
        self.assertEqual((count, amount), (1, 45321))
        self.assertTrue(without.confirmed_without_receipt)
        self.assertFalse(with_receipt.confirmed_without_receipt)
        self.assertEqual(self.Collection._flag_confirmed_without_receipt(), (0, 0), "only once")

    def test_confirm_all_notice_counts_the_ones_without_receipt(self):
        # on a copy of the lab, its own pending cards would join «Confirmar todos»
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET state = 'ignored' WHERE state = 'draft'")
        self.env.invalidate_all()
        self._receipt("5491100000000", datetime(2026, 9, 24, 13, 0),
                      caption="Comprobante operación 900000000006")
        self._import("mp_report_25col.csv")
        with_receipt = self._card("900000000006")
        self.assertTrue(with_receipt.receipt_id)
        self.assertEqual(with_receipt.column, "confirm")
        result = self.Collection.action_confirm_ready_notice()
        notice = self.env["distri.mp.confirm.notice"].browse(result["res_id"])
        self.assertEqual(notice.mode, "ready")
        self.assertEqual((notice.count, notice.missing_count), (2, 1))
        self.assertIn("1 de estos 2 pagos no tiene comprobante en el grupo de WhatsApp",
                      notice.notice)
        self.assertIn("MP 900000000001", notice.missing_names)
        self.assertFalse(self.Collection.search([("state", "=", "confirmed"),
                                                 ("source_id", "like", "9000000000")]),
                         "the preview registers nothing")
        done = notice.action_apply()
        self.assertIn("1 quedó marcado «Confirmado sin comprobante»", done["params"]["message"])
        self.assertTrue(self._card("900000000001").confirmed_without_receipt)
        self.assertEqual(with_receipt.state, "confirmed")
        self.assertFalse(with_receipt.confirmed_without_receipt)
        # the kanban button goes through the preview, never the browser question
        arch = self.env.ref("distri_pedido.view_mp_collection_kanban").arch_db
        self.assertIn('name="action_confirm_ready_notice"', arch)
        self.assertNotIn('name="action_confirm_ready"', arch)

    def test_receipt_candidate_waits_for_its_receipt(self):
        """19.0.1.45.0: a card attached to a customer without its WhatsApp
        receipt is never confirmed (never lowers the balance) until the
        receipt joins it; then the flag clears by itself."""
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        self.assertEqual(card.column, "confirm")
        card.sudo().write({"receipt_candidate": True})
        self.assertEqual(card.column, "review")
        self.assertIn(CANDIDATE_LABEL, card.review_reason)
        # out of «Confirmar todos», the others go on
        ready, _skipped = self.Collection._ready_to_confirm()
        self.assertNotIn(card, ready)
        self.assertIn(self._card("900000000006"), ready)
        # every confirmation path goes through `_confirm_todo`
        for confirm in (card.action_confirm, card.action_confirm_board,
                        (card | self._card("900000000006")).action_confirm_board):
            with self.assertRaises(UserError):
                confirm()
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.payment_id)
        self.assertEqual(self._card("900000000006").state, "draft", "all or nothing")
        # the receipt arrives: every guard lets it go, the mark stays until
        # it is confirmed (the statement still lists it)
        receipt = self._receipt("5491100000000", datetime(2026, 9, 24, 13, 0),
                                caption="Comprobante operación 900000000001")
        card.sudo().write({"receipt_id": receipt.id})
        self.assertTrue(card.receipt_candidate)
        self.assertEqual(card.column, "confirm")
        self.assertFalse(card.review_reason)
        self.assertIn(card, self.Collection._ready_to_confirm()[0])
        self.assertNotIn(card, self.Collection.search(
            safe_eval(self._filter_domain("receipt_candidate"))))
        self.assertIn("ya se puede aprobar", card.message_ids[:1].body)
        card.action_confirm_board()
        self.assertEqual(card.state, "confirmed")
        self.assertFalse(card.receipt_candidate, "cleared when confirmed")
        self.assertFalse(card.confirmed_without_receipt)

    def _filter_domain(self, name):
        arch = etree.fromstring(self.env.ref("distri_pedido.view_mp_collection_search").arch_db)
        return arch.xpath("//filter[@name='%s']" % name)[0].get("domain")

    def test_candidate_in_the_account_statement(self):
        """«Pago a confirmar · falta el comprobante» in the statement: never
        subtracted, the balance if approved, a note, «Aprobar» only with the
        receipt."""
        self._import("mp_report_25col.csv")
        card = self._card("900000000001")
        card.sudo().write({"receipt_candidate": True})
        balance = self.known._distri_account_balance()
        data = self.known._distri_statement()
        self.assertEqual([c["res_id"] for c in data["candidates"]], card.ids)
        self.assertEqual(data["balance"], balance, "not in the balance")
        self.assertFalse([r for r in data["rows"] if r.get("res_model") == card._name])
        self.assertAlmostEqual(data["whatif_balance"], balance - 45321)
        html = str(self.known.distri_statement_html)
        self.assertIn("Pago a confirmar · falta el comprobante", html)
        self.assertIn("Saldo si se aprueban los candidatos", html)
        # the wizard: the totals, the note written from the statement
        action = self.known.action_distri_estado_cuenta()
        wizard = self.env["distri.estado.cuenta"].browse(action["res_id"])
        self.assertEqual(wizard.candidate_ids, card)
        self.assertAlmostEqual(wizard.balance, balance)
        self.assertAlmostEqual(wizard.whatif_balance, balance - 45321)
        wizard.write({"candidate_ids": [Command.update(card.id, {
            "candidate_note": "se le pidió el comprobante el 30/10"})]})
        self.assertEqual(card.candidate_note, "se le pidió el comprobante el 30/10")
        self.assertIn("se le pidió el comprobante el 30/10",
                      str(self.known._distri_statement_table(self.known._distri_statement())))
        arch = self.env.ref("distri_pedido.view_estado_cuenta_form").arch_db
        # 19.0.1.129.13: and only for whom may confirm this card
        self.assertIn('invisible="not receipt_id or state != \'draft\' or not can_confirm_income"',
                      arch)
        # «Aprobar» without the receipt: refused, nothing registered
        with self.assertRaises(UserError):
            card.action_approve_candidate()
        self.assertEqual(card.state, "draft")
        self.assertAlmostEqual(self.known._distri_account_balance(), balance)
        # with the receipt: confirmed the board's way, the balance goes down
        receipt = self._receipt("5491100000000", datetime(2026, 9, 24, 13, 0),
                                caption="Comprobante operación 900000000001")
        card.sudo().write({"receipt_id": receipt.id})
        card.action_approve_candidate()
        self.assertEqual(card.state, "confirmed")
        self.assertTrue(card.payment_id)
        self.assertFalse(card.receipt_candidate)
        self.assertAlmostEqual(self.known._distri_account_balance(), balance - 45321)
        self.assertFalse(self.known._distri_statement()["candidates"])

    # ------------------------------------------------------------ import 20 (06/11/2026)

    # the OCR of a Mercado Pago receipt that lost its header and the label
    # «N.° de operación» (fictitious name and number)
    NO_LABEL = ("Comprobante de\ntransferencia\nComprobante de transferencia\n$ 159.100\n"
                "Carla Ejemplar Gómez\nDistri Neuquén\n%s")

    def _two_transfers_same_amount(self):
        """The receipt's transfer at 18:35 and another one of the same amount
        and payer 8 minutes later."""
        self.Report._import_file("same_amount_8min.csv", self._report(
            self._row("186900000407", "2026-11-05T18:35:28.000-03:00", 159100),
            self._row("186900000737", "2026-11-05T18:43:44.000-03:00", 159100)))
        return self._card("186900000407"), self._card("186900000737")

    def _photo_with_text(self, when, text, operation=False):
        """A receipt whose stored OCR text is `text`, its operation number
        as the parser left it (`operation`)."""
        return self.env["distri.whatsapp.message"].sudo().with_context(
            distri_defer_receipt_read=True).create({
                "wa_message_id": "wamid.test.%s" % when.isoformat(), "direction": "in",
                "wa_id": "5492995000999", "message_type": "image", "timestamp": when,
                "media_mime_type": "image/jpeg", "media_id": "123", "media_state": "done",
                "distri_is_receipt": True, "distri_receipt_state": "pending",
                "distri_amount": 159100, "distri_amount_confidence": "high",
                "distri_file_text": text, "distri_operation": operation})

    def _read_photo(self, when, text):
        """A photo read by the current parser (the file reading patched)."""
        Message = self.env["distri.whatsapp.message"]
        msg = Message.sudo().with_context(distri_defer_receipt_read=True).create({
            "wa_message_id": "wamid.test.%s" % when.isoformat(), "direction": "in",
            "wa_id": "5492995000999", "message_type": "image", "timestamp": when,
            "media_mime_type": "image/jpeg", "media_id": "123", "media_state": "pending"})
        reading = parse_receipt(text, own=own_account(self.env))
        with patch.object(type(Message), "_distri_file_reading",
                          lambda m: (text, reading)):
            msg._distri_read_receipt(read_file=True)
        return msg

    def test_two_photos_of_one_receipt_without_label(self):
        # both photos read now: the number is the operation, the second
        # photo is «Ya recibido», the later transfer keeps waiting
        first = self._read_photo(datetime(2026, 11, 5, 21, 50), self.NO_LABEL % "186900000407")
        second = self._read_photo(datetime(2026, 11, 5, 21, 51), self.NO_LABEL % "186900000407")
        self.assertEqual(first.distri_operation, "186900000407")
        self.assertEqual(second.distri_receipt_state, "duplicate")
        self.assertIn("Ya recibido", second.distri_receipt_note)
        mine, other = self._two_transfers_same_amount()
        self.assertEqual(mine.receipt_id, first)
        self.assertFalse(other.receipt_id, "never the copy by amount")

    def test_number_in_the_text_is_never_another_transfers_receipt(self):
        # stored before the fix: the number is in the text, not in the field
        first = self._photo_with_text(datetime(2026, 11, 5, 21, 50),
                                      self.NO_LABEL % "186900000407")
        second = self._photo_with_text(datetime(2026, 11, 5, 21, 51),
                                       self.NO_LABEL % "186900000407")
        mine, other = self._two_transfers_same_amount()
        self.assertEqual(mine.receipt_id, first, "the first one received, by its number")
        self.assertFalse(other.receipt_id)
        self.assertEqual(second.distri_receipt_state, "duplicate")
        self.assertNotIn(second, other._amount_candidates())

    def test_photo_with_another_transfers_number_after_the_report(self):
        mine, other = self._two_transfers_same_amount()
        photo = self._photo_with_text(datetime(2026, 11, 5, 21, 50),
                                      self.NO_LABEL % "186900000407")
        photo._distri_match_income()
        self.assertEqual(mine.receipt_id, photo)
        self.assertFalse(other.receipt_id)
        # a copy arriving later is «Ya recibido», not the 18:43 transfer's
        copy = self._photo_with_text(datetime(2026, 11, 5, 21, 52),
                                     self.NO_LABEL % "186900000407")
        copy._distri_match_income()
        self.assertFalse(other.receipt_id)
        self.assertEqual(copy.distri_receipt_state, "duplicate")

    def test_recompute_operations_fills_only_open_receipts(self):
        waiting = self._photo_with_text(datetime(2026, 11, 5, 21, 50),
                                        self.NO_LABEL % "186900000407")
        nothing = self._photo_with_text(datetime(2026, 11, 5, 21, 55),
                                        "Comprobante de\ntransferencia\nMercado")
        mine, other = self._two_transfers_same_amount()
        # the photo of the 18:35 transfer tied by amount to a confirmed card
        held = self._photo_with_text(datetime(2026, 11, 5, 21, 51),
                                     self.NO_LABEL % "186900000407")
        other._link_receipt(held)
        other.sudo().write({"state": "confirmed"})
        report = self.env["distri.whatsapp.message"]._distri_recompute_operations(
            [waiting.id, nothing.id, held.id])
        rows = {row["id"]: row for row in report}
        self.assertEqual(set(rows), {waiting.id, held.id})
        self.assertEqual(rows[held.id]["action"], "reported")
        self.assertEqual(rows[held.id]["owner"], mine.id)
        self.assertFalse(held.distri_operation, "a confirmed card is never changed")
        self.assertEqual(other.receipt_id, held)
        self.assertEqual(waiting.distri_operation, "186900000407")
        self.assertEqual(mine.receipt_id, waiting)
