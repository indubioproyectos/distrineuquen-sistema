"""19.0.1.129.24: «Ya lo armé» posts a real CSRF token, the login forgives
case and spaces, and a deploy no longer postpones the daily audit.

- /armado rendered every form with an empty csrf_token (controllers/armado.py
  `_base` never set it): «Ya lo armé» answered 400 «CSRF validation failed»
  in production (S00237, S00239, S00300, S00306). The test takes the token
  from that very form.
- «DENIS», «Mariana» and «mariana » failed to log in: the login is stripped
  and taken in lowercase (unless a user has it exactly as typed).
- data/auditoria_cron.xml set the cron's nextcall by expression without
  noupdate: every upgrade moved it to «tomorrow».
Synthetic data only.
"""
import re
from datetime import datetime

from odoo.exceptions import AccessDenied
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import convert_file

from odoo.addons.distri_pedido.tests.test_v117_ya_armado import YaArmadoMixin

TOKEN = re.compile(r'<input[^>]*name="csrf_token"[^>]*>')


@tagged("post_install", "-at_install", "distri_v129_24")
class TestV12924ArmadoCsrf(YaArmadoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_ya_armado()

    def _form_token(self, page, url):
        form = re.search(r'<form[^>]*action="%s"[^>]*>(.*?)</form>' % re.escape(url),
                         page.text, re.S)
        self.assertTrue(form, "the «Ya lo armé» form is on the page")
        token = re.search(r'name="csrf_token" value="([^"]+)"', form.group(1))
        self.assertTrue(token, "the «Ya lo armé» form carries its own token")
        return token.group(1)

    def _check_ya_armado(self, login):
        order = self._order()
        url = "/armado/facturado/%s/ya-armado" % order.id
        self.authenticate(login, login)
        page = self.url_open("/armado")
        self.assertEqual(page.status_code, 200)
        # no form of the page posts an empty token (view and order too)
        inputs = TOKEN.findall(page.text)
        self.assertTrue(inputs)
        for tag in inputs:
            self.assertRegex(tag, r'value="[^"]+"', tag)
        token = self._form_token(page, url)
        res = self.url_open(url, data={"csrf_token": token, "bultos": "3"})
        self.assertEqual(res.status_code, 200)
        self.assertEqual(order.distri_armado, "armado")
        self.assertEqual(order.distri_bultos, 3)
        self.assertIn("quedó armado en", res.text)

    def test_packer_posts_ya_lo_arme_with_the_form_token(self):
        self._check_ya_armado("armador_v117")

    def test_administration_posts_ya_lo_arme_with_the_form_token(self):
        self._check_ya_armado("administracion_v117")


@tagged("post_install", "-at_install", "distri_v129_24")
class TestV12924Login(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.denis = new_test_user(cls.env, login="denis_v12924", password="Clave.V12924",
                                  groups="base.group_user")
        cls.mixed = new_test_user(cls.env, login="Mixto_V12924", password="Clave.Mixto",
                                  groups="base.group_user")
        cls.Users = cls.env["res.users"]

    def _auth(self, login, password):
        credential = {"type": "password", "login": login, "password": password}
        return self.Users.authenticate(credential, {"interactive": False}), credential

    def test_login_forgives_case_and_spaces(self):
        for typed in ("DENIS_V12924", "Denis_V12924", "denis_v12924 ", "  DENIS_v12924\t"):
            with self.subTest(typed=typed):
                info, credential = self._auth(typed, "Clave.V12924")
                self.assertEqual(info["uid"], self.denis.id)
                self.assertEqual(credential["login"], "denis_v12924",
                                 "the session keeps the clean login")

    def test_password_and_unknown_login_still_fail(self):
        with self.assertRaises(AccessDenied):
            self._auth("DENIS_V12924", "clave.v12924")
        with self.assertRaises(AccessDenied):
            self._auth("nadie_v12924", "Clave.V12924")

    def test_a_mixed_case_login_still_works_as_it_is(self):
        info, _credential = self._auth("Mixto_V12924 ", "Clave.Mixto")
        self.assertEqual(info["uid"], self.mixed.id)


@tagged("post_install", "-at_install", "distri_v129_24")
class TestV12924AuditCron(TransactionCase):

    def test_upgrade_keeps_the_audit_cron_nextcall(self):
        cron = self.env.ref("distri_pedido.cron_mp_audit_daily")
        sftp = self.env.ref("distri_pedido.cron_mp_sftp_fetch")
        data = self.env["ir.model.data"].search([
            ("module", "=", "distri_pedido"),
            ("name", "in", ("cron_mp_audit_daily", "cron_mp_sftp_fetch"))])
        self.assertEqual(len(data), 2)
        self.assertTrue(all(data.mapped("noupdate")), "a new database marks them noupdate")
        # like production before 19.0.1.129.24: the xmlids are not noupdate
        data.write({"noupdate": False})
        planned = datetime(2026, 11, 25, 2, 30)
        cron.nextcall = planned
        sftp.active = True
        convert_file(self.env, "distri_pedido", "data/auditoria_cron.xml", {}, mode="update")
        self.assertEqual(cron.nextcall, planned, "the upgrade keeps the planned run")
        self.assertTrue(sftp.active, "a cron turned on stays on")
