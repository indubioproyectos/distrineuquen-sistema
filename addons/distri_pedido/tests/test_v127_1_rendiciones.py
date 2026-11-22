"""19.0.1.127.1: the kit page «Rendiciones y casos» (/rendiciones).

- It renders for gerencia and administración; a plain driver goes to /tablero.
- The home tile opens it, and «Reparto» stays out of «Todas las aplicaciones».
- Every row points to its screen: /caja/rendicion, /rendiciones/rutas and
  the backend menus; the routes list opens each route's backend form.
- The badges count on a small fixture (relative to the data already there).
Synthetic data only.
"""
import re

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import rendiciones, tablero
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin


def _user(env, login, name, groups):
    return new_test_user(env, login=login, name=name, groups="base.group_user," + groups,
                         company_id=env.company.id, company_ids=[Command.set(env.company.ids)])


@tagged("post_install", "-at_install", "distri_v127")
class TestRendiciones(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls.admin = _user(cls.env, "admin_rend_v1271", "Admin Rend V1271",
                          "distri_pedido.grupo_administracion")
        cls.boss = _user(cls.env, "gerencia_rend_v1271", "Gerencia Rend V1271",
                         "distri_pedido.grupo_gerencia")
        cls.product = cls.env["product.product"].create({
            "name": "Producto rendiciones V1271", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.customer = cls.env["res.partner"].create({"name": "KIOSCO RENDICIONES V1271"})

    def _menu_url(self, xmlid):
        return "/odoo?menu_id=%s" % self.env.ref(xmlid).id

    def _closed_route(self):
        route = self._route(self._order(self.customer, self.product, qty=1))
        route.sudo().write({"state": "closed"})
        return route

    def _get(self, url, login):
        self.authenticate(login, login)
        return self.url_open(url, allow_redirects=False)

    # ------------------------------------------------------------ access

    def test_renders_for_gerencia_and_administracion(self):
        for login in (self.boss.login, self.admin.login):
            res = self._get("/rendiciones", login)
            self.assertEqual(res.status_code, 200, login)
            self.assertIn("<title>Rendiciones y casos", res.text)
            self.assertIn('data-parent="/tablero"', res.text)
            self.assertIn("deposito.css", res.text, "the /deposito look")

    def test_driver_is_sent_home(self):
        for url in ("/rendiciones", "/rendiciones/rutas"):
            res = self._get(url, self.driver.login)
            self.assertEqual(res.status_code, 303, url)
            self.assertTrue(res.headers["Location"].endswith("/tablero"), url)

    # ------------------------------------------------------------ tile

    def test_tile_opens_the_page_and_reparto_stays_out_of_the_apps(self):
        env = self.env(user=self.admin)
        tiles = tablero.staff_tiles(env)
        [tile] = [t for t in tiles if t["key"] == "reparto"]
        self.assertEqual(tile["url"], "/rendiciones")
        root = self.env.ref("distri_pedido.menu_reparto_root")
        self.assertIn(root.id, tablero.tile_roots(env, tiles))
        apps = tablero.other_apps(env, tablero.tile_roots(env, tiles))
        self.assertNotIn(root.name, [a["title"] for a in apps])
        self.assertFalse([t for t in tablero.staff_tiles(self.env(user=self.driver))
                          if t["key"] == "reparto"])

    # ------------------------------------------------------------ links

    def test_links_point_to_their_screens(self):
        rows = {r["key"]: r for r in rendiciones.rows(self.env(user=self.admin))}
        self.assertEqual(list(rows), ["recibir", "rutas", "casos", "cargos", "comprobantes"])
        self.assertEqual(rows["recibir"]["url"], "/caja/rendicion")
        self.assertEqual(rows["rutas"]["url"], "/rendiciones/rutas")
        self.assertEqual(rows["casos"]["url"], self._menu_url("distri_pedido.menu_reparto_cases"))
        self.assertEqual(rows["cargos"]["url"],
                         self._menu_url("distri_pedido.menu_reparto_charges"))
        self.assertEqual(rows["comprobantes"]["url"],
                         self._menu_url("distri_pedido.menu_reparto_promises"))
        html = self._get("/rendiciones", self.admin.login).text
        for row in rows.values():
            self.assertIn('href="%s"' % row["url"], html)
        self.assertIn('href="%s"' % self._menu_url("distri_pedido.menu_reparto_zones"), html)
        self.assertIn("Ajustes de reparto (PC)", html)
        # the kit pages go down one level; the backend ones are plain links
        self.assertRegex(html, r'href="/caja/rendicion"\s+data-dn="down"')
        self.assertNotRegex(html, r'href="/odoo\?menu_id=\d+"\s+data-dn=')

    def test_routes_list_opens_each_route_form(self):
        route = self._closed_route()
        on_road = self._on_road(self._route(self._order(self.customer, self.product, qty=1)))
        res = self._get("/rendiciones/rutas", self.boss.login)
        self.assertEqual(res.status_code, 200)
        self.assertIn('data-parent="/rendiciones"', res.text)
        self.assertIn('href="/odoo/action-distri_pedido.action_route_settle/%s"' % route.id,
                      res.text)
        self.assertIn(route.name, res.text)
        self.assertIn(self.driver.name, res.text)
        self.assertNotIn("/action_route_settle/%s\"" % on_road.id, res.text)

    # ------------------------------------------------------------ badges

    def test_badge_counts(self):
        env = self.env(user=self.admin)
        before = rendiciones.counts(env)
        route = self._closed_route()
        self._closed_route()
        Case = self.env["distri.route.case"].sudo()
        Case.create({"route_id": route.id, "kind": "cash", "amount": -100})
        Case.create({"route_id": route.id, "kind": "unpaid", "amount": -50})
        Case.create({"route_id": route.id, "kind": "other", "state": "closed"})
        self.env["distri.promised.transfer"].sudo().create({
            "stop_id": route.stop_ids[0].id, "kind": "done", "amount": 100,
            "captured_at": fields.Datetime.now()})
        after = rendiciones.counts(env)
        delta = {key: after[key] - before[key] for key in after}
        self.assertEqual(delta, {"recibir": 2, "rutas": 2, "casos": 2, "cargos": 1,
                                 "comprobantes": 1})
        html = self._get("/rendiciones", self.admin.login).text
        chip = re.search(r'data-rd="casos".*?</a>', html, re.S).group(0)
        self.assertIn('class="dn-chip dn-num">%s<' % after["casos"], chip)
