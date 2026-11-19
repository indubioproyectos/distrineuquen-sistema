import base64
import json
from datetime import date, timedelta

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.cuentas import Cuentas
from odoo.addons.distri_pedido.models.cuentas_app import app_scope
from odoo.addons.distri_pedido.models.deudores import seller_tags

# far above any real debt of the copied data: these customers come first
BIG = 950_000_000.0
# 1x1 transparent PNG
PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
CASH_MEMO = "Efectivo · cobró PRUEBA · planilla 09/11 · 0001/2026/99001"


@tagged("post_install", "-at_install", "distri_cuentas_app")
class TestCuentasApp(HttpCase):
    """«Cuentas (celular)» (models/cuentas_app.py, controllers/cuentas.py):
    who opens it, the seller's portfolio, the same numbers as «Deudores» and
    the statement, the receipts and the comments; nothing moves money."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        Category = cls.env["res.partner.category"]
        cls.tag_a = Category.create({"name": "Cartera Prueba A"})
        cls.tag_b = Category.create({"name": "Cartera Prueba B"})

        def user(login, groups, tag=None):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups=groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if tag:
                new.etiqueta_cartera_id = tag
            return new

        cls.admin_u = user("cuentas_admin", "base.group_user,distri_pedido.grupo_administracion")
        cls.manager_u = user("cuentas_gerente", "base.group_user,distri_pedido.grupo_gerencia")
        cls.system_u = user("cuentas_sistema", "base.group_user,base.group_system")
        cls.seller_a = user("cuentas_vendedor_a", "base.group_user,distri_pedido.grupo_vendedor",
                            cls.tag_a)
        cls.seller_b = user("cuentas_vendedor_b", "base.group_user,distri_pedido.grupo_vendedor",
                            cls.tag_b)
        cls.plain_u = user("cuentas_interno", "base.group_user")
        cls.packer_u = user("cuentas_armador", "base.group_user,distri_pedido.grupo_armador")
        cls.driver_u = user("cuentas_reparto", "base.group_user,distri_pedido.grupo_reparto")

        Partner = cls.env["res.partner"]
        cls.pa = Partner.create({
            "name": "CUENTAS PRUEBA A", "customer_rank": 1, "ref": "ODOO-990001",
            "city": "Neuquén", "street": "Calle Prueba 1", "phone": "+54 299 555-0001",
            "category_id": [Command.link(cls.tag_a.id)]})
        cls.pa_zero = Partner.create({
            "name": "CUENTAS PRUEBA A AL DIA", "customer_rank": 1,
            "category_id": [Command.link(cls.tag_a.id)]})
        cls.pb = Partner.create({
            "name": "CUENTAS PRUEBA B", "customer_rank": 1,
            "category_id": [Command.link(cls.tag_b.id)]})
        Mov = cls.env["distri.cuenta.movimiento"].sudo()
        rows = []
        for partner, base, amount in ((cls.pa, 99100, BIG + 7000.0), (cls.pb, 99200, BIG + 3000.0)):
            rows += [
                dict(partner_id=partner.id, fecha="2025-06-10", comprobante="0001/2025/%d" % base,
                     concepto="Factura vieja", tipo="factura", debe=amount, haber=0.0,
                     origen_id=base),
                dict(partner_id=partner.id, fecha="2025-07-15", comprobante="PBNK1/2025/%d" % base,
                     concepto="Pago viejo", tipo="pago", debe=0.0, haber=1000.0,
                     origen_id=base + 1),
            ]
        rows += [
            dict(partner_id=cls.pa_zero.id, fecha="2025-06-10", comprobante="0001/2025/99300",
                 tipo="factura", debe=500.0, haber=0.0, origen_id=99300),
            dict(partner_id=cls.pa_zero.id, fecha="2025-06-11", comprobante="PBNK1/2025/99300",
                 tipo="pago", debe=0.0, haber=500.0, origen_id=99301),
        ]
        Mov.create(rows)
        Mov.search([("origen_id", "=", 99100)]).write(
            {"distri_owed": True, "distri_owed_note": "Revisada en la prueba"})

        Journal = cls.env["account.journal"]
        cls.bank = Journal.search([("type", "=", "bank"), ("company_id", "=", company.id)], limit=1)
        cls.cash = Journal.search([("type", "=", "cash"), ("company_id", "=", company.id)], limit=1)
        today = date.today()
        cls.card_a = cls._card(cls.pa, 2500.0, "990000000101", today - timedelta(days=3))
        cls.card_b = cls._card(cls.pb, 1800.0, "990000000102", today - timedelta(days=2))
        cls.cash_pay = cls.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": cls.pa.id,
            "amount": 400.0, "journal_id": cls.cash.id, "date": today - timedelta(days=1),
            "memo": CASH_MEMO})
        cls.cash_pay.action_post()

    @classmethod
    def _card(cls, partner, amount, operation, when):
        payment = cls.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": partner.id,
            "amount": amount, "journal_id": cls.bank.id, "date": when,
            "memo": "MP %s" % operation})
        payment.action_post()
        attachment = cls.env["ir.attachment"].sudo().create({
            "name": "comprobante-%s.png" % operation, "raw": PNG, "mimetype": "image/png"})
        message = cls.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.cuentas.%s" % operation, "direction": "out",
            "message_type": "image", "media_mime_type": "image/png",
            "attachment_id": attachment.id})
        return cls.env["distri.mp.collection"].sudo().create({
            "kind": "income", "source_id": operation, "amount": amount,
            "date": "%s 13:00:00" % when, "partner_id": partner.id, "payer_name": "PAGADOR PRUEBA",
            "state": "confirmed", "payment_id": payment.id, "receipt_id": message.id})

    # ------------------------------------------------------------ helpers

    def _app(self, user):
        return self.env["distri.account.app"].with_user(user)

    def _get(self, url):
        return self.url_open(url, allow_redirects=False)

    def _post(self, url, body):
        return self.url_open(url, data=json.dumps(body),
                             headers={"Content-Type": "application/json"})

    def _row(self, partner, predicate):
        return next(r for r in partner._distri_statement_rows() if predicate(r))

    def _card_row(self, partner, card):
        return self._row(partner, lambda r: r.get("card_id") == card.id)

    # ------------------------------------------------------------ access

    def test_access_matrix(self):
        for user in (self.admin_u, self.manager_u, self.system_u):
            self.assertEqual(app_scope(self.env(user=user)), "all", user.login)
        # 19.0.1.92.0: sellers and drivers review every customer
        for user in (self.seller_a, self.seller_b, self.driver_u):
            self.assertEqual(app_scope(self.env(user=user)), "staff", user.login)
        for user in (self.plain_u, self.packer_u):
            self.assertIsNone(app_scope(self.env(user=user)), user.login)
            with self.assertRaises(AccessError):
                self._app(user).app_debtors()
        self.assertIsNone(app_scope(self.env(user=self.env.ref("base.public_user"))))

        # anonymous: the login page
        res = self._get("/cuentas")
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertIn("/web/login", res.headers.get("Location", ""))
        self.assertNotEqual(self._get("/cuentas/api/debtors").status_code, 200)

        for user in (self.admin_u, self.manager_u, self.system_u, self.seller_a, self.driver_u):
            self.authenticate(user.login, user.login)
            page = self._get("/cuentas")
            self.assertEqual(page.status_code, 200, user.login)
            # 19.0.1.112.0: cuentas_app.js (cuentas.js stays one version)
            self.assertIn("cuentas_app.js", page.text)
            self.assertTrue(self._get("/cuentas/api/debtors").json()["ok"], user.login)
        for user in (self.plain_u, self.packer_u):
            self.authenticate(user.login, user.login)
            self.assertEqual(self._get("/cuentas").status_code, 403, user.login)
            self.assertEqual(self._get("/cuentas/api/debtors").status_code, 403, user.login)
            self.assertEqual(self._get("/cuentas/api/account?id=%d" % self.pa.id).status_code, 403)
            self.assertEqual(self._post("/cuentas/api/comment", {
                "partner_id": self.pa.id, "body": "no"}).status_code, 403)
        self.assertFalse(self.env["distri.account.comment"].search([("partner_id", "=", self.pa.id)]))

    def test_seller_locked_to_the_accounts_app(self):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param("distri_pedido.vendedor_solo_cuentas", "0")
        self.assertEqual(self.seller_a._distri_landing(), "")
        Param.set_param("distri_pedido.vendedor_solo_cuentas", "1")
        self.assertEqual(self.seller_a._distri_landing(), "/cuentas")
        for user in (self.admin_u, self.manager_u, self.system_u, self.plain_u):
            self.assertFalse(user._distri_seller_only_accounts(), user.login)
        menu = self.env.ref("sale.sale_menu_root", raise_if_not_found=False)
        self.authenticate(self.seller_a.login, self.seller_a.login)
        urls = ["/odoo", "/web", "/odoo/action-base.action_res_users"]
        if menu:
            urls.append("/odoo?menu_id=%d" % menu.id)
        for url in urls:
            res = self._get(url)
            self.assertIn(res.status_code, (301, 302, 303), url)
            self.assertTrue(res.headers.get("Location", "").endswith("/cuentas"), url)
        page = self._get("/cuentas")
        self.assertEqual(page.status_code, 200)
        self.assertTrue(self._get("/cuentas/api/debtors").json()["ok"])
        # the app is his only screen: its «Salir» must log him out
        res = self._get("/web/session/logout?redirect=%2Fweb%2Flogin")
        self.assertIn(res.status_code, (301, 302, 303))
        self.assertIn("/web/login", res.headers.get("Location", ""))
        self.assertNotEqual(self._get("/cuentas/api/debtors").status_code, 200)
        self.authenticate(self.admin_u.login, self.admin_u.login)
        res = self._get("/odoo/action-base.action_res_users")
        self.assertFalse(res.headers.get("Location", "").endswith("/cuentas"))

    def test_seller_and_driver_see_every_customer(self):
        """19.0.1.92.0: sellers and drivers review any customer's account
        (old missing receipts): list, account, lines, receipts, comments."""
        self.assertEqual(self._app(self.seller_a).app_debtors()["settled"], [],
                         "the settled customers come only when asked")
        data = self._app(self.seller_a).app_debtors(seller="italo", settled=True)
        self.assertTrue(data["settled_loaded"])
        self.assertFalse(data["can_filter_seller"])
        self.assertEqual(data["seller"], "all", "a seller has no seller filter")
        ids = {d["id"] for d in data["debtors"] + data["settled"] + data["uncollectible"]}
        self.assertTrue({self.pa.id, self.pb.id} <= {d["id"] for d in data["debtors"]})
        self.assertIn(self.pa_zero.id, {d["id"] for d in data["settled"]})
        self.assertEqual(data["total_debt"], round(sum(d["balance"] for d in data["debtors"]), 2))
        key = self._card_row(self.pb, self.card_b)["line_key"]
        for user in (self.seller_a, self.driver_u):
            app = self._app(user)
            self.assertEqual(app.app_account(self.pb.id)["partner"]["name"], "CUENTAS PRUEBA B")
            self.assertEqual(app.app_line(self.pb.id, key)["card"]["id"], self.card_b.id)
            self.assertEqual(app.app_receipt(self.pb.id, self.card_b.id)[0], PNG)
            for bad in (0, -1, "x", None, 10 ** 9):
                with self.assertRaises(AccessError):
                    app.app_account(bad)
        comment = self._app(self.driver_u).app_add_comment(self.pb.id, "falta el comprobante")
        self.assertEqual(comment["author"], self.driver_u.name)
        self.assertTrue(self._app(self.driver_u).app_delete_comment(comment["id"]))
        # the partner record rule itself is not loosened
        self.assertFalse(self.env["res.partner"].with_user(self.seller_a).search(
            [("id", "=", self.pb.id)]))

        self.authenticate(self.seller_a.login, self.seller_a.login)
        res = self._get("/cuentas/api/account?id=%d" % self.pb.id).json()
        self.assertTrue(res["ok"])
        self.assertEqual(self._get("/cuentas/api/line?id=%d&key=%s" % (self.pb.id, key)).status_code,
                         200)
        self.assertEqual(self._get("/cuentas/api/receipt?id=%d&card=%d" % (
            self.pb.id, self.card_b.id)).status_code, 200)
        own = self._get("/cuentas/api/account?id=%d" % self.pa.id).json()
        self.assertEqual(own["data"]["partner"]["ref"], "ODOO-990001")

    def test_plain_seller_never_reaches_a_distributor(self):
        """The hidden-list policy (models/lista_oculta.py) still holds: a
        seller without Administración does not see a distributor's account;
        administración and a driver do."""
        self.pb.distri_lista_oculta = True
        with self.assertRaises(AccessError):
            self._app(self.seller_a).app_account(self.pb.id)
        with self.assertRaises(AccessError):
            self._app(self.seller_a).app_receipt(self.pb.id, self.card_b.id)
        with self.assertRaises(AccessError):
            self._app(self.seller_a).app_add_comment(self.pb.id, "no")
        self.assertNotIn(self.pb.id, {d["id"] for d in self._app(self.seller_a).app_debtors(
            settled=True)["debtors"]})
        self.assertTrue(self._app(self.admin_u).app_account(self.pb.id))
        self.assertTrue(self._app(self.driver_u).app_account(self.pb.id))
        self.authenticate(self.seller_a.login, self.seller_a.login)
        self.assertEqual(self._get("/cuentas/api/account?id=%d" % self.pb.id).status_code, 403)

    def test_seller_reads_only_his_portfolio_payer_identities(self):
        """19.0.1.91.0 (E2E-B B4): CUITs and phones of the payers, only the
        seller's portfolio; administración and gerencia read all of them."""
        Identity = self.env["distri.payer.identity"].sudo()
        own = Identity.create({"kind": "cuit", "value": "20999000101", "partner_id": self.pa.id})
        other = Identity.create({"kind": "phone", "value": "2995550202", "partner_id": self.pb.id})
        mine = Identity.with_user(self.seller_a).search([("id", "in", (own | other).ids)])
        self.assertEqual(mine, own)
        self.assertEqual(Identity.with_user(self.seller_b).search(
            [("id", "in", (own | other).ids)]), other)
        with self.assertRaises(AccessError):
            other.with_user(self.seller_a).read(["value"])
        self.assertEqual(self.pa.with_user(self.seller_a).distri_payer_ids, own)
        for user in (self.admin_u, self.manager_u):
            self.assertEqual(Identity.with_user(user).search(
                [("id", "in", (own | other).ids)]), own | other)
        # the accounts app keeps working for the seller's own customer
        self.authenticate(self.seller_a.login, self.seller_a.login)
        self.assertTrue(self._get("/cuentas/api/account?id=%d" % self.pa.id).json()["ok"])

    def test_seller_never_sees_the_uncollectible_state(self):
        self.pa.sudo().write({"distri_uncollectible": True,
                              "distri_uncollectible_note": "Nota de prueba"})
        seller = self._app(self.seller_a).app_debtors()
        self.assertFalse(seller["can_see_uncollectible"])
        self.assertEqual(seller["uncollectible"], [])
        self.assertEqual(seller["uncollectible_total"], 0)
        self.assertIn(self.pa.id, {d["id"] for d in seller["debtors"]},
                      "an uncollectible customer is an ordinary debtor for his seller")
        self.assertEqual([d["rank"] for d in seller["debtors"]],
                         list(range(1, len(seller["debtors"]) + 1)))
        balances = [d["balance"] for d in seller["debtors"]]
        self.assertEqual(balances, sorted(balances, reverse=True))
        own = self._app(self.seller_a).app_account(self.pa.id)["partner"]
        self.assertFalse(own["uncollectible"])
        self.assertEqual(own["uncollectible_note"], "")

        admin = self._app(self.admin_u).app_debtors()
        self.assertTrue(admin["can_see_uncollectible"])
        self.assertIn(self.pa.id, {d["id"] for d in admin["uncollectible"]})
        self.assertNotIn(self.pa.id, {d["id"] for d in admin["debtors"]})
        full = self._app(self.admin_u).app_account(self.pa.id)["partner"]
        self.assertTrue(full["uncollectible"])
        self.assertEqual(full["uncollectible_note"], "Nota de prueba")

    def test_debtors_equal_the_debtor_report(self):
        report = self.env["distri.debtor.report"].with_user(self.admin_u).create({})
        data = self._app(self.admin_u).app_debtors()
        self.assertTrue(data["can_filter_seller"])
        lines = report.line_ids.sorted("rank")
        self.assertEqual([(d["id"], d["balance"]) for d in data["debtors"]],
                         [(l.partner_id.id, l.balance) for l in lines])
        self.assertEqual([d["rank"] for d in data["debtors"]], lines.mapped("rank"))
        self.assertAlmostEqual(data["total_debt"], report.total_debt, 2)
        self.assertEqual(data["debtor_count"], report.debtor_count)
        self.assertEqual({d["id"] for d in data["uncollectible"]},
                         set(report.uncollectible_line_ids.partner_id.ids))
        self.assertAlmostEqual(data["uncollectible_total"], report.uncollectible_total, 2)
        first = data["debtors"][0]
        line = lines[0]
        self.assertEqual(first["last_payment_date"], line.last_payment_date.isoformat()
                         if line.last_payment_date else "")
        self.assertEqual(first["candidate_count"], line.candidate_count)
        # highest debt first: the two test customers lead
        self.assertEqual([d["id"] for d in data["debtors"][:2]], [self.pa.id, self.pb.id])
        balances = [d["balance"] for d in data["debtors"]]
        self.assertEqual(balances, sorted(balances, reverse=True))
        self.assertTrue(all(d["balance"] <= 0.005 for d in data["settled"]))

        italo, _distri = seller_tags(self.env)
        if italo:
            report.seller = "italo"
            italo_data = self._app(self.admin_u).app_debtors(seller="italo")
            self.assertEqual([d["id"] for d in italo_data["debtors"]],
                             report.line_ids.sorted("rank").partner_id.ids)
            self.assertAlmostEqual(italo_data["total_debt"], report.total_debt, 2)

    # ------------------------------------------------------------ statement

    def test_statement_payload_equals_the_statement(self):
        app = self._app(self.admin_u)
        full = self.pa._distri_statement()
        data = app.app_account(self.pa.id, period="all", limit=500)
        expected = list(reversed(full["rows"]))
        self.assertEqual(len(data["rows"]), len(expected))
        for got, row in zip(data["rows"], expected):
            self.assertEqual(got["line_key"], row["line_key"])
            self.assertEqual(got["date"], row["date"].isoformat())
            self.assertEqual((got["doc_type"], got["number"], got["concept"], got["remito"]),
                             (row["doc_type"], row["number"], row["concept"], row["remito"]))
            self.assertAlmostEqual(got["debit"], row["debit"] or 0.0)
            self.assertAlmostEqual(got["credit"], row["credit"] or 0.0)
            self.assertAlmostEqual(got["balance"], row["balance"])
            self.assertEqual(got["card_id"], row.get("card_id") or False)
        self.assertAlmostEqual(data["balance"], full["balance"])
        self.assertAlmostEqual(data["balance"], self.pa._distri_account_balance())
        self.assertEqual(data["opening"], 0.0)
        # 19.0.1.99.0: the red is FIFO and sums the balance; the review mark is a note
        owed = [r for r in data["rows"] if r["owed"]]
        self.assertEqual(len(owed), data["owed_count"])
        self.assertAlmostEqual(sum(r["owed"] for r in owed), max(full["balance"], 0.0))
        self.assertAlmostEqual(data["owed_total"], max(full["balance"], 0.0))
        noted = [r for r in data["rows"] if r["review_note"]]
        self.assertEqual([r["line_key"] for r in noted], ["online:99100"])
        self.assertEqual(noted[0]["review_note"], "Revisada en la prueba")

        # 3 months: the old history goes to the opening balance
        data = app.app_account(self.pa.id)
        self.assertEqual(data["period"], "3m")
        cut = date.fromisoformat(data["date_from"])
        period = self.pa._distri_statement(cut, False)
        self.assertAlmostEqual(data["opening"], period["opening"])
        self.assertAlmostEqual(data["opening"], BIG + 6000.0)
        self.assertEqual([r["line_key"] for r in data["rows"]],
                         [r["line_key"] for r in reversed(period["rows"])])

        # pages: newest first, one after the other
        first = app.app_account(self.pa.id, period="all", limit=2)
        self.assertTrue(first["more"])
        second = app.app_account(self.pa.id, period="all", offset=2, limit=500)
        self.assertNotIn("partner", second)
        self.assertEqual([r["line_key"] for r in first["rows"] + second["rows"]],
                         [r["line_key"] for r in expected])

    def test_line_key_is_stable(self):
        keys = [r["line_key"] for r in self.pa._distri_statement_rows()]
        self.assertEqual(keys, [r["line_key"] for r in self.pa._distri_statement_rows()])
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("online:99100", keys)
        self.assertIn("account.move:%d" % self.cash_pay.move_id.id, keys)
        # a new import of the Online history (new local ids, same Online id)
        old = self.env["distri.cuenta.movimiento"].sudo().search([("origen_id", "=", 99101)])
        vals = old.copy_data()[0]
        old.unlink()
        self.env["distri.cuenta.movimiento"].sudo().create(vals)
        self.assertIn("online:99101", [r["line_key"] for r in self.pa._distri_statement_rows()])

    def test_card_and_receipt_only_for_the_partners_payments(self):
        row = self._card_row(self.pa, self.card_a)
        for user in (self.admin_u, self.seller_a):
            detail = self._app(user).app_line(self.pa.id, row["line_key"])
            card = detail["card"]
            self.assertEqual(card["id"], self.card_a.id)
            self.assertEqual(card["operation"], "990000000101")
            self.assertEqual(card["payer_name"], "PAGADOR PRUEBA")
            self.assertTrue(card["has_receipt"])
            self.assertTrue(card["receipt_is_image"])
            raw, mimetype, _name = self._app(user).app_receipt(self.pa.id, self.card_a.id)
            self.assertEqual(raw, PNG)
            self.assertEqual(mimetype, "image/png")
        # the card of another customer, asked through this customer
        for user in (self.admin_u, self.seller_a):
            with self.assertRaises(AccessError):
                self._app(user).app_receipt(self.pa.id, self.card_b.id)
        self.assertFalse(self.env["distri.account.app"]._app_card_for(self.pa, self.card_b.id))

        self.authenticate(self.seller_a.login, self.seller_a.login)
        res = self._get("/cuentas/api/receipt?id=%d&card=%d" % (self.pa.id, self.card_a.id))
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.content, PNG)
        self.assertEqual(self._get("/cuentas/api/receipt?id=%d&card=%d" % (
            self.pa.id, self.card_b.id)).status_code, 403)
        line = self._get("/cuentas/api/line?id=%d&key=%s" % (self.pa.id, row["line_key"])).json()
        self.assertEqual(line["data"]["card"]["id"], self.card_a.id)

    def test_cash_payment_and_online_rows(self):
        app = self._app(self.admin_u)
        cash_row = self._row(self.pa, lambda r: r["res_model"] == "account.move"
                             and r["res_id"] == self.cash_pay.move_id.id)
        detail = app.app_line(self.pa.id, cash_row["line_key"])
        self.assertNotIn("card", detail)
        self.assertIn("Efectivo", detail["cash"]["memo"])
        self.assertIn("planilla 09/11", detail["cash"]["memo"])
        self.assertIsNone(detail["cash"]["sheet"], "a cash payment on no sheet row has no sheet")
        online = app.app_line(self.pa.id, "online:99100")
        self.assertEqual(online["online"]["origen_id"], 99100)
        self.assertEqual(online["online"]["comprobante"], "0001/2025/99100")
        fifo = self._row(self.pa, lambda r: r["line_key"] == "online:99100")
        self.assertAlmostEqual(online["owed"], fifo["owed"])
        self.assertEqual(online["review_note"], "Revisada en la prueba")
        with self.assertRaises(UserError):
            app.app_line(self.pa.id, "online:1")

    # ------------------------------------------------------------ comments

    def test_comments_scope_and_rules(self):
        Comment = self.env["distri.account.comment"]
        key = self._card_row(self.pa, self.card_a)["line_key"]
        self.authenticate(self.seller_a.login, self.seller_a.login)
        res = self._post("/cuentas/api/comment", {"partner_id": self.pa.id,
                                                  "body": "  Llamar el lunes  "}).json()
        self.assertTrue(res["ok"], res)
        self.assertEqual(res["data"]["body"], "Llamar el lunes")
        self.assertTrue(res["data"]["can_delete"])
        res = self._post("/cuentas/api/comment", {"partner_id": self.pa.id, "line_key": key,
                                                  "body": "Este pago es de dos facturas"}).json()
        self.assertTrue(res["ok"], res)
        self.assertIn("Pago", res["data"]["line_label"])
        self.assertEqual(self._post("/cuentas/api/comment", {
            "partner_id": self.pa.id, "body": "   "}).status_code, 400)
        self.assertEqual(self._post("/cuentas/api/comment", {
            "partner_id": self.pa.id, "line_key": "online:1", "body": "x"}).status_code, 400)
        # a form post (no JSON) is refused
        self.assertEqual(self.url_open("/cuentas/api/comment", data={
            "partner_id": self.pa.id, "body": "form"}).status_code, 415)

        comments = Comment.search([("partner_id", "=", self.pa.id)])
        self.assertEqual(len(comments), 2)
        self.assertEqual(comments.create_uid, self.seller_a)

        # counts on the debtor row and on the statement row
        data = self._app(self.admin_u).app_debtors()
        self.assertEqual(next(d for d in data["debtors"] if d["id"] == self.pa.id)["comment_count"], 2)
        account = self._app(self.admin_u).app_account(self.pa.id, period="all", limit=500)
        self.assertEqual(len(account["comments"]), 1)
        self.assertEqual(next(r for r in account["rows"] if r["line_key"] == key)["comment_count"], 1)
        self.assertEqual(len(self._app(self.admin_u).app_line(self.pa.id, key)["comments"]), 1)
        self.assertEqual(self.pa.with_user(self.admin_u).distri_comment_count, 2)

        # the other seller neither reads nor writes them
        self.assertFalse(Comment.with_user(self.seller_b).search([("partner_id", "=", self.pa.id)]))
        with self.assertRaises(AccessError):
            Comment.with_user(self.seller_b).create({"partner_id": self.pa.id, "body": "x"})
        self.assertEqual(len(Comment.with_user(self.admin_u).search(
            [("partner_id", "=", self.pa.id)])), 2)
        with self.assertRaises(AccessError):
            Comment.with_user(self.packer_u).search([])

        # not edited; resolved is fine; deleted only by its author within the hour
        account_comment = comments.filtered(lambda c: not c.line_key)
        line_comment = comments - account_comment
        with self.assertRaises(AccessError):
            account_comment.with_user(self.seller_a).write({"body": "otro texto"})
        account_comment.with_user(self.admin_u).write({"resolved": True})
        with self.assertRaises(AccessError):
            account_comment.with_user(self.admin_u).unlink()
        self.authenticate(self.admin_u.login, self.admin_u.login)
        self.assertEqual(self._post("/cuentas/api/comment/delete", {
            "id": account_comment.id}).status_code, 403)
        self.authenticate(self.seller_a.login, self.seller_a.login)
        self.assertTrue(self._post("/cuentas/api/comment/delete", {
            "id": account_comment.id}).json()["ok"])
        self.assertFalse(account_comment.exists())
        self.env.cr.execute("UPDATE distri_account_comment SET create_date = create_date - "
                            "interval '2 hours' WHERE id = %s", [line_comment.id])
        line_comment.invalidate_recordset()
        with self.assertRaises(AccessError):
            line_comment.with_user(self.seller_a).unlink()

    # ------------------------------------------------------------ read-only

    def test_no_money_action_is_reachable(self):
        routes = {}
        for name in dir(Cuentas):
            routing = getattr(getattr(Cuentas, name), "original_routing", None)
            if routing:
                for url in routing["routes"]:
                    routes[url] = tuple(routing.get("methods") or ("GET",))
        self.assertTrue(all(url.startswith("/cuentas") for url in routes))
        writes = {url for url, methods in routes.items() if "POST" in methods}
        self.assertEqual(writes, {"/cuentas/api/comment", "/cuentas/api/comment/delete"})
        public = {name for name in dir(type(self.env["distri.account.app"]))
                  if name.startswith("app_")}
        # 19.0.1.112.0: app_statement_pdf prints the statement report (a read)
        self.assertEqual(public, {"app_debtors", "app_account", "app_line", "app_receipt",
                                  "app_sheet_file", "app_add_comment", "app_delete_comment",
                                  "app_statement_pdf"})

        def money_state():
            return (self.env["account.payment"].search_count([]),
                    self.env["account.move"].search_count([]),
                    self.env["distri.mp.collection"].search_count([("state", "=", "confirmed")]),
                    self.env["distri.mp.collection"].search_count([("state", "=", "draft")]))

        before = money_state()
        self.authenticate(self.admin_u.login, self.admin_u.login)
        key = self._card_row(self.pa, self.card_a)["line_key"]
        for url in ("/cuentas/api/debtors", "/cuentas/api/account?id=%d&period=all" % self.pa.id,
                    "/cuentas/api/line?id=%d&key=%s" % (self.pa.id, key),
                    "/cuentas/api/receipt?id=%d&card=%d" % (self.pa.id, self.card_a.id)):
            self.assertEqual(self._get(url).status_code, 200, url)
        self.assertEqual(money_state(), before)
        self.assertEqual(self.card_a.state, "confirmed")
        # the app works without the permission to authorize money
        self.assertFalse(self.admin_u.has_group("distri_pedido.grupo_cobros"))
