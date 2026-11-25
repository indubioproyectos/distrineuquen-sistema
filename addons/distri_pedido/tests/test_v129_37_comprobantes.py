"""19.0.1.129.37 (owner, 25/11/2026): receipts of the counter sale and of the
depot hand-over, and the multi-file upload of administración.

Behind distri_pedido.nueva_venta (models/comprobantes_mostrador.py,
controllers/deposito.py):
- a transfer at the depot (also right after «Venta en mostrador») asks
  «Subir comprobante»: one or several files on the spot, each one a receipt
  of the staff upload's pipeline naming the order (it resolves the visit's
  pending receipt); a bad file refuses the whole form, saying which;
- «Lo manda después»: the pending receipt at the name of whoever delivered,
  in «Mis comprobantes a reclamar» (/deposito/reclamar, Inicio); it expires
  and is charged like a driver's, and closes by the same rules (the receipt
  arrives, Mercado Pago verifies the transfer).
Behind distri_pedido.verificacion_v2 (/comprobantes): several files, one
request each, each a receipt of the chosen customer, never credited on
upload; a bad file is refused on its own.
With the flags off everything is as in 129.36. No entry, payment or
reconciliation is created by any of it: the ledger and the customer's
balance are compared before and after. Synthetic data only.
"""
import io
import json
import os
import re
import shutil
import subprocess
from datetime import timedelta
from unittest import SkipTest

from PIL import Image

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.models.comprobantes_mostrador import (
    FILE_TOO_BIG, FILE_WRONG_KIND, NO_FILE)
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.models.reparto_v2 import PARAM_REPARTO_V2
from odoo.addons.distri_pedido.models.verificacion import PARAM_VERIFICACION
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
SEED = [37000]


def _png():
    SEED[0] += 1
    seed = SEED[0]
    buffer = io.BytesIO()
    Image.new("RGB", (30 + seed % 50, 20 + seed // 50 % 50), (seed % 255, 37, 90)).save(
        buffer, format="PNG")
    return buffer.getvalue()


def _pdf():
    SEED[0] += 1
    return b"%PDF-1.4\n% comprobante " + str(SEED[0]).encode() + b"\n%%EOF\n"


@tagged("post_install", "-at_install", "distri_v129_37")
class TestV12937Comprobantes(RepartoMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        (cls.product_a | cls.product_b).write({"invoice_policy": "order"})
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_v137", "distri_pedido.grupo_administracion")
        cls.admin2 = user("admindos_v137", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v137", "distri_pedido.grupo_gerencia")
        cls.vendedor = user("vendedor_v137", "distri_pedido.grupo_vendedor")
        cls.cajero = user("cajero_v137", "distri_pedido.grupo_cajero")
        cls.armador = user("armador_v137", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_v137", "distri_pedido.grupo_reparto")
        cls.nadie = user("nadie_v137", "base.group_user")
        cls.partner = cls.partner_a
        cls.partner.write({"distri_whatsapp": "2995001237", "name": "Almacen Mostrador V137"})
        cls.other = cls.env["res.partner"].create({"name": "Kiosco Otro V137",
                                                   "company_type": "company"})
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Mostrador v137", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})
        numerar_remitos(cls.env)
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Promise = cls.env["distri.promised.transfer"].sudo()

    # ------------------------------------------------------------ helpers

    def _flag(self, on=True, key=PARAM_NUEVA_VENTA):
        self.env["ir.config_parameter"].sudo().set_param(key, "1" if on else "0")

    def _ledger(self):
        """Entries, payments, reconciliations, the receivable total and the
        customer's own receivable."""
        self.env.flush_all()
        self.partner.invalidate_recordset(["credit"])
        return (self.Promise._distri_ledger_fingerprint(), round(self.partner.credit, 2))

    def _quotation(self, qty_a=3, qty_b=2):
        return self.env["sale.order"].create({
            "partner_id": self.partner.id, "pricelist_id": self.pricelist.id,
            "order_line": [
                Command.create({"product_id": self.product_a.id, "product_uom_qty": qty_a,
                                "price_unit": 100.0, "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.product_b.id, "product_uom_qty": qty_b,
                                "price_unit": 40.0, "tax_ids": [Command.clear()]}),
            ]})

    def _packed(self):
        order = self._quotation()
        order.distri_entrega = "retira"
        order.action_confirm()
        self.assertEqual(order.distri_tomar(), "")
        self.assertEqual(order.distri_terminar(2), "")
        return order

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self, path="/deposito"):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1)

    def _post(self, path, data=None, files=None, page="/deposito"):
        data = dict(data or {}, csrf_token=self._csrf(page))
        res = self.url_open(path, data=data, files=files, headers=FETCH, allow_redirects=False,
                            timeout=60)
        self.env.invalidate_all()
        return res

    def _counter(self, order):
        """«Venta en mostrador» as /deposito/mostrador posts it."""
        res = self._post("/deposito/mostrador/%s" % order.id, page="/deposito/mostrador")
        self.assertTrue(res.json()["ok"], res.text)
        return res

    def _deliver(self, order, files=(), mostrador=False, **values):
        data = {"devolvio": "none", "codigo_op": "without_code", "transferencia": "yes",
                "importe_transf": "380", "comprobante": "subir"}
        if mostrador:
            data.update(mostrador="1", presente="1", bultos="2")
        data.update(values)
        upload = [("comprobantes", (name, raw, mime)) for name, raw, mime in files]
        return self._post("/deposito/entrega/%s" % order.id, data, files=upload or None)

    def _stop(self, order):
        return order.distri_stop_ids.sorted("id")[-1:]

    def _promise(self, order):
        return self.Promise.search([("order_id", "=", order.id)])

    def _receipts(self, order):
        return self.Message.search([("distri_order_ref", "=", order.name),
                                    ("distri_upload", "=", True)], order="id")

    # ------------------------------------------------------------ «Subir comprobante»

    def test_counter_sale_with_one_receipt(self):
        self._flag()
        order = self._quotation()
        self._login(self.admin)
        self._counter(order)
        page = self.url_open("/deposito/entrega/%s?mostrador=1" % order.id).text
        self.assertIn("Subir comprobante", page)
        self.assertIn("Lo manda después", page)
        self.assertIn('name="comprobantes"', page)
        self.assertIn('enctype="multipart/form-data"', page)
        self.assertLess(page.index("Subir comprobante"), page.index("Lo manda después"),
                        "the upload comes first")
        before = self._ledger()
        res = self._deliver(order, [("transferencia.png", _png(), "image/png")], mostrador=True)
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("1 comprobante subido", res.json()["url"].replace("+", " "))
        stop = self._stop(order)
        self.assertEqual((stop.transfer, stop.transfer_amount, stop.receipt_answer),
                         ("yes", 380, "photo"))
        msg = self._receipts(order)
        self.assertEqual(len(msg), 1)
        self.assertEqual(msg.distri_partner_id, self.partner)
        self.assertEqual(msg.distri_forwarded_by_id, self.admin, "staff upload: «Para revisar»")
        self.assertEqual(msg.distri_receipt_state, "pending")
        self.assertEqual(msg.distri_amount_typed, 380, "the transfer of the visit, one file")
        self.assertTrue(msg.attachment_id)
        promise = self._promise(order)
        self.assertEqual(promise.state, "fulfilled", "the receipt of its own order resolved it")
        self.assertEqual(promise.receipt_id, msg)
        self.assertFalse(promise.distri_user_charge)
        self.assertEqual(self._ledger(), before, "nothing credited, no balance moved")
        self.assertFalse(stop.route_id.payment_ids)

    def test_depot_delivery_with_several_receipts(self):
        self._flag()
        order = self._packed()
        self._login(self.admin)
        before = self._ledger()
        files = [("a.png", _png(), "image/png"), ("b.pdf", _pdf(), "application/pdf"),
                 ("c.png", _png(), "image/png")]
        res = self._deliver(order, files)
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("3 comprobantes subidos", res.json()["url"].replace("+", " "))
        msgs = self._receipts(order)
        self.assertEqual(len(msgs), 3)
        self.assertEqual(set(msgs.mapped("distri_partner_id")), {self.partner})
        self.assertFalse(any(msgs.mapped("distri_amount_typed")),
                         "several files: the split of the amount is not known")
        self.assertEqual(self._promise(order).receipt_id, msgs[0])
        self.assertEqual(self._ledger(), before)
        # the same file again, at another delivery: «ya estaba cargado», nothing new
        again = self._packed()
        res = self._deliver(again, [("a-de-nuevo.png", files[0][1], "image/png"),
                                    ("d.png", _png(), "image/png")])
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("1 ya estaba cargado", res.json()["url"].replace("+", " "))
        self.assertEqual(len(self._receipts(again)), 1)

    def test_a_bad_file_refuses_the_delivery(self):
        self._flag()
        order = self._packed()
        self._login(self.admin)
        before = self._ledger()
        messages = self.Message.search_count([])
        big = b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024 + 10)
        res = self._deliver(order, [("bien.png", _png(), "image/png"),
                                    ("nota.txt", b"hola", "text/plain"),
                                    ("enorme.pdf", big, "application/pdf")])
        self.assertEqual(res.status_code, 400)
        error = res.json()["error"]
        self.assertIn("nota.txt: %s" % FILE_WRONG_KIND, error)
        self.assertIn("enorme.pdf: %s" % FILE_TOO_BIG, error)
        self.assertNotIn("bien.png", error)
        res = self._deliver(order, [])
        self.assertEqual(res.status_code, 400)
        self.assertEqual(res.json()["error"], NO_FILE)
        res = self._deliver(order, [], comprobante="forwarded")
        self.assertEqual(res.status_code, 400)
        self.assertIn("Lo manda después", res.json()["error"])
        self.assertFalse(order.distri_stop_ids, "nothing recorded")
        self.assertEqual(self.Message.search_count([]), messages)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ «Lo manda después»

    def _later(self, user=None):
        order = self._packed()
        self._login(user or self.admin)
        res = self._deliver(order, comprobante="despues")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("queda a tu cargo", res.json()["url"].replace("+", " "))
        return order

    def test_later_is_at_the_name_of_who_delivered_and_closes_on_verification(self):
        self._flag()
        order = self._later()
        before = self._ledger()
        promise = self._promise(order)
        self.assertEqual(self._stop(order).receipt_answer, "not_yet")
        self.assertTrue(promise.distri_user_charge)
        self.assertEqual(promise.driver_id, self.admin)
        self.assertEqual(promise.state, "open")
        # his list, the row in /deposito and Inicio
        page = self.url_open("/deposito/reclamar")
        self.assertEqual(page.status_code, 200)
        self.assertIn(order.name, page.text)
        self.assertIn("Pendiente", page.text)
        self.assertIn("wa.me/", page.text)
        self.assertIn("Mis comprobantes a reclamar", self.url_open("/deposito").text)
        self.assertIn("Mis comprobantes a reclamar: 1 comprobante", self.url_open("/tablero").text)
        # nobody else has it
        self._login(self.admin2)
        self.assertNotIn(order.name, self.url_open("/deposito/reclamar").text)
        self.assertNotIn("Mis comprobantes a reclamar", self.url_open("/tablero").text)
        # it expires like a driver's: the day after the next one
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "open", "not yet")
        promise.captured_at = fields.Datetime.now() - timedelta(days=3)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "overdue")
        case = promise.charge_case_id
        self.assertEqual((case.kind, case.state, case.amount, case.driver_id),
                         ("receipt", "open", -380, self.admin))
        self.assertIn("Queda a cargo de %s" % self.admin.name, case.candidates)
        self._login(self.admin)
        self.assertIn("cargo a tu nombre", self.url_open("/deposito/reclamar").text)
        # Mercado Pago brings the transfer: resolved, the charge closes
        self.Promise._distri_fulfil_for(self.partner, fields.Datetime.now(), 380)
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(case.state, "closed")
        self.assertNotIn(order.name, self.url_open("/deposito/reclamar").text)
        self.assertEqual(self.Promise._distri_user_summary(self.admin), (0, 0.0))
        self.assertEqual(self._ledger(), before, "only the pending receipt and its charge")

    def test_later_receipt_uploaded_from_the_list(self):
        self._flag()
        order = self._later()
        promise = self._promise(order)
        # another depot user cannot upload it as his
        self._login(self.admin2)
        res = self._post("/deposito/reclamar/%s/subir" % promise.id,
                         files=[("comprobantes", ("x.png", _png(), "image/png"))])
        self.assertEqual(res.status_code, 400)
        self.assertIn("no está a tu nombre", res.json()["error"])
        self._login(self.admin)
        res = self._post("/deposito/reclamar/%s/subir" % promise.id,
                         files=[("comprobantes", ("tarde.jpg", b"text", "image/jpeg"))])
        self.assertEqual(res.status_code, 400)
        self.assertIn(FILE_WRONG_KIND, res.json()["error"])
        before = self._ledger()
        res = self._post("/deposito/reclamar/%s/subir" % promise.id,
                         files=[("comprobantes", ("tarde.png", _png(), "image/png"))])
        self.assertTrue(res.json()["ok"], res.text)
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(promise.receipt_id, self._receipts(order))
        self.assertEqual(self._receipts(order).distri_amount_typed, 380)
        self.assertFalse(promise.charge_case_id)
        self.assertEqual(self.Promise._distri_user_summary(self.admin), (0, 0.0))
        self.assertEqual(self._ledger(), before)

    def test_late_receipt_closes_the_charge_like_a_drivers(self):
        """reparto_v2's rule: a receipt after the deadline closes the charge,
        unless it is the charged user's own upload (human check)."""
        self._flag()
        self._flag(key=PARAM_REPARTO_V2)
        order = self._later()
        promise = self._promise(order)
        promise.captured_at = fields.Datetime.now() - timedelta(days=3)
        self.Promise._distri_cron_expire()
        case = promise.charge_case_id
        self.assertEqual(case.state, "open")
        Message = self.env["distri.whatsapp.message"]
        Message.with_user(self.admin)._distri_depot_upload(order, [("mio.png", _png())])
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(case.state, "open", "his own upload does not close his charge")
        self.assertIn(order.name, self.url_open("/deposito/reclamar").text)
        # a second order: the receipt comes from someone else
        second = self._later()
        promise2 = self._promise(second)
        promise2.captured_at = fields.Datetime.now() - timedelta(days=3)
        self.Promise._distri_cron_expire()
        Message.with_user(self.admin2)._distri_depot_upload(second, [("otro.png", _png())])
        self.assertEqual(promise2.state, "fulfilled")
        self.assertEqual(promise2.charge_case_id.state, "closed")

    # ------------------------------------------------------------ flags off

    def test_flag_off_is_129_36(self):
        self._flag(False)
        order = self._packed()
        self._login(self.admin)
        page = self.url_open("/deposito/entrega/%s" % order.id).text
        self.assertNotIn('name="comprobantes"', page)
        self.assertNotIn("enctype", page)
        self.assertIn('value="forwarded"', page)
        self.assertIn('value="not_yet"', page)
        res = self._deliver(order, comprobante="subir")
        self.assertEqual(res.status_code, 400, "«subir» is no answer of 129.36")
        res = self._deliver(order, comprobante="not_yet")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertNotIn("a tu cargo", res.json()["url"].replace("+", " "))
        promise = self._promise(order)
        self.assertFalse(promise.distri_user_charge)
        promise.captured_at = fields.Datetime.now() - timedelta(days=3)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "open", "a depot receipt never expires in 129.36")
        self.assertEqual(self.url_open("/deposito/reclamar").status_code, 404)
        self.assertNotIn("Mis comprobantes a reclamar", self.url_open("/tablero").text)
        self.assertNotIn("Mis comprobantes a reclamar", self.url_open("/deposito").text)
        # an item marked while the flag was on does not expire with it off
        self._flag()
        later = self._later()
        self._flag(False)
        marked = self._promise(later)
        marked.captured_at = fields.Datetime.now() - timedelta(days=3)
        self.Promise._distri_cron_expire()
        self.assertEqual(marked.state, "open")
        # «Subir comprobante» of administración: no drop zone
        self._flag(False, PARAM_VERIFICACION)
        page = self.url_open("/comprobantes").text
        self.assertNotIn("data-comp-multi", page)
        self.assertNotIn("data-comp-drop", page)
        self.assertNotIn("arrastrá", page)

    def test_permissions(self):
        self._flag()
        order = self._packed()
        for user in (self.vendedor, self.cajero, self.armador, self.repartidor, self.nadie):
            self._login(user)
            self.assertEqual(self.url_open("/deposito/reclamar").status_code, 403, user.login)
            res = self.url_open("/deposito/entrega/%s" % order.id, data={"csrf_token": "x"},
                                allow_redirects=False)
            self.assertIn(res.status_code, (400, 403), user.login)
        self.assertFalse(order.distri_stop_ids)
        # gerencia delivers like administración
        later = self._later(self.gerente)
        self.assertEqual(self._promise(later).driver_id, self.gerente)
        self.assertEqual(self.url_open("/deposito/reclamar").status_code, 200)

    # ------------------------------------------------------------ administración: several files

    def _upload_one(self, name, raw, mime, partner=None):
        """What comprobantes.js sends for each file of a multi-file upload."""
        data = {"cliente_id": str((partner or self.partner).id), "importes": "", "giros": "0"}
        return self._post("/comprobantes/subir", data,
                          files=[("archivos", (name, raw, mime))], page="/comprobantes")

    def test_admin_multi_upload(self):
        self._flag(key=PARAM_VERIFICACION)
        self._login(self.admin)
        page = self.url_open("/comprobantes?cliente_id=%s" % self.partner.id).text
        self.assertIn('data-comp-multi="1"', page)
        self.assertIn('data-comp-drop="1"', page)
        self.assertIn("arrastrá acá los comprobantes", page)
        self.assertIn('data-comp-q-name="%s"' % self.partner.name, page, "customer preselected")
        before = self._ledger()
        count = self.Message.search_count([("distri_upload", "=", True)])
        answers = []
        for name, raw, mime in (("WA0001.jpg", _png(), "image/png"),
                                ("WA0002.pdf", _pdf(), "application/pdf"),
                                ("WA0003.png", _png(), "image/png"),
                                ("nota.txt", b"no es un comprobante", "text/plain")):
            res = self._upload_one(name, raw, mime)
            self.assertEqual(res.status_code, 200, res.text)
            [result] = res.json()["results"]
            answers.append((result["name"], result["status"]))
            if result["status"] == "error":
                self.assertEqual(result["message"], FILE_WRONG_KIND)
        self.assertEqual(answers, [("WA0001.jpg", "nuevo"), ("WA0002.pdf", "nuevo"),
                                   ("WA0003.png", "nuevo"), ("nota.txt", "error")])
        new = self.Message.search([("distri_upload", "=", True)], order="id desc", limit=3)
        self.assertEqual(self.Message.search_count([("distri_upload", "=", True)]), count + 3)
        self.assertEqual(set(new.mapped("distri_partner_id")), {self.partner})
        self.assertEqual(set(new.mapped("distri_receipt_state")), {"pending"})
        self.assertEqual(set(new.mapped("distri_forwarded_by_id")), {self.admin})
        self.assertEqual(self._ledger(), before, "queued for the reading: nothing credited")
        # a file too big is refused on its own
        big = b"%PDF-1.4\n" + b"0" * (10 * 1024 * 1024 + 10)
        [result] = self._upload_one("escaneo.pdf", big, "application/pdf").json()["results"]
        self.assertEqual((result["status"], result["message"]), ("error", FILE_TOO_BIG))
        # links to it: the verification board and the customer screen
        self.assertIn('href="/comprobantes"', self.url_open("/verificacion").text)

    def test_admin_multi_upload_roles(self):
        self._flag(key=PARAM_VERIFICACION)
        for user in (self.admin, self.gerente, self.cajero, self.vendedor):
            self._login(user)
            self.assertIn('data-comp-multi="1"', self.url_open("/comprobantes").text, user.login)
        self._login(self.nadie)
        self.assertEqual(self.url_open("/comprobantes").status_code, 403)

    # ------------------------------------------------------------ the browser side (node)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/comprobantes_v129_37.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        expected = {"jpg": "", "pdf": "", "webpNoType": "", "heic": FILE_WRONG_KIND,
                    "txt": FILE_WRONG_KIND, "big": FILE_TOO_BIG}
        self.assertEqual(out["each"]["problems"], expected)
        self.assertEqual(out["depot"]["problems"], expected)
        each = out["each"]
        self.assertFalse(each["overlap"], "one request at a time")
        self.assertEqual(each["posted"], ["IMG-20261125-WA0001.jpg",
                                          "Comprobante de transferencia.pdf", "captura.webp"],
                         "the bad file is never sent")
        self.assertEqual(each["statuses"], ["nuevo", "error", "falla", "nuevo"],
                         "a failed request does not stop the others")
        self.assertIn("probá de nuevo", each["failedMessage"])
        self.assertEqual(each["empty"], 0)
