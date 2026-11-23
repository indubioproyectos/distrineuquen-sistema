"""19.0.1.129.11: the incomes of the Mercado Pago report and the migration
board only for grupo_ingresos_reporte (models/ingresos_reporte.py).

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
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin, _local

GROUP = "distri_pedido.grupo_ingresos_reporte"


@tagged("post_install", "-at_install", "distri_v129_11")
class TestV12911IngresosReporte(CreditMixin, RepartoMixin, AccountTestInvoicingCommon):

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
        cls.gonzalo = user("gonzalo_v129_11", "distri_pedido.grupo_gerencia,"
                           "distri_pedido.grupo_administracion,distri_pedido.grupo_cobros,"
                           "distri_pedido.grupo_cajero")
        cls.denis = user("denis_v129_11", "distri_pedido.grupo_administracion,"
                         "distri_pedido.grupo_cobros,distri_pedido.grupo_cajero")
        cls.sistemas = user("sistemas_v129_11", "distri_pedido.grupo_administracion,"
                            "distri_pedido.grupo_cobros," + GROUP)
        cls.cashier = user("cajero_v129_11", "distri_pedido.grupo_cajero")
        cls.admin_user = user("admin_v129_11", "distri_pedido.grupo_administracion")
        cls.seller = user("vendedor_v129_11", "distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V129.11",
                                                     "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Report = cls.env["distri.mp.report"]

    # ------------------------------------------------------------ helpers

    _operation = 712910000000

    def _income(self, **vals):
        """A pending income of the report, proposed to the customer by CUIT
        («Confirmado contra reporte MP»)."""
        type(self)._operation += 1
        values = {"kind": "income", "source_id": str(self._operation), "amount": 4321.0,
                  "date": fields.Datetime.now(), "method_type": "bank_transfer",
                  "partner_id": self.partner.id, "proposal": "cuit"}
        values.update(vals)
        return self.Collection.sudo().create(values)

    def _csv(self, *rows):
        header = ("TRANSACTION_DATE;SOURCE_ID;PAYMENT_METHOD_TYPE;TRANSACTION_TYPE;"
                  "TRANSACTION_AMOUNT;PAYER_NAME;PAYMENT_METHOD;PAYER_ID_TYPE;PAYER_ID_NUMBER")
        lines = [header]
        for source, when, amount in rows:
            local = _local(when).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
            lines.append(";".join([local, source, "bank_transfer", "SETTLEMENT",
                                   "%.2f" % amount, "PAGADOR PRUEBA", "bank_transfer", "", ""]))
        return ("\n".join(lines) + "\n").encode()

    def _report(self, *rows, user=None):
        CreditMixin.seq += 1
        Report = self.Report.with_user(user) if user else self.Report
        return Report._import_file("v129-11-%s.csv" % CreditMixin.seq, self._csv(*rows))

    def _credited(self, amount, operation=None, when=None, user=None):
        msg = self._receipt(amount, operation=operation, when=when, user=user)
        self._credit(msg, user=user, confirmed=True)
        return self._card(msg)

    def _paths(self, user, card):
        """Every way a person turns `card` (a pending income) into a payment."""
        Collection = self.Collection.with_user(user)
        mine = card.with_user(user)
        notice = self.env["distri.mp.confirm.notice"].sudo().create(
            {"mode": "selected", "collection_ids": [Command.set(card.ids)]})
        vouch = self.env["distri.mp.vouch"].sudo().create(
            {"collection_id": card.id, "note": "Lo confirmó alguien"})
        vsr = self.env["distri.unrecorded.sale.wizard"].sudo().create(
            {"collection_id": card.id, "partner_id": self.partner.id, "amount": card.amount,
             "payment_amount": card.amount, "invoice_date": fields.Date.today()})
        return [
            ("Confirmar", mine.action_confirm_board),
            ("action_confirm", mine.action_confirm),
            ("Confirmar todos (vista previa)", Collection.action_confirm_ready_notice),
            ("Confirmar todos", Collection.action_confirm_ready),
            ("Aviso: Confirmar", notice.with_user(user).action_apply),
            ("Asignar y confirmar", lambda: self.env["distri.mp.assign"].with_user(user).create(
                {"collection_id": card.id, "partner_id": self.partner.id,
                 "learn": False}).action_assign_confirm()),
            ("Aprobar (estado de cuenta)", mine.action_approve_candidate),
            ("Aprobar sin comprobante", mine.action_open_vouch),
            ("Aprobar sin comprobante: wizard", vouch.with_user(user).action_apply),
            ("Confirmar: es otro pago", mine.action_confirm_other_payment),
            ("Confirmar: no es ese pago", mine.action_confirm_not_bank_twin),
            ("Venta sin registrar", mine.action_unrecorded_sale),
            ("Venta sin registrar: wizard", vsr.with_user(user).action_apply),
            ("Confirmar (revisión por cliente)",
             self.env["distri.balance.review.line"].with_user(user).browse().action_confirm_card),
        ]

    def _migration_paths(self, user, card):
        return [
            ("Confirmar (tarjeta)", card.with_user(user).action_migration_approve),
            ("Confirmar lote",
             self.env["distri.migration.summary"].with_user(user).browse().action_batch_approve),
            ("Vista previa", lambda: self.env["distri.migration.approve"].with_user(user)._open(
                card, "single")),
            ("Aprobar", self.env["distri.migration.approve"].with_user(user).browse()
             .action_approve),
        ]

    # ------------------------------------------------------------ the group

    def test_only_admin_is_a_member(self):
        group = self.env.ref(GROUP)
        admin = self.env.ref("base.user_admin")
        self.assertIn(admin, group.user_ids)
        self.assertIn(group, admin.all_group_ids)
        system = self.env.ref("base.group_system")
        self.assertNotIn(group, system.all_implied_ids, "never implied by Settings")
        self.assertFalse(self.env["res.groups"].search([("implied_ids", "in", group.ids)]),
                         "implied by no group: explicit membership")

    # ------------------------------------------------------------ denied

    def test_gonzalo_and_denis_cannot_confirm_an_income(self):
        card = self._income()
        payments = self._mp_payments()
        for user in (self.gonzalo, self.denis):
            for label, call in self._paths(user, card):
                with self.subTest(user=user.login, path=label), \
                        self.assertRaises(AccessError) as caught:
                    call()
                self.assertEqual(str(caught.exception), INCOME_DENIED)
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.payment_id)
        self.assertEqual(self._mp_payments(), payments, "no payment created")

    def test_confirm_one_is_gated_too(self):
        card = self._income()
        with self.assertRaises(AccessError):
            card.with_user(self.gonzalo)._confirm_one()
        self.assertEqual(card.state, "draft")

    def test_gonzalo_and_denis_cannot_approve_migration(self):
        card = self._income()
        for user in (self.gonzalo, self.denis):
            for label, call in self._migration_paths(user, card):
                with self.subTest(user=user.login, path=label), \
                        self.assertRaises(AccessError) as caught:
                    call()
                self.assertEqual(str(caught.exception), MIGRATION_DENIED)
        self.assertEqual(card.state, "draft")

    # ------------------------------------------------------------ allowed

    def test_group_member_confirms(self):
        card = self._income()
        action = card.with_user(self.sistemas).action_confirm_board()
        # no WhatsApp receipt: the notice first, one more click
        self.assertEqual(action["res_model"], "distri.mp.confirm.notice")
        self.env["distri.mp.confirm.notice"].with_user(self.sistemas).browse(
            action["res_id"]).action_apply()
        self.assertEqual(card.state, "confirmed")
        self.assertTrue(card.payment_id)
        self.assertEqual(card.confirmed_by_id, self.sistemas)

    def test_group_member_confirms_all_and_assigns(self):
        ready = self._income()
        result = self.Collection.with_user(self.sistemas)._confirm_ready(only=ready)
        self.assertEqual(result["params"]["type"], "success")
        self.assertEqual(ready.state, "confirmed")
        unknown = self._income(partner_id=False, proposal="none")
        wizard = self.env["distri.mp.assign"].with_user(self.sistemas).create(
            {"collection_id": unknown.id, "partner_id": self.partner.id, "learn": False})
        action = wizard.action_assign_confirm()
        if action.get("res_model") == "distri.mp.confirm.notice":
            self.env["distri.mp.confirm.notice"].with_user(self.sistemas).browse(
                action["res_id"]).action_apply()
        self.assertEqual(unknown.state, "confirmed")
        self.assertTrue(unknown.payment_id)

    def test_group_member_opens_the_migration_approval(self):
        card = self._income()
        action = card.with_user(self.sistemas).action_migration_approve()
        self.assertEqual(action["res_model"], "distri.migration.approve")

    # ------------------------------------------------------------ automatic paths

    def test_report_import_ties_a_credited_receipt(self):
        """The report imported by Denis (a manual import) or the cron still
        ties the credited receipt: no new payment, the income confirmed."""
        card = self._credited(25000, operation="712910000901", user=self.cashier)
        payment = card.payment_id
        self.assertTrue(payment)
        self._report(("712910000901", fields.Datetime.now() - timedelta(minutes=2), 25000),
                     user=self.denis)
        income = self.Collection.sudo().search([("source_id", "=", "712910000901")])
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, payment)
        self.assertEqual(card.audit_state, "verificado")
        self.assertEqual(self._mp_payments(), payment, "no second payment")

    def test_superuser_confirms(self):
        card = self._income()
        self.Collection.sudo().browse(card.id).with_context(
            distri_not_bank_twin=True).action_confirm()
        self.assertEqual(card.state, "confirmed")

    def test_hand_tie_to_a_credited_receipt_stays_and_creates_no_payment(self):
        when = fields.Datetime.now() - timedelta(minutes=3)
        first = self._credited(26000, when=when, user=self.cashier)
        second = self._credited(26000, when=when + timedelta(seconds=20), user=self.cashier)
        self._report(("712910000902", when + timedelta(seconds=30), 26000))
        income = self.Collection.sudo().search([("source_id", "=", "712910000902")])
        self.assertEqual((income.state, income.proposal), ("draft", "credited"))
        payments = self._mp_payments()
        # waiting for a tie: on Gonzalo's board
        self.assertIn(income, self.Collection.with_user(self.gonzalo).search(
            [("board_visible", "=", True)]))
        action = income.with_user(self.gonzalo).action_open_tie_credited()
        wizard = self.env["distri.mp.tie.credited"].with_user(self.gonzalo).browse(
            action["res_id"])
        wizard.card_id = first
        wizard.action_apply()
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, first.payment_id)
        self.assertEqual(self._mp_payments(), payments, "the tie creates no payment")
        self.assertFalse(second.income_id)

    # ------------------------------------------------------------ crediting stays

    def test_cashier_and_administracion_still_credit(self):
        # amounts far apart: two receipts within $ 1 and close in time are copies
        for user, amount in ((self.cashier, 17000), (self.admin_user, 18000),
                             (self.gonzalo, 19000)):
            with self.subTest(user=user.login):
                card = self._credited(amount, user=user)
                self.assertEqual(card.state, "confirmed")
                self.assertEqual(card.credited_by_id, user)
                self.assertTrue(card.payment_id)

    # ------------------------------------------------------------ the board

    def test_board_hides_the_pending_incomes(self):
        income = self._income()
        confirmed = self._income()
        confirmed.sudo().with_context(distri_not_bank_twin=True).action_confirm()
        verify = self._card(self._receipt(1900, user=self.cashier))
        domain = [("board_visible", "=", True), ("id", "in", (income | confirmed | verify).ids)]
        for user in (self.gonzalo, self.denis, self.admin_user):
            with self.subTest(user=user.login):
                seen = self.Collection.with_user(user).search(domain)
                self.assertEqual(seen, confirmed | verify)
                self.assertFalse(income.with_user(user).can_confirm_income)
        self.assertEqual(self.Collection.with_user(self.sistemas).search(domain),
                         income | confirmed | verify)
        hidden = self.Collection.with_user(self.gonzalo).search(
            [("board_visible", "=", False), ("id", "in", income.ids)])
        self.assertEqual(hidden, income)
        action = self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_mp_board")
        self.assertIn("board_visible", action["domain"])
        # the bar on top counts what the board shows
        counters = self.Collection.with_user(self.gonzalo).distri_board_counters()
        mine = self.Collection.with_user(self.sistemas).distri_board_counters()
        self.assertLess(counters["review"]["count"], mine["review"]["count"])

    def test_menu_and_buttons_hidden(self):
        group = self.env.ref(GROUP)
        menu = self.env.ref("distri_pedido.menu_cobros_migration")
        self.assertEqual(menu.group_ids, group)
        self.assertEqual(self.env.ref("distri_pedido.action_migration_summary_open").group_ids,
                         group)
        for user in (self.gonzalo, self.denis):
            self.assertNotIn(menu.id, self.env["ir.ui.menu"].with_user(user)._visible_menu_ids())
        self.assertIn(menu.id, self.env["ir.ui.menu"].with_user(self.sistemas)
                      ._visible_menu_ids())

        def arch(user, xmlid, model="distri.mp.collection", view_type="form"):
            return self.env[model].with_user(user).get_view(
                self.env.ref(xmlid).id, view_type)["arch"]

        # 19.0.1.129.13: «Confirmar» (action_confirm_board) is back for
        # grupo_cobros, hidden per card by `can_confirm_income`
        protected = {
            ("distri_pedido.view_mp_collection_form", "distri.mp.collection", "form"): (
                "action_confirm_other_payment", "action_confirm_not_bank_twin"),
            ("distri_pedido.view_mp_collection_kanban", "distri.mp.collection", "kanban"): (
                "action_confirm_ready_notice",),
        }
        for (xmlid, model, view_type), names in protected.items():
            gonzalo = arch(self.gonzalo, xmlid, model, view_type)
            sistemas = arch(self.sistemas, xmlid, model, view_type)
            for name in names:
                with self.subTest(view=xmlid, button=name):
                    self.assertNotIn('name="%s"' % name, gonzalo)
                    self.assertIn('name="%s"' % name, sistemas)
        # what stays for everyone: «Acreditar» and the tie
        form = arch(self.gonzalo, "distri_pedido.view_mp_collection_form")
        self.assertIn('name="action_credit"', form)
        self.assertIn('name="action_open_tie_credited"', form)
        self.assertIn('name="action_tie_bank_twin"', form)
        # every button of the wizards and the other screens carries the group
        # (19.0.1.129.13: the ones the staff use with a receipt tied are
        # checked in test_v129_13_ingresos_con_comprobante)
        Views = self.env["ir.ui.view"].sudo()
        for name in ("action_open_vouch", "action_migration_approve", "action_batch_approve",
                     "action_approve", "action_confirm_not_bank_twin",
                     "action_confirm_other_payment", "action_confirm_ready_notice"):
            views = Views.search([("arch_db", "like", 'name="%s"' % name)]).filtered(
                lambda v: (v.xml_id or "").startswith("distri_pedido."))
            buttons = [b for v in views
                       for b in etree.fromstring(v.arch_db).iter("button")
                       if b.get("name") == name]
            with self.subTest(button=name):
                self.assertTrue(buttons, name)
                for button in buttons:
                    condition = button.get("t-if") or button.get("t-elif")
                    if condition and not button.get("groups"):
                        # a kanban card: `groups` would orphan the next t-elif
                        self.assertIn("can_confirm_income", condition, name)
                    else:
                        self.assertEqual(button.get("groups"), GROUP, name)
