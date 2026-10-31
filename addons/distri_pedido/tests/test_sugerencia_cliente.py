"""19.0.1.43.0: the text sent after a receipt photo and the customer it
suggests (models/sugerencia_cliente.py). Every customer here is fictitious."""
from datetime import datetime, timedelta
from unittest.mock import patch

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.cobros import NOTE_VS_PAYER
from odoo.addons.distri_pedido.models.ocr_comprobante import parse_receipt
from odoo.addons.distri_pedido.models.sugerencia_cliente import (
    CustomerNameIndex, name_tokens, note_tokens,
)

WHEN = datetime(2026, 10, 20, 15, 0)
# a Ualá PDF of 2026 (fictitious data, tests/test_ocr.py): the payer's CUIT
# is 27-22222222-8
UALA_2026 = ("“YN ualá\nComprobante de transferencia\n$76.100\n30/10/2026 - 19:59 hs\n"
             "Destinatario Marcos Daniel Ejemplar\nCuenta destino Mercado Pago\n"
             "CVU destino 0000003100090000000018\nCUIT/CUIL destino 23-99999999-9\n"
             "Emisor NORMA/JULIA PRUEBA\nCBU emisor 3840200500000000000001\n"
             "CUIT/CUIL emisor 27-22222222-8\nMotivo Varios\n"
             "Id. Op. 86f905f4-aa8c-c8be-4a64-a45abc783473")


@tagged("post_install", "-at_install", "distri_sugerencia")
class TestSugerenciaCliente(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        Partner = cls.env["res.partner"]
        cls.jose = Partner.create({"name": "Y - ZORVATELLO JOSÉ - (PLOTTIER)",
                                   "company_type": "company"})
        cls.ana = Partner.create({"name": "ZORVATELLO ANA - KIOSCO", "company_type": "company"})
        cls.nico = Partner.create({"name": "ALMACÉN TRESVALLE 9 - NICOLASIO",
                                   "company_type": "company"})
        cls.delta = Partner.create({"name": "Kiosco Prueba Deltaverde",
                                    "company_type": "company"})
        cls.chat = cls.env["distri.whatsapp.chat.import"].sudo().create({"name": "chat.zip"})
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Collection = cls.env["distri.mp.collection"]

    # ------------------------------------------------------------ helpers

    def _text(self, body, minutes=0, sender="Gerente Prueba", pos=0):
        return self.Message.create({
            "wa_message_id": "chat:t:%s:%s:%s" % (sender, minutes, body), "direction": "in",
            "message_type": "text", "wa_id": "grupo-" + sender.replace(" ", "").lower(),
            "profile_name": sender, "body": body, "timestamp": WHEN + timedelta(minutes=minutes),
            "distri_chat_import_id": self.chat.id, "distri_chat_sender": sender,
            "distri_chat_minute_pos": pos})

    def _photo(self, minutes=0, body="", sender="Gerente Prueba", key=None, pos=0, pdf=False,
               defer=False):
        """`defer`: not read on creation, as a chat import does until all
        its files exist (models/puesta_al_dia.py)."""
        Message = self.Message.with_context(distri_defer_receipt_read=defer)
        msg = Message.create({
            "wa_message_id": "chat:%s" % (key or "%s:%s:%s" % (sender, minutes, pos)),
            "direction": "in", "message_type": "document" if pdf else "image",
            "wa_id": "grupo-" + sender.replace(" ", "").lower(),
            "profile_name": sender, "body": body, "timestamp": WHEN + timedelta(minutes=minutes),
            "media_mime_type": "application/pdf" if pdf else "image/jpeg",
            "media_state": "pending", "distri_chat_import_id": self.chat.id,
            "distri_chat_sender": sender, "distri_chat_minute_pos": pos})
        msg.with_context(distri_receipt_matching=True).write(
            {"distri_is_receipt": True, "distri_receipt_state": "matched"})
        return msg

    def _card(self, receipt, source="970000009001"):
        card = self.Collection.create({
            "source_id": source, "date": receipt.timestamp, "amount": 1000,
            "method_type": "bank_transfer", "proposal": "none"})
        receipt.with_context(distri_receipt_matching=True).write(
            {"distri_collection_id": card.id})
        card.sudo().write({"receipt_id": receipt.id})
        return card

    def _index(self):
        return CustomerNameIndex([(p.id, p.name) for p in (
            self.jose, self.ana, self.nico, self.delta)])

    # ------------------------------------------------------------ text after the photo

    def test_following_text_stops_at_the_next_photo(self):
        photo = self._photo(minutes=0)
        self._text("Hola", minutes=0, sender="Otro Vendedor")
        self._text("José Zorvatello", minutes=1)
        self._photo(minutes=2)
        self._text("Ana Zorvatello", minutes=2)
        self._text("Nicola tresvalle", minutes=3)
        self.assertEqual(photo._distri_following_text(), "José Zorvatello")
        self.assertEqual(photo._distri_receipt_text(), ("José Zorvatello", True))

    def test_caption_wins_and_shared_minute_is_nobodys(self):
        captioned = self._photo(minutes=10, body="Ana Zorvatello")
        self._text("José Zorvatello", minutes=11)
        self.assertEqual(captioned._distri_receipt_text(), ("Ana Zorvatello", False))
        # two photos of the same minute: the text of that minute is of neither
        first = self._photo(minutes=20, key="a")
        second = self._photo(minutes=20, key="b")
        self._text("Ana Zorvatello", minutes=20)
        self.assertEqual(first._distri_following_text(), "")
        self.assertEqual(second._distri_following_text(), "")
        self._text("José Zorvatello", minutes=21)
        self.assertEqual(second._distri_following_text(), "José Zorvatello")

    def test_viewer_shows_the_following_text(self):
        photo = self._photo(minutes=30)
        self._text("José Zorvatello", minutes=31)
        card = self._card(photo)
        html = str(card.receipt_context_html)
        self.assertIn("Texto del comprobante (mensaje siguiente):", html)
        self.assertIn("José Zorvatello", html)
        self.assertNotIn("sin texto", html)
        lonely = self._photo(minutes=40)
        self.assertIn("sin texto", str(self._card(lonely, "970000009002").receipt_context_html))

    # ------------------------------------------------------------ matching

    def test_normalized_words(self):
        self.assertEqual(name_tokens("Y - ZORVATELLO JOSÉ - (PLOTTIER)"),
                         ({"zorvatello", "jose"}, {"plottier"}))
        self.assertEqual(note_tokens("José  zorvatello <Se editó este mensaje.>"),
                         {"jose", "zorvatello"})
        self.assertEqual(note_tokens("MERCADO ZORVATELLO AL DIA"), {"mercado", "zorvatello"})
        self.assertEqual(note_tokens("15.000"), set())
        self.assertEqual(note_tokens("mañana te paso lo que falta del pedido de ayer temprano"),
                         set())

    def test_one_customer_whatever_the_order_and_accents(self):
        index = self._index()
        for text in ("José Zorvatello", "ZORVATELLO JOSE", "zorvatello josé plottier",
                     "Jose - Zorvatello"):
            self.assertEqual(index.suggest(text)[0], self.jose.id, text)
        # «Nicola» starts «NICOLASIO»; the number is a word of the name
        self.assertEqual(index.suggest("Nicola tresvalle 9")[0], self.nico.id)

    def test_no_suggestion_when_ambiguous_or_not_a_name(self):
        index = self._index()
        self.assertFalse(index.suggest("Zorvatello")[0])          # two customers
        self.assertFalse(index.suggest("Plottier")[0])            # only the locality
        self.assertFalse(index.suggest("DIFERENCIA RENDICION")[0])
        self.assertFalse(index.suggest("José Zorvatello\nNicola tresvalle")[0])
        self.assertEqual(index.suggest("José Zorvatello\nDIFERENCIA")[0], self.jose.id)

    # ------------------------------------------------------------ suggestion on the card

    def test_suggestion_is_never_assigned_and_confirm_assigns_it(self):
        photo = self._photo(minutes=50)
        self._text("Jose zorvatello", minutes=51)
        card = self._card(photo, "970000009003")
        photo._distri_read_receipt()
        self.assertFalse(photo.distri_partner_id)
        self.assertFalse(card.partner_id)
        self.assertEqual(photo.distri_suggested_partner_id, self.jose)
        self.assertEqual(photo.distri_suggestion_note, "Por el texto «Jose zorvatello»")
        self.assertEqual(card.suggested_partner_id, self.jose)
        card.action_use_suggestion()
        self.assertEqual(card.partner_id, self.jose)
        self.assertEqual(card.proposal, "manual")
        self.assertEqual(card.proposal_note, "Asignado por %s" % self.env.user.name)
        self.assertFalse(photo.distri_suggested_partner_id)
        with self.assertRaises(UserError):
            card.action_use_suggestion()

    def test_ambiguous_text_suggests_nothing(self):
        photo = self._photo(minutes=60)
        self._text("Zorvatello", minutes=61)
        self.assertFalse(photo._distri_suggest_partner())
        self.assertFalse(photo.distri_suggested_partner_id)
        self.assertFalse(photo.distri_partner_id)

    def test_exact_name_still_assigns(self):
        """A text that is (part of) exactly one name keeps the exact match of
        `_distri_partners_by_text`: the customer, not a suggestion."""
        self._text("Kiosco Prueba Deltaverde", minutes=70)
        photo = self._photo(minutes=70)
        photo._distri_read_receipt()
        self.assertEqual(photo.distri_partner_id, self.delta)
        self.assertEqual(photo.distri_partner_source, "Nota de Gerente Prueba junto a la foto")
        self.assertFalse(photo.distri_suggested_partner_id)

    def test_refresh_fills_only_suggestions(self):
        photo = self._photo(minutes=80)
        self._text("Ana Zorvatello Kiosco", minutes=81)
        result = self.Message._distri_refresh_suggestions([("id", "=", photo.id)])
        self.assertEqual(result, {"receipts": 1, "suggested": 1})
        self.assertEqual(photo.distri_suggested_partner_id, self.ana)
        self.assertFalse(photo.distri_partner_id)
        # the receipt gets its customer: the suggestion is over
        photo.write({"distri_partner_id": self.ana.id})
        self.assertFalse(photo.distri_suggested_partner_id)

    # ------------------------------------------------------------ several photos in a minute

    def _norma_and_valdalma(self):
        """Two customers: the first one's CUIT is known (27-22222222-8, the
        payer of UALA_2026) and its note («Norma Prueba») is no exact match."""
        Partner = self.env["res.partner"]
        norma = Partner.create({"name": "PRUEBA NORMA - DESPENSA TESOROS",
                                "company_type": "company"})
        valdalma = Partner.create({"name": "ALMACEN VALDALMA - PRUEBA",
                                   "company_type": "company"})
        self.env["distri.payer.identity"].sudo().create(
            {"partner_id": norma.id, "kind": "cuit", "value": "27222222228"})
        return norma, valdalma

    def _reading(self, *with_uala):
        """`_distri_file_reading` patched: UALA_2026 for the given receipts."""
        def reading(msg):
            return (UALA_2026, parse_receipt(UALA_2026)) if msg in with_uala else ("", {})
        return patch.object(type(self.Message), "_distri_file_reading", reading)

    def test_two_notes_then_two_photos_pair_in_order(self):
        self._text("Zorvatello Ana", minutes=200, pos=1)
        self._text("Kiosco Prueba Deltaverde", minutes=200, pos=2)
        first = self._photo(minutes=200, pos=3, defer=True)
        second = self._photo(minutes=200, pos=4, defer=True)
        self.assertEqual(first._distri_chat_paired_notes(), ["Zorvatello Ana"])
        self.assertEqual(second._distri_following_text(), "Kiosco Prueba Deltaverde")
        (first | second)._distri_read_receipt()
        self.assertEqual(first.distri_partner_id, self.ana)
        self.assertEqual(second.distri_partner_id, self.delta)
        self.assertEqual(second.distri_partner_source, "Nota de Gerente Prueba junto a la foto")

    def test_photo_then_note_twice_in_a_minute(self):
        """The receipts group of 31/10/2026: a PDF, its customer, a photo,
        its customer, all in one minute. The PDF's note is no exact name, and
        the CUIT read on it names the customer; the photo keeps its own note,
        never the PDF's."""
        norma, valdalma = self._norma_and_valdalma()
        pdf = self._photo(minutes=210, pos=1, pdf=True, defer=True)
        self._text("Norma Prueba", minutes=210, pos=2)
        photo = self._photo(minutes=210, pos=3, defer=True)
        self._text("Valdalma", minutes=210, pos=4)
        self.assertEqual(pdf._distri_chat_paired_notes(), ["Norma Prueba"])
        self.assertEqual(photo._distri_chat_paired_notes(), ["Valdalma"])
        with self._reading(pdf):
            (pdf | photo)._distri_read_receipt(read_file=True)
        self.assertEqual((pdf.distri_payer_name, pdf.distri_payer_cuit, pdf.distri_bank),
                         ("NORMA/JULIA PRUEBA", "27222222228", "Ualá"))
        self.assertEqual(pdf.distri_partner_id, norma)
        self.assertEqual(pdf.distri_partner_source,
                         "CUIT de quien pagó (27-22222222-8), leído de la foto")
        self.assertEqual(photo.distri_partner_id, valdalma)

    def test_unclear_pairs_name_nobody(self):
        # two notes, three photos: nobody knows which note is whose
        self._text("Zorvatello Ana", minutes=220, pos=1)
        self._text("Kiosco Prueba Deltaverde", minutes=220, pos=2)
        photos = self.Message.browse()
        for pos in (3, 4, 5):
            photos |= self._photo(minutes=220, pos=pos, defer=True)
        photos._distri_read_receipt()
        for photo in photos:
            self.assertEqual(photo._distri_chat_paired_notes(), [])
            self.assertFalse(photo.distri_partner_id)
            self.assertFalse(photo.distri_suggested_partner_id)

    def test_another_senders_line_splits_the_runs(self):
        self._text("Zorvatello Ana", minutes=230, pos=1)
        first = self._photo(minutes=230, pos=2, defer=True)
        self._text("Recibido", minutes=230, pos=3, sender="Otro Vendedor")
        self._text("Kiosco Prueba Deltaverde", minutes=230, pos=4)
        second = self._photo(minutes=230, pos=5, defer=True)
        (first | second)._distri_read_receipt()
        self.assertEqual(first.distri_partner_id, self.ana)
        self.assertEqual(second.distri_partner_id, self.delta)

    def test_single_photo_keeps_every_nearby_note(self):
        self._text("Kiosco Prueba Deltaverde", minutes=240, pos=2)
        photo = self._photo(minutes=240, pos=1)
        self._photo(minutes=240, pos=1, sender="Otro Vendedor")
        self.assertIsNone(photo._distri_chat_paired_notes())
        self.assertEqual(photo.distri_partner_id, self.delta)

    def test_note_against_the_payers_cuit_is_a_conflict(self):
        """The note names one customer and the CUIT read on the receipt is
        another one's: the receipt keeps no customer and its card waits in
        «Revisar» with both names."""
        norma, valdalma = self._norma_and_valdalma()
        self._text("Valdalma", minutes=250)
        photo = self._photo(minutes=250, pdf=True)
        self.assertEqual(photo.distri_partner_id, valdalma)
        card = self._card(photo)
        card._propose()
        self.assertEqual((card.partner_id, card.proposal), (valdalma, "photo"))
        with self._reading(photo):
            photo.write({"media_state": "done"})
        note = NOTE_VS_PAYER % (valdalma.name, "27-22222222-8", norma.name)
        self.assertEqual(photo.distri_conflict, note)
        self.assertFalse(photo.distri_partner_id)
        self.assertFalse(photo.distri_suggested_partner_id)
        self.assertFalse(card.partner_id)
        self.assertEqual(card.conflict_note, note)
        self.assertEqual(card.column, "review")
        self.assertEqual(card.review_reason, note)

    def test_reread_fixes_a_receipt_paired_with_the_wrong_note(self):
        """A receipt imported before 19.0.1.64.0: the nearest note named the
        customer and the destination's wallet was taken as the payer's."""
        norma, valdalma = self._norma_and_valdalma()
        pdf = self._photo(minutes=260, pos=1, pdf=True, defer=True)
        self._text("Norma Prueba", minutes=260, pos=2)
        photo = self._photo(minutes=260, pos=3, defer=True)
        self._text("Valdalma", minutes=260, pos=4)
        pdf.with_context(distri_receipt_matching=True).write({
            "distri_partner_id": valdalma.id, "distri_bank": "Mercado Pago",
            "distri_partner_source": "Nota de Gerente Prueba junto a la foto"})
        pdf.attachment_id = self.env["ir.attachment"].create(
            {"name": "Comprobante de transferencia.pdf", "raw": b"%PDF-1.4",
             "mimetype": "application/pdf"})
        card = self._card(pdf)
        card._propose()
        self.assertEqual(card.partner_id, valdalma)
        with self._reading(pdf):
            result = self.Message._distri_reread_chat_receipts([pdf.id, photo.id])
        # the photo has no file: nothing to read again
        self.assertEqual(result, {"receipts": 1, "changed": 1})
        self.assertEqual(pdf.distri_partner_id, norma)
        self.assertEqual((pdf.distri_payer_cuit, pdf.distri_bank), ("27222222228", "Ualá"))
        self.assertEqual((card.partner_id, card.proposal), (norma, "photo"))
        self.assertFalse(card.conflict_note)
