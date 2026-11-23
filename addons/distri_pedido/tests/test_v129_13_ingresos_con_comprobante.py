"""19.0.1.129.13: an income of the report with its customer's receipt tied
is confirmed by grupo_cobros again, shows on their board, and «Subir y
acreditar» after the report confirms it in one step
(models/ingresos_reporte.py).

Every customer, amount and operation here is fictitious."""
from datetime import timedelta

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
from odoo.addons.distri_pedido.models.ingresos_reporte import INCOME_DENIED, MIGRATION_DENIED
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_1_subir import UploadMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import _local

GROUP = "distri_pedido.grupo_ingresos_reporte"
STAFF_GROUPS = "distri_pedido.grupo_cobros,base.group_system," + GROUP


@tagged("post_install", "-at_install", "distri_v129_13")
class TestV12913IngresosConComprobante(UploadMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        params.set_param(PARAM_DRIVER_RULE, "")
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        # the roles of production: Gonzalo (gerencia, administración, autorizar
        # pagos, cajero) and Denis (administración, autorizar pagos, cajero)
        cls.gonzalo = user("gonzalo_v129_13", "distri_pedido.grupo_gerencia,"
                           "distri_pedido.grupo_administracion,distri_pedido.grupo_cobros,"
                           "distri_pedido.grupo_cajero")
        cls.denis = user("denis_v129_13", "distri_pedido.grupo_administracion,"
                         "distri_pedido.grupo_cobros,distri_pedido.grupo_cajero")
        cls.cashier = user("cajero_v129_13", "distri_pedido.grupo_cajero")
        cls.seller = user("vendedor_v129_13", "distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V129.13",
                                                     "company_type": "company"})
        cls.other = cls.env["res.partner"].create({"name": "Kiosco Otro V129.13",
                                                   "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Message = cls.env["distri.whatsapp.message"]

    # ------------------------------------------------------------ helpers

    _operation = 712913000000
    _amount = 31000

    def _next(self):
        type(self)._operation += 1
        type(self)._amount += 1000
        return str(self._operation), float(self._amount)

    def _income(self, operation=None, amount=None, when=None, **vals):
        """A pending income of the report, proposed to the customer by CUIT."""
        if not operation:
            operation, amount = self._next()
        values = {"kind": "income", "source_id": operation, "amount": amount,
                  "date": when or fields.Datetime.now(), "method_type": "bank_transfer",
                  "partner_id": self.partner.id, "proposal": "cuit"}
        values.update(vals)
        return self.Collection.sudo().create(values)

    def _tied(self, user=None):
        """The report first, then the customer's receipt (same operation),
        uploaded only (not credited): the receipt ties to the income."""
        operation, amount = self._next()
        income = self._income(operation, amount)
        msg = self._receipt(amount, operation=operation, user=user or self.cashier)
        self.assertEqual(income.receipt_id, msg)
        self.assertEqual((msg.distri_receipt_state, msg.distri_collection_id),
                         ("matched", income))
        self.assertEqual(income.state, "draft")
        return income

    def _upload_as(self, user, amount, when, operation=None, partner=None):
        """«Subir y acreditar» of one file read with `amount` at `when`."""
        reading = {"amount": amount, "amount_confidence": "high", "date": _local(when)}
        if operation:
            reading["operation"] = operation
        with self._reading(reading):
            [result] = self.Message.with_user(user)._distri_staff_upload(
                [self._file()], partner=partner or self.partner, credit=True,
                amounts=["%d" % amount])
        return result, self.Message.sudo().browse(result["id"])

    # ------------------------------------------------------------ the staff confirm

    def test_gonzalo_and_denis_confirm_an_income_with_its_receipt(self):
        for user in (self.gonzalo, self.denis):
            with self.subTest(user=user.login):
                income = self._tied()
                payments = self._mp_payments()
                self.assertTrue(income.with_user(user).can_confirm_income)
                self.assertFalse(income.with_user(user).can_confirm_income_any)
                result = income.with_user(user).action_confirm_board()
                self.assertIs(result, True, "a receipt: no notice")
                self.assertEqual(income.state, "confirmed")
                self.assertEqual(income.confirmed_by_id, user)
                self.assertFalse(income.confirmed_without_receipt)
                self.assertEqual(len(self._mp_payments() - payments), 1, "one payment")

    def test_the_other_confirming_paths_with_a_receipt(self):
        # «Asignar y confirmar» with the receipt's customer
        income = self._tied()
        wizard = self.env["distri.mp.assign"].with_user(self.denis).create(
            {"collection_id": income.id, "partner_id": self.partner.id, "learn": False})
        self.assertTrue(wizard.can_confirm_income)
        wizard.action_assign_confirm()
        self.assertEqual(income.state, "confirmed")
        # «Aprobar» of the statement
        income = self._tied()
        income.with_user(self.gonzalo).action_approve_candidate()
        self.assertEqual(income.state, "confirmed")
        # `_confirm_one` itself
        income = self._tied()
        income.with_user(self.denis)._confirm_one()
        self.assertEqual(income.state, "confirmed")

    # ------------------------------------------------------------ still denied

    def test_still_denied_without_receipt_and_on_the_kept_paths(self):
        bare = self._income()
        for user in (self.gonzalo, self.denis):
            with self.subTest(user=user.login, case="sin comprobante"), \
                    self.assertRaises(AccessError) as caught:
                bare.with_user(user).action_confirm_board()
            self.assertEqual(str(caught.exception), INCOME_DENIED)
            with self.subTest(user=user.login, case="sin comprobante: _confirm_one"), \
                    self.assertRaises(AccessError):
                bare.with_user(user)._confirm_one()
        income = self._tied()
        payments = self._mp_payments()
        vouch = self.env["distri.mp.vouch"].sudo().create(
            {"collection_id": income.id, "note": "Lo confirmó alguien"})
        vsr = self.env["distri.unrecorded.sale.wizard"].sudo().create(
            {"collection_id": income.id, "partner_id": self.partner.id,
             "amount": income.amount, "payment_amount": income.amount,
             "invoice_date": fields.Date.today()})
        notice_all = self.env["distri.mp.confirm.notice"].sudo().create(
            {"mode": "ready", "collection_ids": [Command.set(income.ids)]})
        for user in (self.gonzalo, self.denis):
            mine = income.with_user(user)
            kept = [
                ("Aprobar sin comprobante", mine.action_open_vouch),
                ("Aprobar sin comprobante: wizard", vouch.with_user(user).action_apply),
                ("Confirmar: es otro pago", mine.action_confirm_other_payment),
                ("Confirmar: no es ese pago", mine.action_confirm_not_bank_twin),
                ("Venta sin registrar", mine.action_unrecorded_sale),
                ("Venta sin registrar: wizard", vsr.with_user(user).action_apply),
                ("Confirmar todos", self.Collection.with_user(user).action_confirm_ready_notice),
                ("Confirmar todos: aviso", notice_all.with_user(user).action_apply),
                ("contexto de «no es ese pago»",
                 mine.with_context(distri_not_bank_twin=True).action_confirm_board),
                ("contexto de «es otro pago»",
                 mine.with_context(distri_not_credited=True).action_confirm_board),
                ("otro cliente", lambda: self.env["distri.mp.assign"].with_user(user).create(
                    {"collection_id": income.id, "partner_id": self.other.id,
                     "learn": False}).action_assign_confirm()),
            ]
            for label, call in kept:
                with self.subTest(user=user.login, path=label), \
                        self.assertRaises(AccessError) as caught:
                    call()
                self.assertEqual(str(caught.exception), INCOME_DENIED)
            for label, call in (
                    ("Confirmar (tarjeta)", mine.action_migration_approve),
                    ("Confirmar lote", self.env["distri.migration.summary"].with_user(user)
                     .browse().action_batch_approve)):
                with self.subTest(user=user.login, path=label), \
                        self.assertRaises(AccessError) as caught:
                    call()
                self.assertEqual(str(caught.exception), MIGRATION_DENIED)
        # who never confirmed (a cashier without grupo_cobros) neither now
        with self.assertRaises(AccessError):
            income.with_user(self.cashier).action_confirm_board()
        self.assertEqual((income.state, bare.state), ("draft", "draft"))
        self.assertEqual(self._mp_payments(), payments, "no payment created")

    def test_a_receipt_of_another_customer_is_not_tied(self):
        operation, amount = self._next()
        income = self._income(operation, amount)
        self._receipt(amount, operation=operation, partner=self.other, user=self.cashier)
        income.sudo().write({"partner_id": self.partner.id, "proposal": "manual"})
        self.assertFalse(income._distri_receipt_tied())
        with self.assertRaises(AccessError):
            income.with_user(self.denis).action_confirm_board()

    # ------------------------------------------------------------ the board

    def test_board_shows_the_incomes_with_a_receipt(self):
        tied = self._tied()
        bare = self._income()
        domain = [("board_visible", "=", True), ("id", "in", (tied | bare).ids)]
        for user in (self.gonzalo, self.denis):
            with self.subTest(user=user.login):
                self.assertEqual(self.Collection.with_user(user).search(domain), tied)
                self.assertEqual(self.Collection.with_user(user).search(
                    [("board_visible", "=", False), ("id", "in", (tied | bare).ids)]), bare)
                self.assertTrue(tied.with_user(user).board_visible)
                self.assertFalse(bare.with_user(user).board_visible)
                self.assertTrue(tied.with_user(user).can_confirm_income)
                self.assertFalse(bare.with_user(user).can_confirm_income)
        admin = self.env.ref("base.user_admin")
        self.assertEqual(self.Collection.with_user(admin).search(domain), tied | bare)

    def test_buttons_follow_the_card(self):
        def arch(user, xmlid, model="distri.mp.collection", view_type="form"):
            return self.env[model].with_user(user).get_view(
                self.env.ref(xmlid).id, view_type)["arch"]

        form = etree.fromstring(arch(self.gonzalo, "distri_pedido.view_mp_collection_form"))
        [confirm] = [b for b in form.iter("button") if b.get("name") == "action_confirm_board"]
        self.assertIn("can_confirm_income", confirm.get("invisible"))
        self.assertNotIn('name="action_confirm_other_payment"', etree.tostring(form).decode())
        self.assertIn('name="action_confirm_board"',
                      arch(self.denis, "distri_pedido.view_mp_collection_list", view_type="list"))
        Views = self.env["ir.ui.view"].sudo()
        for name in ("action_assign_confirm", "action_approve_candidate", "action_confirm_card",
                     "action_confirm_board"):
            views = Views.search([("arch_db", "like", 'name="%s"' % name)]).filtered(
                lambda v: (v.xml_id or "").startswith("distri_pedido."))
            for view in views:
                root = etree.fromstring(view.arch_db)
                for button in root.iter("button"):
                    if button.get("name") != name:
                        continue
                    with self.subTest(view=view.xml_id, button=name):
                        condition = button.get("t-if") or button.get("t-elif")
                        if condition:
                            self.assertIn("can_confirm_income", condition)
                            continue
                        self.assertEqual(button.get("groups"), STAFF_GROUPS)
                        if button.getparent().tag != "header":
                            # per record: hidden when this user may not confirm it
                            self.assertIn("can_confirm_income", button.get("invisible") or "")

    # ------------------------------------------------------------ upload after the report

    def test_upload_after_the_report_credits_in_one_step(self):
        operation, amount = self._next()
        income = self._income(operation, amount)
        payments = self._mp_payments()
        result, msg = self._upload_as(self.cashier, amount, fields.Datetime.now()
                                      - timedelta(hours=3), operation=operation)
        self.assertEqual(result["credit"], "acreditado", result["message"])
        self.assertIn("ya estaba en el reporte", result["message"])
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.confirmed_by_id, self.cashier)
        self.assertEqual(income.receipt_id, msg)
        new = self._mp_payments() - payments
        self.assertEqual(len(new), 1, "one payment, no duplicate")
        self.assertEqual(new, income.payment_id)
        card = self._card(msg)
        self.assertEqual((card.state, card.audit_state), ("confirmed", "verificado"))
        self.assertEqual(card.income_id, income)
        self.assertEqual(card.payment_id, income.payment_id)
        self.assertEqual(card.credited_by_id, self.cashier)
        self.assertEqual(msg.distri_collection_id, card)
        self.assertEqual(msg._distri_upload_credit_state(), (card, True, True))
        self.assertEqual(msg._distri_upload_row()["state_label"], "Acreditado")
        self.assertEqual(income._hand_verified_card(), card)
        # the report again: nothing changes, no payment
        self.assertFalse(card._distri_report_incomes()[0] - income)
        self.assertEqual(self._mp_payments() - payments, new)

    def test_upload_by_amount_customer_and_time(self):
        when = fields.Datetime.now() - timedelta(minutes=20)
        operation, amount = self._next()
        income = self._income(operation, amount, when=when)
        payments = self._mp_payments()
        result, msg = self._upload_as(self.denis, amount, when + timedelta(seconds=40))
        self.assertEqual(result["credit"], "acreditado", result["message"])
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(len(self._mp_payments() - payments), 1)
        self.assertEqual(self._card(msg).audit_state, "verificado")

    def test_upload_without_a_sure_match_waits(self):
        """Same amount and customer but 30 minutes away: tied by the usual
        matching, never confirmed by itself; it shows on the staff board."""
        when = fields.Datetime.now() - timedelta(hours=2)
        operation, amount = self._next()
        income = self._income(operation, amount, when=when)
        payments = self._mp_payments()
        result, msg = self._upload_as(self.cashier, amount, when + timedelta(minutes=30))
        self.assertEqual(result["credit"], "pendiente", result["message"])
        self.assertEqual(income.state, "draft")
        self.assertEqual(self._mp_payments(), payments)
        self.assertEqual(income.receipt_id, msg, "tied by the usual matching")
        self.assertIn(income, self.Collection.with_user(self.denis).search(
            [("board_visible", "=", True)]))
        self.assertTrue(income.with_user(self.denis).can_confirm_income)

    def test_upload_with_another_amount_waits(self):
        operation, amount = self._next()
        income = self._income(operation, amount)
        payments = self._mp_payments()
        result, _msg = self._upload_as(self.cashier, amount + 500, fields.Datetime.now()
                                       - timedelta(minutes=5), operation=operation)
        self.assertEqual(result["credit"], "pendiente", result["message"])
        self.assertEqual(income.state, "draft")
        self.assertEqual(self._mp_payments(), payments)

    def test_upload_stops_on_a_credited_receipt_of_the_same_transfer(self):
        """A receipt already credited that may be this transfer: the
        confirmation guard stops the one-step confirmation."""
        when = fields.Datetime.now() - timedelta(minutes=10)
        operation, amount = self._next()
        credited = self._receipt(amount, when=when, user=self.cashier)
        self._credit(credited, user=self.cashier, confirmed=True)
        income = self._income(operation, amount, when=when + timedelta(seconds=30),
                              proposal="none", partner_id=False)
        income._propose()
        payments = self._mp_payments()
        result, _msg = self._upload_as(self.cashier, amount, when + timedelta(seconds=30),
                                       operation=operation)
        self.assertNotEqual(income.state, "confirmed")
        self.assertEqual(self._mp_payments(), payments, "no second payment")
        self.assertEqual(result["credit"], "pendiente", result["message"])
