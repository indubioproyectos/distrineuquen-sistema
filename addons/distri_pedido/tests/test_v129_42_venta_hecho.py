"""19.0.1.129.42 (item 8, ui_v2 + nueva_venta): «Nueva venta»'s done page.

- «Mostrador»: the delivery page carries ?nv=1 and comes back to the done
  page (?mostrador=1), titled «Venta en mostrador», with «Entregar» while
  the depot delivery is pending and «Cobrar ahora» while its invoice has
  something to pay, plus «Nueva venta»;
- the other exits keep «Cargar otra venta»;
- flag off: the URLs and the page of 129.41.
Creating the sale writes what it always did; the done page writes nothing
(the ledger is the same before and after it)."""
import json
import re

from odoo import Command
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.remito import numerar_remitos
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942VentaHecho(RepartoMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        cls.env["website"].search([]).company_id = cls.env.company
        Pricelist = cls.env["product.pricelist"]
        if not Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1):
            Pricelist.create({"name": LISTA_GENERAL, "company_id": False,
                              "currency_id": cls.env.company.currency_id.id})
        cls.admin = new_test_user(
            cls.env, login="admin_v142h", name="Admin V142h",
            groups="base.group_user,distri_pedido.grupo_administracion",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        Tmpl = cls.env["product.template"]
        cls.p1 = Tmpl.create({"name": "Gaseosa V142", "type": "consu", "list_price": 4000.0,
                              "is_published": True, "sale_ok": True,
                              "invoice_policy": "order", "taxes_id": [Command.clear()]})
        cls.cliente = cls.env["res.partner"].create({
            "name": "Almacen Mostrador V142", "company_type": "company",
            "distri_whatsapp": "2995001142", "street": "Mitre 142", "city": "Neuquén"})
        numerar_remitos(cls.env)

    def _flags(self, ui_v2=True):
        Param = self.env["ir.config_parameter"].sudo()
        Param.set_param(PARAM_NUEVA_VENTA, "1")
        Param.set_param(PARAM_UI_V2, "1" if ui_v2 else "0")

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _sell(self, salida):
        self.authenticate(self.admin.login, self.admin.login)
        page = self.url_open("/nueva-venta")
        self.assertEqual(page.status_code, 200, page.text[:300])
        data = {"csrf_token": CSRF_RE.search(page.text).group(1),
                "envio": ENVIO_RE.search(page.text).group(1),
                "cliente_id": str(self.cliente.id),
                "renglones": json.dumps([{"id": self.p1.id, "cantidad": 2}]),
                "salida": salida, "entrega": "domicilio", "telefono": "2995001142",
                "domicilio": "Mitre 142, Neuquén"}
        res = self.url_open("/nueva-venta/crear", data=data, headers=FETCH, allow_redirects=False)
        self.assertEqual(res.status_code, 200, res.text[:500])
        self.env.invalidate_all()
        url = res.json()["url"]
        return self.env["sale.order"].browse(int(re.search(r"/(\d+)", url).group(1))), url

    def test_mostrador_next_steps(self):
        self._flags(True)
        order, url = self._sell("mostrador")
        self.assertEqual(url, "/deposito/entrega/%s?mostrador=1&nv=1" % order.id)
        entrega = self.url_open(url).text
        self.assertIn('<input type="hidden" name="nv" value="1"/>', entrega)
        before = self._ledger()
        page = self.url_open("/nueva-venta/hecho/%s?mostrador=1" % order.id).text
        self.assertIn("Venta en mostrador", page)
        self.assertNotIn("Va a Armado", page)
        self.assertIn('data-nv-paso="entregar"', page)
        self.assertIn('href="/deposito/entrega/%s?mostrador=1&amp;nv=1"' % order.id, page)
        self.assertIn('data-nv-paso="cobrar"', page)
        self.assertIn('href="/cobro?cliente=%s"' % self.cliente.id, page)
        self.assertIn("Cobrar ahora", page)
        self.assertIn('data-nv-paso="nueva"', page)
        self.assertEqual(self._ledger(), before, "the done page writes nothing")

    def test_other_exits_keep_cargar_otra_venta(self):
        self._flags(True)
        order, url = self._sell("presupuesto")
        self.assertEqual(url, "/nueva-venta/hecho/%s" % order.id)
        page = self.url_open(url).text
        self.assertIn("Cargar otra venta", page)
        self.assertNotIn("data-nv-paso", page)

    def test_flag_off_is_129_41(self):
        self._flags(False)
        order, url = self._sell("mostrador")
        self.assertEqual(url, "/deposito/entrega/%s?mostrador=1" % order.id)
        self.assertNotIn('name="nv"', self.url_open(url + "&nv=1").text, "?nv ignored")
        page = self.url_open("/nueva-venta/hecho/%s?mostrador=1" % order.id).text
        self.assertNotIn("data-nv-paso", page)
        self.assertIn("Cargar otra venta", page)
