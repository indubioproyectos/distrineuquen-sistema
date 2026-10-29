"""19.0.1.38.0: catch-up of the customer accounts in the lab first
(models/puesta_al_dia.py). Every customer, CUIT, amount and operation here is
fictitious."""
import io
import random
import zipfile
from datetime import date, datetime

import openpyxl

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.cobros import read_report
from odoo.addons.distri_pedido.models.puesta_al_dia import EXPORT_HEADERS, parse_chat
from odoo.addons.distri_pedido.tests import receipt_images
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

TECH = ["SOURCE_ID", "TRANSACTION_TYPE", "TRANSACTION_AMOUNT", "TRANSACTION_DATE",
        "PAYMENT_METHOD_TYPE", "PAYMENT_METHOD", "PAYER_NAME", "PAYER_ID_TYPE", "PAYER_ID_NUMBER"]
SPANISH = ["FECHA DE ORIGEN", "ID DE OPERACIÓN EN MERCADO PAGO", "TIPO DE MEDIO DE PAGO",
           "TIPO DE OPERACIÓN", "VALOR DE LA COMPRA", "PAGADOR",
           "TIPO DE IDENTIFICACIÓN DEL PAGADOR", "NÚMERO DE IDENTIFICACIÓN DEL PAGADOR"]


def _xlsx(header, rows):
    book = openpyxl.Workbook()
    sheet = book.active
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    out = io.BytesIO()
    book.save(out)
    return out.getvalue()


def _tech(source, when, amount, cuit="", kind="SETTLEMENT", method="bank_transfer"):
    return [source, kind, amount, when, method, method, "Titular Prueba" if cuit else "",
            "CUIT" if cuit else "", cuit]


@tagged("post_install", "-at_install", "distri_puesta_al_dia")
class TestPuestaAlDia(RepartoMixin, AccountTestInvoicingCommon):

    # the export wizard on the test's day only: the lab copy may hold other cards
    PERIOD = {"date_from": date(2026, 10, 1), "date_to": date(2026, 10, 1)}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        Partner = cls.env["res.partner"]
        cls.alpha = Partner.create({"name": "Almacén Prueba Alfa", "company_type": "company",
                                    "ref": "ODOO-990001"})
        cls.beta = Partner.create({"name": "Kiosco Prueba Beta", "company_type": "company",
                                   "ref": "ODOO-990002"})
        cls.nobody = Partner.create({"name": "Despensa Sin Online", "company_type": "company"})
        cls.env["distri.payer.identity"].create({"kind": "cuit", "value": "20999000011",
                                                 "partner_id": cls.alpha.id})
        cls.Report = cls.env["distri.mp.report"]
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Online = cls.env["distri.online.payment"]
        cls.seller = new_test_user(cls.env, login="vendedor_puesta",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")

    def _import(self, rows, **kw):
        return self.Report._import_file("todas.xlsx", _xlsx(TECH, rows), **kw)

    def _card(self, source):
        return self.Collection.search([("source_id", "=", source)])

    def _online(self, online_id, partner_oid, amount, when, memo="", state="paid", journal=13):
        return {"online_id": online_id, "name": "PBNK1/2026/%05d" % online_id, "date": when,
                "amount": amount, "journal_id": journal, "journal_name": "Banco",
                "memo": memo, "state": state, "partner_id": partner_oid,
                "move_name": "PBNK1/2026/%05d" % online_id}

    # ------------------------------------------------------------ report

    def test_xlsx_report_spanish_and_technical(self):
        spanish = _xlsx(SPANISH, [
            ["2026-10-01T10:00:00.000-03:00", 900000000101, "Transferencia bancaria",
             "Pago aprobado", 15000.0, "Titular Prueba", "CUIT", "20999000011"],
            ["2026-10-01T11:00:00.000-03:00", "1234567890123", "", "Pago aprobado", 12.5,
             "", "", ""]])
        rows = read_report(spanish)
        self.assertEqual(rows[0]["SOURCE_ID"], "900000000101")
        self.assertEqual(rows[0]["TRANSACTION_TYPE"], "SETTLEMENT")
        report = self.Report._import_file("settlement.xlsx", spanish)
        self.assertEqual((report.new_count, report.skipped_count), (1, 1))
        card = self._card("900000000101")
        self.assertEqual((card.amount, card.partner_id), (15000.0, self.alpha))
        # the same operation again: idempotent; a date range filters the rows
        report = self._import([_tech("900000000101", "2026-10-01T10:00:00.000-03:00", 15000)])
        self.assertEqual((report.new_count, report.duplicate_count), (0, 1))
        report = self._import([_tech("900000000102", "2026-09-10T10:00:00.000-03:00", 100),
                               _tech("900000000103", "2026-10-02T10:00:00.000-03:00", 200)],
                              date_from=date(2026, 10, 1))
        self.assertEqual(report.new_count, 1)
        self.assertFalse(self._card("900000000102"))
        with self.assertRaises(UserError):
            read_report(_xlsx(["A", "B"], [[1, 2]]))

    # ------------------------------------------------------------ Online

    def test_already_in_online_never_exported(self):
        start = self.Collection.distri_board_counters()
        self.Online._distri_load([
            self._online(1, 990002, 5000, "2026-10-03"),
            # the payer's CUIT is Alfa's: never paired with Beta's payment
            self._online(2, 990002, 7000, "2026-10-03"),
            self._online(3, 990001, 9000, "2026-10-20"),   # too late: 19 days
        ])
        self._import([_tech("900000000201", "2026-10-01T10:00:00.000-03:00", 5000),
                      _tech("900000000202", "2026-10-01T11:00:00.000-03:00", 7000,
                            cuit="20999000011"),
                      _tech("900000000203", "2026-10-01T12:00:00.000-03:00", 9000)])
        first, second, third = (self._card(s) for s in ("900000000201", "900000000202",
                                                        "900000000203"))
        self.assertEqual(first.online_status, "already")
        self.assertEqual((first.state, first.ignore_reason), ("ignored", "online"))
        self.assertEqual(first.partner_id, self.beta)
        self.assertEqual(second.online_status, "none")
        self.assertEqual(third.online_status, "none")
        counters = self.Collection.distri_board_counters()
        self.assertEqual(counters["online"]["count"] - start["online"]["count"], 1)
        self.assertEqual(counters["review"]["count"] - start["review"]["count"], 2)
        # a wrong link is undone and never made again by amount
        first.action_online_unlink()
        self.assertEqual((first.state, first.online_status), ("draft", "none"))
        self.Online._distri_link_all()
        self.assertEqual(first.online_status, "none")
        # confirmed in the lab and found in Online afterwards: flagged, not exported
        second.write({"partner_id": self.alpha.id})
        second.action_confirm()
        self.Online._distri_load([self._online(4, 990001, 7000, "2026-10-02")])
        self.assertTrue(second.online_conflict)
        wizard = self.env["distri.online.export.wizard"].create(dict(self.PERIOD, allow_pending=True))
        self.assertEqual(wizard.ready_count, 0)
        self.assertIn("doble carga", wizard.check_text)

    def test_export_and_close_the_loop(self):
        # on a copy of the lab, its own pending cards of the period would count
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET state = 'ignored' WHERE state = 'draft'")
        self.env.invalidate_all()
        invoice =self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.alpha.id, "invoice_date": "2026-09-30",
            "invoice_line_ids": [Command.create({"name": "mercadería", "quantity": 1,
                                                 "price_unit": 8000,
                                                 "tax_ids": [Command.clear()]})]})
        invoice.action_post()
        self._import([_tech("900000000301", "2026-10-01T10:00:00.000-03:00", 8000,
                            cuit="20999000011"),
                      _tech("900000000302", "2026-10-01T11:00:00.000-03:00", 3000)])
        ready, other = self._card("900000000301"), self._card("900000000302")
        self.assertEqual(ready.column, "confirm")
        before = self.alpha._distri_account_balance()
        ready.action_confirm()
        self.assertEqual(ready.state, "confirmed")
        self.assertAlmostEqual(self.alpha._distri_account_balance(), before - 8000)
        # pending reviews block until the box is ticked
        wizard = self.env["distri.online.export.wizard"].create(self.PERIOD)
        self.assertEqual((wizard.ready_count, wizard.pending_count), (1, 1))
        with self.assertRaises(UserError):
            wizard.action_export()
        # a customer without an Online id is never exported
        other.write({"partner_id": self.nobody.id, "proposal": "manual"})
        other.action_confirm()
        wizard = self.env["distri.online.export.wizard"].create(self.PERIOD)
        self.assertEqual((wizard.ready_count, wizard.blocked_count, wizard.pending_count),
                         (1, 1, 0))
        self.assertIn("no tiene id de Online", wizard.check_text)
        action = wizard.action_export()
        batch = wizard.batch_id
        self.assertEqual(batch.collection_ids, ready)
        self.assertIn(str(batch.attachment_id.id), action["url"])
        sheet = openpyxl.load_workbook(io.BytesIO(batch.attachment_id.raw)).worksheets[0]
        rows = list(sheet.iter_rows(values_only=True))
        self.assertEqual(list(rows[0]), EXPORT_HEADERS)
        row = rows[1]
        self.assertEqual(row[0], "distri_cobro_mp_900000000301")
        self.assertEqual((row[1], row[3], row[4], row[5], row[6]),
                         (990001, 8000, 13, "Recibir", "Cliente"))
        self.assertEqual(row[2].date(), date(2026, 10, 1))
        self.assertEqual(sheet["C2"].number_format, "dd/mm/yyyy")
        self.assertTrue(row[7].startswith("MP 900000000301"))
        self.assertEqual(ready.online_status, "exported")
        # never twice
        wizard = self.env["distri.online.export.wizard"].create(self.PERIOD)
        self.assertEqual(wizard.ready_count, 0)
        with self.assertRaises(UserError):
            wizard.action_export()
        self.assertTrue(batch.action_download()["url"])

        # administración imports it in Online (draft), the sync finds it by memo
        self.Online._distri_load([self._online(10, 990001, 8000, "2026-10-01",
                                               memo=row[7], state="draft")])
        self.assertEqual(ready.online_status, "imported")
        # confirmed in Online: its history row arrives and is archived (same money)
        history = self.env["distri.cuenta.movimiento"].create({
            "partner_id": self.alpha.id, "fecha": "2026-10-01", "comprobante": "PBNK1/2026/00010",
            "tipo": "pago", "debe": 0, "haber": 8000, "origen_id": 777001})
        self.Online._distri_load([self._online(10, 990001, 8000, "2026-10-01", memo=row[7])])
        self.assertEqual(ready.online_status, "loaded")
        self.assertFalse(history.active)
        self.assertEqual(history.lab_payment_id, ready.payment_id)
        self.assertAlmostEqual(self.alpha._distri_account_balance(), before - 8000)

        # balances: Online has the invoice only in the lab here, so the
        # difference is listed per customer
        action = self.env["distri.online.balance"].action_compare()
        lines = self.env["distri.online.balance"].search(action["domain"])
        self.assertIn(self.alpha, lines.partner_id)

    def test_only_administracion(self):
        for model in ("distri.online.export.wizard", "distri.whatsapp.chat.import.wizard"):
            with self.assertRaises(AccessError):
                self.env[model].with_user(self.seller).create({})
        with self.assertRaises(AccessError):
            self.env["distri.online.balance"].with_user(self.seller).action_compare()
        with self.assertRaises(AccessError):
            self.Online.with_user(self.seller).search([])

    # ------------------------------------------------------------ WhatsApp chat

    def test_parse_chat_formats(self):
        text = ("1/10/2026, 10:05 - Vendedor Prueba: ‎IMG-20261001-WA0001.jpg (archivo adjunto)\n"
                "1/10/2026, 10:05 - Vendedor Prueba: ALMACEN\nsegunda línea\n"
                "[2/10/26, 9:01:02] Otra Persona: <adjunto: 00000012-PHOTO.jpg>\n"
                "3/10/2026, 11:00 - Vendedor Prueba: ‎Comprobante de transferencia-90.pdf (archivo adjunto)\n")
        rows = parse_chat(text)
        # bank apps name their PDFs with spaces
        self.assertEqual(rows[3][2:], ("", "Comprobante de transferencia-90.pdf"))
        self.assertEqual(rows[0], (datetime(2026, 10, 1, 10, 5), "Vendedor Prueba", "",
                                   "IMG-20261001-WA0001.jpg"))
        self.assertEqual(rows[1][2], "ALMACEN\nsegunda línea")
        self.assertEqual(rows[2][3], "00000012-PHOTO.jpg")
        self.assertEqual(rows[2][0], datetime(2026, 10, 2, 9, 1, 2))

    def _chat_zip(self):
        data = receipt_images.random_data(random.Random(7))
        data.update(amount=12345.0, operation="900000000777",
                    date=datetime(2026, 10, 1, 9, 58))
        png, _expected = receipt_images.render("Mercado Pago", data)
        chat = (
            "5/9/2026, 08:00 - Vendedor Prueba: ‎IMG-20260905-WA0001.jpg (archivo adjunto)\n"
            "1/10/2026, 10:05 - Vendedor Prueba: ‎IMG-20261001-WA0002.jpg (archivo adjunto)\n"
            "1/10/2026, 10:05 - Vendedor Prueba: Kiosco Prueba Beta\n"
            "1/10/2026, 10:05 - Vendedor Prueba: operación 900000000777\n"
            "1/10/2026, 10:06 - Vendedor Prueba: ‎STK-20261001-WA0003.webp (archivo adjunto)\n")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as zf:
            zf.writestr("Chat de WhatsApp con Grupo Prueba.txt", chat)
            zf.writestr("IMG-20260905-WA0001.jpg", png)
            zf.writestr("IMG-20261001-WA0002.jpg", png)
            zf.writestr("STK-20261001-WA0003.webp", b"RIFF")
        return out.getvalue()

    def test_chat_import_links_and_is_idempotent(self):
        self._import([_tech("900000000777", "2026-10-01T09:58:00.000-03:00", 12345)])
        card = self._card("900000000777")
        self.assertEqual(card.column, "unidentified")
        Import = self.env["distri.whatsapp.chat.import"]
        data = self._chat_zip()
        first = Import._distri_import("chat.zip", date(2026, 10, 1), date(2026, 10, 1),
                                      data=data)
        self.assertEqual((first.file_count, first.skipped_count, first.duplicate_count),
                         (1, 1, 0))
        receipt = first.message_ids.filtered(lambda m: m.message_type == "image")
        self.assertTrue(receipt.attachment_id)
        self.assertEqual(receipt.distri_chat_sender, "Vendedor Prueba")
        self.assertFalse(any(c.isdigit() for c in receipt.wa_id))
        self.assertEqual(card.receipt_id, receipt)
        self.assertEqual(card.partner_id, self.beta)
        self.assertEqual(card.column, "review")
        self.assertEqual(first.matched_count, 1)
        again = Import._distri_import("chat.zip", date(2026, 9, 1), date(2026, 10, 1),
                                      data=data)
        # the September copy is the same file: one message per file hash
        self.assertEqual((again.file_count, again.duplicate_count), (0, 2))
        self.assertEqual(self.env["distri.whatsapp.message"].search_count(
            [("message_type", "=", "image"),
             ("distri_chat_import_id", "in", (first | again).ids)]), 1)
        with self.assertRaises(UserError):
            Import._distri_import("chat.zip", date(2026, 10, 2), date(2026, 10, 1), data=data)

    def test_board_kanban_expressions_are_javascript(self):
        """The card template of the board is compiled by Owl in the browser:
        «not» is not an operator there (it broke the whole board)."""
        import re
        arch = self.env.ref("distri_pedido.view_mp_collection_kanban").arch
        for expr in re.findall(r't-(?:if|elif|att-\w+)="([^"]*)"', arch):
            self.assertNotRegex(expr, r"\bnot\b", expr)

