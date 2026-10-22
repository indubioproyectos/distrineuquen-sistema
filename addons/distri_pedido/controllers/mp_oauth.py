"""Lab only: obtain a Mercado Pago OAuth token with scope=read.

Purpose: prove whether a read-only token is rejected on write operations
(preferences, refunds). The token is stored in its own system parameter and
nothing in the module uses it to charge; `distri_pedido.mp_access_token` is
never touched.

Flow (Authorization Code + PKCE):
  /pedido/mp/oauth/inicio   admin only; saves state + verifier, goes to MP
  /pedido/mp/oauth/retorno  MP comes back with ?code&state; exchanges the code
"""
import base64
import hashlib
import hmac
import html
import json
import logging
import secrets
import time
from urllib.parse import urlencode

import requests

from odoo import http
from odoo.http import request

from ..models.mercado_pago import (
    MP_API, MP_TIMEOUT, PARAM_CLIENT_SECRET, PARAM_OAUTH_PENDING,
    PARAM_OAUTH_READ_SCOPE, PARAM_OAUTH_READ_TOKEN, PARAM_OAUTH_READ_USER,
    PARAM_TOKEN,
)

_logger = logging.getLogger(__name__)

MP_AUTH = "https://auth.mercadopago.com/authorization"
RETORNO = "/pedido/mp/oauth/retorno"
ALCANCE = "read"
# MP gives the code 10 minutes; the pending attempt lives the same.
VIGENCIA = 600


def _pagina(titulo, texto, status=200):
    html = (
        "<!doctype html><html lang='es'><head><meta charset='utf-8'>"
        "<meta name='viewport' content='width=device-width,initial-scale=1'>"
        "<title>%s</title></head><body style='font-family:sans-serif;"
        "max-width:36rem;margin:3rem auto;padding:0 1rem'>"
        "<h1>%s</h1><p>%s</p></body></html>"
    ) % (titulo, titulo, texto)
    return request.make_response(
        html, headers=[("Content-Type", "text/html; charset=utf-8")], status=status,
    )


class MercadoPagoOAuth(http.Controller):

    def _param(self, clave):
        return (request.env["ir.config_parameter"].sudo().get_param(clave) or "").strip()

    def _client_id(self):
        """The app id travels inside the access token:
        APP_USR-<app id>-<fecha>-<hash>-<user id>."""
        partes = self._param(PARAM_TOKEN).split("-")
        return partes[1] if len(partes) == 5 and partes[1].isdigit() else ""

    def _redirect_uri(self):
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return (base or request.httprequest.host_url).rstrip("/") + RETORNO

    @http.route("/pedido/mp/oauth/inicio", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def oauth_inicio(self, **kw):
        if not request.env.user.has_group("base.group_system"):
            return _pagina("Sin permiso", "Solo un administrador puede hacer esto.", 403)
        client_id = self._client_id()
        if not client_id or not self._param(PARAM_CLIENT_SECRET):
            return _pagina(
                "Falta configurar",
                "Falta la clave secreta de la aplicación de Mercado Pago "
                "(distri_pedido.mp_client_secret) o la llave de acceso.", 409,
            )
        estado = secrets.token_urlsafe(32)
        verificador = secrets.token_urlsafe(64)  # 86 chars, MP accepts 43-128
        desafio = base64.urlsafe_b64encode(
            hashlib.sha256(verificador.encode()).digest()
        ).decode().rstrip("=")
        request.env["ir.config_parameter"].sudo().set_param(
            PARAM_OAUTH_PENDING, json.dumps({
                "state": estado, "verifier": verificador,
                "uid": request.env.user.id, "ts": int(time.time()),
            }),
        )
        url = MP_AUTH + "?" + urlencode({
            "client_id": client_id,
            "response_type": "code",
            "platform_id": "mp",
            "state": estado,
            "redirect_uri": self._redirect_uri(),
            "code_challenge": desafio,
            "code_challenge_method": "S256",
            "scope": ALCANCE,
        })
        _logger.info("Mercado Pago OAuth (lab): authorization started by uid %s",
                     request.env.user.id)
        return request.redirect(url, local=False)

    @http.route(RETORNO, type="http", auth="public", methods=["GET"],
                sitemap=False, save_session=False)
    def oauth_retorno(self, **kw):
        Param = request.env["ir.config_parameter"].sudo()
        try:
            pendiente = json.loads(Param.get_param(PARAM_OAUTH_PENDING) or "{}")
        except ValueError:
            pendiente = {}
        estado = str(kw.get("state") or "")
        esperado = str(pendiente.get("state") or "")
        vigente = int(time.time()) - int(pendiente.get("ts") or 0) <= VIGENCIA
        if not (esperado and estado and vigente
                and hmac.compare_digest(esperado.encode(), estado.encode())):
            _logger.warning("Mercado Pago OAuth (lab): return with invalid or expired state")
            return _pagina("Autorización rechazada",
                           "El enlace no es válido o venció. Empezá de nuevo.", 400)
        # one attempt per state, whatever happens next
        Param.set_param(PARAM_OAUTH_PENDING, "")

        if kw.get("error"):
            _logger.info("Mercado Pago OAuth (lab): authorization denied (%s)",
                         str(kw.get("error"))[:60])
            return _pagina("Autorización cancelada", "Mercado Pago no dio el permiso.", 400)
        codigo = str(kw.get("code") or "")
        if not codigo or len(codigo) > 200:
            return _pagina("Autorización rechazada", "Mercado Pago no mandó el código.", 400)

        datos = {
            "grant_type": "authorization_code",
            "client_id": self._client_id(),
            "client_secret": self._param(PARAM_CLIENT_SECRET),
            "code": codigo,
            "redirect_uri": self._redirect_uri(),
            "code_verifier": pendiente.get("verifier") or "",
            "scope": ALCANCE,
        }
        try:
            respuesta = requests.post(
                MP_API + "/oauth/token", data=datos,
                headers={"Accept": "application/json"},
                timeout=MP_TIMEOUT, allow_redirects=False,
            )
        except requests.RequestException as exc:
            _logger.warning("Mercado Pago OAuth (lab): token request failed: %s",
                            type(exc).__name__)
            return _pagina("Error", "No se pudo hablar con Mercado Pago.", 502)
        try:
            cuerpo = respuesta.json()
        except ValueError:
            cuerpo = {}
        if respuesta.status_code != 200 or not cuerpo.get("access_token"):
            # MP errors carry no secret: error, message, status
            _logger.warning(
                "Mercado Pago OAuth (lab): token exchange answered HTTP %s: %s / %s",
                respuesta.status_code, str(cuerpo.get("error"))[:80],
                str(cuerpo.get("message"))[:200],
            )
            return _pagina("Error", "Mercado Pago no entregó la llave (HTTP %s)."
                           % respuesta.status_code, 502)

        alcance = str(cuerpo.get("scope") or "")
        usuario = str(cuerpo.get("user_id") or "")
        Param.set_param(PARAM_OAUTH_READ_TOKEN, cuerpo["access_token"])
        Param.set_param(PARAM_OAUTH_READ_SCOPE, alcance)
        Param.set_param(PARAM_OAUTH_READ_USER, usuario)
        _logger.info("Mercado Pago OAuth (lab): token stored for user %s, scope %r",
                     usuario, alcance)
        return _pagina(
            "Autorización recibida",
            "Listo. Mercado Pago dio el permiso (alcance: %s). Ya podés cerrar esta "
            "ventana." % html.escape(alcance or "sin dato"),
        )
