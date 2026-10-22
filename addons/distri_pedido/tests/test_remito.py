import re

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.remito import es_numero_de_factura, numerar_remitos


@tagged("post_install", "-at_install", "distri_remito")
class TestRemito(HttpCase):
    """El cliente ve el remito, nunca la factura (models/remito.py)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.armador = new_test_user(
            cls.env, login="armador_remito", name="Armador Remito",
            groups="base.group_user,distri_pedido.grupo_armador",
        )
        cls.partner = cls.env["res.partner"].create({"name": "Z - KIOSCO REMITO"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto remito", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()],
        })
        pricelist = cls.env["product.pricelist"].create({
            "name": "Remito test", "currency_id": cls.env.company.currency_id.id})
        cls.order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "pricelist_id": pricelist.id,
            "order_line": [Command.create({
                "product_id": cls.product.id, "product_uom_qty": 4,
                "price_unit": 250.0, "tax_ids": [Command.clear()],
            })],
        })
        cls.order.action_confirm()
        cls.order._portal_ensure_token()
        pedido = cls.order.with_user(cls.armador).sudo()
        assert pedido.distri_tomar() == ""
        assert pedido.distri_terminar(1) == ""
        cls.invoice = cls.order.distri_factura_auto_id
        cls.remito = cls.order._distri_remitos()

        # historia importada del Odoo anterior: una factura, un pago y un
        # documento que tambien esta vivo en Odoo (cuenta una sola vez)
        Mov = cls.env["distri.cuenta.movimiento"].sudo()
        hoy = fields.Date.today()
        cls.historia = Mov.create([
            {"partner_id": cls.partner.id, "fecha": hoy, "comprobante": "0001/2025/00077",
             "tipo": "factura", "debe": 1500.0},
            {"partner_id": cls.partner.id, "fecha": hoy, "comprobante": "PBNK1/2025/00009",
             "tipo": "pago", "haber": 400.0},
            {"partner_id": cls.partner.id, "fecha": hoy, "comprobante": cls.invoice.name,
             "tipo": "factura", "debe": cls.invoice.amount_total},
        ])

    def test_mismo_numero_otro_importe_no_se_descarta(self):
        """Los numeros de Odoo se repiten con los de Odoo Online.

        Un pago de la historia con el mismo numero que un documento vivo pero
        otro importe es otro documento: se cuenta. Antes se descartaba.
        """
        antes = self.partner._distri_account_balance()
        self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": fields.Date.today(),
            "comprobante": self.invoice.name, "tipo": "pago", "haber": 123.0})
        self.assertEqual(self.partner._distri_account_balance(), antes - 123.0)
        html = self.url_open("/pedido/%s/cuenta" % self.order.access_token).text
        self.assertIn("$ %s" % self._plata(antes - 123.0), html)

    def _plata(self, monto):
        return "{:,.0f}".format(monto).replace(",", ".")

    def test_factura_y_remito(self):
        self.assertEqual(self.invoice.state, "posted")
        self.assertEqual(self.invoice.amount_tax, 0.0)
        self.assertEqual(len(self.remito), 1)
        self.assertEqual(self.remito.state, "done")
        self.assertEqual(self.invoice._distri_remito_texto(), self.remito.name)
        self.assertTrue(es_numero_de_factura("0001/2026/06275"))
        self.assertFalse(es_numero_de_factura("PBNK1/2026/00001"))
        self.assertFalse(es_numero_de_factura(self.remito.name))

    def test_remito_con_prefijo_r(self):
        """Las entregas salen como R-00047 (models/remito.py, numerar_remitos)."""
        self.assertRegex(self.remito.name, r"^R-\d{5}$")
        numero = self.remito.name[2:]
        self.remito.name = "WH/OUT/%s" % numero
        self.assertEqual(numerar_remitos(self.env), 1)
        self.assertEqual(self.remito.name, "R-%s" % numero)
        self.assertEqual(numerar_remitos(self.env), 0, "se puede correr de nuevo")

    def test_cuenta_muestra_el_remito_y_no_la_factura(self):
        res = self.url_open("/pedido/%s/cuenta" % self.order.access_token)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn(self.remito.name, html)
        self.assertIn("Remito", html)
        self.assertIn("PBNK1/2025/00009", html, "los pagos conservan su recibo")
        self.assertNotIn(self.invoice.name, html)
        self.assertNotIn("0001/2025/00077", html)
        self.assertNotIn("Factura", html)
        # el saldo de la pagina es el mismo que usa el reparto: el documento
        # que esta en las dos fuentes cuenta una sola vez
        saldo = self.partner._distri_account_balance()
        self.assertEqual(saldo, self.invoice.amount_total + 1500.0 - 400.0)
        self.assertIn("$ %s" % self._plata(saldo), html)
        self.assertEqual(len(re.findall(r'class="mov"', html)), 3)

    def test_pedido_muestra_el_remito(self):
        res = self.url_open("/pedido/%s" % self.order.access_token)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn('id="como-pagar"', html)
        self.assertIn(self.remito.name, html)
        self.assertNotIn(self.invoice.name, html)
        self.assertNotIn("factura", html.lower())

    def test_nota_de_credito_como_devolucion(self):
        nota = self.invoice._reverse_moves([{"ref": "Devolucion test"}])
        nota.invoice_line_ids.quantity = 1
        nota.action_post()
        html = self.url_open("/pedido/%s/cuenta" % self.order.access_token).text
        self.assertIn("Devolución", html)
        self.assertNotIn(nota.name, html)
        self.assertNotIn(self.invoice.name, html)

    def test_portal_sin_facturas(self):
        usuario = new_test_user(
            self.env, login="cliente_remito", name="Z - KIOSCO REMITO", groups="base.group_portal",
            partner_id=self.partner.id,
        )
        Move = self.env["account.move"].with_user(usuario)
        self.assertFalse(Move.search([("id", "=", self.invoice.id)]))
        self.assertFalse(self.env["account.move.line"].with_user(usuario).search(
            [("move_id", "=", self.invoice.id)]))

        self.authenticate("cliente_remito", "cliente_remito")
        res = self.url_open("/my/invoices")
        self.assertTrue(res.url.endswith("/mi-cuenta"), res.url)
        self.assertIn(self.remito.name, res.text)
        self.assertNotIn(self.invoice.name, res.text)

        res = self.url_open("/my/invoices/%s?access_token=%s" % (
            self.invoice.id, self.invoice._portal_ensure_token()))
        self.assertTrue(res.url.endswith("/mi-cuenta"), res.url)

        res = self.url_open("/my/orders/%s" % self.order.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn(self.order.name, res.text)
        self.assertNotIn(self.invoice.name, res.text)

        res = self.url_open("/report/html/account.report_invoice/%s" % self.invoice.id)
        self.assertNotIn(self.invoice.name, res.text)

    def test_personal_interno_sigue_viendo_la_factura(self):
        interno = new_test_user(self.env, login="admin_remito",
                                groups="base.group_user,account.group_account_invoice")
        self.assertEqual(
            self.env["account.move"].with_user(interno).search([("id", "=", self.invoice.id)]),
            self.invoice)
