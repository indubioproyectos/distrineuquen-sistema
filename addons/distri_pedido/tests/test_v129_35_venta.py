"""19.0.1.129.35: the rest of the plan's Etapa 4 (models/anular_venta.py,
controllers/anular_venta.py, models/nueva_venta.py, controllers/
nueva_venta.py, models/rearmar.py, models/factura_sin_armar.py).

- «Anular venta» in one step: compared with the old manual path (invoice
  cancelled, remito returned, order cancelled: scripts/correcciones/
  painen_cancelar_s00212_body.py), it gives the same customer balance and
  the same stock, through a credit note of Odoo's standard reversal. It
  refuses with money reconciled or a route not settled, and then nothing
  changes.
- «Aprobar» on the order screen: the backend's «Confirmar» for whoever may
  confirm the order, with the backend wizard's WhatsApp question.
- «Nueva venta»: the WhatsApp question, «Va por encomienda», «Repetir»,
  «Nombre y apellido» and the GPS, and its tile in place of «Tomar un
  pedido» and «Venta en mostrador».
- «Ya lo armé» and «Rearmar» as corrections with a reason, the two «Ya lo
  armé» answers apart and confirmed by name.
Every action but «Anular venta» leaves the ledger exactly as it was
(entries, payments, reconciliations and the receivable total). With the
flags off nothing of this is reachable. Synthetic data only.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers import tablero
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, role_nav, screen_parents
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.anular_venta import CHANGED, puede_anular
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.pedido_whatsapp import PARAM_REQUIRE
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'(?:name="csrf_token" value|name="csrf-token" content)="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio" value="([^"]+)"')
HUELLA_RE = re.compile(r'name="huella" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_35")
class TestV12935Venta(PedidoAbiertoMixin, RepartoMixin, AccountTestInvoicingHttpCommon):

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

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_v135", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v135", "distri_pedido.grupo_gerencia")
        cls.vendedor = user("vendedor_v135", "distri_pedido.grupo_vendedor")
        cls.otro_vendedor = user("otro_vendedor_v135", "distri_pedido.grupo_vendedor")
        cls.externo = user("externo_v135", "distri_pedido.grupo_vendedor")
        cls.externo.sudo().distri_tipo_vendedor = "externo"
        cls.armador = user("armador_v135", "distri_pedido.grupo_armador")
        Tmpl = cls.env["product.template"]
        cls.p1 = Tmpl.create({"name": "Gaseosa V135", "type": "consu", "is_storable": True,
                              "list_price": 40000.0, "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.p2 = Tmpl.create({"name": "Galletitas V135", "type": "consu", "list_price": 15000.0,
                              "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.stock = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1).lot_stock_id
        cls.env["stock.quant"]._update_available_quantity(
            cls.p1.product_variant_id, cls.stock, 100.0)
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacen Venta V135", "company_type": "company",
            "distri_whatsapp": "2995001135", "street": "Mitre 135", "city": "Neuquén",
            "user_id": cls.vendedor.id})
        cls.sin_wa = cls.env["res.partner"].create({
            "name": "Kiosco Sin WhatsApp V135", "company_type": "company",
            "street": "Roca 35", "city": "Neuquén", "phone": "299 4777888",
            "user_id": cls.vendedor.id})
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def _flags(self, nueva=True, ui=True):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1" if nueva else "0")
        Param.set_param(PARAM_UI_V2, "1" if ui else "0")

    def _require_wa(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_REQUIRE, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self, url):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1), page.text

    def _ledger(self, partner=None):
        """The receivable total and the counts of entries, payments and
        reconciliations (models/comprobante_pendiente.py), plus the customer's
        own receivable."""
        self.env.flush_all()
        partner = partner or self.cliente
        partner.invalidate_recordset(["credit"])
        return (self.env["distri.promised.transfer"]._distri_ledger_fingerprint(),
                round(partner.commercial_partner_id.credit, 2))

    def _quotation(self, partner=None, user=None, q1=2, q2=3):
        Order = self.env["sale.order"].with_user(user or self.admin)
        return Order.create({
            "partner_id": (partner or self.cliente).id,
            "order_line": [
                Command.create({"product_id": self.p1.product_variant_id.id,
                                "product_uom_qty": q1, "price_unit": 40000.0,
                                "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.p2.product_variant_id.id,
                                "product_uom_qty": q2, "price_unit": 15000.0,
                                "tax_ids": [Command.clear()]}),
            ]})

    def _packed_invoiced(self, partner=None):
        """Confirmed, invoiced by hand, then «Ya lo armé»: a posted invoice and
        a validated remito (the depot delivery of what was ordered)."""
        order = self._quotation(partner).sudo()
        order.action_confirm()
        invoice = order.with_context(distri_facturar_sin_armar=True)._create_invoices()
        invoice.action_post()
        self.assertEqual(order.with_user(self.armador).distri_ya_armado(
            2, motivo="Facturado antes de armar (test)"), "")
        self.assertTrue(order.picking_ids.filtered(lambda p: p.state == "done"))
        return order

    def _invoice(self, order):
        return order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")

    def _on_hand(self):
        return self.env["stock.quant"]._get_available_quantity(
            self.p1.product_variant_id, self.stock)

    def _statement(self, partner=None):
        rows = (partner or self.cliente).sudo()._distri_statement_rows()
        return round(rows[-1]["balance"], 2) if rows else 0.0

    def _pay(self, invoice):
        self.env["account.payment.register"].with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "journal_id": self.company_data["default_journal_bank"].id,
                "payment_date": fields.Date.today(),
            }).action_create_payments()

    def _anular(self, order, motivo="El cliente se arrepintió", huella=None):
        csrf, html = self._csrf("/pedidos/%s/anular" % order.id)
        if huella is None:
            huella = HUELLA_RE.search(html).group(1)
        res = self.url_open("/pedidos/%s/anular" % order.id, data={
            "csrf_token": csrf, "motivo": motivo, "huella": huella}, allow_redirects=False)
        self.env.invalidate_all()
        return res

    # ------------------------------------------------------------ flags off

    def test_flags_off_nothing_reachable_and_nothing_changes(self):
        order = self._packed_invoiced()
        quote = self._quotation()
        self._login(self.admin)
        for nueva, ui in ((False, True), (True, False), (False, False)):
            self._flags(nueva, ui)
            self.assertEqual(self.url_open("/pedidos/%s/anular" % order.id).status_code, 404)
            res = self.url_open("/pedidos/%s/aprobar" % quote.id, data={
                "csrf_token": "x"}, allow_redirects=False)
            self.assertIn(res.status_code, (400, 404))
        self._flags(False, True)
        html = self.url_open("/pedidos/%s" % quote.id).text
        self.assertNotIn("/aprobar", html)
        self.assertNotIn("Anular venta", html)
        self.assertIn("Para aprobarlo, abrilo en el sistema", html, "as in 129.34")
        self.assertEqual(quote.state, "draft")
        self.assertEqual(order.state, "sale")
        with self.assertRaisesRegex(UserError, "no está activo"):
            order.with_user(self.admin).distri_anular_venta("motivo")
        # tiles, depot, packing and «Rearmar» as in 129.34
        keys = [t["key"] for t in tablero.staff_tiles(self.env(user=self.vendedor))]
        self.assertIn("pedido", keys)
        self.assertNotIn("nuevaventa", keys)
        depot = self.url_open("/deposito").text
        self.assertIn('href="/deposito/mostrador"', depot)
        self.assertNotIn('href="/nueva-venta"', depot)
        self.assertIn("pedido", [i["key"] for i in role_nav(self.env(user=self.vendedor), "")])
        other = self._quotation().sudo()
        other.action_confirm()
        inv = other.with_context(distri_facturar_sin_armar=True)._create_invoices()
        inv.action_post()
        self.assertEqual(other.with_user(self.armador).distri_ya_armado(2), "", "no reason needed")
        action = other.with_user(self.admin).action_distri_rearmar()
        self.assertEqual(action["res_model"], "sale.order", "no reason asked")

    # ------------------------------------------------------------ «Anular venta»

    def test_anular_venta_against_the_old_manual_path(self):
        self._flags()
        new = self._packed_invoiced()
        old = self._packed_invoiced()
        invoice = self._invoice(new)
        lines_before = [(l.id, l.balance, l.amount_currency) for l in invoice.line_ids]
        residual = invoice.amount_residual
        self.assertEqual(residual, new.amount_total)
        on_hand = self._on_hand()
        statement = self._statement()
        ledger, credit = self._ledger()

        # the old manual path (S00212's script, with the invoice cancelled)
        old_inv = self._invoice(old).with_context(distri_skip_cancel_notice=True)
        old_inv.button_draft()
        old_inv.button_cancel()
        remito = old.picking_ids.filtered(lambda p: p.state == "done")
        wiz = self.env["stock.return.picking"].with_context(
            active_id=remito.id, active_ids=remito.ids, active_model="stock.picking").create(
            {"picking_id": remito.id})
        for line in wiz.product_return_moves:
            line.quantity = line.move_id.quantity
        ret = wiz._create_return()
        for move in ret.move_ids:
            move.quantity = move.product_uom_qty
            move.picked = True
        ret.button_validate()
        old._action_cancel()
        _ledger_old, credit_old = self._ledger()
        statement_old = self._statement()
        on_hand_old = self._on_hand()
        self.assertEqual(credit_old, round(credit - residual, 2))
        self.assertEqual(on_hand_old, on_hand + 2)

        # the screen says exactly what will happen
        self._login(self.admin)
        html = self.url_open("/pedidos/%s" % new.id).text
        self.assertIn('href="/pedidos/%s/anular"' % new.id, html)
        page = self.url_open("/pedidos/%s/anular" % new.id)
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-parent="/pedidos/%s"' % new.id, page.text)
        self.assertEqual(screen_parents("/pedidos/%s/anular" % new.id), ("/pedidos/<id>",))
        plan = new._distri_anular_plan()
        self.assertFalse(plan["refusals"])
        for step in plan["steps"]:
            self.assertIn(step, page.text)
        self.assertTrue(any("nota de crédito" in s and invoice.name in s for s in plan["steps"]))
        self.assertTrue(any("Vuelve al stock" in s and "2 " in s for s in plan["steps"]))
        self.assertTrue(any("El saldo del cliente baja" in s for s in plan["steps"]))

        res = self._anular(new, motivo="  El cliente   se arrepintió ")
        self.assertEqual(res.status_code, 303, res.text[:400])
        self.assertTrue(res.headers["Location"].endswith("/pedidos/%s?hecho=anulada" % new.id))
        self.assertEqual(new.state, "cancel")
        self.assertEqual(new.distri_anulada_motivo, "El cliente se arrepintió")
        self.assertEqual(new.distri_anulada_por_id, self.admin)
        # the invoice: still posted, never edited, reversed by its credit note
        self.assertEqual(invoice.state, "posted")
        self.assertEqual([(l.id, l.balance, l.amount_currency) for l in invoice.line_ids],
                         lines_before, "the posted invoice is not touched")
        self.assertEqual(invoice.payment_state, "reversed")
        self.assertEqual(invoice.amount_residual, 0.0)
        refund = invoice.reversal_move_ids
        self.assertEqual(len(refund), 1)
        self.assertEqual(refund.move_type, "out_refund")
        self.assertEqual(refund.state, "posted")
        self.assertEqual(refund.amount_total, invoice.amount_total, "credit note = invoice")
        self.assertEqual(refund.journal_id, invoice.journal_id)
        self.assertIn("El cliente se arrepintió", refund.ref)
        # money: the same as the old path, and only the reversal was written
        ledger_after, credit_after = self._ledger()
        self.assertEqual(credit_after, round(credit - residual - residual, 2),
                         "both sales are cancelled now")
        self.assertEqual(round(credit - credit_after, 2), round(credit - credit_old + residual, 2))
        self.assertEqual(round(statement_old - self._statement(), 2), residual,
                         "the statement goes down by the residual, as the old path")
        self.assertEqual(round(statement - statement_old, 2), residual)
        self.assertEqual(ledger_after[1], _ledger_old[1] + 1, "one entry more: the credit note")
        self.assertEqual(ledger_after[2], _ledger_old[2], "no payment")
        self.assertEqual(ledger_after[3], _ledger_old[3] + 1, "one reconciliation: refund-invoice")
        # stock: back, as with the old path
        self.assertEqual(self._on_hand(), on_hand_old + 2)
        returns = self.env["stock.picking"].search(
            [("origin", "=like", "%%venta anulada %s" % new.name)])
        self.assertEqual(returns.state, "done")
        self.assertTrue(returns.name.startswith("DEV-"), returns.name)
        self.assertEqual(sorted(returns.move_ids.mapped("quantity")), [2.0, 3.0])
        # the chatter: reason and what was done
        body = new.message_ids.filtered(lambda m: "Venta anulada" in (m.body or ""))[:1].body
        self.assertIn("Venta anulada", body)
        self.assertIn("El cliente se arrepintió", body)
        self.assertIn(refund.name, body)
        self.assertTrue(self.cliente.message_ids.filtered(
            lambda m: "anulada" in (m.body or "") and new.name in (m.body or "")))
        self.assertIn("Listo: la venta quedó anulada.",
                      self.url_open("/pedidos/%s?hecho=anulada" % new.id).text)
        # once is enough
        self.assertIn("ya está anulada", new._distri_anular_plan()["refusals"][0])

    def test_anular_refuses_payments_and_open_routes(self):
        self._flags()
        self._login(self.admin)
        paid = self._packed_invoiced()
        self._pay(self._invoice(paid))
        routed = self._packed_invoiced()
        route = self._route(routed, on_road=False)
        before = self._ledger()
        on_hand = self._on_hand()
        for order, text in ((paid, "tiene pagos"), (routed, "todavía no se rindió")):
            page = self.url_open("/pedidos/%s/anular" % order.id)
            self.assertEqual(page.status_code, 200)
            self.assertIn(text, page.text)
            self.assertIn("No se cambió nada", page.text)
            self.assertNotIn('name="motivo"', page.text, "no form to confirm")
            res = self._anular(order, huella="")
            self.assertEqual(res.status_code, 400)
            self.assertIn(text, res.text)
            self.assertEqual(order.state, "sale")
            with self.assertRaisesRegex(UserError, text):
                order.with_user(self.admin).distri_anular_venta("Motivo de prueba")
        self.assertEqual(self._ledger(), before, "nothing unreconciled, nothing reversed")
        self.assertEqual(self._on_hand(), on_hand)
        self.assertEqual(self._invoice(paid).payment_state, "paid")
        # once the route is settled it can go
        route.sudo().state = "settled"
        self.assertFalse(routed._distri_anular_plan()["refusals"])

    def test_anular_permissions_reason_and_changed_plan(self):
        self._flags()
        order = self._packed_invoiced()
        before = self._ledger()
        self.assertTrue(puede_anular(self.admin))
        self.assertTrue(puede_anular(self.gerente))
        self.assertFalse(puede_anular(self.vendedor))
        for user in (self.vendedor, self.armador):
            self._login(user)
            self.assertEqual(self.url_open("/pedidos/%s/anular" % order.id).status_code, 403)
            with self.assertRaises(AccessError):
                order.with_user(user).distri_anular_venta("Motivo de prueba")
        self._login(self.vendedor)
        page = self.url_open("/pedidos/%s" % order.id)
        if page.status_code == 200:
            self.assertNotIn("/anular", page.text)
        self._login(self.admin)
        res = self._anular(order, motivo=" ")
        self.assertEqual(res.status_code, 400)
        self.assertIn("motivo", res.text)
        res = self._anular(order, huella="otra")
        self.assertEqual(res.status_code, 400)
        self.assertIn(CHANGED[:30], res.text)
        self.assertEqual(order.state, "sale")
        self.assertEqual(self._ledger(), before)
        # gerencia can
        self._login(self.gerente)
        self.assertEqual(self._anular(order).status_code, 303)
        self.assertEqual(order.state, "cancel")

    def test_anular_a_quotation_and_a_draft_invoice(self):
        self._flags()
        quote = self._quotation()
        before = self._ledger()
        plan = quote._distri_anular_plan()
        self.assertFalse(plan["refusals"])
        self.assertTrue(any("presupuesto" in s for s in plan["steps"]))
        quote.with_user(self.admin).distri_anular_venta("No lo quiso", huella=plan["huella"])
        self.assertEqual(quote.state, "cancel")
        draft_order = self._quotation().sudo()
        draft_order.action_confirm()
        draft = draft_order.with_context(distri_facturar_sin_armar=True)._create_invoices()
        self.assertEqual(draft.state, "draft")
        draft_order.with_user(self.admin).distri_anular_venta("Se cargó dos veces")
        self.assertEqual(draft.state, "cancel", "sale's own cancel")
        self.assertFalse(draft.reversal_move_ids)
        ledger, credit = self._ledger()
        self.assertEqual(credit, before[1], "nothing posted, nothing to reverse")
        self.assertEqual(ledger[2:], before[0][2:], "no payment, no reconciliation")

    # ------------------------------------------------------------ «Aprobar»

    def _aprobar(self, order, **data):
        csrf, _html = self._csrf("/pedidos/%s" % order.id)
        data["csrf_token"] = csrf
        res = self.url_open("/pedidos/%s/aprobar" % order.id, data=data, allow_redirects=False)
        self.env.invalidate_all()
        return res

    def test_aprobar_with_and_without_whatsapp(self):
        self._flags()
        self._require_wa(True)
        before = self._ledger()
        self._login(self.admin)
        quote = self._quotation()
        html = self.url_open("/pedidos/%s" % quote.id).text
        self.assertIn('action="/pedidos/%s/aprobar"' % quote.id, html)
        self.assertNotIn("Para aprobarlo, abrilo en el sistema", html)
        self.assertNotIn('name="area"', html, "the customer has a WhatsApp")
        res = self._aprobar(quote)
        self.assertEqual(res.status_code, 303, res.text[:400])
        self.assertEqual(quote.state, "sale")
        self.assertIn("Listo: el pedido quedó aprobado.",
                      self.url_open(res.headers["Location"]).text)
        self.assertTrue(quote.message_ids.filtered(
            lambda m: "Aprobado por" in (m.body or "")))
        # no WhatsApp on file: the question of the backend wizard
        quote = self._quotation(self.sin_wa)
        html = self.url_open("/pedidos/%s" % quote.id).text
        self.assertIn('name="area"', html)
        self.assertIn('value="299"', html, "the phone on file is offered")
        self.assertNotIn("Aprobar sin WhatsApp", html, "required: no skip")
        res = self._aprobar(quote)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Falta el WhatsApp", res.text)
        res = self._aprobar(quote, sin_whatsapp="1")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(quote.state, "draft")
        res = self._aprobar(quote, area="299", numero="12")
        self.assertEqual(res.status_code, 400)
        self.assertIn("10", res.text)
        res = self._aprobar(quote, area="0299", numero="154777888")
        self.assertEqual(res.status_code, 303, res.text[:400])
        self.assertEqual(quote.state, "sale")
        self.assertEqual(self.sin_wa.distri_whatsapp, "+5492994777888")
        # not required: «Aprobar sin WhatsApp»
        self._require_wa(False)
        partner = self.env["res.partner"].create({"name": "Otro Sin WA V135"})
        quote = self._quotation(partner)
        self.assertIn("Aprobar sin WhatsApp", self.url_open("/pedidos/%s" % quote.id).text)
        self.assertEqual(self._aprobar(quote, sin_whatsapp="1").status_code, 303)
        self.assertEqual(quote.state, "sale")
        self.assertFalse(partner.distri_whatsapp)
        self.assertTrue(quote.message_ids.filtered(
            lambda m: "Confirmado sin WhatsApp" in (m.body or "")))
        self.assertEqual(self._ledger()[0], before[0], "no entry, payment or reconcile")

    def test_aprobar_who_and_blocks(self):
        self._flags()
        self._require_wa(False)
        mine = self._quotation(user=self.vendedor)
        self.assertEqual(mine.user_id, self.vendedor)
        otro_cliente = self.env["res.partner"].create({
            "name": "Cliente Otro Vendedor V135", "distri_whatsapp": "2995009135",
            "user_id": self.otro_vendedor.id})
        theirs = self._quotation(otro_cliente)
        theirs.sudo().user_id = self.otro_vendedor
        # the seller approves his own, as the backend «Confirmar» lets him
        self._login(self.vendedor)
        self.assertEqual(self._aprobar(mine).status_code, 303)
        self.assertEqual(mine.state, "sale")
        # the external seller cannot reach another seller's order
        self._login(self.externo)
        self.assertEqual(self.url_open("/pedidos/%s" % theirs.id).status_code, 404)
        csrf, _html = self._csrf("/nueva-venta")
        res = self.url_open("/pedidos/%s/aprobar" % theirs.id, data={"csrf_token": csrf},
                            allow_redirects=False)
        self.assertIn(res.status_code, (400, 403, 404))
        self.assertEqual(theirs.state, "draft")
        # the packer has no order screen
        self._login(self.armador)
        self.assertEqual(self.url_open("/pedidos/%s" % theirs.id).status_code, 403)
        # blocks: generic customer, held
        self._login(self.admin)
        generic = self.env["res.partner"].search([("distri_generic_partner", "=", True)], limit=1)
        if generic:
            quote = self._quotation(generic)
            html = self.url_open("/pedidos/%s" % quote.id).text
            self.assertIn("Asigná el cliente para aprobarlo.", html)
            self.assertNotIn('action="/pedidos/%s/aprobar"' % quote.id, html)
            self.assertEqual(self._aprobar(quote).status_code, 400)
            self.assertEqual(quote.state, "draft")
        held = self._quotation()
        held.sudo().distri_retenido = True
        self.assertIn("retenido", self.url_open("/pedidos/%s" % held.id).text)
        self.assertEqual(self._aprobar(held).status_code, 400)
        self.assertEqual(held.state, "draft")

    # ------------------------------------------------------------ «Nueva venta»

    def _post(self, salida, partner=None, cart=None, **extra):
        csrf, html = self._csrf("/nueva-venta")
        data = {"csrf_token": csrf, "envio": ENVIO_RE.search(html).group(1),
                "cliente_id": str((partner or self.cliente).id),
                "renglones": json.dumps(cart or [{"id": self.p1.id, "cantidad": 2},
                                                 {"id": self.p2.id, "cantidad": 3}]),
                "salida": salida, "entrega": "domicilio", "telefono": "",
                "domicilio": "Mitre 135, Neuquén"}
        data.update(extra)
        res = self.url_open("/nueva-venta/crear", data=data, headers=FETCH,
                            allow_redirects=False)
        self.env.invalidate_all()
        return res

    def _order_of(self, res):
        self.assertEqual(res.status_code, 200, res.text[:500])
        return self.env["sale.order"].browse(int(re.search(r"/(\d+)", res.json()["url"]).group(1)))

    def test_nueva_venta_whatsapp_question(self):
        self._flags()
        self._require_wa(True)
        before = self._ledger(self.sin_wa)
        self._login(self.vendedor)
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.sin_wa.id).json()
        self.assertTrue(data["cliente"]["sin_whatsapp"])
        self.assertEqual((data["cliente"]["wa_area"], data["cliente"]["wa_numero"]),
                         ("299", "4777888"))
        count = self.env["sale.order"].search_count([])
        res = self._post("armar", self.sin_wa)
        self.assertEqual(res.status_code, 400)
        self.assertIn("no tiene WhatsApp", res.json()["error"])
        res = self._post("armar", self.sin_wa, sin_whatsapp="1")
        self.assertEqual(res.status_code, 400, "required: no skip")
        self.assertIn("hace falta el WhatsApp", res.json()["error"])
        res = self._post("armar", self.sin_wa, wa_area="299", wa_numero="1")
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.env["sale.order"].search_count([]), count, "nothing created")
        self.assertFalse(self.sin_wa.distri_whatsapp)
        order = self._order_of(self._post("armar", self.sin_wa, wa_area="299",
                                          wa_numero="4777888"))
        self.assertEqual(order.state, "sale")
        self.assertEqual(self.sin_wa.distri_whatsapp, "+5492994777888")
        self.assertTrue(self.sin_wa.message_ids.filtered(
            lambda m: "«Nueva venta»" in (m.body or "")))
        # «Presupuesto» does not ask
        self._require_wa(False)
        other = self.env["res.partner"].create({
            "name": "Despensa Sin WA V135", "company_type": "company",
            "user_id": self.vendedor.id})
        order = self._order_of(self._post("presupuesto", other, telefono="299 4000111"))
        self.assertEqual(order.state, "draft")
        self.assertEqual(self._ledger(self.sin_wa)[0], before[0], "no entry, payment, reconcile")
        # not required: «No tiene WhatsApp» confirms and says so (the counter
        # exit types no phone)
        other = self.env["res.partner"].create({
            "name": "Despensa Sin WA 2 V135", "company_type": "company"})
        self._login(self.admin)
        res = self._post("mostrador", other, sin_whatsapp="1")
        order = self._order_of(res)
        self.assertEqual(order.state, "sale")
        self.assertFalse(other.distri_whatsapp)
        self.assertTrue(order.message_ids.filtered(
            lambda m: "Confirmado sin WhatsApp" in (m.body or "")))

    def test_nueva_venta_parcel_name_gps_and_repeat(self):
        self._flags()
        self._login(self.vendedor)
        before = self._ledger()
        res = self._post("armar", encomienda="1", enc_nombre="Juana Pérez", enc_dni="12",
                         enc_localidad="Zapala")
        self.assertEqual(res.status_code, 400)
        self.assertIn("DNI", res.json()["error"])
        self.assertFalse(self.cliente.partner_latitude)
        order = self._order_of(self._post(
            "armar", encomienda="1", enc_nombre="Juana Pérez", enc_dni="30.111.222",
            enc_localidad="Zapala", contacto="  María   Gómez ", lat="-38.95", lng="-68.06",
            precision="20"))
        self.assertEqual(order.state, "sale")
        self.assertTrue(order.distri_encomienda_pedida)
        self.assertEqual(order.distri_encomienda_nombre, "Juana Pérez")
        self.assertEqual(order.distri_encomienda_dni, "30111222")
        self.assertEqual(order.distri_encomienda_localidad, "Zapala")
        self.assertEqual(order.distri_contacto_nombre, "María Gómez")
        self.assertAlmostEqual(self.cliente.partner_latitude, -38.95)
        self.assertEqual(self.cliente.distri_location_user_id, self.vendedor)
        # a location already on file is never replaced; a bad one is not used
        order2 = self._order_of(self._post("presupuesto", lat="-38.90", lng="-68.00",
                                           precision="10"))
        self.assertAlmostEqual(self.cliente.partner_latitude, -38.95)
        self.assertTrue(self.cliente.message_ids.filtered(
            lambda m: "no se cambió" in (m.body or "")))
        order3 = self._order_of(self._post("presupuesto", lat="-38.90", lng="-68.00",
                                           precision="900"))
        self.assertTrue(order3.message_ids.filtered(lambda m: "no era precisa" in (m.body or "")))
        # «Repetir»: his last orders, with their lines in the catalog's unit
        data = self.url_open("/nueva-venta/catalogo?cliente_id=%s" % self.cliente.id).json()
        nombres = [a["nombre"] for a in data["anteriores"]]
        self.assertEqual(nombres[:3], [order3.name, order2.name, order.name])
        self.assertEqual(sorted((r["id"], r["cantidad"]) for r in data["anteriores"][0]["renglones"]),
                         sorted([(self.p1.id, 2), (self.p2.id, 3)]))
        # an external seller sees no other seller's orders to repeat
        self.assertEqual(self._ledger(), before)

    def test_nueva_venta_replaces_the_tiles(self):
        self._flags()
        for user in (self.vendedor, self.admin, self.gerente):
            keys = [t["key"] for t in tablero.staff_tiles(self.env(user=user))]
            self.assertEqual(keys[0], "nuevaventa")
            self.assertNotIn("pedido", keys, user.login)
        nav = [i["key"] for i in role_nav(self.env(user=self.vendedor), "")]
        self.assertIn("nuevaventa", nav)
        self.assertNotIn("pedido", nav)
        self._login(self.admin)
        depot = self.url_open("/deposito").text
        self.assertIn('href="/nueva-venta"', depot)
        self.assertNotIn('href="/deposito/mostrador"', depot)
        self.assertEqual(self.url_open("/deposito/mostrador").status_code, 200,
                         "the old screen still answers")
        self.assertEqual(self.url_open("/pedido").status_code, 200)

    # ------------------------------------------------------------ corrections

    def _factura_sin_armar(self):
        order = self._quotation().sudo()
        order.action_confirm()
        invoice = order.with_context(distri_facturar_sin_armar=True)._create_invoices()
        invoice.action_post()
        return order

    def _ya(self, order, **data):
        csrf, _html = self._csrf("/armado")
        data.setdefault("bultos", "2")
        data["csrf_token"] = csrf
        res = self.url_open("/armado/facturado/%s/ya-armado" % order.id, data=data,
                            headers=FETCH, allow_redirects=False)
        self.env.invalidate_all()
        return res

    def test_ya_lo_arme_is_a_confirmed_correction(self):
        self._flags()
        order = self._factura_sin_armar()
        other = self._factura_sin_armar()
        before = self._ledger()
        self._login(self.armador)
        html = self.url_open("/armado").text
        self.assertIn('data-arm-confirma="armado"', html)
        self.assertIn('data-arm-confirma="entregado"', html)
        self.assertIn("Es una corrección", html)
        self.assertIn("%s ya se ENTREGÓ al cliente" % order.name, html)
        # without the phone's confirmation, or with the other one: refused
        for data in ({"motivo": "Se armó a mano"},
                     {"motivo": "Se armó a mano", "entregado": "1", "confirmo": "armado"},
                     {"motivo": "Se armó a mano", "confirmo": "entregado"}):
            res = self._ya(order, **data)
            self.assertEqual(res.status_code, 422, data)
            self.assertIn("Confirmá", res.json()["error"])
            self.assertEqual(order.distri_armado, "pendiente")
            self.assertFalse(order.distri_entregado_fuera)
        res = self._ya(order, entregado="1", confirmo="entregado")
        self.assertEqual(res.status_code, 422)
        self.assertIn("motivo", res.json()["error"])
        res = self._ya(order, entregado="1", confirmo="entregado", motivo="Se lo llevó el 24/11")
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertTrue(order.distri_entregado_fuera)
        self.assertIn("Corrección. Motivo: Se lo llevó el 24/11.",
                      order.message_ids.filtered(
                          lambda m: "entregado fuera del sistema" in (m.body or ""))[:1].body)
        res = self._ya(other, confirmo="armado", motivo="Armado sin la pantalla")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(other.distri_armado, "armado")
        self.assertFalse(other.distri_entregado_fuera)
        self.assertEqual(self._ledger(), before, "no entry, payment or reconcile")

    def test_rearmar_asks_the_reason(self):
        self._flags()
        order = self._packed_invoiced()
        action = order.with_user(self.admin).action_distri_rearmar()
        self.assertEqual(action["res_model"], "distri.rearmar.wizard")
        self.assertEqual(order.state, "sale", "nothing done yet")
        wizard = self.env["distri.rearmar.wizard"].with_user(self.admin).with_context(
            action["context"]).create({"order_id": order.id, "motivo": " "})
        with self.assertRaisesRegex(UserError, "motivo"):
            wizard.action_rearmar()
        wizard.motivo = "Faltó un bulto"
        result = wizard.action_rearmar()
        self.assertEqual(order.state, "cancel")
        new = self.env["sale.order"].browse(result["res_id"])
        self.assertEqual(new.state, "sale")
        for record in (new, order):
            self.assertTrue(record.message_ids.filtered(
                lambda m: "Rearmado" in (m.body or "") or "rearmado" in (m.body or "")
            ).filtered(lambda m: "Motivo: Faltó un bulto" in m.body), record.name)
        with self.assertRaises(UserError):
            order.with_user(self.vendedor).action_distri_rearmar()

    # ------------------------------------------------------------ the JS (node)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/venta_v129_35.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # «Ya lo armé»: nothing goes until the phone says which one
        self.assertEqual(out["arm"], {"firstPrevented": True, "askedText": True,
                                      "cancelKeeps": True, "okSubmits": True,
                                      "confirmo": "entregado", "secondPasses": True})
        # «Nueva venta»: the draft, «Repetir» and the WhatsApp question
        self.assertTrue(out["nv"]["draftOffered"], out["nv"])
        self.assertEqual(out["nv"]["restoredCart"], [{"id": 11, "cantidad": 4}])
        self.assertEqual(out["nv"]["restoredField"], "Pedro")
        self.assertTrue(out["nv"]["waShown"])
        self.assertIn("no tiene WhatsApp", out["nv"]["waError"])
        self.assertFalse(out["nv"]["postedWithoutWa"])
        self.assertEqual(out["nv"]["repeated"], [{"id": 11, "cantidad": 1}, {"id": 12, "cantidad": 6}])
        self.assertTrue(out["nv"]["draftSaved"])
        self.assertTrue(out["nv"]["draftClearedAfterSale"])
