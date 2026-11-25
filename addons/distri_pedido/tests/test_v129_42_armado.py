"""19.0.1.129.42 (item 2, ui_v2): the packer after «Terminar».

- «Terminar» (fetch and plain form) and the offline «Todo listo» (/sync)
  answer the finished order's own page (?recien=1), not the list: there,
  a big «Imprimir etiquetas» (the list banner's print action) and
  «Siguiente pedido» (the next «Para armar», same zone first);
- a double tap lands there too;
- the list has state chips (Para armar · En armado · Armados hoy) and a
  reprint link under each «Armado» card;
- flag off: /armado?terminado=<id> and the plain list, as in 129.41.
Synthetic orders. Finishing invoices the order as it always did (the same
distri_terminar); the pages themselves write nothing (ledger checked)."""
import json
import os
import re
import shutil
import subprocess
import uuid
from unittest import SkipTest

from odoo import Command
from odoo.modules.module import get_module_path
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2

FETCH = {"X-Distri-Fetch": "1"}


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Armado(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        cls.packer = new_test_user(
            cls.env, login="armador_v142a", name="Armador V142a",
            groups="base.group_user,distri_pedido.grupo_armador",
            company_id=company.id, company_ids=[Command.set(company.ids)])
        cls.packer.write({"distri_armado_vista": "tarjetas"})
        cls.partner = cls.env["res.partner"].create({"name": "Kiosco Armado V142", "city": "Centenario"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto V142", "list_price": 10, "type": "consu", "taxes_id": [Command.clear()]})
        cls.pricelist = cls.env["product.pricelist"].create({
            "name": "Lista V142", "company_id": company.id, "currency_id": company.currency_id.id})

    def _order(self, qty=2):
        order = self.env["sale.order"].create({
            "partner_id": self.partner.id, "pricelist_id": self.pricelist.id,
            "order_line": [Command.create({"product_id": self.product.id, "product_uom_qty": qty,
                                           "price_unit": 10, "tax_ids": [Command.clear()]})]})
        order.action_confirm()
        return order

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _open(self, order):
        self.authenticate(self.packer.login, self.packer.login)
        page = self.url_open("/armado/pedido/%s" % order.id)
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.csrf = re.search(r'name="csrf-token" content="([^"]+)"', page.text).group(1)
        return page

    def _finish(self, order, fetch=True):
        return self.url_open("/armado/pedido/%s/terminar" % order.id,
                             data={"csrf_token": self.csrf, "bultos": "2"},
                             headers=FETCH if fetch else None, allow_redirects=False)

    def test_finish_stays_on_the_order_with_print_and_next(self):
        self._flag(True)
        order, nxt = self._order(), self._order(qty=5)
        self._open(order)
        res = self._finish(order)
        self.assertEqual(res.json(), {"ok": True, "url": "/armado/pedido/%s?recien=1" % order.id})
        self.assertEqual(order.distri_armado, "armado")
        before = self._ledger()
        page = self.url_open("/armado/pedido/%s?recien=1" % order.id).text
        self.assertIn("Quedó armado en 2", page)
        self.assertIn('data-arm-recien="imprimir"', page)
        self.assertIn('href="/armado/pedido/%s/etiquetas"' % order.id, page)
        self.assertIn('href="/armado/pedido/%s"' % nxt.id, page, "«Siguiente pedido»")
        self.assertIn("Siguiente pedido", page)
        self.assertIn('data-arm-recien="lista"', page)
        # without ?recien the finished order is as before
        self.assertNotIn("arm-recien", self.url_open("/armado/pedido/%s" % order.id).text)
        self.assertEqual(self._ledger(), before, "the page writes nothing")
        # a double tap lands on the same page
        res = self._finish(order)
        self.assertEqual(res.json()["url"], "/armado/pedido/%s?recien=1" % order.id)
        # without JavaScript: the redirect goes there too
        other = self._order()
        self._open(other)
        res = self._finish(other, fetch=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/armado/pedido/%s?recien=1" % other.id))

    def test_last_order_says_none_left(self):
        self._flag(True)
        pending = self.env["sale.order"].sudo().search([("distri_armado", "=", "pendiente")])
        order = self._order()
        self._open(order)
        self._finish(order)
        page = self.url_open("/armado/pedido/%s?recien=1" % order.id).text
        if not (pending - order).filtered(lambda p: p._distri_para_armar()):
            self.assertIn("No quedan pedidos para armar.", page)

    def test_offline_sync_answers_the_order_page(self):
        self._flag(True)
        order = self._order()
        self._open(order)
        events = [{"uuid": str(uuid.uuid4()), "tipo": "terminar", "valor": 1}]
        res = self.url_open("/armado/pedido/%s/sync" % order.id,
                            data={"csrf_token": self.csrf, "eventos": json.dumps(events)},
                            headers=FETCH)
        data = res.json()
        self.assertEqual(data["armado"], "armado", data)
        self.assertEqual(data["url"], "/armado/pedido/%s?recien=1" % order.id)

    def test_list_chips_and_reprint(self):
        self._flag(True)
        done, todo = self._order(), self._order()
        self._open(done)
        self._finish(done)
        before = self._ledger()
        page = self.url_open("/armado").text
        for key, label in (("pendiente", "Para armar"), ("en_armado", "En armado"),
                           ("armado", "Armados hoy")):
            self.assertIn('data-arm-estado="%s"' % key, page)
            self.assertIn(label, page)
        self.assertIn('data-estado="armado"', page)
        self.assertIn('data-estado="pendiente"', page)
        self.assertIn('class="dn-btn dn-btn--sm dn-btn--ghost arm-ped__print"', page)
        self.assertIn('href="/armado/pedido/%s/etiquetas"' % done.id, page)
        self.assertNotIn('href="/armado/pedido/%s/etiquetas"' % todo.id, page)
        self.assertEqual(self._ledger(), before)

    def test_flag_off_is_the_old_flow(self):
        self._flag(False)
        order = self._order()
        self._open(order)
        res = self._finish(order)
        self.assertEqual(res.json()["url"], "/armado?terminado=%s" % order.id)
        self.assertEqual(self._finish(order).json()["url"], "/armado", "double tap as before")
        page = self.url_open("/armado?terminado=%s" % order.id).text
        self.assertIn("Imprimir etiquetas", page)
        self.assertNotIn("data-arm-estado", page)
        self.assertNotIn("arm-ped__print", page)
        self.assertNotIn("data-estado=", page)
        self.assertNotIn("arm-recien", self.url_open(
            "/armado/pedido/%s?recien=1" % order.id).text)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/armado_v129_42.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["destinoRecien"], "replace")
        for key in ("destinoLista", "destinoDejado", "destinoVacio"):
            self.assertEqual(out[key], "up", key)
        for key in ("todos", "palabra", "chip", "ambos"):
            self.assertTrue(out[key], key)
        for key in ("palabraNo", "chipNo"):
            self.assertFalse(out[key], key)
        with open(os.path.join(root, "static/src/armado/armado.js"), encoding="utf-8") as f:
            js = f.read()
        self.assertIn("irDespues(DN, j.url)", js)
        self.assertIn("location.replace(url)", js)
