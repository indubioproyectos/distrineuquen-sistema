"""19.0.1.129.38: «Tablero de verificación», second slice
(models/verificacion_tramo2.py, controllers/verificacion.py): queue 9
«Entregados a confirmar» (decision 2 of 24/11), «Reclamar por WhatsApp» and
«Comprobante de otro cliente» in queue 7, the filter «Sin cliente» (#48),
«Comprobantes subidos» (#10, #66), the round-amount rule on the report's ties
of credited receipts, and the cleanup of the old board alerts. Every rule
with distri_pedido.verificacion_v2 on, and with it off as in 129.37. No
balance moves: every test compares the ledger.

Every customer, amount and operation here is fictitious."""
import re
from datetime import datetime, timedelta
from urllib.parse import unquote

from odoo import Command, fields
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers.kit import screen_parents
from odoo.addons.distri_pedido.models import verificacion as vf
from odoo.addons.distri_pedido.models import verificacion_tramo2 as v2
from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
from odoo.addons.distri_pedido.models.cobros import ALERT_SUMMARY
from odoo.addons.distri_pedido.models.reparto_a_confirmar import CONFIRM_SUMMARY
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_1_subir import UploadMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin, _local, _uuid
from odoo.addons.distri_pedido.tests.test_v129_25_reparto import V12925Mixin

GROUP = "distri_pedido.grupo_ingresos_reporte"


def _ledger(env):
    return env["distri.promised.transfer"]._distri_ledger_fingerprint()


def _flag(env, on=True):
    env["ir.config_parameter"].sudo().set_param(vf.PARAM_VERIFICACION, "1" if on else "0")


class BoardMixin(UploadMixin):

    @classmethod
    def _setup_board(cls):
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

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        cls.gonzalo = user("gonzalo_v129_38", "distri_pedido.grupo_gerencia,"
                           "distri_pedido.grupo_administracion,distri_pedido.grupo_cobros,"
                           "distri_pedido.grupo_cajero")
        cls.denis = user("denis_v129_38", "distri_pedido.grupo_administracion,"
                         "distri_pedido.grupo_cobros,distri_pedido.grupo_cajero")
        cls.sistemas = user("sistemas_v129_38", "distri_pedido.grupo_administracion,"
                            "distri_pedido.grupo_cobros," + GROUP)
        cls.cashier = user("cajero_v129_38", "distri_pedido.grupo_cajero")
        cls.seller = user("vendedor_v129_38", "distri_pedido.grupo_vendedor")
        cls.partner = cls.env["res.partner"].create({
            "name": "Almacén Prueba V129.38", "company_type": "company",
            "distri_whatsapp": "2995553838"})
        cls.other = cls.env["res.partner"].create({"name": "Kiosco Otro V129.38",
                                                   "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Message = cls.env["distri.whatsapp.message"]
        cls.Board = cls.env["distri.verificacion"]

    _operation = 712938000000
    _amount = 38000.0

    def _next(self):
        # never round: 38.170, 38.340...
        type(self)._operation += 1
        type(self)._amount += 170
        return str(self._operation), self._amount

    def _income(self, operation=None, amount=None, when=None, **vals):
        if not operation:
            operation, amount = self._next()
        values = {"kind": "income", "source_id": operation, "amount": amount,
                  "date": when or fields.Datetime.now() - timedelta(days=3),
                  "method_type": "bank_transfer", "partner_id": self.partner.id,
                  "proposal": "cuit"}
        values.update(vals)
        return self.Collection.sudo().create(values)

    def _pending_receipt(self, partner, amount, **vals):
        """A receipt waiting for the report, written as the reading leaves it
        (no matching run)."""
        CreditMixin.seq += 1
        values = {"wa_message_id": "v129-38.%s.%s" % (CreditMixin.seq, _uuid()),
                  "direction": "in", "message_type": "image", "body": "",
                  "timestamp": fields.Datetime.now() - timedelta(days=3),
                  "wa_id": "549299%07d" % CreditMixin.seq, "profile_name": "Cliente",
                  "distri_partner_id": partner.id, "distri_is_receipt": True,
                  "distri_receipt_state": "pending", "distri_amount": amount}
        values.update(vals)
        return self.Message.sudo().with_context(distri_defer_receipt_read=True).create(values)

    def _report(self, *rows):
        header = ("TRANSACTION_DATE;SOURCE_ID;PAYMENT_METHOD_TYPE;TRANSACTION_TYPE;"
                  "TRANSACTION_AMOUNT;PAYER_NAME;PAYMENT_METHOD;PAYER_ID_TYPE;PAYER_ID_NUMBER")
        lines = [header]
        for source, when, amount in rows:
            local = _local(when).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
            lines.append(";".join([local, source, "bank_transfer", "SETTLEMENT",
                                   "%.2f" % amount, "PAGADOR PRUEBA", "bank_transfer", "", ""]))
        CreditMixin.seq += 1
        return self.env["distri.mp.report"]._import_file(
            "v129-38-%s.csv" % CreditMixin.seq, ("\n".join(lines) + "\n").encode())


@tagged("post_install", "-at_install", "distri_v129_38")
class TestV12938Board(BoardMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_board()
        cls._setup_reparto()

    def _rows(self, key, user=None, filtro=""):
        return self.Board._rows(key, user or self.denis, filtro=filtro)

    def _row(self, key, record, filtro=""):
        found = [r for r in self._rows(key, filtro=filtro) if r["id"] == record.id]
        return found[0] if found else None

    # ------------------------------------------------------------ queue 7: claim

    def test_claim_by_whatsapp(self):
        _flag(self.env)
        income = self._income()
        before = _ledger(self.env)
        row = self._row("sin_comprobante", income)
        self.assertTrue(row and row["claim"], row)
        url = income.with_user(self.denis)._distri_v2_claim()
        self.assertTrue(url.startswith("https://wa.me/5492995553838?text="), url)
        text = unquote(url.split("text=", 1)[1])
        self.assertIn("transferencia", text)
        self.assertIn(vf._money(income.amount), text)
        self.assertIn("comprobante", text)
        self.assertIn(self.env.company.name, text)
        note = income.message_ids.filtered(lambda m: "Reclamado por WhatsApp" in (m.body or ""))
        self.assertTrue(note)
        self.assertIn(self.denis.name, note[0].body)
        self.assertEqual(income.state, "draft", "only a note")
        self.assertTrue(income._distri_v2_in_no_receipt_queue())
        # no WhatsApp: no button, no link
        silent = self._income(partner_id=self.other.id)
        self.assertFalse(self._row("sin_comprobante", silent)["claim"])
        self.assertEqual(silent._distri_v2_claim(), "")
        self.assertEqual(_ledger(self.env), before)

    # ------------------------------------------------------------ queue 7: another customer

    def test_receipt_of_another_customer_by_operation(self):
        _flag(self.env)
        income = self._income()
        self._pending_receipt(self.other, income.amount, distri_operation=income.source_id)
        row = self._row("sin_comprobante", income)
        self.assertIn("Comprobante de otro cliente", row["other"])
        self.assertIn(self.other.name, row["other"])
        self.assertIn("número de operación", row["other"])
        self.assertIn(income.id, [r["id"] for r in self._rows(
            "sin_comprobante", filtro=v2.FILTER_OTHER)])

    def test_receipt_of_another_customer_by_cuit_or_time(self):
        _flag(self.env)
        by_cuit = self._income(payer_cuit="20383838389")
        self._pending_receipt(self.other, by_cuit.amount, distri_payer_cuit="20-38383838-9")
        self.assertIn("CUIT", self._row("sin_comprobante", by_cuit)["other"])
        by_time = self._income()
        self._pending_receipt(self.other, by_time.amount,
                              distri_receipt_date=by_time.date + timedelta(seconds=50))
        self.assertIn("la hora", self._row("sin_comprobante", by_time)["other"])

    def test_no_flag_without_a_sure_sign(self):
        _flag(self.env)
        income = self._income()
        # the same customer's receipt is not «another customer»
        self._pending_receipt(self.partner, income.amount, distri_operation=income.source_id)
        # another customer, same amount, but another operation number
        self._pending_receipt(self.other, income.amount, distri_operation="999938000001")
        # another customer, same amount, far in time and no CUIT
        self._pending_receipt(self.other, income.amount,
                              distri_receipt_date=income.date - timedelta(hours=5))
        # another customer, matched already
        self._pending_receipt(self.other, income.amount, distri_operation=income.source_id,
                              distri_receipt_state="matched")
        self.assertEqual(self._row("sin_comprobante", income)["other"], "")
        self.assertNotIn(income.id, [r["id"] for r in self._rows(
            "sin_comprobante", filtro=v2.FILTER_OTHER)])

    # ------------------------------------------------------------ «sin cliente» (#48)

    def test_unidentified_filter(self):
        _flag(self.env)
        badge = self.Board._badge(self.denis)
        unknown = self._income(partner_id=False, proposal="none")
        old = self._income(partner_id=False, proposal="none",
                           when=datetime(2026, 8, 1, 15, 0))
        old.sudo().historic = True
        self.assertEqual(unknown.column, "unidentified")
        ids = [r["id"] for r in self._rows("sin_comprobante", filtro=v2.FILTER_UNIDENTIFIED)]
        self.assertIn(unknown.id, ids)
        self.assertNotIn(old.id, ids, "historic ones stay in queue 8")
        self.assertNotIn(unknown.id, [r["id"] for r in self._rows("sin_comprobante")])
        self.assertGreaterEqual(self.Board._distri_v2_unidentified_count(), 1)
        row = self._row("sin_comprobante", unknown, filtro=v2.FILTER_UNIDENTIFIED)
        self.assertEqual(row["partner"], "Sin cliente")
        self.assertEqual(row["url"], "", "administración does not open the report's incomes")
        sistemas = [r for r in self.Board._rows("sin_comprobante", self.sistemas,
                                                filtro=v2.FILTER_UNIDENTIFIED)
                    if r["id"] == unknown.id]
        self.assertTrue(sistemas[0]["url"])
        self.assertEqual(self.Board._badge(self.denis), badge, "informs only: not on Inicio")

    # ------------------------------------------------------------ «Comprobantes subidos»

    def test_uploads_by_user(self):
        _flag(self.env)
        before = _ledger(self.env)
        _op, amount = self._next()
        self._upload(self.denis, partner=self.partner, amount=amount, credit=False)
        self._upload(self.gonzalo, partner=self.partner, amount=amount + 1, credit=False)
        self._pending_receipt(self.other, 1234.0, timestamp=fields.Datetime.now())
        data = self.Board._distri_v2_uploads(days=1)
        groups = {g["who"]: g for g in data["groups"]}
        self.assertEqual(groups[self.denis.name]["total"], 1)
        self.assertEqual(groups[self.denis.name]["buckets"]["pendiente"], 1)
        self.assertIn(self.gonzalo.name, groups)
        self.assertIn("Cliente · por WhatsApp", groups)
        self.assertIn("quien=", groups[self.denis.name]["url"])
        mine = self.Board._distri_v2_uploads(days=1, who=self.denis.name)
        self.assertEqual({r["who"] for r in mine["rows"]}, {self.denis.name})
        self.assertFalse(self.Board._distri_v2_uploads(days=1, who=self.denis.name,
                                                       bucket="aprobado")["rows"])
        self.assertEqual(self.Board._distri_v2_uploads(days=999)["days"], v2.UPLOAD_DAYS_MAX)
        self.assertEqual(_ledger(self.env), before)

    # ------------------------------------------------------------ round rule, credited ties

    def _credited(self, amount, when, operation=None):
        msg = self._receipt(amount, user=self.denis, when=when, operation=operation)
        payment = self._credit(msg, user=self.denis, confirmed=True)
        self.assertTrue(payment)
        return msg, payment

    def test_round_amount_credited_tie_waits_for_a_person(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        _msg, _payment = self._credited(40000, when)
        payments = self.env["account.payment"].search_count([])
        before = _ledger(self.env)
        self._report(("712938990001", when + timedelta(minutes=1), 40000))
        income = self.Collection.sudo().search([("source_id", "=", "712938990001")])
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.proposal, "credited")
        self.assertIn("Importe redondo", income.proposal_note)
        self.assertEqual(self.env["account.payment"].search_count([]), payments)
        self.assertEqual(_ledger(self.env)[1:], before[1:], "no entry, payment or reconcile")

    def test_round_amount_with_the_exact_operation_still_ties(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        _msg, payment = self._credited(50000, when, operation="712938990002")
        self._report(("712938990002", when + timedelta(minutes=1), 50000))
        income = self.Collection.sudo().search([("source_id", "=", "712938990002")])
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, payment)

    def test_not_round_or_flag_off_ties_as_before(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        _msg, payment = self._credited(41170, when)
        self._report(("712938990003", when + timedelta(minutes=1), 41170))
        income = self.Collection.sudo().search([("source_id", "=", "712938990003")])
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, payment)
        _flag(self.env, False)
        when = fields.Datetime.now() - timedelta(minutes=20)
        _msg, payment = self._credited(60000, when)
        self._report(("712938990004", when + timedelta(minutes=1), 60000))
        income = self.Collection.sudo().search([("source_id", "=", "712938990004")])
        self.assertEqual(income.state, "confirmed", "flag off: the round amount ties (129.37)")
        self.assertEqual(income.payment_id, payment)

    def test_round_amount_in_the_audit(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        msg, _payment = self._credited(70000, when)
        card = self._card(msg)
        # an income loaded without the report's matching
        income = self._income("712938990005", 70000.0, when=when + timedelta(minutes=1),
                              partner_id=False, proposal="none")
        self._report(("712938990006", fields.Datetime.now(), 1717.0))
        before = _ledger(self.env)
        self.env["distri.mp.audit"]._run()
        self.assertEqual(income.state, "draft")
        self.assertEqual(income.proposal, "credited")
        self.assertIn("Importe redondo", income.proposal_note)
        self.assertFalse(card.income_id)
        self.assertEqual(card.audit_state, "pendiente")
        self.assertEqual(_ledger(self.env), before)

    # ------------------------------------------------------------ flag off

    def test_flag_off_identical(self):
        income = self._income()
        self.assertFalse(vf.verificacion_on(self.env))
        # the stop rule falls back to 129.25 (every administración user)
        Stop = self.env["distri.route.stop"]
        admins = self.env.ref("distri_pedido.grupo_administracion").all_user_ids.filtered(
            lambda u: u.active and not u.share and u != self.env.ref("base.user_root"))
        self.assertEqual(Stop._distri_admin_users(), admins.sorted("id"))
        self.assertIn(self.gonzalo, Stop._distri_admin_users())
        _flag(self.env)
        # 19.0.1.129.39: by login only (Ale = «admin» and Denis)
        self.env["ir.config_parameter"].sudo().set_param(
            v2.PARAM_CONFIRM_LOGINS, "admin,%s" % self.denis.login)
        self.assertNotIn(self.gonzalo, Stop._distri_admin_users(), "gerencia is not Denis")
        self.assertIn(self.denis, Stop._distri_admin_users())
        self.assertNotIn(self.sistemas, Stop._distri_admin_users(), "only the listed logins")
        self.assertIn(self.env.ref("base.user_admin"), Stop._distri_admin_users())
        self.assertNotIn(self.cashier, Stop._distri_admin_users())
        self.assertTrue(income.exists())

    def test_queue_screens_render(self):
        _flag(self.env)
        income = self._income()
        self._pending_receipt(self.other, income.amount, distri_operation=income.source_id)
        from odoo.addons.distri_pedido.controllers.kit import kit_context
        base = {"dn": kit_context(), "user": self.denis, "inicio": "/tablero",
                "csrf_token": "x", "nav": [], "error": "", "ok": ""}
        QWeb = self.env["ir.qweb"].with_user(self.denis)
        html = str(QWeb._render("distri_pedido.verificacion_cola", dict(
            base, cola=self.Board._summary("sin_comprobante", self.denis),
            filas=self._rows("sin_comprobante"), filtro="", sin_cliente=3)))
        self.assertIn("Reclamar por WhatsApp", html)
        self.assertIn("Comprobante de otro cliente", html)
        self.assertIn("Sin cliente · 3", html)
        html = str(QWeb._render("distri_pedido.verificacion_page", dict(
            base, colas=self.Board._summaries(self.denis), subir=True, sin_cliente=3,
            subidos=True)))
        self.assertIn("Entregados a confirmar", html)
        self.assertIn("Comprobantes subidos", html)
        self.assertIn("Además 3 sin cliente", html)
        html = str(QWeb._render("distri_pedido.verificacion_subidos", dict(
            base, data=self.Board._distri_v2_uploads(days=7))))
        self.assertIn("Comprobantes subidos", html)
        self.assertEqual(screen_parents("/verificacion/subidos"), ("/verificacion",))
        self.assertEqual(screen_parents("/verificacion/entregados_a_confirmar"),
                         ("/verificacion",))


@tagged("post_install", "-at_install", "distri_v129_38")
class TestV12938ToConfirm(V12925Mixin, AccountTestInvoicingCommon):
    """Decision 2 of 24/11/2026: the delivery «a confirmar» after 48 h goes
    to Ale and Denis, and to queue 9."""

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_v129_25()
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({"name": "Almacén A Confirmar V12938",
                                                     "company_type": "company"})
        _flag(cls.env, False)

    def _to_confirm_stop(self):
        order = self._order(self.partner, self.product, qty=10, price=100.0)
        order._create_invoices().action_post()
        card = self._card(self.partner, 1000, order=order)
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(
            stop, already_paid=True, to_confirm=True))])
        self.assertEqual(res[0]["status"], "ok", res)
        return card, route, stop

    def test_escalated_to_ale_and_denis_and_queue_9(self):
        _flag(self.env)
        # 19.0.1.129.39: the To-Do goes to the listed logins (Ale and Denis)
        self.env["ir.config_parameter"].sudo().set_param(
            v2.PARAM_CONFIRM_LOGINS, "admin,%s" % self.admin_user.login)
        card, _route, stop = self._to_confirm_stop()
        self.assertEqual(stop.state, "already_paid", "valid for the delivery")
        self.assertEqual(stop.paid_confirm_state, "to_confirm")
        Board = self.env["distri.verificacion"]
        self.assertFalse([r for r in Board._rows("entregados_a_confirmar", self.admin_user)])
        before = _ledger(self.env)
        Stop = self.env["distri.route.stop"]
        since = stop.paid_confirm_since
        _c, escalated = Stop._distri_cron_paid_to_confirm(now=since + timedelta(hours=49))
        self.assertEqual(escalated, stop)
        todos = card.activity_ids.filtered(lambda a: a.summary == CONFIRM_SUMMARY)
        self.assertIn(self.admin_user, todos.user_id, "Denis: administración")
        self.assertIn(self.env.ref("base.user_admin"), todos.user_id, "Ale")
        self.assertNotIn(self.manager, todos.user_id, "gerencia is not Ale nor Denis")
        self.assertTrue(all(vf.is_owner(u) or not u.has_group(vf.GERENCIA)
                            for u in todos.user_id))
        rows = Board._rows("entregados_a_confirmar", self.admin_user)
        self.assertEqual([(r["model"], r["id"]) for r in rows], [("distri.route.stop", stop.id)])
        self.assertIn("a confirmar", rows[0]["reason"])
        self.assertEqual(rows[0]["url"], "/odoo/distri.mp.collection/%s" % card.id)
        summary = Board._summary("entregados_a_confirmar", self.admin_user)
        self.assertEqual(summary["count"], 1)
        self.assertEqual(summary["who"], "Denis y Ale")
        self.assertTrue(vf.queue_member(self.admin_user, "entregados_a_confirmar"))
        # credited later: confirmed, out of the queue
        card.write({"state": "confirmed"})
        Stop._distri_cron_paid_to_confirm(now=since + timedelta(hours=61))
        self.assertEqual(stop.paid_confirm_state, "confirmed")
        self.assertFalse(Board._rows("entregados_a_confirmar", self.admin_user))
        self.assertEqual(_ledger(self.env), before)

    def test_flag_off_every_administracion_user(self):
        card, _route, stop = self._to_confirm_stop()
        Stop = self.env["distri.route.stop"]
        Stop._distri_cron_paid_to_confirm(now=stop.paid_confirm_since + timedelta(hours=49))
        todos = card.activity_ids.filtered(lambda a: a.summary == CONFIRM_SUMMARY)
        admins = self.env.ref("distri_pedido.grupo_administracion").all_user_ids.filtered(
            lambda u: u.active and not u.share and u != self.env.ref("base.user_root"))
        self.assertEqual(todos.user_id, admins, "129.37: one per administración user")
        self.assertIn(self.manager, todos.user_id)


@tagged("post_install", "-at_install", "distri_v129_38")
class TestV12938AjustarSaldo(AccountTestInvoicingCommon):
    """«Ajustar saldo» proposes the statement's balance (a real case of
    25/11/2026: a paid lab invoice missing from the review's figure)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Ajuste V12938",
                                                     "company_type": "company"})
        cls.cash = cls.company_data["default_journal_cash"]
        cls.seller = new_test_user(cls.env, login="vendedor_ajuste_v12938",
                                   groups="base.group_user,distri_pedido.grupo_vendedor")

    def _history(self, debe):
        self.env["distri.cuenta.movimiento"].create({
            "partner_id": self.partner.id, "fecha": datetime(2026, 8, 1).date(),
            "comprobante": "0001-2026-0%s" % int(debe), "tipo": "factura", "debe": debe,
            "haber": 0.0})

    def _pay(self, amount, invoice=None):
        pay = self.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer",
            "partner_id": self.partner.id, "amount": amount, "journal_id": self.cash.id,
            "date": fields.Date.today(), "memo": "Planilla de prueba"})
        pay.action_post()
        if invoice:
            lines = (invoice.line_ids | pay.move_id.line_ids).filtered(
                lambda l: l.account_id.account_type == "asset_receivable" and not l.reconciled)
            lines.reconcile()
        return pay

    def _lab_case(self):
        """Online history $ 500.000, a lab payment of $ 200.000 and a lab
        invoice of $ 106.200 paid in the lab (S00324-like)."""
        self._history(500000)
        self._pay(200000)
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create({"name": "mercadería", "quantity": 1,
                                                 "price_unit": 106200,
                                                 "tax_ids": [Command.clear()]})]})
        invoice.action_post()
        self._pay(106200, invoice)
        self.assertEqual(invoice.payment_state in ("paid", "in_payment"), True)
        return invoice

    def _wizard(self, **vals):
        action = self.partner.action_distri_balance_adjustment()
        vals.setdefault("motive", "Saldo confirmado por la gerencia")
        vals.setdefault("authorized_by", "Gerente de prueba")
        return self.env[action["res_model"]].with_context(action["context"]).create(vals)

    def test_paid_lab_invoice_proposes_the_statement(self):
        self._lab_case()
        Review = self.env["distri.balance.review"]
        self.assertAlmostEqual(self.partner._distri_account_balance(), 300000)
        self.assertEqual(Review._distri_lab_balance(self.partner), 193800,
                         "the review misses the lab invoice (the bug's cause)")
        wizard = self._wizard(target_balance=250000)
        self.assertEqual(wizard.current_balance, 300000, "the statement's figure")
        self.assertEqual(wizard.review_balance, 193800)
        self.assertTrue(wizard.balances_differ)
        self.assertEqual(wizard.difference, -50000)
        moves = self.env["account.move"].search_count([])
        payments = self.env["account.payment"].search_count([])
        partials = self.env["account.partial.reconcile"].search_count([])
        wizard.action_apply()
        adjustment = self.env["distri.balance.adjustment"].search(
            [("partner_id", "=", self.partner.id)])
        self.assertEqual(adjustment.balance_before, 300000)
        self.assertEqual(adjustment.review_balance_before, 193800)
        self.assertEqual(adjustment.difference, -50000)
        self.assertEqual(adjustment.move_id.move_type, "out_refund")
        self.assertEqual(adjustment.move_id.amount_total, 50000)
        self.assertEqual(adjustment.move_id.journal_id.code, "AJC")
        # exactly one new move (the AJC credit note), no payment, no reconcile
        self.assertEqual(self.env["account.move"].search_count([]), moves + 1)
        self.assertEqual(self.env["account.payment"].search_count([]), payments)
        self.assertEqual(self.env["account.partial.reconcile"].search_count([]), partials)
        # the statement now says the real balance
        self.assertAlmostEqual(self.partner._distri_account_balance(), 250000)
        self.assertAlmostEqual(self.partner._distri_statement()["balance"], 250000)
        note = self.partner.message_ids.filtered(lambda m: "Ajuste de cuenta" in (m.body or ""))
        self.assertIn("estado de cuenta", note[0].body)

    def test_same_figures_show_one_balance(self):
        self._history(10000)
        self._pay(2000)
        wizard = self._wizard()
        self.assertEqual(wizard.current_balance, 8000)
        self.assertEqual(wizard.review_balance, 8000)
        self.assertFalse(wizard.balances_differ)

    def test_zero_against_the_statement_is_refused(self):
        self._lab_case()
        with self.assertRaisesRegex(Exception, "estado de cuenta ya da"):
            self._wizard(target_balance=300000).action_apply()
        self.assertFalse(self.env["distri.balance.adjustment"].search(
            [("partner_id", "=", self.partner.id)]))

    def test_only_administration(self):
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.partner.with_user(self.seller).action_distri_balance_adjustment()


@tagged("post_install", "-at_install", "distri_v129_38")
class TestV12938OldAlerts(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.user = new_test_user(cls.env, login="admin_alertas_v12938",
                                 groups="base.group_user,distri_pedido.grupo_administracion")
        cls.Collection = cls.env["distri.mp.collection"].sudo()
        cls.todo = cls.env.ref("mail.mail_activity_data_todo")

    _operation = 712938500000

    def _income(self, **vals):
        type(self)._operation += 1
        values = {"kind": "income", "source_id": str(self._operation), "amount": 3838.0,
                  "date": fields.Datetime.now(), "method_type": "bank_transfer"}
        values.update(vals)
        return self.Collection.create(values)

    def _no_money(self, minute, **vals):
        receipt = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.test.v129_38.%s" % minute, "direction": "in",
            "wa_id": "5492995013838", "message_type": "image", "body": "$ 3.000",
            "timestamp": datetime(2026, 11, 20, 10, minute), "media_mime_type": "image/jpeg",
            "media_id": "123", "media_state": "pending"})
        values = {"kind": "receipt", "receipt_id": receipt.id, "amount": 3000,
                  "date": receipt.timestamp}
        values.update(vals)
        return self.Collection.create(values)

    def _alert(self, card, summary=ALERT_SUMMARY, note="Hace más de 24 h"):
        return card.with_context(mail_activity_quick_update=True).activity_schedule(
            activity_type_id=self.todo.id, summary=summary, user_id=self.user.id, note=note)

    def _created(self, card, when):
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET create_date = %s WHERE id = %s",
                            (when, card.id))
        card.invalidate_recordset(["create_date"])

    def test_closes_only_the_old_ones(self):
        income = self._income()
        live = self._no_money(1)
        self.assertEqual(live.column, "no_money")
        resolved = self._no_money(2)
        old = self._no_money(3)
        self._created(old, datetime(2026, 8, 1, 12, 0))
        a_income, a_live, a_old = self._alert(income), self._alert(live), self._alert(old)
        a_resolved = self._alert(resolved)
        resolved.write({"state": "ignored"})
        other = self._alert(income, summary="Llamar al cliente")
        blank = self._alert(income, summary="x")
        # what practica/sanitizar.sql leaves: no summary, no note
        self.env.flush_all()
        self.env.cr.execute("UPDATE mail_activity SET summary = NULL, note = NULL WHERE id = %s",
                            (blank.id,))
        blank.invalidate_recordset(["summary", "note"])
        states = {c.id: (c.state, c.payment_id.id) for c in income | live | resolved | old}
        ledger = _ledger(self.env)
        counts = self.Collection._distri_v2_close_old_alerts()
        self.assertEqual(counts, {"income": 1, "resolved": 1, "old": 1})
        self.assertTrue(all(a.active for a in a_income | a_live | a_old | a_resolved),
                        "dry run by default")
        counts = self.Collection._distri_v2_close_old_alerts(include_blank=True)
        self.assertEqual(counts["income"], 2, "the sanitized one only on request")
        counts = self.Collection._distri_v2_close_old_alerts(apply=True)
        self.assertEqual(sum(counts.values()), 3)
        closed = (a_income | a_old | a_resolved).with_context(active_test=False)
        self.assertFalse(any(closed.mapped("active")), "marked done")
        self.assertTrue(a_live.active, "a live alert stays")
        self.assertTrue(other.active and blank.active)
        self.assertTrue(any("limpieza de la v129.38" in (m.body or "")
                            for m in income.message_ids))
        self.assertEqual({c.id: (c.state, c.payment_id.id)
                          for c in income | live | resolved | old}, states)
        self.assertEqual(_ledger(self.env), ledger)
        self.assertEqual(sum(self.Collection._distri_v2_close_old_alerts().values()), 0)


@tagged("post_install", "-at_install", "distri_v129_38")
class TestV12938Routes(BoardMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_board()

    def test_routes(self):
        income = self._income()
        self.authenticate(self.denis.login, self.denis.login)
        for url in ("/verificacion/subidos", "/verificacion/entregados_a_confirmar"):
            self.assertEqual(self.url_open(url).status_code, 404, "flag off: %s" % url)
        _flag(self.env)
        for url in ("/verificacion", "/verificacion/subidos", "/verificacion/subidos?dias=30",
                    "/verificacion/entregados_a_confirmar",
                    "/verificacion/sin_comprobante?filtro=sin_cliente",
                    "/verificacion/sin_comprobante?filtro=otro_cliente"):
            res = self.url_open(url)
            self.assertEqual(res.status_code, 200, url)
        page = self.url_open("/verificacion/sin_comprobante").text
        self.assertIn("Reclamar por WhatsApp", page)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        before = _ledger(self.env)
        res = self.url_open("/verificacion/reclamar", data={
            "csrf_token": token, "card_id": income.id}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].startswith("https://wa.me/5492995553838?text="))
        res = self.url_open("/verificacion/reclamar", data={
            "csrf_token": token, "card_id": 0}, allow_redirects=False)
        self.assertIn("/verificacion/sin_comprobante?error=", res.headers["Location"])
        self.assertEqual(_ledger(self.env), before)
        # roles: a cashier and a seller have no queue
        for user in (self.cashier, self.seller):
            self.authenticate(user.login, user.login)
            for url in ("/verificacion/subidos", "/verificacion/sin_comprobante"):
                self.assertEqual(self.url_open(url).status_code, 403, (user.login, url))
            res = self.url_open("/verificacion/reclamar", data={
                "csrf_token": token, "card_id": income.id}, allow_redirects=False)
            self.assertIn(res.status_code, (400, 403), user.login)
