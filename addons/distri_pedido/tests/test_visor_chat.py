"""19.0.1.46.0: the chat viewer of the transfers group (models/visor_chat.py).
Which export is shown for each period, the duplicates between exports, the
order inside a minute and who may open it. Every sender, customer and file
here is fictitious; the dates are in 2031 so the lab copy's real chat never
falls inside the tested window.
19.0.1.49.0: the search results panel (count, pages, snippet) and the
receipt's own date on each photo or PDF."""
import io
import os
import tempfile
import zipfile
from datetime import date, datetime, timedelta

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.puesta_al_dia import minute_positions, parse_chat
from odoo.addons.distri_pedido.models.reparto_cliente import AR_TZ
from odoo.addons.distri_pedido.models.visor_chat import (
    PENDING_DAYS, is_visible, parse_amount, receipt_is_far, receipt_moment, select_visible,
    snippet, to_utc)

OWN_SINCE = date(2031, 5, 10)
DAY_BEFORE = datetime(2031, 5, 9, 10, 5)     # Argentine local times
DAY_OWN = datetime(2031, 5, 10, 9, 30)


def _entry(key, owner, kind, local, body="", pos=0, checksum="", from_card=False):
    return {"key": key, "owner": owner, "kind": kind, "local": local, "ts": to_utc(local),
            "body": body, "pos": pos, "checksum": checksum, "from_card": from_card}


@tagged("post_install", "-at_install", "distri_visor_chat")
class TestVisorChatRule(AccountTestInvoicingCommon):
    """The source rule alone, on plain entries."""

    def test_texts_come_from_one_export_per_day(self):
        before = [_entry("m1", "manager", "text", DAY_BEFORE, "Kiosco Prueba"),
                  _entry("m2", "own", "text", DAY_BEFORE, "Kiosco Prueba")]
        on_own = [_entry("m3", "manager", "text", DAY_OWN, "Almacén Prueba"),
                  _entry("m4", "own", "text", DAY_OWN, "Almacén Prueba")]
        keys = [e["key"] for e in select_visible(before + on_own, OWN_SINCE)]
        self.assertEqual(keys, ["m1", "m4"])

    def test_files_are_shown_whatever_the_export(self):
        self.assertTrue(is_visible(_entry("m5", "manager", "image", DAY_OWN), OWN_SINCE))
        self.assertTrue(is_visible(_entry("m6", "own", "pdf", DAY_BEFORE), OWN_SINCE))
        # an import without owner: its files, never its texts
        self.assertTrue(is_visible(_entry("m7", "", "image", DAY_BEFORE), OWN_SINCE))
        self.assertFalse(is_visible(_entry("m8", "", "text", DAY_BEFORE, "x"), OWN_SINCE))

    def test_denis_fills_only_the_manager_period(self):
        self.assertTrue(is_visible(_entry("d1", "admin", "pdf", DAY_BEFORE, from_card=True),
                                   OWN_SINCE))
        self.assertFalse(is_visible(_entry("d2", "admin", "pdf", DAY_OWN, from_card=True),
                                    OWN_SINCE))

    def test_same_file_twice_is_shown_once_preferring_the_message(self):
        entries = [_entry("d9", "admin", "image", DAY_BEFORE, checksum="abc", from_card=True),
                   _entry("m9", "manager", "image", DAY_BEFORE, checksum="abc")]
        self.assertEqual([e["key"] for e in select_visible(entries, OWN_SINCE)], ["m9"])

    def test_order_inside_the_minute_follows_the_export(self):
        # the text was created first (lower id) but came second in the chat
        entries = [_entry("m10", "manager", "text", DAY_BEFORE, "Nota", pos=2),
                   _entry("m11", "manager", "image", DAY_BEFORE, pos=1),
                   _entry("d12", "admin", "pdf", DAY_BEFORE, from_card=True),
                   _entry("m13", "manager", "text", DAY_BEFORE - timedelta(minutes=1), "Hola",
                          pos=1)]
        self.assertEqual([e["key"] for e in select_visible(entries, OWN_SINCE)],
                         ["m13", "m11", "m10", "d12"])

    def test_minute_positions_count_every_line_of_the_minute(self):
        text = ("9/05/2031, 10:05 - Vendedor Prueba: IMG-1.jpg (archivo adjunto)\n"
                "9/05/2031, 10:05 - Vendedor Prueba: Kiosco Prueba\n"
                "9/05/2031, 10:05 - Otro Prueba: <Multimedia omitido>\n"
                "9/05/2031, 10:06 - Otro Prueba: Almacén\nsegunda línea\n")
        rows = parse_chat(text)
        self.assertEqual(minute_positions(rows), [1, 2, 3, 1])

    def test_receipt_date_prefers_what_was_read(self):
        sent = datetime(2031, 5, 9, 10, 5)
        read = to_utc(datetime(2031, 5, 8, 22, 47))
        card = to_utc(datetime(2031, 5, 8, 22, 48))
        self.assertEqual(receipt_moment(read, "", sent, "income", card),
                         (datetime(2031, 5, 8, 22, 47), True, "read"))
        # the text read from the file, with the same parser as the receipts
        moment = receipt_moment(None, "Transferencia enviada\n7 de mayo de 2031 a las 21:15 hs",
                                sent, "income", card)
        self.assertEqual(moment, (datetime(2031, 5, 7, 21, 15), True, "text"))
        # a bare time is not the receipt's date: the card's transfer date
        self.assertEqual(receipt_moment(None, "a las 21:15", sent, "income", card),
                         (datetime(2031, 5, 8, 22, 48), True, "card"))
        # only an income card has a transfer date
        self.assertEqual(receipt_moment(None, "", sent, "receipt", card), (None, False, ""))

    def test_receipt_far_from_its_message(self):
        sent = datetime(2031, 5, 9, 10, 5)
        self.assertFalse(receipt_is_far(datetime(2031, 5, 8, 22, 47), True, sent))
        self.assertTrue(receipt_is_far(datetime(2031, 5, 8, 10, 4), True, sent))
        self.assertFalse(receipt_is_far(datetime(2031, 5, 8), False, sent))
        self.assertTrue(receipt_is_far(datetime(2031, 5, 7), False, sent))
        self.assertFalse(receipt_is_far(None, False, sent))

    def test_snippet_around_the_match(self):
        text = "x" * 300 + " Albarrán Prueba " + "y" * 300
        cut = snippet(text, "albarrán")
        self.assertTrue(cut.startswith("…") and cut.endswith("…"))
        self.assertIn("Albarrán Prueba", cut)
        self.assertEqual(snippet("Kiosco Prueba", "prueba"), "Kiosco Prueba")

    def test_parse_amount(self):
        self.assertEqual(parse_amount("$ 100.000"), 100000.0)
        self.assertEqual(parse_amount("1.500,50"), 1500.5)
        self.assertIsNone(parse_amount("Albarrán"))


@tagged("post_install", "-at_install", "distri_visor_chat")
class TestVisorChat(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.chat_propio_desde", OWN_SINCE.isoformat())
        Import = cls.env["distri.whatsapp.chat.import"].sudo()
        cls.manager = Import.create({"name": "gerente.zip", "distri_export_owner": "manager"})
        cls.own = Import.create({"name": "propio.zip", "distri_export_owner": "own"})
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Viewer = cls.env["distri.chat.viewer"]
        cls.counter = 0

        # 09/05: the manager's export, with the same texts in the own export
        cls.m_photo = cls._file(cls.manager, DAY_BEFORE, b"photo-a", pos=1)
        cls.m_note = cls._text(cls.manager, DAY_BEFORE, "Kiosco Prueba Uno", pos=2,
                               sender="Vendedor Prueba Largo")
        cls.o_note_dup = cls._text(cls.own, DAY_BEFORE, "Kiosco Prueba Uno", pos=2,
                                   sender="Vendedor")
        # 10/05: the own export; the manager's copy of the same text is hidden
        cls.m_text_own_day = cls._text(cls.manager, DAY_OWN, "Almacén Prueba Dos", pos=1)
        cls.o_text = cls._text(cls.own, DAY_OWN, "Almacén Prueba Dos", pos=1)
        cls.m_shared_photo = cls._file(cls.manager, DAY_OWN, b"photo-b", pos=2)

        # Denis's receipts kept on cards by the cross
        cls.card_denis = cls._card("970000009101", DAY_BEFORE + timedelta(minutes=3))
        cls.denis_file = cls._card_file(cls.card_denis, b"pdf-denis", DAY_BEFORE + timedelta(
            minutes=3), "comprobante-denis.pdf")
        cls.card_dup = cls._card("970000009102", DAY_BEFORE)
        cls._card_file(cls.card_dup, b"photo-a", DAY_BEFORE, "IMG-dup.jpg")
        cls.card_late = cls._card("970000009103", DAY_OWN)
        cls._card_file(cls.card_late, b"pdf-late", DAY_OWN, "tarde.pdf")
        cls.card_photo = cls._card("970000009104", DAY_BEFORE, receipt=cls.m_photo,
                                   amount=123456)
        cls.card_none = cls._card("970000009105", DAY_OWN + timedelta(hours=2))

    # ------------------------------------------------------------ helpers

    @classmethod
    def _next(cls):
        cls.counter += 1
        return cls.counter

    @classmethod
    def _text(cls, chat, local, body, pos=0, sender="Vendedor Prueba"):
        return cls.Message.create({
            "wa_message_id": "chat:t:visor-%s" % cls._next(), "direction": "in",
            "message_type": "text", "wa_id": "grupo-visorprueba", "profile_name": sender,
            "body": body, "timestamp": to_utc(local), "distri_chat_import_id": chat.id,
            "distri_chat_sender": sender, "distri_chat_minute_pos": pos})

    @classmethod
    def _file(cls, chat, local, raw, pos=0, sender="Vendedor Prueba"):
        msg = cls.Message.create({
            "wa_message_id": "chat:visor-%s" % cls._next(), "direction": "in",
            "message_type": "image", "wa_id": "grupo-visorprueba", "profile_name": sender,
            "timestamp": to_utc(local), "media_mime_type": "image/jpeg",
            "media_filename": "IMG-%s.jpg" % cls.counter, "media_state": "pending",
            "distri_chat_import_id": chat.id, "distri_chat_sender": sender,
            "distri_chat_minute_pos": pos})
        attachment = cls.env["ir.attachment"].sudo().create({
            "name": msg.media_filename, "raw": raw, "mimetype": "image/jpeg",
            "res_model": msg._name, "res_id": msg.id})
        msg.write({"attachment_id": attachment.id})
        return msg

    @classmethod
    def _card(cls, source, local, receipt=None, amount=1000):
        card = cls.env["distri.mp.collection"].sudo().create({
            "source_id": source, "date": to_utc(local), "amount": amount,
            "method_type": "bank_transfer", "proposal": "none"})
        if receipt:
            card.sudo().write({"receipt_id": receipt.id})
        return card

    @classmethod
    def _card_file(cls, card, raw, local, name):
        attachment = cls.env["ir.attachment"].sudo().create({
            "name": name, "raw": raw, "res_model": card._name, "res_id": card.id})
        card.sudo().message_post(
            body="Cobro cargado por el cruce de prueba. Comprobante %s, export del "
                 "administrador, llegó el %s, lo mandó Denis Prueba, vínculo importe_hora, "
                 "leyenda «Kiosco Prueba»." % (name, local.strftime("%Y-%m-%d %H:%M")),
            attachment_ids=[attachment.id])
        return attachment

    def _window(self):
        entries = self.Viewer._entries_between(to_utc(datetime(2031, 5, 9)),
                                               to_utc(datetime(2031, 5, 11)))
        return [e["key"] for e in entries]

    # ------------------------------------------------------------ source and duplicates

    def test_each_period_shows_its_export_once(self):
        keys = self._window()
        expected = ["m%d" % self.m_photo.id, "m%d" % self.m_note.id,
                    "d%d" % self.denis_file.id,
                    "m%d" % self.o_text.id, "m%d" % self.m_shared_photo.id]
        self.assertEqual(keys, expected)
        for hidden in (self.o_note_dup, self.m_text_own_day):
            self.assertNotIn("m%d" % hidden.id, keys)

    def test_open_from_the_receipt_is_centered_on_it(self):
        action = self.m_photo.action_open_chat()
        self.assertEqual(action["tag"], "distri_chat_viewer")
        self.assertEqual(action["params"], {"message_id": self.m_photo.id})
        data = self.Viewer.distri_chat_open(message_id=self.m_photo.id)
        keys = [e["key"] for e in data["entries"]]
        self.assertEqual(data["anchor"], "m%d" % self.m_photo.id)
        # the note sent after the photo, in the same minute group
        photo = keys.index("m%d" % self.m_photo.id)
        self.assertEqual(keys[photo + 1], "m%d" % self.m_note.id)
        entry = data["entries"][photo]
        self.assertEqual((entry["source_tag"], entry["time"], entry["kind"]),
                         ("gerente", "10:05", "image"))
        self.assertTrue(entry["pos_known"])

    def test_chat_in_a_new_tab_from_the_card(self):
        action = self.card_photo.action_tab_chat()
        self.assertEqual((action["type"], action["target"]), ("ir.actions.act_url", "new"))
        self.assertEqual(action["url"],
                         "/odoo/action-distri_chat_viewer?collection_id=%d" % self.card_photo.id)

    def test_chat_in_a_new_tab_from_the_receipt(self):
        action = self.m_photo.action_tab_chat()
        self.assertEqual((action["type"], action["target"]), ("ir.actions.act_url", "new"))
        self.assertEqual(action["url"],
                         "/odoo/action-distri_chat_viewer?message_id=%d" % self.m_photo.id)

    def test_open_from_cards(self):
        self.assertEqual(self.card_photo.action_open_chat()["params"],
                         {"collection_id": self.card_photo.id})
        data = self.Viewer.distri_chat_open(collection_id=self.card_photo.id)
        self.assertEqual(data["anchor"], "m%d" % self.m_photo.id)
        data = self.Viewer.distri_chat_open(collection_id=self.card_denis.id)
        self.assertEqual(data["anchor"], "d%d" % self.denis_file.id)
        denis = next(e for e in data["entries"] if e["key"] == data["anchor"])
        self.assertEqual((denis["source_tag"], denis["sender"], denis["pos_known"]),
                         ("Denis", "Denis Prueba", False))
        data = self.Viewer.distri_chat_open(collection_id=self.card_none.id)
        self.assertFalse(data["anchor"])
        self.assertIn("no tiene comprobante", data["note"])

    def test_hidden_duplicate_opens_at_its_time(self):
        data = self.Viewer.distri_chat_open(message_id=self.o_note_dup.id)
        self.assertFalse(data["anchor"])
        self.assertIn("otro export", data["note"])

    def test_pages_never_split_a_minute_nor_repeat(self):
        first = self.Viewer.distri_chat_open(message_id=self.m_shared_photo.id)
        before = self.Viewer.distri_chat_more("before", first["first"])
        seen = [e["key"] for e in before["entries"] + first["entries"]]
        self.assertEqual(len(seen), len(set(seen)))
        self.assertLessEqual(before["entries"][-1]["ts"] if before["entries"] else "",
                             first["entries"][0]["ts"])

    def test_search(self):
        hits = self.Viewer.distri_chat_search("Kiosco Prueba Uno")["hits"]
        self.assertEqual([h["key"] for h in hits], ["m%d" % self.m_note.id])
        self.m_photo.with_context(distri_receipt_matching=True).write({"distri_amount": 123456})
        hits = self.Viewer.distri_chat_search("$ 123.456")["hits"]
        self.assertIn("m%d" % self.m_photo.id, [h["key"] for h in hits])
        hits = self.Viewer.distri_chat_search("Denis Prueba")["hits"]
        self.assertIn("d%d" % self.denis_file.id, [h["key"] for h in hits])
        jump = self.Viewer.distri_chat_search("9/5/2031 10:05")
        self.assertEqual(jump["jump"], "2031-05-09 13:05:00")

    def test_search_counts_every_match_and_pages(self):
        long_tail = "nota " * 60
        for minute in range(3):
            self._text(self.manager, DAY_BEFORE + timedelta(minutes=10 + minute),
                       long_tail + "Zxqvisor Prueba %d" % minute, pos=1)
        first = self.Viewer.distri_chat_search("zxqvisor", limit=2)
        self.assertEqual((first["total"], len(first["hits"]), first["more"]), (3, 2, True))
        last = self.Viewer.distri_chat_search("zxqvisor", offset=2, limit=2)
        self.assertEqual((last["total"], len(last["hits"]), last["more"]), (3, 1, False))
        hits = first["hits"] + last["hits"]
        # newest first, none repeated, the snippet shows the match
        self.assertEqual([h["snippet"].rsplit(" ", 1)[-1] for h in hits], ["2", "1", "0"])
        self.assertTrue(all(h["snippet"].startswith("…") and "Zxqvisor" in h["snippet"]
                            for h in hits))
        self.assertEqual((hits[0]["source_tag"], hits[0]["day"], hits[0]["time"]),
                         ("gerente", "2031-05-09", "10:17"))

    def test_search_hit_of_a_receipt_has_its_card_and_date(self):
        partner = self.env["res.partner"].create({"name": "Cliente Visor Prueba"})
        self.card_photo.sudo().write({"partner_id": partner.id})
        self.m_photo.with_context(distri_receipt_matching=True).write(
            {"distri_amount": 123456, "distri_payer_name": "Pagador Visor Prueba"})
        hits = self.Viewer.distri_chat_search("Pagador Visor")["hits"]
        hit = next(h for h in hits if h["key"] == "m%d" % self.m_photo.id)
        self.assertEqual((hit["collection_id"], hit["card_partner"], hit["card_amount"],
                          hit["payer"], hit["amount"]),
                         (self.card_photo.id, "Cliente Visor Prueba", 123456,
                          "Pagador Visor Prueba", 123456))
        # no date read from the photo: the card's transfer date
        self.assertEqual((hit["receipt_date_label"], hit["receipt_date_source"],
                          hit["receipt_far"]), ("09/05/2031 10:05", "card", False))

    def test_receipt_date_on_the_bubbles(self):
        self.m_photo.with_context(distri_receipt_matching=True).write(
            {"distri_receipt_date": to_utc(datetime(2031, 5, 6, 22, 47))})
        data = self.Viewer.distri_chat_open(message_id=self.m_photo.id)
        by_key = {e["key"]: e for e in data["entries"]}
        photo = by_key["m%d" % self.m_photo.id]
        self.assertEqual((photo["receipt_date_label"], photo["receipt_date_source"],
                          photo["receipt_far"]), ("06/05/2031 22:47", "read", True))
        self.assertEqual(photo["collection_id"], self.card_photo.id)
        # Denis's file on its card: that card's transfer date
        denis = by_key["d%d" % self.denis_file.id]
        self.assertEqual((denis["receipt_date_label"], denis["receipt_date_source"]),
                         ("09/05/2031 10:08", "card"))
        # a text has none
        note = by_key["m%d" % self.m_note.id]
        self.assertEqual((note["receipt_date_label"], note["receipt_far"]), ("", False))

    # ------------------------------------------------------------ importer

    def test_import_keeps_owner_and_order_inside_the_minute(self):
        text = ("9/05/2031, 11:00 - Vendedor Prueba: <Multimedia omitido>\n"
                "9/05/2031, 11:00 - Vendedor Prueba: Kiosco Prueba Tres\n")
        out = io.BytesIO()
        with zipfile.ZipFile(out, "w") as archive:
            archive.writestr("Chat de WhatsApp con Prueba.txt", text)
        record = self.env["distri.whatsapp.chat.import"]._distri_import(
            "visor.zip", date(2031, 5, 9), date(2031, 5, 9), data=out.getvalue(),
            owner="manager")
        self.assertEqual(record.distri_export_owner, "manager")
        note = record.message_ids
        self.assertEqual((note.body, note.distri_chat_minute_pos), ("Kiosco Prueba Tres", 2))
        # the reorder script for imports made before 19.0.1.46.0
        note.write({"distri_chat_minute_pos": 0})
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "visor.zip")
            with open(path, "wb") as handle:
                handle.write(out.getvalue())
            result = record._distri_backfill_minute_pos(path)
        self.assertEqual(result["updated"], 1)
        self.assertEqual(note.distri_chat_minute_pos, 2)

    # ------------------------------------------------------------ access

    def test_only_staff_open_it(self):
        seller = new_test_user(self.env, login="vendedor_visor",
                               groups="distri_pedido.grupo_vendedor")
        portal = new_test_user(self.env, login="cliente_visor", groups="base.group_portal")
        for user in (seller, portal):
            viewer = self.Viewer.with_user(user)
            with self.assertRaises(AccessError):
                viewer.distri_chat_open(message_id=self.m_photo.id)
            with self.assertRaises(AccessError):
                viewer.distri_chat_more("before", "2031-05-10 00:00:00")
            with self.assertRaises(AccessError):
                viewer.distri_chat_search("Kiosco")
        system = new_test_user(self.env, login="sistema_visor", groups="base.group_system")
        self.assertTrue(self.Viewer.with_user(system).distri_chat_open(
            message_id=self.m_photo.id)["entries"])

    # ------------------------------------------------------------ pending (19.0.1.92.0)

    def test_pending_column_lists_the_open_cards_of_the_chat(self):
        """«Pendientes de aprobar»: receipts of this chat whose card is still
        «Pendiente» or «Autorizado para entregar», of the last PENDING_DAYS,
        newest first; a resolved card, an old one or a card without a
        receipt in the chat is not listed."""
        now_local = datetime.now(AR_TZ).replace(tzinfo=None, microsecond=0)
        partner = self.env["res.partner"].create({"name": "Kiosco Pendiente Prueba"})

        def card_on(local, raw, state="draft", **vals):
            msg = self._file(self.own, local, raw, sender="Pagador Pendiente")
            card = self._card("97000001%04d" % self._next(), local, receipt=msg, **vals)
            card.sudo().write(dict({"state": state}, **({"partner_id": partner.id}
                                                       if state == "draft" else {})))
            return card

        draft = card_on(now_local - timedelta(hours=3), b"pend-draft", amount=4321)
        authorized = card_on(now_local - timedelta(hours=1), b"pend-auth", state="authorized")
        confirmed = card_on(now_local - timedelta(hours=2), b"pend-ok", state="confirmed")
        ignored = card_on(now_local - timedelta(hours=2), b"pend-ignored", state="ignored")
        old = card_on(now_local - timedelta(days=PENDING_DAYS + 5), b"pend-old")

        data = self.Viewer.distri_chat_pending()
        ids = [item["collection_id"] for item in data["items"]]
        self.assertEqual(data["days"], PENDING_DAYS)
        self.assertEqual(data["count"], len(ids))
        self.assertIn(draft.id, ids)
        self.assertIn(authorized.id, ids)
        self.assertLess(ids.index(authorized.id), ids.index(draft.id), "newest first")
        for card in (confirmed, ignored, old, self.card_none, self.card_denis):
            self.assertNotIn(card.id, ids)
        item = next(i for i in data["items"] if i["collection_id"] == draft.id)
        self.assertEqual(item["key"], "m%d" % draft.receipt_id.id)
        self.assertEqual(item["message_id"], draft.receipt_id.id)
        self.assertEqual((item["sender"], item["amount"], item["partner"]),
                         ("Pagador Pendiente", 4321, "Kiosco Pendiente Prueba"))
        self.assertEqual(item["state_label"], "Pendiente")
        # clicking it opens the chat centered on that receipt (the viewer's jump)
        self.assertEqual(self.Viewer.distri_chat_open(message_id=item["message_id"])["anchor"],
                         item["key"])
        # after an action on the card: its state is read again
        draft.sudo().write({"state": "confirmed"})
        self.assertEqual(self.Viewer.distri_chat_card_states([draft.id, authorized.id]),
                         {draft.id: "confirmed", authorized.id: "authorized"})
        self.assertNotIn(draft.id, [i["collection_id"]
                                    for i in self.Viewer.distri_chat_pending()["items"]])

    def test_pending_column_is_for_the_chat_viewers_only(self):
        seller = new_test_user(self.env, login="vendedor_pendientes",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(AccessError):
            self.Viewer.with_user(seller).distri_chat_pending()
        with self.assertRaises(AccessError):
            self.Viewer.with_user(seller).distri_chat_card_states([self.card_photo.id])
