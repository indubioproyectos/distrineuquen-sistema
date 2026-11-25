"""19.0.1.129.44: «Rendiciones» and «Casos» on the design system
(controllers/rendiciones_v2.py, views/rendiciones_v2.xml), behind ui_v2.

- the hub /rendiciones (routes to settle and settled, per driver) and the
  route's screen /rendiciones/ruta/<id>: its numbers are the route's own
  fields (the backend form's), and reading them changes nothing;
- every action from the kit has the very effect of the backend path, ledger
  and caja included: caja reception (the link to /caja/rendicion/<id>),
  «Reabrir» (now asked first, refused to drivers), «Abrir caso por la
  diferencia», saving and closing a case, «Marcar llegó», «Anular» and
  «Reclamar por WhatsApp» of a pending receipt;
- /casos (filters, detail, history), /casos/cargos and
  /rendiciones/comprobantes; permissions per role; CSRF; flag off = 129.43;
- the confirm dialog in Node (tests/data/rendiciones_v129_44.js).
Synthetic data only.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers import rendiciones_v2 as rv2
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, screen_parents
from odoo.addons.distri_pedido.controllers.rendiciones import ROUTE_URL, rows
from odoo.addons.distri_pedido.models.caja_contabilidad import ACCOUNTS, PARAM_JOURNAL
from odoo.addons.distri_pedido.models.gasto import EXPENSE_ACCOUNTS
from odoo.addons.distri_pedido.models.reparto import ROUTE_TO_CASHIER, _money
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
META_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
WEB_RE = re.compile(r'csrf_token: "([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_44")
class TestV12944Rendiciones(CajaMixin, AccountTestInvoicingHttpCommon):

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
        Account = cls.env["account.account"]
        for code, kind in [(c, t[0]) for c, t, _l in ACCOUNTS.values() if c] + [
                (c, "expense") for c in EXPENSE_ACCOUNTS.values()]:
            if not Account.search([("code", "=", code)], limit=1):
                Account.create({"name": "Caja %s" % code, "code": code, "account_type": kind,
                                "reconcile": kind == "liability_payable"})
        kw = {"company_id": company.id, "company_ids": [Command.set(company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **kw)

        cls.admin = user("admin_v144", "distri_pedido.grupo_administracion")
        cls.caja_admin = user("cajadmin_v144", "distri_pedido.grupo_administracion,"
                                               "distri_pedido.grupo_cajero")
        cls.sistemas = user("sistemas_v144", "base.group_system")
        cls.product = cls.product_a
        cls.product.invoice_policy = "order"
        cls.partner = cls.env["res.partner"].create({
            "name": "Almacén Rendir V12944", "company_type": "company", "phone": "2994123456"})
        cls.other = cls.env["res.partner"].create({"name": "Kiosco Rendir V12944",
                                                   "company_type": "company"})

    # ------------------------------------------------------------ helpers

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def setUp(self):
        super().setUp()
        self._flag(True)

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _delta(self, before, after):
        return tuple(round(a - b, 2) for a, b in zip(after, before))

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        found = CSRF_RE.search(page.text) or META_RE.search(page.text) or WEB_RE.search(page.text)
        return found.group(1)

    def _closed_route(self, cash=600, transfer=0, driver=None, partner=None):
        """A route of one invoiced order: the visit (cash and/or a transfer
        whose receipt comes later) and the closing, as the phone sends them."""
        order = self._order(partner or self.partner, self.product, qty=10, price=100)
        order._create_invoices().action_post()
        route = self._route(order, driver=driver)
        payload = self._visit(route.stop_ids, cash=cash)
        if transfer:
            payload.update(transfer="yes", transfer_amount=transfer, receipt_answer="not_yet")
        driver = driver or self.driver
        res = self._sync([self._event(route, "visit", payload)], user=driver)
        self.assertEqual(res[0]["status"], "ok", res)
        res = self._sync([self._event(route, "close", {}, captured="2026-11-25T18:00:00.000Z")],
                         user=driver)
        self.assertEqual(res[0]["status"], "ok", res)
        self.assertEqual(route.state, "closed")
        return route

    def _snapshot(self, routes):
        """What a read-only page must leave as it found: the ledger and the
        last write of the routes, their cases and pending receipts."""
        self.env.flush_all()
        return self._ledger(), sorted((r._name, r.id, str(r.write_date)) for r in
                                      [x for rs in (routes, routes.case_ids, routes.promise_ids)
                                       for x in rs])

    # ------------------------------------------------------------ flag off

    def test_flag_off_is_129_43(self):
        route = self._closed_route()
        self._flag(False)
        before = self._ledger()
        self._login(self.admin)
        page = self.url_open("/rendiciones")
        self.assertEqual(page.status_code, 200)
        self.assertIn('data-page="rendiciones"', page.text)
        self.assertNotIn("data-v2", page.text)
        self.assertNotIn("rendiciones_v2.css", page.text)
        self.assertIn('href="/rendiciones/rutas"', page.text)
        keys = {r["key"]: r for r in rows(self.env(user=self.admin))}
        for key in ("casos", "cargos", "comprobantes"):
            self.assertTrue(keys[key]["url"].startswith("/odoo"), keys[key])
            self.assertEqual(keys[key]["how"], "")
        listing = self.url_open("/rendiciones/rutas").text
        self.assertIn(ROUTE_URL % route.id, listing, "the Odoo form, as before")
        res = self.url_open("/rendiciones/ruta/%s" % route.id, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/rendiciones"))
        for url in ("/casos", "/casos/cargos", "/rendiciones/comprobantes"):
            res = self.url_open(url, allow_redirects=False)
            self.assertEqual(res.status_code, 303, url)
            self.assertIn("/odoo", res.headers["Location"], url)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ hub

    def test_hub_lists_and_totals_read_only(self):
        a = self._closed_route(cash=600)
        b = self._closed_route(cash=250, driver=self.other_driver, partner=self.other)
        done = self._closed_route(cash=100)
        done.sudo().write({"state": "settled", "settled_at": fields.Datetime.now()})
        snap = self._snapshot(a | b | done)
        self._login(self.admin)
        page = self.url_open("/rendiciones")
        self.assertEqual(page.status_code, 200)
        html = page.text
        self.assertIn('data-v2="1"', html)
        self.assertIn("rendiciones_v2.css", html)
        for route in (a, b):
            self.assertIn('href="/rendiciones/ruta/%s"' % route.id, html)
            self.assertIn(_money(route.cash_expected), html)
        self.assertNotIn('href="/rendiciones/ruta/%s"' % done.id, html, "settled: other tab")
        data = rv2.hub_data(self.env(user=self.admin))
        pending, _boxes = self.env["distri.caja"].sudo().pending_receptions()
        self.assertEqual(data["cash_to_settle"], _money(sum(pending.mapped("cash_expected"))))
        self.assertEqual(data["n_to_settle"], len(pending))
        self.assertIn(a.id, [r["id"] for r in data["rows"]])
        # the settled tab and the driver filter
        html = self.url_open("/rendiciones?vista=rendidas").text
        self.assertIn('href="/rendiciones/ruta/%s"' % done.id, html)
        self.assertIn("Rendida", html)
        html = self.url_open("/rendiciones?repartidor=%s" % self.other_driver.id).text
        self.assertIn('data-rd-ruta="%s"' % b.id, html)
        self.assertNotIn('data-rd-ruta="%s"' % a.id, html)
        # the PC side panel of the selected route
        html = self.url_open("/rendiciones?ruta=%s" % b.id).text
        self.assertIn('data-rd-panel="%s"' % b.id, html)
        # no cashier: no «Recibir rendiciones»; links to the kit lists
        self.assertNotIn('href="/caja/rendicion"', html)
        for url in ("/casos", "/casos/cargos", "/rendiciones/comprobantes"):
            self.assertIn('href="%s"' % url, html)
        self.assertEqual(self._snapshot(a | b | done), snap, "the hub changes nothing")
        # the old list goes to the hub with the flag on
        res = self.url_open("/rendiciones/rutas", allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/rendiciones"))
        keys = {r["key"]: r for r in rows(self.env(user=self.admin))}
        self.assertEqual(keys["casos"]["url"], "/casos")
        self.assertEqual(keys["cargos"]["url"], "/casos/cargos")
        self.assertEqual(keys["comprobantes"]["url"], "/rendiciones/comprobantes")

    # ------------------------------------------------------------ route screen

    def test_route_screen_numbers_are_the_backend_fields(self):
        route = self._closed_route(cash=600, transfer=400)
        route.sudo().write({"cash_counted": 550})
        snap = self._snapshot(route)
        self.assertEqual(route.cash_difference, -50)
        view = rv2.route_view(self.env(user=self.admin), route.sudo())
        self.assertEqual(view["expected"], _money(route.cash_expected))
        self.assertEqual(view["collected"], _money(route.cash_collected))
        self.assertEqual(view["expense_total"], _money(route.expense_total))
        self.assertEqual(view["counted"], _money(550))
        self.assertEqual(view["difference"], "−" + _money(50))
        self.assertEqual(view["transfer_total"], _money(400))
        self.assertEqual(view["receipts_pending"], 1)
        self.assertEqual(view["visits_cash"], _money(sum(route.stop_ids.mapped("cash_amount"))))
        self.assertEqual(len(view["visits"]), len(route.stop_ids))
        self._login(self.admin)
        html = self.url_open("/rendiciones/ruta/%s" % route.id).text
        self.assertIn('data-parent="/rendiciones"', html)
        for text in (_money(route.cash_expected), _money(550), "−" + _money(50),
                     "Falta efectivo", _money(400), "1 sin comprobante", "Sin comprobante",
                     route.stop_ids.partner_id.name):
            self.assertIn(text, html, text)
        # administración settles nothing here: the cashier receives it
        self.assertNotIn('href="/caja/rendicion/%s"' % route.id, html)
        self.assertIn(ROUTE_TO_CASHIER.split(":")[0], html)
        self.assertIn('href="%s"' % (ROUTE_URL % route.id), html, "Ver en el sistema")
        self.assertIn('/rendiciones/ruta/%s/caso"' % route.id, html)
        # counted: administración may not reopen, the page says why
        self.assertNotIn('/rendiciones/ruta/%s/reabrir"' % route.id, html)
        self.assertIn("solo caja o gerencia", html)
        self._login(self.caja_admin)
        html = self.url_open("/rendiciones/ruta/%s" % route.id).text
        self.assertIn('href="/caja/rendicion/%s"' % route.id, html)
        self.assertIn('/rendiciones/ruta/%s/reabrir"' % route.id, html)
        self.assertIn("data-rd-confirm=", html)
        self.assertEqual(self._snapshot(route), snap, "the screen changes nothing")
        # a depot box or a missing route: 404
        self.assertEqual(self.url_open("/rendiciones/ruta/999999").status_code, 404)

    # ------------------------------------------------------------ actions = backend

    def test_caja_reception_from_the_kit_equals_the_backend_path(self):
        kit, backend = self._closed_route(cash=600), self._closed_route(cash=600)
        caja = self._caja(self.caja_admin).open_session("0")
        self._login(self.caja_admin)
        html = self.url_open("/rendiciones/ruta/%s" % kit.id).text
        link = '/caja/rendicion/%s' % kit.id
        self.assertIn('href="%s"' % link, html)
        token = self._csrf(link)
        before = self._ledger()
        res = self.url_open(link, data={"csrf_token": token, "entrego": "600"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:500])
        middle = self._ledger()
        caja.with_user(self.caja_admin).receive_route(backend, "600")
        after = self._ledger()
        self.assertEqual(self._delta(before, middle), self._delta(middle, after))
        Movement = self.env["distri.caja.movimiento"].sudo()
        for route in (kit, backend):
            self.assertEqual(route.state, "settled")
        moves = [Movement.search([("route_id", "=", r.id)]) for r in (kit, backend)]
        self.assertEqual([(m.kind, m.amount, m.route_difference) for m in moves[0]],
                         [(m.kind, m.amount, m.route_difference) for m in moves[1]])
        self.assertEqual(sorted(kit.payment_ids.mapped("amount")),
                         sorted(backend.payment_ids.mapped("amount")))
        self.assertEqual(kit.cash_counted, backend.cash_counted)
        self.assertEqual(caja.movement_ids.filtered(lambda m: m.route_id == kit).amount, 600)

    def test_reopen_asks_first_and_equals_the_backend(self):
        kit, backend = self._closed_route(), self._closed_route()
        before = self._ledger()
        self._login(self.admin)
        token = self._csrf("/rendiciones/ruta/%s" % kit.id)
        url = "/rendiciones/ruta/%s/reabrir" % kit.id
        res = self.url_open(url, data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Sí, reabrir", res.text)
        self.assertIn('id="rd-ask"', res.text)
        self.assertEqual(kit.state, "closed", "nothing without the confirmation")
        res = self.url_open(url, data={"csrf_token": token, "confirmar": "1"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertIn("reabierta", res.headers["Location"])
        backend.with_user(self.admin).action_reopen()
        for field in ("state", "reopen_count", "reopened_by_id"):
            self.assertEqual(kit[field], backend[field], field)
        self.assertEqual(kit.state, "on_route")
        self.assertTrue(any("Ruta reabierta para el repartidor por %s" % self.admin.name
                            in (m.body or "") for m in kit.message_ids))
        self.assertEqual(self._ledger(), before, "reopening touches no balance")

    def test_reopen_refused_to_drivers_and_after_the_count(self):
        route = self._closed_route()
        self._login(self.driver)
        res = self.url_open("/rendiciones/ruta/%s" % route.id, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/tablero", res.headers["Location"])
        token = self._csrf("/odoo")
        res = self.url_open("/rendiciones/ruta/%s/reabrir" % route.id,
                            data={"csrf_token": token, "confirmar": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/tablero", res.headers["Location"])
        self.assertEqual(route.state, "closed", "a driver never reopens it")
        # counted: administración is refused with the model's reason, gerencia may
        route.sudo().cash_counted = 600
        self._login(self.admin)
        token = self._csrf("/rendiciones/ruta/%s" % route.id)
        res = self.url_open("/rendiciones/ruta/%s/reabrir" % route.id,
                            data={"csrf_token": token, "confirmar": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("solo caja o gerencia", res.text)
        self.assertEqual(route.state, "closed")
        self._login(self.manager)
        token = self._csrf("/rendiciones/ruta/%s" % route.id)
        res = self.url_open("/rendiciones/ruta/%s/reabrir" % route.id,
                            data={"csrf_token": token, "confirmar": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(route.state, "on_route")
        self.assertEqual(route.reopened_by_id, self.manager)

    def test_cash_case_from_the_kit_equals_the_backend(self):
        kit, backend = self._closed_route(cash=600), self._closed_route(cash=600)
        for route in (kit, backend):
            route.sudo().write({"cash_counted": 550, "driver_explanation": "Le faltó cambio"})
        before = self._ledger()
        self._login(self.admin)
        token = self._csrf("/rendiciones/ruta/%s" % kit.id)
        res = self.url_open("/rendiciones/ruta/%s/caso" % kit.id, data={"csrf_token": token},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        backend.with_user(self.admin).action_open_cash_case()
        mine, theirs = kit._cash_case(), backend._cash_case()
        self.assertTrue(mine and theirs)
        self.assertIn("/casos/%s" % mine.id, res.headers["Location"])
        for field in ("kind", "amount", "candidates", "driver_explanation", "state"):
            self.assertEqual(mine[field], theirs[field], field)
        self.assertEqual(mine.amount, -50)
        # once open, the page links it instead of opening another
        html = self.url_open("/rendiciones/ruta/%s" % kit.id).text
        self.assertNotIn('/rendiciones/ruta/%s/caso"' % kit.id, html)
        self.assertIn("/casos/%s?desde=" % mine.id, html)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ cases

    def _cases(self):
        a = self._closed_route()
        b = self._closed_route(driver=self.other_driver, partner=self.other)
        Case = self.env["distri.route.case"].sudo()
        cash = Case.create({"route_id": a.id, "kind": "cash", "amount": -50})
        ret = Case.create({"route_id": a.id, "kind": "return", "amount": -120,
                           "stop_id": a.stop_ids.id})
        charge = Case.create({"route_id": a.id, "kind": "receipt", "amount": -300,
                              "state": "closed", "resolution": "Llegó tarde"})
        other = Case.create({"route_id": b.id, "kind": "unpaid", "amount": -900})
        return a, b, cash, ret, charge, other

    def test_cases_list_filters_and_detail(self):
        a, b, cash, ret, charge, other = self._cases()
        snap = self._snapshot(a | b)
        self._login(self.admin)
        html = self.url_open("/casos").text
        self.assertIn('data-page="casos"', html)
        for case in (cash, ret, other):
            self.assertIn('data-rd-caso="%s"' % case.id, html)
        self.assertNotIn('data-rd-caso="%s"' % charge.id, html, "closed: not by default")
        html = self.url_open("/casos?estado=resueltos").text
        self.assertIn('data-rd-caso="%s"' % charge.id, html)
        self.assertNotIn('data-rd-caso="%s"' % cash.id, html)
        html = self.url_open("/casos?tipo=cash").text
        self.assertIn('data-rd-caso="%s"' % cash.id, html)
        self.assertNotIn('data-rd-caso="%s"' % ret.id, html)
        html = self.url_open("/casos?repartidor=%s" % self.other_driver.id).text
        self.assertIn('data-rd-caso="%s"' % other.id, html)
        self.assertNotIn('data-rd-caso="%s"' % cash.id, html)
        # the backend's numbers for the same filter
        data = rv2.case_list(self.env(user=self.admin), [], "abiertos")
        self.assertEqual(data["total"], self.env["distri.route.case"].with_user(
            self.admin).search_count([("state", "=", "open")]))
        # «Cargos al repartidor»: only receipt / short / unpaid, per driver
        html = self.url_open("/casos/cargos").text
        self.assertIn('data-page="casos-cargos"', html)
        self.assertIn('data-rd-caso="%s"' % other.id, html)
        self.assertNotIn('data-rd-caso="%s"' % cash.id, html)
        self.assertIn(self.other_driver.name, html)
        html = self.url_open("/casos/cargos?estado=todos").text
        self.assertIn('data-rd-caso="%s"' % charge.id, html)
        # the detail: where it goes back, the history
        html = self.url_open("/casos/%s?desde=/rendiciones/ruta/%s" % (cash.id, a.id)).text
        self.assertIn('data-parent="/rendiciones/ruta/%s"' % a.id, html)
        self.assertIn('href="/rendiciones/ruta/%s"' % a.id, html)
        self.assertIn('href="/odoo/distri.route.case/%s"' % cash.id, html)
        html = self.url_open("/casos/%s?desde=//evil.example" % cash.id).text
        self.assertIn('data-parent="/casos"', html)
        self.assertIn('data-parent="/casos/cargos"',
                      self.url_open("/casos/%s?desde=/casos/cargos" % other.id).text)
        self.assertEqual(self.url_open("/casos/999999").status_code, 404)
        self.assertEqual(self._snapshot(a | b), snap, "the lists change nothing")

    def test_case_resolution_equals_the_backend(self):
        a, b, cash, ret, charge, other = self._cases()
        twin = self.env["distri.route.case"].sudo().create(
            {"route_id": a.id, "kind": "cash", "amount": -50})
        before = self._ledger()
        self._login(self.admin)
        url = "/casos/%s" % cash.id
        token = self._csrf(url)
        # closing without a resolution: the model's refusal, nothing saved
        res = self.url_open(url, data={"csrf_token": token, "accion": "cerrar",
                                       "investigacion": "Contamos de nuevo"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Escribí la resolución", res.text)
        self.assertIn("Contamos de nuevo", res.text, "what was typed stays on the page")
        self.assertEqual(cash.state, "open")
        self.assertFalse(cash.investigation)
        res = self.url_open(url, data={"csrf_token": token, "accion": "guardar",
                                       "investigacion": "Contamos de nuevo", "repuesto": "1"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertEqual(cash.investigation, "Contamos de nuevo")
        self.assertTrue(cash.replenished)
        res = self.url_open(url, data={"csrf_token": token, "accion": "cerrar",
                                       "investigacion": "Contamos de nuevo", "repuesto": "1",
                                       "resolucion": "Lo repuso el viernes"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        twin_admin = twin.with_user(self.admin)
        twin_admin.write({"investigation": "Contamos de nuevo", "replenished": True,
                          "resolution": "Lo repuso el viernes"})
        twin_admin.action_close()
        for field in ("state", "investigation", "resolution", "replenished"):
            self.assertEqual(cash[field], twin[field], field)
        self.assertEqual(cash.state, "closed")
        html = self.url_open(url).text
        self.assertIn("Lo repuso el viernes", html)
        self.assertNotIn('id="rd-resolver"', html, "a closed case is read-only")
        self.assertIn("Resuelto", html)
        self.assertIn("Repuesto por el repartidor", html)
        # a closed case is not written again
        res = self.url_open(url, data={"csrf_token": token, "accion": "guardar",
                                       "resolucion": "otra"}, allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(cash.resolution, "Lo repuso el viernes")
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ pending receipts

    def test_pending_receipts_actions_equal_the_backend(self):
        routes = [self._closed_route(cash=0, transfer=1000) for _i in range(4)]
        kit, backend, kit_cancel, backend_cancel = [r.promise_ids for r in routes]
        for promise in (kit, backend, kit_cancel, backend_cancel):
            self.assertEqual(promise.state, "open")
        open_one = self._closed_route(cash=0, transfer=500).promise_ids
        no_wa = self._closed_route(cash=0, transfer=500, partner=self.other).promise_ids
        before = self._ledger()
        self._login(self.admin)
        html = self.url_open("/rendiciones/comprobantes").text
        self.assertIn('data-page="rendiciones-comprobantes"', html)
        for promise in (kit, backend):
            self.assertIn('data-rd-promise="%s"' % promise.id, html)
        self.assertIn("Marcar llegó el comprobante", html)
        token = CSRF_RE.search(html).group(1)
        url = "/rendiciones/comprobantes/%s/llego" % kit.id
        res = self.url_open(url, data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertIn(rv2.CONFIRM_ARRIVED, res.text)
        self.assertEqual(kit.state, "open", "asked first")
        res = self.url_open(url, data={"csrf_token": token, "confirmar": "1",
                                       "volver": "/rendiciones/comprobantes?estado=todas"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/rendiciones/comprobantes?estado=todas", res.headers["Location"])
        backend.with_user(self.admin).action_mark_arrived()
        for field in ("state", "resolved_via", "resolved_by_id", "resolved_by_label"):
            self.assertEqual(kit[field], backend[field], field)
        self.assertEqual(kit.state, "fulfilled")
        # «Anular»
        res = self.url_open("/rendiciones/comprobantes/%s/anular" % kit_cancel.id,
                            data={"csrf_token": token, "confirmar": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        backend_cancel.with_user(self.admin).action_cancel()
        self.assertEqual(kit_cancel.state, backend_cancel.state)
        self.assertEqual(kit_cancel.state, "cancelled")
        # «Reclamar por WhatsApp»: the backend's wa.me link, logged the same way
        res = self.url_open("/rendiciones/comprobantes/%s/reclamar" % open_one.id,
                            data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].startswith("https://wa.me/"), res.headers)
        self.assertEqual(open_one.reminded_by_id, self.admin)
        res = self.url_open("/rendiciones/comprobantes/%s/reclamar" % no_wa.id,
                            data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertIn("no tiene WhatsApp", res.text)
        self.assertEqual(self._ledger(), before, "no money moves")

    # ------------------------------------------------------------ permissions

    def test_permissions_per_role(self):
        a, b, cash, ret, charge, other = self._cases()
        pages = ["/rendiciones", "/rendiciones/ruta/%s" % a.id, "/casos", "/casos/cargos",
                 "/casos/%s" % cash.id, "/rendiciones/comprobantes"]
        for user in (self.admin, self.manager, self.caja_admin, self.sistemas):
            self._login(user)
            for url in pages:
                res = self.url_open(url, allow_redirects=False)
                self.assertEqual(res.status_code, 200, (user.login, url, res.text[:200]))
            route_html = self.url_open(pages[1]).text
            self.assertIn("Ver en el sistema", route_html, user.login)
            self.assertEqual('href="/caja/rendicion/%s"' % a.id in route_html,
                             user == self.caja_admin, user.login)
        for user in (self.cashier, self.driver, self.seller):
            self._login(user)
            for url in pages if user != self.seller else pages[:-1]:
                res = self.url_open(url, allow_redirects=False)
                self.assertEqual(res.status_code, 303, (user.login, url))
                self.assertIn("/tablero", res.headers["Location"], (user.login, url))
        # a seller sees «Comprobantes pendientes» in the backend too (menu
        # Cobros), only his portfolio's (record rules) and without «Anular»
        self._login(self.seller)
        html = self.url_open("/rendiciones/comprobantes").text
        self.assertNotIn("data-rd-promise=", html, "none of his portfolio")
        self.assertNotIn("/anular", html)
        self._login(self.driver)
        self.assertEqual(self.url_open("/rendiciones/comprobantes",
                                       allow_redirects=False).status_code, 303)

    def test_csrf_is_required(self):
        route = self._closed_route()
        case = self.env["distri.route.case"].sudo().create(
            {"route_id": route.id, "kind": "cash", "amount": -10})
        self._login(self.manager)
        res = self.url_open("/rendiciones/ruta/%s/reabrir" % route.id,
                            data={"confirmar": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(route.state, "closed")
        res = self.url_open("/casos/%s" % case.id, data={"accion": "cerrar", "resolucion": "x"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 400)
        self.assertEqual(case.state, "open")

    # ------------------------------------------------------------ navigation, JS

    def test_screens_are_declared(self):
        self.assertEqual(screen_parents("/rendiciones/ruta/7"), ("/rendiciones",))
        self.assertEqual(screen_parents("/casos"), ("/rendiciones",))
        self.assertEqual(screen_parents("/casos/cargos"), ("/rendiciones",))
        self.assertEqual(screen_parents("/rendiciones/comprobantes"), ("/rendiciones",))
        self.assertIn("/rendiciones/ruta/<id>", screen_parents("/casos/12"))

    def test_confirm_dialog_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/rendiciones_v129_44.js"), root],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        checks = json.loads(res.stdout)
        failed = [name for name, ok in checks.items() if ok is not True]
        self.assertFalse(failed, checks)
