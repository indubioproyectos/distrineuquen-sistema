"""«Ir a practicar» without the practice code (19.0.1.129.42, ui_v2).

Someone already logged into production opens «Capacitación» (/proyecto) and
taps «Ir a practicar». Until 129.41 the practice site
(practica.distrineuquen.com, another database) asked him its access code.
Now, when both sides hold the same key, the link carries a short-lived
signed pass and the practice landing shows his role's button without the
code.

The pass:  1.<roles>.<expiry>.<nonce>.<signature>
- roles:     the practice landing keys of the user (proyecto.practice_roles),
             joined by «-» (gerencia, administracion, deposito, cajero,
             armador, repartidor, vendedor; may be empty: Sistemas);
- expiry:    unix time, now + PASS_TTL (10 minutes);
- nonce:     random, so the practice side can take each pass once;
- signature: HMAC-SHA256 of everything before it, hex, with the shared key.
No name, login or id of the production user travels: the practice side only
learns which roles to show first.

The key:
- production: the system parameter PARAM_KEY. Its name contains «secret»,
  so the practice environment's sanitize step (practica/sanitizar.sql)
  blanks it in the practice database even when the golden copy comes from
  a production dump: practice can never mint passes from its database;
- practice: a file outside the database (distri_practica reads it,
  /etc/odoo/distri_practica_pase.key inside the container =
  practica/config/distri_practica_pase.key on the host);
- both are written once by scripts/practica/configurar_pase.sh.
With no key (production parameter empty or shorter than MIN_KEY) the link
is the plain practice URL and everything behaves as before. Production
never sees or stores the practice access code.

verify_pass() is used by the practice module (distri_practica depends on
distri_pedido), so both sides share this one implementation.
"""
import hashlib
import hmac
import re
import secrets
import time
from urllib.parse import quote

from odoo import http
from odoo.http import request

from .proyecto import PRACTICE_FLAG, PRACTICE_KEYS, PRACTICE_URL, practice_roles, staff_only

PARAM_KEY = "distri_pedido.practica_pase_secret"
PASS_TTL = 600            # seconds a pass is valid
CLOCK_SKEW = 60           # tolerated difference between the two clocks
MIN_KEY = 32              # characters
MAX_TOKEN = 400
VERSION = "1"
NONCE_RE = re.compile(r"^[A-Za-z0-9_-]{16,64}$")
PASS_PATH = "/practica/pase"


def _sign(key, payload):
    return hmac.new(key.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_pass(key, roles, now=None, ttl=PASS_TTL, nonce=None):
    """The signed pass for these practice keys (unknown keys dropped)."""
    if not key or len(key) < MIN_KEY:
        raise ValueError("practice pass key missing or too short")
    roles = [r for r in roles if r in PRACTICE_KEYS]
    expiry = int(now if now is not None else time.time()) + int(ttl)
    payload = ".".join((VERSION, "-".join(roles), str(expiry), nonce or secrets.token_urlsafe(18)))
    return payload + "." + _sign(key, payload)


def verify_pass(key, token, now=None, ttl=PASS_TTL):
    """{"roles": [...], "nonce": str, "expiry": int} for a valid pass, else
    None (no key, malformed, bad signature, expired or too far ahead)."""
    if not key or len(key) < MIN_KEY or not token or len(token) > MAX_TOKEN:
        return None
    parts = token.split(".")
    if len(parts) != 5 or parts[0] != VERSION:
        return None
    version, roles_txt, expiry_txt, nonce, signature = parts
    payload = ".".join((version, roles_txt, expiry_txt, nonce))
    if not hmac.compare_digest(_sign(key, payload).encode(), signature.encode()):
        return None
    if not expiry_txt.isdigit() or not NONCE_RE.match(nonce):
        return None
    roles = [r for r in roles_txt.split("-") if r] if roles_txt else []
    if any(r not in PRACTICE_KEYS for r in roles):
        return None
    expiry = int(expiry_txt)
    now = int(now if now is not None else time.time())
    if expiry < now or expiry > now + ttl + CLOCK_SKEW:
        return None
    return {"roles": roles, "nonce": nonce, "expiry": expiry}


def pass_key(env):
    key = (env["ir.config_parameter"].sudo().get_param(PARAM_KEY) or "").strip()
    return key if len(key) >= MIN_KEY else ""


def pass_enabled(env):
    """The production side can sign passes (and is not the practice site)."""
    if env["ir.config_parameter"].sudo().get_param(PRACTICE_FLAG) == "1":
        return False
    return bool(pass_key(env))


class PracticaPase(http.Controller):

    @http.route("/proyecto/practicar", type="http", auth="public", website=True, sitemap=False)
    @staff_only
    def practicar(self, **kw):
        """«Ir a practicar» (ui_v2 and a key configured): to the practice
        landing with a fresh pass; anything else, the plain practice URL."""
        from .kit import ui_v2_on
        env = request.env
        target = PRACTICE_URL
        if ui_v2_on(env) and pass_enabled(env):
            token = make_pass(pass_key(env), practice_roles(env.user))
            target = "%s%s?t=%s" % (PRACTICE_URL, PASS_PATH, quote(token, safe=""))
        response = request.redirect(target, 303, local=False)
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response
