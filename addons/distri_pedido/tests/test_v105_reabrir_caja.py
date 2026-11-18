"""19.0.1.105.0: «Reabrir mi caja» (models/caja.py action_reopen,
distri.caja.cierre, controllers/caja.py /caja/cerradas, /caja/historial).

The cashier reopens his own latest closed session (no gerente), loads a
last-minute payment and closes again: the second closing REPLACES the first
(new statement, new case, new label) and nothing is counted twice; the
replaced closing stays in the history."""
import base64
import re

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.caja import CajaQuestion
from odoo.addons.distri_pedido.models.caja_contabilidad import (
    ACCOUNTS, PARAM_JOURNAL, caja_account, open_outstanding_lines)
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests import receipt_images
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin, photo_url
from odoo.addons.distri_pedido.tests.test_reparto import _uuid


@tagged("post_install", "-at_install", "distri_caja")
class TestReabrirCaja(CajaMixin, AccountTestInvoicingCommon):

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
        cls.other_cashier = new_test_user(
            cls.env, login="cajero_otro_v105", name="Otra Cajera",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.cash_journal = cls.company_data["default_journal_cash"]
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_JOURNAL, cls.cash_journal.code)
        cls.depot_journal = cls.env["distri.route"]._depot_journal(company)
        cls._like_production(cls.cash_journal | cls.depot_journal)
        cls.partner = cls.partner_a
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        cls.ticket = receipt_images.fuel_ticket(20000)

    # ------------------------------------------------------------ helpers

    def _collect(self, amount, user=None):
        return self.env["distri.cash.collection"].with_user(user or self.cashier).cash_register(
            self.partner.id, str(amount), confirmed=True, route_ok=True, over_ok=True)

    def _closed_route(self, cash=100000, expenses=()):
        order = self._order(self.partner, self.product_a)
        route = self._route(order)
        res = self._sync([self._event(route, "visit", self._visit(route.stop_ids, cash=cash))])
        self.assertEqual(res[0]["status"], "ok", res)
        for amount in expenses:
            payload = {"expense_uuid": _uuid(), "kind": "fuel", "amount": amount, "comment": "YPF",
                       "photo": photo_url(self.ticket)}
            res = self._sync([self._event(route, "expense", payload)])
            self.assertEqual(res[0]["status"], "ok", res)
        route.sudo().state = "closed"
        return route

    def _reconciled_once(self, payments):
        """Every payment is «paid» and its receipts line is matched by
        exactly one statement line (never twice, never left open)."""
        payments.invalidate_recordset()
        self.assertEqual(set(payments.mapped("state")), {"paid"})
        self.assertFalse(open_outstanding_lines(payments.sudo()))
        for payment in payments.sudo():
            line = payment.move_id.line_ids.filtered(
                lambda l, p=payment: l.account_id == p.outstanding_account_id)
            partials = line.matched_debit_ids | line.matched_credit_ids
            self.assertEqual(len(partials), 1, payment.name)

    def _said(self, caja, text):
        return any(text in (body or "") for body in caja.message_ids.mapped("body"))

    def _statements(self, caja):
        return self.env["account.bank.statement"].sudo().search([("name", "=", caja.name)])

    # ------------------------------------------------------------ the main flow

    def test_close_reopen_pay_close_again(self):
        Caja = self._caja()
        caja = Caja.open_session("1000")
        first = self._collect(4000).payment_id
        caja.with_user(self.cashier).add_outflow(
            "nafta", "500", who="YPF", photo=base64.b64encode(self.ticket))
        route = self._closed_route(cash=100000, expenses=(20000, 5000))
        fuel, small = route.expense_ids.sorted("amount", reverse=True)
        received = caja.with_user(self.cashier).receive_route(
            route, "74000", papers=[small.id], no_paper_ok=True, difference_mode="manana")
        caja.with_user(self.cashier).bring_shortage(received)
        self.assertEqual(caja.expected, 1000 + 4000 - 500 + 74000 + 1000)
        # first closing: 500 short
        caja.with_user(self.cashier).close_session(
            self._count(b10000=7, b2000=4, b1000=1), left="1000", reason="Faltan 500")
        self.assertEqual(caja.difference, -500)
        statement_1 = caja.statement_id
        lines_1 = statement_1.line_ids
        case_1 = caja.case_id
        self.assertEqual(case_1.amount, -500)
        self.assertTrue(small.move_id)
        self.assertEqual(len(lines_1), 8 + len(route.payment_ids))

        # the cashier reopens it himself: «pago de último minuto»
        self.assertTrue(caja.with_user(self.cashier).can_reopen_now())
        caja.with_user(self.cashier).action_reopen("pago de último minuto")
        self.assertEqual(caja.state, "open")
        self.assertFalse(statement_1.exists(), "the closing's statement is undone")
        self.assertFalse(lines_1.exists())
        self.assertEqual(case_1.state, "closed")
        self.assertIn("Cajero Test", case_1.resolution)
        self.assertIn("pago de último minuto", case_1.resolution)
        self.assertFalse(caja.case_id)
        self.assertFalse(small.move_id, "the approved expense waits for the next closing")
        payments = first | route.payment_ids
        payments.invalidate_recordset()
        self.assertEqual(set(payments.mapped("state")), {"in_process"})
        self.assertEqual(caja.reopen_count, 1)
        self.assertEqual(caja.reopen_label, "Reabierta 1 vez")
        replaced = caja.closing_ids
        self.assertEqual(len(replaced), 1)
        self.assertEqual((replaced.number, replaced.expected, replaced.counted, replaced.difference),
                         (1, 79500, 79000, -500))
        self.assertEqual(replaced.reason, "Faltan 500")
        self.assertEqual(replaced.left_for_tomorrow, 1000)
        self.assertEqual(replaced.withdrawn, 78000)
        self.assertEqual(replaced.closed_by_id, self.cashier)
        self.assertEqual(replaced.reopened_by_id, self.cashier)
        self.assertEqual(replaced.reopen_reason, "pago de último minuto")
        self.assertEqual(replaced.case_id, case_1)
        self.assertEqual(replaced.statement_name, caja.name)
        self.assertIn("$ 10.000 × 7", replaced.bills)
        self.assertTrue(self._said(caja, "Motivo: pago de último minuto"))

        # the last-minute payment: a new (provisional) customer, after the reopening
        late = self.env["distri.cash.collection"].with_user(self.cashier).cash_register_new(
            "Kiosco Ultimo Minuto Qwz", "2000", new_ok=True, over_ok=True)
        self.assertTrue(late.partner_id.distri_cliente_provisorio)
        caja._sync_own_collections()
        self.assertEqual(caja.expected, 81500)
        self.assertEqual(len(caja.movement_ids.filtered(lambda m: m.kind == "cobro")), 2,
                         "the first payment is not taken again")

        # second closing: exact; it replaces the first
        caja.with_user(self.cashier).close_session(
            self._count(b10000=8, b1000=1, b500=1), left="1500")
        self.assertEqual(caja.state, "closed")
        self.assertEqual(caja.expected, 81500)
        self.assertEqual(caja.counted, 81500)
        self.assertFalse(caja.difference)
        self.assertFalse(caja.case_id, "no difference, no case")
        self.assertEqual(self.env["distri.route.case"].sudo().search(
            [("caja_id", "=", caja.id), ("kind", "=", "cash")]), case_1, "only the closed one")
        self.assertEqual(caja.withdrawn, 80000)
        statement = caja.statement_id
        self.assertEqual(self._statements(caja), statement, "one statement: the first one is gone")
        self.assertEqual(statement.balance_start, 1000)
        self.assertEqual(statement.balance_end_real, 1500)
        self.assertAlmostEqual(statement.balance_end, 1500)
        self.assertEqual(sorted(statement.line_ids.mapped("amount")), sorted(
            [4000, 2000] + route.payment_ids.mapped("amount")
            + [-5000, -20000, -1000, 1000, -500, -80000]))
        self._reconciled_once(first | late.payment_id | route.payment_ids)
        self.assertEqual(small.move_id.statement_line_id.statement_id, statement)
        diff_account = caja_account(self.env, self.env.company, "diferencias")
        diff = statement.line_ids.move_id.line_ids.filtered(lambda l: l.account_id == diff_account)
        # rejected expense 20000 + driver shortage 1000 - brought back 1000; no session difference
        self.assertAlmostEqual(sum(diff.mapped("balance")), 20000)
        cash = self.cash_journal.default_account_id
        cash_lines = statement.line_ids.move_id.line_ids.filtered(lambda l: l.account_id == cash)
        self.assertAlmostEqual(sum(cash_lines.mapped("balance")), 500)
        # the photo of the outflow goes with the new line only
        nafta = caja.movement_ids.filtered(lambda m: m.kind == "gasto")
        self.assertEqual(len(nafta.statement_line_ids), 1)
        self.assertEqual(self.env["ir.attachment"].sudo().search_count([
            ("res_model", "=", "account.move"),
            ("res_id", "in", nafta.statement_line_ids.move_id.ids)]), 1)
        # the label is the second count
        values = caja._label_values()
        self.assertEqual([(r["value"], r["quantity"]) for r in values["rows"]],
                         [("10.000", 8), ("1.000", 1), ("500", 1)])
        self.assertEqual(values["total"], "$ 81.500")
        self.assertTrue(self._said(caja, "Cierre n.º 2: reemplaza al anterior"))
        self.assertEqual(len(caja.closing_ids), 1, "the history keeps the replaced one only")
        # the provisional customer is still waiting for administración
        self.assertIn(late.partner_id, self.env["res.partner"]._distri_provisorios_pendientes())
        # tomorrow's float comes from the latest closing
        caja.sudo().date = fields.Date.subtract(today_ar(), days=1)
        with self.assertRaises(CajaQuestion):
            Caja.open_session("1000")
        tomorrow = Caja.open_session("1500")
        self.assertEqual(tomorrow.proposed_float, 1500)
        self.assertEqual(tomorrow.previous_id, caja)

    def test_reopen_twice(self):
        caja = self._caja().open_session("0")
        payment = self._collect(3000).payment_id
        caja.with_user(self.cashier).close_session(self._count(b2000=1), left="0",
                                                   reason="faltan mil")
        case_a = caja.case_id
        self.assertEqual(case_a.amount, -1000)
        caja.with_user(self.cashier).action_reopen("conté mal")
        caja.with_user(self.cashier).close_session(self._count(b2000=1, b500=1), left="0",
                                                   reason="faltan 500")
        case_b = caja.case_id
        self.assertNotEqual(case_a, case_b, "the second closing opens its own case")
        self.assertEqual(case_b.amount, -500)
        self.assertEqual(case_b.state, "open")
        # gerencia reopens it the second time
        caja.with_user(self.manager).action_reopen("apareció el billete de 500")
        self.assertEqual(case_b.state, "closed")
        self.assertIn("Gerente Test", case_b.resolution)
        caja.with_user(self.cashier).close_session(self._count(b2000=1, b1000=1), left="0")
        self.assertFalse(caja.difference)
        self.assertFalse(caja.case_id)
        self.assertEqual(caja.reopen_count, 2)
        self.assertEqual(caja.reopen_label, "Reabierta 2 veces")
        history = caja.closing_ids
        self.assertEqual(history.mapped("number"), [1, 2])
        self.assertEqual(history.mapped("counted"), [2000, 2500])
        self.assertEqual(history.mapped("difference"), [-1000, -500])
        self.assertEqual(history.mapped("reopened_by_id"), self.cashier | self.manager)
        self.assertEqual(history.mapped("case_id"), case_a | case_b)
        self.assertEqual(set((case_a | case_b).mapped("state")), {"closed"})
        self.assertEqual(self._statements(caja), caja.statement_id)
        self.assertEqual(sorted(caja.statement_id.line_ids.mapped("amount")), [-3000, 3000])
        self._reconciled_once(payment)
        self.assertTrue(self._said(caja, "Cierre n.º 3"))
        self.assertEqual(caja._label_values()["total"], "$ 3.000")

    def test_reopen_guard_and_permissions(self):
        caja = self._caja().open_session("0")
        self._collect(1000)
        with self.assertRaisesRegex(UserError, "La caja está abierta"):
            caja.with_user(self.cashier).action_reopen("todavía abierta")
        caja.with_user(self.cashier).close_session(self._count(b1000=1), left="0")
        for user in (self.other_cashier, self.seller):
            with self.assertRaises(AccessError):
                caja.with_user(user).action_reopen("no es mía")
            self.assertFalse(caja.with_user(user).can_reopen_now())
        with self.assertRaises(CajaQuestion):
            caja.with_user(self.cashier).action_reopen("   ")
        # a later session blocks it, for him and for gerencia
        caja.sudo().date = fields.Date.subtract(today_ar(), days=1)
        later = self._caja().open_session("0")
        day = later.date.strftime("%d/%m")
        with self.assertRaisesRegex(UserError, "Ya abriste la caja del %s: solo se reabre la última"
                                    % day):
            caja.with_user(self.cashier).action_reopen("pago de último minuto")
        with self.assertRaisesRegex(UserError, "Cajero Test ya abrió la caja del %s" % day):
            caja.with_user(self.manager).action_reopen("pago de último minuto")
        self.assertFalse(caja.with_user(self.cashier).can_reopen_now())
        self.assertEqual(caja.state, "closed")
        self.assertFalse(caja.closing_ids)
        # closed today too: the older one stays blocked, the latest one can
        later.with_user(self.cashier).close_session(self._count(), left="0")
        self.assertTrue(later.with_user(self.cashier).can_reopen_now())
        self.assertFalse(caja.with_user(self.cashier).can_reopen_now())
        # the history follows the session's rules
        later.with_user(self.cashier).action_reopen("probar")
        Cierre = self.env["distri.caja.cierre"]
        self.assertEqual(Cierre.with_user(self.cashier).search([("caja_id", "=", later.id)]),
                         later.closing_ids)
        self.assertFalse(Cierre.with_user(self.other_cashier).search([("caja_id", "=", later.id)]))
        self.assertTrue(Cierre.with_user(self.manager).search([("caja_id", "=", later.id)]))
        with self.assertRaises(AccessError):
            Cierre.with_user(self.cashier).browse(later.closing_ids.id).write({"counted": 1})


@tagged("post_install", "-at_install", "distri_caja")
class TestReabrirCajaHttp(CajaMixin, HttpCase):
    """The pages: «Cajas cerradas», one session, «Reabrir mi caja» and
    «Historial de cajas», with who may see what."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        company = cls.env.company
        cls._setup_caja(company)
        cls.other_cashier = new_test_user(
            cls.env, login="cajera_v105", name="Otra Cajera",
            groups="base.group_user,distri_pedido.grupo_cajero",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.admin_only = new_test_user(
            cls.env, login="administracion_v105", name="Solo Administracion",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=company.id, company_ids=[Command.set(company.ids)])

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)

    def _closed_caja(self):
        caja = self._caja().open_session("0")
        caja.with_user(self.cashier).close_session(self._count(b500=1), left="0",
                                                   reason="sobran 500")
        return caja

    def test_cashier_reopens_from_the_page(self):
        caja = self._closed_caja()
        self.authenticate("cajero_test", "cajero_test")
        home = self.url_open("/caja?cerrada=%s" % caja.id)
        self.assertIn("/caja/cerradas/%s#reabrir" % caja.id, home.text)
        self.assertIn('href="/caja/cerradas"', home.text)
        page = self.url_open("/caja/cerradas")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Cajas cerradas", page.text)
        self.assertIn("Reabrir mi caja", page.text)
        self.assertIn("/caja/cerradas/%s/reabrir" % caja.id, page.text)
        detail = self.url_open("/caja/cerradas/%s" % caja.id)
        self.assertEqual(detail.status_code, 200)
        for piece in ("Reimprimir etiqueta", "Conteo de billetes", "sobran 500", "Reabrir mi caja",
                      "/caja/%s/etiqueta" % caja.id):
            self.assertIn(piece, detail.text)
        self.assertEqual(self.url_open("/caja/%s/etiqueta" % caja.id).headers["Content-Type"],
                         "application/pdf")
        # no reason: refused, still closed
        token = self._csrf("/caja/cerradas/%s" % caja.id)
        url = "/caja/cerradas/%s/reabrir" % caja.id
        res = self.url_open(url, data={"csrf_token": token, "motivo": " "},
                            headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("motivo", res.json()["error"])
        self.assertEqual(caja.state, "closed")
        res = self.url_open(url, data={"csrf_token": token, "motivo": "pago de último minuto"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].split("?")[0].endswith("/caja"))
        self.assertEqual(caja.state, "open")
        home = self.url_open("/caja")
        self.assertIn("Caja reabierta", home.text)
        self.assertIn("pago de último minuto", home.text)
        self.assertIn("reemplaza al cierre n.º 1", self.url_open("/caja/cierre").text)
        caja.with_user(self.cashier).close_session(self._count(b500=1), left="0",
                                                   reason="sobran 500")
        detail = self.url_open("/caja/cerradas/%s" % caja.id)
        for piece in ("Cierres reemplazados", "Cierre n.º 1", "Cierre n.º 2", "Reabierta 1 vez",
                      "Motivo: pago de último minuto"):
            self.assertIn(piece, detail.text)
        # a later session: the button goes, the reason why shows
        caja.sudo().date = fields.Date.subtract(today_ar(), days=1)
        self._caja().open_session("0")
        page = self.url_open("/caja/cerradas")
        self.assertNotIn("/reabrir", page.text)
        self.assertIn("Ya abriste la caja del %s" % today_ar().strftime("%d/%m"), page.text)

    def test_who_sees_what(self):
        caja = self._closed_caja()
        detail = "/caja/cerradas/%s" % caja.id
        # another cashier: it does not exist for her
        self.authenticate("cajera_v105", "cajera_v105")
        for path in (detail, "/caja/%s/etiqueta" % caja.id):
            self.assertEqual(self.url_open(path).status_code, 404, path)
        page = self.url_open("/caja/cerradas")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn(detail, page.text)
        self.assertEqual(self.url_open("/caja/historial").status_code, 403)
        token = self._csrf("/caja")
        res = self.url_open(detail + "/reabrir", data={"csrf_token": token, "motivo": "x"})
        self.assertEqual(res.status_code, 404)
        self.assertEqual(caja.state, "closed")
        # administración without «Cajero» nor gerencia: nothing on /caja
        self.authenticate("administracion_v105", "administracion_v105")
        for path in ("/caja/cerradas", detail, "/caja/historial"):
            self.assertEqual(self.url_open(path).status_code, 403, path)
        # a seller neither
        self.authenticate("vendedor_caja", "vendedor_caja")
        self.assertEqual(self.url_open("/caja/cerradas").status_code, 403)
        # gerencia: every cashier, with the cashier filter
        self.authenticate("gerente_test", "gerente_test")
        self.assertEqual(self.url_open("/caja", allow_redirects=False).status_code, 303)
        page = self.url_open("/caja/historial")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Historial de cajas", page.text)
        self.assertIn(detail, page.text)
        self.assertIn("Cajero Test", page.text)
        page = self.url_open("/caja/historial?cajero=%s" % self.other_cashier.id)
        self.assertEqual(page.status_code, 200)
        page = self.url_open("/caja/historial?cajero=%s&ver=dif" % self.cashier.id)
        self.assertIn(detail, page.text)
        page = self.url_open("/caja/historial?ver=reabiertas&cajero=%s" % self.cashier.id)
        self.assertNotIn(detail, page.text)
        res = self.url_open(detail)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Reabrir la caja de Cajero Test", res.text)
        self.assertEqual(self.url_open("/caja/%s/etiqueta" % caja.id).status_code, 200)
        token = self._csrf(detail)
        res = self.url_open(detail + "/reabrir", data={"csrf_token": token,
                                                       "motivo": "lo pidió el cajero"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(caja.state, "open")
        self.assertEqual(caja.closing_ids.reopened_by_id, self.manager)
        page = self.url_open("/caja/historial?ver=reabiertas")
        self.assertIn(detail, page.text)
        self.assertIn("lo pidió el cajero", page.text)

    def test_tablero_tile_for_gerencia(self):
        from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
        self.assertIn("/caja/historial", [t["url"] for t in staff_tiles(self.env(user=self.manager))])
        self.assertNotIn("/caja/historial",
                         [t["url"] for t in staff_tiles(self.env(user=self.cashier))])
