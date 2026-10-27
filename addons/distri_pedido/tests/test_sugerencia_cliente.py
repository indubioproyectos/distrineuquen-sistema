"""19.0.1.43.0: the text sent after a receipt photo and the customer it
suggests (models/sugerencia_cliente.py). Every customer here is fictitious."""
from datetime import datetime, timedelta

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.sugerencia_cliente import (
    CustomerNameIndex, name_tokens, note_tokens,
)

WHEN = datetime(2026, 10, 20, 15, 0)


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

    def _text(self, body, minutes=0, sender="Gerente Prueba"):
        return self.Message.create({
            "wa_message_id": "chat:t:%s:%s:%s" % (sender, minutes, body), "direction": "in",
            "message_type": "text", "wa_id": "grupo-" + sender.replace(" ", "").lower(),
            "profile_name": sender, "body": body, "timestamp": WHEN + timedelta(minutes=minutes),
            "distri_chat_import_id": self.chat.id, "distri_chat_sender": sender})

    def _photo(self, minutes=0, body="", sender="Gerente Prueba", key=None):
        msg = self.Message.create({
            "wa_message_id": "chat:%s" % (key or "%s:%s" % (sender, minutes)), "direction": "in",
            "message_type": "image", "wa_id": "grupo-" + sender.replace(" ", "").lower(),
            "profile_name": sender, "body": body, "timestamp": WHEN + timedelta(minutes=minutes),
            "media_mime_type": "image/jpeg", "media_state": "pending",
            "distri_chat_import_id": self.chat.id, "distri_chat_sender": sender})
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
