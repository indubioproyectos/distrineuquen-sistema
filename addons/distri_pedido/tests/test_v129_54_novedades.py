"""19.0.1.129.54 (owner, 26/11/2026): «Novedades» (controllers/novedades.py).

- Staff (any internal user) opens /novedades and its first entry, the
  «Nueva venta» selling guide; both carry «Compartir por WhatsApp» with the
  entry's absolute URL.
- A customer's portal account gets 404; a visitor (logged out, e.g. a link
  opened from WhatsApp) goes to the app's login and, after it, back to the
  same entry.
- The «Novedades» tile is on Inicio for staff, before «Capacitación»
  (which stays last), in the «Sistema» group.
Read only, synthetic users only."""
import re
from datetime import timedelta
from urllib.parse import quote

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import novedades
from odoo.addons.distri_pedido.controllers.kit import screen_parents
from odoo.addons.distri_pedido.controllers.tablero import (
    TILE_GROUPS, staff_tiles, tile_badges)

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
GUIDE = "/novedades/venta"


@tagged("post_install", "-at_install", "distri_v129_54")
class TestV12954Novedades(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.vendedor = new_test_user(cls.env, login="vendedor_nv154", name="Vendedor Nv",
                                     groups="base.group_user,distri_pedido.grupo_vendedor",
                                     **company)
        cls.interno = new_test_user(cls.env, login="interno_nv154", name="Interno Nv",
                                    groups="base.group_user", **company)
        cls.portal = new_test_user(cls.env, login="portal_nv154", name="Portal Nv",
                                   groups="base.group_portal")

    def test_staff_sees_index_and_guide(self):
        for user in (self.vendedor, self.interno):
            self.authenticate(user.login, user.login)
            res = self.url_open("/novedades")
            self.assertEqual(res.status_code, 200, user.login)
            self.assertIn('href="%s"' % GUIDE, res.text)
            self.assertIn("https://wa.me/?text=", res.text)
            res = self.url_open(GUIDE)
            self.assertEqual(res.status_code, 200, user.login)
            self.assertIn("Toda venta empieza en «Nueva venta»", res.text)
            # the two corrections applied to the poster
            self.assertIn("(solo Denis y Gonzalo)", res.text)
            self.assertNotIn("Dudas:", res.text)
            # shared with the entry's absolute URL, from web.base.url
            self.assertIn("https://wa.me/?text=", res.text)
            self.assertIn(quote(novedades.absolute_url(self.env, GUIDE), safe=""), res.text)
            self.assertIn('data-parent="/novedades"', res.text)
        self.assertEqual(self.url_open("/novedades/no-existe").status_code, 404)

    def test_portal_and_visitor_do_not_see_it(self):
        self.authenticate(self.portal.login, self.portal.login)
        for url in ("/novedades", GUIDE):
            res = self.url_open(url, allow_redirects=False)
            self.assertEqual(res.status_code, 404, url)
            self.assertNotIn("Toda venta empieza", res.text, url)
        self.authenticate(None, None)
        for url in ("/novedades", GUIDE):
            res = self.url_open(url, allow_redirects=False)
            self.assertIn(res.status_code, (302, 303), url)
            self.assertTrue(res.headers["Location"].endswith(
                "/web/login?redirect=%s" % quote(url, safe="")), res.headers["Location"])

    def test_link_from_whatsapp_logs_in_and_comes_back(self):
        self.authenticate(None, None)
        res = self.url_open(GUIDE)  # follows the redirect to the login page
        self.assertEqual(res.status_code, 200)
        token = CSRF.search(res.text)
        self.assertTrue(token, "the login form carries its token")
        self.assertIn('value="%s"' % GUIDE, res.text, "the way back is in the login form")
        res = self.url_open("/web/login", data={
            "csrf_token": token.group(1), "login": self.vendedor.login,
            "password": self.vendedor.login, "redirect": GUIDE, "type": "password"},
            allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertTrue(res.headers["Location"].endswith(GUIDE), res.headers["Location"])
        self.assertEqual(self.url_open(GUIDE).status_code, 200)

    def test_tile_for_staff_before_training(self):
        for user in (self.vendedor, self.interno):
            tiles = staff_tiles(self.env(user=user))
            keys = [t["key"] for t in tiles]
            self.assertIn("novedades", keys, user.login)
            self.assertEqual(keys[-1], "capacitacion", user.login)
            self.assertLess(keys.index("novedades"), keys.index("capacitacion"))
            tile = tiles[keys.index("novedades")]
            self.assertEqual(tile["url"], "/novedades")
        self.assertIn("novedades", dict(TILE_GROUPS)["Sistema"])
        self.authenticate(self.vendedor.login, self.vendedor.login)
        self.assertIn('href="/novedades"', self.url_open("/tablero").text)

    def test_registry_and_badge(self):
        rows = novedades.entries()
        self.assertEqual(rows[0]["slug"], "venta")
        self.assertEqual([r["date"] for r in rows], sorted((r["date"] for r in rows),
                                                           reverse=True))
        self.assertEqual(screen_parents("/novedades"), ("inicio",))
        for row in rows:
            self.assertEqual(screen_parents(row["url"]), ("/novedades",))
            self.assertTrue(self.env.ref(row["template"], raise_if_not_found=False), row)
        newest = rows[0]["date"]
        self.assertTrue(novedades.has_new(newest))
        self.assertFalse(novedades.has_new(newest + timedelta(days=novedades.NEW_DAYS + 1)))
        tiles = [t for t in staff_tiles(self.env(user=self.interno)) if t["key"] == "novedades"]
        tile_badges(self.env(user=self.interno), tiles)
        self.assertIn(tiles[0]["badge"], (None, {"text": "Nuevo", "kind": "", "n": 1}))
