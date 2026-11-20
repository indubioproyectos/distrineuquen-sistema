"""19.0.1.114.0: /proyecto is «Capacitación Distri Neuquén», the staff
training page approved by the owner, with the former project hub collapsed
in «Historia del proyecto»."""
import html
import os
import re

from odoo.tests import HttpCase, new_test_user, tagged

MODULE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ROLES = ("armado", "reparto", "deposito", "caja", "administracion", "vendedores")

# Button labels quoted on the page (<span class="k"> / <span class="k2">)
# that are composed at run time and so never appear literally in the
# module's templates, scripts or Python. Keep it short; each entry says
# where the label is built. Empty today: every quoted label is written
# literally somewhere in the module.
DYNAMIC_LABELS = {
    # "Label as shown": "file and function that composes it",
}
# Files that are the page itself (they would trivially "contain" every label)
# and folders that are not screens.
SKIP_FILES = {os.path.join("views", "proyecto_paginas.xml")}
SKIP_DIRS = {"tests", "migrations", "__pycache__", os.path.join("static", "src", "proyecto")}


def module_sources():
    """Text of the module's templates, scripts and Python, entities decoded."""
    chunks = []
    for root, dirs, files in os.walk(MODULE_DIR):
        rel_root = os.path.relpath(root, MODULE_DIR)
        dirs[:] = [d for d in dirs if os.path.normpath(os.path.join(rel_root, d)) not in SKIP_DIRS]
        for name in files:
            rel = os.path.normpath(os.path.join(rel_root, name))
            if rel in SKIP_FILES or not name.endswith((".xml", ".js", ".py")):
                continue
            with open(os.path.join(root, name), encoding="utf-8") as handle:
                text = handle.read()
            # JS escapes of the usual typographic characters
            text = re.sub(r"\\u([0-9a-fA-F]{4})", lambda m: chr(int(m.group(1), 16)), text)
            chunks.append(html.unescape(text))
    return "\n".join(chunks)


@tagged("post_install", "-at_install")
class TestCapacitacion(HttpCase):

    def setUp(self):
        super().setUp()
        # 19.0.1.119.0: /proyecto is for the staff
        staff = new_test_user(self.env, login="staff_capacitacion", groups="base.group_user")
        self.authenticate(staff.login, staff.login)

    def _page(self):
        response = self.url_open("/proyecto")
        self.assertEqual(response.status_code, 200)
        return response

    def test_public_page_renders_standalone_and_noindex(self):
        response = self._page()
        body = response.text
        self.assertIn("<title>Capacitación Distri Neuquén</title>", body)
        self.assertIn('name="robots" content="noindex,nofollow"', body)
        self.assertIn("noindex", response.headers.get("X-Robots-Tag", ""))
        # no website layout, assets through the kit route, no external fonts/CDNs
        self.assertNotIn("o_main_navbar", body)
        self.assertNotIn("/web/assets/", body)
        self.assertRegex(body, r'href="/dn/proyecto\.css\?v=[0-9a-f]{12}"')
        self.assertRegex(body, r'src="/dn/proyecto\.js\?v=[0-9a-f]{12}"')
        self.assertNotIn("fonts.googleapis.com", body)
        for name in ("proyecto.css", "proyecto.js"):
            asset = self.url_open("/dn/%s" % name)
            self.assertEqual(asset.status_code, 200, name)
        self.assertNotIn("googleapis", self.url_open("/dn/proyecto.css").text)

    def test_role_picker_phase_one_and_cycle(self):
        body = self._page().text
        for role in ROLES:
            self.assertIn('href="#%s"' % role, body, role)
            self.assertIn('id="rol-%s"' % role, body, role)
            self.assertIn('data-check="%s"' % role, body, role)
        # Gerencia was removed on purpose
        self.assertNotIn('href="#gerencia"', body)
        self.assertIn("Fase 1: el sistema interno.", body)
        self.assertIn("siguen entrando por AgilPedido", body)
        self.assertIn("Si pasa esto…", body)
        self.assertIn("Ya lo sé hacer", body)
        self.assertIn('id="circuito"', body)
        self.assertIn("Un pedido, de la entrada a la cuenta", body)

    def test_practice_link_without_access_code(self):
        # the practice database keeps its code in this parameter; even if it
        # existed here, the public page must never print it
        sentinel = "codigo-centinela-v114"
        self.env["ir.config_parameter"].sudo().set_param("distri_practica.codigo", sentinel)
        body = self._page().text
        self.assertIn('href="https://practica.distrineuquen.com"', body)
        self.assertIn("poné el código que te pasa administración", body)
        self.assertNotIn(sentinel, body)
        self.assertNotIn("distri_practica", body)

    def test_history_lists_project_modules(self):
        body = self._page().text
        self.assertIn('id="historia"', body)
        self.assertIn("Historia del proyecto", body)
        self.assertRegex(body, r'<details class="historia">')
        text = html.unescape(body)
        rows = self.env["distri.project.module"].project_rows()
        self.assertTrue(rows)
        for row in rows:
            self.assertIn(row["name"], text, row["code"])
            self.assertIn('id="parte-%s"' % row["code"], body)
        self.assertIn("/presentacion/migracion", body)
        self.assertIn("Despacho con doble control de carga", body)
        # a module edited in the backend shows on the next visit
        module = self.env.ref("distri_pedido.project_stock")
        module.next_step = "Paso de prueba v114"
        self.assertIn("Paso de prueba v114", self._page().text)

    def test_quoted_button_labels_exist_in_the_module(self):
        body = self._page().text
        labels = {html.unescape(m) for m in re.findall(r'<span class="k2?">(.*?)</span>', body)}
        self.assertGreater(len(labels), 50)
        sources = module_sources()
        missing = sorted(label for label in labels
                         if label not in sources and label not in DYNAMIC_LABELS)
        self.assertFalse(missing, "Labels quoted on /proyecto that no screen has: %s" % missing)
