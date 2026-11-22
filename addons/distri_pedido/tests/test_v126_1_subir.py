"""19.0.1.126.1: the staff upload asks for the customer and credits at once.

- No customer and no «No está en el sistema»: refused. «No está en el
  sistema» needs who sent it; then «Pendiente de asignación», no payment, no
  customer created, only the approvers credit it.
- Customer + amount + «Acreditar comprobantes»: one call uploads and
  credits; a warning (> $ 100.000 in favor) is confirmed without uploading
  again; without the permission it is only uploaded; a defense (operation
  already credited) leaves it pending with the reason.
- Suggested customers (CUIT, name) never become the customer by
  themselves.
- The uploader cancels (soft; a credit is undone), replaces or turns his
  upload; a cancelled file can be uploaded again.
Synthetic data only.
"""
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.ocr_comprobante import CONFIDENCE_HIGH
from odoo.addons.distri_pedido.models.subir_v126_1 import (
    CUSTOMER_MISSING, NO_CREDIT_RIGHT, NOTE_MISSING, PENDING_ASSIGNMENT,
)
from odoo.addons.distri_pedido.tests.test_v126_acreditar import (
    CSRF, CreditMixin, _local, _png,
)

FETCH = {"X-Distri-Fetch": "1"}
PAYER_CUIT = "20999888771"


class UploadMixin(CreditMixin):

    def _read_as(self, amount, operation=None, **extra):
        CreditMixin.seq += 1
        reading = {"amount": amount, "amount_confidence": CONFIDENCE_HIGH,
                   "date": _local(fields.Datetime.now() - timedelta(minutes=2,
                                                                    seconds=CreditMixin.seq))}
        if operation:
            reading["operation"] = operation
        reading.update(extra)
        return self._reading(reading)

    def _file(self):
        CreditMixin.seq += 1
        return ("c%s.png" % CreditMixin.seq, _png(CreditMixin.seq + 3000))

    def _upload(self, user, partner=None, amount=None, unknown=False, note="", credit=True,
                files=None, read=None, operation=None):
        files = files or [self._file()]
        amounts = ["%d" % amount if amount else ""] * len(files)
        read = amount if read is None else read
        Message = self.env["distri.whatsapp.message"].with_user(user)
        with self._read_as(read or 0, operation):
            results = Message._distri_staff_upload(files, partner=partner, unknown=unknown,
                                                   sender_note=note, credit=credit,
                                                   amounts=amounts)
        return results


@tagged("post_install", "-at_install", "distri_v126")
class TestV1261Subir(UploadMixin, AccountTestInvoicingCommon):

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
        cls.seller = user("vendedor_v1261", "distri_pedido.grupo_vendedor")
        cls.other_seller = user("vendedora_v1261", "distri_pedido.grupo_vendedor")
        cls.cashier = user("cajero_v1261", "distri_pedido.grupo_cajero")
        cls.packer = user("armador_v1261", "distri_pedido.grupo_armador")
        cls.admin_user = user("admin_v1261", "distri_pedido.grupo_administracion")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba Subir V1261",
                                                     "company_type": "company"})
        cls.Message = cls.env["distri.whatsapp.message"]

    def _msg(self, result):
        return self.Message.sudo().browse(result["id"])

    def _cards_paid(self, partner=None):
        return len(self._mp_payments(partner))

    # ------------------------------------------------------------ customer

    def test_customer_is_mandatory(self):
        before = self.Message.sudo().search_count([("distri_upload", "=", True)])
        with self.assertRaisesRegex(UserError, "Elegí el cliente"):
            self._upload(self.seller, amount=1000)
        self.assertEqual(CUSTOMER_MISSING.split(".")[0], "Elegí el cliente")
        self.assertEqual(self.Message.sudo().search_count([("distri_upload", "=", True)]), before,
                         "nothing uploaded")

    def test_not_in_system_waits_for_assignment(self):
        partners = self.env["res.partner"].sudo().search_count([])
        with self.assertRaises(UserError) as caught:
            self._upload(self.seller, amount=2000, unknown=True, note="   ")
        self.assertEqual(caught.exception.args[0], NOTE_MISSING)
        payments = self._cards_paid()
        [result] = self._upload(self.seller, amount=2000, unknown=True,
                                note="Kiosco La Esquina, 299 555 0101")
        msg = self._msg(result)
        self.assertEqual((result["status"], result["credit"]), ("nuevo", "asignacion"))
        self.assertEqual(result["message"], PENDING_ASSIGNMENT)
        self.assertEqual(msg.distri_sender_note, "Kiosco La Esquina, 299 555 0101")
        self.assertFalse(msg.distri_partner_id)
        self.assertEqual(self.env["res.partner"].sudo().search_count([]), partners,
                         "no customer created")
        self.assertEqual(self._cards_paid(), payments, "no payment")
        card = self._card(msg)
        self.assertEqual(card.receipt_sender_note, "Kiosco La Esquina, 299 555 0101")
        self.assertEqual(msg._distri_upload_row()["state_label"], "Pendiente de asignación")
        # an assignment: only the approvers credit it, after choosing the customer
        with self.assertRaisesRegex(UserError, "No está en el sistema"):
            self._credit(msg, user=self.seller, partner=self.partner, amount=2000)
        self.assertFalse(card.with_user(self.seller).sudo().can_credit)
        self._credit(msg, user=self.admin_user, partner=self.partner, amount=2000,
                     confirmed=True)
        self.assertEqual(card.state, "confirmed")

    # ------------------------------------------------------------ credit in one step

    def test_upload_and_credit_in_one_call(self):
        before = self._balance()
        [result] = self._upload(self.seller, partner=self.partner, amount=30000)
        msg = self._msg(result)
        self.assertEqual(result["credit"], "acreditado", result["message"])
        self.assertEqual(result["message"], "Acreditado: $ 30.000 a Almacén Prueba Subir V1261.")
        card = self._card(msg)
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(card.credited_by_id, self.seller)
        self.assertEqual(card.payment_id.amount, 30000)
        self.assertEqual(self._balance(), before - 30000, "the balance goes down at once")
        self.assertEqual(msg._distri_upload_row()["state_label"], "Acreditado")

    def test_big_credit_warning_then_confirm_without_uploading_again(self):
        uploads = self.Message.sudo().search_count([("distri_upload", "=", True)])
        [result] = self._upload(self.seller, partner=self.partner, amount=150000)
        self.assertEqual(result["credit"], "aviso")
        self.assertIn("El cliente va a quedar con $ 150.000 a favor. ¿Acreditar igual?",
                      result["warnings"])
        msg = self._msg(result)
        card = self._card(msg)
        self.assertEqual(card.state, "draft", "uploaded, pending")
        done = msg.with_user(self.seller)._distri_upload_credit_now(
            self.partner, amount=150000, confirmed=True)
        self.assertEqual(done["credit"], "acreditado", done["message"])
        self.assertEqual(card.state, "confirmed")
        self.assertEqual(self.Message.sudo().search_count([("distri_upload", "=", True)]),
                         uploads + 1, "one upload only")

    def test_without_permission_only_uploaded(self):
        payments = self._cards_paid()
        [result] = self._upload(self.packer, partner=self.partner, amount=4000)
        self.assertEqual((result["status"], result["credit"]), ("nuevo", "pendiente"))
        self.assertEqual(result["message"], NO_CREDIT_RIGHT)
        self.assertEqual(self._card(self._msg(result)).state, "draft")
        self.assertEqual(self._cards_paid(), payments)

    def test_defense_refusal_keeps_it_pending(self):
        first = self._receipt(9100, operation="126100000011", user=self.seller)
        self._credit(first)
        [result] = self._upload(self.seller, partner=self.partner, amount=9100,
                                operation="126100000019")
        msg = self._msg(result)
        self.assertEqual(result["credit"], "pendiente")
        self.assertIn("no se acreditó", result["message"])
        self.assertIn("ya se acreditó", result["message"])
        self.assertEqual(msg.distri_receipt_state, "pending")
        self.assertEqual(self._card(msg).state, "draft")

    # ------------------------------------------------------------ suggestions

    def test_suggestions_never_assign(self):
        self.env["distri.payer.identity"].sudo().create(
            {"partner_id": self.partner.id, "kind": "cuit", "value": PAYER_CUIT})
        Message = type(self.Message)
        with patch.object(Message, "_distri_read_raw", lambda self, raw, degrees=0: (
                "Transferencia", {"amount": 5100.0, "amount_confidence": CONFIDENCE_HIGH,
                                  "payer_cuit": PAYER_CUIT})):
            preview = self.Message.with_user(self.seller)._distri_upload_preview(_png(3999))
        self.assertEqual(preview["suggestions"][0]["id"], self.partner.id)
        self.assertEqual(preview["suggestions"][0]["reason"], "por CUIT")
        self.assertEqual(preview["amount_text"], "5100")
        names = self.Message._distri_partner_suggestions(payer_name="ALMACEN PRUEBA SUBIR")
        self.assertIn((self.partner.id, "por nombre"), [(s["id"], s["reason"]) for s in names])
        # uploaded without customer: the reading only suggests it
        [result] = self._upload(self.seller, amount=5100, unknown=True, note="Juan, 299 444",
                                credit=False)
        msg = self._msg(result)
        with self._read_as(5100, payer_cuit=PAYER_CUIT):
            msg._distri_read_now()
        self.assertFalse(msg.distri_partner_id, "never assigned by itself")
        self.assertEqual(msg.distri_suggested_partner_id, self.partner)
        detail = msg.with_user(self.admin_user)._distri_upload_detail()
        self.assertEqual(detail["suggestions"][0]["id"], self.partner.id)

    # ------------------------------------------------------------ cancel

    def test_cancel_before_credit(self):
        [result] = self._upload(self.seller, partner=self.partner, amount=6100, credit=False)
        msg = self._msg(result)
        attachment = msg.attachment_id
        text = msg.with_user(self.seller)._distri_upload_cancel("Lo subí dos veces")
        self.assertEqual(text, "Comprobante cancelado.")
        self.assertEqual(msg.distri_receipt_state, "dismissed")
        self.assertEqual(msg.distri_receipt_note, "Cancelado por Vendedor V1261: Lo subí dos veces")
        self.assertEqual(msg.attachment_id, attachment, "the image stays")
        self.assertTrue(attachment.exists())
        self.assertEqual(msg._distri_upload_bucket(), "cancelado")
        card = self._card(msg)
        self.assertEqual(card.state, "ignored")
        self.assertIn("Cancelado por Vendedor V1261", card.message_ids[:1].body)
        with self.assertRaisesRegex(UserError, "motivo|por qué"):
            msg.with_user(self.seller)._distri_upload_cancel(" ")

    def test_cancel_after_credit_undoes_it(self):
        before = self._balance()
        [result] = self._upload(self.seller, partner=self.partner, amount=6200)
        msg = self._msg(result)
        payment = self._card(msg).payment_id
        self.assertEqual(self._balance(), before - 6200)
        text = msg.with_user(self.seller)._distri_upload_cancel("Era de otro cliente")
        self.assertIn("Se anuló la acreditación de $ 6.200 a Almacén Prueba Subir V1261.", text)
        self.assertEqual(payment.state, "canceled")
        self.assertEqual(self._balance(), before, "balance restored")
        self.assertEqual(msg.distri_receipt_state, "dismissed")

    def test_cancel_after_verified_only_for_administracion(self):
        [result] = self._upload(self.cashier, partner=self.partner, amount=6300)
        msg = self._msg(result)
        card = self._card(msg)
        card.sudo().audit_state = "verificado"
        payment = card.payment_id
        with self.assertRaisesRegex(UserError, "verificado con Mercado Pago"):
            msg.with_user(self.cashier)._distri_upload_cancel("Me equivoqué")
        self.assertEqual(card.state, "confirmed")
        msg.with_user(self.admin_user)._distri_upload_cancel("Lo pidió el cajero")
        self.assertEqual(payment.state, "canceled")
        self.assertEqual(msg.distri_receipt_state, "dismissed")

    def test_only_the_uploader_or_administracion(self):
        [result] = self._upload(self.seller, partner=self.partner, amount=6400, credit=False)
        msg = self._msg(result)
        with self.assertRaisesRegex(UserError, "Solo quien lo subió"):
            msg.with_user(self.other_seller)._distri_upload_cancel("No es mío")
        # the next day the uploader no longer can
        self.env.cr.execute("UPDATE distri_whatsapp_message SET create_date = %s WHERE id = %s",
                            [fields.Datetime.now() - timedelta(days=2), msg.id])
        msg.invalidate_recordset()
        with self.assertRaisesRegex(UserError, "mismo día"):
            msg.with_user(self.seller)._distri_upload_cancel("Tarde")
        msg.with_user(self.admin_user)._distri_upload_cancel("Pedido por el vendedor")
        self.assertEqual(msg._distri_upload_bucket(), "cancelado")

    def test_cancelled_file_can_be_uploaded_again(self):
        file = self._file()
        [first] = self._upload(self.seller, partner=self.partner, amount=6500, credit=False,
                               files=[file])
        [again] = self._upload(self.seller, partner=self.partner, amount=6500, credit=False,
                               files=[file])
        self.assertEqual(again["status"], "repetido")
        self._msg(first).with_user(self.seller)._distri_upload_cancel("Foto equivocada")
        [third] = self._upload(self.seller, partner=self.partner, amount=6500, files=[file])
        self.assertEqual(third["status"], "nuevo")
        self.assertNotEqual(third["id"], first["id"])
        self.assertEqual(third["credit"], "acreditado", third["message"])

    # ------------------------------------------------------------ replace and turn

    def test_replace_photo_rereads_and_undoes_the_credit(self):
        [result] = self._upload(self.seller, partner=self.partner, amount=7100)
        msg = self._msg(result)
        card = self._card(msg)
        payment = card.payment_id
        name, raw = self._file()
        with self._read_as(7150, operation="126100000077"):
            text = msg.with_user(self.seller)._distri_upload_replace(name, raw)
        self.assertIn("Se anuló la acreditación de $ 7.100", text)
        self.assertEqual(payment.state, "canceled")
        self.assertEqual(card.state, "draft")
        self.assertEqual(msg.distri_receipt_state, "pending")
        self.assertEqual(msg.distri_read_amount, 7150, "read again")
        self.assertEqual(msg.distri_operation, "126100000077")
        self.assertEqual(msg.attachment_id.raw, raw)
        # credited again after checking the new reading
        self._credit(msg, amount=7150, confirmed=True)
        self.assertEqual(card.state, "confirmed")
        # a photo already loaded is not used to replace
        [other] = self._upload(self.seller, partner=self.partner, amount=7200, credit=False)
        with self.assertRaisesRegex(UserError, "ya está cargada"):
            self._msg(other).with_user(self.seller)._distri_upload_replace(name, raw)

    def test_rotate_rereads(self):
        [result] = self._upload(self.seller, partner=self.partner, amount=7300, credit=False)
        msg = self._msg(result)
        with self._read_as(7000):
            msg._distri_read_now()
        self.assertEqual(msg.distri_read_amount, 7000)
        before = msg.attachment_id.raw
        with self._read_as(7300):
            msg.with_user(self.seller)._distri_upload_rotate(90)
        self.assertNotEqual(msg.attachment_id.raw, before)
        self.assertEqual(msg.distri_read_amount, 7300, "read again after the turn")


@tagged("post_install", "-at_install", "distri_v126")
class TestV1261SubirPaginas(UploadMixin, HttpCase):
    """The upload screen: mandatory customer, suggestion, «Subir y acreditar»,
    «Acreditar igual»."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.seller = new_test_user(cls.env, login="vendedor_v1261_web", name="Vendedor V1261 Web",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V1261 Web",
                                                     "company_type": "company"})
        cls.env["distri.payer.identity"].sudo().create(
            {"partner_id": cls.partner.id, "kind": "cuit", "value": PAYER_CUIT})

    def _page(self):
        self.authenticate("vendedor_v1261_web", "vendedor_v1261_web")
        page = self.url_open("/comprobantes")
        self.assertEqual(page.status_code, 200, page.text[:300])
        return page, CSRF.search(page.text).group(1)

    def test_suggestion_then_explicit_choice_credits(self):
        page, csrf = self._page()
        self.assertIn('data-comp-api="126"', page.text)
        self.assertIn("No está en el sistema", page.text)
        self.assertIn("Subir y acreditar", page.text)
        self.assertRegex(page.text, r'<input type="hidden" name="cliente_id" '
                                    r'data-comp-cliente-id=""\s*/?>', "the customer starts empty")
        name, raw = self._file()
        Message = type(self.env["distri.whatsapp.message"])
        with patch.object(Message, "_distri_read_raw", lambda self, raw, degrees=0: (
                "Transferencia", {"amount": 12500.0, "amount_confidence": CONFIDENCE_HIGH,
                                  "payer_cuit": PAYER_CUIT})):
            res = self.url_open("/comprobantes/sugerir", data={"csrf_token": csrf},
                                files=[("archivo", (name, raw, "image/png"))], headers=FETCH)
        self.assertEqual(res.status_code, 200, res.text[:300])
        data = res.json()
        self.assertEqual(data["suggestions"][0], {"id": self.partner.id,
                                                  "name": "Almacén Prueba V1261 Web",
                                                  "reason": "por CUIT"})
        # a suggestion is not a choice: refused, nothing created
        form = {"csrf_token": csrf, "acreditar": "1", "importes": "12.500"}
        before = self.env["distri.whatsapp.message"].sudo().search_count(
            [("distri_upload", "=", True)])
        res = self.url_open("/comprobantes/subir", data=form,
                            files=[("archivos", (name, raw, "image/png"))], headers=FETCH)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Elegí el cliente", res.json()["error"])
        self.assertEqual(self.env["distri.whatsapp.message"].sudo().search_count(
            [("distri_upload", "=", True)]), before)
        # the tap on the suggestion fills cliente_id: uploaded and credited
        with self._read_as(12500, payer_cuit=PAYER_CUIT):
            res = self.url_open("/comprobantes/subir", data=dict(form, cliente_id=self.partner.id),
                                files=[("archivos", (name, raw, "image/png"))], headers=FETCH)
        self.assertEqual(res.status_code, 200, res.text[:300])
        [result] = res.json()["results"]
        self.assertEqual(result["credit"], "acreditado", result["message"])
        self.assertEqual(result["message"], "Acreditado: $ 12.500 a Almacén Prueba V1261 Web.")
        msg = self.env["distri.whatsapp.message"].sudo().browse(result["id"])
        self.assertEqual(self._card(msg).state, "confirmed")
        row = [u for u in res.json()["uploads"] if u["id"] == msg.id][0]
        self.assertEqual(row["state_label"], "Acreditado")

    def test_warning_confirmed_without_uploading_again(self):
        _page, csrf = self._page()
        name, raw = self._file()
        with self._read_as(260000):
            res = self.url_open("/comprobantes/subir", headers=FETCH, data={
                "csrf_token": csrf, "acreditar": "1", "importes": "260.000",
                "cliente_id": self.partner.id}, files=[("archivos", (name, raw, "image/png"))])
        [result] = res.json()["results"]
        self.assertEqual(result["credit"], "aviso")
        msg = self.env["distri.whatsapp.message"].sudo().browse(result["id"])
        res = self.url_open("/comprobantes/%d/acreditar" % msg.id, headers=FETCH, data={
            "csrf_token": csrf, "importe": result["amount_text"],
            "cliente_id": result["partner_id"], "confirmado": "1"})
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertTrue(res.json()["credited"], res.json())
        self.assertEqual(self._card(msg).state, "confirmed")

    def test_not_in_system_and_cancel_from_the_page(self):
        _page, csrf = self._page()
        name, raw = self._file()
        res = self.url_open("/comprobantes/subir", headers=FETCH, data={
            "csrf_token": csrf, "no_en_sistema": "1", "remitente": ""},
            files=[("archivos", (name, raw, "image/png"))])
        self.assertEqual(res.status_code, 400)
        res = self.url_open("/comprobantes/subir", headers=FETCH, data={
            "csrf_token": csrf, "no_en_sistema": "1", "remitente": "Pedro, kiosco del barrio"},
            files=[("archivos", (name, raw, "image/png"))])
        [result] = res.json()["results"]
        self.assertEqual(result["message"], PENDING_ASSIGNMENT)
        page = self.url_open("/comprobantes/%d" % result["id"])
        self.assertIn("Pedro, kiosco del barrio", page.text)
        self.assertIn("Cancelar comprobante", page.text)
        res = self.url_open("/comprobantes/%d/cancelar" % result["id"],
                            data={"csrf_token": csrf, "motivo": "Lo subí dos veces"})
        self.assertEqual(res.status_code, 200)
        self.assertIn("Comprobante cancelado.", res.text)
