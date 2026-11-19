"""19.0.1.112.0: the «Cuentas» statement with a free date range, «Compartir
PDF» and «Por WhatsApp» (a public, signed, expiring, read-only link), the PC
debtors filters, and the bundled fixes (bulto label town, v108 bridge gone,
old cover files, depot CSS, no «Aprobar» for driver expenses).

Synthetic data only."""
import os
from datetime import date, datetime, timedelta
from urllib.parse import unquote

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models import revisar as revisar_model
from odoo.addons.distri_pedido.controllers import revisar as revisar_controller
from odoo.addons.distri_pedido.models.cuentas_app import filter_debtors
from odoo.addons.distri_pedido.models.deudores import debtor_balances
from odoo.addons.distri_pedido.models.estado_cuenta import (
    AR_TZ, PARAM_LINK_DAYS, read_share_token, share_token,
)
from odoo.addons.distri_pedido.models.etiquetas import label_town

MODULE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@tagged("post_install", "-at_install", "distri_v112", "distri_v112_estado")
class TestEstadoCuentaV112(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        Category = cls.env["res.partner.category"]
        cls.tag_a = Category.create({"name": "Cartera V112 A"})
        cls.tag_b = Category.create({"name": "Cartera V112 B"})

        def user(login, groups, tag=None):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups=groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if tag:
                new.etiqueta_cartera_id = tag
            return new

        cls.admin_u = user("v112_admin", "base.group_user,distri_pedido.grupo_administracion")
        cls.seller_a = user("v112_vendedor_a", "base.group_user,distri_pedido.grupo_vendedor",
                            cls.tag_a)
        cls.driver_u = user("v112_reparto", "base.group_user,distri_pedido.grupo_reparto")

        Partner = cls.env["res.partner"]
        cls.pa = Partner.create({
            "name": "ESTADO V112 PROPIO", "customer_rank": 1, "phone": "+54 299 555-1120",
            "category_id": [Command.link(cls.tag_a.id)]})
        cls.pb = Partner.create({
            "name": "ESTADO V112 AJENO", "customer_rank": 1, "phone": "+54 299 555-1121",
            "category_id": [Command.link(cls.tag_b.id)]})
        Mov = cls.env["distri.cuenta.movimiento"].sudo()
        rows = []
        for partner, base in ((cls.pa, 112100), (cls.pb, 112200)):
            rows += [
                dict(partner_id=partner.id, fecha="2026-01-10", comprobante="0001/2026/%d" % base,
                     tipo="factura", debe=1000.0, haber=0.0, origen_id=base),
                dict(partner_id=partner.id, fecha="2026-02-10",
                     comprobante="PBNK1/2026/%d" % base, tipo="pago", debe=0.0, haber=400.0,
                     origen_id=base + 1),
                dict(partner_id=partner.id, fecha="2026-03-10",
                     comprobante="0001/2026/%d" % (base + 2), tipo="factura", debe=700.0,
                     haber=0.0, origen_id=base + 2),
                dict(partner_id=partner.id, fecha="2026-04-10",
                     comprobante="PBNK1/2026/%d" % (base + 3), tipo="pago", debe=0.0, haber=200.0,
                     origen_id=base + 3),
            ]
        Mov.create(rows)

    def _app(self, user):
        return self.env["distri.account.app"].with_user(user)

    # ------------------------------------------------------------ date range

    def test_free_range_rows_opening_and_totals(self):
        data = self._app(self.admin_u).app_account(
            self.pa.id, period="custom", date_from="2026-02-01", date_to="2026-03-31")
        self.assertEqual((data["period"], data["date_from"], data["date_to"]),
                         ("custom", "2026-02-01", "2026-03-31"))
        self.assertEqual([r["date"] for r in data["rows"]], ["2026-03-10", "2026-02-10"])
        self.assertEqual(data["opening"], 1000.0)
        self.assertEqual((data["debit"], data["credit"], data["closing"]), (700.0, 400.0, 1300.0))
        self.assertEqual(data["balance"], 1100.0, "the header keeps today's balance")
        # one end open
        data = self._app(self.admin_u).app_account(self.pa.id, period="custom",
                                                   date_from="2026-03-01")
        self.assertEqual([r["date"] for r in data["rows"]], ["2026-04-10", "2026-03-10"])
        self.assertEqual(data["opening"], 600.0)
        # no dates: everything
        data = self._app(self.admin_u).app_account(self.pa.id, period="custom")
        self.assertEqual((data["period"], data["row_count"]), ("all", 4))
        # quick periods still run up to today
        data = self._app(self.admin_u).app_account(self.pa.id, period="all")
        self.assertEqual(data["date_to"], "")

    def test_bad_range_is_refused(self):
        app = self._app(self.admin_u)
        with self.assertRaises(UserError):
            app.app_account(self.pa.id, period="custom", date_from="2026-04-01",
                            date_to="2026-03-01")
        with self.assertRaises(UserError):
            app.app_account(self.pa.id, period="custom", date_from="31/12/2026")
        self.authenticate("v112_admin", "v112_admin")
        res = self.url_open("/cuentas/api/account?id=%d&period=custom&date_from=2026-04-01"
                            "&date_to=2026-03-01" % self.pa.id)
        self.assertEqual(res.status_code, 400)
        self.assertIn("posterior", res.json()["error"])

    def test_fifo_red_is_the_same_in_any_range(self):
        app = self._app(self.admin_u)
        full = {r["line_key"]: r["owed"] for r in app.app_account(self.pa.id, "all")["rows"]}
        self.assertEqual(sorted(v for v in full.values() if v), [400.0, 700.0])
        part = app.app_account(self.pa.id, "custom", date_from="2026-03-01",
                               date_to="2026-03-31")
        for row in part["rows"]:
            self.assertEqual(row["owed"], full[row["line_key"]])
        self.assertEqual((part["owed_count"], part["owed_total"]), (2, 1100.0),
                         "the red total follows the whole account, not the range")

    # ------------------------------------------------------------ share rules

    def test_seller_shares_only_his_portfolio(self):
        app = self._app(self.seller_a)
        own = app.app_account(self.pa.id, "all")["share"]
        self.assertTrue(own["pdf"])
        # v92: he reads any account, but F9 decides what he may print or send
        other = app.app_account(self.pb.id, "all")
        self.assertEqual(other["partner"]["name"], "ESTADO V112 AJENO")
        self.assertFalse(other["share"]["pdf"])
        self.assertFalse(other["share"]["whatsapp_url"])
        self.assertIn("cartera", other["share"]["pdf_reason"])
        # a driver reads accounts but prints none
        self.assertFalse(self._app(self.driver_u).app_account(self.pa.id, "all")["share"]["pdf"])

    def test_statement_pdf_through_http(self):
        self.authenticate("v112_vendedor_a", "v112_vendedor_a")
        url = "/cuentas/api/statement_pdf?id=%d&period=custom&date_from=2026-02-01&date_to=2026-03-31"
        res = self.url_open(url % self.pa.id)
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertEqual(res.headers["Content-Type"], "application/pdf")
        # tests render the report as HTML unless forced (ir.actions.report)
        self.assertTrue(res.content.startswith(b"%PDF") or b"ESTADO V112 PROPIO" in res.content)
        res = self.url_open(url % self.pb.id)
        self.assertEqual(res.status_code, 403)
        self.assertFalse(res.json()["ok"])
        self.authenticate("v112_reparto", "v112_reparto")
        self.assertEqual(self.url_open(url % self.pa.id).status_code, 403)

    def test_whatsapp_link_only_while_the_account_is_shown(self):
        app = self._app(self.seller_a)
        hidden = app.app_account(self.pa.id, "all")["share"]
        self.assertFalse(hidden["whatsapp_url"])
        self.assertIn("oculta", hidden["whatsapp_reason"])
        self.pa.sudo().distri_cuenta_visible = True
        share = app.app_account(self.pa.id, "custom", date_from="2026-02-01",
                                date_to="2026-03-31")["share"]
        self.assertTrue(share["whatsapp_url"].startswith("https://wa.me/549299555112"))
        self.assertIn("/estado-de-cuenta/", share["link"])
        text = unquote(share["whatsapp_url"])
        self.assertIn(share["link"], text)
        self.assertIn("del 01/02/2026 al 31/03/2026", text)
        self.assertIn("vence el %s" % share["expires"], text)
        # no WhatsApp loaded: no link
        self.pa.sudo().phone = False
        share = app.app_account(self.pa.id, "all")["share"]
        self.assertFalse(share["whatsapp_url"])
        self.assertIn("WhatsApp", share["whatsapp_reason"])

    # ------------------------------------------------------------ the token

    def test_token_is_signed_and_expires(self):
        # unset: 7 days
        self.env["ir.config_parameter"].sudo().set_param(PARAM_LINK_DAYS, False)
        token, expires = share_token(self.env, self.pa, date(2026, 2, 1), date(2026, 3, 31))
        link = read_share_token(self.env, token)
        self.assertEqual(link["partner"], self.pa)
        self.assertEqual((link["date_from"], link["date_to"]), (date(2026, 2, 1), date(2026, 3, 31)))
        self.assertEqual(expires.date(), datetime.now(AR_TZ).date() + timedelta(days=7))
        # another customer with the same signature, a changed range, garbage
        encoded, signature = token.split(".")
        forged = share_token(self.env, self.pb)[0].split(".")[0] + "." + signature
        self.assertIsNone(read_share_token(self.env, forged))
        self.assertIsNone(read_share_token(self.env, token[:-2] + ("AA" if token[-2:] != "AA" else "BB")))
        for junk in ("", "abc", "a.b.c", "....", None):
            self.assertIsNone(read_share_token(self.env, junk))
        # expired
        self.assertIsNone(read_share_token(self.env, token, now=expires + timedelta(seconds=1)))
        self.assertTrue(read_share_token(self.env, token, now=expires - timedelta(minutes=1)))
        # the parameter sets the days
        self.env["ir.config_parameter"].sudo().set_param(PARAM_LINK_DAYS, "2")
        _token, expires = share_token(self.env, self.pa)
        self.assertEqual(expires.date(), datetime.now(AR_TZ).date() + timedelta(days=2))

    def test_public_page_shows_only_that_customer_read_only(self):
        self.pa.sudo().distri_cuenta_visible = True
        self.pb.sudo().distri_cuenta_visible = True
        token, _expires = share_token(self.env, self.pa, date(2026, 2, 1), date(2026, 3, 31))
        res = self.url_open("/estado-de-cuenta/%s" % token)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn("ESTADO V112 PROPIO", html)
        self.assertNotIn("ESTADO V112 AJENO", html)
        self.assertIn("Saldo anterior", html)
        self.assertIn("1.100", html, "today's balance")
        # the customer's view: remitos, never the invoice number
        self.assertNotIn("0001/2026/112102", html)
        self.assertNotIn("Odoo Online", html)
        self.assertIn("noindex", res.headers.get("X-Robots-Tag", ""))
        # only the range of the link
        self.assertIn("10/03/2026", html)
        self.assertNotIn("10/04/2026", html)
        # read-only: GET only
        res = self.url_open("/estado-de-cuenta/%s" % token, data={"x": "1"})
        self.assertEqual(res.status_code, 405)
        # bad or expired link: nothing about anybody
        for bad in ("nada", token[:-3] + "xyz"):
            res = self.url_open("/estado-de-cuenta/%s" % bad)
            self.assertEqual(res.status_code, 404)
            self.assertNotIn("ESTADO V112", res.text)
        self.env["ir.config_parameter"].sudo().set_param(PARAM_LINK_DAYS, "1")
        old, expires = share_token(self.env, self.pa,
                                   now=datetime.now(AR_TZ) - timedelta(days=3))
        self.assertLess(expires, datetime.now(AR_TZ))
        self.assertEqual(self.url_open("/estado-de-cuenta/%s" % old).status_code, 404)

    def test_public_page_respects_the_hidden_account(self):
        token, _expires = share_token(self.env, self.pa)
        html = self.url_open("/estado-de-cuenta/%s" % token).text
        self.assertIn("en construcción", html)
        self.assertNotIn("1.100", html)

    # ------------------------------------------------------------ PC debtors

    def test_oldest_red_matches_the_statement(self):
        bulk = debtor_balances(self.env, self.pa)[self.pa.id]["oldest_owed"]
        rows = self.pa._distri_statement_rows()
        first = next(r["date"] for r in rows if r["owed"])
        self.assertEqual(bulk, first)
        self.assertEqual(bulk, date(2026, 1, 10))

    def test_filter_debtors_rule(self):
        today = date(2026, 11, 18)
        entries = [
            {"zone": "Zapala", "balance": 5000.0, "oldest_owed": date(2026, 8, 1)},
            {"zone": "Centro", "balance": 900.0, "oldest_owed": date(2026, 11, 1)},
            {"zone": "", "balance": 20000.0, "oldest_owed": None},
        ]
        self.assertEqual(len(filter_debtors(entries, today=today)), 3)
        self.assertEqual(filter_debtors(entries, zone="Zapala", today=today), entries[:1])
        self.assertEqual(filter_debtors(entries, min_balance="1000", today=today),
                         [entries[0], entries[2]])
        self.assertEqual(filter_debtors(entries, min_age=30, today=today), entries[:1])
        self.assertEqual(filter_debtors(entries, min_age="x", min_balance=None, today=today),
                         entries)

    def test_debtors_filters_on_the_app(self):
        app = self._app(self.admin_u)
        data = app.app_debtors()
        mine = next(d for d in data["debtors"] if d["id"] == self.pa.id)
        self.assertEqual(mine["oldest_owed_date"], "2026-01-10")
        self.assertGreater(mine["oldest_owed_days"], 180)
        self.assertIn(mine["zone"], data["zones"])
        self.assertTrue(data["report_url"])

        def ids(**kw):
            return {d["id"] for d in app.app_debtors(**kw)["debtors"]}

        self.assertIn(self.pa.id, ids(zone=mine["zone"]))
        self.assertNotIn(self.pa.id, ids(zone="Zona que no existe V112"))
        self.assertIn(self.pa.id, ids(min_balance=1000))
        self.assertNotIn(self.pa.id, ids(min_balance=1101))
        self.assertIn(self.pa.id, ids(min_age=180))
        filtered = app.app_debtors(min_balance=1101)
        self.assertEqual(filtered["total_debt"],
                         round(sum(d["balance"] for d in filtered["debtors"]), 2))
        # a seller gets no link to the backend report
        self.assertFalse(self._app(self.seller_a).app_debtors()["report_url"])
        self.authenticate("v112_admin", "v112_admin")
        res = self.url_open("/cuentas/api/debtors?min_balance=1101&zone=")
        self.assertNotIn(self.pa.id, {d["id"] for d in res.json()["data"]["debtors"]})

    def test_page_loads_the_new_assets(self):
        self.authenticate("v112_admin", "v112_admin")
        html = self.url_open("/cuentas").text
        self.assertIn("cuentas_app.js?v=", html)
        self.assertIn("cuentas_app.css?v=", html)


@tagged("post_install", "-at_install", "distri_v112", "distri_v112_fixes")
class TestBundledFixesV112(HttpCase):

    def test_label_prefers_the_customer_city(self):
        Partner = self.env["res.partner"]
        atencio = Partner.create({"name": "C-ATENCIO ALMACEN CHOS MALAL", "city": "Chos Malal"})
        self.assertEqual(label_town(atencio), ("Chos Malal", ""))
        atencio.city = False
        self.assertEqual(label_town(atencio), ("Cutral Có", "C"))
        zc = Partner.create({"name": "ZC - KIOSCO V112", "city": "CUTRAL CO"})
        self.assertEqual(label_town(zc), ("CUTRAL CO", "ZC"), "its own prefix for its town")
        plain = Partner.create({"name": "KIOSCO V112", "city": "cutral có"})
        self.assertEqual(label_town(plain), ("cutral có", "C"))
        y = Partner.create({"name": "Y - DESPENSA V112", "city": "Neuquén"})
        self.assertEqual(label_town(y), ("Neuquén", ""), "the city wins over the prefix")
        company = Partner.create({"name": "C - EMPRESA V112", "city": "Zapala",
                                  "company_type": "company"})
        contact = Partner.create({"name": "C - SUCURSAL V112", "parent_id": company.id,
                                  "type": "delivery"})
        contact.city = False
        self.assertEqual(label_town(contact)[0], "Zapala", "the company's city")
        self.assertEqual(label_town(Partner.browse(), generic=True, zone="Plottier"),
                         ("Plottier", "Y"))

    def test_v108_bridge_is_gone(self):
        self.assertFalse(hasattr(self.env["sale.order"], "action_distri_revisar_asignar"))
        self.assertFalse(hasattr(revisar_controller, "asignar_backend"))
        self.assertTrue(revisar_model.puede_revisar)
        # the backend wizard stays
        self.assertIn("distri.asignar.cliente", self.env.registry)

    def test_old_cover_files_are_gone(self):
        img = os.path.join(MODULE, "static", "src", "sitio", "img")
        names = os.listdir(img)
        self.assertFalse([n for n in names if n.startswith("portada2-")])
        self.assertNotIn("og-distri-neuquen-2.jpg", names)
        self.assertIn("portada3-1280.webp", names)
        self.assertIn("og-distri-neuquen-3.jpg", names)

    def test_depot_panel_rows(self):
        with open(os.path.join(MODULE, "static", "src", "deposito", "deposito.css")) as css:
            self.assertIn(".dp-panel__h+.dp-li{border-top:0}", css.read())

    def test_no_approve_for_driver_expenses(self):
        views = self.env["ir.ui.view"].search([("model", "in", ("distri.route.expense",
                                                                "distri.route"))])
        for view in views:
            arch = view.arch_db or ""
            self.assertNotIn('string="Aprobar"', arch, view.xml_id)
            self.assertNotIn("action_approve", arch, view.xml_id)
        self.assertFalse(hasattr(self.env["distri.route.expense"], "action_approve"))
        for name in ("caja/caja.js", "reparto/app.js"):
            path = os.path.join(MODULE, "static", "src", name)
            if os.path.exists(path):
                with open(path) as js:
                    self.assertNotIn("Aprobar", js.read(), name)
