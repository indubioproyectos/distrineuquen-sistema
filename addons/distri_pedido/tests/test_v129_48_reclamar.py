"""19.0.1.129.48 (owner, 26/11/2026): «Mis comprobantes a reclamar» as the
first block of each one's Inicio (ui_v2), and of the driver app's home
screens (reparto_v2; tests/data/reparto_v129_28.js checks the app).

- Depot (who opens /deposito): the receipts «Lo manda después» left at his
  name, with «Subir comprobante» (the existing /deposito/reclamar upload)
  and «Pedir por WhatsApp».
- Driver: his pending receipts, with «Sacar foto» (the app) and WhatsApp.
- Seller: the pending receipts of his own customers only, with the existing
  «Reclamar por WhatsApp» action and the /comprobantes upload.
- One merged block without duplicates; not drawn when there is nothing; no
  totals and nothing «a tu cargo»; with ui_v2 off, the 129.47 page.
Read-only: the ledger is compared before and after. Synthetic data only.
"""
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.tablero import RECLAMAR_MAX
from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA
from odoo.addons.distri_pedido.models.reclamar_inicio import days_ago
from odoo.addons.distri_pedido.models.reparto import _money
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.models.reparto_v2 import PARAM_REPARTO_V2
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

BLOCK_RE = re.compile(r'<section class="dn-card tab2-rec".*?</section>', re.S)
ROW_RE = re.compile(r'<li class="tab2-rec__item" data-rec="(\d+)" data-rol="(\w+)"')
LIABILITY = ("a tu cargo", "cargo a tu", "a tu nombre", "tu cuenta", "responsabilidad", "Total",
             "total")


@tagged("post_install", "-at_install", "distri_v129_48")
class TestV12948Reclamar(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        env = cls.env
        cls._setup_reparto()
        company = {"company_id": env.company.id, "company_ids": [Command.set(env.company.ids)]}

        def user(login, groups):
            return new_test_user(env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.cartera = env["res.partner.category"].create({"name": "Cartera Prueba148"})
        Partner = env["res.partner"]
        cls.mine = Partner.create({"name": "Almacen Propio148", "company_type": "company",
                                   "distri_whatsapp": "2995004801",
                                   "category_id": [Command.link(cls.cartera.id)]})
        cls.mine2 = Partner.create({"name": "Kiosco Propio148", "company_type": "company",
                                    "category_id": [Command.link(cls.cartera.id)]})
        cls.other = Partner.create({"name": "Despensa Ajena148", "company_type": "company",
                                    "distri_whatsapp": "2995004802"})
        cls.product = env["product.product"].create({
            "name": "Producto v129.48", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.vendedor = user("vendedor_r148", "distri_pedido.grupo_vendedor")
        cls.vendedor.etiqueta_cartera_id = cls.cartera
        cls.admin = user("admin_r148", "distri_pedido.grupo_administracion")
        cls.mixto = user("mixto_r148", "distri_pedido.grupo_vendedor,distri_pedido.grupo_reparto")
        cls.mixto.etiqueta_cartera_id = cls.cartera
        cls.armador = user("armador_r148", "distri_pedido.grupo_armador")
        cls.Promise = env["distri.promised.transfer"].sudo()

    def setUp(self):
        super().setUp()
        Param = self.env["ir.config_parameter"].sudo()
        for key in (PARAM_UI_V2, PARAM_NUEVA_VENTA, PARAM_REPARTO_V2):
            Param.set_param(key, "1")

    # ------------------------------------------------------------ helpers

    def _flag(self, key, on):
        self.env["ir.config_parameter"].sudo().set_param(key, "1" if on else "0")

    def _promise(self, partner, amount, driver, days=1, depot=False):
        """An open pending receipt delivered `days` ago by `driver`, on a
        route (or, `depot`, at the depot with «Lo manda después»)."""
        order = self._order(partner, self.product, qty=1, price=amount)
        Route = self.env["distri.route"].sudo()
        vals = {"driver_id": driver.id, "date": today_ar()}
        if depot:
            vals["kind"] = "depot"
        route = Route.create(vals)
        stop = route._add_orders(order)
        promise = self.Promise.create({
            "stop_id": stop.id, "kind": "done", "amount": amount,
            "captured_at": fields.Datetime.now() - timedelta(days=days)})
        if depot:
            promise.distri_user_charge = True
        return promise

    def _ledger(self):
        self.env.flush_all()
        return self.Promise._distri_ledger_fingerprint()

    def _inicio(self, user):
        self.authenticate(user.login, user.login)
        res = self.url_open("/tablero")
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.text

    def _block(self, html):
        found = BLOCK_RE.search(html)
        return found.group(0) if found else ""

    def _rows(self, html):
        return [(int(i), rol) for i, rol in ROW_RE.findall(self._block(html))]

    def _calm(self, block):
        # the claim message inside a wa.me link is the customer's text
        block = re.sub(r'href="https://wa\.me/[^"]*"', "", block)
        for word in LIABILITY:
            self.assertNotIn(word, block, word)

    # ------------------------------------------------------------ depot

    def test_depot_block_lists_lo_manda_despues_with_its_actions(self):
        promise = self._promise(self.mine, 380, self.admin, days=3, depot=True)
        before = self._ledger()
        html = self._inicio(self.admin)
        block = self._block(html)
        self.assertTrue(block, "the block is drawn")
        self.assertEqual(self._rows(html), [(promise.id, "deposito")])
        self.assertIn("Mis comprobantes a reclamar", block)
        self.assertIn(self.mine._distri_app_name(), block)
        self.assertIn(_money(380), block)
        self.assertIn("entregado hace 3 días", block)
        self.assertIn('action="/deposito/reclamar/%s/subir"' % promise.id, block)
        self.assertIn('name="comprobantes"', block)
        self.assertIn("wa.me/", block)
        self.assertIn("Pedir por WhatsApp", block)
        self._calm(block)
        # it replaces the 129.37 banner line, and goes before the tiles
        self.assertNotIn("Mis comprobantes a reclamar:", html)
        self.assertLess(html.index('class="dn-card tab2-rec"'), html.index('class="dn-tile'))
        # the depot's own list: a count, no total, nothing «a tu cargo»
        page = self.url_open("/deposito/reclamar").text
        self.assertIn("1 comprobante", page)
        self.assertNotIn("a tu cargo", page)
        self.assertNotIn("cargo a tu nombre", page)
        # it leaves by itself when the receipt arrives
        promise.state = "fulfilled"
        self.assertFalse(self._block(self._inicio(self.admin)))
        self.assertEqual(self._ledger(), before, "Inicio writes nothing")

    def test_depot_overdue_item_still_calm(self):
        promise = self._promise(self.mine, 500, self.admin, days=4, depot=True)
        self.Promise._distri_cron_expire()
        self.assertEqual(promise.state, "overdue")
        block = self._block(self._inicio(self.admin))
        self.assertIn('data-rec="%s"' % promise.id, block)
        self._calm(block)
        page = self.url_open("/deposito/reclamar").text
        self.assertIn("Vencido desde el", page)
        self.assertNotIn("cargo a tu nombre", page)

    # ------------------------------------------------------------ driver

    def test_driver_block_and_reparto_v2_off(self):
        promise = self._promise(self.mine, 1200, self.driver, days=2)
        html = self._inicio(self.driver)
        block = self._block(html)
        self.assertEqual(self._rows(html), [(promise.id, "reparto")])
        self.assertIn('href="/reparto#reclamar"', block)
        self.assertIn("Sacar foto", block)
        self.assertIn("wa.me/", block)
        self.assertIn("entregado hace 2 días", block)
        self._calm(block)
        # another driver does not see it
        self.assertFalse(self._block(self._inicio(self.other_driver)))
        # the driver part follows reparto_v2
        self._flag(PARAM_REPARTO_V2, False)
        self.assertFalse(self._block(self._inicio(self.driver)))

    # ------------------------------------------------------------ seller

    def test_seller_sees_only_his_customers(self):
        mine = self._promise(self.mine, 900, self.driver)
        no_wa = self._promise(self.mine2, 700, self.driver)
        other = self._promise(self.other, 800, self.driver)
        before = self._ledger()
        html = self._inicio(self.vendedor)
        block = self._block(html)
        self.assertEqual(sorted(self._rows(html)), sorted([(mine.id, "vendedor"),
                                                           (no_wa.id, "vendedor")]))
        self.assertNotIn(self.other._distri_app_name(), block)
        self.assertNotIn('data-rec="%s"' % other.id, block)
        self.assertIn('action="/rendiciones/comprobantes/%s/reclamar"' % mine.id, block)
        self.assertNotIn("/rendiciones/comprobantes/%s/reclamar" % no_wa.id, block,
                         "no WhatsApp button for a customer without WhatsApp")
        self.assertIn('href="/comprobantes?cliente_id=%s"' % self.mine.id, block)
        self.assertIn(_money(900), block)
        self._calm(block)
        # the existing action answers (no 403 for the seller) and only
        # records who claimed
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', block).group(1)
        res = self.url_open("/rendiciones/comprobantes/%s/reclamar" % mine.id, data={
            "csrf_token": csrf, "volver": "/rendiciones/comprobantes"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("wa.me/", res.headers["Location"])
        mine.invalidate_recordset()
        self.assertEqual(mine.reminded_by_id, self.vendedor)
        self.assertEqual(self._ledger(), before)
        # model level: the same filter
        rows = self.Promise.with_user(self.vendedor)._distri_inicio_claims(
            self.vendedor, {"vendedor"})
        self.assertEqual(sorted(r["id"] for r in rows), sorted([mine.id, no_wa.id]))

    def test_administracion_without_portfolio_gets_no_customers(self):
        self._promise(self.mine, 900, self.driver)
        self._promise(self.other, 800, self.driver)
        self.assertFalse(self._block(self._inicio(self.admin)))
        # with his own portfolio, only that one
        self.admin.etiqueta_cartera_id = self.cartera
        rows = self._rows(self._inicio(self.admin))
        self.assertEqual([r[1] for r in rows], ["vendedor"])

    # ------------------------------------------------------------ merged

    def test_several_roles_one_block_without_duplicates(self):
        # mixto drives the order of one of his own customers: one row
        both = self._promise(self.mine, 650, self.mixto, days=5)
        his = self._promise(self.mine2, 300, self.driver, days=1)
        html = self._inicio(self.mixto)
        rows = self._rows(html)
        self.assertEqual(rows, [(both.id, "reparto"), (his.id, "vendedor")], "oldest first")
        self.assertEqual(self._block(html).count('data-rec="%s"' % both.id), 1)
        self.assertIn('<span class="dn-tile__bd">2</span>', self._block(html))

    def test_more_than_the_first_rows_fold(self):
        promises = [self._promise(self.mine, 100 + i, self.driver, days=i + 1)
                    for i in range(RECLAMAR_MAX + 2)]
        block = self._block(self._inicio(self.vendedor))
        self.assertEqual(len(ROW_RE.findall(block)), len(promises))
        self.assertIn("Ver 2 más", block)
        self.assertLess(block.index('data-rec="%s"' % promises[-1].id),
                        block.index('data-rec="%s"' % promises[0].id), "oldest first")

    # ------------------------------------------------------------ empty / flag off

    def test_empty_and_flag_off(self):
        # nothing to claim: no block at all
        for user in (self.vendedor, self.admin, self.driver, self.armador):
            self.assertFalse(self._block(self._inicio(user)), user.login)
        # ui_v2 off: the 129.47 page and its banner line
        self._promise(self.mine, 380, self.admin, depot=True)
        self._flag(PARAM_UI_V2, False)
        html = self._inicio(self.admin)
        self.assertFalse(self._block(html))
        self.assertIn("Mis comprobantes a reclamar: 1 comprobante", html)

    def test_days_ago(self):
        today = today_ar()
        self.assertEqual(days_ago(today, today), "hoy")
        self.assertEqual(days_ago(today - timedelta(days=1), today), "ayer")
        self.assertEqual(days_ago(today - timedelta(days=6), today), "hace 6 días")
        self.assertEqual(days_ago(False, today), "")
