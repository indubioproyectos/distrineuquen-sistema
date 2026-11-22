"""19.0.1.126.2: cancelled uploads and the uploader's own pending receipt.

The practice case: a cashier uploads a photo with «No está en el sistema»,
uploads the same photo again meaning to pick the customer, cancels the
first one and uploads it once more.

- A cancelled upload is never a duplicate (also one still holding its key:
  the unique `wa_message_id` is freed lazily).
- The same bytes as one's own receipt pending assignment offer to complete
  it; completing credits the EXISTING receipt (defenses and the > $ 100.000
  warning included), no second record.
- Someone else's receipt, or a credited one, is still refused, naming who
  uploaded it and how it is.
Synthetic data only.
"""
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.comprobante_subir import CANCEL_KEY, UPLOAD_KEY, file_key
from odoo.addons.distri_pedido.models.subir_v126_2 import (
    ATTEMPT_CANCELLED_FLAG, COMPLETE_GONE, COMPLETE_OFFER, COMPLETED_NO_CREDIT,
)
from odoo.addons.distri_pedido.tests.test_v126_1_subir import FETCH, UploadMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CSRF

NOTE = "Kiosco del barrio, 299 555 0102"


@tagged("post_install", "-at_install", "distri_v126")
class TestV1262Subir(UploadMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        cls.cashier = user("cajero_v1262", "distri_pedido.grupo_cajero")
        cls.seller = user("vendedor_v1262", "distri_pedido.grupo_vendedor")
        cls.packer = user("armador_v1262", "distri_pedido.grupo_armador")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V1262",
                                                     "company_type": "company"})
        cls.Message = cls.env["distri.whatsapp.message"]
        cls.Attempt = cls.env["distri.receipt.upload.attempt"]

    def _msg(self, result):
        return self.Message.sudo().browse(result["id"])

    def _uploads(self):
        return self.Message.sudo().search_count([("distri_upload", "=", True)])

    def _pending(self, user, file, amount=8000):
        [result] = self._upload(user, amount=amount, unknown=True, note=NOTE, files=[file])
        self.assertEqual((result["status"], result["credit"]), ("nuevo", "asignacion"))
        return self._msg(result)

    # ------------------------------------------------------------ the practice case

    def test_practice_case_pending_then_cancel_then_upload_again(self):
        file = self._file()
        first = self._pending(self.cashier, file)
        # 2. the same photo again, now with the customer: not refused any more
        uploads, attempts = self._uploads(), self.Attempt.sudo().search_count([])
        [again] = self._upload(self.cashier, partner=self.partner, amount=8000, files=[file])
        self.assertEqual(again["status"], "completar")
        self.assertEqual(again["id"], first.id)
        self.assertEqual(self._uploads(), uploads, "no record created")
        self.assertEqual(self.Attempt.sudo().search_count([]), attempts, "not a refused attempt")
        # 3. cancelled to free it
        first.with_user(self.cashier)._distri_upload_cancel("Lo subí sin cliente")
        self.assertEqual(first.distri_receipt_state, "dismissed")
        self.assertEqual(first.wa_message_id, CANCEL_KEY % (first.id, UPLOAD_KEY % file_key(
            file[1])))
        # 4. uploaded again: a new receipt, credited
        [third] = self._upload(self.cashier, partner=self.partner, amount=8000, files=[file])
        self.assertEqual(third["status"], "nuevo", third["message"])
        self.assertNotEqual(third["id"], first.id)
        self.assertEqual(third["credit"], "acreditado", third["message"])
        self.assertEqual(self._uploads(), uploads + 1)

    def test_cancelled_upload_still_holding_its_key_is_freed(self):
        # the unique key is what a new upload of the same bytes would hit
        self.env.cr.execute("""
            SELECT 1 FROM pg_constraint
             WHERE conrelid = 'distri_whatsapp_message'::regclass AND contype = 'u'
               AND pg_get_constraintdef(oid) = 'UNIQUE (wa_message_id)'""")
        self.assertTrue(self.env.cr.fetchone(), "wa_message_id is unique")
        file = self._file()
        [result] = self._upload(self.seller, partner=self.partner, amount=6600, credit=False,
                                files=[file])
        old = self._msg(result)
        key = UPLOAD_KEY % file_key(file[1])
        # cancelled without freeing the key (as before the key was freed)
        old._distri_close("dismissed", "Cancelado por Vendedor V1262: foto equivocada")
        self.assertEqual(old.wa_message_id, key)
        self.assertTrue(old._distri_upload_cancelled())
        self.assertFalse(self.Message._distri_upload_duplicate(file[1]), "never a duplicate")
        self.assertEqual(old.wa_message_id, CANCEL_KEY % (old.id, key), "freed lazily")
        [new] = self._upload(self.seller, partner=self.partner, amount=6600, files=[file])
        self.assertEqual(new["status"], "nuevo", new["message"])
        self.assertEqual(self._msg(new).wa_message_id, key)
        self.assertEqual(old._distri_upload_bucket(), "cancelado")

    # ------------------------------------------------------------ own pending receipt

    def test_own_pending_duplicate_offers_completion_and_credits_it(self):
        file = self._file()
        msg = self._pending(self.cashier, file, amount=9200)
        uploads = self._uploads()
        [offer] = self._upload(self.cashier, partner=self.partner, amount=9200, files=[file])
        self.assertEqual(offer["status"], "completar")
        self.assertEqual(offer["message"], COMPLETE_OFFER)
        self.assertEqual(offer["url"], "/comprobantes/%d/completar" % msg.id)
        self.assertEqual(offer["file_url"], "/comprobantes/%d/archivo" % msg.id)
        self.assertTrue(offer["image"])
        self.assertEqual(offer["amount"], 9200)
        self.assertEqual(offer["sender_note"], NOTE)
        self.assertEqual(offer["partner_id"], self.partner.id, "the customer chosen now, pre-filled")
        self.assertIsInstance(offer["suggestions"], list)
        self.assertEqual(self._card(msg).state, "draft", "nothing credited by itself")
        row = msg.with_user(self.cashier)._distri_upload_row()
        self.assertEqual(row["complete_url"], offer["url"])
        before = self._balance()
        with self._read_as(9200):
            done = msg.with_user(self.cashier)._distri_upload_complete(self.partner)
        self.assertEqual(done["credit"], "acreditado", done["message"])
        self.assertEqual(done["message"], "Acreditado: $ 9.200 a Almacén Prueba V1262.")
        card = self._card(msg)
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.credited_by_id, self.cashier)
        self.assertEqual(msg.distri_partner_id, self.partner)
        self.assertFalse(msg.distri_sender_note, "no longer an assignment")
        self.assertIn(NOTE, msg.distri_partner_source)
        self.assertEqual(self._balance(), before - 9200)
        self.assertEqual(self._uploads(), uploads, "the existing receipt, no second record")
        # done: the same file is now refused, credited
        [late] = self._upload(self.cashier, partner=self.partner, amount=9200, files=[file])
        self.assertEqual(late["status"], "repetido")
        with self.assertRaisesRegex(UserError, COMPLETE_GONE.split(":")[0]):
            msg.with_user(self.cashier)._distri_upload_complete(self.partner)

    def test_completion_keeps_the_big_credit_warning(self):
        file = self._file()
        msg = self._pending(self.cashier, file, amount=150000)
        [offer] = self._upload(self.cashier, partner=self.partner, amount=150000, files=[file])
        self.assertEqual(offer["status"], "completar")
        with self._read_as(150000):
            warned = msg.with_user(self.cashier)._distri_upload_complete(self.partner)
        self.assertEqual(warned["credit"], "aviso")
        self.assertIn("El cliente va a quedar con $ 150.000 a favor. ¿Acreditar igual?",
                      warned["warnings"])
        self.assertEqual(self._card(msg).state, "draft")
        done = msg.with_user(self.cashier)._distri_upload_credit_now(
            self.partner, amount=150000, confirmed=True)
        self.assertEqual(done["credit"], "acreditado", done["message"])
        self.assertEqual(self._card(msg).state, "confirmed")

    def test_completion_without_credit_right_only_assigns(self):
        file = self._file()
        msg = self._pending(self.packer, file, amount=4100)
        [offer] = self._upload(self.packer, partner=self.partner, amount=4100, credit=False,
                               files=[file])
        self.assertEqual(offer["status"], "completar")
        done = msg.with_user(self.packer)._distri_upload_complete(self.partner)
        self.assertEqual(done, {"credit": "pendiente",
                                "message": COMPLETED_NO_CREDIT % self.partner.name})
        self.assertEqual(msg.distri_partner_id, self.partner)
        self.assertEqual(self._card(msg).state, "draft")

    # ------------------------------------------------------------ still refused

    def test_someone_elses_duplicate_is_refused_naming_the_uploader(self):
        file = self._file()
        msg = self._pending(self.seller, file, amount=5300)
        uploads = self._uploads()
        [result] = self._upload(self.cashier, partner=self.partner, amount=5300, files=[file])
        self.assertEqual(result["status"], "repetido")
        self.assertEqual(result["id"], msg.id)
        self.assertEqual(result["message"], "Ya estaba cargado: lo subió Vendedor V1262 hoy, "
                                            "pendiente de asignación ($ 5.300).")
        self.assertTrue(self.Attempt.sudo().browse(result["attempt_id"]).exists())
        self.assertEqual(self._uploads(), uploads)
        self.assertFalse(msg.distri_partner_id, "untouched")

    def test_credited_duplicate_is_refused(self):
        file = self._file()
        [first] = self._upload(self.cashier, partner=self.partner, amount=7400, files=[file])
        self.assertEqual(first["credit"], "acreditado", first["message"])
        [again] = self._upload(self.cashier, partner=self.partner, amount=7400, files=[file])
        self.assertEqual(again["status"], "repetido")
        self.assertEqual(again["message"], "Ya estaba cargado: lo subiste vos hoy, acreditado a "
                                           "Almacén Prueba V1262 ($ 7.400).")
        [other] = self._upload(self.seller, partner=self.partner, amount=7400, files=[file])
        self.assertIn("lo subió Cajero V1262 hoy, acreditado a Almacén Prueba V1262", other["message"])

    def test_attempt_of_a_receipt_cancelled_later_says_so(self):
        file = self._file()
        [first] = self._upload(self.seller, partner=self.partner, amount=6700, credit=False,
                               files=[file])
        [again] = self._upload(self.seller, partner=self.partner, amount=6700, credit=False,
                               files=[file])
        self.assertEqual(again["status"], "repetido", "a customer already: not to complete")
        attempt = self.Attempt.sudo().browse(again["attempt_id"])
        self._msg(first).with_user(self.seller)._distri_upload_cancel("Foto equivocada")
        row = attempt._distri_row(self.seller)
        self.assertEqual(row["flag"], ATTEMPT_CANCELLED_FLAG)
        self.assertTrue(row["cancelled"])
        self.assertTrue(attempt._distri_detail()["cancelled"])

    def test_pending_assignment_row_reaches_cancel(self):
        msg = self._pending(self.cashier, self._file())
        row = msg.with_user(self.cashier)._distri_upload_row()
        self.assertTrue(row["can_manage"])
        self.assertEqual(row["state_label"], "Pendiente de asignación")
        detail = msg.with_user(self.cashier)._distri_upload_detail()
        self.assertTrue(detail["manage"], detail["manage_problem"])


@tagged("post_install", "-at_install", "distri_v126")
class TestV1262SubirPaginas(UploadMixin, HttpCase):
    """The practice case on the pages: upload pending, the same file offers
    «Completar y acreditar», the page credits the existing receipt."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cashier = new_test_user(cls.env, login="cajero_v1262_web", name="Cajero V1262 Web",
                                    groups="base.group_user,distri_pedido.grupo_cajero")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V1262 Web",
                                                     "company_type": "company"})

    def test_complete_from_the_pages(self):
        self.authenticate("cajero_v1262_web", "cajero_v1262_web")
        page = self.url_open("/comprobantes")
        csrf = CSRF.search(page.text).group(1)
        name, raw = self._file()
        res = self.url_open("/comprobantes/subir", headers=FETCH, data={
            "csrf_token": csrf, "no_en_sistema": "1", "remitente": NOTE, "importes": "8.800",
            "acreditar": "1"}, files=[("archivos", (name, raw, "image/png"))])
        [first] = res.json()["results"]
        msg = self.env["distri.whatsapp.message"].sudo().browse(first["id"])
        row = [u for u in res.json()["uploads"] if u["id"] == msg.id][0]
        self.assertTrue(row["can_manage"])
        self.assertEqual(row["complete_url"], "/comprobantes/%d/completar" % msg.id)
        uploads = self.env["distri.whatsapp.message"].sudo().search_count(
            [("distri_upload", "=", True)])
        res = self.url_open("/comprobantes/subir", headers=FETCH, data={
            "csrf_token": csrf, "cliente_id": self.partner.id, "importes": "8.800",
            "acreditar": "1"}, files=[("archivos", (name, raw, "image/png"))])
        self.assertEqual(res.status_code, 200, res.text[:300])
        [offer] = res.json()["results"]
        self.assertEqual((offer["status"], offer["id"]), ("completar", msg.id))
        # «Completar y acreditar» opens the existing receipt with the customer
        page = self.url_open("%s?cliente_id=%d" % (offer["url"], self.partner.id))
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn(COMPLETE_OFFER, page.text)
        self.assertIn("Completar y acreditar", page.text)
        self.assertIn("Cancelar comprobante", page.text, "cancel reachable too")
        self.assertIn('value="%d"' % self.partner.id, page.text)
        self.assertIn("/comprobantes/%d/archivo" % msg.id, page.text, "the photo")
        with self._read_as(8800):
            res = self.url_open(offer["url"], headers=FETCH, data={
                "csrf_token": csrf, "cliente_id": self.partner.id, "importe": "8.800"})
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertTrue(res.json()["credited"], res.json())
        self.assertEqual(self._card(msg).state, "confirmed")
        self.assertEqual(msg.distri_partner_id, self.partner)
        self.assertEqual(self.env["distri.whatsapp.message"].sudo().search_count(
            [("distri_upload", "=", True)]), uploads, "no second record")
        # completed: the page is «Ver comprobante» again
        res = self.url_open(offer["url"], allow_redirects=False)
        self.assertEqual(res.status_code, 303)
