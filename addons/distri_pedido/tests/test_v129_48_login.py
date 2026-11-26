"""19.0.1.129.48: the tolerant login (models/res_users.py `_login`, since
19.0.1.129.24) through the real paths a phone uses.

Production logs showed failed logins «Mariana», «mariana » (trailing space
from the phone's autocomplete) and «DENIS». res.users._login strips the
login and takes it in lowercase unless a user has it exactly as typed; the
model-level checks are in test_v129_24_armado_login_cron.py. Here the same
inputs go through the custom /web/login form (views/login.xml) and
/web/session/authenticate, and a wrong password or a different login still
fails. No stored login is changed. Synthetic data only.
"""
import re

from odoo.tests import HttpCase, new_test_user, tagged

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')


@tagged("post_install", "-at_install", "distri_v129_48")
class TestV12948Login(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.mariana = new_test_user(cls.env, login="mariana_v12948", password="Clave.V12948",
                                    groups="base.group_user", name="Mariana")

    def _form_login(self, login, password):
        self.url_open("/web/session/logout", allow_redirects=False)
        page = self.url_open("/web/login")
        self.assertEqual(page.status_code, 200)
        token = CSRF.search(page.text)
        self.assertTrue(token, "the login form carries its token")
        self.url_open("/web/login", data={"csrf_token": token.group(1), "login": login,
                                          "password": password, "redirect": "/odoo"},
                      allow_redirects=False)
        # auth="user": without a session it answers a «session expired» error
        info = self.url_open("/web/session/get_session_info", data="{}",
                             headers={"Content-Type": "application/json"}).json()
        return (info.get("result") or {}).get("uid")

    def _json_login(self, login, password):
        self.url_open("/web/session/logout", allow_redirects=False)
        res = self.make_jsonrpc_request("/web/session/authenticate", {
            "db": self.env.cr.dbname, "login": login, "password": password})
        return res.get("uid")

    def test_login_form_forgives_case_and_spaces(self):
        for typed in ("mariana_v12948", "Mariana_v12948 ", "MARIANA_V12948", " mariana_v12948"):
            with self.subTest(typed=typed):
                self.assertEqual(self._form_login(typed, "Clave.V12948"), self.mariana.id)
        self.assertEqual(self.mariana.login, "mariana_v12948", "the stored login is untouched")

    def test_login_form_still_refuses_wrong_password_and_other_logins(self):
        self.assertFalse(self._form_login("Mariana_v12948 ", "clave.v12948"))
        self.assertFalse(self._form_login("Mariano_v12948", "Clave.V12948"))
        self.assertFalse(self._form_login("mariana_v1294", "Clave.V12948"))

    def test_session_authenticate_forgives_case_and_spaces(self):
        self.assertEqual(self._json_login("MARIANA_V12948 ", "Clave.V12948"), self.mariana.id)
        with self.assertRaises(Exception):
            self._json_login("MARIANA_V12948", "wrong")
