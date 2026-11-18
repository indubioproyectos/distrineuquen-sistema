"""19.0.1.99.0 (audit 3a steps 1-2, F16): «Subir comprobante» (/comprobantes),
«Compartir con Distri» (share target of the staff app) and PDF receipts from
the driver's app. Synthetic data only: no real customer or receipt."""
import base64
import io
import re
from urllib.parse import parse_qs, unquote, urlparse

from PIL import Image

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.comprobante_subir import receipt_kind
from odoo.addons.distri_pedido.models.gasto import decode_receipt_file
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
FETCH = {"X-Distri-Fetch": "1"}
PDF = (b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n2 0 obj<</Type/Pages/Kids[]/Count 0>>"
       b"endobj\ntrailer<</Root 1 0 R>>\n%%EOF\n")


def _png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (30, 40), color).save(buffer, format="PNG")
    return buffer.getvalue()


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_comprobantes")
class TestSubirComprobante(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = new_test_user(cls.env, login="vendedor_comp_v99", name="Vendedor Comp V99",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.packer = new_test_user(cls.env, login="armador_comp_v99", name="Armador Comp V99",
                                   groups="base.group_user,distri_pedido.grupo_armador")
        cls.customer = cls.env["res.partner"].create({"name": "Kiosco Comprobante V99",
                                                      "company_type": "company"})
        cls.Message = cls.env["distri.whatsapp.message"].sudo()

    def _page(self, url="/comprobantes"):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res, CSRF.search(res.text).group(1)

    def _upload(self, files, csrf, **data):
        return self.url_open("/comprobantes/subir", data=dict(data, csrf_token=csrf),
                             files=[("archivos", f) for f in files], headers=FETCH)

    def test_upload_photos_and_pdf_with_customer(self):
        self.authenticate("vendedor_comp_v99", "vendedor_comp_v99")
        page, csrf = self._page()
        self.assertIn("Sacar foto", page.text)
        self.assertIn("Elegir de galería", page.text)
        self.assertIn('accept="image/*,application/pdf,.pdf"', page.text)
        self.assertIn("Subidos hoy por vos: <span data-comp-count=\"\">0</span>", page.text)
        photo = _png((200, 10, 10))
        res = self._upload([("comp.png", photo, "image/png"), ("comp.pdf", PDF, "application/pdf"),
                            ("nota.txt", b"hola", "text/plain")], csrf,
                           cliente_id=str(self.customer.id))
        self.assertEqual(res.status_code, 200, res.text[:300])
        data = res.json()
        self.assertEqual([r["status"] for r in data["results"]], ["nuevo", "nuevo", "error"])
        self.assertEqual(len(data["uploads"]), 2)
        msgs = self.Message.browse([r["id"] for r in data["results"] if r["status"] == "nuevo"])
        self.assertEqual(sorted(msgs.mapped("message_type")), ["document", "image"])
        for msg in msgs:
            self.assertTrue(msg.distri_upload)
            self.assertTrue(msg.distri_is_receipt)
            self.assertEqual(msg.distri_receipt_state, "pending")
            self.assertEqual(msg.distri_forwarded_by_id, self.seller)
            self.assertEqual(msg.distri_partner_id, self.customer)
            self.assertEqual(msg.distri_partner_source, "Elegido por Vendedor Comp V99 al subirlo")
            self.assertTrue(msg.attachment_id)
        # the same file again, by this or any channel: told at once, not created
        res = self._upload([("otra-vez.png", photo, "image/png")], csrf)
        self.assertEqual(res.json()["results"][0]["status"], "repetido")
        self.assertIn("Ya estaba cargado", res.json()["results"][0]["message"])
        self.assertEqual(self.Message.search_count([("distri_upload", "=", True),
                                                    ("distri_forwarded_by_id", "=", self.seller.id)]), 2)
        # the background reading picks them up
        self.env["distri.whatsapp.message"]._distri_read_app_receipts()
        self.assertTrue(all(m.media_state for m in msgs))
        # «Subidos hoy por vos»
        listing = self.url_open("/comprobantes/hoy").json()
        self.assertEqual(len(listing["uploads"]), 2)

    def test_without_customer_and_without_javascript(self):
        self.authenticate("vendedor_comp_v99", "vendedor_comp_v99")
        _page, csrf = self._page()
        res = self.url_open("/comprobantes/subir", data={"csrf_token": csrf},
                            files=[("archivos", ("sola.png", _png((1, 2, 3)), "image/png"))])
        self.assertEqual(res.status_code, 200)
        self.assertIn("Subido. Lo revisa administración.", res.text)
        msg = self.Message.search([("distri_upload", "=", True)], order="id desc", limit=1)
        self.assertFalse(msg.distri_partner_id)
        # nothing chosen: refused with a message
        res = self.url_open("/comprobantes/subir", data={"csrf_token": csrf}, headers=FETCH)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Elegí al menos una foto", res.json()["error"])

    def test_only_staff_that_handles_collections(self):
        self.authenticate("armador_comp_v99", "armador_comp_v99")
        self.assertEqual(self.url_open("/comprobantes").status_code, 403)
        self.authenticate(None, None)
        res = self.url_open("/comprobantes", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/web/login", res.headers["Location"])

    def test_share_target_logged_out_then_login(self):
        manifest = self.url_open("/tablero/manifest.webmanifest").json()
        share = manifest["share_target"]
        self.assertEqual((share["action"], share["method"], share["enctype"]),
                         ("/comprobantes/compartir", "POST", "multipart/form-data"))
        self.assertEqual(share["params"]["files"][0]["name"], "archivos")
        self.assertIn("application/pdf", share["params"]["files"][0]["accept"])
        # the session expired: the file waits, the login comes back to it
        res = self.url_open("/comprobantes/compartir", data={"titulo": "x"},
                            files=[("archivos", ("wa.png", _png((9, 9, 9)), "image/png"))],
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        location = res.headers["Location"]
        self.assertIn("/web/login", location)
        target = unquote(parse_qs(urlparse(location).query)["redirect"][0])
        token = parse_qs(urlparse(target).query)["compartido"][0]
        stage = self.env["distri.receipt.share"].sudo().search([("token", "=", token)])
        self.assertTrue(stage)
        self.assertFalse(stage.user_id)
        before = self.Message.search_count([("distri_upload", "=", True)])
        self.assertEqual(before, self.Message.search_count([("distri_upload", "=", True)]),
                         "sharing creates nothing until confirmed")
        self.authenticate("vendedor_comp_v99", "vendedor_comp_v99")
        page, csrf = self._page(target)
        self.assertIn("Llegaron con «Compartir con Distri».", page.text)
        self.assertIn('name="compartido"', page.text)
        stage.invalidate_recordset()
        self.assertEqual(stage.user_id, self.seller)
        res = self._upload([], csrf, compartido=token)
        self.assertEqual([r["status"] for r in res.json()["results"]], ["nuevo"])
        stage.invalidate_recordset()
        self.assertFalse(stage.exists(), "the stage is gone once uploaded")
        msg = self.Message.search([("distri_upload", "=", True)], order="id desc", limit=1)
        self.assertIn("(compartido)", msg.body)
        # another user cannot take somebody else's stage
        stage2 = self.env["distri.receipt.share"].sudo()._distri_stage(
            [("x.png", _png((5, 5, 5)))], user=self.packer)
        self.assertFalse(self.env["distri.receipt.share"].with_user(self.seller)._distri_claim(
            stage2.token))

    def test_share_target_logged_in_and_bad_files(self):
        self.authenticate("vendedor_comp_v99", "vendedor_comp_v99")
        res = self.url_open("/comprobantes/compartir",
                            files=[("archivos", ("x.heic", b"\x00\x00\x00 ftypheic", "image/heic"))],
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/comprobantes"), "nothing usable staged")
        res = self.url_open("/comprobantes/compartir",
                            files=[("archivos", ("r.pdf", PDF, "application/pdf"))],
                            allow_redirects=False)
        self.assertIn("/comprobantes?compartido=", res.headers["Location"])

    def test_tile_on_the_home(self):
        self.authenticate("vendedor_comp_v99", "vendedor_comp_v99")
        from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
        titles = [t["title"] for t in staff_tiles(self.env(user=self.seller))]
        self.assertIn("Comprobantes", titles)
        self.assertNotIn("Comprobantes",
                         [t["title"] for t in staff_tiles(self.env(user=self.packer))])

    def test_kinds(self):
        self.assertEqual(receipt_kind(PDF)[0], "application/pdf")
        self.assertEqual(receipt_kind(_png((1, 1, 1)))[1], "image")
        self.assertIsNone(receipt_kind(b"GIF89a"))
        self.assertIsNone(receipt_kind(b""))


@tagged("post_install", "-at_install", "distri_v99", "distri_v99_comprobantes")
class TestDriverPdfReceipt(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.product_a.invoice_policy = "order"

    def test_decode_receipt_file(self):
        url = "data:application/pdf;base64," + base64.b64encode(PDF).decode()
        self.assertEqual(base64.b64decode(decode_receipt_file(url)), PDF)
        png = _png((3, 3, 3))
        self.assertEqual(base64.b64decode(decode_receipt_file(
            "data:image/png;base64," + base64.b64encode(png).decode())), png)
        from odoo.exceptions import UserError
        with self.assertRaises(UserError):
            decode_receipt_file("data:application/pdf;base64," + base64.b64encode(b"nope").decode())

    def test_pdf_from_the_driver_app(self):
        order = self._order(self.partner_a, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            stop, transfer="yes", transfer_amount=1000, receipt_answer="photo"))])
        self.assertEqual(res[0]["status"], "ok", res)
        res = self._sync([self._event(route, "receipt", {
            "photo_uuid": _uuid(), "stop_id": stop.id, "visit_uuid": stop.visit_uuid,
            "transfer_amount": 1000,
            "photo": "data:application/pdf;base64," + base64.b64encode(PDF).decode()})])
        self.assertEqual(res[0]["status"], "ok", res)
        msg = self.env["distri.whatsapp.message"].sudo().search(
            [("distri_app_stop_id", "=", stop.id)])
        self.assertEqual(msg.message_type, "document")
        self.assertEqual(msg.media_mime_type, "application/pdf")
        self.assertTrue(msg.media_filename.endswith(".pdf"))
        self.assertTrue(stop.has_receipt_photo)

    def test_app_texts(self):
        import os
        path = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static", "src", "reparto",
                            "app.js")
        with open(path, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('accept="image/*,application/pdf,.pdf" data-f="rphoto"', source)
        # audit F15: a photo still on the phone is not «sin foto», and what
        # already arrived is apart from «A tu cargo»
        self.assertIn("foto guardada en el celular", source)
        self.assertIn("Resueltas · ya no están a tu cargo", source)
