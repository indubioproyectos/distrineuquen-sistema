"""19.0.1.129.42 (item 1): «Nueva venta», the customer list does not unfold
on open nor on «Cambiar»; it searches from the 2nd letter typed
(static/src/nueva_venta/nueva_venta.js, Node harness
tests/data/venta_v129_42.js). The server does not change: /pedido/clientes
answers as before. Nothing is written but the flag."""
import json
import os
import shutil
import subprocess
from unittest import SkipTest

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

PARAM_NUEVA_VENTA = "distri_pedido.nueva_venta"
HINT = "Escribí el nombre, el CUIT o el WhatsApp."


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942VentaClientes(HttpCase):

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/venta_v129_42.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["helper"], {"a": False, "ab": True, "spaces": False, "min": 2})
        self.assertEqual(out["askedOnOpen"], 0, "nothing asked on open")
        self.assertIn(HINT, out["hintOnOpen"])
        self.assertTrue(out["focusedOnOpen"], "the cursor in the search")
        self.assertEqual(out["askedOneLetter"], 0, "one letter asks nothing")
        self.assertIn(HINT, out["hintOneLetter"])
        self.assertEqual(out["askedBeforePause"], 0, "waits for the pause")
        self.assertEqual(out["askedAfterPause"], ["/pedido/clientes?q=al"])
        self.assertFalse(out["lateOldDrawn"], "a late older answer is dropped")
        self.assertTrue(out["newestDrawn"])
        self.assertEqual(out["askedOnCambiar"], 0, "«Cambiar» asks nothing")
        self.assertTrue(out["searchClearedOnCambiar"])
        self.assertIn(HINT, out["hintOnCambiar"])

    def test_page_starts_without_a_list(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_NUEVA_VENTA, "1")
        admin = new_test_user(self.env, login="admin_v142v", name="Admin V142v",
                              groups="base.group_user,distri_pedido.grupo_administracion",
                              company_id=self.env.company.id,
                              company_ids=[Command.set(self.env.company.ids)])
        self.authenticate(admin.login, admin.login)
        page = self.url_open("/nueva-venta")
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn('<div id="nv-clientes" class="nv-list" aria-live="polite"></div>', page.text)
        # item 5: the kit's small button, no local variant left
        self.assertIn("dn-btn--sm", page.text)
        self.assertNotIn("nv-btn-sm", page.text)
        self.assertIn('class="dn-back"', page.text)
