"""19.0.1.129.14: «Es un pago a proveedor autorizado por Gerencia» in «Subir
comprobante».

- A user of grupo_pago_proveedor (Gonzalo) uploads with the option: the
  receipt is registered as a supplier payment, the customer credited in PPCC,
  the receipt closed («Ya registrado», linked) and never offered again.
- A cajero or vendedor without the group never sees the option and the
  server refuses it (upload, supplier search, the form of a receipt).
- A receipt refused for «transferencia a otra cuenta» offers the button and
  is converted without uploading it again.
- No double registration: same file, same operation, a supplier payment
  loaded by hand; amount > 0; never the generic customer.
- The registered receipt is not matched later against the MP report.
Synthetic data only.
"""
import base64
from datetime import timedelta
from unittest.mock import patch

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.pago_proveedor_subir import SUPPLIER_HINT
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_v126_1_subir import FETCH, UploadMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CSRF, CreditMixin, _local, _png

# valid CUITs (check digit), never the company's own (test_ocr.TEST_OWN_ACCOUNT)
SUPPLIER_CUIT = "30700000008"
NEW_SUPPLIER_CUIT = "30712345671"
OTHER_ACCOUNT = {"dest_cuit": SUPPLIER_CUIT, "dest_name": "Proveedor Prueba V12914",
                 "destination": "0000003100099999999991"}


class SupplierMixin(UploadMixin):

    @classmethod
    def _setup_supplier(cls, suffix):
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        # Gonzalo: cajero with «Cobros: pago a proveedor de Distri»
        cls.gonzalo = user("gonzalo_%s" % suffix, "distri_pedido.grupo_cajero,"
                                                 "distri_pedido.grupo_pago_proveedor")
        cls.cashier = user("cajero_%s" % suffix, "distri_pedido.grupo_cajero")
        cls.seller = user("vendedor_%s" % suffix, "distri_pedido.grupo_vendedor")
        cls.manager = user("gerente_%s" % suffix, "distri_pedido.grupo_gerencia")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba %s" % suffix,
                                                     "company_type": "company"})
        cls.supplier = cls.env["res.partner"].create({
            "name": "Proveedor Prueba V12914", "company_type": "company",
            "vat": SUPPLIER_CUIT, "supplier_rank": 1})
        cls.Message = cls.env["distri.whatsapp.message"]
        cls.Supplier = cls.env["distri.supplier.payment"]

    def _values(self, **extra):
        values = {"proveedor_id": str(self.supplier.id), "proveedor_importe": "20.000",
                  "proveedor_fecha": today_ar().isoformat(), "autorizo_id": str(self.manager.id)}
        values.update(extra)
        return values

    def _supplier_upload(self, user, values=None, amount=20000, operation=None, file=None,
                         partner=None):
        name, raw = file or self._file()
        Message = self.Message.with_user(user)
        with self._read_as(amount, operation, **OTHER_ACCOUNT):
            return Message._distri_supplier_upload(name, raw, partner or self.partner,
                                                   values or self._values())

    def _ppcc_payments(self, partner=None):
        journal = self.env["account.journal"]._distri_supplier_journal()
        return self.env["account.payment"].sudo().search([
            ("journal_id", "=", journal.id), ("partner_id", "=", (partner or self.partner).id),
            ("state", "not in", ("draft", "canceled"))])


@tagged("post_install", "-at_install", "distri_v129_14")
class TestV12914PagoProveedorSubir(SupplierMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_supplier("v12914")
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
        ]})

    def _msg(self, result):
        return self.Message.sudo().browse(result["id"])

    # ------------------------------------------------------------ the option

    def test_gonzalo_uploads_with_the_option_and_the_customer_is_credited_in_ppcc(self):
        before = self._balance()
        result = self._supplier_upload(self.gonzalo, operation="129140000001")
        msg = self._msg(result)
        record = msg.distri_supplier_payment_id
        self.assertEqual(result["credit"], "proveedor", result["message"])
        self.assertEqual(result["message"], "Registrado como pago a proveedor %s: $ 20.000 "
                         "acreditados a Almacén Prueba v12914 (proveedor Proveedor Prueba "
                         "V12914)." % record.name)
        self.assertEqual((record.state, record.partner_id, record.supplier_id, record.amount),
                         ("posted", self.partner, self.supplier, 20000))
        self.assertEqual((record.loaded_by_id, record.authorized_by_id),
                         (self.gonzalo, self.manager))
        self.assertEqual(record.receipt_message_id, msg)
        self.assertEqual(record.operation, "129140000001")
        self.assertEqual(base64.b64decode(record.receipt_file), msg.attachment_id.raw)
        self.assertEqual(record.payment_id.journal_id.code, "PPCC")
        self.assertEqual(self._ppcc_payments(), record.payment_id)
        self.assertEqual(self._balance(), before - 20000)
        self.assertFalse(self._mp_payments(), "never a «Mercado Pago» payment")
        # the receipt: closed, linked, never offered again
        self.assertEqual(msg.distri_receipt_state, "registered")
        self.assertTrue(msg.distri_receipt_note.startswith(
            "Registrado como pago a proveedor %s" % record.name), msg.distri_receipt_note)
        card = self._card(msg)
        self.assertEqual((card.state, card.ignore_reason), ("ignored", "supplier_payment"))
        self.assertFalse(msg._distri_credit_card(self.gonzalo))
        self.assertIsNone(msg.with_user(self.gonzalo)._distri_credit_form(self.gonzalo))
        row = msg.with_user(self.gonzalo)._distri_upload_row()
        self.assertEqual((row["bucket"], row["state_label"]), ("aprobado", "Pago a proveedor"))
        self.assertNotIn("supplier_url", row)
        with self.assertRaises(UserError):
            self._credit(msg, user=self.gonzalo, partner=self.partner, amount=20000,
                         confirmed=True)

    def test_new_supplier_from_the_receipt_and_authorizer_rules(self):
        # Gonzalo is not Gerencia: «Autorizó» is required
        with self.assertRaisesRegex(UserError, "Gerencia"):
            self._supplier_upload(self.gonzalo, self._values(autorizo_id=""))
        with self.assertRaisesRegex(UserError, "Gerencia o Sistemas"):
            self._supplier_upload(self.gonzalo, self._values(autorizo_id=str(self.seller.id)))
        values = self._values(proveedor_id="", proveedor_cuit="30-71234567-1",
                              proveedor_nombre="Bebidas del Sur SRL")
        result = self._supplier_upload(self.gonzalo, values)
        supplier = self._msg(result).distri_supplier_payment_id.supplier_id
        self.assertEqual((supplier.name, supplier.vat.replace("-", "")),
                         ("Bebidas del Sur SRL", NEW_SUPPLIER_CUIT))
        self.assertTrue(supplier.supplier_rank)
        # the same CUIT again: the existing partner, never a second one
        self.assertEqual(self.env["res.partner"]._distri_supplier_resolve(
            None, NEW_SUPPLIER_CUIT, "Otro nombre"), supplier)
        self.assertIn(supplier.id, [r["id"] for r in self.env["res.partner"]
                                    ._distri_supplier_search(NEW_SUPPLIER_CUIT)])
        self.assertIn(self.supplier.id, [r["id"] for r in self.env["res.partner"]
                                         ._distri_supplier_search("Proveedor Prueba V129")])
        # a Gerencia user authorizes by default
        fields_ = self.Message.with_user(self.manager)._distri_supplier_fields()
        self.assertEqual(fields_["authorizer_id"], self.manager.id)
        self.assertFalse(self.Message.with_user(self.gonzalo)._distri_supplier_fields()[
            "authorizer_id"])

    def test_the_destination_cuit_preselects_the_supplier(self):
        msg = self._receipt(18000, user=self.gonzalo, extra=OTHER_ACCOUNT)
        defaults = msg._distri_supplier_defaults()
        self.assertEqual(defaults["supplier"]["id"], self.supplier.id)
        self.assertEqual(defaults["amount_text"], "18000")
        Message = type(self.env["distri.whatsapp.message"])
        with patch.object(Message, "_distri_read_raw", lambda self, raw, degrees=0: (
                "Transferencia", {"amount": 18000.0, "dest_cuit": SUPPLIER_CUIT,
                                  "date": _local(fields.Datetime.now())})):
            preview = self.Message.with_user(self.gonzalo)._distri_upload_preview(_png(9101))
            hidden = self.Message.with_user(self.cashier)._distri_upload_preview(_png(9101))
        self.assertEqual(preview["supplier"]["id"], self.supplier.id)
        self.assertEqual(preview["date"], today_ar().isoformat())
        self.assertNotIn("supplier", hidden)
        self.assertNotIn("dest_cuit", hidden)

    # ------------------------------------------------------------ permissions

    def test_cashier_and_seller_without_the_group_are_refused(self):
        msg = self._receipt(15000, user=self.cashier, extra=OTHER_ACCOUNT)
        for user in (self.cashier, self.seller):
            with self.assertRaises(AccessError):
                self._supplier_upload(user)
            with self.assertRaises(AccessError):
                msg.with_user(user)._distri_supplier_register(
                    self.partner, self.supplier, 15000, today_ar(), self.manager)
            self.assertFalse(msg._distri_supplier_offer(user))
            self.assertNotIn("supplier_url", msg.with_user(user)._distri_upload_row())
        self.assertFalse(msg.distri_supplier_payment_id)
        self.assertEqual(msg.distri_receipt_state, "pending")

    # ------------------------------------------------------------ the rejected receipt

    def test_receipt_refused_for_another_account_is_converted(self):
        with self._read_as(25000, **OTHER_ACCOUNT):
            [result] = self.Message.with_user(self.gonzalo)._distri_staff_upload(
                [self._file()], partner=self.partner, credit=True, amounts=["25000"])
        msg = self._msg(result)
        self.assertEqual(result["credit"], "pendiente")
        self.assertIn("transferencia a otra cuenta", result["message"])
        self.assertEqual(result["supplier_url"], "/comprobantes/%d/pago-proveedor" % msg.id)
        self.assertEqual(result["supplier_hint"], SUPPLIER_HINT)
        self.assertEqual(msg.with_user(self.gonzalo)._distri_upload_row()["supplier_url"],
                         result["supplier_url"])
        # a cajero sees the refusal, never the button
        self.assertFalse(msg._distri_supplier_hint(self.cashier))
        before = self._balance()
        record = msg.with_user(self.gonzalo)._distri_supplier_register(
            self.partner, self.supplier, 25000, today_ar(), self.manager)
        self.assertEqual(record.state, "posted")
        self.assertEqual(self._balance(), before - 25000)
        self.assertEqual(msg.distri_supplier_payment_id, record)
        self.assertEqual(self.Message.sudo().search_count([("distri_upload", "=", True),
                                                           ("id", "=", msg.id)]), 1)
        self.assertFalse(msg._distri_supplier_hint(self.gonzalo), "offered once")

    # ------------------------------------------------------------ guards

    def test_no_double_registration(self):
        name, raw = self._file()
        result = self._supplier_upload(self.gonzalo, operation="129140000077", file=(name, raw))
        record = self._msg(result).distri_supplier_payment_id
        # the same file again, with the option: refused, nothing created
        count = self.Supplier.sudo().search_count([])
        with self.assertRaisesRegex(UserError, "Ya estaba cargado|ya estaba"):
            self._supplier_upload(self.gonzalo, file=(name, raw))
        self.assertEqual(self.Supplier.sudo().search_count([]), count)
        # the same receipt twice
        with self.assertRaisesRegex(UserError, "Ya está registrado"):
            self._msg(result).with_user(self.gonzalo)._distri_supplier_register(
                self.partner, self.supplier, 20000, today_ar(), self.manager)
        # another photo of the same operation: neither registered nor credited
        twin = self._receipt(20000, user=self.gonzalo, operation="129140000077")
        with self.assertRaises(UserError):
            twin.with_user(self.gonzalo)._distri_supplier_register(
                self.partner, self.supplier, 20000, today_ar(), self.manager)
        if self._card(twin).state == "draft":
            with self.assertRaisesRegex(UserError, "pago a proveedor|registrado"):
                self._credit(twin, user=self.gonzalo, partner=self.partner, amount=20000,
                             confirmed=True)
        self.assertEqual(self.Supplier.sudo().search_count([("state", "=", "posted"),
                                                            ("operation", "=", "129140000077")]),
                         1)
        self.assertEqual(record.state, "posted")

    def test_a_supplier_payment_loaded_by_hand_blocks_the_same_file(self):
        raw = _png(9201)
        hand = self.Supplier.with_user(self.manager).create({
            "partner_id": self.partner.id, "supplier_id": self.supplier.id, "amount": 9000,
            "authorized_by_id": self.manager.id, "receipt_file": base64.b64encode(raw),
            "receipt_filename": "a-mano.png"})
        hand.action_post()
        self.assertTrue(hand.receipt_checksum)
        msg = self._receipt(9000, user=self.gonzalo, raw=raw)
        with self.assertRaisesRegex(UserError, hand.name):
            self._credit(msg, user=self.gonzalo, partner=self.partner, amount=9000,
                         confirmed=True)
        with self.assertRaisesRegex(UserError, hand.name):
            msg.with_user(self.gonzalo)._distri_supplier_register(
                self.partner, self.supplier, 9000, today_ar(), self.manager)

    def test_amount_and_generic_customer(self):
        generic = self.env["res.partner"].create({"name": "Consumidor genérico V12914",
                                                  "distri_generic_partner": True})
        count = self.Supplier.sudo().search_count([])
        with self.assertRaisesRegex(UserError, "mayor que cero|importe"):
            self._supplier_upload(self.gonzalo, self._values(proveedor_importe="0"))
        with self.assertRaisesRegex(UserError, "genérico"):
            self._supplier_upload(self.gonzalo, partner=generic)
        msg = self._receipt(5000, user=self.gonzalo, extra=OTHER_ACCOUNT)
        with self.assertRaisesRegex(UserError, "mayor que cero"):
            msg.with_user(self.gonzalo)._distri_supplier_register(
                self.partner, self.supplier, 0, today_ar(), self.manager)
        with self.assertRaisesRegex(UserError, "genérico"):
            msg.with_user(self.gonzalo)._distri_supplier_register(
                generic, self.supplier, 5000, today_ar(), self.manager)
        with self.assertRaisesRegex(UserError, "posterior a hoy"):
            msg.with_user(self.gonzalo)._distri_supplier_register(
                self.partner, self.supplier, 5000, today_ar() + timedelta(days=2), self.manager)
        self.assertEqual(self.Supplier.sudo().search_count([]), count)
        self.assertEqual(msg.distri_receipt_state, "pending", "a refusal leaves it as it was")

    # ------------------------------------------------------------ the report

    def _report(self, *rows):
        header = ("TRANSACTION_DATE;SOURCE_ID;PAYMENT_METHOD_TYPE;TRANSACTION_TYPE;"
                  "TRANSACTION_AMOUNT;PAYER_NAME;PAYMENT_METHOD;PAYER_ID_TYPE;PAYER_ID_NUMBER")
        lines = [header]
        for source, when, amount in rows:
            local = _local(when).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
            lines.append(";".join([local, source, "bank_transfer", "SETTLEMENT", "%.2f" % amount,
                                   "PAGADOR PRUEBA", "bank_transfer", "", ""]))
        CreditMixin.seq += 1
        return self.env["distri.mp.report"]._import_file(
            "v12914-%s.csv" % CreditMixin.seq, ("\n".join(lines) + "\n").encode())

    def test_registered_receipt_is_never_matched_against_the_report(self):
        result = self._supplier_upload(self.gonzalo, operation="129140000555", amount=31000)
        msg = self._msg(result)
        card = self._card(msg)
        self._report(("129140000555", fields.Datetime.now() - timedelta(minutes=2), 31000))
        income = self.env["distri.mp.collection"].sudo().search(
            [("kind", "=", "income"), ("source_id", "=", "129140000555")])
        self.assertTrue(income)
        msg.invalidate_recordset()
        card.invalidate_recordset()
        self.assertEqual(msg.distri_receipt_state, "registered")
        self.assertNotEqual(income.receipt_id, msg)
        self.assertNotEqual(msg.distri_collection_id, income)
        self.assertEqual((card.state, card.income_id), ("ignored", income.browse()))
        self.assertFalse(income.payment_id and income.payment_id.journal_id.code == "PPCC")
        self.assertEqual(self._ppcc_payments(), msg.distri_supplier_payment_id.payment_id)
        self.assertEqual(len(self._ppcc_payments()), 1)


@tagged("post_install", "-at_install", "distri_v129_14")
class TestV12914PagoProveedorPaginas(SupplierMixin, HttpCase):
    """The upload page: the option only for the group, the server refuses the
    rest; the button of a receipt refused for another account."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_supplier("v12914_web")

    def _page(self, user):
        self.authenticate(user.login, user.login)
        page = self.url_open("/comprobantes")
        self.assertEqual(page.status_code, 200, page.text[:300])
        return page, CSRF.search(page.text).group(1)

    def test_only_the_group_sees_and_uses_the_option(self):
        for user in (self.cashier, self.seller):
            page, csrf = self._page(user)
            self.assertNotIn("Es un pago a proveedor autorizado por Gerencia", page.text)
            self.assertNotIn('name="pago_proveedor"', page.text)
            count = self.Supplier.sudo().search_count([])
            uploads = self.Message.sudo().search_count([("distri_upload", "=", True)])
            name, raw = self._file()
            with self._read_as(20000, **OTHER_ACCOUNT):
                res = self.url_open("/comprobantes/subir", headers=FETCH, data=dict(
                    self._values(), csrf_token=csrf, pago_proveedor="1",
                    cliente_id=self.partner.id), files=[("archivos", (name, raw, "image/png"))])
            self.assertEqual(res.status_code, 403, res.text[:300])
            self.assertEqual(self.Supplier.sudo().search_count([]), count)
            self.assertEqual(self.Message.sudo().search_count([("distri_upload", "=", True)]),
                             uploads, "nothing uploaded")
            self.assertEqual(self.url_open("/comprobantes/proveedores?q=Proveedor").status_code,
                             403)
            msg = self._receipt(12000, user=user, extra=OTHER_ACCOUNT)
            self.assertEqual(self.url_open("/comprobantes/%d/pago-proveedor" % msg.id)
                             .status_code, 403)
            res = self.url_open("/comprobantes/%d/pago-proveedor" % msg.id, headers=FETCH,
                                data=dict(self._values(), csrf_token=csrf,
                                          cliente_id=self.partner.id))
            self.assertEqual(res.status_code, 403)
            self.assertFalse(msg.distri_supplier_payment_id)

    def test_gonzalo_uploads_with_the_option(self):
        page, csrf = self._page(self.gonzalo)
        self.assertIn("Es un pago a proveedor autorizado por Gerencia", page.text)
        res = self.url_open("/comprobantes/proveedores?q=%s" % SUPPLIER_CUIT)
        self.assertEqual(res.json()["results"][0]["id"], self.supplier.id)
        name, raw = self._file()
        # two files: one at a time
        with self._read_as(20000, **OTHER_ACCOUNT):
            res = self.url_open("/comprobantes/subir", headers=FETCH, data=dict(
                self._values(), csrf_token=csrf, pago_proveedor="1", cliente_id=self.partner.id),
                files=[("archivos", (name, raw, "image/png")),
                       ("archivos", ("b.png", _png(9301), "image/png"))])
        self.assertEqual(res.status_code, 400)
        self.assertIn("un solo comprobante", res.json()["error"])
        with self._read_as(20000, "129140000901", **OTHER_ACCOUNT):
            res = self.url_open("/comprobantes/subir", headers=FETCH, data=dict(
                self._values(), csrf_token=csrf, pago_proveedor="1", cliente_id=self.partner.id),
                files=[("archivos", (name, raw, "image/png"))])
        self.assertEqual(res.status_code, 200, res.text[:300])
        [result] = res.json()["results"]
        self.assertEqual(result["credit"], "proveedor", result)
        msg = self.Message.sudo().browse(result["id"])
        self.assertEqual(msg.distri_supplier_payment_id.state, "posted")
        row = [u for u in res.json()["uploads"] if u["id"] == msg.id][0]
        self.assertEqual(row["state_label"], "Pago a proveedor")
        page = self.url_open("/comprobantes/%d" % msg.id)
        self.assertIn("Registrado como pago a proveedor", page.text)
        self.assertNotIn("Acreditar</button>", page.text)

    def test_refused_receipt_converted_with_the_button(self):
        _page, csrf = self._page(self.gonzalo)
        name, raw = self._file()
        with self._read_as(26000, **OTHER_ACCOUNT):
            res = self.url_open("/comprobantes/subir", headers=FETCH, data={
                "csrf_token": csrf, "acreditar": "1", "importes": "26.000",
                "cliente_id": self.partner.id}, files=[("archivos", (name, raw, "image/png"))])
        [result] = res.json()["results"]
        self.assertEqual(result["credit"], "pendiente", result)
        self.assertIn("otra cuenta", result["message"])
        self.assertEqual(result["supplier_url"], "/comprobantes/%d/pago-proveedor" % result["id"])
        view = self.url_open("/comprobantes/%d" % result["id"])
        self.assertIn(SUPPLIER_HINT, view.text)
        form = self.url_open(result["supplier_url"])
        self.assertEqual(form.status_code, 200, form.text[:300])
        self.assertIn("Registrar pago a proveedor", form.text)
        self.assertIn('value="%s"' % self.supplier.id, form.text, "preselected by the CUIT")
        self.assertIn('value="26000"', form.text, "the amount read")
        res = self.url_open(result["supplier_url"], data=dict(
            self._values(proveedor_importe="26.000"), csrf_token=csrf,
            cliente_id=self.partner.id))
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertIn("Registrado como pago a proveedor PPC-", res.text)
        msg = self.Message.sudo().browse(result["id"])
        self.assertEqual(msg.distri_supplier_payment_id.amount, 26000)
        self.assertEqual(msg.distri_receipt_state, "registered")
        # once
        res = self.url_open(result["supplier_url"], headers=FETCH, data=dict(
            self._values(proveedor_importe="26.000"), csrf_token=csrf,
            cliente_id=self.partner.id))
        self.assertEqual(res.status_code, 400)
        self.assertIn("Ya está registrado", res.json()["error"])
