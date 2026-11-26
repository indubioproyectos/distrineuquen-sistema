"""19.0.1.129.43 «Cuentas de clientes» on the DistriNeuquén design system
(models/cuentas_app.py app_list, controllers/cuentas.py, static/src/cuentas/
cuentas_app.js «list-v2», tests/data/cuentas_v129_43.js):

- ONE list of every customer, highest debt first, then zero balances, then
  credit balances; no filter on by default (no «deudores» default, no
  «Saldos a favor o en cero» toggle);
- by pages (offset / limit, at most 200), searched on the server, the totals
  over every customer of the filters (equal to app_debtors);
- every balance equals the old full computation (app_debtors /
  debtor_entries), customer by customer, for each scope;
- permissions as before (seller, driver, external seller, wholesale customers,
  the «incobrable» state); PDF / Excel only for administración;
- flag off: the page and kit as in 129.42; the ledger never moves.
"""
import json
import os
import shutil
import subprocess
from datetime import date, timedelta
from unittest import SkipTest

from odoo import Command
from odoo.exceptions import AccessError
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.models.cuentas_app import LIST_MAX
from odoo.addons.distri_pedido.models.deudores import debtor_balances
from odoo.addons.distri_pedido.tests.common_pedido import ensure_company_chart

# far above any real debt of the copied data: these customers come first
BIG = 970_000_000.0


@tagged("post_install", "-at_install", "distri_v129_43")
class TestV12943Cuentas(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_company_chart(cls.env)
        company = cls.env.company

        def user(login, groups, **vals):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups="base.group_user" + ("," + groups if groups else ""),
                                company_id=company.id, company_ids=[Command.set(company.ids)])
            if vals:
                new.write(vals)
            return new

        cls.admin_u = user("cu43_admin", "distri_pedido.grupo_administracion")
        cls.seller_u = user("cu43_vendedor", "distri_pedido.grupo_vendedor")
        cls.driver_u = user("cu43_reparto", "distri_pedido.grupo_reparto")
        cls.external_u = user("cu43_externo", "distri_pedido.grupo_vendedor",
                              distri_tipo_vendedor="externo")
        cls.system_u = user("cu43_sistemas", "base.group_system")
        cls.plain_u = user("cu43_interno", "")
        cls.packer_u = user("cu43_armador", "distri_pedido.grupo_armador")

        Partner = cls.env["res.partner"]

        def customer(name, **vals):
            return Partner.create(dict({"name": name, "customer_rank": 1}, **vals))

        cls.p1 = customer("CU43 PRIMERO", ref="CU43-REF-1", city="Centenario")
        cls.p2 = customer("CU43 SEGUNDO", vat="20304050607")
        cls.p3 = customer("CU43 TERCERO", distri_whatsapp="5492995551234")
        cls.unc = customer("CU43 INCOBRABLE")
        cls.zero = customer("CU43 AL DIA")
        cls.cr_small = customer("CU43 A FAVOR CHICO")
        cls.cr_big = customer("CU43 A FAVOR GRANDE")
        cls.whole = customer("CU43 MAYORISTA", distri_es_mayorista=True)
        cls.ext = customer("CU43 DEL EXTERNO")
        cls.mine = (cls.p1 | cls.p2 | cls.p3 | cls.unc | cls.zero | cls.cr_small | cls.cr_big
                    | cls.whole | cls.ext)
        today = date.today()
        old, recent = "2025-03-10", (today - timedelta(days=5)).isoformat()
        rows = []
        for partner, base, amount, when in (
                (cls.p1, 43100, BIG + 9000.0, old), (cls.p2, 43200, BIG + 5000.0, old),
                (cls.unc, 43300, BIG + 7000.0, old), (cls.p3, 43400, 1500.0, recent),
                (cls.whole, 43500, BIG + 1000.0, old), (cls.ext, 43600, BIG + 2000.0, old),
                (cls.zero, 43700, 800.0, old)):
            rows.append(dict(partner_id=partner.id, fecha=when, comprobante="0001/2025/%d" % base,
                             concepto="Factura", tipo="factura", debe=amount, haber=0.0,
                             origen_id=base))
        for partner, base, amount in ((cls.zero, 43701, 800.0), (cls.cr_small, 43800, 100.0),
                                      (cls.cr_big, 43900, 2000.0)):
            rows.append(dict(partner_id=partner.id, fecha=old, comprobante="PBNK1/2025/%d" % base,
                             concepto="Pago", tipo="pago", debe=0.0, haber=amount,
                             origen_id=base))
        cls.env["distri.cuenta.movimiento"].sudo().create(rows)
        cls.unc.sudo().write({"distri_uncollectible": True,
                              "distri_uncollectible_note": "Prueba 129.43"})
        # a posted Odoo payment too (the receivable lines, not only history)
        cash = cls.env["account.journal"].search(
            [("type", "=", "cash"), ("company_id", "=", company.id)], limit=1)
        payment = cls.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": cls.p3.id,
            "amount": 300.0, "journal_id": cash.id, "date": today - timedelta(days=1)})
        payment.action_post()
        # the external seller's customer: an order he took
        cls.env["sale.order"].sudo().create({"partner_id": cls.ext.id,
                                             "user_id": cls.external_u.id})

    # ------------------------------------------------------------ helpers

    def _app(self, user):
        return self.env["distri.account.app"].with_user(user)

    def _all_rows(self, user, **kw):
        """Every row of app_list, page after page."""
        rows, offset = [], 0
        while True:
            data = self._app(user).app_list(offset=offset, limit=kw.pop("limit", 37), **kw)
            rows += data["rows"]
            offset += len(data["rows"])
            if not data["more"]:
                return rows, data
            self.assertTrue(data["rows"], "more pages never come empty")

    def _old(self, user):
        """The 129.42 numbers: app_debtors with every customer."""
        data = self._app(user).app_debtors(settled=True)
        return data, {d["id"]: d for d in data["debtors"] + data["uncollectible"] + data["settled"]}

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _flag(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def setUp(self):
        super().setUp()
        # 19.0.1.129.45: /cuentas/api/list and the exports need ui_v2
        self._flag(True)

    def _mine(self, rows):
        return [r for r in rows if r["id"] in self.mine.ids]

    # ------------------------------------------------------------ the list

    def test_one_list_ordered_debt_zero_credit(self):
        rows, data = self._all_rows(self.admin_u)
        names = [r["name"] for r in self._mine(rows)]
        self.assertEqual(names, [
            "CU43 PRIMERO", "CU43 INCOBRABLE", "CU43 SEGUNDO", "CU43 DEL EXTERNO",
            "CU43 MAYORISTA", "CU43 TERCERO", "CU43 AL DIA", "CU43 A FAVOR CHICO",
            "CU43 A FAVOR GRANDE"])
        balances = [r["balance"] for r in rows]
        self.assertEqual(balances, sorted(balances, reverse=True), "highest debt first, all of them")
        states = {r["name"]: r["state"] for r in self._mine(rows)}
        self.assertEqual(states["CU43 PRIMERO"], "debt")
        self.assertEqual(states["CU43 INCOBRABLE"], "uncollectible")
        self.assertEqual(states["CU43 AL DIA"], "zero")
        self.assertEqual(states["CU43 A FAVOR GRANDE"], "credit")
        self.assertEqual([r["rank"] for r in rows], list(range(1, len(rows) + 1)))
        self.assertEqual(data["counts"]["all"], len(rows))

    def test_no_default_filter(self):
        data = self._app(self.admin_u).app_list(limit=LIST_MAX)
        self.assertEqual((data["state"], data["sort"], data["q"], data["zone"], data["seller"]),
                         ("all", "debt", "", "", "all"))
        self.assertEqual(data["min_balance"], 0)
        self.assertEqual(data["min_age"], 0)
        self.assertEqual(data["total_rows"], data["counts"]["all"])
        self.authenticate(self.admin_u.login, self.admin_u.login)
        res = self.url_open("/cuentas/api/list?limit=%d" % LIST_MAX)
        self.assertEqual(res.status_code, 200)
        listed = {r["id"]: r["state"] for r in res.json()["data"]["rows"]}
        # the settled customers come in the same list, unasked
        self.assertEqual(listed[self.zero.id], "zero")
        self.assertEqual(listed[self.cr_big.id], "credit")
        self.assertEqual(listed[self.p1.id], "debt")
        # the v2 list of the page has no «deudores» default nor the toggle
        path = os.path.join(get_module_path("distri_pedido"), "static/src/cuentas/cuentas_app.js")
        with open(path, encoding="utf-8") as f:
            js = f.read()
        v2 = js[js.index("// list-v2:start"):js.index("// ---- account")]
        self.assertNotIn("Saldos a favor o en cero", v2)
        self.assertNotIn("t-set", v2)
        self.assertIn('state: "all"', v2)

    def test_pages_limits_and_counts(self):
        app = self._app(self.admin_u)
        full = app.app_list(limit=LIST_MAX)
        self.assertLessEqual(len(full["rows"]), LIST_MAX)
        first = app.app_list(limit=3)
        second = app.app_list(offset=3, limit=3)
        self.assertEqual([r["id"] for r in first["rows"] + second["rows"]],
                         [r["id"] for r in full["rows"][:6]])
        self.assertTrue(first["more"])
        self.assertEqual(second["offset"], 3)
        self.assertEqual(len(app.app_list(limit=10_000)["rows"]), min(LIST_MAX, full["total_rows"]))
        self.assertEqual(len(app.app_list(limit="x")["rows"]), min(50, full["total_rows"]))
        last = app.app_list(offset=full["total_rows"], limit=5)
        self.assertEqual((last["rows"], last["more"]), ([], False))
        counts = app.app_list(limit=0)
        self.assertNotIn("rows", counts, "limit 0: only the counts")
        self.assertEqual(counts["total_rows"], full["total_rows"])
        self.assertEqual(counts["total_debt"], full["total_debt"])

    def test_totals_equal_the_old_computation(self):
        for user in (self.admin_u, self.seller_u, self.driver_u, self.external_u, self.system_u):
            old, _ = self._old(user)
            new = self._app(user).app_list(limit=1)
            if user in (self.seller_u, self.driver_u, self.external_u):
                # 19.0.1.129.50: no aggregate totals outside gerencia,
                # administración and Sistemas
                self.assertNotIn("total_debt", new, user.login)
                self.assertNotIn("total_debt", old, user.login)
                continue
            self.assertEqual(new["total_debt"], old["total_debt"], user.login)
            self.assertEqual(new["debtor_count"], old["debtor_count"], user.login)
            self.assertEqual(new["uncollectible_total"], old["uncollectible_total"], user.login)
            settled = [d["balance"] for d in old["settled"]]
            self.assertEqual(new["credit_total"], round(sum(b for b in settled if b < -0.005), 2))
            self.assertEqual(new["customer_count"],
                             len(old["debtors"]) + len(old["uncollectible"]) + len(settled))
            self.assertEqual(new["total_candidates"], old["total_candidates"], user.login)

    def test_balances_equal_customer_by_customer(self):
        for user in (self.admin_u, self.seller_u, self.external_u):
            _, old = self._old(user)
            rows, _ = self._all_rows(user)
            new = {r["id"]: r for r in rows}
            self.assertEqual(set(new), set(old), user.login)
            for pid, row in new.items():
                was = old[pid]
                for key in ("balance", "last_payment_date", "last_payment_amount",
                            "last_invoice_date", "candidate_count", "oldest_owed_date",
                            "zone", "seller", "phone", "comment_count"):
                    self.assertEqual(row[key], was[key], (user.login, row["name"], key))
        # the batched balance is the statement's (debtor_balances) for all
        customers = self._app(self.admin_u)._app_customers()
        batch = self.env["res.partner"].sudo()._distri_account_balances(customers.ids)
        full = debtor_balances(self.env, customers)
        for pid in customers.ids:
            self.assertEqual(round(batch[pid], 2), round(full[pid]["balance"], 2), pid)
        self.assertEqual(round(batch[self.p3.id], 2), 1200.0, "history minus the posted payment")

    def test_search_on_the_server(self):
        app = self._app(self.admin_u)

        def found(q):
            return {r["id"] for r in app.app_list(q=q, limit=LIST_MAX)["rows"]} & set(self.mine.ids)

        self.assertEqual(found("cu43 favor grande"), {self.cr_big.id})
        self.assertEqual(found("tercero"), {self.p3.id})
        self.assertEqual(found("CU43-REF-1"), {self.p1.id})
        self.assertEqual(found("30405060"), {self.p2.id}, "CUIT digits")
        self.assertEqual(found("2995551234"), {self.p3.id}, "WhatsApp digits")
        self.assertEqual(found("al día"), {self.zero.id}, "accents do not matter")
        data = app.app_list(q="cu43 favor")
        self.assertEqual(data["counts"]["credit"], 2)
        # the total is the account's, not the search's
        self.assertEqual(data["total_debt"], app.app_list(limit=1)["total_debt"])
        self.assertEqual(data["rows"][0]["rank"] > 1, True, "the rank of the whole list")

    def test_optional_filters(self):
        app = self._app(self.admin_u)

        def names(**kw):
            return [r["name"] for r in self._mine(app.app_list(limit=LIST_MAX, **kw)["rows"])]

        self.assertEqual(names(state="credit"), ["CU43 A FAVOR CHICO", "CU43 A FAVOR GRANDE"])
        self.assertEqual(names(state="zero"), ["CU43 AL DIA"])
        self.assertEqual(names(state="uncollectible"), ["CU43 INCOBRABLE"])
        self.assertNotIn("CU43 TERCERO", names(min_age=30), "a 5-day debt is not 30 days old")
        self.assertIn("CU43 PRIMERO", names(min_age=30))
        self.assertNotIn("CU43 AL DIA", names(min_age=30))
        self.assertEqual(names(min_balance=BIG)[:2], ["CU43 PRIMERO", "CU43 INCOBRABLE"])
        self.assertNotIn("CU43 TERCERO", names(min_balance=BIG))
        by_name = names(sort="name")
        self.assertEqual(by_name, sorted(by_name))
        oldest = names(sort="oldest")
        self.assertLess(oldest.index("CU43 PRIMERO"), oldest.index("CU43 TERCERO"))
        self.assertGreater(oldest.index("CU43 AL DIA"), oldest.index("CU43 TERCERO"),
                           "no red row: last")
        zone = next(r["zone"] for r in app.app_list(limit=LIST_MAX)["rows"] if r["id"] == self.p1.id)
        self.assertTrue(zone, "the city gives the zone")
        self.assertIn(zone, app.app_list(limit=1, with_zones=True)["zones"])
        self.assertIsNone(app.app_list(limit=1)["zones"], "the zones only when asked")
        self.assertEqual(names(zone=zone), ["CU43 PRIMERO"])
        self.assertEqual(app.app_list(state="nope", sort="nope", limit=1)["state"], "all")

    # ------------------------------------------------------------ permissions

    def test_permissions_as_before(self):
        seller = self._app(self.seller_u).app_list(limit=LIST_MAX)
        listed = {r["id"]: r for r in seller["rows"]}
        self.assertNotIn(self.whole.id, listed, "a seller never reaches a wholesale customer")
        self.assertEqual(listed[self.unc.id]["state"], "debt", "no «incobrable» for a seller")
        self.assertFalse(seller["can_see_uncollectible"])
        self.assertFalse(seller["can_filter_seller"])
        self.assertFalse(seller["can_export"])
        self.assertEqual(seller["counts"]["uncollectible"], 0)
        self.assertEqual(self._app(self.seller_u).app_list(state="uncollectible", limit=1)["state"],
                         "all")
        self.assertEqual(self._app(self.seller_u).app_list(seller="italo", limit=1)["seller"],
                         "all")
        external = {r["id"] for r in self._all_rows(self.external_u)[0]}
        self.assertEqual(external & set(self.mine.ids), {self.ext.id})
        admin = self._app(self.admin_u).app_list(limit=1)
        self.assertTrue(admin["can_export"])
        self.assertTrue(admin["can_see_uncollectible"])
        self.assertFalse(self._app(self.system_u).app_list(limit=1)["can_export"])
        for user in (self.plain_u, self.packer_u):
            with self.assertRaises(AccessError):
                self._app(user).app_list()
            self.authenticate(user.login, user.login)
            self.assertEqual(self.url_open("/cuentas/api/list").status_code, 403)
            self.assertEqual(self.url_open("/cuentas/deudores.xlsx", allow_redirects=False)
                             .status_code, 403)

    def test_exports_for_administracion_only(self):
        before = self._ledger()
        self.authenticate(self.seller_u.login, self.seller_u.login)
        self.assertEqual(self.url_open("/cuentas/deudores.xlsx", allow_redirects=False).status_code, 403)
        self.assertEqual(self.url_open("/cuentas/deudores.pdf", allow_redirects=False).status_code, 403)
        self.authenticate(self.admin_u.login, self.admin_u.login)
        res = self.url_open("/cuentas/deudores.xlsx?seller=italo", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/web/content/", res.headers["Location"])
        res = self.url_open("/cuentas/deudores.pdf", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/report/pdf/distri_pedido.deudores_documento/", res.headers["Location"])
        self.assertEqual(self.url_open("/cuentas/deudores.txt", allow_redirects=False).status_code, 404)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ flag, kit, ledger

    def test_flag_off_as_before_and_on_with_the_system(self):
        self.authenticate(self.admin_u.login, self.admin_u.login)
        self._flag(False)
        page = self.url_open("/cuentas").text
        self.assertNotIn("data-v2", page)
        self.assertNotIn("dn-v2.css", page)
        self.assertNotIn("dn-v2.css", self.url_open("/tablero").text, "kit screens as in 129.42")
        old = self.url_open("/cuentas/api/debtors").json()["data"]
        self.assertIn("debtors", old)
        self.assertEqual(old["settled"], [], "the old API keeps its default")
        self._flag(True)
        page = self.url_open("/cuentas").text
        self.assertIn('data-v2="1"', page)
        self.assertIn("/dn/dn-v2.css?v=", page)
        self.assertIn("/dn/dn-v2.css?v=", self.url_open("/tablero").text)
        css = self.url_open("/dn/dn-v2.css?v=x").text
        for token in ("--dn-input:#7d8da2", "--dn-link:#007868", "--dn-focus:var(--dn-navy)"):
            self.assertIn(token, css)
        base = self.url_open("/dn/dn.css?v=x").text
        self.assertIn("--dn-input:#b9c6d6", base, "dn.css keeps today's values")
        self.assertIn(".dn-menu{", base)
        self.assertIn(".dn-top__action{", base)
        self.assertRegex(base, r"\.dn-btn--sm\{[^}]*min-height:44px")

    def test_ledger_unchanged(self):
        before = self._ledger()
        for user in (self.admin_u, self.seller_u):
            self._all_rows(user)
            self._app(user).app_list(q="cu43", state="credit", sort="oldest", min_age=1)
        self.authenticate(self.admin_u.login, self.admin_u.login)
        self.assertEqual(self.url_open("/cuentas/api/list").status_code, 200)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ the page's JS

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/cuentas_v129_43.js"), root],
                             capture_output=True, text=True, timeout=60)
        self.assertEqual(res.returncode, 0, res.stderr)
        checks = json.loads(res.stdout)
        failed = [name for name, ok in checks.items() if ok is not True]
        self.assertFalse(failed, checks)
