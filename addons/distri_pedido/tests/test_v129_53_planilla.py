"""19.0.1.129.53 (owner, 26/11/2026): «Nueva venta» on the PC, «estilo
planilla» (controllers/nueva_venta_pc.py, views/nueva_venta_planilla.xml,
static/src/nueva_venta/planilla.js), behind nueva_venta + ui_v2.

The screen posts to the endpoints that already exist, so these tests go
through exactly those requests, as planilla.js sends them:
- the 4 exits through /nueva-venta/crear (its JSON now carries order_id) and
  the summary /nueva-venta/planilla/venta/<id>;
- «Mostrador» with the delivery and payment on the same screen:
  /deposito/entrega/<id>?mostrador=1 with cash, with a transfer receipt,
  with «Lo manda después», with a return;
- «Ya entregado» + «Cobrar» (/nueva-venta/planilla/cobrar/<id>, distri.cobro);
- prices from the customer's list (a hidden list too) and a seller who can
  neither see a hidden customer's prices nor reach the admin exits;
- the keyboard line entry, shortcuts and layout choice in Node.
HARD RULE: the ledger changes only by the new sale's own documents: every
new entry is of that customer, the entries that existed before keep their
receivable, and another customer's balance does not move.
Synthetic data only."""
import io
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from PIL import Image

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
SEED = [53000]


def _png():
    SEED[0] += 1
    seed = SEED[0]
    buffer = io.BytesIO()
    Image.new("RGB", (30 + seed % 50, 20 + seed // 50 % 50), (seed % 255, 53, 90)).save(
        buffer, format="PNG")
    return buffer.getvalue()


@tagged("post_install", "-at_install", "distri_v129_53")
class TestV12953Planilla(RepartoMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        cls.env["website"].search([]).company_id = cls.env.company
        Pricelist = cls.env["product.pricelist"]
        if not Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1):
            Pricelist.create({"name": LISTA_GENERAL, "company_id": False,
                              "currency_id": cls.env.company.currency_id.id})
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin = new_test_user(cls.env, login="admin_v153", name="Admin V153",
                                  groups="base.group_user,distri_pedido.grupo_administracion",
                                  **company)
        cls.vendedor = new_test_user(cls.env, login="vendedor_v153", name="Vendedor V153",
                                     groups="base.group_user,distri_pedido.grupo_vendedor",
                                     **company)
        cls.armador = new_test_user(cls.env, login="armador_v153", name="Armador V153",
                                    groups="base.group_user,distri_pedido.grupo_armador",
                                    **company)
        Tmpl = cls.env["product.template"]
        common = {"type": "consu", "is_published": True, "sale_ok": True,
                  "invoice_policy": "order", "taxes_id": [Command.clear()]}
        cls.p1 = Tmpl.create(dict(common, name="Gaseosa Planilla V153", list_price=4000.0))
        cls.p2 = Tmpl.create(dict(common, name="Galletitas Planilla V153", list_price=2500.0))
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacen Planilla V153", "company_type": "company",
            "distri_whatsapp": "2995001153", "street": "Mitre 153", "city": "Neuquén"})
        cls.otro = cls.env["res.partner"].create({
            "name": "Kiosco Ajeno V153", "company_type": "company",
            "distri_whatsapp": "2995002153"})
        # a hidden list with its own price for p1
        cls.oculta = Pricelist.create({
            "name": "Oculta Planilla V153", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id,
            "item_ids": [Command.create({"applied_on": "1_product",
                                         "product_tmpl_id": cls.p1.id,
                                         "compute_price": "fixed", "fixed_price": 3000.0})]})
        cls.distribuidor = cls.env["res.partner"].create({
            "name": "Distribuidora Planilla V153", "company_type": "company",
            "distri_whatsapp": "2995003153", "distri_lista_oculta": True,
            "property_product_pricelist": cls.oculta.id})
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def _flags(self, nueva_venta=True, ui_v2=True):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1" if nueva_venta else "0")
        Param.set_param(PARAM_UI_V2, "1" if ui_v2 else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self):
        page = self.url_open("/nueva-venta/planilla")
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1)

    def _post(self, path, data, files=None):
        data = dict(data, csrf_token=self._csrf())
        res = self.url_open(path, data=data, files=files, headers=FETCH, allow_redirects=False,
                            timeout=90)
        self.env.invalidate_all()
        return res

    def _crear(self, salida, cliente=None, renglones=None, **extra):
        """What planilla.js posts for «Cargar la venta»."""
        data = {"envio": os.urandom(16).hex(), "cliente_id": str((cliente or self.cliente).id),
                "renglones": json.dumps(renglones or [{"id": self.p1.id, "cantidad": 2},
                                                      {"id": self.p2.id, "cantidad": 1}]),
                "salida": salida, "entrega": "domicilio"}
        data.update(extra)
        return self._post("/nueva-venta/crear", data)

    def _venta(self, order):
        res = self.url_open("/nueva-venta/planilla/venta/%s" % order.id, headers=FETCH)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.json()

    def _order_of(self, res):
        self.assertEqual(res.status_code, 200, res.text[:500])
        data = res.json()
        self.assertTrue(data["ok"], data)
        return self.env["sale.order"].browse(data["order_id"])

    def _entregar(self, order, files=None, **values):
        """What planilla.js posts for «Entregar y cobrar»."""
        data = {"mostrador": "1", "presente": "1", "codigo_op": "code", "devolvio": "none",
                "transferencia": "no", "bultos": "1"}
        data.update(values)
        upload = [("comprobantes", (name, raw, mime)) for name, raw, mime in files or ()]
        return self._post("/deposito/entrega/%s?mostrador=1" % order.id, data,
                          files=upload or None)

    def _snap(self):
        """The ledger before the sale (see the module docstring)."""
        self.env.flush_all()
        cr = self.env.cr
        cr.execute("SELECT COALESCE(MAX(id), 0) FROM account_move")
        last_move = cr.fetchone()[0]
        return {"last_move": last_move, "old": self._receivable(last_move),
                "otro": self._partner_balance(self.otro),
                "cliente": self._partner_balance(self.cliente),
                "fingerprint": self.env["distri.promised.transfer"]._distri_ledger_fingerprint()}

    def _receivable(self, last_move):
        self.env.cr.execute("""
            SELECT COALESCE(SUM(l.balance), 0) FROM account_move_line l
              JOIN account_account a ON a.id = l.account_id
             WHERE a.account_type = 'asset_receivable' AND l.parent_state = 'posted'
               AND l.move_id <= %s""", [last_move])
        return round(float(self.env.cr.fetchone()[0]), 2)

    def _partner_balance(self, partner):
        self.env.cr.execute("""
            SELECT COALESCE(SUM(l.balance), 0) FROM account_move_line l
              JOIN account_account a ON a.id = l.account_id
             WHERE a.account_type = 'asset_receivable' AND l.parent_state = 'posted'
               AND l.partner_id = %s""", [partner.commercial_partner_id.id])
        return round(float(self.env.cr.fetchone()[0]), 2)

    def _assert_only_this_sale(self, snap, order):
        """Every entry created since `snap` is of the sale's customer and
        the receivable of the older ones did not move; the other customer's
        balance is the same. Returns the customer's balance change."""
        self.env.flush_all()
        new = self.env["account.move"].sudo().search([("id", ">", snap["last_move"])])
        commercial = order.partner_id.commercial_partner_id
        for move in new:
            receivable = move.line_ids.filtered(
                lambda l: l.account_id.account_type == "asset_receivable")
            if receivable:
                self.assertEqual(receivable.partner_id.commercial_partner_id, commercial,
                                 "%s is not of the sale's customer" % move.display_name)
        self.assertEqual(self._receivable(snap["last_move"]), snap["old"],
                         "no older entry changed its receivable")
        self.assertEqual(self._partner_balance(self.otro), snap["otro"])
        return round(self._partner_balance(self.cliente) - snap["cliente"], 2)

    # ------------------------------------------------------------ screen and flags

    def test_page_flags_and_device_switch(self):
        self._flags()
        self._login(self.admin)
        page = self.url_open("/nueva-venta/planilla")
        self.assertEqual(page.status_code, 200)
        for key in ('id="pl-form"', 'id="pl-filas"', 'id="pl-entrega-form"', 'id="pl-cobro-form"',
                    "planilla.js", "planilla.css", "Ver versión celular", "Atajos de teclado"):
            self.assertIn(key, page.text)
        self.assertEqual(page.text.count('data-salida="'), 4, "admin: the 4 exits")
        phone = self.url_open("/nueva-venta").text
        self.assertIn('id="nv-vista"', phone, "the PC switch script")
        self.assertIn("/nueva-venta/planilla?vista=planilla", phone)
        self.assertLess(phone.index('id="nv-vista"'), phone.index('rel="stylesheet"'),
                        "before any CSS")
        self._login(self.vendedor)
        page = self.url_open("/nueva-venta/planilla").text
        self.assertEqual(re.findall(r'data-salida="(\w+)"', page), ["armar", "presupuesto"])
        self._login(self.armador)
        self.assertEqual(self.url_open("/nueva-venta/planilla").status_code, 403)
        self._login(self.admin)
        self._flags(ui_v2=False)
        self.assertEqual(self.url_open("/nueva-venta/planilla").status_code, 404)
        self.assertNotIn('id="nv-vista"', self.url_open("/nueva-venta").text,
                         "flag off: the phone page as before")
        self._flags(nueva_venta=False)
        self.assertEqual(self.url_open("/nueva-venta/planilla").status_code, 404)

    # ------------------------------------------------------------ the 4 exits

    def test_armar_and_presupuesto_end_on_the_summary(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("armar"))
        self.assertEqual(order.state, "sale")
        self.assertEqual(order.partner_id, self.cliente)
        self.assertEqual(sorted(order.order_line.mapped("product_uom_qty")), [1, 2])
        v = self._venta(order)
        self.assertEqual(v["titulo"], "Pedido confirmado")
        self.assertIsNone(v["entrega"])
        self.assertIsNone(v["cobrar"], "no invoice until it is packed")
        self.assertIn("Pedido", [d["tipo"] for d in v["documentos"]])
        quote = self._order_of(self._crear("presupuesto"))
        self.assertEqual(quote.state, "draft")
        self.assertEqual(self._venta(quote)["titulo"], "Presupuesto guardado")
        self.assertEqual(self.env["distri.promised.transfer"]._distri_ledger_fingerprint(),
                         snap["fingerprint"], "neither one touches the ledger")

    def test_same_envio_twice_is_one_sale(self):
        self._flags()
        self._login(self.admin)
        envio = os.urandom(16).hex()
        first = self._order_of(self._crear("mostrador", envio=envio))
        again = self._order_of(self._crear("mostrador", envio=envio))
        self.assertEqual(first, again, "a retry after a failed step finds the same sale")

    def test_mostrador_cash_inline(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        total = order.amount_total
        self.assertEqual(total, 10500)
        v = self._venta(order)
        self.assertEqual(v["titulo"], "Venta en mostrador")
        self.assertFalse(v["entregado"])
        self.assertEqual(v["entrega"]["pedido"], total)
        self.assertEqual(len(v["entrega"]["lineas"]), 2)
        self.assertEqual(v["cobrar"]["debe"], total)
        res = self._entregar(order, efectivo="10500")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("entregado a", res.json()["message"])
        v = self._venta(order)
        self.assertTrue(v["entregado"])
        self.assertIsNone(v["entrega"])
        self.assertIsNone(v["cobrar"], "paid in full")
        tipos = [d["tipo"] for d in v["documentos"]]
        self.assertEqual(tipos[:1], ["Factura"])
        self.assertIn("Remito", tipos)
        remito = [d for d in v["documentos"] if d["tipo"] == "Remito"][0]
        self.assertEqual(remito["url"],
                         "/report/pdf/distri_pedido.remito_pedido_documento/%s" % order.id)
        self.assertTrue(any(p.startswith("Efectivo $ 10.500") for p in v["pagos"]), v["pagos"])
        stop = order.distri_stop_ids
        self.assertEqual(stop.route_id.kind, "depot", "the depot box, as the depot form")
        self.assertEqual(stop.cash_amount, 10500)
        self.assertEqual(self._assert_only_this_sale(snap, order), 0,
                         "invoice and its cash: the customer's balance is back where it was")

    def test_mostrador_transfer_with_receipts(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        res = self._entregar(order, files=[("a.png", _png(), "image/png"),
                                           ("b.png", _png(), "image/png")],
                             transferencia="yes", importe_transf="10500", comprobante="subir")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("2 comprobantes subidos", res.json()["message"])
        msgs = self.env["distri.whatsapp.message"].sudo().search(
            [("distri_order_ref", "=", order.name), ("distri_upload", "=", True)])
        self.assertEqual(len(msgs), 2)
        self.assertEqual(set(msgs.mapped("distri_receipt_state")), {"pending"})
        self.assertEqual(self._assert_only_this_sale(snap, order), order.amount_total,
                         "a receipt never credits by itself: only the invoice moved")

    def test_mostrador_bad_file_refuses_and_keeps_the_sale(self):
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("mostrador"))
        res = self._entregar(order, files=[("nota.txt", b"hola", "text/plain")],
                             transferencia="yes", importe_transf="10500", comprobante="subir")
        self.assertEqual(res.status_code, 400)
        self.assertIn("nota.txt", res.json()["error"])
        self.assertIsNotNone(self._venta(order)["entrega"], "still deliverable: try again")

    def test_mostrador_lo_manda_despues(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        res = self._entregar(order, transferencia="yes", importe_transf="10500",
                             comprobante="despues")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("queda a tu cargo", res.json()["message"])
        promise = self.env["distri.promised.transfer"].sudo().search([("order_id", "=", order.id)])
        self.assertTrue(promise.distri_user_charge)
        self.assertEqual(promise.stop_id.route_id.driver_id, self.admin)
        v = self._venta(order)
        self.assertTrue(any("Transferencia" in p for p in v["pagos"]))
        self.assertIsNone(v["cobrar"], "the transfer on its way is not offered again")
        self.assertEqual(self._assert_only_this_sale(snap, order), order.amount_total)

    def test_mostrador_with_a_return_and_mixed_payment(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        line = order.order_line.filtered(lambda l: l.product_id == self.p1.product_variant_id)
        # returns 1 gaseosa (4000): owes 6500, pays 3000 cash + 3500 transfer later
        res = self._entregar(order, devolvio="yes", recibido="1",
                             efectivo="3000", transferencia="yes", importe_transf="3500",
                             comprobante="despues", **{"dev_%s" % line.id: "1",
                                                       "motivo_%s" % line.id: "Vencida"})
        self.assertEqual(res.status_code, 200, res.text)
        stop = order.distri_stop_ids
        self.assertEqual(stop.return_ids.quantity, 1)
        self.assertEqual(stop.return_ids.reason, "Vencida")
        self.assertEqual(stop.cash_amount, 3000)
        change = self._assert_only_this_sale(snap, order)
        self.assertLessEqual(change, order.amount_total - 3000,
                             "only the invoice, its cash and its own credit note")

    def test_mostrador_return_without_reason_is_refused_inline(self):
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("mostrador"))
        line = order.order_line[:1]
        res = self._entregar(order, devolvio="yes", efectivo="100",
                             **{"dev_%s" % line.id: "1"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("motivo", res.json()["error"])

    def test_ya_entregado_and_cobrar_inline(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("entregado", fecha=str(fields.Date.today()),
                                           motivo="Ya entregado: lo llevó el dueño"))
        self.assertTrue(order.distri_entregado_fuera)
        v = self._venta(order)
        self.assertEqual(v["titulo"], "Venta cargada como entregada")
        self.assertTrue(v["entregado"])
        self.assertIsNone(v["entrega"])
        self.assertEqual(v["cobrar"]["debe"], order.amount_total)
        # more than the debt «a cuenta»: a question, nothing written
        before = self.env["distri.promised.transfer"]._distri_ledger_fingerprint()
        res = self._post("/nueva-venta/planilla/cobrar/%s" % order.id,
                         {"efectivo": "99999", "destino": "cuenta"})
        self.assertEqual(res.status_code, 409, res.text)
        self.assertEqual(res.json()["campo"], "deuda_ok")
        self.assertEqual(self.env["distri.promised.transfer"]._distri_ledger_fingerprint(), before)
        res = self._post("/nueva-venta/planilla/cobrar/%s" % order.id,
                         {"efectivo": "10500", "destino": "factura"})
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertIn("Cobrado $ 10.500", data["texto"])
        self.assertIsNone(data["venta"]["cobrar"])
        cobro = self.env["distri.cobro"].sudo().browse(data["cobro_id"])
        self.assertEqual(cobro.invoice_id, order.invoice_ids)
        self.assertTrue(cobro.cash_collection_id, "not a cashier: his /cobro box, as /cobro")
        self.assertEqual(self._assert_only_this_sale(snap, order), 0)

    def test_cobrar_transfer_needs_its_receipt(self):
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("entregado", fecha=str(fields.Date.today()),
                                           motivo="Ya entregado"))
        before = self.env["distri.promised.transfer"]._distri_ledger_fingerprint()
        res = self._post("/nueva-venta/planilla/cobrar/%s" % order.id,
                         {"importe_transf_1": "10500"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("omprobante", res.json()["error"])
        self.assertEqual(self.env["distri.promised.transfer"]._distri_ledger_fingerprint(), before)

    # ------------------------------------------------------------ prices and permissions

    def test_prices_from_the_customer_list_and_hidden_list(self):
        self._flags()
        self._login(self.admin)
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.distribuidor.id).json()
        precios = {p["id"]: p["precio"] for p in data["productos"]}
        self.assertEqual(precios[self.p1.id], 3000, "his hidden list")
        self.assertEqual(data["cliente"]["lista"], self.oculta.name)
        order = self._order_of(self._crear("presupuesto", cliente=self.distribuidor,
                                           renglones=[{"id": self.p1.id, "cantidad": 3}]))
        self.assertEqual(order.order_line.price_unit, 3000, "the order agrees with the screen")
        general = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
        self.assertEqual({p["id"]: p["precio"] for p in general["productos"]}[self.p1.id], 4000)

    def test_seller_cannot_see_hidden_prices_nor_admin_steps(self):
        self._flags()
        self._login(self.vendedor)
        res = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.distribuidor.id)
        self.assertEqual(res.status_code, 403)
        self.assertNotIn("3000", res.text)
        found = self.url_open("/pedido/clientes?q=Distribuidora Planilla").json()
        self.assertFalse(found, "a hidden customer is not offered to a seller")
        res = self._crear("presupuesto", cliente=self.distribuidor)
        self.assertEqual(res.status_code, 403)
        res = self._crear("mostrador")
        self.assertEqual(res.status_code, 403)
        self.assertIn("solo para administración", res.json()["error"])
        # his own sale: no delivery for him, and the summary is his
        # (/pedido's minimum purchase applies to a seller: $ 70.000)
        order = self._order_of(self._crear("presupuesto",
                                           renglones=[{"id": self.p1.id, "cantidad": 20}]))
        self.assertIsNone(self._venta(order)["entrega"])
        res = self._entregar(order)
        self.assertEqual(res.status_code, 403)

    def test_other_users_sale_is_not_reachable(self):
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("presupuesto", cliente=self.otro))
        self._login(self.armador)
        self.assertEqual(self.url_open("/nueva-venta/planilla/venta/%s" % order.id,
                                       headers=FETCH).status_code, 403)

    # ------------------------------------------------------------ the JS (node)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/planilla_v129_53.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        failed = [k for k, v in out.items() if v is not True]
        self.assertFalse(failed, "%s\n%s" % (failed, json.dumps(out, indent=1)))
        self.assertGreaterEqual(len(out), 20)
