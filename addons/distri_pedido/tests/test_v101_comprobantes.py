"""19.0.1.101.0: a file refused at «Subir comprobante» because it was already
loaded is kept as an attempt, listed in «Subidos hoy por vos» and «Mis
comprobantes», and opens the existing receipt (few facts, read only) for the
user who tried. Synthetic data only: no real customer or receipt."""
import hashlib
import io
import os
import re

from PIL import Image

from odoo.tests import HttpCase, new_test_user, tagged

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
FETCH = {"X-Distri-Fetch": "1"}
FLAG = "No se cargó · ya estaba en el sistema"


def _png(color, size=(40, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@tagged("post_install", "-at_install", "distri_v101", "distri_v101_comprobantes")
class TestComprobantesV101(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = new_test_user(cls.env, login="vendedor_comp_v101", name="Vendedor Comp V101",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.other = new_test_user(cls.env, login="otro_comp_v101", name="Otro Comp V101",
                                  groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.admin = new_test_user(cls.env, login="admin_comp_v101", name="Admin Comp V101",
                                  groups="base.group_user,distri_pedido.grupo_administracion")
        cls.customer = cls.env["res.partner"].create({"name": "Kiosco Comprobante V101",
                                                      "company_type": "company"})
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Attempt = cls.env["distri.receipt.upload.attempt"].sudo()

    def _login(self, user):
        self.authenticate(user.login, user.login)
        res = self.url_open("/comprobantes")
        self.assertEqual(res.status_code, 200, res.text[:300])
        return CSRF.search(res.text).group(1)

    def _upload(self, files, csrf, amounts=None, **data):
        # 19.0.1.126.1: the customer is mandatory; these uploads stand for
        # «No está en el sistema» when the test picks none
        if "cliente_id" not in data:
            data = dict(data, no_en_sistema="1", remitente="Cliente de la prueba")
        form = [("csrf_token", csrf)] + list(data.items())
        form += [("importes", a) for a in (amounts or [])]
        res = self.url_open("/comprobantes/subir", data=form,
                            files=[("archivos", f) for f in files], headers=FETCH)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.json()

    def _first_upload(self, photo, user=None):
        csrf = self._login(user or self.seller)
        data = self._upload([("original.png", photo, "image/png")], csrf,
                            cliente_id=str(self.customer.id))
        self.assertEqual(data["results"][0]["status"], "nuevo")
        return self.Message.browse(data["results"][0]["id"])

    # ------------------------------------------------------------ recorded

    def test_refused_duplicate_is_recorded_without_the_file(self):
        photo = _png((1, 101, 0))
        msg = self._first_upload(photo)
        checksum = hashlib.sha1(photo).hexdigest()
        files_before = self.env["ir.attachment"].sudo().search_count([("checksum", "=", checksum)])
        messages_before = self.Message.search_count([("distri_upload", "=", True)])
        csrf = self._login(self.seller)
        data = self._upload([("otra-vez.png", photo, "image/png")], csrf, amounts=["1.500"])
        result = data["results"][0]
        self.assertEqual(result["status"], "repetido")
        self.assertIn("Ya estaba cargado", result["message"])
        attempt = self.Attempt.browse(result["attempt_id"])
        self.assertEqual(attempt.user_id, self.seller)
        self.assertEqual(attempt.message_id, msg)
        self.assertEqual(attempt.file_name, "otra-vez.png")
        self.assertEqual(attempt.via, "pantalla")
        self.assertEqual(attempt.amount_typed, 1500.0)
        self.assertTrue(attempt.attempted_at)
        # nothing created, the file not stored again
        self.assertEqual(self.Message.search_count([("distri_upload", "=", True)]), messages_before)
        self.assertEqual(self.env["ir.attachment"].sudo().search_count(
            [("checksum", "=", checksum)]), files_before)
        # «uploads» as before; the attempt apart
        self.assertEqual([u["id"] for u in data["uploads"]], [msg.id])
        self.assertEqual([a["id"] for a in data["attempts"]], [attempt.id])
        # administración: the count on the receipt form and the list
        msg.invalidate_recordset()
        self.assertEqual(msg.with_user(self.admin).distri_upload_attempt_count, 1)
        action = msg.with_user(self.admin).action_distri_view_upload_attempts()
        self.assertEqual(action["res_model"], "distri.receipt.upload.attempt")
        self.assertEqual(self.Attempt.with_user(self.admin).search(action["domain"]), attempt)

    def test_shared_duplicate_is_recorded_as_shared(self):
        photo = _png((2, 101, 0))
        msg = self._first_upload(photo)
        res = self.url_open("/comprobantes/compartir",
                            files=[("archivos", ("wa.png", photo, "image/png"))],
                            allow_redirects=False)
        token = res.headers["Location"].split("compartido=")[1]
        page = self.url_open(res.headers["Location"])
        csrf = CSRF.search(page.text).group(1)
        data = self.url_open("/comprobantes/subir", headers=FETCH, data=[
            ("csrf_token", csrf), ("compartido", token), ("no_en_sistema", "1"),
            ("remitente", "Prueba")]).json()
        attempt = self.Attempt.browse(data["results"][0]["attempt_id"])
        self.assertEqual((attempt.via, attempt.message_id), ("compartido", msg))

    def test_new_uploads_unchanged(self):
        csrf = self._login(self.seller)
        before = self.Attempt.search_count([])
        data = self._upload([("a.png", _png((3, 101, 0)), "image/png"),
                             ("b.png", _png((4, 101, 0)), "image/png")], csrf)
        self.assertEqual([r["status"] for r in data["results"]], ["nuevo", "nuevo"])
        self.assertNotIn("attempt_id", data["results"][0])
        self.assertEqual(len(data["uploads"]), 2)
        self.assertEqual(data["attempts"], [])
        self.assertEqual(self.Attempt.search_count([]), before)

    # ------------------------------------------------------------ listed

    def test_listed_today_and_in_mine(self):
        photo = _png((5, 101, 0))
        msg = self._first_upload(photo)
        csrf = self._login(self.seller)
        attempt = self.Attempt.browse(self._upload(
            [("repetido.png", photo, "image/png")], csrf)["results"][0]["attempt_id"])
        page = self.url_open("/comprobantes")
        self.assertIn('data-comp-api="126"', page.text)  # 19.0.1.126.1
        self.assertIn('href="/comprobantes/intento/%d"' % attempt.id, page.text)
        self.assertIn(FLAG, page.text)
        self.assertIn("Subido por vos", page.text)
        # the count is the one of the receipts uploaded
        self.assertIn('Subidos hoy por vos: <span data-comp-count="">1</span>', page.text)
        today = self.url_open("/comprobantes/hoy").json()
        self.assertEqual([u["id"] for u in today["uploads"]], [msg.id])
        row = today["attempts"][0]
        self.assertEqual((row["id"], row["flag"], row["bucket_label"]),
                         (attempt.id, FLAG, "Pendiente"))
        self.assertEqual(row["partner"], self.customer.name)
        self.assertEqual(row["existing_channel"], "Subido por vos")
        # «Mis comprobantes»: among all, and alone with its filter
        link = "/comprobantes/intento/%d?desde=mios" % attempt.id
        receipt_link = "/comprobantes/%d?desde=mios" % msg.id
        mine = self.url_open("/comprobantes/mios").text
        self.assertIn(link, mine)
        self.assertIn(receipt_link, mine)
        self.assertIn("Ya estaban cargados", mine)
        only = self.url_open("/comprobantes/mios?estado=repetido").text
        self.assertIn(link, only)
        self.assertNotIn(receipt_link, only)
        pending = self.url_open("/comprobantes/mios?estado=pendiente").text
        self.assertNotIn(link, pending)
        self.assertIn(receipt_link, pending)
        # another user does not see it listed
        self._login(self.other)
        self.assertNotIn("/comprobantes/intento/%d" % attempt.id,
                         self.url_open("/comprobantes/mios").text)
        self.assertEqual(self.url_open("/comprobantes/hoy").json()["attempts"], [])

    # ------------------------------------------------------------ opened

    def test_attempt_opens_the_existing_receipt_for_its_user_only(self):
        photo = _png((6, 101, 0))
        msg = self._first_upload(photo)
        msg.write({"distri_receipt_note": "Nota interna de otro cliente"})
        # another seller tries the same file
        csrf = self._login(self.other)
        attempt = self.Attempt.browse(self._upload(
            [("mio.png", photo, "image/png")], csrf, amounts=["2.000"])["results"][0]["attempt_id"])
        self.assertEqual(attempt.user_id, self.other)
        res = self.url_open("/comprobantes/intento/%d" % attempt.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn(FLAG, res.text)
        self.assertIn(self.customer.name, res.text)
        self.assertIn("Subido por Vendedor Comp V101", res.text)
        self.assertIn("Pendiente", res.text)
        self.assertIn("$ 2.000", res.text, "the amount the user typed")
        self.assertIn("mio.png", res.text)
        self.assertIn("/comprobantes/intento/%d/archivo" % attempt.id, res.text)
        for hidden in ("Nota interna", "original.png", "Aprobar", "Descartar", "Borrar",
                       "Eliminar"):
            self.assertNotIn(hidden, res.text)
        file_res = self.url_open("/comprobantes/intento/%d/archivo" % attempt.id)
        self.assertEqual(file_res.status_code, 200)
        self.assertEqual(file_res.content, photo)
        # the existing receipt itself keeps its rules
        self.assertEqual(self.url_open("/comprobantes/%d" % msg.id).status_code, 404)
        self.assertEqual(self.url_open("/comprobantes/%d/archivo" % msg.id).status_code, 404)
        self.assertEqual(self.url_open("/comprobantes/intento/%d" % attempt.id,
                                       data={"csrf_token": csrf}).status_code, 405)
        # the attempt belongs to its user: not the uploader, not administración
        for user in (self.seller, self.admin):
            self._login(user)
            self.assertEqual(self.url_open("/comprobantes/intento/%d" % attempt.id).status_code,
                             404, user.login)
            self.assertEqual(self.url_open(
                "/comprobantes/intento/%d/archivo" % attempt.id).status_code, 404, user.login)
        self.assertEqual(self.url_open("/comprobantes/intento/999999").status_code, 404)
        # logged out
        self.authenticate(None, None)
        res = self.url_open("/comprobantes/intento/%d" % attempt.id, allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))

    def test_channel_of_the_existing_receipt(self):
        raw = _png((7, 101, 0))
        whatsapp = self.Message.create({
            "wa_message_id": "wamid.v101.test", "direction": "in", "message_type": "image",
            "media_mime_type": "image/png", "body": "foto"})
        whatsapp.attachment_id = self.env["ir.attachment"].sudo().create({
            "name": "wa.png", "raw": raw, "mimetype": "image/png",
            "res_model": whatsapp._name, "res_id": whatsapp.id})
        csrf = self._login(self.seller)
        attempt = self.Attempt.browse(self._upload(
            [("de-wa.png", raw, "image/png")], csrf)["results"][0]["attempt_id"])
        self.assertEqual(attempt.message_id, whatsapp)
        res = self.url_open("/comprobantes/intento/%d" % attempt.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Por WhatsApp", res.text)
        # the receipt did not come from «Subir comprobante»: still not open by its id
        self.assertEqual(self.url_open("/comprobantes/%d" % whatsapp.id).status_code, 404)
        chat = self.Message.create({"wa_message_id": "chat:v101test", "direction": "in",
                                    "message_type": "image", "body": "x"})
        app = self.Message.create({"wa_message_id": "app:v101test", "direction": "in",
                                   "message_type": "image", "body": "x",
                                   "distri_from_app": True})
        self.assertEqual(chat._distri_upload_channel(), "Exportación del chat de WhatsApp")
        self.assertEqual(app._distri_upload_channel(), "App del repartidor")

    def test_static_file_gates_the_attempt_rows(self):
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src",
                            "comprobantes", "comprobantes.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        # a data-comp-api="100" page (the v100 server) keeps drawing only «uploads»
        self.assertIn("v101 = api >= 101", source)
        self.assertIn("v101 && attempts", source)
        self.assertIn("v100 = api >= 100", source)
