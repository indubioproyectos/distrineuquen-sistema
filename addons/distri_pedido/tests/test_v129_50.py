"""19.0.1.129.50.

1. /cuentas: the aggregate totals of the customer list («Total a cobrar»,
   «N clientes con deuda», «Pagos a confirmar», «Saldos a favor»,
   «Incobrables (aparte)») only for gerencia, administración and Sistemas;
   sellers, drivers and external sellers keep the rows with their balances
   but the server neither computes nor sends the totals (models/
   cuentas_app.py can_see_totals), and the «Deudores» export answers 403.
2. Galicia «Office Banking» receipts (synthetic text, made-up CUITs, names
   and accounts): payer name and CUIT, destination name, CUIT and CVU,
   amount, date, «Identificador de la operación» and the bank.
3. Inicio (ui_v2): each tile shows its short name (TILE_LABELS), the full
   title and subtitle in the tooltip; same tiles and links as before.

Nothing here writes a payment, a move or a balance (ledger fingerprint)."""
import os
import re
from datetime import datetime

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.tablero import (
    TILE_LABELS, badge_compact, grouped_tiles, label_is_long, staff_tiles, tile_label)
from odoo.addons.distri_pedido.models.cuentas_app import TOTAL_KEYS, can_see_totals
from odoo.addons.distri_pedido.models.ocr_comprobante import (
    cuit_is_valid, own_account_values, parse_receipt)
from odoo.addons.distri_pedido.tests.common_pedido import ensure_company_chart

PARAM_VERIFICACION = "distri_pedido.verificacion_v2"
# fictitious own account, as tests/test_ocr.py
OWN = own_account_values("23-99999999-9, Marcos Daniel Ejemplar, 0000003100090000000018")


def _ledger(env):
    return env["distri.promised.transfer"]._distri_ledger_fingerprint()


@tagged("post_install", "-at_install", "distri_v129_50")
class TestCuentasTotales(HttpCase):

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

        cls.allowed = {
            "administracion": user("t50_admin", "distri_pedido.grupo_administracion"),
            "gerencia": user("t50_gerencia", "distri_pedido.grupo_gerencia"),
            "sistemas": user("t50_sistemas", "base.group_system"),
        }
        cls.denied = {
            "vendedor": user("t50_vendedor", "distri_pedido.grupo_vendedor"),
            "repartidor": user("t50_reparto", "distri_pedido.grupo_reparto"),
            "externo": user("t50_externo", "distri_pedido.grupo_vendedor",
                            distri_tipo_vendedor="externo"),
            "vendedor_cajero": user("t50_vend_cajero",
                                    "distri_pedido.grupo_vendedor,distri_pedido.grupo_cajero"),
        }
        cls.customer = cls.env["res.partner"].create({"name": "T50 CLIENTE DEUDOR",
                                                      "customer_rank": 1})
        cls.env["distri.cuenta.movimiento"].sudo().create([dict(
            partner_id=cls.customer.id, fecha="2025-03-10", comprobante="0001/2025/50001",
            concepto="Factura", tipo="factura", debe=975_000_000.0, haber=0.0, origen_id=50001)])
        # the external seller's customer: an order he took
        cls.env["sale.order"].sudo().create({"partner_id": cls.customer.id,
                                             "user_id": cls.denied["externo"].id})

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1")

    def _app(self, user):
        return self.env["distri.account.app"].with_user(user)

    def test_rule(self):
        for role, user in self.allowed.items():
            self.assertTrue(can_see_totals(self.env(user=user)), role)
        for role, user in self.denied.items():
            self.assertFalse(can_see_totals(self.env(user=user)), role)

    def test_models_send_totals_only_to_the_allowed_roles(self):
        before = _ledger(self.env)
        for role, user in self.allowed.items():
            app = self._app(user)
            for data in (app.app_list(), app.app_list(limit=0), app.app_debtors()):
                self.assertTrue(data["can_see_totals"], role)
                for key in ("total_debt", "debtor_count"):
                    self.assertIn(key, data, "%s %s" % (role, key))
            # «Pagos a confirmar»: with the page, not in the count-only answer
            self.assertIn("total_candidates", app.app_list(), role)
            self.assertIn("total_candidates", app.app_debtors(), role)
            self.assertGreaterEqual(app.app_list()["total_debt"], 975_000_000.0, role)
            self.assertIn("credit_total", app.app_list())
        for role, user in self.denied.items():
            app = self._app(user)
            listed = app.app_list()
            for data in (listed, app.app_list(limit=0), app.app_debtors(settled=True)):
                self.assertFalse(data["can_see_totals"], role)
                self.assertFalse(set(TOTAL_KEYS) & set(data), role)
            # the rows and their balances stay
            row = next(r for r in listed["rows"] if r["id"] == self.customer.id)
            self.assertEqual(row["balance"], 975_000_000.0, role)
            debtors = app.app_debtors()["debtors"]
            self.assertIn(self.customer.id, {d["id"] for d in debtors}, role)
        self.assertEqual(_ledger(self.env), before)

    def test_http_api_and_export(self):
        for role, user in {**self.allowed, **self.denied}.items():
            self.authenticate(user.login, user.login)
            allowed = role in self.allowed
            for url in ("/cuentas/api/list", "/cuentas/api/list?limit=0",
                        "/cuentas/api/debtors?settled=1"):
                res = self.url_open(url)
                self.assertEqual(res.status_code, 200, "%s %s" % (role, url))
                body = res.text
                self.assertEqual('"total_debt"' in body, allowed, "%s %s" % (role, url))
                self.assertEqual('"credit_total"' in body, allowed and "debtors" not in url,
                                 "%s %s" % (role, url))
                if "limit=0" not in url:
                    self.assertIn("T50 CLIENTE DEUDOR", body, "%s %s" % (role, url))
            self.assertEqual(self.url_open("/cuentas").status_code, 200, role)
            if not allowed:
                for fmt in ("pdf", "xlsx"):
                    self.assertEqual(self.url_open("/cuentas/deudores.%s" % fmt,
                                                   allow_redirects=False).status_code, 403, role)

    def test_screen_draws_totals_only_when_sent(self):
        root = get_module_path("distri_pedido")
        with open(os.path.join(root, "static/src/cuentas/cuentas_app.js"), encoding="utf-8") as f:
            src = f.read()
        # every place that draws a total checks the flag the server sends
        for start in ("money(D.total_debt)", "Total a cobrar", "Clientes que deben"):
            for match in re.finditer(re.escape(start), src):
                window = src[max(0, match.start() - 400):match.start()]
                self.assertIn("can_see_totals", window, start)


@tagged("post_install", "-at_install", "distri_v129_50")
class TestGaliciaOfficeBanking(TransactionCase):

    def _check(self, result, operation, amount, day, payer, payer_cuit):
        self.assertEqual(result["bank"], "Banco Galicia")
        self.assertEqual(result["payer_name"], payer)
        self.assertEqual(result["payer_cuit"], payer_cuit)
        self.assertEqual(result["dest_name"], "Marcos Daniel Ejemplar")
        self.assertEqual(result["dest_cuit"], "23999999999")
        self.assertEqual(result["destination"], "0000003100090000000018")
        self.assertEqual(result["amount"], amount)
        self.assertEqual(result["amount_confidence"], "high")
        self.assertEqual(result["operation"], operation)
        self.assertEqual(result["date"].date(), day)
        self.assertFalse(result["payer_own"])

    def test_pdf_text_layout(self):
        """The text the module reads from the PDF (label and value on one line)."""
        root = get_module_path("distri_pedido")
        with open(os.path.join(root, "tests/data/galicia_office_banking.txt"),
                  encoding="utf-8") as f:
            text = f.read()
        self.assertTrue(cuit_is_valid("30700000016") and cuit_is_valid("23999999999"))
        result = parse_receipt(text, own=OWN)
        self._check(result, "ZQWTEx4127", 1234567.0, datetime(2026, 11, 24).date(),
                    "PATAGONIA EJEMPLO S. A. S.", "30700000016")
        # never the header «Datos del pagador», with or without the own account
        self.assertEqual(parse_receipt(text)["payer_name"], "PATAGONIA EJEMPLO S. A. S.")

    def test_one_value_per_line_layout(self):
        """The same receipt with each label and value on its own line."""
        text = "\n".join((
            "Identificador de la operación: KJHGfd5521", "Datos del pago",
            "Tipo de transferencia", "Nro. de Transferencia", "Proveedores", "1",
            "Fecha de envío", "Monto", "25/11/2026", "$ 98.500,50", "Concepto", "Descripción",
            "Varios", "-", "Datos del pagador", "CUIT/CUIL", "Razón Social", "20999999999",
            "Lucia Fernanda Inventada", "Cuenta a debitar", "CA$ 33334444",
            "Datos del destinatario", "CUIT/CUIL", "Razón social", "23999999999",
            "Marcos Daniel Ejemplar", "Cuenta en", "Cuenta de destino", "Mercado Pago",
            "0000003100090000000018", "COMPROBANTE DE LA OPERACIÓN",
            "1/1 Impreso 25/11/2026 - 18:03", "Office Banking"))
        result = parse_receipt(text, own=OWN)
        self._check(result, "KJHGfd5521", 98500.5, datetime(2026, 11, 25).date(),
                    "Lucia Fernanda Inventada", "20999999999")

    def test_company_names_with_a_legal_form(self):
        from odoo.addons.distri_pedido.models.ocr_comprobante import _looks_like_name
        self.assertEqual(_looks_like_name("PATAGONIA EJEMPLO S. A. S."), "PATAGONIA EJEMPLO S. A. S.")
        self.assertEqual(_looks_like_name("FRUTAS DEL SUR S.R.L."), "FRUTAS DEL SUR S.R.L.")
        self.assertEqual(_looks_like_name("ACME SA"), "ACME SA")
        # unchanged: people, and the headers are never a name
        self.assertEqual(_looks_like_name("Valeria Gómez"), "Valeria Gómez")
        self.assertEqual(_looks_like_name("Juan Sa"), "Juan Sa")
        for header in ("Datos del pagador", "Datos del destinatario", "Razón Social",
                       "Office Banking", "Impreso hoy"):
            self.assertEqual(_looks_like_name(header), "", header)
        # an identifier without digits is a word, not the operation
        self.assertEqual(parse_receipt("Identificador de la operación: SOLOLETRAS\n$ 1.000")
                         ["operation"], "")


@tagged("post_install", "-at_install", "distri_v129_50")
class TestInicioEtiquetas(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin = new_test_user(cls.env, login="admin_t50", name="Admin T50",
                                  groups="base.group_user,distri_pedido.grupo_administracion,"
                                         "base.group_system", **company)

    def test_short_names_and_tooltips(self):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_UI_V2, "1")
        Param.set_param(PARAM_VERIFICACION, "1")
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/tablero").text
        tiles = staff_tiles(self.env(user=self.admin))
        drawn = re.findall(r'data-tile="([^"]+)"', page)
        self.assertEqual(drawn, [t["key"] for g in grouped_tiles(tiles) for t in g["tiles"]])
        labels = dict(re.findall(
            r'data-tile="([^"]+)"[^>]*>\s*<span class="dn-tile__ico">.*?'
            r'<span class="dn-tile__lbl[^"]*">([^<]+)</span>', page, re.S))
        for tile in tiles:
            self.assertEqual(labels[tile["key"]], tile_label(tile), tile["key"])
            # never more than two words per line at 2 columns: short names
            self.assertLessEqual(max(len(w) for w in tile_label(tile).split()), 12, tile["key"])
        shown = {t["key"] for t in tiles}
        for key in ("revisar", "rutas", "reparto", "historial"):
            self.assertIn(key, shown)
        self.assertEqual(labels["rutas"], "Rutas")
        self.assertEqual(labels["revisar"], "Revisar")
        self.assertEqual(labels["reparto"], "Rendiciones")
        self.assertEqual(labels["historial"], "Cajas")
        # the full title stays in the tooltip and the spoken name
        self.assertIn('title="Planificación de rutas · Pedidos armados a cada ruta del día"', page)
        self.assertRegex(page, r'aria-label="Rutas · Planificación de rutas')
        self.assertTrue(set(TILE_LABELS) >= {"verificacion", "reportemp", "tablerocobros"})
        self.assertEqual(TILE_LABELS["tablerocobros"], "Cobros MP")
        self.assertEqual(TILE_LABELS["reportemp"], "Reporte MP")
        self.assertEqual(TILE_LABELS["verificacion"], "Verificación")

    def test_css_never_breaks_a_word(self):
        root = get_module_path("distri_pedido")
        with open(os.path.join(root, "static/src/tablero/tablero.css"), encoding="utf-8") as f:
            css = f.read()
        v2 = css[css.index("19.0.1.129.47"):]
        self.assertNotIn("overflow-wrap:anywhere", v2.split("19.0.1.129.48")[0])
        self.assertIn("hyphens:manual", v2)
        self.assertIn("-webkit-line-clamp:2", v2)
        self.assertIn("grid-auto-rows:1fr", v2)
        # owner, second review: 3 columns on the phone, 44 px icon with a
        # 26 px glyph, compact tiles
        self.assertIn(".tab2-tiles{display:grid;grid-template-columns:repeat(3,minmax(0,1fr))", v2)
        self.assertRegex(v2, r"\.tab2-tiles \.dn-tile__ico \.dn-i\{width:26px;height:26px\}")
        self.assertIn("min-height:100px", v2)

    def test_compact_badges_and_long_names(self):
        self.assertEqual(badge_compact(None), "")
        self.assertEqual(badge_compact({"text": "5", "n": 5}), "5")
        self.assertEqual(badge_compact({"text": "4 nuevos", "n": 4}), "4")
        self.assertEqual(badge_compact({"text": "441 · 436 vencidos", "n": 441}), "441")
        self.assertEqual(badge_compact({"text": "Abierta", "n": 1}), "", "a word is a dot")
        self.assertTrue(label_is_long("Comprobantes"))
        self.assertTrue(label_is_long("Rendiciones"))
        self.assertFalse(label_is_long("Mis ventas"))
        self.assertFalse(label_is_long("Reporte MP"))
