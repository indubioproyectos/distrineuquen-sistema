"""«Caja del día» (19.0.1.97.0, models/caja.py, caja_contabilidad.py,
controllers/caja.py): sessions, receiving settlements and boxes, outflows,
the closing's cash statement (owner's option B), cases, reopening, the
catch-up wizard, permissions and the closing label."""
import base64
import re

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja import CajaQuestion
from odoo.addons.distri_pedido.models.caja_contabilidad import (
    ACCOUNTS, PARAM_ACCOUNT, PARAM_JOURNAL, caja_account)
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests import receipt_images
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin, _uuid


def photo_url(png):
    return "data:image/png;base64," + base64.b64encode(png).decode()


class CajaMixin(RepartoMixin):

    @classmethod
    def _setup_caja(cls, company):
        cls.cashier = new_test_user(
            cls.env, login="cajero_test", name="Cajero Test",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.manager = new_test_user(
            cls.env, login="gerente_test", name="Gerente Test",
            groups="base.group_user,distri_pedido.grupo_gerencia",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.seller = new_test_user(
            cls.env, login="vendedor_caja", name="Vendedor Caja",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.bills = {d.value: d for d in cls.env["distri.caja.denominacion"].search([])}

    @classmethod
    def _like_production(cls, journals):
        """As in production: the cash journals' method lines have no account
        of their own, so the payments wait in «Recibos pendientes»."""
        for journal in journals:
            (journal.inbound_payment_method_line_ids
             | journal.outbound_payment_method_line_ids).write({"payment_account_id": False})

    def _caja(self, user=None):
        return self.env["distri.caja"].with_user(user or self.cashier)

    def _count(self, **bills):
        """_count(b20000=2, b100=3) -> {denomination id: quantity}"""
        return {str(self.bills[int(k[1:])].id): str(v) for k, v in bills.items()}


@tagged("post_install", "-at_install", "distri_caja")
class TestCaja(CajaMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        cls.partner = cls.partner_a
        # the accounts of the Argentine chart the closing posts to
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        cls.ticket = receipt_images.fuel_ticket(20000)

    # ------------------------------------------------------------ helpers

    def _collect(self, amount, user=None):
        """Cash collected at the depot by `user` (default the cashier)."""
        return self.env["distri.cash.collection"].with_user(user or self.cashier).cash_register(
            self.partner.id, str(amount), confirmed=True, route_ok=True, over_ok=True)

    def _closed_route(self, cash=100000, expenses=()):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        stop = route.stop_ids
        res = self._sync([self._event(route, "visit", self._visit(stop, cash=cash))])
        self.assertEqual(res[0]["status"], "ok", res)
        for amount in expenses:
            payload = {"expense_uuid": _uuid(), "kind": "fuel", "amount": amount, "comment": "YPF",
                       "photo": photo_url(self.ticket)}
            res = self._sync([self._event(route, "expense", payload)])
            self.assertEqual(res[0]["status"], "ok", res)
        route.sudo().state = "closed"
        return route

    def _open_lines(self, payments):
        from odoo.addons.distri_pedido.models.caja_contabilidad import open_outstanding_lines
        return open_outstanding_lines(payments.sudo())

    # ------------------------------------------------------------ lifecycle

    def test_lifecycle_float_and_one_open(self):
        Caja = self._caja()
        caja = Caja.open_session("10.000")
        self.assertEqual(caja.state, "open")
        self.assertEqual(caja.opening_float, 10000)
        self.assertEqual(caja.journal_id, self.cash_journal)
        self.assertEqual(caja.expected, 10000)
        with self.assertRaisesRegex(UserError, "Tenés abierta la caja"):
            Caja.open_session("0")
        caja.with_user(self.cashier).close_session(self._count(b10000=1), left="5000")
        self.assertEqual(caja.state, "closed")
        self.assertEqual(caja.withdrawn, 5000)
        # same day again: closed, only gerencia reopens it
        with self.assertRaisesRegex(UserError, "ya se cerró"):
            Caja.open_session("5000")
        # the next day: the float is proposed from «queda para mañana»
        caja.sudo().date = fields.Date.subtract(today_ar(), days=1)
        with self.assertRaises(CajaQuestion):
            Caja.open_session("7000")  # different without a reason
        tomorrow = Caja.open_session("5000")
        self.assertEqual(tomorrow.proposed_float, 5000)
        self.assertEqual(tomorrow.previous_id, caja)

    def test_previous_open_blocks_today(self):
        caja = self._caja().open_session("0")
        caja.sudo().date = fields.Date.subtract(today_ar(), days=1)
        with self.assertRaisesRegex(UserError, "cerrala antes"):
            self._caja().open_session("0")
        self.assertEqual(self._caja().current_for(self.cashier), caja)

    def test_expected_breakdown(self):
        caja = self._caja().open_session("1000")
        self._collect(5000)
        caja._sync_own_collections()
        caja._sync_own_collections()  # once
        self.assertEqual(len(caja.movement_ids), 1)
        caja.with_user(self.cashier).add_outflow("nafta", "1.500", who="YPF")
        caja.with_user(self.cashier).add_outflow("retiro", "500")
        self.assertEqual(caja.total_cobros, 5000)
        self.assertEqual(caja.total_salidas, -1500)
        self.assertEqual(caja.total_retiros, -500)
        self.assertEqual(caja.expected, 1000 + 5000 - 1500 - 500)
        with self.assertRaisesRegex(UserError, "No alcanza"):
            caja.with_user(self.cashier).add_outflow("banco", "999999")

    # ------------------------------------------------------------ accounting

    def test_closing_reconciles_payments_and_posts_outflows(self):
        caja = self._caja().open_session("0")
        collections = self._collect(3000) | self._collect(2000)
        payments = collections.payment_id
        self.assertEqual(set(payments.mapped("state")), {"in_process"})
        self.assertTrue(self._open_lines(payments))
        supplier = self.partner_b
        caja.with_user(self.cashier).add_outflow("proveedor", "1000", who=supplier.name,
                                                 partner_id=supplier.id)
        caja.with_user(self.cashier).add_outflow("libreria", "200", who="Librería Sur")
        caja.with_user(self.cashier).close_session(self._count(b2000=1, b1000=1, b500=1, b200=1, b100=1),
                                                   left="3800")
        self.assertEqual(caja.expected, 3800)
        self.assertFalse(caja.difference)
        payments.invalidate_recordset()
        self.assertEqual(set(payments.mapped("state")), {"paid"})
        self.assertFalse(self._open_lines(payments), "«Recibos pendientes» emptied for them")
        statement = caja.statement_id
        self.assertEqual(statement.journal_id, self.cash_journal)
        self.assertEqual(statement.balance_start, 0)
        self.assertEqual(statement.balance_end_real, 3800)
        self.assertAlmostEqual(statement.balance_end, 3800)
        cash = self.cash_journal.default_account_id
        lines = statement.line_ids.move_id.line_ids
        self.assertAlmostEqual(sum(lines.filtered(lambda l: l.account_id == cash).mapped("balance")), 3800)
        payable = lines.filtered(lambda l: l.partner_id == supplier and l.debit)
        self.assertEqual(payable.account_id, supplier.property_account_payable_id)
        self.assertEqual(payable.debit, 1000)
        library = caja_account(self.env, self.env.company, "libreria")
        self.assertEqual(lines.filtered(lambda l: l.account_id == library).debit, 200)

    def test_difference_posted_and_case_opened(self):
        caja = self._caja().open_session("0")
        self._collect(10000)
        with self.assertRaises(CajaQuestion):
            caja.with_user(self.cashier).close_session(self._count(b2000=4, b1000=1), left="0")
        self.assertEqual(caja.state, "open")
        caja.with_user(self.cashier).close_session(self._count(b2000=4, b1000=1), left="0",
                                                   reason="Vuelto mal dado")
        self.assertEqual(caja.counted, 9000)
        self.assertEqual(caja.difference, -1000)
        diff_account = caja_account(self.env, self.env.company, "diferencias")
        line = caja.statement_id.line_ids.move_id.line_ids.filtered(
            lambda l: l.account_id == diff_account)
        self.assertEqual(line.debit, 1000)
        case = caja.case_id
        self.assertEqual(case.kind, "cash")
        self.assertEqual(case.caja_id, caja)
        self.assertFalse(case.route_id)
        self.assertEqual(case.driver_id, self.cashier)
        self.assertEqual(case.amount, -1000)
        self.assertEqual(case.state, "open")

    def test_difference_account_is_a_parameter(self):
        account = self.env["account.account"].create({
            "name": "Faltantes", "code": "999777", "account_type": "expense"})
        self.env["ir.config_parameter"].sudo().set_param(PARAM_ACCOUNT % "diferencias", "999777")
        self.assertEqual(caja_account(self.env, self.env.company, "diferencias"), account)

    def test_reopen_rules(self):
        caja = self._caja().open_session("0")
        payment = self._collect(4000).payment_id
        caja.with_user(self.cashier).close_session(self._count(b2000=1), left="0", reason="faltan")
        self.assertEqual(payment.state, "paid")
        case = caja.case_id
        # 19.0.1.105.0: his own cashier reopens it too (test_v105_reabrir_caja);
        # somebody without the group never
        with self.assertRaises(AccessError):
            caja.with_user(self.seller).action_reopen("me equivoqué")
        with self.assertRaisesRegex(UserError, "motivo"):
            caja.with_user(self.manager).action_reopen("  ")
        caja.with_user(self.manager).action_reopen("Contó mal los de 2000")
        self.assertEqual(caja.state, "open")
        self.assertFalse(caja.statement_id)
        self.assertEqual(case.state, "closed")
        payment.invalidate_recordset()
        self.assertEqual(payment.state, "in_process")
        self.assertIn("Contó mal", caja.message_ids[:1].body)
        caja.with_user(self.cashier).close_session(self._count(b2000=2), left="0")
        self.assertEqual(payment.state, "paid")
        self.assertFalse(caja.difference)
        # only the latest session of the cashier is reopened
        caja.sudo().date = fields.Date.subtract(today_ar(), days=1)
        self._caja().open_session("0")
        with self.assertRaisesRegex(UserError, "solo se reabre la última"):
            caja.with_user(self.manager).action_reopen("otra vez")

    # ------------------------------------------------------------ receiving

    def test_receive_settlement_expense_without_paper_asks(self):
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=100000, expenses=(20000, 5000))
        fuel, small = route.expense_ids.sorted("amount", reverse=True)
        self.assertEqual(route.cash_expected, 75000)
        Caja = caja.with_user(self.cashier)
        with self.assertRaises(CajaQuestion) as asked:
            Caja.receive_route(route, "75000", papers=[small.id])
        self.assertEqual(asked.exception.field, "sin_papel_ok")
        self.assertIn("a cargo de %s" % self.driver.name, asked.exception.args[0])
        self.assertEqual(route.state, "closed", "nothing settled before the answer")
        self.assertEqual(fuel.state, "reported")
        self.assertFalse(caja.movement_ids)
        # a difference must be answered too
        with self.assertRaises(CajaQuestion) as asked:
            Caja.receive_route(route, "74000", papers=[small.id], no_paper_ok=True)
        self.assertEqual(asked.exception.field, "diferencia")
        movement = Caja.receive_route(route, "74000", papers=[small.id], no_paper_ok=True,
                                      difference_mode="manana")
        self.assertEqual(route.state, "settled")
        self.assertEqual(fuel.state, "rejected")
        self.assertEqual(fuel.case_id.kind, "expense")
        self.assertEqual(small.state, "approved")
        self.assertFalse(small.move_id, "posted at the closing, in the cash statement")
        self.assertEqual(movement.kind, "rendicion")
        self.assertEqual(movement.amount, 74000)
        self.assertEqual(movement.route_difference, -1000)
        self.assertIn("Lo trae mañana", route._cash_case().driver_explanation)
        self.assertEqual(caja.expected, 74000)
        with self.assertRaisesRegex(UserError, "no la cerró|no se puede"):
            Caja.receive_route(route, "1")
        # the shortage comes back the next day
        self.assertIn(movement, self.env["distri.caja"].pending_shortages())
        Caja.bring_shortage(movement)
        self.assertEqual(caja.expected, 75000)
        self.assertTrue(movement.case_id.replenished)
        payments = route.payment_ids
        self.assertEqual(sum(payments.mapped("amount")), 100000)
        caja.with_user(self.cashier).close_session(self._count(b10000=7, b1000=5), left="0")
        self.assertFalse(caja.difference)
        payments.invalidate_recordset()
        self.assertEqual(set(payments.mapped("state")), {"paid"})
        self.assertTrue(small.move_id)
        lines = caja.statement_id.line_ids.move_id.line_ids
        self.assertEqual(lines.filtered(
            lambda l: l.account_id == small._expense_account() and l.debit).debit, 5000)
        diff_account = caja_account(self.env, self.env.company, "diferencias")
        # rejected expense 20000 + shortage 1000 - brought back 1000
        diff = lines.filtered(lambda l: l.account_id == diff_account)
        self.assertAlmostEqual(sum(diff.mapped("balance")), 20000)
        self.assertAlmostEqual(caja.statement_id.balance_end, 0)

    def test_receive_other_users_box(self):
        caja = self._caja().open_session("0")
        collection = self._collect(8000, user=self.env.user)
        box = collection.route_id
        routes, boxes = self.env["distri.caja"].with_user(self.cashier).pending_receptions()
        self.assertIn(box, boxes)
        own = self._collect(100).route_id
        routes, boxes = self.env["distri.caja"].with_user(self.cashier).pending_receptions()
        self.assertNotIn(own, boxes, "his own box enters as «cobros en el local»")
        movement = caja.with_user(self.cashier).receive_route(box, "8000")
        self.assertEqual(movement.kind, "recepcion")
        self.assertEqual(box.state, "settled")
        caja.with_user(self.cashier).close_session(self._count(b2000=4, b100=1), left="0")
        self.assertEqual(collection.payment_id.state, "paid")
        self.assertEqual(own.state, "settled", "the cashier's own box closes with the session")

    # ------------------------------------------------------------ catch-up

    def test_catch_up_is_idempotent(self):
        Payment = self.env["account.payment"]

        def cash_payment(journal, amount):
            payment = Payment.create({
                "payment_type": "inbound", "partner_type": "customer",
                "partner_id": self.partner.id, "amount": amount, "journal_id": journal.id})
            payment.action_post()
            return payment

        # backlog: a box closed in «Cierre de caja depósito», a payment of each journal
        closed = self._collect(1000, user=self.env.user)
        box = closed.route_id
        box.cash_counted = 1000
        box.action_close_depot()
        backlog = closed.payment_id | cash_payment(self.cash_journal, 700) \
            | cash_payment(self.depot_journal, 2500)
        self.assertEqual(set(backlog.mapped("state")), {"in_process"})
        # left to the daily cash: an open box of someone else, and the session's own cobro
        open_box = self._collect(50, user=self.driver)
        caja = self._caja().open_session("0")
        in_session = self._collect(300)
        with self.assertRaises(AccessError):
            self.env["distri.caja.conciliacion.inicial"].with_user(self.cashier).create({})
        Wizard = self.env["distri.caja.conciliacion.inicial"].with_user(self.manager)
        wizard = Wizard.create({})
        self.assertEqual(wizard.payment_count, 3)
        self.assertEqual(wizard.total, 4200)
        before = self.env["account.bank.statement"].search([]).ids
        wizard.action_apply()
        self.assertTrue(wizard.done)
        backlog.invalidate_recordset()
        self.assertEqual(set(backlog.mapped("state")), {"paid"})
        self.assertFalse(self._open_lines(backlog))
        self.assertEqual(open_box.payment_id.state, "in_process")
        self.assertEqual(in_session.payment_id.state, "in_process")
        new = self.env["account.bank.statement"].search([("id", "not in", before)])
        self.assertEqual(new.journal_id, self.cash_journal | self.depot_journal,
                         "one statement per journal")
        self.assertEqual([len(s.line_ids) for s in new], [1, 1], "one line each")
        self.assertEqual(sorted(new.line_ids.mapped("amount")), [700, 3500])
        again = Wizard.create({})
        self.assertEqual(again.payment_count, 0)
        again.action_apply()
        self.assertFalse(self.env["account.bank.statement"].search(
            [("id", "not in", before + new.ids)]), "running it again does nothing")
        caja.with_user(self.cashier).close_session(self._count(b200=1, b100=1), left="0")
        self.assertEqual(in_session.payment_id.state, "paid")

    # ------------------------------------------------------------ permissions

    def test_non_cashier_refused(self):
        with self.assertRaises(AccessError):
            self._caja(self.seller).open_session("0")
        caja = self._caja().open_session("0")
        with self.assertRaises(AccessError):
            caja.with_user(self.seller).add_outflow("nafta", "100", who="x")
        other = new_test_user(self.env, login="cajero_otro", name="Otro Cajero",
                              groups="base.group_user,distri_pedido.grupo_cajero",
                              company_id=self.env.company.id,
                              company_ids=[Command.set(self.env.company.ids)])
        with self.assertRaisesRegex(AccessError, "otro cajero"):
            caja.with_user(other).add_outflow("nafta", "100", who="x")
        # the backend: a cashier reads his own sessions only
        self.assertFalse(self.env["distri.caja"].with_user(other).search([("id", "=", caja.id)]))
        self.assertTrue(self.env["distri.caja"].with_user(self.cashier).search([("id", "=", caja.id)]))

    # ------------------------------------------------------------ label

    def test_closing_label(self):
        caja = self._caja().open_session("0")
        self._collect(5450)
        caja.with_user(self.cashier).close_session(
            self._count(b2000=2, b1000=1, b200=2), other="50", left="0")
        values = caja._label_values()
        self.assertEqual(values["cashier"], "Cajero Test")
        self.assertEqual([(r["value"], r["quantity"], r["subtotal"]) for r in values["rows"]],
                         [("2.000", 2, "4.000"), ("1.000", 1, "1.000"), ("200", 2, "400")])
        self.assertEqual(values["other"], "50")
        self.assertEqual(values["total"], "$ 5.450")
        html = self.env["ir.actions.report"]._render_qweb_html(
            "distri_pedido.report_caja_cierre", caja.ids)[0].decode()
        text = " ".join(re.sub(r"<[^>]+>", " ", html.split("<body", 1)[1]).split())
        for piece in (caja.date.strftime("%d/%m/%Y"), "Cajero Test", "2.000", "4.000",
                      "Monedas / otros", "TOTAL CONTADO", "$ 5.450"):
            self.assertIn(piece, text)
        for absent in ("Tiene que haber", "Diferencia", "Queda", "500 ×"):
            self.assertNotIn(absent, text)
        self.assertEqual(html.count('class="cc"'), 1, "one single label")

    def test_closing_label_worst_case_two_columns(self):
        caja = self._caja().open_session("0")
        counts = {"b%s" % v: 999 for v in self.bills}
        total = sum(v * 999 for v in self.bills) + 99999.5
        self.env["account.payment"]  # no cobro: everything is a difference
        caja.with_user(self.cashier).close_session(self._count(**counts), other="99.999,50",
                                                   left="0", reason="prueba del tope")
        self.assertEqual(caja.counted, total)
        html = self.env["ir.actions.report"]._render_qweb_html(
            "distri_pedido.report_caja_cierre", caja.ids)[0].decode()
        self.assertEqual(html.count("col2a"), 2)  # style + the column
        self.assertIn("Monedas/otros", html)
        self.assertEqual(len(re.findall(r'<td class="q"', html)), 10)


@tagged("post_install", "-at_install", "distri_caja")
class TestCajaHttp(CajaMixin, HttpCase):
    """The /caja pages with a real session on the copy's company."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def test_pages_and_permissions(self):
        self.authenticate("vendedor_caja", "vendedor_caja")
        self.assertEqual(self.url_open("/caja").status_code, 403)
        self.assertEqual(self.url_open("/caja/cierre").status_code, 403)
        self.authenticate("cajero_test", "cajero_test")
        home = self.url_open("/caja")
        self.assertIn("Abrir la caja", home.text)
        self.assertIn("/web/session/logout", home.text)
        token = self._csrf("/caja")
        res = self.url_open("/caja/abrir", data={"csrf_token": token, "fondo": "1000"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        caja = self.env["distri.caja"].current_for(self.cashier)
        self.assertTrue(caja)
        home = self.url_open("/caja")
        self.assertIn("Tiene que haber", home.text)
        for path in ("/caja/rendicion", "/caja/salida", "/caja/cierre"):
            self.assertEqual(self.url_open(path).status_code, 200, path)
        # an outflow with fetch: JSON up to /caja
        token = self._csrf("/caja/salida")
        res = self.url_open("/caja/salida", data={
            "csrf_token": token, "tipo": "nafta", "importe": "300", "quien": "YPF"},
            headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.json()["ok"], True, res.text)
        self.assertTrue(res.json()["url"].startswith("/caja?ok="))
        self.assertEqual(caja.expected, 700)
        # closing with a difference and no reason: refused, nothing closed
        token = self._csrf("/caja/cierre")
        data = {"csrf_token": token, "cant_%s" % self.bills[500].id: "1", "queda": "0"}
        res = self.url_open("/caja/cierre", data=data, headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("motivo", res.json()["error"])
        self.assertEqual(caja.state, "open")
        data["motivo"] = "faltan 200"
        res = self.url_open("/caja/cierre", data=data, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(caja.state, "closed")
        pdf = self.url_open("/caja/%s/etiqueta" % caja.id)
        self.assertEqual(pdf.headers["Content-Type"], "application/pdf")
        self.authenticate("vendedor_caja", "vendedor_caja")
        self.assertEqual(self.url_open("/caja/%s/etiqueta" % caja.id).status_code, 403)

    def test_tablero_tile_for_cashier(self):
        from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
        urls = [t["url"] for t in staff_tiles(self.env(user=self.cashier))]
        self.assertIn("/caja", urls)
        urls = [t["url"] for t in staff_tiles(self.env(user=self.seller))]
        self.assertNotIn("/caja", urls)
