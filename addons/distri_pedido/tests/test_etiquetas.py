"""19.0.1.33.0: package labels, 100 x 50 mm, one page per package, never a
price. Synthetic data only."""
import io
import re

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools.pdf import PdfReader

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.etiquetas import big_number_size, format_phone
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

MM = 72 / 25.4  # PDF points per millimetre


@tagged("post_install", "-at_install", "distri_etiquetas")
class TestEtiquetas(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls._setup_reparto()
        cls.customer = cls.env["res.partner"].create({"name": "Y - KIOSCO ETIQUETA PRUEBA",
                                                      "company_type": "company"})

    def _packed(self, bultos=3, price=1234.0):
        order = self._order(self.customer, self.product_a, qty=10, price=price)
        order.sudo().distri_bultos = bultos
        return order

    def _pdf(self, report, ids):
        Report = self.env["ir.actions.report"].with_context(force_report_rendering=True)
        pdf, kind = Report._render_qweb_pdf(report, ids)
        self.assertEqual(kind, "pdf")
        return PdfReader(io.BytesIO(pdf))

    def _html(self, packages):
        html, _kind = self.env["ir.actions.report"]._render_qweb_html(
            "distri_pedido.report_etiquetas_bultos", packages.ids)
        return html.decode()

    def test_one_package_record_per_package(self):
        order = self._packed(3)
        packages = order._distri_packages()
        self.assertEqual(packages.mapped("number"), [1, 2, 3])
        self.assertEqual(packages.mapped("code"), ["%s-0%s" % (order.name, n) for n in (1, 2, 3)])
        order.sudo().distri_bultos = 2
        self.assertEqual(order._distri_packages().mapped("number"), [1, 2])
        self.assertFalse(packages[2].active, "the extra one is archived, never deleted")
        order.sudo().distri_bultos = 3
        self.assertEqual(order._distri_packages(), packages, "the same codes come back")

    def test_pdf_is_100_by_50_mm_one_page_per_package(self):
        order = self._packed(3)
        packages = order._distri_packages()
        reader = self._pdf("distri_pedido.report_etiquetas_bultos", packages.ids)
        self.assertEqual(len(reader.pages), 3)
        for page in reader.pages:
            box = page.mediabox
            self.assertAlmostEqual(float(box.width) / MM, 100, delta=1)
            self.assertAlmostEqual(float(box.height) / MM, 50, delta=1)

    def test_label_content_and_never_a_price(self):
        order = self._packed(2, price=4321.0)
        html = self._html(order._distri_packages())
        text = re.sub(r"<[^>]+>", " ", html)
        self.assertIn("KIOSCO ETIQUETA PRUEBA".title(), text.title())
        self.assertIn("Plottier", text)
        self.assertIn(order.name, text)
        # 19.0.1.94.0: the big «1/2» block, with the total spelled out
        self.assertIn("1/2", text)
        self.assertIn("2/2", text)
        self.assertIn("de 2 bultos", " ".join(text.split()))
        self.assertIn("Entrega:", text)
        self.assertIn("data:image/png;base64,", html, "the Code128 barcode is embedded")
        self.assertIn("%s-01" % order.name, text)
        # never a price or an amount
        self.assertNotIn("$", text)
        for amount in ("4321", "4.321", "43210", "43.210", "43,210"):
            self.assertNotIn(amount, text)
        self.assertNotRegex(text.lower(), r"total|precio|importe")

    def test_destination_lines(self):
        order = self._packed(1)
        order.sudo().distri_entrega = "retira"
        self.assertEqual(order._distri_packages()._label_values()["destination"],
                         "Retira en depósito")
        other = self._packed(1)
        other.sudo().write({"distri_encomienda": True, "distri_encomienda_manual": True,
                            "distri_envio_paga": "customer"})
        values = other._distri_packages()._label_values()
        self.assertTrue(values["destination"].startswith("Encomienda a"))
        self.assertTrue(values["detail"])
        routed = self._packed(1)
        route = self._route(routed)
        values = routed._distri_packages()._label_values()
        self.assertTrue(values["destination"].startswith("Reparto"))
        self.assertEqual(values["date"], route.date.strftime("%d/%m/%Y"))

    def test_route_prints_all_its_packages(self):
        first, second = self._packed(2), self._packed(3)
        route = self._route(first | second)
        action = route.action_distri_print_labels()
        self.assertEqual(action["report_name"], "distri_pedido.etiquetas_bultos_documento")
        self.assertEqual(len(action["context"]["active_ids"]), 5)

    def test_not_packed_has_no_labels(self):
        order = self._order(self.customer, self.product_a)
        order.sudo().write({"distri_armado": "pendiente", "distri_bultos": 0})
        with self.assertRaises(UserError):
            order._distri_packages()

    def test_calibration_page(self):
        reader = self._pdf("distri_pedido.report_etiqueta_prueba", self.env.company.ids)
        self.assertEqual(len(reader.pages), 1)
        box = reader.pages[0].mediabox
        self.assertAlmostEqual(float(box.width) / MM, 100, delta=1)
        self.assertAlmostEqual(float(box.height) / MM, 50, delta=1)

    # ------------------------------------------------ 19.0.1.94.0 layout

    def test_label_shows_store_address_company_and_phone(self):
        self.env["ir.config_parameter"].sudo().set_param("distri_pedido.telefono_empresa", "2995797248")
        self.customer.write({"street": "Av. San Martín 1234", "street2": "Local 2", "city": "Plottier"})
        order = self._packed(2)
        values = order._distri_packages()[0]._label_values()
        self.assertEqual(values["street"], "Av. San Martín 1234, Local 2")
        self.assertEqual(values["locality"], "Plottier")
        self.assertEqual(values["company"], "Distri Neuquén · 299 579-7248")
        self.assertEqual(values["number_text"], "1/2")
        html = self._html(order._distri_packages())
        self.assertIn("Av. San Martín 1234, Local 2", html)
        self.assertIn("299 579-7248", html)

    def test_label_without_street_or_phone(self):
        self.env["ir.config_parameter"].sudo().set_param("distri_pedido.telefono_empresa", "")
        order = self._packed(1)
        values = order._distri_packages()._label_values()
        self.assertEqual(values["street"], "")
        self.assertEqual(values["locality"], "Plottier", "falls back to the town")
        self.assertEqual(values["company"], "Distri Neuquén")
        self.assertIn("Plottier", self._html(order._distri_packages()))

    def test_long_values_stay_on_one_page_per_label(self):
        """A long name, a long street and «12/12»: still one 100 x 50 page each."""
        self.customer.write({
            "name": "Y - SUPERMERCADO Y AUTOSERVICIO LOS HERMANOS GONZALEZ DE LA COSTA SRL",
            "street": "Avenida Presidente Doctor Arturo Umberto Illia 12345 esquina Belgrano",
            "city": "Plottier"})
        order = self._packed(12)
        values = order._distri_packages()[-1]._label_values()
        self.assertEqual(values["number_text"], "12/12")
        reader = self._pdf("distri_pedido.report_etiquetas_bultos", order._distri_packages().ids)
        self.assertEqual(len(reader.pages), 12)

    def test_phone_format_and_number_size(self):
        self.assertEqual(format_phone("2995797248"), "299 579-7248")
        self.assertEqual(format_phone("(299) 579-7248"), "299 579-7248")
        self.assertEqual(format_phone("+54 9 299 579 7248"), "+54 9 299 579 7248")
        self.assertEqual(format_phone(False), "")
        self.assertGreaterEqual(big_number_size("3/5"), 30)
        self.assertLess(big_number_size("12/12"), big_number_size("3/5"))
