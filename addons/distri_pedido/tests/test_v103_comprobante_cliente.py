"""19.0.1.103.0: the customer uploads his transfer receipt from his order
page (/pedido/<token>), and «Salida de caja» takes a PDF. Synthetic data
only: no real customer or receipt."""
import io
import re

from PIL import Image

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.comprobante_cliente import (
    CUSTOMER_ATTEMPT_STATE, CUSTOMER_MAX_BYTES, PARAM_MAX_PER_DAY,
)
from odoo.addons.distri_pedido.tests.test_caja import CajaMixin

CSRF = re.compile(r'name="csrf_token" value="([^"]+)"')
OK_TEXT = "Recibimos tu comprobante · lo revisa administración."
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def _png(color, size=(40, 30)):
    buffer = io.BytesIO()
    Image.new("RGB", size, color).save(buffer, format="PNG")
    return buffer.getvalue()


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_comprobante")
class TestComprobanteDelCliente(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        cls.customer = Partner.create({"name": "Kiosco Sube V103", "company_type": "company",
                                       "customer_rank": 1})
        cls.other_customer = Partner.create({"name": "Almacen Ajeno V103",
                                             "company_type": "company", "customer_rank": 1})
        cls.generic = Partner._distri_generic_partner_for(False)
        cls.product = cls.env["product.product"].create({
            "name": "Producto Sube V103", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.order = cls._order(cls.customer)
        cls.other_order = cls._order(cls.other_customer)
        cls.Message = cls.env["distri.whatsapp.message"].sudo()
        cls.Attempt = cls.env["distri.receipt.upload.attempt"].sudo()
        params = cls.env["ir.config_parameter"].sudo()
        for key in ("distri_pedido.comprobante_cliente_limite_ip_10min",
                    "distri_pedido.comprobante_cliente_limite_ip_dia",
                    "distri_pedido.comprobante_cliente_limite_sesion_10min", PARAM_MAX_PER_DAY):
            params.set_param(key, False)

    @classmethod
    def _order(cls, partner, **vals):
        order = cls.env["sale.order"].sudo().create(dict({
            "partner_id": partner.id,
            "distri_nombre": "Comprador Web V103",
            "distri_telefono": "299 000-3103",
            "distri_wa_state": "direct",
            "order_line": [Command.create({"product_id": cls.product.id, "product_uom_qty": 1,
                                           "price_unit": 100.0, "tax_ids": [Command.clear()]})],
        }, **vals))
        order._portal_ensure_token()
        return order

    def _page(self, order):
        res = self.url_open("/pedido/%s" % order.access_token)
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res

    def _post(self, order, name, raw, mime="image/png", importe="", path_token=None,
              csrf=None):
        if csrf is None:
            csrf = CSRF.search(self._page(order).text).group(1)
        res = self.url_open("/pedido/%s/comprobante" % (path_token or order.access_token),
                            data={"csrf_token": csrf, "importe": importe},
                            files=[("archivo", (name, raw, mime))], allow_redirects=False)
        return res

    def _code(self, res):
        self.assertEqual(res.status_code, 303, res.text[:300])
        found = re.search(r"\?comprobante=(\w+)#subir-comprobante", res.headers["Location"])
        self.assertTrue(found, res.headers["Location"])
        return found.group(1)

    def _mine(self, order):
        return self.Message.search([("distri_customer_order_id", "=", order.id)])

    # ------------------------------------------------------------ happy path

    def test_block_before_upload(self):
        html = self._page(self.order).text
        self.assertIn('id="subir-comprobante"', html)
        self.assertIn("Subir comprobante de transferencia", html)
        self.assertIn('action="/pedido/%s/comprobante"' % self.order.access_token, html)
        self.assertIn('accept="image/jpeg,image/png,image/webp,application/pdf"', html)
        self.assertNotIn("Tus comprobantes de este pedido", html)

    def test_customer_uploads_a_photo(self):
        photo = _png((10, 31, 3))
        res = self._post(self.order, "transferencia.png", photo, importe="15.000")
        self.assertEqual(self._code(res), "ok")
        msg = self._mine(self.order)
        self.assertEqual(len(msg), 1)
        self.assertEqual(msg.distri_partner_id, self.customer)
        self.assertIn("Subido por el cliente", msg.distri_partner_source)
        self.assertEqual(msg.profile_name, "Subido por el cliente")
        self.assertEqual(msg.distri_upload_source, "Subido por el cliente")
        self.assertFalse(msg.distri_forwarded_by_id)
        self.assertTrue(msg.distri_upload)
        self.assertTrue(msg.distri_is_receipt)
        self.assertEqual(msg.distri_amount_typed, 15000.0)
        self.assertEqual(msg.distri_order_ref, self.order.name)
        self.assertEqual(msg.attachment_id.mimetype, "image/png")
        self.assertEqual(msg._distri_upload_channel(), "Subido por el cliente")
        html = self.url_open(res.headers["Location"]).text
        self.assertIn(OK_TEXT, html)
        self.assertIn("Tus comprobantes de este pedido", html)
        self.assertIn("transferencia.png", html)
        self.assertIn("Lo revisa administración", html)

    def test_generic_contact_order_keeps_the_typed_data(self):
        order = self._order(self.generic, distri_nombre="Despensa Sin Cliente V103")
        res = self._post(order, "comprobante.pdf", PDF, mime="application/pdf")
        self.assertEqual(self._code(res), "ok")
        msg = self._mine(order)
        self.assertFalse(msg.distri_partner_id)
        self.assertIn("Despensa Sin Cliente V103", msg.body)
        self.assertIn("299 000-3103", msg.body)
        self.assertEqual(msg.media_mime_type, "application/pdf")

    # ------------------------------------------------------------ refusals

    def test_bad_token(self):
        csrf = CSRF.search(self._page(self.order).text).group(1)
        res = self._post(self.order, "x.png", _png((11, 31, 3)), csrf=csrf,
                         path_token="NoExisteEsteToken0000000000")
        self.assertEqual(res.status_code, 404)
        self.assertFalse(self.Message.search_count([("media_filename", "=", "x.png")]))

    def test_without_csrf(self):
        res = self._post(self.order, "x.png", _png((12, 31, 3)), csrf="")
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self._mine(self.order))

    def test_wrong_state(self):
        cancelled = self._order(self.customer)
        cancelled._action_cancel()
        csrf = CSRF.search(self._page(self.order).text).group(1)
        res = self._post(cancelled, "x.png", _png((13, 31, 3)), csrf=csrf)
        self.assertEqual(self._code(res), "estado")
        self.assertFalse(self._mine(cancelled))
        self.assertNotIn('id="subir-comprobante"', self._page(cancelled).text)
        waiting = self._order(self.customer, distri_wa_state="waiting")
        self.assertNotIn('id="subir-comprobante"', self._page(waiting).text)
        self.assertEqual(self._code(self._post(waiting, "x.png", _png((14, 31, 3)), csrf=csrf)),
                         "estado")
        self.assertFalse(self._mine(waiting))

    def test_too_big(self):
        raw = b"\x89PNG\r\n\x1a\n" + b"0" * CUSTOMER_MAX_BYTES
        self.assertEqual(self._code(self._post(self.order, "enorme.png", raw)), "grande")
        self.assertFalse(self._mine(self.order))
        html = self.url_open("/pedido/%s?comprobante=grande" % self.order.access_token).text
        self.assertIn("más de 10 MB", html)

    def test_wrong_type(self):
        gif = b"GIF89a" + b"\x00" * 64
        self.assertEqual(self._code(self._post(self.order, "foto.gif", gif, mime="image/gif")),
                         "tipo")
        html_file = b"<html><script>alert(1)</script></html>"
        self.assertEqual(self._code(self._post(self.order, "x.png", html_file)), "tipo")
        self.assertFalse(self._mine(self.order))

    def test_bad_amount(self):
        res = self._post(self.order, "x.png", _png((15, 31, 3)), importe="1.5")
        self.assertEqual(self._code(res), "importe")
        self.assertFalse(self._mine(self.order))

    def test_too_many_per_order_and_day(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_MAX_PER_DAY, "2")
        for n in range(2):
            self.assertEqual(self._code(self._post(self.order, "c%d.png" % n,
                                                   _png((20 + n, 31, 3)))), "ok")
        self.assertEqual(self._code(self._post(self.order, "c3.png", _png((29, 31, 3)))),
                         "muchos")
        self.assertEqual(len(self._mine(self.order)), 2)
        html = self.url_open("/pedido/%s?comprobante=muchos" % self.order.access_token).text
        self.assertIn("Ya subiste 2 comprobantes hoy", html)
        # another order of the same customer is counted apart
        self.assertEqual(self._code(self._post(self.other_order, "c4.png",
                                               _png((30, 31, 3)))), "ok")

    def test_rate_limit(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.comprobante_cliente_limite_sesion_10min", "1")
        self.assertEqual(self._code(self._post(self.order, "a.png", _png((40, 31, 3)))), "ok")
        self.assertEqual(self._code(self._post(self.order, "b.png", _png((41, 31, 3)))), "limite")
        self.assertEqual(len(self._mine(self.order)), 1)
        html = self.url_open("/pedido/%s?comprobante=limite" % self.order.access_token).text
        self.assertIn("Esperá unos minutos", html)

    def test_duplicate_reveals_nothing_of_the_existing_receipt(self):
        photo = _png((50, 31, 3))
        # the same file already loaded by the staff for another customer
        seller = new_test_user(self.env, login="vend_sube_v103", name="Vendedor Sube V103",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        existing = self.Message.with_user(seller)._distri_upload_receipts(
            [("del-vendedor.png", photo)], partner=self.other_customer, amounts=["99.999"])
        self.assertEqual(existing[0]["status"], "nuevo")
        res = self._post(self.order, "mio.png", photo)
        self.assertEqual(self._code(res), "repetido")
        self.assertFalse(self._mine(self.order))
        attempt = self.Attempt.search([("order_id", "=", self.order.id)])
        self.assertEqual(len(attempt), 1)
        self.assertEqual(attempt.via, "cliente")
        self.assertEqual(attempt.message_id.id, existing[0]["id"])
        html = self.url_open(res.headers["Location"]).text
        self.assertIn("ya lo teníamos", html)
        self.assertIn(CUSTOMER_ATTEMPT_STATE, html)
        self.assertIn("mio.png", html)
        for secret in ("Almacen Ajeno V103", "del-vendedor.png", "99.999", "Vendedor Sube"):
            self.assertNotIn(secret, html)

    def test_other_orders_receipts_are_hidden(self):
        self.assertEqual(self._code(self._post(self.other_order, "ajeno.png",
                                               _png((60, 31, 3)))), "ok")
        # a staff upload that names this order is not the customer's
        seller = new_test_user(self.env, login="vend_oculto_v103", name="Vendedor Oculto V103",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        self.Message.with_user(seller)._distri_upload_receipts(
            [("del-personal.png", _png((61, 31, 3)))], partner=self.customer)
        self.assertEqual(self._code(self._post(self.order, "propio.png",
                                               _png((62, 31, 3)))), "ok")
        html = self._page(self.order).text
        self.assertIn("propio.png", html)
        self.assertNotIn("ajeno.png", html)
        self.assertNotIn("del-personal.png", html)
        rows = self.Message._distri_customer_rows(self.order)
        self.assertEqual([r["name"] for r in rows], ["propio.png"])


@tagged("post_install", "-at_install", "distri_v103", "distri_v103_caja")
class TestSalidaDeCajaConPdf(CajaMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls._setup_caja(cls.env.company)

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF.search(page.text).group(1)

    def _open_box(self):
        self.authenticate("cajero_test", "cajero_test")
        res = self.url_open("/caja/abrir", data={"csrf_token": self._csrf("/caja"),
                                                 "fondo": "1000"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        return self.env["distri.caja"].current_for(self.cashier)

    def _outflow(self, name, raw, mime):
        return self.url_open("/caja/salida", data={
            "csrf_token": self._csrf("/caja/salida"), "tipo": "libreria", "importe": "200",
            "quien": "Librería V103"}, files=[("foto", (name, raw, mime))],
            headers={"X-Distri-Fetch": "1"})

    def test_pdf_receipt_stored_viewable_and_label_unaffected(self):
        caja = self._open_box()
        self.assertIn('accept="image/*,application/pdf"', self.url_open("/caja/salida").text)
        res = self._outflow("factura-libreria.pdf", PDF, "application/pdf")
        self.assertTrue(res.json()["ok"], res.text)
        move = caja.movement_ids.filtered(lambda m: m.amount == -200)
        self.assertEqual(len(move), 1)
        self.assertTrue(move.document)
        self.assertEqual(move.document_name, "factura-libreria.pdf")
        self.assertFalse(move.photo)
        self.assertTrue(move.has_receipt)
        home = self.url_open("/caja").text
        self.assertIn("/caja/movimiento/%s/comprobante" % move.id, home)
        self.assertIn("Ver PDF", home)
        shown = self.url_open("/caja/movimiento/%s/comprobante" % move.id)
        self.assertEqual(shown.headers["Content-Type"], "application/pdf")
        self.assertEqual(shown.content, PDF)
        # a photo still works the same way
        res = self._outflow("ticket.png", _png((70, 31, 3)), "image/png")
        self.assertTrue(res.json()["ok"], res.text)
        photo_move = caja.movement_ids.filtered(lambda m: m.photo)
        self.assertEqual(len(photo_move), 1)
        self.assertIn("Ver foto", self.url_open("/caja").text)
        self.assertTrue(self.url_open("/caja/movimiento/%s/comprobante" % photo_move.id)
                        .headers["Content-Type"].startswith("image/"))
        # closing: the label is the same PDF, the receipt goes with the line
        data = {"csrf_token": self._csrf("/caja/cierre"), "queda": "0", "motivo": "prueba",
                "cant_%s" % self.bills[500].id: "1"}
        res = self.url_open("/caja/cierre", data=data, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertEqual(caja.state, "closed")
        label = self.url_open("/caja/%s/etiqueta" % caja.id)
        self.assertEqual(label.headers["Content-Type"], "application/pdf")
        moves = move.statement_line_ids.move_id
        if moves:
            self.assertTrue(self.env["ir.attachment"].sudo().search_count([
                ("res_model", "=", "account.move"), ("res_id", "in", moves.ids),
                ("mimetype", "=", "application/pdf")]))
        # another user does not see it
        self.authenticate("vendedor_caja", "vendedor_caja")
        self.assertEqual(self.url_open("/caja/movimiento/%s/comprobante" % move.id).status_code,
                         403)

    def test_refused_files(self):
        caja = self._open_box()
        big = b"%PDF-1.4\n" + b"0" * (4 * 1024 * 1024 + 1)
        res = self._outflow("grande.pdf", big, "application/pdf")
        self.assertEqual(res.status_code, 400)
        self.assertIn("demasiado grande", res.json()["error"])
        res = self._outflow("nota.txt", b"hola", "text/plain")
        self.assertEqual(res.status_code, 400)
        self.assertIn("foto o un PDF", res.json()["error"])
        self.assertFalse(caja.movement_ids.filtered(lambda m: m.amount == -200))
