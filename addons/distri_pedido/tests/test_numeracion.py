from lxml import etree

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

from odoo.addons.distri_pedido.models.factura_numeracion import PARAMETRO


@tagged("post_install", "-at_install", "distri_numeracion")
class TestFacturaSinEnviar(TransactionCase):
    """Nadie le manda la factura al cliente desde Odoo (views/factura.xml)."""

    def _botones(self, vista, nombre):
        arch = self.env["account.move"].get_view(self.env.ref(vista).id)["arch"]
        return etree.fromstring(arch).xpath("//button[@name='%s']" % nombre)

    def test_sin_boton_enviar(self):
        for vista, boton in (("account.view_move_form", "action_invoice_sent"),
                             ("account.view_out_invoice_tree", "action_send_and_print"),
                             ("account.view_out_credit_note_tree", "action_send_and_print")):
            botones = self._botones(vista, boton)
            self.assertTrue(botones, vista)
            for b in botones:
                self.assertEqual(b.get("invisible"), "True", vista)

    def test_imprimir_sigue(self):
        imprimir = self._botones("account.view_move_form", "action_print_pdf")
        self.assertTrue(imprimir)
        self.assertTrue(all(b.get("invisible") != "True" for b in imprimir))

    def test_sin_compartir_en_el_menu_accion(self):
        self.assertFalse(self.env.ref("account.model_account_move_action_share").binding_model_id)
        acciones = self.env["ir.actions.actions"].search(
            [("binding_model_id.model", "=", "account.move")])
        self.assertFalse([a.name for a in acciones if "send" in (a.with_context(lang="en_US").name or "").lower()
                          or "share" in (a.with_context(lang="en_US").name or "").lower()])


@tagged("post_install", "-at_install", "distri_numeracion")
class TestNumeracionFactura(TransactionCase):
    """La serie de facturas sigue la de Odoo Online (models/factura_numeracion.py)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.diario = cls.env["account.journal"].search(
            [("type", "=", "sale"), ("company_id", "=", cls.env.company.id)], limit=1)
        cls.partner = cls.env["res.partner"].create({"name": "Z - KIOSCO NUMERACION"})
        cls.producto = cls.env["product.product"].create({
            "name": "Producto numeracion", "list_price": 10, "taxes_id": [Command.clear()]})

    def _factura(self, tipo="out_invoice"):
        factura = self.env["account.move"].create({
            "move_type": tipo, "journal_id": self.diario.id, "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create({
                "product_id": self.producto.id, "quantity": 1, "price_unit": 10,
                "tax_ids": [Command.clear()]})],
        })
        factura.action_post()
        return factura

    def _serie(self, numero):
        return "%s/%s/%05d" % (self.diario.code, fields.Date.today().year, numero)

    def test_sigue_despues_del_ultimo_de_online(self):
        ultimos = self.env["account.move"].search(
            [("journal_id", "=", self.diario.id), ("state", "=", "posted")]).mapped("sequence_number")
        piso = max(ultimos + [0]) + 5000
        self.env["ir.config_parameter"].sudo().set_param(PARAMETRO, self._serie(piso))
        factura = self._factura()
        self.assertEqual(factura.name, self._serie(piso + 1))
        nota = self._factura("out_refund")
        self.assertEqual(nota.name, self._serie(piso + 2), "las NC van en la misma serie")

    def test_el_piso_no_frena_la_serie(self):
        primera = self._factura()
        self.env["ir.config_parameter"].sudo().set_param(PARAMETRO, self._serie(1))
        segunda = self._factura()
        self.assertEqual(segunda.sequence_number, primera.sequence_number + 1)

    def test_otro_anio_no_aplica(self):
        self.env["ir.config_parameter"].sudo().set_param(PARAMETRO, "%s/1999/99999" % self.diario.code)
        factura = self._factura()
        self.assertLess(factura.sequence_number, 99999)
