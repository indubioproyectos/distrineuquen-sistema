"""19.0.1.129.51: each staff member confirms his own e-mail, and a forgotten
password is reset through it.

- «Mi correo» (controllers/correo.py /mi-correo, linked from the «Ajustes»
  sheet next to «Cambiar clave»): the user types an address, the system
  e-mails a 6-digit code (valid CODE_MINUTES, CODE_MAX_TRIES wrong tries,
  at most CODE_PER_HOUR codes an hour) and only a right code saves it on
  the user's partner (res.partner.email) and in
  res.users.distri_email_verificado. Until then the old address stays.
  Only a keyed hash of the code is stored.
- «¿Olvidaste la contraseña?» (/clave/olvide, /clave/nueva): a user with a
  confirmed address gets a single-use link (TOKEN_MINUTES) to set a new
  password with the rules of /mi-clave. Every request gets the same
  neutral answer (no user enumeration) and is rate limited per typed
  login and per IP.

The address counts as confirmed only while the partner's e-mail is still
the confirmed one: if somebody edits it in the backend, it is no longer
confirmed (no reset link goes to an address nobody confirmed).

Every e-mail is created with an explicit sender «Distri Neuquén
<mail.default.from>»: in Odoo 19 a mail.mail without email_from fails
(NO_FOUND_FROM) and the company has no e-mail. Without that parameter
nothing is sent and the user reads a clear message. Portal customers are
out of scope: their flows are unchanged. Nothing here touches accounting.
"""
import hashlib
import hmac
import secrets
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.fields import Domain
from odoo.tools import email_normalize, formataddr

CODE_MINUTES = 15
CODE_MAX_TRIES = 5
CODE_PER_HOUR = 3
TOKEN_MINUTES = 60
TOKEN_PER_HOUR = 3
RESET_PER_LOGIN_HOUR = 5
RESET_PER_IP_HOUR = 20
SENDER_NAME = "Distri Neuquén"
NO_SENDER = ("El sistema todavía no tiene configurado el correo de salida. "
             "Avisale a administración.")
MIN_PASSWORD = 8


def mask_email(address):
    """«m***@gmail.com»: the first letter and the domain."""
    if not address or "@" not in address:
        return ""
    local, _, domain = address.partition("@")
    return "%s***@%s" % (local[:1], domain)


def password_problem(new, repeated):
    """The rules of /mi-clave (controllers/main.py mi_clave), or ""."""
    if len(new) < MIN_PASSWORD:
        return "La contraseña nueva tiene que tener 8 caracteres o más."
    if new != repeated:
        return "Las dos contraseñas nuevas no coinciden."
    return ""


def _sender(env):
    address = (env["ir.config_parameter"].sudo().get_param("mail.default.from") or "").strip()
    if "@" not in address or not email_normalize(address):
        raise UserError(NO_SENDER)
    return formataddr((SENDER_NAME, email_normalize(address)))


def _body(paragraphs):
    """A short branded HTML body; paragraphs are already safe Markup."""
    inner = Markup("").join(
        Markup('<p style="margin:0 0 12px">%s</p>') % p for p in paragraphs)
    return Markup(
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:15px;color:#1f2a37;'
        'max-width:480px">'
        '<p style="margin:0 0 16px;font-size:18px;font-weight:bold;color:#10243f">'
        'Distri Neuquén</p>%s'
        '<p style="margin:16px 0 0;font-size:12px;color:#6b7785">Mensaje automático, '
        'no hace falta responderlo.</p></div>') % inner


def send_mail(env, email_to, subject, paragraphs, now=False):
    """Queue one e-mail with the explicit sender. now=True sends it right
    after the commit, but only when an outgoing mail server is active
    (practice has none: its mails stay queued). Otherwise the mail queue
    cron sends it (triggered here)."""
    mail = env["mail.mail"].sudo().create({
        "email_from": _sender(env),
        "email_to": email_to,
        "subject": subject,
        "body_html": _body(paragraphs),
        "auto_delete": True,
    })
    if now and env["ir.mail_server"].sudo().search_count([("active", "=", True)], limit=1):
        mail.send_after_commit()
    else:
        cron = env.ref("mail.ir_cron_mail_scheduler_action", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()
    return mail


def _keyed_hash(env, value):
    secret = env["ir.config_parameter"].sudo().get_param("database.secret") or ""
    return hmac.new(secret.encode(), value.encode(), hashlib.sha256).hexdigest()


class ResUsersCorreo(models.Model):
    _inherit = "res.users"

    # the confirmed address (lowercase) and when; system only, read with sudo
    distri_email_verificado = fields.Char(
        string="Correo confirmado", copy=False, groups="base.group_system",
        help="El correo que el usuario confirmó con un código (Ajustes › Mi correo). "
             "Sirve para restablecer la contraseña.")
    distri_email_verificado_fecha = fields.Datetime(
        string="Correo confirmado el", copy=False, groups="base.group_system")

    def _distri_verified_email(self):
        """The confirmed address while it is still the partner's e-mail, or ""."""
        self.ensure_one()
        user = self.sudo()
        if user.share or not user.distri_email_verificado:
            return ""
        if (email_normalize(user.partner_id.email or "") or "") != user.distri_email_verificado:
            return ""
        return user.distri_email_verificado

    def _distri_pending_code(self):
        """The live code of this user (not used, not expired, tries left)."""
        self.ensure_one()
        return self.env["distri.correo.codigo"].sudo().search([
            ("user_id", "=", self.id), ("estado", "=", "pendiente"),
            ("vence", ">", fields.Datetime.now()),
            ("intentos", "<", CODE_MAX_TRIES)], order="id desc", limit=1)

    def _distri_request_code(self, address):
        """Send a confirmation code to `address`. Returns the code record;
        raises UserError with the message for the user."""
        self.ensure_one()
        user = self.sudo()
        if user.share or user._is_public():
            raise UserError("Solo para el equipo.")
        typed = (address or "").strip()
        email = email_normalize(typed)
        if not email or len(typed) > 254 or any(ch.isspace() for ch in typed):
            raise UserError("Ese correo no parece válido. Revisalo.")
        if email == user._distri_verified_email():
            raise UserError("Ese ya es tu correo confirmado.")
        others = self.env["res.users"].sudo().with_context(active_test=False).search_count([
            ("distri_email_verificado", "=", email), ("id", "!=", user.id), ("share", "=", False)])
        if others:
            raise UserError("Ese correo ya lo confirmó otro usuario del equipo. Usá uno tuyo.")
        Code = self.env["distri.correo.codigo"].sudo()
        hour_ago = fields.Datetime.now() - timedelta(hours=1)
        if Code.search_count([("user_id", "=", user.id), ("create_date", ">=", hour_ago)]) \
                >= CODE_PER_HOUR:
            raise UserError("Ya pediste %s códigos en la última hora. Esperá un rato y probá "
                            "de nuevo." % CODE_PER_HOUR)
        _sender(self.env)  # refuse before creating anything
        Code.search([("user_id", "=", user.id), ("estado", "=", "pendiente")]).write(
            {"estado": "anulado"})
        code = "%06d" % secrets.randbelow(1000000)
        record = Code.create({
            "user_id": user.id,
            "email": email,
            "vence": fields.Datetime.now() + timedelta(minutes=CODE_MINUTES),
        })
        record.codigo_hash = record._hash(code)
        send_mail(self.env, email, "Tu código para confirmar el correo · %s" % SENDER_NAME, [
            Markup("Hola %s:") % user.name,
            Markup("Tu código para confirmar este correo es:"),
            Markup('<b style="font-size:26px;letter-spacing:4px">%s</b>') % code,
            Markup("Vence en %s minutos. Si no lo pediste vos, ignorá este mensaje.")
            % CODE_MINUTES,
        ], now=True)
        return record

    def _distri_confirm_code(self, typed):
        """Check the typed code against the live one; a right code saves the
        address. Returns the saved address; raises UserError otherwise."""
        self.ensure_one()
        user = self.sudo()
        record = user._distri_pending_code()
        if not record:
            raise UserError("El código venció o ya no sirve. Pedí uno nuevo.")
        typed = "".join(ch for ch in (typed or "") if ch.isdigit())
        if len(typed) != 6 or not hmac.compare_digest(record._hash(typed), record.codigo_hash or ""):
            record.intentos += 1
            left = CODE_MAX_TRIES - record.intentos
            if left <= 0:
                record.estado = "anulado"
                raise UserError("Te equivocaste %s veces. Pedí un código nuevo." % CODE_MAX_TRIES)
            raise UserError("El código no es correcto. Te quedan %s intento%s."
                            % (left, "" if left == 1 else "s"))
        record.estado = "usado"
        user.partner_id.email = record.email
        user.write({"distri_email_verificado": record.email,
                    "distri_email_verificado_fecha": fields.Datetime.now()})
        return record.email

    @api.model
    def _distri_find_for_reset(self, typed):
        """The internal users a reset request means: the login typed (as
        tolerant as _login: spaces dropped, lowercase unless a user has it
        exactly as typed) or a confirmed e-mail. Only those with a
        confirmed address."""
        Users = self.sudo()
        clean = (typed or "").strip()
        if not clean or len(clean) > 254:
            return Users
        users = Users
        if "@" in clean:
            email = email_normalize(clean)
            if email:
                users = Users.search([("distri_email_verificado", "=", email),
                                      ("share", "=", False)])
        if not users:
            login = clean
            if login != login.lower() and not Users.search_count(
                    self._get_login_domain(login), limit=1):
                login = login.lower()
            users = Users.search(
                Domain(self._get_login_domain(login)) & Domain("share", "=", False), limit=1)
        return users.filtered(lambda u: u._distri_verified_email())

    @api.model
    def _distri_request_reset(self, typed, ip, base_url):
        """Log the request and, within the limits, mail a reset link to each
        user it means. Returns nothing: the caller always shows the same
        neutral answer."""
        Log = self.env["distri.clave.pedido"].sudo()
        key = (typed or "").strip().lower()[:254]
        hour_ago = fields.Datetime.now() - timedelta(hours=1)
        too_many = Log.search_count([("clave", "=", key), ("create_date", ">=", hour_ago)]) \
            >= RESET_PER_LOGIN_HOUR or (ip and Log.search_count(
                [("ip", "=", ip), ("create_date", ">=", hour_ago)]) >= RESET_PER_IP_HOUR)
        Log.create({"clave": key, "ip": ip or "", "limitado": bool(too_many)})
        if too_many:
            return
        Token = self.env["distri.clave.token"].sudo()
        for user in self._distri_find_for_reset(typed):
            if Token.search_count([("user_id", "=", user.id), ("create_date", ">=", hour_ago)]) \
                    >= TOKEN_PER_HOUR:
                continue
            try:
                _sender(self.env)
            except UserError:
                return
            raw = secrets.token_urlsafe(32)
            Token.create({
                "user_id": user.id,
                "token_hash": hashlib.sha256(raw.encode()).hexdigest(),
                "vence": fields.Datetime.now() + timedelta(minutes=TOKEN_MINUTES),
                "ip": ip or "",
            })
            link = "%s/clave/nueva?t=%s" % ((base_url or "").rstrip("/"), raw)
            send_mail(self.env, user._distri_verified_email(),
                      "Restablecer tu contraseña · %s" % SENDER_NAME, [
                          Markup("Hola %s:") % user.name,
                          Markup("Pediste una contraseña nueva para el usuario <b>%s</b>.")
                          % user.login,
                          Markup('<a href="%s" style="display:inline-block;background:#10243f;'
                                 'color:#ffffff;text-decoration:none;font-weight:bold;'
                                 'padding:12px 18px;border-radius:10px">Elegir una contraseña '
                                 'nueva</a>') % link,
                          Markup("El enlace sirve una sola vez y vence en 1 hora. Si no lo "
                                 "pediste vos, ignorá este mensaje: tu contraseña no cambia."),
                      ])


class DistriCorreoCodigo(models.Model):
    """A confirmation code sent to a staff member's new address."""
    _name = "distri.correo.codigo"
    _description = "Código para confirmar el correo"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    email = fields.Char(required=True)
    codigo_hash = fields.Char(copy=False)
    vence = fields.Datetime(required=True)
    intentos = fields.Integer(default=0)
    estado = fields.Selection([("pendiente", "Pendiente"), ("usado", "Usado"),
                               ("anulado", "Anulado")], default="pendiente", required=True)

    def _hash(self, code):
        self.ensure_one()
        return _keyed_hash(self.env, "%s:%s:%s" % (self.id, self.user_id.id, code))


class DistriClaveToken(models.Model):
    """A single-use password reset link (only its SHA-256 is stored)."""
    _name = "distri.clave.token"
    _description = "Enlace para restablecer la contraseña"
    _order = "id desc"

    user_id = fields.Many2one("res.users", required=True, ondelete="cascade", index=True)
    token_hash = fields.Char(required=True, index=True)
    vence = fields.Datetime(required=True)
    usado = fields.Datetime(copy=False)
    ip = fields.Char()

    @api.model
    def _live(self, raw):
        if not raw or len(raw) > 200:
            return self.browse()
        return self.sudo().search([
            ("token_hash", "=", hashlib.sha256(raw.encode()).hexdigest()),
            ("usado", "=", False), ("vence", ">", fields.Datetime.now())], limit=1).filtered(
                lambda t: t.user_id.active and t.user_id._distri_verified_email())

    def _use(self, new_password):
        """Set the password and spend this link and every other live link
        of the user."""
        self.ensure_one()
        user = self.user_id.sudo()
        user._change_password(new_password)
        if "distri_debe_cambiar_clave" in user._fields:
            user.distri_debe_cambiar_clave = False
        self.sudo().search([("user_id", "=", user.id), ("usado", "=", False)]).write(
            {"usado": fields.Datetime.now()})
        return user


class DistriClavePedido(models.Model):
    """Each «¿Olvidaste la contraseña?» request, for the rate limits."""
    _name = "distri.clave.pedido"
    _description = "Pedido de contraseña nueva"
    _order = "id desc"

    clave = fields.Char(index=True)
    ip = fields.Char(index=True)
    limitado = fields.Boolean()

    @api.autovacuum
    def _gc_old(self):
        self.sudo().search([("create_date", "<", fields.Datetime.now() - timedelta(days=30))]).unlink()
