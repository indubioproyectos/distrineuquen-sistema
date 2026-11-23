"""19.0.1.73.0: only grupo_cobros (for now, the user admin) authorizes money.

Administración and Gerencia keep everything else: they see the cards, the
statements and the debtors, but confirming, vouching, choosing a card's
customer, discarding, undoing, closing the day, adjusting a balance and
registering a customer payment need grupo_cobros (models/permisos_cobros.py).
Every customer and amount here is fictitious."""
from datetime import date

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


@tagged("post_install", "-at_install", "distri_permisos_cobros")
class TestPermisosCobros(RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        common = {"company_id": company.id, "company_ids": [Command.set(company.ids)]}
        cls.cobros_group = cls.env.ref("distri_pedido.grupo_cobros")
        # the roles of the lab: denis (administración), gonzalo (gerencia)
        cls.admin_user = new_test_user(
            cls.env, login="admin_sin_cobros", name="Administración sin cobros",
            groups="base.group_user,distri_pedido.grupo_administracion", **common)
        cls.manager = new_test_user(
            cls.env, login="gerencia_sin_cobros", name="Gerencia sin cobros",
            groups="base.group_user,distri_pedido.grupo_gerencia", **common)
        # the permission given back: administración plus grupo_cobros
        cls.cashier = new_test_user(
            cls.env, login="admin_con_cobros", name="Administración con cobros",
            groups="base.group_user,distri_pedido.grupo_administracion,distri_pedido.grupo_cobros,"
                   "distri_pedido.grupo_ingresos_reporte",
            **common)
        cls.admin = cls.env.ref("base.user_admin")
        cls.admin.sudo().write({"company_ids": [Command.link(company.id)]})
        cls.readonly_users = cls.admin_user | cls.manager
        cls.customer = cls.env["res.partner"].create({"name": "Almacén Permisos Prueba",
                                                      "company_type": "company"})
        cls.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": cls.customer.id, "fecha": date(2026, 8, 1),
            "comprobante": "0001-PERMISOS-1", "tipo": "factura", "debe": 5000.0, "haber": 0.0})
        cls.cash = cls.company_data["default_journal_cash"]
        cls.Collection = cls.env["distri.mp.collection"]
        # the fixtures (orders of the route) are made by the test's own user
        cls.env.user.sudo().write({"group_ids": [
            # 19.0.1.129.11: confirming a report income needs grupo_ingresos_reporte
            Command.link(cls.env.ref("distri_pedido.grupo_ingresos_reporte").id),
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()

    # ------------------------------------------------------------ helpers

    _operation = 700000090000

    def _card(self, **vals):
        """A pending income of the report, already proposed to the customer."""
        type(self)._operation += 1
        values = {"source_id": str(self._operation), "amount": 1500.0,
                  "date": fields.Datetime.now(), "method_type": "bank_transfer",
                  "partner_id": self.customer.id, "proposal": "cuit"}
        values.update(vals)
        return self.Collection.sudo().create(values)

    def _payment_vals(self, **vals):
        values = {"payment_type": "inbound", "partner_type": "customer",
                  "partner_id": self.customer.id, "amount": 700.0, "journal_id": self.cash.id,
                  "date": date(2026, 9, 1), "memo": "Pago de prueba"}
        values.update(vals)
        return values

    def _adjustment_wizard(self, user):
        action = self.customer.with_user(user).action_distri_balance_adjustment()
        return self.env[action["res_model"]].with_user(user).with_context(
            action["context"]).create({"motive": "Saldo confirmado por la gerencia",
                                       "authorized_by": "Gerente de prueba",
                                       "date": date(2026, 9, 10)})

    # ------------------------------------------------------------ the group

    def test_group_is_its_own_and_admin_has_it(self):
        self.assertTrue(self.admin.has_group("distri_pedido.grupo_cobros"))
        for xmlid in ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia"):
            self.assertNotIn(self.cobros_group, self.env.ref(xmlid).all_implied_ids, xmlid)
        for user in self.readonly_users:
            self.assertFalse(user.has_group("distri_pedido.grupo_cobros"), user.login)
        # its own drop-down in the user form: choosing it never takes the role away
        self.assertNotEqual(self.cobros_group.privilege_id,
                            self.env.ref("distri_pedido.grupo_administracion").privilege_id)
        self.assertEqual(self.cobros_group.privilege_id.category_id,
                         self.env.ref("distri_pedido.grupo_administracion").privilege_id.category_id)

    # ------------------------------------------------------------ refused

    def test_readonly_users_cannot_confirm_vouch_assign_or_discard(self):
        card = self._card()
        candidate = self._card(receipt_candidate=True)
        for user in self.readonly_users:
            with self.subTest(user=user.login):
                mine = card.with_user(user)
                with self.assertRaises(AccessError):
                    mine.action_confirm()
                with self.assertRaises(AccessError):
                    mine.action_confirm_board()
                with self.assertRaises(AccessError):
                    self.Collection.with_user(user).action_confirm_ready_notice()
                with self.assertRaises(AccessError):
                    candidate.with_user(user).action_open_vouch()
                with self.assertRaises(AccessError):
                    mine.action_assign()
                with self.assertRaises(AccessError):
                    self.env["distri.mp.assign"].with_user(user).create(
                        {"collection_id": card.id, "partner_id": self.customer.id})
                with self.assertRaises(AccessError):
                    mine.action_ignore()
                with self.assertRaises(AccessError):
                    self.env["distri.mp.ignore"].with_user(user).create(
                        {"collection_id": card.id, "reason": "other", "note": "prueba"})
                with self.assertRaises(AccessError):
                    mine.action_mark_wholesale()
                with self.assertRaises(AccessError):
                    mine.action_choose_invoice()
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.payment_id)

    def test_readonly_users_cannot_undo_a_confirmed_card(self):
        card = self._card()
        card.with_user(self.cashier).action_confirm()
        payment = card.payment_id
        self.assertTrue(payment)
        for user in self.readonly_users:
            with self.assertRaises(AccessError):
                card.with_user(user).action_undo()
        self.assertEqual(card.state, "confirmed")
        self.assertNotIn(payment.state, ("draft", "canceled"))

    def test_readonly_users_cannot_create_or_post_customer_payments(self):
        for user in self.readonly_users:
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.env["account.payment"].with_user(user).create(self._payment_vals())
        draft = self.env["account.payment"].create(self._payment_vals())
        for user in self.readonly_users:
            with self.assertRaises(AccessError):
                draft.with_user(user).action_post()
            with self.assertRaises(AccessError):
                draft.with_user(user).write({"partner_id": self.partner_a.id})
        self.assertEqual(draft.state, "draft")
        draft.with_user(self.cashier).action_post()
        for user in self.readonly_users:
            with self.assertRaises(AccessError):
                draft.with_user(user).action_draft()
            with self.assertRaises(AccessError):
                draft.with_user(user).action_cancel()
        self.assertNotIn(draft.state, ("draft", "canceled"))

    def test_vendor_payments_stay_with_gerencia(self):
        # only customer money is reserved: gerencia keeps paying its purchases
        payment = self.env["account.payment"].with_user(self.manager).create(self._payment_vals(
            payment_type="outbound", partner_type="supplier", partner_id=self.partner_a.id))
        payment.action_post()
        self.assertNotEqual(payment.state, "draft")

    def test_readonly_users_cannot_adjust_a_balance_or_close_the_day(self):
        day = self.env["distri.mp.day"].sudo().create({"date": date(2026, 9, 2),
                                                       "company_id": self.env.company.id})
        for user in self.readonly_users:
            with self.subTest(user=user.login):
                with self.assertRaises(AccessError):
                    self.customer.with_user(user).action_distri_balance_adjustment()
                with self.assertRaises(AccessError):
                    self.env["distri.balance.adjustment.wizard"].with_user(user).create({
                        "partner_id": self.customer.id, "motive": "x", "authorized_by": "y"})
                with self.assertRaises(AccessError):
                    day.with_user(user).action_close()
        self.assertFalse(self.env["distri.balance.adjustment"].search(
            [("partner_id", "=", self.customer.id)]))
        self.assertEqual(day.state, "open")

    # ------------------------------------------------------------ allowed

    def test_cobros_user_and_admin_authorize(self):
        for user in (self.cashier, self.admin):
            with self.subTest(user=user.login):
                card = self._card().with_user(user)
                card.action_confirm()
                self.assertEqual(card.state, "confirmed")
                self.assertTrue(card.payment_id)
                card.action_undo()
                self.assertEqual(card.state, "draft")
                # «Elegir cliente», then «Descartar»
                other = self.env["res.partner"].create({"name": "Kiosco Permisos %s" % user.id})
                self.env["distri.mp.assign"].with_user(user).create(
                    {"collection_id": card.id, "partner_id": other.id,
                     "learn": False}).action_assign()
                self.assertEqual(card.partner_id, other)
                self.env["distri.mp.ignore"].with_user(user).create(
                    {"collection_id": card.id, "reason": "other",
                     "note": "prueba"}).action_ignore()
                self.assertEqual(card.state, "ignored")
                payment = self.env["account.payment"].with_user(user).create(
                    self._payment_vals())
                payment.action_post()
                self.assertNotEqual(payment.state, "draft")

    def test_cobros_user_vouches_and_adjusts(self):
        candidate = self._card(receipt_candidate=True).with_user(self.cashier)
        action = candidate.action_open_vouch()
        self.env["distri.mp.vouch"].with_user(self.cashier).browse(action["res_id"]).action_apply()
        self.assertEqual(candidate.state, "confirmed")
        self.assertTrue(candidate.confirmed_without_receipt)
        wizard = self._adjustment_wizard(self.cashier)
        wizard.action_apply()
        self.assertTrue(self.env["distri.balance.adjustment"].search(
            [("partner_id", "=", self.customer.id)]))

    def test_route_settlement_cash_is_not_reserved(self):
        # the cash the driver hands in is registered by the settlement itself
        # (models/reparto.py, with sudo), whoever of administración closes it
        order = self._order(self.customer, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        self._sync([self._event(route, "visit", self._visit(stop, cash=300))])
        self.assertEqual(stop.cash_amount, 300)
        route.sudo().journal_id = self.cash
        payment = route.with_user(self.admin_user)._register_stop_cash(stop)
        self.assertEqual(payment.partner_id, self.customer)
        self.assertNotEqual(payment.state, "draft")

    # ------------------------------------------------------------ still readable

    def test_readonly_users_keep_reading(self):
        card = self._card()
        form = self.env.ref("distri_pedido.view_mp_collection_form").id
        kanban = self.env.ref("distri_pedido.view_mp_collection_kanban").id
        for user in self.readonly_users:
            with self.subTest(user=user.login):
                mine = card.with_user(user)
                self.assertEqual(mine.read(["amount", "partner_id", "state"])[0]["amount"], 1500.0)
                self.assertFalse(mine.can_authorize)
                views = self.Collection.with_user(user).get_views(
                    [(kanban, "kanban"), (form, "form")])["views"]
                self.assertNotIn('name="action_confirm_board"', views["form"]["arch"])
                self.assertNotIn('name="action_ignore"', views["form"]["arch"])
                self.assertIn('name="action_view_receipt"', views["form"]["arch"])
                self.assertIn("can_authorize", views["kanban"]["arch"])
                self.assertFalse(self.Collection.with_user(user).distri_board_counters()
                                 ["can_authorize"])
                statement = self.customer.with_user(user)._distri_statement()
                self.assertEqual(statement["closing"], 5000.0)
                self.assertTrue(self.customer.with_user(user).action_distri_estado_cuenta())
                report = self.env["distri.debtor.report"].with_user(user).create({})
                self.assertTrue(report.exists())
        views = self.Collection.with_user(self.cashier).get_views([(form, "form")])["views"]
        self.assertIn('name="action_confirm_board"', views["form"]["arch"])
        self.assertTrue(card.with_user(self.cashier).can_authorize)

    def test_old_admin_gates_still_refuse_sellers(self):
        seller = new_test_user(self.env, login="vendedor_permisos",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(UserError):
            self._card().with_user(seller).action_confirm()
