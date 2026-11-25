"""19.0.1.129.26 (#111): the WhatsApp chat import of the backend form saves
the files and answers at once; the cron reads them (OCR) out of the web
request, with the same result as reading them inline. OCR is mocked."""
import base64
import io
import zipfile
from datetime import date
from unittest.mock import patch

from odoo import Command
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models import cobros, ocr_comprobante, puesta_al_dia
from odoo.addons.distri_pedido.tests.test_puesta_al_dia import TECH, _tech, _xlsx
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

OCR_TEXT = ("Mercado Pago\nComprobante de transferencia\n1 de octubre de 2026 a las 09:58 hs\n"
            "$ 12.345\nNúmero de operación de Mercado Pago 900000000777")
DAY = date(2026, 10, 1)


@tagged("post_install", "-at_install", "distri_v129_26")
class TestChatOcrCron(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls.beta = cls.env["res.partner"].create({
            "name": "Kiosco Prueba Beta", "company_type": "company", "ref": "ODOO-990002"})
        cls.Import = cls.env["distri.whatsapp.chat.import"]

    def setUp(self):
        super().setUp()
        self.ocr_calls = []

        def fake_ocr(data, lang="spa", change=None, psm=3):
            self.ocr_calls.append(lang)
            return OCR_TEXT, 92.0

        for module in (ocr_comprobante, cobros):
            patcher = patch.object(module, "ocr_image", fake_ocr)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.env["distri.mp.report"]._import_file(
            "todas.xlsx", _xlsx(TECH, [_tech("900000000777", "2026-10-01T09:58:00.000-03:00",
                                             12345)]))

    def _zip(self):
        chat = (
            "1/10/2026, 10:05 - Vendedor Prueba: ‎IMG-20261001-WA0002.jpg (archivo adjunto)\n"
            "1/10/2026, 10:05 - Vendedor Prueba: Kiosco Prueba Beta\n"
            "1/10/2026, 10:06 - Vendedor Prueba: ‎STK-20261001-WA0003.webp (archivo adjunto)\n")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("Chat de WhatsApp con Grupo Prueba.txt", chat)
            zf.writestr("IMG-20261001-WA0002.jpg", b"\xff\xd8 not a real photo, OCR is mocked")
            zf.writestr("STK-20261001-WA0003.webp", b"RIFF")
        return out.getvalue()

    def _card(self):
        return self.env["distri.mp.collection"].search([("source_id", "=", "900000000777")])

    def _result_of(self, record):
        photo = record.message_ids.filtered(lambda m: m.message_type == "image")
        card = self._card()
        return {"media_state": photo.media_state, "partner": photo.distri_partner_id,
                "amount": photo.distri_amount, "operation": photo.distri_operation,
                "receipt_state": photo.distri_receipt_state, "card_receipt": card.receipt_id == photo,
                "card_partner": card.partner_id, "card_column": card.column,
                "counts": (record.file_count, record.skipped_count, record.duplicate_count,
                           record.receipt_count, record.matched_count),
                "summary": record.summary}

    def test_form_answers_at_once_and_the_cron_reads(self):
        wizard = self.env["distri.whatsapp.chat.import.wizard"].create({
            "file": base64.b64encode(self._zip()), "filename": "chat.zip",
            "date_from": DAY, "date_to": DAY, "owner": "own"})
        wizard.action_import()
        record = wizard.import_id
        self.assertEqual(self.ocr_calls, [], "no OCR inside the request")
        self.assertEqual(record.distri_read_state, "queued")
        self.assertIn("segundo plano", wizard.summary)
        photo = record.message_ids.filtered(lambda m: m.message_type == "image")
        self.assertEqual(photo.media_state, "pending")
        self.assertTrue(photo.attachment_id)
        self.assertEqual(record.distri_read_queue_ids, photo)
        self.assertFalse(self._card().receipt_id)
        cron = self.env.ref("distri_pedido.cron_chat_import_read")
        self.assertTrue(self.env["ir.cron.trigger"].search([("cron_id", "=", cron.id)]))

        self.Import._cron_read_queued()

        self.assertTrue(self.ocr_calls)
        self.assertEqual(record.distri_read_state, "done")
        self.assertFalse(record.distri_read_queue_ids)
        self.assertEqual(photo.media_state, "done")
        self.assertEqual(self._card().receipt_id, photo)
        self.assertEqual(record.summary, record._summary_text())
        self.assertNotIn("segundo plano", record.summary)

    def test_same_result_as_reading_inline(self):
        savepoint = self.env.cr.savepoint()
        inline = self._result_of(self.Import._distri_import("chat.zip", DAY, DAY, data=self._zip()))
        savepoint.close(rollback=True)
        self.env.invalidate_all()
        self.assertFalse(self._card().receipt_id)

        record = self.Import._distri_import("chat.zip", DAY, DAY, data=self._zip(),
                                            background=True)
        self.Import._cron_read_queued()
        queued = self._result_of(record)
        self.assertEqual(queued, inline)
        self.assertEqual(inline["media_state"], "done")
        self.assertTrue(inline["card_receipt"])

    def test_a_failing_file_does_not_block_the_queue(self):
        record = self.Import._distri_import("chat.zip", DAY, DAY, data=self._zip(),
                                            background=True)
        Message = type(self.env["distri.whatsapp.message"])
        with patch.object(Message, "_distri_read_receipt", side_effect=RuntimeError("boom")), \
                self.assertLogs(puesta_al_dia._logger.name, level="ERROR"):
            self.Import._cron_read_queued()
        photo = record.message_ids.filtered(lambda m: m.message_type == "image")
        self.assertEqual(photo.media_state, "error")
        self.assertEqual(record.distri_read_state, "done")

    def test_server_script_path_still_reads_inline(self):
        record = self.Import._distri_import("chat.zip", DAY, DAY, data=self._zip(), commit_every=0)
        self.assertEqual(record.distri_read_state, "done")
        self.assertTrue(self.ocr_calls)
        self.assertEqual(self._card().receipt_id.distri_chat_import_id, record)

    def test_no_queued_import_is_a_no_op(self):
        self.Import._cron_read_queued()
        self.assertEqual(self.ocr_calls, [])
