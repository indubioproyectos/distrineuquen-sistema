"""19.0.1.129.56 (owner, 27/11/2026): the counter sale closed on one screen
(«Nueva venta» on the PC, controllers/nueva_venta_pc.py) and the old entry
points to quotations and invoices hidden behind
distri_pedido.ocultar_presupuesto_factura (models/entradas_viejas.py).

Over HTTP, with the requests planilla.js sends:
- the whole counter sale: create → deliver → cash → the next sale;
- a transfer with its receipt, and «Cuenta corriente» (nothing paid now,
  then «Cobrar»);
- «Corregir la venta», the last-minute change before the delivery: the
  sale is cancelled by «Anular venta» (credit note, stock back), its lines
  come back and the changed sale is loaded as a new one;
- the flag: what each role stops seeing and what stays.
HARD RULE (as tests/test_v129_53_planilla.py): the ledger changes only by
the sale's own documents, and a corrected sale leaves the customer's
balance where it was. Synthetic data only."""
import json
import os
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.distri_pedido.models.entradas_viejas import (
    MENUS_OCULTOS, PARAM_OCULTAR, exento, hidden_root_ids)
from odoo.addons.distri_pedido.tests.test_v129_53_planilla import PlanillaCase, _png


@tagged("post_install", "-at_install", "distri_v129_56")
class TestV12956Mostrador(PlanillaCase):

    def _catalogo(self, cliente=None):
        res = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % (cliente or self.cliente).id)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.json()

    def _corregir(self, order):
        return self._post("/nueva-venta/planilla/corregir/%s" % order.id, {})

    # ------------------------------------------------------------ the screen

    def test_page_has_the_new_pieces(self):
        self._flags()
        self._login(self.admin)
        page = self.url_open("/nueva-venta/planilla").text
        for key in ('id="pl-pasos"', 'data-paso="entregada"', 'id="pl-saldo"', 'id="pl-corregir"',
                    'id="pl-corregir-si"', 'id="pl-imprimir"', 'id="pl-compartir"',
                    "Cuenta corriente", "Corregir la venta", "Alt+G"):
            self.assertIn(key, page)
        self.assertNotIn("Odoo", page, "no vendor name on the screen")
        cliente = self._catalogo()["cliente"]
        self.assertEqual(cliente["saldo"], 0, "the customer's balance next to his name")
        # the phone page still answers with its own layout
        phone = self.url_open("/nueva-venta")
        self.assertEqual(phone.status_code, 200)
        self.assertIn('id="nv-form"', phone.text)
        self.assertNotIn('id="pl-pasos"', phone.text)

    # ------------------------------------------------------------ the whole counter sale

    def test_counter_sale_cash_then_the_next_sale(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        total = order.amount_total
        self.assertEqual(order.state, "sale")
        v = self._venta(order)
        self.assertTrue(v["facturada"])
        self.assertEqual(v["residual"], total)
        self.assertFalse(v["entregado"])
        self.assertTrue(v["entrega"], "the delivery and the payment on the same screen")
        self.assertEqual(v["corregir"]["motivo"], "", "it can still be corrected")
        self.assertTrue(v["corregir"]["pasos"])
        self.assertEqual(v["remito"]["url"],
                         "/report/pdf/distri_pedido.remito_pedido_documento/%s" % order.id)
        self.assertTrue(v["remito"]["archivo"].endswith(".pdf"))
        res = self._entregar(order, efectivo="10500")
        self.assertTrue(res.json()["ok"], res.text)
        v = self._venta(order)
        self.assertTrue(v["entregado"])
        self.assertEqual(v["residual"], 0)
        self.assertIsNone(v["entrega"])
        self.assertIsNone(v["cobrar"])
        self.assertIsNone(v["corregir"], "nothing to correct once delivered")
        self.assertFalse(v["a_cuenta"])
        self.assertTrue(v["remito"], "the remito to print or share")
        self.assertEqual(self._assert_only_this_sale(snap, order), 0)
        # the next sale, on the same screen: another order, same rules
        snap = self._snap()
        otra = self._order_of(self._crear("mostrador",
                                          renglones=[{"id": self.p2.id, "cantidad": 2}]))
        self.assertNotEqual(otra, order)
        self.assertTrue(self._entregar(otra, efectivo="5000").json()["ok"])
        self.assertEqual(self._assert_only_this_sale(snap, otra), 0)
        self.assertEqual(self._catalogo()["cliente"]["saldo"], 0)

    def test_counter_sale_transfer_with_receipt(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        total = order.amount_total
        res = self._entregar(order, files=[("transferencia.png", _png(), "image/png")],
                             transferencia="yes", importe_transf="10500", comprobante="subir")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertIn("omprobante", res.json()["message"])
        v = self._venta(order)
        self.assertTrue(v["entregado"])
        self.assertEqual(v["residual"], total, "a receipt never credits by itself")
        self.assertEqual(v["en_camino"], total)
        self.assertIsNone(v["cobrar"], "the transfer on its way is not collected twice")
        msgs = self.env["distri.whatsapp.message"].sudo().search(
            [("distri_order_ref", "=", order.name), ("distri_upload", "=", True)])
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs.distri_receipt_state, "pending")
        self.assertEqual(self._assert_only_this_sale(snap, order), total)

    def test_counter_sale_on_account_then_cobrar(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        total = order.amount_total
        # «Cuenta corriente»: what planilla.js posts for it
        res = self._entregar(order, no_pago="1")
        self.assertTrue(res.json()["ok"], res.text)
        self.assertEqual(order.distri_stop_ids.state, "not_paid")
        v = self._venta(order)
        self.assertTrue(v["entregado"])
        self.assertEqual(v["residual"], total)
        self.assertEqual(v["en_camino"], 0)
        self.assertTrue(any("cuenta corriente" in p for p in v["pagos"]), v["pagos"])
        self.assertTrue(v["a_cuenta"], "nothing paid: «Cobrar» is not prefilled")
        self.assertEqual(v["cobrar"]["debe"], total, "«Cobrar» stays offered for later")
        self.assertEqual(self._assert_only_this_sale(snap, order), total,
                         "only the invoice: it is in his account")
        self.assertEqual(self._catalogo()["cliente"]["saldo"], total,
                         "the balance the next sale shows")
        res = self._post("/nueva-venta/planilla/cobrar/%s" % order.id,
                         {"efectivo": "10500", "destino": "factura"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIsNone(res.json()["venta"]["cobrar"])
        self.assertEqual(res.json()["venta"]["residual"], 0)
        self.assertEqual(self._assert_only_this_sale(snap, order), 0)

    # ------------------------------------------------------------ the last-minute change

    def test_last_minute_change_before_the_delivery(self):
        self._flags()
        self._login(self.admin)
        snap = self._snap()
        order = self._order_of(self._crear("mostrador"))
        invoice = order.invoice_ids
        self.assertEqual(invoice.state, "posted")
        res = self._corregir(order)
        self.assertEqual(res.status_code, 200, res.text)
        data = res.json()
        self.assertTrue(data["ok"])
        self.assertEqual(sorted((r["id"], r["cantidad"]) for r in data["renglones"]),
                         sorted([(self.p1.id, 2), (self.p2.id, 1)]),
                         "the lines come back to change them")
        self.assertIn(order.name, data["texto"])
        self.assertEqual(order.state, "cancel")
        self.assertIn("Corrección antes de entregar", order.distri_anulada_motivo)
        # the invoice is not edited nor reset: a credit note reverses it
        self.assertEqual(invoice.state, "posted")
        self.assertEqual(invoice.amount_residual, 0)
        refund = invoice.reversal_move_ids
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.state, "posted")
        # the goods are back: every delivered move has its return done
        out = order.picking_ids.filtered(lambda p: p.picking_type_code == "outgoing")
        back = order.picking_ids - out
        self.assertTrue(back and set(back.mapped("state")) == {"done"})
        self.assertEqual(sum(back.move_ids.mapped("quantity")),
                         sum(out.move_ids.mapped("quantity")))
        self.assertEqual(self._assert_only_this_sale(snap, order), 0,
                         "the customer's balance is where it was")
        v = self._venta(order)
        self.assertEqual(v["titulo"], "Venta anulada")
        self.assertIsNone(v["entrega"])
        self.assertIsNone(v["corregir"])
        # a second tap (or a retry after a lost answer) writes nothing more
        before = self.env["distri.promised.transfer"]._distri_ledger_fingerprint()
        again = self._corregir(order)
        self.assertEqual(again.status_code, 200, again.text)
        self.assertEqual(len(again.json()["renglones"]), 2)
        self.assertEqual(self.env["distri.promised.transfer"]._distri_ledger_fingerprint(), before)
        # the changed sale: one more gaseosa, no galletitas; a sale of its own
        nueva = self._order_of(self._crear("mostrador",
                                           renglones=[{"id": self.p1.id, "cantidad": 3}]))
        self.assertNotEqual(nueva, order)
        self.assertEqual(nueva.order_line.product_uom_qty, 3)
        self.assertEqual(nueva.amount_total, 12000)
        self.assertTrue(self._entregar(nueva, efectivo="12000").json()["ok"])
        self.assertEqual(self._venta(nueva)["residual"], 0)
        self.assertEqual(self._assert_only_this_sale(snap, nueva), 0)

    def test_corregir_is_refused_after_the_delivery_and_for_a_seller(self):
        self._flags()
        self._login(self.admin)
        order = self._order_of(self._crear("mostrador"))
        self.assertTrue(self._entregar(order, efectivo="10500").json()["ok"])
        before = self.env["distri.promised.transfer"]._distri_ledger_fingerprint()
        res = self._corregir(order)
        self.assertEqual(res.status_code, 400)
        self.assertIn("ya se entregó", res.json()["error"])
        self.assertEqual(order.state, "sale")
        self.assertEqual(self.env["distri.promised.transfer"]._distri_ledger_fingerprint(), before)
        # an order that waits for the packing is not a counter sale to correct
        armar = self._order_of(self._crear("armar"))
        self.assertIsNone(self._venta(armar)["corregir"])
        res = self._corregir(armar)
        self.assertEqual(res.status_code, 400)
        self.assertIn("no está esperando la entrega", res.json()["error"])
        self.assertEqual(armar.state, "sale")
        self._login(self.vendedor)
        quote = self._order_of(self._crear("presupuesto",
                                           renglones=[{"id": self.p1.id, "cantidad": 20}]))
        res = self._corregir(quote)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(quote.state, "draft")

    # ------------------------------------------------------------ the flag

    def _ocultar(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_OCULTAR, "1" if on else "0")

    def _menus(self, user):
        return set(self.env["ir.ui.menu"].with_user(user).load_menus(False))

    def test_flag_hides_the_old_entry_points_per_role(self):
        self._flags()
        company = {"company_id": self.env.company.id,
                   "company_ids": [Command.set(self.env.company.ids)]}
        gerente = new_test_user(self.env, login="gerente_v156", name="Gerente V156",
                                groups="base.group_user,distri_pedido.grupo_gerencia", **company)
        sistemas = new_test_user(
            self.env, login="sistemas_v156", name="Sistemas V156",
            groups="base.group_user,base.group_system,distri_pedido.grupo_administracion",
            **company)
        directa = new_test_user(
            self.env, login="directa_v156", name="Directa V156",
            groups="base.group_user,distri_pedido.grupo_administracion,"
                   "distri_pedido.grupo_facturacion_directa", **company)
        # the «PRESUPUESTOS» app of distri_tema (not installed in the tests)
        Menu = self.env["ir.ui.menu"]
        app = Menu.create({
            "name": "PRESUPUESTOS V156", "sequence": 31,
            "action": "ir.actions.act_window,%s" % self.env.ref(
                "sale.action_quotations_with_onboarding").id,
            "group_ids": [Command.link(self.env.ref("sales_team.group_sale_salesman").id)]})
        self.env["ir.model.data"].create({"module": "distri_tema", "name": "menu_presupuestos_raiz",
                                          "model": "ir.ui.menu", "res_id": app.id})
        ref = self.env.ref
        cotizaciones = ref("sale.menu_sale_quotations").id
        pedidos = ref("sale.menu_sale_order").id
        a_facturar = ref("sale.menu_sale_invoicing").id
        facturas = ref("account.menu_action_move_out_invoice_type").id
        notas = ref("account.menu_action_move_out_refund_type").id
        directas = ref("distri_pedido.menu_facturas_directas").id
        viejas = {cotizaciones, pedidos, a_facturar, facturas, app.id}
        self.assertEqual(len(MENUS_OCULTOS), 5)

        # off (production's default): nothing changes for anybody
        self._ocultar(False)
        for user in (self.admin, gerente, sistemas, directa):
            self.assertTrue(viejas <= self._menus(user), user.login)
        self.assertTrue({cotizaciones, pedidos, app.id} <= self._menus(self.vendedor))
        self.assertNotIn(directas, self._menus(self.admin), "only «Facturación directa»")
        self.assertIn(directas, self._menus(directa))
        self.assertFalse(hidden_root_ids(self.env(user=self.admin)))

        # on: the staff lose them; Gerencia and Sistemas keep everything
        self._ocultar(True)
        self.assertFalse(exento(self.admin))
        self.assertFalse(exento(self.vendedor))
        self.assertFalse(exento(directa))
        self.assertTrue(exento(gerente))
        self.assertTrue(exento(sistemas))
        for user in (self.admin, directa, self.vendedor):
            self.assertFalse(viejas & self._menus(user), user.login)
        for user in (gerente, sistemas):
            self.assertTrue(viejas <= self._menus(user), user.login)
        # what stays for the staff: credit notes, and direct invoicing its own way in
        self.assertIn(notas, self._menus(self.admin))
        self.assertIn(notas, self._menus(directa))
        self.assertIn(directas, self._menus(directa))
        self.assertNotIn(directas, self._menus(self.admin))
        self.assertEqual(hidden_root_ids(self.env(user=self.admin)), {app.id})
        self.assertFalse(hidden_root_ids(self.env(user=gerente)))

        # Inicio › «Todas las aplicaciones»
        self._login(self.admin)
        self.assertNotIn("PRESUPUESTOS V156", self.url_open("/tablero").text)
        self.assertIn("Nueva venta", self.url_open("/tablero").text)
        self._login(gerente)
        self.assertIn("PRESUPUESTOS V156", self.url_open("/tablero").text)

        # «Crear factura» on the order form
        order = self.env["sale.order"].create({
            "partner_id": self.cliente.id,
            "order_line": [Command.create({"product_id": self.p1.product_variant_id.id,
                                           "product_uom_qty": 1})]})
        self.assertTrue(order.with_user(self.admin).distri_sin_entradas_viejas)
        self.assertTrue(order.with_user(directa).distri_sin_entradas_viejas)
        self.assertFalse(order.with_user(gerente).distri_sin_entradas_viejas)
        self.assertFalse(order.with_user(sistemas).distri_sin_entradas_viejas)
        arch = self.env["sale.order"].get_view(view_type="form")["arch"]
        self.assertEqual(arch.count("or (distri_sin_entradas_viejas)"), 2,
                         "both «Crear factura» buttons")

        # reading what exists still works: the records, the actions (a link
        # or a bookmark) and the kit screens the audit uses
        self._login(self.admin)
        venta = self._order_of(self._crear("mostrador"))
        factura = venta.invoice_ids
        venta.with_user(self.admin).check_access("read")
        factura.with_user(self.admin).check_access("read")
        self.assertEqual(venta.with_user(self.vendedor).name, venta.name)
        for xmlid in ("sale.action_quotations_with_onboarding", "account.action_move_out_invoice"):
            action = self.env["ir.actions.act_window"].with_user(self.admin)._for_xml_id(xmlid)
            self.assertTrue(action["res_model"])
        self.assertTrue(self.env["sale.order"].with_user(self.admin).search_count(
            [("id", "=", venta.id)]))
        self.assertTrue(self.env["account.move"].with_user(self.admin).search_count(
            [("id", "=", factura.id)]))
        self.assertEqual(self.url_open("/pedidos/%s" % venta.id).status_code, 200)
        self.assertEqual(self.url_open("/facturas/%s" % factura.id).status_code, 200)
        # «Nueva venta» keeps its four exits, «Presupuesto» among them
        self.assertEqual(self.url_open("/nueva-venta/planilla").text.count('data-salida="'), 4)
        quote = self._order_of(self._crear("presupuesto"))
        self.assertEqual(quote.state, "draft")

        # «Facturación directa de servicios» works as before, and its list
        # holds the invoices that do not come from an order
        servicio = self.env["product.product"].create({
            "name": "Flete V156", "type": "service", "list_price": 1000.0,
            "taxes_id": [Command.clear()]})
        Move = self.env["account.move"].with_user(directa)
        flete = Move.create({
            "move_type": "out_invoice", "partner_id": self.cliente.id,
            "invoice_line_ids": [Command.create({"product_id": servicio.id, "quantity": 1,
                                                 "price_unit": 1000.0,
                                                 "tax_ids": [Command.clear()]})]})
        flete.action_post()
        self.assertEqual(flete.state, "posted")
        domain = [("move_type", "=", "out_invoice"), ("invoice_origin", "=", False)]
        action = ref("distri_pedido.action_facturas_directas")
        self.assertEqual(action.res_model, "account.move")
        listed = Move.search(domain + [("id", "in", (flete | factura).ids)])
        self.assertEqual(listed, flete, "the direct invoice, not the one of the order")
        # «Revertir»: the credit note of that invoice
        wizard = self.env["account.move.reversal"].with_user(directa).with_context(
            active_model="account.move", active_ids=flete.ids).create({
                "move_ids": [Command.set(flete.ids)], "reason": "Revertir V156",
                "journal_id": flete.journal_id.id})
        wizard.refund_moves()
        nota = flete.reversal_move_ids
        self.assertEqual(nota.move_type, "out_refund")
        nota.with_user(directa).action_post()
        self.assertEqual(nota.state, "posted")

        # off again: everything is back on the next menu load
        self._ocultar(False)
        self.env.invalidate_all()
        self.assertTrue(viejas <= self._menus(self.admin))
        self.assertFalse(order.with_user(self.admin).distri_sin_entradas_viejas)

    # ------------------------------------------------------------ the JS (node)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/planilla_v129_56.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        failed = [k for k, v in out.items() if v is not True]
        self.assertFalse(failed, "%s\n%s" % (failed, json.dumps(out, indent=1)))
        self.assertGreaterEqual(len(out), 30)
