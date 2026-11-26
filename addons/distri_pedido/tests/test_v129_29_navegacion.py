"""19.0.1.129.29 («Etapa 3a · Navegación y kit mínimo»).

- #100, live for everyone: «‹ Atrás» goes UP one level. The parent of every
  staff screen is declared once (controllers/kit.py SCREEN_PARENTS); every
  chain reaches Inicio, every kit page declares its parent on <body> and the
  rendered pages say the same as the registry. The three hash apps that
  pushed history (despacho.js, cuentas_app.js, reparto/app3.js + app5.js)
  go up with history.back() or replace the entry (Node harness).
- Behind distri_pedido.ui_v2 (OFF by default): the role's bottom bar
  (kit.role_nav) on the top-level screens, and /pedido and /cobro with the
  kit's look; with the flag off nothing of it shows, and with it on the
  forms keep every field, step and post.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import kit
from odoo.addons.distri_pedido.controllers.kit import (
    HOME, NAV_ITEMS, PARAM_UI_V2, ROLES, SCREEN_LEVELS, SCREEN_PARENTS, page_nav, role_nav,
    screen_parents, staff_nav, ui_v2_on, user_role)
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles

BODY_RE = re.compile(r"<body\b[^>]*>", re.S)
PARENT_RE = re.compile(r'<body\b[^>]*\bdata-parent="([^"]*)"', re.S)
NAV_RE = re.compile(r'<nav class="dn-nav".*?</nav>', re.S)
HREF_RE = re.compile(r'<a href="([^"]+)"')
FIELD_RE = re.compile(r"<(input|select|textarea|form|button)\b([^>]*)>", re.S)
ATTR_RE = re.compile(r'\b(name|id|action|method|type|form)="([^"]*)"')


def _walk(path, seen=()):
    """Every chain of parents from path, as tuples ending at HOME or the top."""
    assert path not in seen, "cycle: %s" % (seen + (path,),)
    parents = SCREEN_PARENTS[path]
    if not parents:
        return [seen + (path,)]
    chains = []
    for parent in parents:
        if parent == HOME:
            chains.append(seen + (path, HOME))
        else:
            chains += _walk(parent, seen + (path,))
    return chains


def _fields(html):
    """(tag, name, id, action, method, type, form) of every form control, in order."""
    out = []
    for tag, attrs in FIELD_RE.findall(html):
        found = dict(ATTR_RE.findall(attrs))
        out.append((tag,) + tuple(found.get(k, "") for k in
                                  ("name", "id", "action", "method", "type", "form")))
    return out


@tagged("post_install", "-at_install", "distri_v129_29")
class TestScreenParents(TransactionCase):

    def test_every_chain_reaches_inicio(self):
        for path in SCREEN_PARENTS:
            for chain in _walk(path):
                self.assertLessEqual(len(chain), 5, chain)
                self.assertIn(chain[-1], (HOME, "/tablero"), chain)
                for step in chain[1:-1]:
                    self.assertIn(step, SCREEN_PARENTS, "%s: undeclared parent %s" % (chain, step))
        self.assertEqual(SCREEN_PARENTS["/tablero"], (), "Inicio is the top")

    def test_levels_of_the_hash_apps(self):
        for app, levels in SCREEN_LEVELS.items():
            self.assertIn(app, SCREEN_PARENTS)
            for level in levels:
                seen, cur = [], level
                while levels[cur] is not None:
                    self.assertNotIn(cur, seen, (app, level))
                    seen.append(cur)
                    cur = levels[cur]
                    self.assertIn(cur, levels, (app, level))

    def test_route_patterns(self):
        self.assertEqual(screen_parents("/caja/cerradas/12"), ("/caja/cerradas", "/caja/historial"))
        self.assertEqual(screen_parents("/armado/pedido/7"), ("/armado",))
        self.assertEqual(screen_parents("/comprobantes/mios"), ("/comprobantes",))
        self.assertEqual(screen_parents("/comprobantes/31"), ("/comprobantes", "/comprobantes/mios"))
        self.assertIsNone(screen_parents("/armado/pedido/x"))
        self.assertIsNone(screen_parents("/web/login"))

    def test_every_kit_page_declares_its_parent(self):
        """Each <body> of a kit page (dn_head) says its parent explicitly, and
        a literal parent is a declared screen."""
        views = os.path.join(get_module_path("distri_pedido"), "views")
        checked = 0
        for name in sorted(os.listdir(views)):
            text = open(os.path.join(views, name), encoding="utf-8").read()
            for template in re.split(r"<template\b", text)[1:]:
                if "distri_pedido.dn_head" not in template:
                    continue
                for body in BODY_RE.findall(template):
                    checked += 1
                    self.assertRegex(body, r"\b(t-att-)?data-parent=", "%s: %s" % (name, body))
                    self.assertRegex(body, r"\bdata-page=", "%s: %s" % (name, body))
                    literal = re.search(r'\sdata-parent="([^"]*)"', body)
                    if literal and literal.group(1):
                        self.assertIsNotNone(screen_parents(literal.group(1)),
                                             "%s: %s is not a declared screen" % (name, body))
        self.assertGreaterEqual(checked, 30, "every kit page was looked at")


@tagged("post_install", "-at_install", "distri_v129_29")
class TestNavegacion(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.gerente = user("gerente_n129", "distri_pedido.grupo_gerencia")
        cls.admin = user("admin_n129", "distri_pedido.grupo_administracion")
        cls.jefe = user("jefe_n129", "distri_pedido.grupo_gerencia,distri_pedido.grupo_cajero")
        cls.cajero = user("cajero_n129", "distri_pedido.grupo_cajero")
        cls.armador = user("armador_n129", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_n129", "distri_pedido.grupo_reparto")
        cls.vendedor = user("vendedor_n129", "distri_pedido.grupo_vendedor")
        cls.nadie = new_test_user(cls.env, login="nadie_n129", groups="base.group_user", **company)
        cls.roles = {"gerencia": cls.gerente, "administracion": cls.admin, "cajero": cls.cajero,
                     "armador": cls.armador, "repartidor": cls.repartidor,
                     "vendedor": cls.vendedor}

    def _flag(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    # ------------------------------------------------------------ #100

    def test_rendered_pages_say_their_registry_parent(self):
        self._login(self.jefe)
        paths = ["/tablero", "/armado", "/armado/faltantes", "/caja", "/caja/rendicion",
                 "/caja/cerradas", "/caja/historial", "/comprobantes", "/comprobantes/mios",
                 "/revisar", "/rutas", "/agilpedido", "/agilpedido/cargar", "/deposito",
                 "/deposito/mostrador", "/rendiciones", "/rendiciones/rutas", "/mis-ventas"]
        shown = []
        for path in paths:
            res = self.url_open(path, allow_redirects=False)
            if res.status_code != 200:
                continue
            match = PARENT_RE.search(res.text)
            self.assertIsNotNone(match, "%s declares no parent" % path)
            parent = match.group(1)
            resolved = () if not parent else (HOME if parent == "/tablero" and path != "/tablero"
                                              else parent)
            expected = screen_parents(path)
            if expected == ():
                self.assertEqual(resolved, (), path)
            else:
                self.assertIn(resolved, expected, "%s says %s" % (path, parent))
            shown.append(path)
        for core in ("/tablero", "/armado", "/caja", "/comprobantes", "/revisar", "/mis-ventas"):
            self.assertIn(core, shown, "%s was not checked" % core)

    def test_cobro_goes_back_to_its_parent(self):
        self._login(self.admin)
        page = self.url_open("/cobro").text
        back = re.search(r'<a class="back"[^>]*\bhref="([^"]+)"', page).group(1)
        self.assertIn(HOME if back == "/tablero" else back, SCREEN_PARENTS["/cobro"])

    def test_hash_apps_go_up_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/navegacion_v129_29.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        # one press of «Atrás» (or the phone back) leaves the list again
        for key in ("despacho_back_from_opened_route", "despacho_twice",
                    "despacho_back_from_direct_route", "cuentas_row_then_debtors",
                    "cuentas_direct_account", "reparto_back_from_visit", "reparto_two_levels",
                    "reparto_go_after_saving", "reparto_home_is_the_list"):
            self.assertEqual(out[key]["leave"], 1, key)
        self.assertEqual(out["despacho_back_from_opened_route"]["hash"], "")
        self.assertEqual(out["despacho_back_from_direct_route"]["entries"], 1, "replaced, not pushed")
        self.assertEqual(out["cuentas_row_then_debtors"]["row"], {"hash": "#cuenta/12", "index": 1})
        self.assertTrue(out["cuentas_row_then_debtors"]["prevented"])
        self.assertEqual(out["cuentas_transfers"], {"hash": "#transferencias", "leave": 2})
        self.assertFalse(out["cuentas_other_links"]["prevented"], "other links are left alone")
        self.assertEqual(out["reparto_two_levels"]["hash"], "#visitas")
        self.assertEqual(out["reparto_go_after_saving"]["hash"], "#visitas")
        self.assertEqual(out["reparto_forward_go"], {"hash": "#salida", "entries": 2},
                         "a forward step still adds its entry")
        # 19.0.1.129.48: the «Cambiar el orden» screen (and its back link) is gone
        self.assertEqual(out["app5_back_links"], 2, "app5.js uses the same back links")

    # ------------------------------------------------------------ ui_v2 off

    def test_flag_is_off_by_default_and_changes_nothing(self):
        self.assertFalse(ui_v2_on(self.env))
        env = self.env(user=self.admin)
        self.assertEqual(page_nav(env, "revisar"), [])
        # the packer's bar of before: Inicio, Pedidos, Faltantes (+ Despacho)
        keys = [i["key"] for i in staff_nav(env, "armado")]
        self.assertEqual(keys[:3], ["inicio", "armado", "faltantes"])
        self._login(self.jefe)
        for path in ("/revisar", "/comprobantes", "/caja", "/mis-ventas", "/rendiciones", "/cobro"):
            text = self.url_open(path).text
            self.assertNotIn('class="dn-nav"', text, path)
            self.assertNotIn("dn-bottom--rol", text, path)
        cobro = self.url_open("/cobro").text
        self.assertNotIn("dn-legado", cobro)
        self.assertNotIn("cobro_v2.css", cobro)
        pedido = self.url_open("/pedido").text
        self.assertNotIn("dn-pedido-v2", pedido)
        self.assertNotIn("pedido_v2.css", pedido)

    # ------------------------------------------------------------ role bars

    def test_each_role_gets_exactly_its_screens(self):
        self._flag(True)
        role_keys = {role: keys for role, _group, keys in ROLES}
        for role, user in self.roles.items():
            env = self.env(user=user)
            self.assertEqual(user_role(user), role, user.login)
            visible = {t["key"] for t in staff_tiles(env) if t["url"]}
            if "armado" in visible:
                visible.add("faltantes")
            keys = [i["key"] for i in role_nav(env, "")]
            self.assertEqual(keys[0], "inicio", role)
            self.assertEqual(keys[1:], [k for k in role_keys[role] if k in visible], role)
            self.assertLessEqual(len(keys), kit.NAV_MAX, role)
            self.assertTrue(keys[1:], "%s has screens in its bar" % role)
            # staff_nav (the packing screens) is the same bar with the flag on
            self.assertEqual([i["key"] for i in staff_nav(env, "")], keys, role)
        self.assertEqual(role_nav(self.env(user=self.nadie), ""), [], "no role, no bar")
        self.assertEqual(user_role(self.jefe), "gerencia", "the first role wins")
        self.assertIn("pedido", [i["key"] for i in role_nav(self.env(user=self.vendedor), "")])
        self.assertIn("mireparto", [i["key"] for i in role_nav(self.env(user=self.repartidor), "")])
        self.assertEqual([i["key"] for i in role_nav(self.env(user=self.cajero), "")][:3],
                         ["inicio", "caja", "cobro"])

    def test_a_screen_outside_the_bar_is_shown_marked(self):
        self._flag(True)
        env = self.env(user=self.admin)
        items = role_nav(env, "faltantes")
        keys = [i["key"] for i in items]
        # 19.0.1.129.42: the screen's role first (the packer's bar: /armado's
        # tabs together), then the user's own screens that fit
        self.assertEqual(keys[1:4], ["armado", "faltantes", "despacho"],
                         "/armado's tabs travel together")
        self.assertEqual([i["key"] for i in items if i["on"]], ["faltantes"])
        self.assertLessEqual(len(keys), kit.NAV_MAX)

    def test_the_bar_is_the_same_on_every_top_level_screen(self):
        self._flag(True)
        user = self.gerente
        self._login(user)
        env = self.env(user=user)
        base = [i["url"] for i in role_nav(env, "")]
        pages = {"/revisar": "revisar", "/mis-ventas": "misventas", "/comprobantes": "comprobantes",
                 "/rendiciones": "reparto", "/agilpedido": "agilpedido", "/cobro": "cobro",
                 "/armado/faltantes": "faltantes"}
        seen = 0
        for path, key in pages.items():
            res = self.url_open(path, allow_redirects=False)
            if res.status_code != 200:
                continue
            match = NAV_RE.search(res.text)
            self.assertIsNotNone(match, "%s has the role's bar" % path)
            hrefs = HREF_RE.findall(match.group(0))
            self.assertEqual(hrefs[0], "/tablero", path)
            if key in [k for _r, _g, keys in ROLES if _r == "gerencia" for k in keys]:
                self.assertEqual(hrefs, base, path)
            else:
                own = [i["url"] for i in role_nav(env, key)]
                self.assertEqual(hrefs, own, path)
                # 19.0.1.129.42: there, the bar of the screen's role (nav_keys)
                if key in NAV_ITEMS:
                    self.assertIn(path, hrefs, path)
                else:
                    self.assertEqual(hrefs, base, path)
            seen += 1
        self.assertGreaterEqual(seen, 5)
        self.assertTrue(set(NAV_ITEMS) >= {k for _r, _g, keys in ROLES for k in keys})

    # ------------------------------------------------------------ /cobro and /pedido

    def test_cobro_on_the_kit_keeps_every_field(self):
        self._login(self.admin)
        partner = self.env["res.partner"].create({"name": "Kiosco Navegacion 129"})
        urls = ["/cobro", "/cobro?q=Navegacion", "/cobro?cliente=%d" % partner.id,
                "/cobro?nuevo=1&q=Kiosco"]
        before = {url: self.url_open(url).text for url in urls}
        self._flag(True)
        for url in urls:
            after = self.url_open(url).text
            self.assertIn('class="dn-legado"', after, url)
            self.assertIn("/dn/dn.css", after, url)
            self.assertIn("/dn/cobro_v2.css", after, url)
            self.assertNotIn("/dn/dn.js", after, "the page keeps sin_historial.js")
            self.assertIn("sin_historial", after)
            self.assertEqual(_fields(after), _fields(before[url]), url)
            self.assertIn('class="dn-nav"', after, "the role's bar")
            # no dn.js here: sin_historial.js replaces the page for a sibling
            self.assertRegex(after, r'href="/tablero" data-dn="up" data-up=""')

    def test_pedido_on_the_kit_keeps_every_step_and_field(self):
        # a public customer
        self.authenticate(None, None)
        before = self.url_open("/pedido").text
        self._flag(True)
        after = self.url_open("/pedido").text
        self.assertIn('class="dn-pedido-v2"', after)
        self.assertIn("/dn/pedido_v2.css", after)
        self.assertNotIn('class="dn-nav"', after, "customers get no staff bar")
        self.assertEqual(_fields(after), _fields(before))
        for step in ('id="barra"', 'id="p2"', 'id="p3"', 'action="/pedido/enviar"'):
            self.assertIn(step, after)
        # the staff: same fields, plus the role's bar under the order bar
        self._flag(False)
        self._login(self.vendedor)
        before = self.url_open("/pedido").text
        self._flag(True)
        after = self.url_open("/pedido").text
        self.assertIn('class="dn-pedido-v2 dn-con-nav"', after)
        self.assertEqual(_fields(after), _fields(before))
        bar = re.search(r'<div class="barra vacia" id="barra">.*?</nav>', after, re.S)
        self.assertIsNotNone(bar, "the bar is inside the order bar")
        self.assertIn('href="/pedido"', bar.group(0))
        self.assertIn('data-side=""', bar.group(0))
