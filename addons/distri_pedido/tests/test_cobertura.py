"""19.0.1.37.0 (T8): tests that were missing for public and external entry
points: the WhatsApp webhook verification and rejections, the media
download, web access and /mi-clave, the home page and the payment link."""
import hashlib
import hmac
import json
import re
from unittest.mock import MagicMock, patch

from odoo import Command
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.distri_pedido.controllers.main import FICHA
from odoo.addons.distri_pedido.models import whatsapp as wa
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin

WEBHOOK = "/pedido/whatsapp/webhook"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64


def _param(env, key, value):
    env["ir.config_parameter"].sudo().set_param(key, value)


@tagged("post_install", "-at_install", "distri_cobertura")
class TestWebhookWhatsapp(PedidoAbiertoMixin, HttpCase):

    def test_get_verification(self):
        _param(self.env, wa.PARAM_VERIFY_TOKEN, "token-de-prueba")
        ok = self.url_open(WEBHOOK + "?hub.mode=subscribe&hub.verify_token=token-de-prueba"
                           "&hub.challenge=12345")
        self.assertEqual((ok.status_code, ok.text), (200, "12345"))
        for query in ("?hub.mode=subscribe&hub.verify_token=otro&hub.challenge=1",
                      "?hub.mode=unsubscribe&hub.verify_token=token-de-prueba&hub.challenge=1",
                      "?hub.mode=subscribe&hub.verify_token=token-de-prueba"):
            self.assertEqual(self.url_open(WEBHOOK + query).status_code, 403, query)
        # without a token configured nothing verifies, not even an empty one
        _param(self.env, wa.PARAM_VERIFY_TOKEN, "")
        res = self.url_open(WEBHOOK + "?hub.mode=subscribe&hub.verify_token=&hub.challenge=1")
        self.assertEqual(res.status_code, 403)

    def _post(self, body, signature=None):
        headers = {"Content-Type": "application/json"}
        if signature:
            headers["X-Hub-Signature-256"] = signature
        return self.url_open(WEBHOOK, data=body, headers=headers)

    def test_post_rejections(self):
        Message = self.env["distri.whatsapp.message"].sudo()
        before = Message.search_count([])
        body = json.dumps({"object": "whatsapp_business_account", "entry": []}).encode()
        # no app secret: everything is rejected
        _param(self.env, wa.PARAM_APP_SECRET, "")
        self.assertEqual(self._post(body, "sha256=00").status_code, 403)
        secret = "secreto-de-prueba"
        _param(self.env, wa.PARAM_APP_SECRET, secret)
        good = "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        self.assertEqual(self._post(body).status_code, 401)
        self.assertEqual(self._post(body, "sha256=" + "0" * 64).status_code, 401)
        self.assertEqual(self._post(body, good.replace("sha256", "sha1")).status_code, 401)
        self.assertEqual(self._post(body, good).status_code, 200)
        bad = b"{no es json"
        signed_bad = "sha256=" + hmac.new(secret.encode(), bad, hashlib.sha256).hexdigest()
        self.assertEqual(self._post(bad, signed_bad).status_code, 400)
        big = b"x" * (wa_controller_max() + 1)
        self.assertEqual(self._post(big, "sha256=00").status_code, 413)
        self.assertEqual(Message.search_count([]), before)


def wa_controller_max():
    from odoo.addons.distri_pedido.controllers import whatsapp as controller
    return controller.MAX_CUERPO


@tagged("post_install", "-at_install", "distri_cobertura")
class TestDescargaDeArchivos(TransactionCase):

    def _message(self, mime="image/png", sha=None, media_id="123456"):
        return self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.cobertura.%s.%s" % (media_id, mime),
            "direction": "in", "wa_id": "5492990000000", "message_type": "image",
            "media_id": media_id, "media_mime_type": mime, "media_state": "pending",
            "media_sha256": sha or "",
        })

    @staticmethod
    def _responses(data, url="https://lookaside.fbsbx.com/whatsapp/media/1", mime="image/png"):
        info = MagicMock(status_code=200)
        info.json.return_value = {"url": url, "file_size": len(data),
                                  "sha256": hashlib.sha256(data).hexdigest()}
        download = MagicMock(status_code=200, headers={"Content-Type": mime,
                                                      "Content-Length": str(len(data))})
        download.__enter__.return_value = download
        download.iter_content.return_value = [data]
        return [info, download]

    def test_download_saves_the_attachment(self):
        msg = self._message()
        with patch.object(wa.requests, "get", side_effect=self._responses(PNG)) as get:
            msg._wa_download_media("token")
        self.assertEqual(msg.media_state, "done")
        self.assertEqual(msg.attachment_id.raw, PNG)
        self.assertEqual(msg.attachment_id.mimetype, "image/png")
        # the token goes to Meta only, as a header
        self.assertEqual(get.call_args_list[0].kwargs["headers"]["Authorization"], "Bearer token")

    def test_download_rejections(self):
        Message = self.env["distri.whatsapp.message"]
        # not a PNG inside: final error, nothing attached
        msg = self._message(media_id="200")
        with patch.object(wa.requests, "get", side_effect=self._responses(b"GIF89a" + b"0" * 20)):
            msg._wa_download_media("token")
        self.assertEqual((msg.media_state, bool(msg.attachment_id)), ("error", False))
        # a download host that is not Meta's: refused before downloading
        msg = self._message(media_id="201")
        with patch.object(wa.requests, "get",
                          side_effect=self._responses(PNG, url="https://otro.example.com/x")) as get:
            msg._wa_download_media("token")
        self.assertEqual(msg.media_state, "error")
        self.assertEqual(get.call_count, 1)
        # a type that is not a receipt is never fetched
        msg = self._message(mime="video/mp4", media_id="202")
        with patch.object(wa.requests, "get") as get:
            msg._wa_download_media("token")
        self.assertEqual(msg.media_state, "error")
        get.assert_not_called()
        # a SHA-256 that does not match: retried later
        msg = self._message(media_id="203", sha="0" * 64)
        with patch.object(wa.requests, "get", side_effect=self._responses(PNG)):
            msg._wa_download_media("token")
        self.assertEqual((msg.media_state, msg.media_attempts), ("pending", 1))
        # without an access token the cron downloads nothing
        _param(self.env, wa.PARAM_ACCESS_TOKEN, "")
        with patch.object(wa.requests, "get") as get:
            Message._wa_cron_download_media()
        get.assert_not_called()


@tagged("post_install", "-at_install", "distri_cobertura")
class TestAccesoWebYPortada(PedidoAbiertoMixin, HttpCase):

    def test_web_access_and_password_change(self):
        partner = self.env["res.partner"].create({"name": "Almacén Cobertura",
                                                  "company_type": "company"})
        notice = partner.action_crear_acceso_web()
        user = partner.distri_usuario_id
        self.assertTrue(user.share)
        self.assertIn(self.env.ref("base.group_portal"), user.group_ids)
        self.assertFalse(user._is_internal())
        self.assertTrue(user.distri_debe_cambiar_clave)
        password = re.search(r"Contraseña: (\S+)", notice["params"]["message"]).group(1)
        self.assertGreaterEqual(len(password), 8)
        # never stored in clear on the partner or the user
        self.assertNotIn(password, json.dumps(partner.read()[0], default=str))
        with self.assertRaises(Exception):
            partner.action_crear_acceso_web()

        self.authenticate(user.login, password)
        res = self.url_open("/pedido", allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/mi-clave"))
        page = self.url_open("/mi-clave").text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)

        def change(actual, nueva, repetida):
            return self.url_open("/mi-clave", data={
                "csrf_token": csrf, "actual": actual, "nueva": nueva, "repetida": repetida,
            }, allow_redirects=False)

        self.assertIn("8 caracteres", change(password, "corta", "corta").text)
        self.assertIn("no coinciden", change(password, "nueva-clave-1", "nueva-clave-2").text)
        self.assertIn("no es correcta", change("mala-clave", "nueva-clave-1",
                                               "nueva-clave-1").text)
        res = change(password, "nueva-clave-1", "nueva-clave-1")
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/mi-cuenta"))
        user.invalidate_recordset()
        self.assertFalse(user.distri_debe_cambiar_clave)
        self.authenticate(user.login, "nueva-clave-1")
        self.assertEqual(self.url_open("/mi-cuenta").status_code, 200)

    def test_mi_clave_needs_a_session(self):
        res = self.url_open("/mi-clave", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/web/login", res.headers["Location"])

    def test_home_page(self):
        res = self.url_open("/")
        self.assertEqual(res.status_code, 200)
        self.assertIn('href="/pedido"', res.text)
        # 19.0.1.104.0 (new public website): the brand of FICHA, not the
        # company record's name (a fresh database's is «My Company»)
        self.assertIn(FICHA["nombre"], res.text)
        self.assertNotRegex(res.text.lower(), r"entrega\s+sin\s+comprobante")


@tagged("post_install", "-at_install", "distri_cobertura")
class TestLinkDePago(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        partner = cls.env["res.partner"].create({"name": "Kiosco Pago Cobertura"})
        product = cls.env["product.product"].create({"name": "Producto Pago", "list_price": 1000})
        cls.order = cls.env["sale.order"].create({
            "partner_id": partner.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 1,
                                           "price_unit": 1000})],
        })
        cls.order._portal_ensure_token()

    def _pay(self, **extra):
        page = self.url_open("/pedido").text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        data = dict(csrf_token=csrf, **extra)
        return self.url_open("/pedido/%s/pagar" % self.order.access_token, data=data,
                             allow_redirects=False)

    def test_link_off_goes_back_to_the_order(self):
        _param(self.env, "distri_pedido.mp_link_habilitado", "0")
        res = self._pay()
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/pedido/%s" % self.order.access_token))

    def test_unknown_token(self):
        page = self.url_open("/pedido").text
        csrf = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
        res = self.url_open("/pedido/no-existe/pagar", data={"csrf_token": csrf},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].endswith("/pedido"))

    def test_link_on_goes_to_mercado_pago_with_the_order_amount(self):
        Payment = type(self.env["distri.mp.payment"])
        Order = type(self.env["sale.order"])
        seen = {}

        def checkout(order, base_url):
            seen["amount"] = order.amount_total
            return "https://www.mercadopago.com.ar/checkout/v1/redirect?pref_id=PRUEBA"

        with patch.object(Payment, "_mp_link_enabled", return_value=True), \
                patch.object(Order, "_distri_mp_payable", return_value=True), \
                patch.object(Order, "_distri_editable", return_value=False), \
                patch.object(Order, "_distri_mp_checkout_url", autospec=True,
                             side_effect=checkout):
            # an amount in the request is ignored
            res = self._pay(amount="1")
        self.assertEqual(res.status_code, 303)
        self.assertTrue(res.headers["Location"].startswith(
            "https://www.mercadopago.com.ar/checkout/v1/redirect"))
        self.assertEqual(seen["amount"], self.order.amount_total)
