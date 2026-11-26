"""19.0.1.129.51: «Mi correo» (confirm the e-mail with a code) and
«¿Olvidaste la contraseña?» (a single-use reset link to a confirmed
address), plus the e-mail state on «Equipo».

No e-mail ever leaves: Odoo's test mode never connects to an SMTP server
and mock_mail_gateway records what would be built/sent. Synthetic data.
"""
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.mail.tests.common import MockEmail
from odoo.addons.distri_pedido.controllers.equipo import team_rows
from odoo.addons.distri_pedido.models import correo_verificado as cv

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
CODE = re.compile(r">(\d{6})</b>")
LINK = re.compile(r'href="[^"]*(/clave/nueva\?t=[^"]+)"')
SENDER = "sistema.test@example.com"
FROM = '"Distri Neuquén" <%s>' % SENDER
NEUTRAL = "Si el usuario tiene un correo confirmado, le mandamos un enlace"
HINT = "Si no cargaste tu correo, pedí a administración que te la restablezca."


def _ledger(env):
    env.flush_all()
    env.cr.execute("SELECT count(*), coalesce(sum(debit), 0), coalesce(sum(credit), 0) "
                   "FROM account_move_line")
    return env.cr.fetchone()


@tagged("post_install", "-at_install", "distri_v129_51")
class TestV12951Codigo(TransactionCase, MockEmail):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("mail.default.from", SENDER)
        cls.user = new_test_user(cls.env, login="denis_v12951", groups="base.group_user",
                                 name="Denis Prueba")
        cls.other = new_test_user(cls.env, login="otro_v12951", groups="base.group_user",
                                  name="Otro Prueba")
        cls.ledger = _ledger(cls.env)

    def _code_from(self, mail):
        return CODE.search(mail.body_html).group(1)

    def _request(self, user, address):
        with self.mock_mail_gateway():
            record = user.with_user(user)._distri_request_code(address)
        mail = self._new_mails.filtered(lambda m: m.email_to == address.strip().lower())
        self.assertEqual(len(mail), 1)
        return record, mail

    def test_code_mail_has_explicit_sender_and_only_a_hash_is_kept(self):
        record, mail = self._request(self.user, "Denis@Example.com ")
        self.assertEqual(mail.email_from, FROM)
        self.assertEqual(record.email, "denis@example.com")
        code = self._code_from(mail)
        self.assertIn("Vence en 15 minutos", mail.body_html)
        self.assertIn("Distri Neuquén", mail.subject)
        self.assertNotEqual(record.codigo_hash, code)
        self.assertEqual(len(record.codigo_hash), 64)
        for value in record.read()[0].values():
            self.assertNotEqual(value, code)
        self.assertAlmostEqual(record.vence, fields.Datetime.now() + timedelta(minutes=15),
                               delta=timedelta(minutes=1))

    def test_sent_through_the_mail_server_with_the_sender(self):
        self.env["ir.mail_server"].sudo().create({
            "name": "test", "smtp_host": "smtp.invalid", "smtp_port": 587,
            "from_filter": SENDER})
        with self.mock_mail_gateway():
            self.user.with_user(self.user)._distri_request_code("denis@example.com")
        self.assertEqual(len(self._mails), 1, "send_after_commit sends at once in tests (mocked)")
        self.assertEqual(self._mails[0]["email_from"], FROM)
        self.assertEqual(self._mails[0]["email_to"], ["denis@example.com"])

    def test_no_sender_parameter_refuses_cleanly(self):
        self.env["ir.config_parameter"].sudo().set_param("mail.default.from", "")
        with self.mock_mail_gateway(), self.assertRaises(UserError) as err:
            self.user.with_user(self.user)._distri_request_code("denis@example.com")
        self.assertIn("correo de salida", str(err.exception))
        self.assertFalse(self._new_mails)
        self.assertFalse(self.env["distri.correo.codigo"].sudo().search(
            [("user_id", "=", self.user.id)]))

    def test_email_saved_only_after_the_right_code(self):
        self.user.partner_id.email = False
        record, mail = self._request(self.user, "denis@example.com")
        self.assertFalse(self.user.partner_id.email, "nothing saved before the code")
        self.assertFalse(self.user._distri_verified_email())
        saved = self.user.with_user(self.user)._distri_confirm_code(self._code_from(mail))
        self.assertEqual(saved, "denis@example.com")
        self.assertEqual(self.user.partner_id.email, "denis@example.com")
        self.assertEqual(self.user.sudo().distri_email_verificado, "denis@example.com")
        self.assertTrue(self.user.sudo().distri_email_verificado_fecha)
        self.assertEqual(record.estado, "usado")
        # changing it: the old one stays until the new code
        record2, mail2 = self._request(self.user, "nuevo@example.com")
        self.assertEqual(self.user._distri_verified_email(), "denis@example.com")
        self.assertEqual(self.user.partner_id.email, "denis@example.com")
        self.user.with_user(self.user)._distri_confirm_code(self._code_from(mail2))
        self.assertEqual(self.user._distri_verified_email(), "nuevo@example.com")
        self.assertEqual(_ledger(self.env), self.ledger, "no accounting change")

    def test_wrong_codes_lock_after_five(self):
        record, mail = self._request(self.user, "denis@example.com")
        good = self._code_from(mail)
        wrong = "000000" if good != "000000" else "111111"
        # not assertRaises: its savepoint would roll the counter back (the
        # controller catches the error, so the count is kept there too)
        for left in (4, 3, 2, 1, 0):
            try:
                self.user.with_user(self.user)._distri_confirm_code(wrong)
            except UserError as exc:
                message = str(exc)
            else:
                self.fail("a wrong code is refused")
            self.assertIn("Te quedan %s" % left if left else "Pedí un código nuevo", message)
        self.assertEqual(record.intentos, 5)
        self.assertEqual(record.estado, "anulado")
        with self.assertRaises(UserError) as err:
            self.user.with_user(self.user)._distri_confirm_code(good)
        self.assertIn("venció", str(err.exception))
        self.assertFalse(self.user._distri_verified_email())

    def test_expired_code_is_refused(self):
        record, mail = self._request(self.user, "denis@example.com")
        record.vence = fields.Datetime.now() - timedelta(seconds=1)
        with self.assertRaises(UserError):
            self.user.with_user(self.user)._distri_confirm_code(self._code_from(mail))
        self.assertFalse(self.user._distri_verified_email())

    def test_a_new_code_cancels_the_old_one(self):
        _record, mail = self._request(self.user, "denis@example.com")
        old = self._code_from(mail)
        _record2, mail2 = self._request(self.user, "denis@example.com")
        new = self._code_from(mail2)
        if old != new:
            with self.assertRaises(UserError):
                self.user.with_user(self.user)._distri_confirm_code(old)
        self.user.with_user(self.user)._distri_confirm_code(new)
        self.assertTrue(self.user._distri_verified_email())

    def test_three_codes_an_hour(self):
        for _i in range(3):
            self._request(self.user, "denis@example.com")
        with self.mock_mail_gateway(), self.assertRaises(UserError) as err:
            self.user.with_user(self.user)._distri_request_code("denis@example.com")
        self.assertIn("última hora", str(err.exception))
        self.assertFalse(self._new_mails)

    def test_invalid_and_taken_addresses(self):
        for bad in ("", "denis", "denis@", "a b@example.com"):
            with self.subTest(bad=bad), self.assertRaises(UserError):
                self.user.with_user(self.user)._distri_request_code(bad)
        _r, mail = self._request(self.other, "compartido@example.com")
        self.other.with_user(self.other)._distri_confirm_code(self._code_from(mail))
        with self.assertRaises(UserError) as err:
            self.user.with_user(self.user)._distri_request_code("Compartido@example.com")
        self.assertIn("otro usuario", str(err.exception))

    def test_backend_edit_of_the_address_unconfirms_it(self):
        _r, mail = self._request(self.user, "denis@example.com")
        self.user.with_user(self.user)._distri_confirm_code(self._code_from(mail))
        self.user.partner_id.email = "cualquiera@example.com"
        self.assertFalse(self.user._distri_verified_email())

    def test_fields_are_system_only(self):
        for name in ("distri_email_verificado", "distri_email_verificado_fecha"):
            self.assertEqual(self.env["res.users"]._fields[name].groups, "base.group_system")

    def test_mask(self):
        self.assertEqual(cv.mask_email("mariana@gmail.com"), "m***@gmail.com")
        self.assertEqual(cv.mask_email(""), "")


@tagged("post_install", "-at_install", "distri_v129_51")
class TestV12951Http(HttpCase, MockEmail):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("mail.default.from", SENDER)
        cls.mariana = new_test_user(cls.env, login="mariana_v12951", password="Clave.V12951",
                                    groups="base.group_user", name="Mariana Prueba")
        cls.sin_correo = new_test_user(cls.env, login="pedro_v12951", password="Clave.Pedro1",
                                       groups="base.group_user", name="Pedro Prueba")
        cls.admin_u = new_test_user(cls.env, login="admin_v12951", password="Clave.Admin1",
                                    groups="base.group_user,distri_pedido.grupo_administracion",
                                    name="Admin Prueba")
        cls.sistemas = new_test_user(cls.env, login="sis_v12951", password="Clave.Sis1234",
                                     groups="base.group_user,base.group_system",
                                     name="Sistemas Prueba")
        # Mariana confirmed hers (as «Mi correo» leaves it)
        cls.mariana.partner_id.email = "mariana.v12951@gmail.com"
        cls.mariana.sudo().write({"distri_email_verificado": "mariana.v12951@gmail.com",
                                  "distri_email_verificado_fecha": fields.Datetime.now()})
        cls.ledger = _ledger(cls.env)

    def _csrf(self, url):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200)
        return CSRF.search(page.text).group(1), page

    def _forgot(self, typed):
        self.url_open("/web/session/logout", allow_redirects=False)
        token, _page = self._csrf("/clave/olvide")
        with self.mock_mail_gateway():
            res = self.url_open("/clave/olvide", data={"csrf_token": token, "login": typed})
        self.assertEqual(res.status_code, 200)
        return res, self._new_mails

    def _can_login(self, login, password):
        self.url_open("/web/session/logout", allow_redirects=False)
        try:
            res = self.make_jsonrpc_request("/web/session/authenticate", {
                "db": self.env.cr.dbname, "login": login, "password": password})
        except Exception:
            return False
        return bool(res.get("uid"))

    def test_login_page_links_to_forgot(self):
        self.url_open("/web/session/logout", allow_redirects=False)
        page = self.url_open("/web/login")
        self.assertIn('href="/clave/olvide"', page.text)
        self.assertIn("¿Olvidaste la contraseña?", page.text)
        self.assertNotIn("Pedila a administración", page.text)
        forgot = self.url_open("/clave/olvide")
        self.assertIn('name="login"', forgot.text)
        self.assertIn("radial-gradient", forgot.text, "the login page's look")

    def test_same_neutral_answer_for_everybody(self):
        texts = []
        for typed in ("nadie_v12951", "pedro_v12951", "mariana_v12951"):
            res, mails = self._forgot(typed)
            self.assertIn(NEUTRAL, res.text)
            self.assertIn(HINT, res.text)
            texts.append(re.sub(r'value="[^"]*"', "", res.text))
            if typed == "mariana_v12951":
                self.assertEqual(len(mails), 1)
                self.assertEqual(mails.email_to, "mariana.v12951@gmail.com")
                self.assertEqual(mails.email_from, FROM)
                self.assertIn("Restablecer tu contraseña", mails.subject)
                self.assertIn("vence en 1 hora", mails.body_html)
            else:
                self.assertFalse(mails, "nothing sent for %s" % typed)
        self.assertEqual(texts[0], texts[1])
        self.assertEqual(texts[1], texts[2])

    def test_tolerant_login_and_email_lookup(self):
        for typed in (" MARIANA_V12951 ", "Mariana_v12951", "Mariana.V12951@Gmail.com"):
            with self.subTest(typed=typed):
                self.env["distri.clave.token"].sudo().search([]).unlink()
                _res, mails = self._forgot(typed)
                self.assertEqual(len(mails), 1)

    def test_link_works_once_and_sets_the_password(self):
        _res, mails = self._forgot("mariana_v12951")
        path = LINK.search(mails.body_html).group(1).replace("&amp;", "&")
        page = self.url_open(path)
        self.assertIn("mariana_v12951", page.text)
        self.assertIn('no-referrer', page.text)
        t = re.search(r'name="t" value="([^"]+)"', page.text).group(1)
        token = CSRF.search(page.text).group(1)
        # the rules of /mi-clave
        short = self.url_open("/clave/nueva", data={"csrf_token": token, "t": t,
                                                     "nueva": "corta", "repetida": "corta"})
        self.assertIn("8 caracteres", short.text)
        diff = self.url_open("/clave/nueva", data={"csrf_token": token, "t": t,
                                                    "nueva": "Nueva.Clave1", "repetida": "Otra.Clave1"})
        self.assertIn("no coinciden", diff.text)
        done = self.url_open("/clave/nueva", data={"csrf_token": token, "t": t,
                                                    "nueva": "Nueva.Clave1", "repetida": "Nueva.Clave1"})
        self.assertIn('data-clave="hecho"', done.text)
        self.assertTrue(self._can_login("mariana_v12951", "Nueva.Clave1"))
        self.assertFalse(self._can_login("mariana_v12951", "Clave.V12951"))
        again = self.url_open(path)
        self.assertIn('data-clave="muerto"', again.text)
        self.assertNotIn('name="nueva"', again.text)
        self.assertEqual(_ledger(self.env), self.ledger)

    def test_expired_link(self):
        _res, mails = self._forgot("mariana_v12951")
        path = LINK.search(mails.body_html).group(1)
        self.env["distri.clave.token"].sudo().search([("user_id", "=", self.mariana.id)]).write(
            {"vence": fields.Datetime.now() - timedelta(seconds=1)})
        self.assertIn('data-clave="muerto"', self.url_open(path).text)
        self.assertIn('data-clave="muerto"', self.url_open("/clave/nueva?t=inventado").text)

    def test_rate_limits(self):
        sent = 0
        for _i in range(7):
            _res, mails = self._forgot("mariana_v12951")
            sent += len(mails)
        self.assertEqual(sent, cv.TOKEN_PER_HOUR, "at most 3 links an hour per user")
        log = self.env["distri.clave.pedido"].sudo().search([("clave", "=", "mariana_v12951")])
        self.assertEqual(len(log), 7)
        self.assertEqual(len(log.filtered("limitado")), 7 - cv.RESET_PER_LOGIN_HOUR)
        # per IP: every request counts, whatever was typed (7 so far)
        with patch.object(cv, "RESET_PER_IP_HOUR", 7):
            self.env["distri.clave.token"].sudo().search([]).unlink()
            res, mails = self._forgot("Mariana.V12951@gmail.com")
            self.assertIn(NEUTRAL, res.text)
            self.assertFalse(mails, "over the per-IP limit nothing is sent")

    def test_mi_correo_flow_over_http(self):
        self.authenticate("pedro_v12951", "Clave.Pedro1")
        token, page = self._csrf("/mi-correo")
        self.assertIn("Todavía no confirmaste", page.text)
        with self.mock_mail_gateway():
            res = self.url_open("/mi-correo", data={"csrf_token": token, "accion": "enviar",
                                                    "email": "pedro.v12951@example.com"})
        self.assertIn('data-correo="codigo"', res.text)
        code = CODE.search(self._new_mails.body_html).group(1)
        res = self.url_open("/mi-correo", data={"csrf_token": token, "accion": "confirmar",
                                                "codigo": "000000" if code != "000000" else "111111"})
        self.assertIn("Te quedan 4", res.text)
        res = self.url_open("/mi-correo", data={"csrf_token": token, "accion": "confirmar",
                                                "codigo": "12345"})
        self.assertIn("Te quedan 3", res.text, "the wrong tries are kept between requests")
        res = self.url_open("/mi-correo", data={"csrf_token": token, "accion": "confirmar",
                                                "codigo": code})
        self.assertIn("quedó confirmado", res.text)
        self.assertIn("✓ Correo confirmado", res.text)
        self.assertIn(">Cambiar<", res.text)
        self.assertEqual(self.sin_correo.partner_id.email, "pedro.v12951@example.com")

    def test_ajustes_shows_mi_correo_for_staff_only(self):
        self.authenticate("pedro_v12951", "Clave.Pedro1")
        page = self.url_open("/mi-clave")
        self.assertIn('href="/mi-correo"', page.text)
        self.assertIn('href="/mi-clave"', page.text)
        portal = new_test_user(self.env, login="cliente_v12951", password="Clave.Cli123",
                               groups="base.group_portal")
        self.assertFalse(portal._distri_ajustes().get("correo"))
        self.authenticate("cliente_v12951", "Clave.Cli123")
        res = self.url_open("/mi-correo", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/mi-cuenta", res.headers.get("Location", ""))

    def test_equipo_shows_masked_state(self):
        rows = {r["id"]: r for r in team_rows(self.env(user=self.admin_u))}
        self.assertEqual(rows[self.mariana.id]["email"], "m***@gmail.com")
        self.assertEqual(rows[self.sin_correo.id]["email"], "")
        rows = {r["id"]: r for r in team_rows(self.env(user=self.sistemas))}
        self.assertEqual(rows[self.mariana.id]["email"], "mariana.v12951@gmail.com")
        self.env["ir.config_parameter"].sudo().set_param("distri_pedido.ui_v2", "1")
        self.authenticate("admin_v12951", "Clave.Admin1")
        text = self.url_open("/equipo").text
        self.assertIn("✓ Correo: m***@gmail.com", text)
        self.assertNotIn("mariana.v12951@gmail.com", text)
        self.assertIn("Sin correo", text)
        self.authenticate("sis_v12951", "Clave.Sis1234")
        self.assertIn("mariana.v12951@gmail.com", self.url_open("/equipo").text)
