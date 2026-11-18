"""19.0.1.100.0: «Subir comprobante» with the amount typed by the uploader,
upright photos («Girar», EXIF, turning a stored photo) and «Ver comprobante»
/ «Mis comprobantes». Synthetic data only: no real customer or receipt."""
import hashlib
import io
import os
import re
from unittest.mock import patch

from PIL import Image

from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.distri_pedido.models.comprobante_subir import file_key, rotation, upright_image
from odoo.addons.distri_pedido.models.visor_chat import _dedup_key

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
FETCH = {"X-Distri-Fetch": "1"}


def _png(color, size=(40, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


def _jpeg_with_orientation(size=(40, 30), orientation=6):
    image = Image.new("RGB", size, (120, 30, 30))
    exif = Image.Exif()
    exif[0x0112] = orientation
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", exif=exif.tobytes())
    return buffer.getvalue()


def _size(raw):
    return Image.open(io.BytesIO(raw)).size


@tagged("post_install", "-at_install", "distri_v100", "distri_v100_comprobantes")
class TestComprobantesV100(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = new_test_user(cls.env, login="vendedor_comp_v100", name="Vendedor Comp V100",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.other = new_test_user(cls.env, login="otro_comp_v100", name="Otro Comp V100",
                                  groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.admin = new_test_user(cls.env, login="admin_comp_v100", name="Admin Comp V100",
                                  groups="base.group_user,distri_pedido.grupo_administracion")
        cls.Message = cls.env["distri.whatsapp.message"].sudo()

    # ------------------------------------------------------------ helpers

    def _csrf(self, url="/comprobantes"):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res, CSRF.search(res.text).group(1)

    def _upload(self, files, csrf, amounts=None, turns=None, **data):
        form = [("csrf_token", csrf)] + list(data.items())
        form += [("importes", a) for a in (amounts or [])]
        form += [("giros", t) for t in (turns or [])]
        return self.url_open("/comprobantes/subir", data=form,
                             files=[("archivos", f) for f in files], headers=FETCH)

    def _reading(self, reading):
        """The file reading (OCR / PDF text) answers `reading`."""
        return patch.object(type(self.Message), "_distri_file_reading",
                            lambda msg: ("texto leído", dict(reading)))

    def _read(self, msgs, reading):
        with self._reading(reading):
            self.env["distri.whatsapp.message"]._distri_read_app_receipts()
        msgs.invalidate_recordset()

    def _created(self, res):
        data = res.json()
        return self.Message.browse([r["id"] for r in data["results"] if r["status"] == "nuevo"])

    # ------------------------------------------------------------ typed amount

    def test_typed_amount_saved_and_shown_at_once(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        page, csrf = self._csrf()
        self.assertIn('data-comp-api="100"', page.text)
        res = self._upload([("a.png", _png((10, 0, 0)), "image/png"),
                            ("b.png", _png((0, 10, 0)), "image/png")], csrf,
                           amounts=["150.000", "150000,50"])
        self.assertEqual(res.status_code, 200, res.text[:300])
        a, b = self._created(res)
        self.assertEqual(a.distri_amount_typed, 150000.0)
        self.assertEqual(b.distri_amount_typed, 150000.5)
        # apart from the receipt amount until the file is read
        self.assertFalse(a.distri_amount)
        rows = {r["id"]: r for r in res.json()["uploads"]}
        self.assertEqual(rows[a.id]["amount_typed"], 150000.0)
        self.assertTrue(rows[a.id]["reading"])
        page = self.url_open("/comprobantes")
        self.assertIn("$ 150.000,50", page.text)
        self.assertIn("Importe cargado por vos · leyendo el comprobante…", page.text)

    def test_typed_amount_used_when_the_reading_has_none(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        msg = self._created(self._upload([("a.png", _png((20, 0, 0)), "image/png")], csrf,
                                         amounts=["82.200"]))
        self._read(msg, {})
        self.assertEqual(msg.media_state, "done")
        self.assertEqual(msg.distri_amount, 82200.0)
        self.assertEqual(msg.distri_amount_confidence, "low", "one source only: a person confirms it")
        self.assertTrue(msg.distri_amount_review)
        self.assertFalse(msg.distri_amount_typed_differs)
        # the file could not be read at all: the typed amount too
        msg2 = self._created(self._upload([("b.png", _png((21, 0, 0)), "image/png")], csrf,
                                          amounts=["5000"]))

        def broken(_msg):
            raise ValueError("unreadable")
        with patch.object(type(self.Message), "_distri_file_reading", broken), \
                mute_logger("odoo.addons.distri_pedido.models.comprobante_subir"):
            self.env["distri.whatsapp.message"]._distri_read_app_receipts()
        msg2.invalidate_recordset()
        self.assertEqual(msg2.media_state, "error")
        self.assertEqual(msg2.distri_amount, 5000.0)

    def test_same_amount_read_is_sure(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        msg = self._created(self._upload([("a.png", _png((30, 0, 0)), "image/png")], csrf,
                                         amounts=["1.500"]))
        self._read(msg, {"amount": 1500.0, "amount_confidence": "low"})
        self.assertEqual(msg.distri_amount, 1500.0)
        self.assertEqual(msg.distri_amount_confidence, "high")
        self.assertFalse(msg.distri_amount_typed_note)

    def test_mismatch_keeps_the_read_amount_and_is_flagged(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        msg = self._created(self._upload([("a.png", _png((40, 0, 0)), "image/png")], csrf,
                                         amounts=["150.000"]))
        self._read(msg, {"amount": 200000.0, "amount_confidence": "high"})
        self.assertEqual(msg.distri_amount, 200000.0, "the read amount stays")
        self.assertEqual(msg.distri_amount_typed, 150000.0, "the reading never writes the typed one")
        self.assertEqual(msg.distri_amount_confidence, "low")
        self.assertTrue(msg.distri_amount_review)
        self.assertTrue(msg.distri_amount_typed_differs)
        self.assertEqual(msg.distri_amount_typed_note, "Cargado: $ 150.000 · leído: $ 200.000")
        self.assertIn(msg, self.Message.search([("distri_amount_typed_differs", "=", True)]))
        card = msg.distri_collection_id or self.env["distri.mp.collection"].sudo().search(
            [("receipt_id", "=", msg.id)], limit=1)
        self.assertTrue(card, "the receipt has its card on the board")
        self.assertEqual(card.receipt_amount_typed_note, "Cargado: $ 150.000 · leído: $ 200.000")
        # «Subidos hoy por vos» tells the uploader
        row = self.url_open("/comprobantes/hoy").json()["uploads"][0]
        self.assertTrue(row["amount_differs"])
        # reading again changes neither
        with self._reading({"amount": 200000.0, "amount_confidence": "high"}):
            msg.action_distri_read_again()
        self.assertEqual(msg.distri_amount, 200000.0)
        self.assertEqual(msg.distri_amount_typed, 150000.0)
        # administración types the amount: settled
        msg.write({"distri_amount": 150000.0})
        self.assertEqual(msg.distri_amount_confidence, "manual")
        self.assertFalse(msg.distri_amount_typed_differs)
        with self._reading({"amount": 200000.0, "amount_confidence": "high"}):
            msg.action_distri_read_again()
        self.assertEqual(msg.distri_amount, 150000.0, "a typed amount by administración wins")

    def test_bad_amount_is_refused_politely(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        before = self.Message.search_count([("distri_upload", "=", True)])
        for bad in ("1.5", "abc", "-30", "0"):
            res = self._upload([("a.png", _png((50, 0, 0)), "image/png"),
                                ("b.png", _png((51, 0, 0)), "image/png")], csrf,
                               amounts=["100", bad])
            self.assertEqual(res.status_code, 400, bad)
            self.assertIn("Revisá el importe de «b.png»", res.json()["error"])
        self.assertEqual(self.Message.search_count([("distri_upload", "=", True)]), before,
                         "nothing created")
        # without JavaScript: the page with the message
        res = self.url_open("/comprobantes/subir", data=[("csrf_token", csrf), ("importes", "x")],
                            files=[("archivos", ("c.png", _png((52, 0, 0)), "image/png"))])
        self.assertEqual(res.status_code, 400)
        self.assertIn("Revisá el importe de «c.png»", res.text)

    def test_upload_without_amount_as_in_v99(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        # the v99 form: no «importes» nor «giros» at all
        photo = _png((60, 0, 0))
        msg = self._created(self._upload([("a.png", photo, "image/png")], csrf))
        self.assertFalse(msg.distri_amount_typed)
        self.assertEqual(msg.attachment_id.raw, photo, "same bytes stored")
        self._read(msg, {})
        self.assertFalse(msg.distri_amount)
        self.assertEqual(msg.distri_amount_confidence, "none")
        self.assertFalse(msg.distri_amount_typed_note)
        # an empty amount is no amount
        msg2 = self._created(self._upload([("b.png", _png((61, 0, 0)), "image/png")], csrf,
                                          amounts=[" "]))
        self.assertFalse(msg2.distri_amount_typed)

    def test_shared_files_carry_amount_and_turn(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        res = self.url_open("/comprobantes/compartir",
                            files=[("archivos", ("wa.png", _png((70, 0, 0)), "image/png"))],
                            allow_redirects=False)
        target = res.headers["Location"]
        token = target.split("compartido=")[1]
        page, csrf = self._csrf(target)
        self.assertIn('name="importe_compartido"', page.text)
        self.assertIn('name="giro_compartido"', page.text)
        res = self.url_open("/comprobantes/subir", headers=FETCH, data=[
            ("csrf_token", csrf), ("compartido", token), ("importe_compartido", "9.999,90"),
            ("giro_compartido", "90")])
        msg = self._created(res)
        self.assertEqual(msg.distri_amount_typed, 9999.9)
        self.assertEqual(_size(msg.attachment_id.raw), (30, 40))

    # ------------------------------------------------------------ upright photos

    def test_turned_photo_is_stored_turned_and_still_a_duplicate(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        photo = _png((80, 0, 0), size=(40, 30))
        msg = self._created(self._upload([("a.png", photo, "image/png")], csrf, turns=["90"]))
        self.assertEqual(_size(msg.attachment_id.raw), (30, 40))
        self.assertEqual(msg.wa_message_id, "upload:%s" % file_key(photo), "key of the original")
        res = self._upload([("otra.png", photo, "image/png")], csrf, turns=["0"])
        self.assertEqual(res.json()["results"][0]["status"], "repetido")
        # a «Girar» value that is not a quarter turn is ignored
        photo2 = _png((81, 0, 0), size=(40, 30))
        msg2 = self._created(self._upload([("b.png", photo2, "image/png")], csrf, turns=["45"]))
        self.assertEqual(msg2.attachment_id.raw, photo2)

    def test_exif_orientation_applied(self):
        raw = _jpeg_with_orientation(size=(40, 30), orientation=6)
        upright = upright_image(raw, "image/jpeg")
        self.assertEqual(_size(upright), (30, 40))
        self.assertNotIn(0x0112, Image.open(io.BytesIO(upright)).getexif())
        # EXIF first, then the user's turn
        self.assertEqual(_size(upright_image(raw, "image/jpeg", 90)), (40, 30))
        # nothing to do: the same bytes; a PDF untouched
        plain = _png((1, 1, 1))
        self.assertIs(upright_image(plain, "image/png"), plain)
        self.assertEqual(upright_image(b"%PDF-1.4", "application/pdf", 90), b"%PDF-1.4")
        self.assertEqual(upright_image(b"not an image", "image/png", 90), b"not an image")
        self.assertEqual([rotation(v) for v in ("90", "180", 270, "x", None, "-90")],
                         [90, 180, 270, 0, 0, 0])

    def test_administracion_turns_a_stored_photo(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        msg = self._created(self._upload([("a.png", _png((90, 0, 0), size=(40, 30)), "image/png")],
                                         csrf))
        calls = []

        def reading(m):
            calls.append(m.id)
            return "", {}
        with patch.object(type(self.Message), "_distri_file_reading", reading):
            msg.with_user(self.admin).action_distri_rotate_right()
        self.assertEqual(_size(msg.attachment_id.raw), (30, 40))
        self.assertEqual(calls, [msg.id], "read again after turning")
        with self.assertRaises(AccessError):
            msg.with_user(self.seller).action_distri_rotate_left()

    # ------------------------------------------------------------ «Ver comprobante»

    def test_uploader_sees_own_receipt_read_only(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        photo = _png((100, 0, 0))
        msg = self._created(self._upload([("ver.png", photo, "image/png")], csrf,
                                         amounts=["12.345"]))
        page = self.url_open("/comprobantes")
        self.assertIn('href="/comprobantes/%d"' % msg.id, page.text)
        self.assertIn('href="/comprobantes/mios"', page.text)
        res = self.url_open("/comprobantes/%d" % msg.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn("$ 12.345", res.text)
        self.assertIn("Pendiente", res.text)
        self.assertIn("/comprobantes/%d/archivo" % msg.id, res.text)
        for word in ("Aprobar", "Descartar", "Borrar", "Eliminar"):
            self.assertNotIn(word, res.text)
        file_res = self.url_open("/comprobantes/%d/archivo" % msg.id)
        self.assertEqual(file_res.status_code, 200)
        self.assertEqual(file_res.content, photo)
        self.assertEqual(file_res.headers["Content-Type"], "image/png")
        # administración rejects it, with a note: the uploader sees both
        msg.action_distri_dismiss_receipt()
        msg.write({"distri_receipt_note": "Es de otro cliente"})
        res = self.url_open("/comprobantes/%d?desde=mios" % msg.id)
        self.assertIn("Rechazado", res.text)
        self.assertIn("Es de otro cliente", res.text)
        self.assertIn('data-parent="/comprobantes/mios"', res.text)
        # «Mis comprobantes», by state
        mine = self.url_open("/comprobantes/mios")
        self.assertEqual(mine.status_code, 200)
        self.assertIn("/comprobantes/%d?desde=mios" % msg.id, mine.text)
        self.assertIn("/comprobantes/%d?desde=mios" % msg.id,
                      self.url_open("/comprobantes/mios?estado=rechazado").text)
        self.assertNotIn("/comprobantes/%d?desde=mios" % msg.id,
                         self.url_open("/comprobantes/mios?estado=pendiente").text)
        # the uploader cannot approve or delete through the routes
        self.assertEqual(self.url_open("/comprobantes/%d" % msg.id,
                                       data={"csrf_token": csrf}).status_code, 405)

    def test_others_cannot_see_it_administracion_can(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        msg = self._created(self._upload([("priv.png", _png((110, 0, 0)), "image/png")], csrf))
        self.authenticate("otro_comp_v100", "otro_comp_v100")
        self.assertEqual(self.url_open("/comprobantes/%d" % msg.id).status_code, 404)
        self.assertEqual(self.url_open("/comprobantes/%d/archivo" % msg.id).status_code, 404)
        self.assertNotIn("/comprobantes/%d" % msg.id, self.url_open("/comprobantes/mios").text)
        # a receipt that did not come from «Subir comprobante»: never
        whatsapp = self.Message.create({"wa_message_id": "wamid.v100.test", "direction": "in",
                                        "message_type": "text", "body": "hola"})
        self.authenticate("admin_comp_v100", "admin_comp_v100")
        self.assertEqual(self.url_open("/comprobantes/%d" % msg.id).status_code, 200)
        self.assertEqual(self.url_open("/comprobantes/%d/archivo" % msg.id).status_code, 200)
        self.assertEqual(self.url_open("/comprobantes/%d" % whatsapp.id).status_code, 404)

    # ------------------------------------------------------------ original checksum

    def _admin_card_file(self, card, raw, name):
        """A receipt of the manager's export kept on a card (models/visor_chat.py)."""
        attachment = self.env["ir.attachment"].sudo().create({
            "name": name, "raw": raw, "mimetype": "image/png",
            "res_model": "distri.mp.collection", "res_id": card.id})
        card.sudo().message_post(
            body="Comprobante del export del administrador, llegó el 2026-11-18 10:00, lo mandó Denis.",
            attachment_ids=[attachment.id])
        return attachment

    def test_turned_upload_pairs_with_the_original_file(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        photo = _png((120, 0, 0), size=(40, 30))
        original = hashlib.sha1(photo).hexdigest()
        msg = self._created(self._upload([("a.png", photo, "image/png")], csrf, turns=["90"]))
        self.assertNotEqual(msg.attachment_id.checksum, original)
        self.assertEqual(msg.distri_original_checksum, original)
        # the chat viewer: the same key as an export entry of the original bytes
        entry = self.env["distri.chat.viewer"]._message_entries(msg)
        if entry:
            self.assertEqual(_dedup_key(entry[0]), _dedup_key(
                {"kind": "image", "key": "x", "checksum": original}))
        else:
            # without timestamp nothing is listed; the rule itself
            self.assertEqual(_dedup_key({"kind": "image", "key": "m", "checksum": "turned",
                                         "original_checksum": original}), "f:" + original)
        # a file of the manager's export with those bytes is not shown apart
        card = self.env["distri.mp.collection"].sudo().search([("receipt_id", "=", msg.id)],
                                                               limit=1)
        same = self._admin_card_file(card, photo, "export.png")
        other = self._admin_card_file(card, _png((121, 0, 0)), "otro.png")
        listed = {e["attachment_id"] for e in self.env["distri.chat.viewer"]._card_file_entries()}
        self.assertNotIn(same.id, listed)
        self.assertIn(other.id, listed)
        # the upload check by checksum, for a receipt with another key
        whatsapp = self.Message.create({
            "wa_message_id": "wamid.v100.turned", "direction": "in", "message_type": "image",
            "media_mime_type": "image/png", "body": "foto"})
        raw = _png((122, 0, 0), size=(40, 30))
        whatsapp.attachment_id = self.env["ir.attachment"].sudo().create({
            "name": "wa.png", "raw": raw, "mimetype": "image/png",
            "res_model": whatsapp._name, "res_id": whatsapp.id})
        with self._reading({}):
            whatsapp.with_user(self.admin)._distri_rotate_photo(90)
        self.assertEqual(self.Message._distri_upload_duplicate(raw), whatsapp)
        res = self._upload([("de-nuevo.png", raw, "image/png")], csrf)
        self.assertEqual(res.json()["results"][0]["status"], "repetido")

    def test_second_turn_keeps_the_first_original(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        photo = _png((130, 0, 0), size=(40, 30))
        msg = self._created(self._upload([("a.png", photo, "image/png")], csrf))
        self.assertFalse(msg.distri_original_checksum, "not turned: nothing kept")
        self.assertEqual(msg.attachment_id.checksum, hashlib.sha1(photo).hexdigest())
        with self._reading({}):
            msg.with_user(self.admin).action_distri_rotate_right()
            first = msg.distri_original_checksum
            msg.with_user(self.admin).action_distri_rotate_right()
        self.assertEqual(first, hashlib.sha1(photo).hexdigest())
        self.assertEqual(msg.distri_original_checksum, first)
        self.assertEqual(_size(msg.attachment_id.raw), (40, 30), "two quarter turns")
        # turned at upload, then by administración: still the uploaded bytes
        photo2 = _png((131, 0, 0), size=(40, 30))
        msg2 = self._created(self._upload([("b.png", photo2, "image/png")], csrf, turns=["90"]))
        with self._reading({}):
            msg2.with_user(self.admin).action_distri_rotate_left()
        self.assertEqual(msg2.distri_original_checksum, hashlib.sha1(photo2).hexdigest())

    def test_unturned_files_keep_v99_checksums(self):
        self.authenticate("vendedor_comp_v100", "vendedor_comp_v100")
        _page, csrf = self._csrf()
        photo = _png((140, 0, 0))
        msg = self._created(self._upload([("a.png", photo, "image/png")], csrf, turns=["0"]))
        self.assertFalse(msg.distri_original_checksum)
        checksum = hashlib.sha1(photo).hexdigest()
        self.assertEqual(msg.attachment_id.checksum, checksum)
        self.assertEqual(_dedup_key({"kind": "image", "key": "m", "checksum": checksum,
                                     "original_checksum": ""}), "f:" + checksum)
        card = self.env["distri.mp.collection"].sudo().search([("receipt_id", "=", msg.id)],
                                                               limit=1)
        same = self._admin_card_file(card, photo, "export.png")
        listed = {e["attachment_id"] for e in self.env["distri.chat.viewer"]._card_file_entries()}
        self.assertNotIn(same.id, listed, "matched by the attachment checksum, as in v99")

    def test_static_file_stays_compatible_with_v99_pages(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src",
                            "comprobantes", "comprobantes.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        # the new inputs only on a page that says the server takes them
        self.assertIn("data-comp-api", source)
        self.assertIn("u.amount || u.amount_typed", source)
        self.assertIn("u.url", source)
