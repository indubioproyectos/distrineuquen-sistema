import hashlib
import hmac
import json
import logging

from odoo import http
from odoo.http import request

_logger = logging.getLogger(__name__)

# Meta manda avisos chicos (un mensaje, a lo sumo unos pocos). Cualquier cosa
# mas grande no es de Meta y no vale la pena calcularle el HMAC.
MAX_CUERPO = 1024 * 1024


def _texto(cuerpo, status):
    return request.make_response(
        cuerpo, headers=[("Content-Type", "text/plain; charset=utf-8")], status=status,
    )


class WhatsappWebhook(http.Controller):

    def _firma_wa_valida(self, secreto, crudo):
        """X-Hub-Signature-256: sha256=HMAC-SHA256(app secret, cuerpo crudo)."""
        firma = request.httprequest.headers.get("X-Hub-Signature-256") or ""
        prefijo, _, recibido = firma.partition("=")
        if prefijo != "sha256" or not recibido:
            return False
        esperado = hmac.new(secreto.encode(), crudo, hashlib.sha256).hexdigest()
        return hmac.compare_digest(esperado, recibido.strip().lower())

    @http.route("/pedido/whatsapp/webhook", type="http", auth="public",
                methods=["GET", "POST"], csrf=False, sitemap=False, save_session=False)
    def whatsapp_webhook(self, **kw):
        """Avisos de WhatsApp Cloud API. GET es la verificacion que hace Meta
        al guardar la URL; POST trae mensajes y estados, siempre firmados."""
        Mensaje = request.env["distri.whatsapp.message"].sudo()
        if request.httprequest.method == "GET":
            return self._verificar(Mensaje)

        # Sin clave de la app no hay forma de comprobar quien manda el aviso:
        # se rechaza todo antes que aceptar algo sin firma.
        secreto = Mensaje._wa_app_secret()
        if not secreto:
            _logger.warning("WhatsApp notification rejected: app secret not configured")
            return _texto("not configured", 403)
        largo = request.httprequest.content_length
        if largo is not None and largo > MAX_CUERPO:
            return _texto("too large", 413)
        crudo = request.httprequest.get_data(cache=False)
        if len(crudo) > MAX_CUERPO:
            return _texto("too large", 413)
        if not self._firma_wa_valida(secreto, crudo):
            _logger.warning("WhatsApp notification with invalid signature")
            return _texto("invalid signature", 401)

        try:
            cuerpo = json.loads(crudo)
        except ValueError:
            # firmado pero ilegible: reintentarlo no lo arregla
            _logger.warning("WhatsApp notification with a signed but invalid body")
            return _texto("bad request", 400)
        creados, actualizados = Mensaje._wa_store_webhook(cuerpo)
        _logger.info("WhatsApp notification: %s new messages, %s status updates",
                     creados, actualizados)
        return _texto("ok", 200)

    def _verificar(self, Mensaje):
        args = request.httprequest.args
        esperado = Mensaje._wa_verify_token()
        recibido = args.get("hub.verify_token") or ""
        desafio = args.get("hub.challenge") or ""
        if (esperado and args.get("hub.mode") == "subscribe" and desafio
                and hmac.compare_digest(esperado.encode(), recibido.encode())):
            _logger.info("WhatsApp webhook verified by Meta")
            return _texto(desafio[:256], 200)
        _logger.warning("WhatsApp webhook verification rejected")
        return _texto("forbidden", 403)
