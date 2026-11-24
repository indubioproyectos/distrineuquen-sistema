"""19.0.1.129.16 (pendiente 63): the «Buscar cliente o pedido» box of
/armado shows the search key on the phone and closes the keyboard on Enter,
like /pedido; it filters the loaded list by customer name and order number
(static/src/armado/armado.js)."""
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools.misc import file_path


@tagged("post_install", "-at_install", "distri_v129_16")
class TestV12916ArmadoBuscar(HttpCase):

    def test_search_box_renders_with_the_search_key(self):
        packer = new_test_user(self.env, login="armador_v129_16", name="Armador V129.16",
                               groups="base.group_user,distri_pedido.grupo_armador",
                               company_id=self.env.company.id,
                               company_ids=[Command.set(self.env.company.ids)])
        self.authenticate(packer.login, packer.login)
        res = self.url_open("/armado")
        self.assertEqual(res.status_code, 200)
        box = re.search(r'<input[^>]*id="arm-q"[^>]*>', res.text)
        self.assertTrue(box, "the search box is on the page")
        for attr in ('type="search"', 'enterkeyhint="search"', "data-arm-buscar",
                     'placeholder="Buscar cliente o pedido"'):
            self.assertIn(attr, box.group(0))
        with open(file_path("distri_pedido/static/src/armado/armado.js"), encoding="utf-8") as f:
            js = f.read()
        # filters by the customer's name and the order number, closes on Enter
        self.assertIn(".arm-ped__nom", js)
        self.assertIn("data-pedido", js)
        self.assertIn("input.blur();", js)
