"""Receipt reading (models/ocr_comprobante.py) on synthetic receipts.

The images are drawn by tests/receipt_images.py from made-up data: no real
customer receipt is used.
"""
import base64
import io
import random
import unittest
import zlib
from datetime import datetime
from unittest.mock import patch

from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models import ocr_comprobante
from odoo.addons.distri_pedido.models.ocr_comprobante import (
    CONFIDENCE_HIGH, CONFIDENCE_LOW, cuit_is_valid, differs_only_by_3_and_8,
    local_to_utc, ocr_available, ocr_image, own_account_values, parse_receipt, pdf_images,
    read_image,
)
from odoo.addons.distri_pedido.models.reparto_cliente import AR_TZ
from odoo.addons.distri_pedido.tests import receipt_images

needs_tesseract = unittest.skipUnless(ocr_available(), "tesseract is not installed")


# fictitious own account (the real one is a lab parameter, never in the code)
TEST_OWN_ACCOUNT = ("23-99999999-9, Marcos Daniel Ejemplar, Distri Neuquén, "
                    "0000003100090000000018, alias.ejemplo")


@tagged("post_install", "-at_install", "distri_ocr")
class TestParseReceipt(TransactionCase):
    """Parsing of receipt text: no OCR involved."""

    def test_labeled_amount_and_parties(self):
        text = """Galicia
Comprobante de transferencia
Fecha 25/09/2026
Hora 11:57
Importe $ 150.000,50
Ordenante Valeria Gómez
CUIT ordenante 20-11111111-2
Cuenta origen CA $ 4012-3 123-4
Beneficiario Martín Sánchez
CUIT/CUIL 23-99999999-9
CBU destino 0000003100012345678905
Número de operación 362057025792"""
        result = parse_receipt(text)
        self.assertEqual(result["amount"], 150000.5)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
        self.assertEqual(result["operation"], "362057025792")
        self.assertEqual(result["date"], datetime(2026, 9, 25, 11, 57))
        self.assertEqual(result["payer_name"], "Valeria Gómez")
        self.assertEqual(result["payer_cuit"], "20111111112")
        self.assertEqual(result["dest_name"], "Martín Sánchez")
        self.assertEqual(result["dest_cuit"], "23999999999")
        self.assertEqual(result["destination"], "0000003100012345678905")
        self.assertEqual(result["bank"], "Banco Galicia")

    def test_two_different_amounts_ask_for_review(self):
        result = parse_receipt("Transferencia\n$ 12.000\nOtro $ 15.000\nOperación 123456789")
        self.assertEqual(result["amount_confidence"], CONFIDENCE_LOW)

    def test_balances_and_fees_are_not_the_payment(self):
        result = parse_receipt("Enviaste\n$ 8.500\nComisión $ 0\nSaldo disponible $ 91.200,33")
        self.assertEqual(result["amount"], 8500)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)

    def test_lost_decimal_comma_is_doubtful(self):
        result = parse_receipt("Monto $ 734.269 45")
        self.assertEqual(result["amount"], 734269.45)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_LOW)

    def test_thousands_dot_read_as_comma(self):
        # Banco Nación receipts: the OCR turns «$1.000.000,00» into «$1.000,000,00»
        self.assertEqual(parse_receipt("Monto\n$1.000,000,00")["amount"], 1000000)
        self.assertEqual(parse_receipt("Monto\n$200,000,00")["amount"], 200000)
        self.assertEqual(parse_receipt("Monto $ 12.500,50")["amount"], 12500.5)

    def test_thousands_comma_at_the_end_of_the_number(self):
        # cents are two digits: a comma followed by three digits is thousands
        for text, amount in (("Importe\n$1,500", 1500), ("Monto $ 45,000", 45000),
                             ("$ 1,500,000", 1500000), ("Total $ 200,000", 200000)):
            with self.subTest(text=text):
                result = parse_receipt(text)
                self.assertEqual(result["amount"], amount)
                self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
        # the two-digit cents stay cents
        self.assertEqual(parse_receipt("Monto $1.000,50")["amount"], 1000.5)
        self.assertEqual(parse_receipt("Monto $ 45,00")["amount"], 45)

    def test_trailing_dash_does_not_cut_the_amount(self):
        for text in ("$12.500-", "Importe 12.500-", "Monto $ 12.500 -"):
            with self.subTest(text=text):
                result = parse_receipt(text)
                self.assertEqual(result["amount"], 12500)
                self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)

    def test_the_last_separator_is_the_decimal_mark(self):
        # both separators: the last one holds the cents
        for text, amount in (("Monto $ 1,500.50", 1500.5), ("-$20,000.00", 20000),
                             ("Monto $ 1.234,56", 1234.56), ("$ 1,234,567.89", 1234567.89),
                             ("Monto $ 1.500.50", 1500.5)):
            with self.subTest(text=text):
                result = parse_receipt(text)
                self.assertEqual(result["amount"], amount)
                self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)

    def test_decimal_dot_after_a_plain_number(self):
        # Banco Macro writes «Importe: 250000.00»
        result = parse_receipt("Importe: 250000.00")
        self.assertEqual(result["amount"], 250000)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
        # «$ 27.50» may be a blurred «$ 27.500»: read, but never sure
        result = parse_receipt("Transferencia\n$ 27.50")
        self.assertEqual(result["amount"], 27.5)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_LOW)
        # thousands have three digits: never a decimal dot
        self.assertEqual(parse_receipt("Monto\n$560.000")["amount"], 560000)
        self.assertEqual(parse_receipt("Monto\n$83.50000")["amount"], 83500)

    def test_a_cut_number_is_not_a_sure_amount(self):
        # «$ 1156.250» (a blurred «1.156.250»): never 1156 for sure
        result = parse_receipt("Transferencia\n$ 1156.250")
        self.assertNotEqual(result["amount_confidence"], CONFIDENCE_HIGH)
        self.assertNotEqual(result["amount"], 1156)

    def test_operation_label_does_not_take_a_date_or_a_dni(self):
        self.assertEqual(parse_receipt("Transacción\n2026-09-26 14:30:00")["operation"], "")
        self.assertEqual(parse_receipt("Transacción\n25/09/2026 11:57")["operation"], "")
        self.assertEqual(parse_receipt("Referencia\nDNI 12345678")["operation"], "")
        self.assertEqual(parse_receipt("Referencia CUIT 20-11111111-2")["operation"], "")
        # the real number after them is still found
        self.assertEqual(parse_receipt(
            "Transacción\n2026-09-26 14:30:00\n123456789012")["operation"], "123456789012")
        self.assertEqual(parse_receipt(
            "Referencia\nDNI 12345678\nNúmero de operación 362057025792")["operation"],
            "362057025792")

    def test_spaced_out_digits_of_a_pdf(self):
        # Supervielle PDFs put a space between the digits of the amount
        result = parse_receipt("Comprobante de transferencia\nImporte\n$  5 6 0 . 0 0 00 0")
        self.assertEqual(result["amount"], 560000)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)

    def test_lost_cents_comma_of_a_pdf(self):
        # Mercado Pago PDFs write «$83.500,00» as «$83.50000»
        self.assertEqual(parse_receipt("Monto\n$83.50000")["amount"], 83500)
        self.assertEqual(parse_receipt("Monto\n$100.00000")["amount"], 100000)
        self.assertEqual(parse_receipt("Transferencia\n$ 65.80000")["amount"], 65800)

    def test_normal_amounts_are_not_touched(self):
        self.assertEqual(parse_receipt("Monto\n$ 83.500,00")["amount"], 83500)
        self.assertEqual(parse_receipt("Monto\n$560.000")["amount"], 560000)
        self.assertEqual(parse_receipt("Monto\n$ 1.234,56")["amount"], 1234.56)
        self.assertEqual(parse_receipt("Monto\n$ 1.234.567")["amount"], 1234567)

    def test_blurry_photo_is_never_sure(self):
        result = parse_receipt("Monto $ 5.000", ocr_confidence=30)
        self.assertEqual(result["amount"], 5000)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_LOW)

    def test_dollar_read_as_s(self):
        result = parse_receipt("Comprobante de transferencia\nS 55.864\nMotivo: Varios")
        self.assertEqual(result["amount"], 55864)

    def test_alias_and_cuit_check_digit(self):
        result = parse_receipt("Para: Juan Pérez\nCUIT 20-11111111-2\nAlias: distri. neuquen.mp")
        self.assertEqual(result["destination"], "distri.neuquen.mp")
        self.assertTrue(cuit_is_valid("20-11111111-2"))
        self.assertFalse(cuit_is_valid("20-35040349-5"))


@tagged("post_install", "-at_install", "distri_ocr")
class TestOcrLayouts(TransactionCase):
    """Every supported layout, drawn clean and as a phone JPEG."""

    @needs_tesseract
    def test_every_layout(self):
        rng = random.Random(27)
        for bank in receipt_images.LAYOUTS:
            for level in (0, 1):
                with self.subTest(bank=bank, level=level):
                    png, expected = receipt_images.render(bank, receipt_images.random_data(rng))
                    text, confidence = ocr_image(receipt_images.degrade(png, rng, level))
                    result = parse_receipt(text, confidence)
                    self.assertAlmostEqual(result["amount"], expected["amount"], places=2)
                    self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
                    self.assertEqual(result["operation"], expected["operation"])
                    self.assertEqual(result["payer_cuit"], expected["payer_cuit"])
                    self.assertEqual(result["destination"], expected["destination"])
                    self.assertEqual(result["date"].replace(second=0), expected["date"])


def scanned_pdf(png, flate=False):
    """A PDF that is only a picture of the receipt, as a scanner or a bank app
    makes it. flate=True stores raw pixels with an array colour space (like
    the ICCBased PDFs of Banco Provincia del Neuquén) instead of a JPEG."""
    from PIL import Image
    image = Image.open(io.BytesIO(png)).convert("RGB")
    if not flate:
        buffer = io.BytesIO()
        image.save(buffer, "PDF")
        return buffer.getvalue()
    width, height = image.size
    pixels = zlib.compress(image.tobytes())
    content = b"q %d 0 0 %d 0 0 cm /X4 Do Q" % (width, height)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
        b"/Resources << /XObject << /X4 4 0 R >> >> /Contents 5 0 R >>" % (width, height),
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d "
        b"/ColorSpace [/CalRGB << /WhitePoint [0.9505 1 1.089] >>] /BitsPerComponent 8 "
        b"/Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (width, height, len(pixels), pixels),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


@tagged("post_install", "-at_install", "distri_ocr")
class TestReceiptFromWhatsapp(TransactionCase):
    """A WhatsApp photo fills the receipt; the administrator has the last word."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)
        cls.admin_user = new_test_user(
            cls.env, login="admin_ocr", name="Admin OCR",
            groups="base.group_user,distri_pedido.grupo_administracion")
        cls.data = receipt_images.random_data(random.Random(3))
        cls.data["amount"] = 45300.0
        cls.png, cls.expected = receipt_images.render("Mercado Pago", cls.data)
        cls.customer = cls.env["res.partner"].create({
            "name": "KIOSCO OCR", "vat": cls.data["payer_cuit"]})

    def _photo(self, caption="", png=None, number="5492990000001", mimetype="image/png"):
        msg = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.ocr.%s.%s" % (number, len(caption)),
            "direction": "in",
            "wa_id": number,
            "message_type": "document" if mimetype == "application/pdf" else "image",
            "body": caption,
            "timestamp": datetime(2026, 9, 25, 15, 0),
            "media_mime_type": mimetype,
            "media_id": "456",
            "media_state": "pending",
        })
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "comprobante.pdf" if mimetype == "application/pdf" else "comprobante.png",
            "raw": png or self.png, "mimetype": mimetype,
            "res_model": msg._name, "res_id": msg.id})
        msg.write({"attachment_id": attachment.id, "media_state": "done"})
        return msg

    def test_pictures_inside_a_scanned_pdf(self):
        for flate in (False, True):
            pngs = pdf_images(scanned_pdf(self.png, flate=flate))
            self.assertEqual(len(pngs), 1, "flate=%s" % flate)
            self.assertTrue(pngs[0].startswith(b"\x89PNG"))
        self.assertEqual(pdf_images(b"not a pdf"), [])

    @needs_tesseract
    def test_scanned_pdf_fills_the_receipt(self):
        for flate in (False, True):
            msg = self._photo(png=scanned_pdf(self.png, flate=flate), mimetype="application/pdf",
                              number="54929900000%02d" % (10 + flate))
            self.assertTrue(msg.distri_is_receipt)
            self.assertEqual(msg.distri_amount, 45300, "flate=%s" % flate)
            self.assertEqual(msg.distri_operation, self.data["operation"])
            self.assertEqual(msg.distri_payer_cuit, self.data["payer_cuit"])

    @needs_tesseract
    def test_photo_fills_the_receipt(self):
        msg = self._photo()
        self.assertTrue(msg.distri_is_receipt)
        self.assertEqual(msg.distri_amount, 45300)
        self.assertEqual(msg.distri_amount_confidence, CONFIDENCE_HIGH)
        self.assertFalse(msg.distri_amount_review)
        self.assertEqual(msg.distri_operation, self.data["operation"])
        self.assertEqual(msg.distri_payer_cuit, self.data["payer_cuit"])
        self.assertEqual(msg.distri_destination, self.data["dest_cvu"])
        self.assertEqual(msg.distri_bank, "Mercado Pago")
        # the receipt's local time, stored in UTC (Argentina is UTC-3)
        self.assertEqual(msg.distri_receipt_date,
                         local_to_utc(self.data["date"], AR_TZ))
        # the payer's CUIT identifies the customer
        self.assertEqual(msg.distri_partner_id, self.customer)
        self.assertIn("Comprobante de transferencia", msg.distri_file_text)

    @needs_tesseract
    def test_caption_that_disagrees_asks_for_review(self):
        msg = self._photo(caption="Te pasé $ 45.000")
        self.assertEqual(msg.distri_amount, 45000, "what the customer wrote stays")
        self.assertEqual(msg.distri_read_amount, 45300)
        self.assertEqual(msg.distri_amount_confidence, CONFIDENCE_LOW)
        self.assertTrue(msg.distri_amount_review)
        msg.with_user(self.admin_user).action_distri_use_read_amount()
        self.assertEqual(msg.distri_amount, 45300)
        self.assertEqual(msg.distri_amount_confidence, "manual")
        self.assertFalse(msg.distri_amount_review)

    @needs_tesseract
    def test_typed_amount_always_wins(self):
        msg = self._photo()
        msg.with_user(self.admin_user).write({"distri_amount": 45310})
        self.assertEqual(msg.distri_amount_confidence, "manual")
        msg.action_distri_read_again()
        self.assertEqual(msg.distri_amount, 45310)
        self.assertEqual(msg.distri_amount_confidence, "manual")

    def test_unreadable_photo_needs_the_amount(self):
        msg = self._photo(caption="comprobante", png=b"\x89PNG\r\n\x1a\nnot really")
        self.assertTrue(msg.distri_is_receipt)
        self.assertFalse(msg.distri_amount)
        self.assertEqual(msg.distri_amount_confidence, "none")
        self.assertTrue(msg.distri_amount_review)


def masked_pdf(png):
    """A PDF like Ualá's «Comprobante de transferencia-N.pdf»: the picture is
    all black and the receipt lives in the alpha channel (/SMask), with the
    page resources as an indirect object."""
    from PIL import Image, ImageOps
    image = Image.open(io.BytesIO(png)).convert("L")
    width, height = image.size
    black = zlib.compress(b"\x00" * (width * height * 3))
    alpha = zlib.compress(ImageOps.invert(image).tobytes())  # dark ink -> opaque
    content = b"q %d 0 0 %d 0 0 cm /X4 Do Q" % (width, height)
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] /Resources 7 0 R "
        b"/Contents 5 0 R >>" % (width, height),
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceRGB "
        b"/BitsPerComponent 8 /SMask 6 0 R /Filter /FlateDecode /Length %d >>\nstream\n%s"
        b"\nendstream" % (width, height, len(black), black),
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(content), content),
        b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
        b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n%s\nendstream" % (
            width, height, len(alpha), alpha),
        b"<< /XObject << /X4 4 0 R >> >>",
    ]
    out, offsets = io.BytesIO(), []
    out.write(b"%PDF-1.4\n")
    for number, body in enumerate(objects, 1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n%s\nendobj\n" % (number, body))
    xref = out.tell()
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1))
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref))
    return out.getvalue()


@tagged("post_install", "-at_install", "distri_ocr")
class TestParseReceiptRules(TransactionCase):
    """The rules added in 19.0.1.23.0 after measuring the parser against real
    receipts (synthetic texts here)."""

    OWN = own_account_values(TEST_OWN_ACCOUNT)

    def test_split_thousands_of_a_photo(self):
        for text, amount in (("Monto\n$ 174.:120,00", 174120), ("Monto\n$ 250. 500,00", 250500),
                             ("$ 115:350,00", 115350), ("Transferiste $221. 330 a Juan", 221330),
                             ("$ 147,.000,00", 147000),
                             ("$ Motivo: 253. 253.500 Varios", 253500),
                             ("$ Motivo: 282.000 Varios", 282000)):
            with self.subTest(text=text):
                result = parse_receipt(text)
                self.assertEqual(result["amount"], amount)
                self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
        # one letter between «$» and a small number is a blob, not a word
        self.assertEqual(parse_receipt("o Ó $ O 95 4 3\n$ 124.350")["amount"], 124350)

    def test_lost_thousands_dot_is_read_but_doubtful(self):
        for text, amount in (("Transferencia\n$ 1156.250", 1156250),
                             ("Transferencia\n$ 217.2183,00", 217218)):
            with self.subTest(text=text):
                result = parse_receipt(text)
                self.assertEqual(result["amount"], amount)
                self.assertEqual(result["amount_confidence"], CONFIDENCE_LOW)

    def test_negative_debit_notice(self):
        for text in ("Importe: $ -120.000,00", "$ -111.600,00"):
            result = parse_receipt(text)
            self.assertEqual(result["amount"], {"Importe: $ -120.000,00": 120000}.get(text, 111600))
            self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)

    def test_supervielle_cents_without_separators(self):
        result = parse_receipt("App Supervielle\nImporte $ 26186000")
        self.assertEqual(result["amount"], 261860)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_LOW)
        self.assertEqual(result["bank"], "Banco Supervielle")
        # another bank: a plain run of digits is what it says
        self.assertEqual(parse_receipt("Galicia\nImporte $ 2618600")["amount"], 2618600)

    def test_bare_number_after_the_label_must_look_like_money(self):
        result = parse_receipt("¡Listo! Le transferiste a (9)\nJuan Pérez\n$ 342.200")
        self.assertEqual(result["amount"], 342200)
        self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
        self.assertEqual(parse_receipt("Transferiste 500")["amount"], 0)
        self.assertEqual(parse_receipt("Transferiste 5000")["amount"], 5000)
        self.assertEqual(parse_receipt("Transferiste S 2.169.780")["amount"], 2169780)
        self.assertEqual(parse_receipt("Monto 500,00")["amount"], 500)

    def test_bare_mercado_pago_id_without_its_label(self):
        text = "Mercado Pago\nTransferencia enviada\n$ 12.000\n107785538272\nCVU 0000003100090000000018"
        self.assertEqual(parse_receipt(text)["operation"], "107785538272")
        # a labelled number wins; a CUIT or a phone never counts
        self.assertEqual(parse_receipt(text + "\nNúmero de operación 123456789012")["operation"],
                         "123456789012")
        self.assertEqual(parse_receipt("Mercado Pago\n$ 12.000\nCUIT 20-11111111-2")["operation"], "")
        self.assertEqual(parse_receipt("Mercado Pago\n$ 12.000\n5491155551234")["operation"], "")
        # only Mercado Pago screenshots: a bank receipt keeps asking for a label
        self.assertEqual(parse_receipt("Galicia\n$ 12.000\n107785538272")["operation"], "")

    def test_uala_operation_id(self):
        result = parse_receipt("Ualá\nid Op.\nA1B2C3D4E5F6G7H8I9J0K1\n$ 20.000")
        self.assertEqual(result["operation"], "A1B2C3D4E5F6G7H8I9J0K1")
        self.assertEqual(result["bank"], "Ualá")

    def test_year_and_day_come_from_the_message(self):
        arrived = datetime(2026, 8, 5, 14, 30)
        # «12026» is not a year: the year of the message
        self.assertEqual(parse_receipt("5/agosto 12026 a las 14:06\n$ 100", reference=arrived)["date"],
                         datetime(2026, 8, 5, 14, 6))
        # no year at all: never a date after the message
        self.assertEqual(parse_receipt("Lunes 5 de agosto a las 14:06\n$ 5.000",
                                       reference=arrived)["date"], datetime(2026, 8, 5, 14, 6))
        self.assertEqual(parse_receipt("30 de diciembre a las 10:00\n$ 5.000",
                                       reference=arrived)["date"], datetime(2025, 12, 30, 10, 0))
        # only the time survived: the day of the message
        self.assertEqual(parse_receipt("Comprobante a las 09:15\n$ 5.000", reference=arrived)["date"],
                         datetime(2026, 8, 5, 9, 15))
        self.assertIsNone(parse_receipt("Comprobante a las 09:15\n$ 5.000")["date"])

    def test_own_account_is_never_the_payer(self):
        # the receipt names only the destination (Santander style)
        text = "Comprobante de Transferencia\nImporte debitado $ 10.000\nCUIT 23-99999999-9\n" \
               "Cuenta destino 0000003100090000000018"
        result = parse_receipt(text, own=self.OWN)
        self.assertEqual(result["payer_cuit"], "")
        self.assertFalse(result["payer_own"])
        self.assertEqual(result["dest_cuit"], "23999999999")
        self.assertEqual(result["destination"], "0000003100090000000018")
        self.assertEqual(result["bank"], "Transferencia bancaria")
        # without the own set the old behaviour stays
        self.assertEqual(parse_receipt(text)["payer_cuit"], "23999999999")
        # the company really paid: a refund
        refund = parse_receipt("Origen\nMarcos Daniel Ejemplar\nCUIT 23-99999999-9\nDestino\n"
                               "Juan Pérez\nCUIT 20-11111111-2\n$ 10.000", own=self.OWN)
        self.assertTrue(refund["payer_own"])
        self.assertEqual(refund["payer_cuit"], "")
        self.assertEqual(refund["payer_name"], "")
        self.assertEqual(refund["dest_cuit"], "20111111112")
        # the set is configurable
        custom = own_account_values("30-71234567-0, Distri Prueba SRL, mi.alias.mp")
        self.assertEqual(custom, {"30712345670", "distri prueba srl", "mi.alias.mp"})
        other = parse_receipt("Origen\nDistri Prueba SRL\nCUIT 30-71234567-0\nDestino\nJuan Pérez\n"
                              "CUIT 20-11111111-2\n$ 1.000", own=custom)
        self.assertTrue(other["payer_own"])
        # without anybody else on the destination side, never
        alone = parse_receipt("Origen\nDistri Prueba SRL\nCUIT 30-71234567-0\n$ 1.000", own=custom)
        self.assertFalse(alone["payer_own"])

    def test_company_as_receiver_is_never_the_payer(self):
        # Mercado Pago app, «Origen y destino»: the customer first, the company
        # second (its CUIT read, the customer's not)
        mp_income = ("Transferencia enviada\n$ 45.000\nOrigen y destino\nJenifer Ayelen Berrios\n"
                     "Distri Neuquén\nCUIT/CUIL: 23-99999999-9\nCVU: 0000003100090000000018\n"
                     "Número de operación 123456789012")
        result = parse_receipt(mp_income, own=self.OWN)
        self.assertFalse(result["payer_own"])
        self.assertEqual(result["payer_name"], "Jenifer Ayelen Berrios")
        self.assertEqual(result["dest_cuit"], "23999999999")
        # the same layout, the company first and a customer's CUIT after: a refund
        mp_refund = ("Transferencia enviada\n$ 45.000\nOrigen y destino\nMarcos Daniel Ejemplar\n"
                     "CUIT/CUIL: 23-99999999-9\nCVU: 0000003100090000000018\nDebora Clara\n"
                     "CUIT/CUIL: 20-11111111-2\nCVU: 0000003100011111111111")
        result = parse_receipt(mp_refund, own=self.OWN)
        self.assertTrue(result["payer_own"])
        self.assertEqual(result["payer_cuit"], "20111111112", "the other party is what is read")
        # a junk line read as a name after the company is not another party
        junk = "$ 5.000\nOrigen y destino\nCVU: 0000003100090000000018\nEditar categoría\nAcciones relacionadas"
        self.assertFalse(parse_receipt(junk, own=self.OWN)["payer_own"])
        # bank PDF: «Titular origen» the customer, «Titular destino» the company
        bank_income = ("Comprobante de transferencia\nImporte $ 20.000,00\nTitular origen Juan Pérez\n"
                       "CUIT origen 20-11111111-2\nTitular destino Marcos Daniel Ejemplar\n"
                       "CUIT destino 23-99999999-9\nCBU destino 0000003100090000000018")
        result = parse_receipt(bank_income, own=self.OWN)
        self.assertFalse(result["payer_own"])
        self.assertEqual(result["payer_cuit"], "20111111112")
        bank_refund = ("Comprobante de transferencia\nImporte $ 20.000,00\nTitular origen Marcos Daniel "
                       "Ejemplar\nCUIT origen 23-99999999-9\nTitular destino Juan Pérez\n"
                       "CUIT destino 20-11111111-2")
        self.assertTrue(parse_receipt(bank_refund, own=self.OWN)["payer_own"])
        # Ualá: «Recibe» the company, only its block is read
        uala = ("Ualá\nComprobante de transferencia\nMonto debitado $ 12.000,00\nRecibe Marcos Daniel "
                "Ejemplar\nCUIT 23-99999999-9\nCVU 0000003100090000000018\nid Op.\nA1B2C3D4E5F6G7H8I9J0K1")
        result = parse_receipt(uala, own=self.OWN)
        self.assertFalse(result["payer_own"])
        self.assertEqual(result["payer_cuit"], "")
        # a receipt that names only the company after «De» (no destination marker)
        only_company = "De\nMarcos Daniel Ejemplar\nCUIT 23-99999999-9\n$ 3.000"
        self.assertFalse(parse_receipt(only_company, own=self.OWN)["payer_own"])
        self.assertIn("distri neuquen", self.OWN)

    def test_bank_markers(self):
        for text, bank in (("Resimple\nTe informamos de transferencia", "Resimple"),
                           ("FEPN\nComprobante de transferencia", "FEPN"),
                           ("App Supervielle", "Banco Supervielle"),
                           ("Nro. de Referencia: 123", "Transferencia bancaria"),
                           ("Importe debitado $ 5.000", "Transferencia bancaria"),
                           ("COELSA ID ABC", "Transferencia bancaria"),
                           ("Mercado Pago\nCOELSA ID ABC", "Mercado Pago")):
            with self.subTest(text=text):
                self.assertEqual(parse_receipt(text)["bank"], bank)

    def test_amounts_that_differ_only_by_3_and_8(self):
        self.assertTrue(differs_only_by_3_and_8(133500, 138500))
        self.assertTrue(differs_only_by_3_and_8(83000.5, 38000.5))
        self.assertFalse(differs_only_by_3_and_8(133500, 133500))
        self.assertFalse(differs_only_by_3_and_8(133500, 138600))
        self.assertFalse(differs_only_by_3_and_8(133500, 1338500))

    def test_english_pass_arbitrates_a_3_read_for_an_8(self):
        readings = {"spa": ("Monto\n$ 133.500\nNúmero de operación 123456789012", 90.0),
                    "eng": ("Monto\n$ 138.500\nNumero de operacion 123456789012", 88.0)}
        calls = []

        def fake(data, lang="spa", change=None, psm=3):
            calls.append(lang)
            return readings[lang]

        with patch.object(ocr_comprobante, "ocr_image", fake):
            text, confidence, reading = read_image(b"photo", double_check=False)
        self.assertEqual(calls, ["spa", "eng"])
        self.assertEqual(reading["amount"], 138500)
        self.assertTrue(reading["amount_arbitrated"])
        self.assertEqual(reading["amount_confidence"], CONFIDENCE_HIGH)
        self.assertIn("133.500", text, "the Spanish text is kept")
        # no «3» in the amount: no second pass; another difference: Spanish wins
        readings["spa"] = ("Monto\n$ 145.000", 90.0)
        calls.clear()
        with patch.object(ocr_comprobante, "ocr_image", fake):
            self.assertEqual(read_image(b"photo", double_check=False)[2]["amount"], 145000)
        self.assertEqual(calls, ["spa"])
        readings["spa"] = ("Monto\n$ 133.500", 90.0)
        readings["eng"] = ("Monto\n$ 135.500", 90.0)
        with patch.object(ocr_comprobante, "ocr_image", fake):
            self.assertEqual(read_image(b"photo", double_check=False)[2]["amount"], 133500)

    def test_double_reading_confirms_or_doubts_every_amount(self):
        """19.0.1.55.0: an amount stands only when a second reading agrees;
        a first reading without amount gets only a suggestion («low»)."""
        def run(first, seconds):
            texts = iter([first] + seconds)

            def fake(data, lang="spa", change=None, psm=3):
                return next(texts, ""), 90.0

            with patch.object(ocr_comprobante, "ocr_image", fake):
                return read_image(b"photo")[2]

        one = "Monto\n$ 45.000\nNúmero de operación 123456789012"
        other = "Monto\n$ 54.000\nNúmero de operación 123456789012"
        nothing = "Comprobante de transferencia"
        agreed = run(one, [one])
        self.assertEqual((agreed["amount"], agreed["amount_confidence"]), (45000, CONFIDENCE_HIGH))
        doubted = run(one, [other, nothing, nothing, nothing])
        self.assertEqual(doubted["amount_confidence"], CONFIDENCE_LOW)
        self.assertTrue(doubted["amount_unconfirmed"])
        suggested = run(nothing, [one, one])
        self.assertEqual((suggested["amount"], suggested["amount_confidence"]),
                         (45000, CONFIDENCE_LOW))
        self.assertEqual(suggested["operation"], "123456789012", "two readings agree")
        noise = run(nothing, ["Monto\n$ 400", nothing, nothing, nothing])
        self.assertFalse(noise.get("amount"), "below $1.000 is noise")

    def test_pdf_with_the_receipt_in_the_alpha_channel(self):
        png, expected = receipt_images.render("Ualá", receipt_images.random_data(random.Random(5)))
        pngs = pdf_images(masked_pdf(png))
        self.assertEqual(len(pngs), 1)
        from PIL import Image
        picture = Image.open(io.BytesIO(pngs[0])).convert("L")
        histogram = picture.histogram()
        total = sum(histogram)
        self.assertGreater(sum(histogram[200:]) / total, 0.5, "mostly white paper")
        self.assertGreater(sum(histogram[:80]) / total, 0.005, "with dark ink")
        if ocr_available():
            text, confidence = ocr_image(pngs[0])
            self.assertAlmostEqual(parse_receipt(text, confidence)["amount"], expected["amount"], 2)

    @needs_tesseract
    def test_read_image_keeps_every_layout_right(self):
        rng = random.Random(11)
        for bank in receipt_images.LAYOUTS:
            with self.subTest(bank=bank):
                png, expected = receipt_images.render(bank, receipt_images.random_data(rng))
                _text, _confidence, result = read_image(receipt_images.degrade(png, rng, 1))
                self.assertAlmostEqual(result["amount"], expected["amount"], places=2)
                self.assertEqual(result["amount_confidence"], CONFIDENCE_HIGH)
