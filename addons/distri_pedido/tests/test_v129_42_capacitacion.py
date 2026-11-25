"""19.0.1.129.42 (item 3): «Capacitación».

(a) /proyecto shows each role only its own training modules (from the
    user's groups, on the server); administración, gerencia and Sistemas see
    all; a user with one role gets it opened directly. Flag off: all.
(b) «Ir a practicar» with a short-lived signed pass (controllers/
    practica_pase.py): HMAC over the roles and the expiry, 10 minutes, a
    nonce; no key = the plain practice link as before. The pass carries no
    name or login, and production never holds the practice code.
Nothing is written but the flag and the key parameter."""
import time
from urllib.parse import parse_qs, unquote, urlparse

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import practica_pase as pp
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.controllers.proyecto import (
    PRACTICE_URL, practice_roles, training_roles)

KEY = "k" * 20 + "-test-key-v129-42-0123456789"


@tagged("post_install", "-at_install", "distri_v129_42")
class TestV12942Capacitacion(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.armador = user("armador_v142c", "distri_pedido.grupo_armador")
        cls.vendedor = user("vendedor_v142c", "distri_pedido.grupo_vendedor")
        cls.cajero = user("cajero_v142c", "distri_pedido.grupo_cajero,distri_pedido.grupo_reparto")
        cls.admin = user("admin_v142c", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v142c", "distri_pedido.grupo_gerencia")
        cls.sistemas = user("sistemas_v142c", "base.group_system")

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _key(self, value):
        self.env["ir.config_parameter"].sudo().set_param(pp.PARAM_KEY, value)

    # ------------------------------------------------------------ (a)

    def test_training_roles_by_group(self):
        self.assertEqual(training_roles(self.env(user=self.armador)), ["armado"])
        self.assertEqual(training_roles(self.env(user=self.vendedor)), ["vendedores"])
        self.assertEqual(training_roles(self.env(user=self.cajero)), ["reparto", "caja"])
        everything = ["armado", "reparto", "deposito", "caja", "administracion", "vendedores"]
        for user in (self.admin, self.gerente, self.sistemas):
            self.assertEqual(training_roles(self.env(user=user)), everything, user.login)

    def test_proyecto_shows_only_the_users_modules(self):
        self._flag(True)
        self.authenticate(self.armador.login, self.armador.login)
        page = self.url_open("/proyecto").text
        self.assertIn('id="rol-armado"', page)
        for other in ("reparto", "deposito", "caja", "administracion", "vendedores"):
            self.assertNotIn('id="rol-%s"' % other, page, other)
            self.assertNotIn('data-rol="%s"' % other, page, other)
        self.assertIn('data-unico="armado"', page, "one role: opened directly")
        self.authenticate(self.cajero.login, self.cajero.login)
        page = self.url_open("/proyecto").text
        self.assertIn('id="rol-caja"', page)
        self.assertIn('id="rol-reparto"', page)
        self.assertNotIn('id="rol-administracion"', page)
        self.assertNotIn("data-unico", page)
        self.authenticate(self.gerente.login, self.gerente.login)
        page = self.url_open("/proyecto").text
        for role in ("armado", "reparto", "deposito", "caja", "administracion", "vendedores"):
            self.assertIn('id="rol-%s"' % role, page, role)

    def test_flag_off_shows_every_module(self):
        self._flag(False)
        self.authenticate(self.armador.login, self.armador.login)
        page = self.url_open("/proyecto").text
        for role in ("armado", "reparto", "deposito", "caja", "administracion", "vendedores"):
            self.assertIn('id="rol-%s"' % role, page, role)
        self.assertNotIn("data-unico", page)
        self.assertIn('href="%s"' % PRACTICE_URL, page)

    def test_practice_roles(self):
        self.assertEqual(practice_roles(self.armador), ["armador"])
        self.assertEqual(practice_roles(self.cajero), ["cajero", "repartidor"])
        self.assertEqual(practice_roles(self.admin), ["administracion", "deposito"])
        self.assertEqual(practice_roles(self.gerente), ["gerencia"])
        self.assertEqual(practice_roles(self.sistemas), [])

    # ------------------------------------------------------------ (b)

    def test_pass_signature_expiry_and_tampering(self):
        now = int(time.time())
        token = pp.make_pass(KEY, ["cajero", "repartidor", "intruso"], now=now)
        data = pp.verify_pass(KEY, token, now=now)
        self.assertEqual(data["roles"], ["cajero", "repartidor"], "unknown keys never travel")
        self.assertEqual(data["expiry"], now + pp.PASS_TTL)
        self.assertEqual(pp.PASS_TTL, 600)
        self.assertIsNone(pp.verify_pass(KEY, token, now=now + pp.PASS_TTL + 1), "expired")
        self.assertIsNone(pp.verify_pass("x" * 40, token, now=now), "another key")
        self.assertIsNone(pp.verify_pass("", token, now=now), "no key")
        forged = token.replace("cajero-repartidor", "gerencia")
        self.assertIsNone(pp.verify_pass(KEY, forged, now=now), "roles are signed")
        later = pp.make_pass(KEY, ["armador"], now=now + 3600)
        self.assertIsNone(pp.verify_pass(KEY, later, now=now), "too far ahead")
        self.assertIsNone(pp.verify_pass(KEY, token + "0", now=now))
        self.assertIsNone(pp.verify_pass(KEY, "1.a.b.c", now=now))
        self.assertIsNone(pp.verify_pass(KEY, "x" * 500, now=now))
        with self.assertRaises(ValueError):
            pp.make_pass("short", ["armador"])
        empty = pp.make_pass(KEY, [], now=now)
        self.assertEqual(pp.verify_pass(KEY, empty, now=now)["roles"], [])
        # nothing personal travels: version, roles, expiry, nonce, signature
        self.assertEqual(len(token.split(".")), 5)

    def test_practicar_link_without_key_is_the_plain_url(self):
        self._flag(True)
        self._key("")
        self.authenticate(self.armador.login, self.armador.login)
        page = self.url_open("/proyecto").text
        self.assertIn('href="%s"' % PRACTICE_URL, page)
        self.assertNotIn('href="/proyecto/practicar"', page)
        res = self.url_open("/proyecto/practicar", allow_redirects=False)
        self.assertEqual(res.headers["Location"], PRACTICE_URL)

    def test_practicar_link_with_key_carries_a_valid_pass(self):
        self._flag(True)
        self._key(KEY)
        self.authenticate(self.cajero.login, self.cajero.login)
        page = self.url_open("/proyecto").text
        self.assertIn('href="/proyecto/practicar"', page)
        res = self.url_open("/proyecto/practicar", allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        location = res.headers["Location"]
        self.assertTrue(location.startswith(PRACTICE_URL + pp.PASS_PATH + "?t="), location)
        self.assertEqual(res.headers["Cache-Control"], "no-store")
        self.assertEqual(res.headers["Referrer-Policy"], "no-referrer")
        token = unquote(parse_qs(urlparse(location).query)["t"][0])
        data = pp.verify_pass(KEY, token)
        self.assertEqual(data["roles"], ["cajero", "repartidor"])
        self.assertNotIn(self.cajero.login, location)
        # flag off: the plain link, even with the key
        self._flag(False)
        res = self.url_open("/proyecto/practicar", allow_redirects=False)
        self.assertEqual(res.headers["Location"], PRACTICE_URL)

    def test_practice_site_never_signs(self):
        self._flag(True)
        self._key(KEY)
        self.env["ir.config_parameter"].sudo().set_param("distri_practica.activo", "1")
        self.assertFalse(pp.pass_enabled(self.env))
        self.authenticate(self.armador.login, self.armador.login)
        self.assertIn('href="/"', self.url_open("/proyecto").text)
