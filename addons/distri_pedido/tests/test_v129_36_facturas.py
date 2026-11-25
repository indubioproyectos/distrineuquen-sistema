"""19.0.1.129.36 (owner's decision B, 25/11/2026), behind
distri_pedido.nueva_venta (models/factura_directa.py, models/
factura_sin_armar.py, models/anular_venta.py).

- A customer invoice not linked to a sale order is posted only by the group
  «Facturación directa de servicios» (Denis, Gonzalo, Ale), and only with
  services, lines without a product, sections and notes.
- Every sale path keeps invoicing as before (Odoo's «Crear factura», the end
  of the packing, «Nueva venta» Mostrador / Ya entregado / A armar), and so
  do reversals, «Anular venta», «Ajuste de cuenta» and «Venta sin
  registrar».
- «Facturar igual» is refused by the server.
- With the flag off everything is as in 129.35.
Refusals change nothing: entries, payments, reconciliations and the
customer's balance are compared before and after. Synthetic data only.
"""
import json
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import Form, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.factura_directa import (
    AVISO_NO_PERMITIDO, AVISO_PERMITIDO, GRUPO_DIRECTA, LOGINS_DIRECTA, puede_facturar_directo)
from odoo.addons.distri_pedido.models.factura_sin_armar import can_force
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_36")
class TestV12936Facturas(PedidoAbiertoMixin, RepartoMixin, AccountTestInvoicingHttpCommon):

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
        invoicing = "account.group_account_invoice,sales_team.group_sale_salesman_all_leads,"

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + invoicing + groups, **company)

        # the three of the owner's decision, by their own logins in the test
        cls.denis = user("denis_v136", "distri_pedido.grupo_administracion")
        cls.gonzalo = user("gonzalo_v136", "distri_pedido.grupo_gerencia")
        cls.ale = user("ale_v136", "distri_pedido.grupo_administracion,base.group_system")
        cls.directos = cls.denis | cls.gonzalo | cls.ale
        cls.env["res.users"]._distri_asignar_facturacion_directa(
            ("denis_v136", "gonzalo_v136", "ale_v136"), apply=True)
        # everybody else, administración and gerencia included
        cls.admin = user("admin_v136", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v136", "distri_pedido.grupo_gerencia")
        cls.armador = user("armador_v136", "distri_pedido.grupo_armador")
        cls.cobros = user("cobros_v136",
                          "distri_pedido.grupo_administracion,distri_pedido.grupo_cobros")
        Tmpl = cls.env["product.template"]
        cls.goods = Tmpl.create({"name": "Gaseosa V136 x 6 u.", "type": "consu",
                                 "is_storable": True, "list_price": 40000.0,
                                 "is_published": True, "sale_ok": True,
                                 "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.goods2 = Tmpl.create({"name": "Galletitas V136 x 12 u.", "type": "consu",
                                  "list_price": 15000.0, "is_published": True, "sale_ok": True,
                                  "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.service = Tmpl.create({"name": "Flete V136", "type": "service",
                                   "list_price": 5000.0, "sale_ok": True,
                                   "taxes_id": [Command.clear()]})
        cls.stock = cls.env["stock.warehouse"].search(
            [("company_id", "=", cls.env.company.id)], limit=1).lot_stock_id
        cls.env["stock.quant"]._update_available_quantity(
            cls.goods.product_variant_id, cls.stock, 100.0)
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacen Facturas V136", "company_type": "company",
            "distri_whatsapp": "2995001136", "street": "Mitre 136", "city": "Neuquén"})
        cls.income = cls.company_data["default_account_revenue"]
        numerar_remitos(cls.env)

    # ------------------------------------------------------------ helpers

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "1" if on else "0")

    def _ledger(self):
        """Entries, payments, reconciliations, the receivable total and the
        customer's own receivable."""
        self.env.flush_all()
        self.cliente.invalidate_recordset(["credit"])
        return (self.env["distri.promised.transfer"]._distri_ledger_fingerprint(),
                round(self.cliente.credit, 2))

    def _direct(self, user, lines, move_type="out_invoice"):
        """A draft customer invoice with no sale order, made by `user`."""
        return self.env["account.move"].with_user(user).create({
            "move_type": move_type, "partner_id": self.cliente.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create(vals) for vals in lines]})

    def _service_lines(self):
        return [
            {"product_id": self.service.product_variant_id.id, "quantity": 1,
             "price_unit": 5000.0, "tax_ids": [Command.clear()]},
            {"name": "Cargo administrativo", "quantity": 1, "price_unit": 700.0,
             "account_id": self.income.id, "tax_ids": [Command.clear()]},
            {"display_type": "line_note", "name": "Nota al pie"},
        ]

    def _goods_line(self):
        return {"product_id": self.goods.product_variant_id.id, "quantity": 2,
                "price_unit": 40000.0, "tax_ids": [Command.clear()]}

    def _order(self, user=None):
        order = self.env["sale.order"].with_user(user or self.admin).create({
            "partner_id": self.cliente.id, "order_line": [
                Command.create({"product_id": self.goods.product_variant_id.id,
                                "product_uom_qty": 2, "price_unit": 40000.0,
                                "tax_ids": [Command.clear()]}),
                Command.create({"product_id": self.goods2.product_variant_id.id,
                                "product_uom_qty": 3, "price_unit": 15000.0,
                                "tax_ids": [Command.clear()]})]})
        return order

    def _packed(self):
        """Confirmed and packed by the packer: the invoice of the packing."""
        order = self._order()
        order.with_user(self.admin).action_confirm()
        packer = order.with_user(self.armador)
        self.assertEqual(packer.distri_tomar() or packer.distri_terminar(1), "")
        return order

    def _invoice(self, order):
        return order.invoice_ids.filtered(lambda m: m.move_type == "out_invoice")

    def _posted(self, invoice, user):
        """Posted as the normal flow leaves it; a draft is posted by `user`."""
        self.assertEqual(len(invoice), 1)
        if invoice.state == "draft":
            invoice.with_user(user).action_post()
        self.assertEqual(invoice.state, "posted")
        return invoice

    # ------------------------------------------------------------ the group

    def test_group_assignment_by_login(self):
        Users = self.env["res.users"]
        self.assertEqual(LOGINS_DIRECTA, ("admin", "denis", "gonzalo"))
        group = self.env.ref(GRUPO_DIRECTA)
        self.assertFalse(self.env.ref("base.user_admin") in group.user_ids,
                         "the module itself puts nobody in the group")
        for u in self.directos:
            self.assertTrue(puede_facturar_directo(u), u.login)
        for u in (self.admin, self.gerente, self.armador, self.cobros):
            self.assertFalse(puede_facturar_directo(u), u.login)
        archived = new_test_user(self.env, login="archivado_v136",
                                 groups="base.group_user")
        archived.active = False
        logins = ("archivado_v136", "nadie_v136", "denis_v136")
        dry = Users._distri_asignar_facturacion_directa(logins)
        self.assertTrue(dry[0].startswith("WOULD_ADD archivado_v136"), dry[0])
        self.assertTrue(dry[0].endswith(", archived)"), dry[0])
        self.assertEqual(dry[1], "MISSING nadie_v136: no such user")
        self.assertTrue(dry[2].startswith("ALREADY denis_v136"))
        self.assertNotIn(group, archived.group_ids, "a dry run writes nothing")
        done = Users._distri_asignar_facturacion_directa(logins, apply=True)
        self.assertTrue(done[0].startswith("ADDED archivado_v136"))
        self.assertIn(group, archived.group_ids)
        again = Users._distri_asignar_facturacion_directa(logins, apply=True)
        self.assertTrue(again[0].startswith("ALREADY archivado_v136"))

    # ------------------------------------------------------------ direct invoices

    def test_each_allowed_user_posts_a_service_invoice_not_goods(self):
        self._flag()
        for user in self.directos:
            invoice = self._direct(user, self._service_lines())
            invoice.with_user(user).action_post()
            self.assertEqual(invoice.state, "posted", user.login)
            self.assertEqual(invoice.amount_total, 5700.0)
            # a credit note of services too
            refund = self._direct(user, self._service_lines()[:1], "out_refund")
            refund.with_user(user).action_post()
            self.assertEqual(refund.state, "posted")
            # goods: refused, for them too, and nothing moves
            mixed = self._direct(user, self._service_lines() + [self._goods_line()])
            before = self._ledger()
            with self.assertRaisesRegex(UserError, "solo para servicios.*Gaseosa V136"):
                mixed.with_user(user).action_post()
            self.assertEqual(mixed.state, "draft")
            self.assertEqual(self._ledger(), before)

    def test_everybody_else_is_refused(self):
        self._flag()
        for user in (self.admin, self.gerente, self.cobros):
            for lines, kind in ((self._service_lines(), "out_invoice"),
                                ([self._goods_line()], "out_invoice"),
                                (self._service_lines()[:1], "out_refund")):
                move = self._direct(user, lines, kind)
                before = self._ledger()
                with self.assertRaisesRegex(UserError, "Nueva venta"):
                    move.with_user(user).action_post()
                self.assertEqual(move.state, "draft")
                self.assertEqual(self._ledger(), before)
        # an invoice made by one of them and posted by an allowed user goes
        move = self._direct(self.admin, self._service_lines())
        move.with_user(self.denis).action_post()
        self.assertEqual(move.state, "posted")
        # scheduled for later: the cron posts it as its author would
        later = self._direct(self.admin, self._service_lines())
        later.write({"auto_post": "at_date", "date": fields.Date.today() - timedelta(days=1),
                     "invoice_date": fields.Date.today() - timedelta(days=1)})
        with self.assertRaisesRegex(UserError, "Nueva venta"):
            later._post(soft=False)
        # vendor bills are not affected
        bill = self.env["account.move"].with_user(self.admin).create({
            "move_type": "in_invoice", "partner_id": self.cliente.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create(self._goods_line())]})
        bill.with_user(self.admin).action_post()
        self.assertEqual(bill.state, "posted")

    def test_invoice_form_drops_a_goods_line_and_says_why(self):
        self._flag()
        for user, text in ((self.denis, AVISO_PERMITIDO), (self.admin, AVISO_NO_PERMITIDO)):
            Move = self.env["account.move"].with_user(user).with_context(
                default_move_type="out_invoice")
            form = Form(Move)
            form.partner_id = self.cliente
            with form.invoice_line_ids.new() as line:
                line.product_id = self.goods.product_variant_id
                self.assertFalse(line.product_id, "a goods line is dropped")
                line.name = "Renglón sin producto"
                line.price_unit = 100.0
            with form.invoice_line_ids.new() as line:
                line.product_id = self.service.product_variant_id
                self.assertEqual(line.product_id, self.service.product_variant_id)
                line.price_unit = 5000.0
            move = form.save()
            self.assertEqual(move.distri_aviso_directa, text)
        # flag off: the form is as before
        self._flag(False)
        form = Form(self.env["account.move"].with_user(self.admin).with_context(
            default_move_type="out_invoice"))
        form.partner_id = self.cliente
        with form.invoice_line_ids.new() as line:
            line.product_id = self.goods.product_variant_id
            self.assertEqual(line.product_id, self.goods.product_variant_id)
            line.price_unit = 1.0
        self.assertFalse(form.save().distri_aviso_directa)

    # ------------------------------------------------------------ sale paths

    def test_sale_invoices_of_every_path_are_not_affected(self):
        self._flag()
        # the end of the packing (/pedido, AgilPedido, «A armar»)
        packed = self._packed()
        inv = self._posted(self._invoice(packed), self.admin)
        self.assertFalse(inv.distri_aviso_directa)
        # Odoo's «Crear factura» on a packed order, by administración
        order = self._order()
        order.with_user(self.admin).action_confirm()
        order.sudo().write({"distri_armado": "armado"})
        wizard = self.env["sale.advance.payment.inv"].with_user(self.admin).with_context(
            active_model="sale.order", active_ids=order.ids, active_id=order.id).create(
            {"advance_payment_method": "delivered"})
        wizard.create_invoices()
        self._posted(self._invoice(order), self.admin)
        # a goods line added by hand to a sale invoice is still a sale invoice
        order2 = self._order()
        order2.with_user(self.admin).action_confirm()
        order2.sudo().write({"distri_armado": "armado"})
        draft = order2.with_user(self.admin)._create_invoices()
        draft.with_user(self.admin).write({"invoice_line_ids": [
            Command.create(self._goods_line())]})
        draft.with_user(self.admin).action_post()
        self.assertEqual(draft.state, "posted")
        # «Venta en mostrador» (the «Mostrador» exit of «Nueva venta»)
        counter = self._order()
        counter.with_user(self.admin).action_distri_counter_sale()
        self._posted(self._invoice(counter), self.admin)

    def test_nueva_venta_exits_still_invoice(self):
        self._flag()
        self.authenticate(self.admin.login, self.admin.login)
        cart = json.dumps([{"id": self.goods.id, "cantidad": 2},
                           {"id": self.goods2.id, "cantidad": 1}])

        def post(salida, **extra):
            page = self.url_open("/nueva-venta")
            self.assertEqual(page.status_code, 200, page.text[:300])
            data = {"csrf_token": CSRF_RE.search(page.text).group(1),
                    "envio": ENVIO_RE.search(page.text).group(1),
                    "cliente_id": str(self.cliente.id), "renglones": cart, "salida": salida,
                    "entrega": "domicilio", "telefono": "2995001136",
                    "domicilio": "Mitre 136, Neuquén"}
            data.update(extra)
            res = self.url_open("/nueva-venta/crear", data=data, headers=FETCH,
                                allow_redirects=False)
            self.assertEqual(res.status_code, 200, res.text[:500])
            self.env.invalidate_all()
            order_id = int(re.search(r"/(\d+)", res.json()["url"]).group(1))
            return self.env["sale.order"].browse(order_id)

        mostrador = post("mostrador")
        self._posted(self._invoice(mostrador), self.admin)
        entregado = post("entregado", fecha=str(today_ar() - timedelta(days=2)),
                         motivo="Ya entregado por Gonzalo")
        self._posted(self._invoice(entregado), self.admin)
        armar = post("armar")
        self.assertEqual(armar.state, "sale")
        packer = armar.with_user(self.armador)
        self.assertEqual(packer.distri_tomar() or packer.distri_terminar(1), "")
        self._posted(self._invoice(armar), self.admin)

    # ------------------------------------------------------------ reversals

    def test_reversal_and_anular_venta_still_work(self):
        self._flag()
        # Odoo's «Revertir» of a sale invoice, by administración
        order = self._packed()
        invoice = self._posted(self._invoice(order), self.admin)
        reversal = self.env["account.move.reversal"].with_user(self.admin).with_context(
            active_model="account.move", active_ids=invoice.ids).create({
                "reason": "Devolución", "journal_id": invoice.journal_id.id})
        reversal.refund_moves()
        refund = invoice.reversal_move_ids
        self.assertEqual(len(refund), 1)
        if refund.state == "draft":
            refund.with_user(self.admin).action_post()
        self.assertEqual(refund.state, "posted")
        # the reversal of an old direct invoice (posted before the flag)
        self._flag(False)
        old = self._direct(self.admin, [self._goods_line()])
        old.with_user(self.admin).action_post()
        self._flag()
        reversal = self.env["account.move.reversal"].with_user(self.admin).with_context(
            active_model="account.move", active_ids=old.ids).create({
                "reason": "Anulación", "journal_id": old.journal_id.id})
        reversal.refund_moves()
        credit = old.reversal_move_ids
        credit.with_user(self.admin).action_post()
        self.assertEqual(credit.state, "posted")
        # a «reversal» raised above the invoice's total is a direct credit note
        bigger = self._direct(self.admin, [dict(self._goods_line(), price_unit=50000.0)],
                              "out_refund")
        bigger.reversed_entry_id = old
        with self.assertRaisesRegex(UserError, "Nueva venta"):
            bigger.with_user(self.admin).action_post()
        # «Anular venta», by administración (not in the group)
        sold = self._packed()
        sold_inv = self._posted(self._invoice(sold), self.admin)
        plan = sold.with_user(self.admin).distri_anular_venta("El cliente se arrepintió")
        self.assertEqual(sold.state, "cancel")
        self.assertEqual(plan["refunds"].state, "posted")
        self.assertEqual(sold_inv.amount_residual, 0.0)
        # 129.35's «..»: a product name ending in a period
        steps = [s for s in plan["steps"] if s.startswith("Vuelve al stock")]
        self.assertTrue(steps)
        self.assertTrue(all(".." not in s for s in steps), steps)
        self.assertTrue(steps[0].endswith(" u."), steps[0])

    def test_account_tools_are_not_direct_sales(self):
        self._flag()
        # «Ajuste de cuenta» (AJC) by Cobros, not in the group: a credit note
        wizard = self.env["distri.balance.adjustment.wizard"].with_user(self.cobros).create({
            "partner_id": self.cliente.id, "target_balance": -500.0,
            "motive": "Diferencia con Online", "authorized_by": "Gonzalo"})
        wizard.action_apply()
        adjustment = self.env["distri.balance.adjustment"].search(
            [("partner_id", "=", self.cliente.id)])
        self.assertEqual(adjustment.move_id.state, "posted")
        self.assertEqual(adjustment.move_id.move_type, "out_refund")

    # ------------------------------------------------------------ «Facturar igual»

    def test_facturar_igual_is_refused_by_the_server(self):
        self._flag()
        self.assertFalse(can_force(self.admin))
        order = self._order()
        order.with_user(self.admin).action_confirm()
        Wizard = self.env["sale.advance.payment.inv"].with_user(self.admin).with_context(
            active_model="sale.order", active_ids=order.ids, active_id=order.id)
        wizard = Wizard.create({"advance_payment_method": "delivered"})
        self.assertEqual(wizard.distri_sin_armar, order.name)
        self.assertTrue(wizard.distri_sin_facturar_igual)
        self.assertFalse(wizard.distri_puede_forzar)
        before = self._ledger()
        wizard.distri_facturar_sin_armar = True  # a post made by hand
        with self.assertRaisesRegex(UserError, "Nueva venta.*Mostrador.*Ya entregado"):
            wizard.create_invoices()
        with self.assertRaisesRegex(UserError, "«Facturar igual» ya no existe"):
            order.with_user(self.admin).with_context(
                distri_facturar_sin_armar=True)._create_invoices()
        self.assertFalse(order.invoice_ids)
        self.assertEqual(self._ledger(), before)
        # an order invoiced that way before keeps «Ya lo armé»
        old = self._order().sudo()
        old.action_confirm()
        inv = old.with_context(distri_facturar_sin_armar=True)._create_invoices()
        inv.action_post()
        self.assertTrue(old._distri_es_facturado_sin_armar())
        self.assertEqual(old.with_user(self.armador).distri_ya_armado(
            1, motivo="Facturado con «Facturar igual» antes"), "")
        self.assertEqual(old.distri_armado, "armado")

    # ------------------------------------------------------------ flag off

    def test_flag_off_everything_as_before(self):
        self._flag(False)
        self.assertTrue(can_force(self.admin))
        # anybody with invoicing posts a direct invoice of goods, as today
        move = self._direct(self.admin, [self._goods_line()])
        move.with_user(self.admin).action_post()
        self.assertEqual(move.state, "posted")
        self.assertFalse(move.distri_aviso_directa)
        # «Facturar igual» as in 129.35
        order = self._order()
        order.with_user(self.admin).action_confirm()
        wizard = self.env["sale.advance.payment.inv"].with_user(self.admin).with_context(
            active_model="sale.order", active_ids=order.ids, active_id=order.id).create(
            {"advance_payment_method": "delivered"})
        self.assertFalse(wizard.distri_sin_facturar_igual)
        self.assertTrue(wizard.distri_puede_forzar)
        with self.assertRaisesRegex(UserError, "tilda «Facturar igual»"):
            wizard.create_invoices()
        wizard.distri_facturar_sin_armar = True
        wizard.create_invoices()
        self.assertEqual(len(order.invoice_ids), 1)

    def test_turning_the_flag_on_changes_no_existing_record(self):
        """Posted invoices, payments and reconciliations made before stay as
        they are; paying an old direct invoice still works."""
        self._flag(False)
        old = self._direct(self.admin, [self._goods_line()])
        old.with_user(self.admin).action_post()
        packed = self._packed()
        sale_inv = self._posted(self._invoice(packed), self.admin)
        self.env["account.payment.register"].with_user(self.cobros).with_context(
            active_model="account.move", active_ids=sale_inv.ids).create({
                "journal_id": self.company_data["default_journal_bank"].id,
                "payment_date": fields.Date.today()}).action_create_payments()
        snapshot = [(m.id, m.state, m.amount_residual, m.payment_state,
                     tuple(m.line_ids.mapped("balance"))) for m in old | sale_inv]
        before = self._ledger()
        self._flag()
        self.env.invalidate_all()
        self.assertEqual(self._ledger(), before)
        self.assertEqual([(m.id, m.state, m.amount_residual, m.payment_state,
                           tuple(m.line_ids.mapped("balance"))) for m in old | sale_inv],
                         snapshot)
        self.env["account.payment.register"].with_user(self.cobros).with_context(
            active_model="account.move", active_ids=old.ids).create({
                "journal_id": self.company_data["default_journal_bank"].id,
                "payment_date": fields.Date.today()}).action_create_payments()
        self.assertIn(old.payment_state, ("paid", "in_payment"))
