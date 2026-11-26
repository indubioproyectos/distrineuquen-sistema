"""19.0.1.129.31: «Tablero de verificación», first slice
(models/verificacion.py, controllers/verificacion.py). Every rule with the
flag distri_pedido.verificacion_v2 on, and the same cases with it off
behaving exactly as before.

Every customer, amount and operation here is fictitious."""
from datetime import datetime, timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers import tablero
from odoo.addons.distri_pedido.controllers.kit import screen_parents
from odoo.addons.distri_pedido.models import verificacion as vf
from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_1_subir import UploadMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin, _local, _png, _uuid

GROUP = "distri_pedido.grupo_ingresos_reporte"


@tagged("post_install", "-at_install", "distri_v129_31")
class TestV12931Verificacion(UploadMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        params.set_param(PARAM_DRIVER_RULE, "")
        params.set_param(vf.PARAM_VERIFICACION, "0")
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
        cls.gonzalo = user("gonzalo_v129_31", "distri_pedido.grupo_gerencia,"
                           "distri_pedido.grupo_administracion,distri_pedido.grupo_cobros,"
                           "distri_pedido.grupo_cajero")
        cls.denis = user("denis_v129_31", "distri_pedido.grupo_administracion,"
                         "distri_pedido.grupo_cobros,distri_pedido.grupo_cajero")
        cls.sistemas = user("sistemas_v129_31", "distri_pedido.grupo_administracion,"
                            "distri_pedido.grupo_cobros," + GROUP)
        cls.cashier = user("cajero_v129_31", "distri_pedido.grupo_cajero")
        cls.seller = user("vendedor_v129_31", "distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V129.31",
                                                     "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Message = cls.env["distri.whatsapp.message"]
        cls.Board = cls.env["distri.verificacion"]

    # ------------------------------------------------------------ helpers

    _operation = 712931000000
    _amount = 31000.0

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(vf.PARAM_VERIFICACION,
                                                         "1" if on else "0")

    def _next(self):
        # never round: 31.170, 31.340...
        type(self)._operation += 1
        type(self)._amount += 170
        return str(self._operation), self._amount

    def _income(self, operation=None, amount=None, when=None, **vals):
        """A pending income of the report, loaded before the receipt."""
        if not operation:
            operation, amount = self._next()
        values = {"kind": "income", "source_id": operation, "amount": amount,
                  "date": when or fields.Datetime.now() - timedelta(minutes=10),
                  "method_type": "bank_transfer", "partner_id": self.partner.id,
                  "proposal": "cuit"}
        values.update(vals)
        return self.Collection.sudo().create(values)

    def _customer(self, amount, operation=None, when=None, vals=None):
        """A receipt the customer sent by WhatsApp, read with `amount`,
        `operation` and the time `when`; `vals` on the message itself."""
        CreditMixin.seq += 1
        when = when or (fields.Datetime.now() - timedelta(minutes=3, seconds=CreditMixin.seq))
        values = {
            "wa_message_id": "v129-31.%s.%s" % (CreditMixin.seq, _uuid()),
            "direction": "in", "message_type": "image", "body": "",
            "timestamp": fields.Datetime.now() - timedelta(minutes=1),
            "media_mime_type": "image/png", "wa_id": "549299%07d" % CreditMixin.seq,
            "profile_name": "Cliente",
            "distri_partner_id": self.partner.id, "distri_partner_source": "Elegido en la prueba",
            "distri_is_receipt": True, "distri_receipt_state": "pending",
        }
        values.update(vals or {})
        msg = self.Message.sudo().with_context(distri_defer_receipt_read=True).create(values)
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "v%s.png" % CreditMixin.seq, "raw": _png(CreditMixin.seq + 7000),
            "mimetype": "image/png", "res_model": msg._name, "res_id": msg.id})
        msg = msg.with_context(distri_defer_receipt_read=False)
        msg.with_context(distri_receipt_matching=True).write({"attachment_id": attachment.id})
        reading = {"amount": amount, "amount_confidence": "high", "date": _local(when)}
        if operation:
            reading["operation"] = operation
        with self._reading(reading):
            msg.write({"media_state": "done"})
        return msg

    def _upload_as(self, user, amount, when, operation=None):
        """«Subir y acreditar» of one file read with `amount` at `when`."""
        reading = {"amount": amount, "amount_confidence": "high", "date": _local(when)}
        if operation:
            reading["operation"] = operation
        with self._reading(reading):
            [result] = self.Message.with_user(user)._distri_staff_upload(
                [self._file()], partner=self.partner, credit=True, amounts=["%d" % amount])
        return result, self.Message.sudo().browse(result["id"])

    def _row_ids(self, key, user=None):
        return {(r["model"], r["id"]) for r in self.Board._rows(key, user or self.sistemas)}

    # ------------------------------------------------------------ rules

    def test_round_amount_rule(self):
        for amount, expected in ((10000, True), (50000, True), (120000, True),
                                 (1000000, True), (15000, False), (9990, False),
                                 (20000.5, False), (5000, False), (0, False), (31170, False)):
            with self.subTest(amount=amount):
                self.assertEqual(vf.is_round_amount(amount, 10000.0), expected)
        self.assertTrue(vf.is_round_amount(15000, 5000.0), "the step is a parameter")
        self.assertEqual(vf.round_step(self.env), 10000.0)

    def test_flag_off_the_customer_receipt_only_ties(self):
        """Flag off: today's behavior, the receipt ties to the income and
        waits for a person; no payment."""
        operation, amount = self._next()
        income = self._income(operation, amount)
        payments = self._mp_payments()
        msg = self._customer(amount, operation=operation)
        self.assertEqual(income.receipt_id, msg)
        self.assertEqual(income.state, "draft")
        self.assertEqual(self._mp_payments(), payments)
        self.assertFalse(self.Collection.sudo().search([("auto_credited", "=", True)]))

    def test_customer_receipt_credits_itself_by_operation(self):
        self._flag()
        operation, amount = self._next()
        income = self._income(operation, amount)
        payments = self._mp_payments()
        msg = self._customer(amount, operation=operation)
        self.assertEqual(income.state, "confirmed")
        new = self._mp_payments() - payments
        self.assertEqual(len(new), 1, "one payment, no duplicate")
        self.assertEqual(new, income.payment_id)
        card = self._card(msg)
        self.assertEqual((card.state, card.audit_state), ("confirmed", "verificado"))
        self.assertTrue(card.auto_credited)
        self.assertEqual(card.human_check, "pendiente")
        self.assertEqual(card.income_id, income)
        self.assertIn(("distri.mp.collection", card.id), self._row_ids("revision_humana"))
        # a person always checks it: not a seller, yes Denis
        with self.assertRaises(AccessError):
            card.with_user(self.seller).action_distri_human_check()
        card.with_user(self.denis).action_distri_human_check()
        self.assertEqual((card.human_check, card.human_checked_by_id), ("revisado", self.denis))
        self.assertNotIn(("distri.mp.collection", card.id), self._row_ids("revision_humana"))
        with self.assertRaises(UserError):
            card.with_user(self.denis).action_distri_human_check()

    def test_customer_receipt_by_amount_within_two_minutes(self):
        self._flag()
        when = fields.Datetime.now() - timedelta(minutes=20)
        _operation, amount = self._next()
        income = self._income(amount=amount, operation=_operation, when=when)
        msg = self._customer(amount, when=when + timedelta(seconds=40))
        self.assertEqual(income.receipt_id, msg)
        self.assertEqual(income.state, "confirmed")
        self.assertTrue(self._card(msg).auto_credited)

    def test_customer_receipt_three_minutes_away_waits(self):
        self._flag()
        when = fields.Datetime.now() - timedelta(minutes=30)
        _operation, amount = self._next()
        income = self._income(amount=amount, operation=_operation, when=when)
        payments = self._mp_payments()
        msg = self._customer(amount, when=when + timedelta(minutes=3))
        self.assertEqual(income.receipt_id, msg, "tied by the usual matching")
        self.assertEqual(income.state, "draft")
        self.assertEqual(self._mp_payments(), payments)

    def test_another_operation_never_by_amount(self):
        self._flag()
        when = fields.Datetime.now() - timedelta(minutes=15)
        operation, amount = self._next()
        income = self._income(operation, amount, when=when)
        self._customer(amount, operation="99887766554433", when=when)
        self.assertEqual(income.state, "draft")

    def test_round_amount_never_credits_itself(self):
        self._flag()
        operation, _amount = self._next()
        income = self._income(operation, 50000.0)
        payments = self._mp_payments()
        msg = self._customer(50000.0, operation=operation)
        self.assertEqual(income.receipt_id, msg)
        self.assertEqual(income.state, "draft", "a round amount always goes to a person")
        self.assertEqual(self._mp_payments(), payments)
        self.assertIn(("distri.mp.collection", income.id), self._row_ids("para_acreditar"))

    def test_whatsapp_zip_receipt_never_credits_itself(self):
        self._flag()
        chat = self.env["distri.whatsapp.chat.import"].sudo().create({"name": "v129-31.zip"})
        operation, amount = self._next()
        income = self._income(operation, amount)
        payments = self._mp_payments()
        msg = self._customer(amount, operation=operation,
                             vals={"distri_chat_import_id": chat.id})
        self.assertEqual(income.receipt_id, msg)
        self.assertEqual(income.state, "draft")
        self.assertEqual(self._mp_payments(), payments)
        self.assertIn(("distri.mp.collection", income.id), self._row_ids("para_acreditar"),
                      "always «Para acreditar»")

    def test_receipt_before_the_flag_is_not_credited(self):
        self._flag()
        future = fields.Datetime.now() + timedelta(days=1)
        self.env["ir.config_parameter"].sudo().set_param(vf.PARAM_SINCE,
                                                         fields.Datetime.to_string(future))
        operation, amount = self._next()
        income = self._income(operation, amount)
        self._customer(amount, operation=operation)
        self.assertEqual(income.state, "draft")

    def test_receipt_before_the_report_is_not_credited(self):
        """Only «the report arrived first»: an income loaded after the receipt."""
        self._flag()
        operation, amount = self._next()
        income = self._income(operation, amount)
        msg = self._customer(amount, operation=operation)
        # undo and replay with the income newer than the receipt
        self.assertEqual(income.state, "confirmed")
        operation, amount = self._next()
        income = self._income(operation, amount)
        self.env.cr.execute("UPDATE distri_mp_collection SET create_date = %s WHERE id = %s",
                            (fields.Datetime.now() + timedelta(hours=1), income.id))
        income.invalidate_recordset(["create_date"])
        msg = self._customer(amount, operation=operation)
        self.assertEqual(income.receipt_id, msg)
        self.assertEqual(income.state, "draft")

    def test_staff_round_amount_by_time_waits(self):
        when = fields.Datetime.now() - timedelta(minutes=20)
        for on, expected in ((False, "acreditado"), (True, "pendiente")):
            with self.subTest(flag=on):
                self._flag(on)
                type(self)._operation += 1
                amount = 40000.0 + (10000 if on else 0)
                income = self._income(str(self._operation), amount, when=when)
                result, _msg = self._upload_as(self.cashier, amount, when + timedelta(seconds=40))
                self.assertEqual(result["credit"], expected, result["message"])
                self.assertEqual(income.state, "confirmed" if not on else "draft")

    def test_staff_round_amount_by_operation_still_credits(self):
        self._flag()
        operation, _amount = self._next()
        income = self._income(operation, 70000.0)
        result, _msg = self._upload_as(self.cashier, 70000.0, fields.Datetime.now()
                                       - timedelta(minutes=12), operation=operation)
        self.assertEqual(result["credit"], "acreditado", result["message"])
        self.assertEqual(income.state, "confirmed")

    # ------------------------------------------------------------ twin guard

    def _pair(self, amount):
        when = fields.Datetime.now() - timedelta(hours=5)
        first = self._customer(amount, when=when)
        second = self._customer(amount, when=when)
        return first, second

    def test_flag_off_twin_is_closed_as_before(self):
        _op, amount = self._next()
        first, second = self._pair(amount)
        self.assertEqual(first.distri_receipt_state, "pending")
        self.assertEqual(second.distri_receipt_state, "duplicate")
        self.assertFalse(second.distri_twin_review)

    def test_twin_is_held_for_a_person(self):
        self._flag()
        _op, amount = self._next()
        first, second = self._pair(amount)
        self.assertEqual(second.distri_receipt_state, "pending", "never closed on sight")
        self.assertTrue(second.distri_twin_review)
        self.assertEqual(second.distri_twin_of_id, first)
        self.assertIn(("distri.whatsapp.message", second.id), self._row_ids("duplicados"))
        self.assertNotIn(("distri.mp.collection", self._card(second).id),
                         self._row_ids("para_acreditar"))
        # a held twin is not matched to a transfer by itself
        income = self._income(amount=amount, operation=str(self._operation + 500),
                              when=second.distri_receipt_date)
        self.assertFalse(second._distri_match_income())
        self.assertNotEqual(income.receipt_id, second)
        # crediting it by hand warns first
        _errors, warnings = self._card(second)._distri_credit_review(self.partner, amount)
        self.assertTrue(any("mismo pago" in w for w in warnings), warnings)
        with self.assertRaises(AccessError):
            second.with_user(self.seller).action_distri_twin_two()
        second.with_user(self.denis).action_distri_twin_two()
        self.assertFalse(second.distri_twin_review)
        self.assertTrue(second.distri_twin_ok)
        second.with_context(distri_receipt_matching=True)._distri_classify()
        self.assertFalse(second.distri_twin_review, "never held again")
        self.assertNotEqual(second.distri_receipt_state, "duplicate")

    def test_twin_same_payment_closes_it(self):
        self._flag()
        _op, amount = self._next()
        _first, second = self._pair(amount)
        second.with_user(self.denis).action_distri_twin_same()
        self.assertEqual(second.distri_receipt_state, "duplicate")
        self.assertFalse(second.distri_twin_review)
        self.assertIn("Ya recibido", second.distri_receipt_note)

    def test_twin_with_operation_number_still_closes(self):
        self._flag()
        operation, amount = self._next()
        when = fields.Datetime.now() - timedelta(hours=6)
        self._customer(amount, operation=operation, when=when)
        second = self._customer(amount, operation=operation, when=when)
        self.assertEqual(second.distri_receipt_state, "duplicate",
                         "the same operation number is the same transfer")

    # ------------------------------------------------------------ histórico

    def test_historic_mark_is_a_dry_run_and_needs_the_flag(self):
        old = self._income(when=datetime(2026, 8, 1, 15, 0))
        new = self._income()
        with self.assertRaises(UserError):
            self.Collection._distri_v2_mark_historic(apply=True)
        self._flag()
        balance = self._balance()
        count, total = self.Collection._distri_v2_mark_historic()
        self.assertGreaterEqual(count, 1)
        self.assertGreaterEqual(total, old.amount)
        self.assertFalse(old.historic, "dry run by default")
        self.Collection._distri_v2_mark_historic(apply=True)
        self.assertTrue(old.historic)
        self.assertFalse(new.historic)
        self.assertEqual(old.state, "draft", "only marked: never confirmed nor deleted")
        self.assertTrue(old.exists())
        self.assertEqual(self._balance(), balance, "no balance changes")
        self.assertIn(("distri.mp.collection", old.id), self._row_ids("historicos"))
        self.assertNotIn(("distri.mp.collection", old.id), self._row_ids("sin_comprobante"))
        # hidden by the board's default filter (19.0.1.129.32: added with
        # the flag on only, models/verificacion.py _get_action_dict)
        action = self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_mp_board")
        self.assertIn("search_default_not_historic", action["context"])

    def test_no_receipt_queue_uses_the_term(self):
        self._flag()
        recent = self._income(when=fields.Datetime.now() - timedelta(days=1))
        late = self._income(when=fields.Datetime.now() - timedelta(days=3))
        ids = self._row_ids("sin_comprobante")
        self.assertIn(("distri.mp.collection", late.id), ids)
        self.assertNotIn(("distri.mp.collection", recent.id), ids)
        self.env["ir.config_parameter"].sudo().set_param(vf.PARAM_NO_RECEIPT_DAYS, "4")
        self.assertNotIn(("distri.mp.collection", late.id), self._row_ids("sin_comprobante"))

    # ------------------------------------------------------------ queues and screen

    def test_queue_members(self):
        keys = lambda user: {q[0] for q in vf.user_queues(user)}  # noqa: E731
        # 19.0.1.129.38: queue 9 «Entregados a confirmar»
        admin_queues = {"para_acreditar", "revision_humana", "esperando_reporte", "diferencias",
                        "duplicados", "sin_comprobante", "entregados_a_confirmar"}
        self.assertEqual(keys(self.denis), admin_queues)
        self.assertEqual(keys(self.gonzalo), admin_queues | {"sin_transferencia"})
        self.assertEqual(keys(self.sistemas), set(vf.QUEUE_KEYS), "Ale in every queue")
        self.assertTrue(vf.is_owner(self.env.ref("base.user_admin")))
        self.assertEqual(keys(self.seller), set())
        self.assertEqual(keys(self.cashier), set())

    def test_owner_only_hides_the_board_from_administracion_and_gerencia(self):
        keys = lambda user: {q[0] for q in vf.user_queues(user)}  # noqa: E731
        self._flag()
        self.env["ir.config_parameter"].sudo().set_param(vf.PARAM_OWNER_ONLY, "1")
        self.assertEqual(keys(self.denis), set())
        self.assertEqual(keys(self.gonzalo), set())
        self.assertEqual(keys(self.sistemas), set(vf.QUEUE_KEYS), "Ale keeps every queue")
        for user in (self.denis, self.gonzalo):
            self.assertNotIn("verificacion",
                             [t["key"] for t in tablero.staff_tiles(self.env(user=user))])
        self.assertTrue(vf.verificacion_on(self.env), "the crediting rules stay on")
        self.env["ir.config_parameter"].sudo().set_param(vf.PARAM_OWNER_ONLY, "0")
        self.assertIn("verificacion",
                      [t["key"] for t in tablero.staff_tiles(self.env(user=self.denis))])

    def test_sistemas_direct_tiles_only_for_ale(self):
        extra = {"tablerocobros", "reportemp"}
        keys = lambda user: {t["key"] for t in tablero.staff_tiles(self.env(user=user))}  # noqa: E731
        self.assertLessEqual(extra, keys(self.sistemas))
        for user in (self.denis, self.gonzalo, self.seller, self.cashier):
            self.assertFalse(extra & keys(user), user.login)

    def test_inicio_tile_only_with_the_flag(self):
        denis_env = self.env(user=self.denis)
        self.assertNotIn("verificacion", [t["key"] for t in tablero.staff_tiles(denis_env)])
        self._flag()
        tiles = tablero.staff_tiles(denis_env)
        self.assertIn("verificacion", [t["key"] for t in tiles])
        self.assertNotIn("verificacion",
                         [t["key"] for t in tablero.staff_tiles(self.env(user=self.seller))])
        operation, amount = self._next()
        self._income(operation, 60000.0)
        self._customer(60000.0, operation=operation)
        tablero.tile_badges(denis_env, tiles)
        badge = next(t for t in tiles if t["key"] == "verificacion")["badge"]
        self.assertTrue(badge and badge["n"] >= 1, badge)
        self.assertEqual(screen_parents("/verificacion"), ("inicio",))
        self.assertEqual(screen_parents("/verificacion/duplicados"), ("/verificacion",))

    def test_screens_render(self):
        self._flag()
        _op, amount = self._next()
        self._pair(amount)
        from odoo.addons.distri_pedido.controllers.kit import kit_context
        base = {"dn": kit_context(), "user": self.denis, "inicio": "/tablero",
                "csrf_token": "x", "nav": [], "error": "", "ok": ""}
        html = self.env["ir.qweb"].with_user(self.denis)._render(
            "distri_pedido.verificacion_page",
            dict(base, colas=self.Board.with_user(self.denis)._summaries(self.denis)))
        self.assertIn("Posibles duplicados", str(html))
        self.assertNotIn("Históricos y migración", str(html), "Sistemas only")
        html = self.env["ir.qweb"].with_user(self.denis)._render(
            "distri_pedido.verificacion_cola",
            dict(base, cola=self.Board._summary("duplicados", self.denis),
                 filas=self.Board._rows("duplicados", self.denis)))
        self.assertIn("Son dos pagos", str(html))
