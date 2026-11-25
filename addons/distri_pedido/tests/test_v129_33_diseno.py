"""19.0.1.129.33 («Etapa 3b», part 1). Everything behind existing flags.

- ui_v2: /despacho and /cuentas take the kit's look and the role's bottom
  bar; with the flag off they render as before (distri_ui.css, no bar). The
  same users open them (or are refused) with the flag on and off.
- ui_v2: «Equipo» (/equipo, #74), read-only, for administración, gerencia
  and Sistemas; «Ordenar el Inicio» (/tablero/ordenar, #3), each user's own
  order of the tiles. Both 404 with the flag off, and Inicio is unchanged.
- reparto_v2: the customer's WhatsApp saved from «Ubicación y horarios»
  goes through the same «whatsapp» sync event and validation as the old
  visit; the app5.js checks (a saved visit edited again, the message of a
  tap the screen cannot take, the WhatsApp card) run in Node.
- No path touches account moves, payments or reconciliations.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.equipo import can_see_team, team_rows
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, role_nav
from odoo.addons.distri_pedido.controllers.tablero import apply_user_order, staff_tiles
from odoo.addons.distri_pedido.models.reparto_v2 import PARAM_REPARTO_V2

from .test_reparto import RepartoMixin

NAV_RE = re.compile(r'<nav class="dn-nav".*?</nav>', re.S)
HREF_RE = re.compile(r'<a href="([^"]+)"')
TILE_RE = re.compile(r'class="tab-tile"[^>]*data-tile="([^"]+)"')
SHARED_CSS = "/distri_pedido/static/src/css/distri_ui.css"


def _ledger(env):
    """Counts and residual sum of everything that makes a customer balance."""
    env = env(su=True)
    lines = env["account.move.line"].search([])
    return (env["account.move"].search_count([]), env["account.payment"].search_count([]),
            env["account.partial.reconcile"].search_count([]),
            env["account.full.reconcile"].search_count([]), len(lines),
            round(sum(lines.mapped("amount_residual")), 2))


@tagged("post_install", "-at_install", "distri_v129_33")
class TestDiseno3b(RepartoMixin, HttpCase):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.gerente = user("gerente_d133", "distri_pedido.grupo_gerencia")
        cls.admin = user("admin_d133", "distri_pedido.grupo_administracion")
        cls.cajero = user("cajero_d133", "distri_pedido.grupo_cajero")
        cls.armador = user("armador_d133", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_d133", "distri_pedido.grupo_reparto")
        cls.vendedor = user("vendedor_d133", "distri_pedido.grupo_vendedor")
        cls.sistemas = user("sistemas_d133", "base.group_system")
        cls.suspendido = user("suspendido_d133", "distri_pedido.grupo_armador")
        cls.suspendido.active = False
        cls.cliente = new_test_user(cls.env, login="cliente_d133", groups="base.group_portal",
                                    **company)
        cls.roles = [cls.gerente, cls.admin, cls.cajero, cls.armador, cls.repartidor,
                     cls.vendedor]
        cls._setup_reparto()

    def setUp(self):
        super().setUp()
        self.before = _ledger(self.env)

    def tearDown(self):
        self.assertEqual(_ledger(self.env), self.before, "no customer balance moved")
        super().tearDown()

    def _flag(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _get(self, path):
        return self.url_open(path, allow_redirects=False)

    # ------------------------------------------------------------ /despacho, /cuentas

    def test_flag_off_despacho_and_cuentas_as_before(self):
        self._flag(False)
        for user in (self.admin, self.gerente, self.vendedor, self.repartidor):
            self._login(user)
            for path in ("/despacho", "/cuentas"):
                res = self._get(path)
                if res.status_code != 200:
                    continue
                text = res.text
                self.assertIn(SHARED_CSS + "?v=", text, (user.login, path))
                for absent in ("dn-legado", "/dn/dn.css", "_v2.css", "dn-bottom--rol",
                               'class="dn-nav"', "sin_historial"):
                    self.assertNotIn(absent, text, (user.login, path, absent))
                self.assertRegex(text, r"<body>\s", (user.login, path))

    def test_flag_on_despacho_and_cuentas_on_the_kit(self):
        cases = {"/despacho": ("despacho", "despacho.js?v=", "despacho_v2.css"),
                 "/cuentas": ("cuentas", "cuentas_app.js?v=", "cuentas_v2.css")}
        seen = set()
        self._flag(True)
        for user in self.roles + [self.sistemas]:
            self._login(user)
            env = self.env(user=user)
            for path, (key, script, css) in cases.items():
                res = self._get(path)
                if res.status_code != 200:
                    continue
                text = res.text
                self.assertNotIn(SHARED_CSS, text, (user.login, path))
                self.assertIn("/dn/dn.css?v=", text)
                self.assertIn("/dn/%s?v=" % css, text)
                self.assertIn(script, text, "the same app script")
                self.assertIn('<div id="app"', text)
                expected = [i["url"] for i in role_nav(env, key)]
                match = NAV_RE.search(text)
                if expected:
                    self.assertIn('class="dn-legado dn-legado--nav"', text, (user.login, path))
                    self.assertIsNotNone(match, (user.login, path))
                    self.assertEqual(HREF_RE.findall(match.group(0)), expected, (user.login, path))
                    self.assertIn('data-side=""', match.group(0), "siblings replace the page")
                else:
                    self.assertIsNone(match)
                seen.add((path, user.login))
        self.assertIn(("/despacho", self.admin.login), seen)
        self.assertIn(("/cuentas", self.vendedor.login), seen)
        self.assertIn(("/cuentas", self.repartidor.login), seen)

    def test_same_permissions_with_the_flag_on_and_off(self):
        paths = ("/despacho", "/cuentas", "/despacho/api/routes", "/cuentas/api/debtors")
        status = {}
        for on in (False, True):
            self._flag(on)
            for user in self.roles + [self.sistemas]:
                self._login(user)
                for path in paths:
                    status.setdefault((user.login, path), set()).add(self._get(path).status_code)
        for key, codes in status.items():
            self.assertEqual(len(codes), 1, "%s: %s" % (key, codes))
        self.assertEqual(status[(self.armador.login, "/despacho")], {403})
        self.assertEqual(status[(self.vendedor.login, "/despacho")], {403})
        self.assertEqual(status[(self.cajero.login, "/cuentas")], {403})

    # ------------------------------------------------------------ #74 Equipo

    def test_equipo_is_404_with_the_flag_off(self):
        self._flag(False)
        self._login(self.gerente)
        self.assertEqual(self._get("/equipo").status_code, 404)
        tiles = TILE_RE.findall(self.url_open("/tablero").text)
        self.assertNotIn("equipo", tiles)

    def test_equipo_access_by_role(self):
        self._flag(True)
        allowed = {self.gerente, self.admin, self.sistemas}
        for user in self.roles + [self.sistemas]:
            self._login(user)
            res = self._get("/equipo")
            if user in allowed:
                self.assertEqual(res.status_code, 200, user.login)
                self.assertTrue(can_see_team(user))
            else:
                self.assertEqual(res.status_code, 303, user.login)
                self.assertTrue(res.headers["Location"].endswith("/tablero"), user.login)
                self.assertFalse(can_see_team(user))
            tiles = [t["key"] for t in staff_tiles(self.env(user=user))]
            self.assertEqual("equipo" in tiles, user in allowed, user.login)
        self.assertFalse(can_see_team(self.cliente))

    def test_equipo_lists_the_team_read_only(self):
        self._flag(True)
        self.env["res.users.log"].with_user(self.vendedor).sudo().create({})
        self._login(self.admin)
        text = self._get("/equipo").text
        for user in self.roles:
            self.assertIn(user.name, text)
        self.assertIn(self.suspendido.name, text)
        self.assertIn("Suspendido", text)
        self.assertNotIn(self.cliente.name, text, "customers are not the team")
        main = re.search(r'<main class="dn-main">.*?</main>', text, re.S).group(0)
        self.assertNotIn("<form", main, "read-only")
        self.assertNotIn("<input", main, "read-only")
        self.assertNotIn("<button", main, "read-only")
        self.assertIn("1 suspendido<", text)
        rows = {r["id"]: r for r in team_rows(self.env)}
        self.assertNotIn(self.env.ref("base.user_root").id, rows)
        self.assertNotIn(self.cliente.id, rows)
        self.assertFalse(rows[self.suspendido.id]["active"])
        self.assertEqual(rows[self.gerente.id]["roles"], "Gerencia")
        self.assertRegex(rows[self.vendedor.id]["last_login"], r"^\d\d/\d\d/\d{4} \d\d:\d\d$")
        order = [r["active"] for r in team_rows(self.env)]
        self.assertEqual(order, sorted(order, reverse=True), "active first")
        # nothing on the page writes a user
        before = self.suspendido.with_context(active_test=False).read(["active", "write_date"])
        self.url_open("/equipo", data={"active": "1"})
        self.assertEqual(self.suspendido.with_context(active_test=False).read(["active", "write_date"]),
                         before)

    # ------------------------------------------------------------ #3 Ordenar el Inicio

    def _tiles(self):
        return TILE_RE.findall(self.url_open("/tablero").text)

    def _move(self, key, direction):
        return self.url_open("/tablero/ordenar", data={
            "key": key, "direction": direction, "csrf_token": self._csrf()}, allow_redirects=False)

    def _csrf(self):
        page = self.url_open("/tablero/ordenar").text
        return re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

    def test_ordering_is_off_with_the_flag_off(self):
        self._flag(False)
        self._login(self.gerente)
        default = [t["key"] for t in staff_tiles(self.env(user=self.gerente))]
        self.gerente.sudo().distri_inicio_orden = ",".join(reversed(default))
        page = self.url_open("/tablero").text
        self.assertEqual(TILE_RE.findall(page), default, "a saved order is ignored with the flag off")
        self.assertNotIn("Ordenar el Inicio", page)
        self.assertEqual(self._get("/tablero/ordenar").status_code, 404)

    def test_each_user_orders_his_inicio(self):
        self._flag(True)
        self._login(self.gerente)
        default = self._tiles()
        self.assertIn("Ordenar el Inicio", self.url_open("/tablero").text)
        self.assertGreaterEqual(len(default), 3)
        second = default[1]
        res = self._move(second, "up")
        self.assertEqual(res.status_code, 303)
        moved = self._tiles()
        self.assertEqual(moved[:2], [second, default[0]])
        self.assertEqual(sorted(moved), sorted(default), "the same tiles")
        # the first one cannot go up, the last one cannot go down
        self._move(moved[0], "up")
        self._move(moved[-1], "down")
        self.assertEqual(self._tiles(), moved)
        # by fetch: JSON with the new order
        res = self.url_open("/tablero/ordenar", data={
            "key": moved[-1], "direction": "up", "csrf_token": self._csrf()},
            headers={"X-Distri-Fetch": "1"})
        self.assertEqual(res.json()["order"][-2], moved[-1])
        # another user keeps the default order
        self._login(self.admin)
        admin_default = [t["key"] for t in staff_tiles(self.env(user=self.admin))]
        self.assertEqual(self._tiles(), admin_default)
        # «Restablecer»
        self._login(self.gerente)
        self.url_open("/tablero/ordenar", data={"reset": "1", "csrf_token": self._csrf()})
        self.assertEqual(self._tiles(), default)
        self.assertFalse(self.gerente.distri_inicio_orden)

    def test_apply_user_order(self):
        tiles = [{"key": k} for k in ("a", "b", "c", "d")]
        self.assertEqual([t["key"] for t in apply_user_order(tiles, "")], ["a", "b", "c", "d"])
        self.assertEqual([t["key"] for t in apply_user_order(tiles, "c,zz,a")], ["c", "a", "b", "d"])

    # ------------------------------------------------------------ WhatsApp (reparto_v2)

    def test_whatsapp_from_the_customer_screen(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_REPARTO_V2, "1")
        partner = self.env["res.partner"].create({"name": "X - KIOSCO WA 133", "city": "Centenario"})
        product = self.env["product.product"].create({
            "name": "Producto WA 133", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        order = self._order(partner, product, qty=2, price=100)
        route = self._route(order)
        stop = route.stop_ids
        self.authenticate("repartidor_test", "repartidor_test")
        data = self.url_open("/reparto/api/route").json()
        self.assertTrue(data["route"].get("reparto_v2"), "the app draws the WhatsApp card")
        headers = {"Content-Type": "application/json"}

        def send(number):
            event = self._event(route, "whatsapp", {"partner_id": stop.partner_id.id,
                                                    "number": number, "action": "correct"})
            return self.url_open("/reparto/api/sync", data=json.dumps({"events": [event]}),
                                 headers=headers).json()["results"][0]

        short = send("299")
        self.assertNotEqual(short["status"], "ok")
        self.assertFalse(stop.partner_id.distri_whatsapp)
        ok = send("299 4111222")
        self.assertEqual(ok["status"], "ok", ok)
        stop.partner_id.invalidate_recordset()
        self.assertIn("2994111222", re.sub(r"\D", "", stop.partner_id.distri_whatsapp))
        self.assertTrue(stop.partner_id.distri_whatsapp_confirmed)
        self.assertEqual(stop.partner_id.distri_whatsapp_user_id, self.driver)

    # ------------------------------------------------------------ app5.js (node)

    def test_app_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/reparto_v129_33.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertTrue(out["flagOffCustomerSame"], "flag off: «Ubicación y horarios» unchanged")
        self.assertTrue(out["flagOffNoWhatsapp"])
        # a saved visit opens with what was saved and is changed and saved again
        self.assertEqual(out["reopenShowsSaved"], {"pay": "cash", "cash": 1000, "cashOn": True,
                                                   "deliveredOn": True})
        self.assertEqual(out["reedit"], {"sameUuid": True, "firstCash": 1000, "cash": 0,
                                         "transfer": "yes", "amount": 1000, "receipt": "not_yet",
                                         "local": "yes"})
        # a tap the screen cannot take says why
        self.assertIn("retenido", out["retained"]["toast"])
        self.assertTrue(out["retained"]["redrawn"])
        self.assertEqual(out["closed"]["toast"],
                         "La ruta está cerrada: reabrila para corregir esta visita.")
        self.assertTrue(out["closed"]["reopen"], "«Reabrir la ruta» on the screen")
        self.assertTrue(out["closed"]["title"])
        self.assertIn("hay que reabrirla", out["closedLocked"]["toast"])
        self.assertFalse(out["closedLocked"]["reopen"])
        # the customer's WhatsApp on «Ubicación y horarios»
        self.assertTrue(all(out["waCard"].values()), out["waCard"])
        self.assertTrue(out["waEditing"])
        self.assertEqual(out["waShort"], "El número es muy corto.")
        self.assertEqual(out["waSent"], {"partner": 9, "number": "299 4111222", "action": "correct",
                                         "route": 60})
        self.assertTrue(out["waAfter"])
        self.assertIn("La ruta está cerrada", out["waClosed"])
