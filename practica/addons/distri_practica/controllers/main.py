"""Landing of the practice environment and the passwordless role buttons.

Everything here requires the system parameter distri_practica.activo = '1'
(set only by the sanitize step of /srv/odoo-lab/practica, i.e. only in the
practice database). Without it «/» is the normal public site and the role
buttons refuse.

A role button POSTs /practica/entrar with the role and the company's short
access code (distri_practica.codigo). The server checks the code once per
browser session, then opens a session as the FIXED practice user of that
role (data/usuarios.xml) and redirects to the role's main screen. Nothing
the visitor sends chooses the user: only the role key, looked up in ROLES.
"""
import hmac
import logging
import threading
import time

from markupsafe import Markup

from odoo import http
from odoo.http import request

from odoo.addons.distri_pedido.controllers.sitio import SitioPublico

_logger = logging.getLogger(__name__)

PARAM_ACTIVE = "distri_practica.activo"
PARAM_CODE = "distri_practica.codigo"
SESSION_OK = "distri_practica_ok"

# key, button label, one-line hint, user xmlid, main screen
ROLES = [
    ("armador", "Armador", "Arma los pedidos con el celular",
     "distri_practica.user_armador", "/armado"),
    ("repartidor", "Repartidor", "Sale a repartir, entrega y cobra",
     "distri_practica.user_repartidor_1", "/reparto"),
    ("deposito", "Depósito y despacho", "Controla la carga de los viajes y entrega en el local",
     "distri_practica.user_deposito", "/despacho"),
    ("cajero", "Cajero", "Abre, recibe y cierra la caja del día",
     "distri_practica.user_cajero", "/caja"),
    ("administracion", "Administración", "Revisa y aprueba pedidos, cuentas y cobros",
     "distri_practica.user_administracion", "/tablero"),
    ("gerencia", "Gerencia", "Ve todo: tablero, cajas, rutas y cobros",
     "distri_practica.user_gerencia", "/tablero"),
    ("vendedor", "Vendedor", "Carga pedidos de sus clientes",
     "distri_practica.user_vendedor", "/pedido"),
]
# second driver: a small link under «Repartidor», so two people can practice
EXTRA_ROLES = [
    ("repartidor2", "Repartidor 2", "", "distri_practica.user_repartidor_2", "/reparto"),
]
ROLE_BY_KEY = {r[0]: r for r in ROLES + EXTRA_ROLES}

# wrong codes: at most this many per IP in the window (per worker process)
MAX_FAILS = 10
FAIL_WINDOW = 600
_fails = {}
_fails_lock = threading.Lock()


def is_active(env):
    return env["ir.config_parameter"].sudo().get_param(PARAM_ACTIVE) == "1"


def practice_users(env):
    users = env["res.users"]
    for _key, _label, _hint, xmlid, _url in ROLES + EXTRA_ROLES:
        users |= env.ref(xmlid, raise_if_not_found=False) or env["res.users"]
    return users


def _client_ip():
    return request.httprequest.remote_addr or "?"


def _too_many_fails(ip):
    now = time.time()
    with _fails_lock:
        recent = [t for t in _fails.get(ip, []) if now - t < FAIL_WINDOW]
        _fails[ip] = recent
        return len(recent) >= MAX_FAILS


def _add_fail(ip):
    with _fails_lock:
        _fails.setdefault(ip, []).append(time.time())


class PracticaLanding(SitioPublico):

    @http.route()
    def inicio(self, **kw):
        if not is_active(request.env):
            return super().inicio(**kw)
        return self._landing()

    def _landing(self, error="", status=200):
        env = request.env
        user = env.user
        current = ""
        if not user._is_public():
            current = user.name
        ok = bool(request.session.get(SESSION_OK))
        values = {
            "roles": [{"key": k, "label": label, "hint": hint} for k, label, hint, _x, _u in ROLES],
            "extra": [{"key": k, "label": label} for k, label, _h, _x, _u in EXTRA_ROLES],
            "code_ok": ok,
            "error": error,
            "current": current,
            "datos": self._practice_data() if ok else {},
            "empresa": env.company.name,
        }
        response = request.render("distri_practica.landing", values)
        response.status_code = status
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        return response

    def _practice_data(self):
        """Today's receipt codes of the practice routes on the street, so the
        driver can practice «Entregado con código»."""
        env = request.env
        drivers = practice_users(env)
        routes = env["distri.route"].sudo().search([
            ("kind", "=", "route"), ("state", "=", "on_route"), ("driver_id", "in", drivers.ids),
        ])
        codes = []
        for stop in routes.stop_ids.sorted(lambda s: (s.route_id.id, s.sequence, s.id)):
            order = stop.order_id
            if stop.state != "pending" or not order.distri_receipt_code:
                continue
            codes.append({"route": stop.route_id.name, "order": order.name,
                          "partner": order.partner_id.name, "code": order.distri_receipt_code})
        return {"codes": codes}

    @http.route("/practica/entrar", type="http", auth="public", methods=["POST"],
                website=True, sitemap=False)
    def entrar(self, rol="", codigo="", **kw):
        env = request.env
        if not is_active(env):
            _logger.warning("distri_practica: role login refused, %s is not '1'", PARAM_ACTIVE)
            return request.make_response("No disponible.", status=403,
                                         headers=[("Content-Type", "text/plain; charset=utf-8")])
        role = ROLE_BY_KEY.get(rol or "")
        if not role:
            return self._landing(error="Elegí un rol.", status=400)
        if not request.session.get(SESSION_OK):
            ip = _client_ip()
            if _too_many_fails(ip):
                return self._landing(error="Demasiados intentos. Probá de nuevo en unos minutos.",
                                     status=429)
            expected = (env["ir.config_parameter"].sudo().get_param(PARAM_CODE) or "").strip().lower()
            given = (codigo or "").strip().lower()
            if not expected or not hmac.compare_digest(given.encode(), expected.encode()):
                _add_fail(ip)
                _logger.info("distri_practica: wrong access code from %s", ip)
                return self._landing(error="El código no es correcto.", status=403)
        user = env.ref(role[3], raise_if_not_found=False)
        user = user.sudo() if user else None
        if not user or not user.active or user.share or not user.login.startswith("practica."):
            return self._landing(error="Ese usuario de práctica no está disponible.", status=409)
        session = request.session
        session.logout(keep_db=True)
        session["pre_login"] = user.login
        session["pre_uid"] = user.id
        session.finalize(env)
        session[SESSION_OK] = True
        request.update_env(user=user.id)
        _logger.info("distri_practica: %s entered as %s", _client_ip(), user.login)
        return request.redirect(role[4], 303)


def banner_html():
    return BANNER


BANNER = Markup("""<style id="dnp-style">
:root{--dnp-h:28px}
@media (max-width:719px){:root{--dnp-h:44px}}
#dn-practica-banner{position:sticky;top:0;left:0;right:0;z-index:2147483000;box-sizing:border-box;
height:var(--dnp-h);min-height:var(--dnp-h);flex:0 0 var(--dnp-h);overflow:hidden;margin:0;
display:flex;flex-wrap:wrap;align-items:center;justify-content:center;align-content:center;
gap:0 10px;padding:3px 10px;background:#f59e0b;color:#231400;border-bottom:2px solid #b45309;
font:700 13px/1.3 system-ui,-apple-system,"Segoe UI",Roboto,Helvetica,Arial,sans-serif;
letter-spacing:.01em;text-align:center}
#dn-practica-banner span{white-space:nowrap}
#dn-practica-banner a{color:#231400;text-decoration:underline;white-space:nowrap;font-weight:800}
#dn-practica-banner a:focus-visible{outline:2px solid #231400;outline-offset:2px}
.dn-top,.cab,.rt-bar,.ag-bar,body>.sticky,.sticky{top:var(--dnp-h)!important}
.dn-gear--float{top:calc(var(--dnp-h) + 6px)!important}
@media print{#dn-practica-banner{display:none}}
</style><div id="dn-practica-banner" role="note" aria-label="Entorno de práctica">
<span>ENTORNO DE PRÁCTICA · Nada de esto es real</span>
<span>Se reinicia todas las noches · <a href="/">Cambiar de rol</a></span>
</div>""")
