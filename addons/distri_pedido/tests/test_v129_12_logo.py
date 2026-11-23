"""19.0.1.129.12: the new brand logo.

The images of static/src/img/marca (built from the owner's PNGs): the square
icon on /pedido and in the home header, the favicons, the Open Graph image of
the link previews (absolute URL, served without a session) and the horizontal
logo on the company (report headers), set by migrations/19.0.1.129.12."""
import base64
import importlib.util
import io
import re

from PIL import Image

from odoo import Command
from odoo.tests import HttpCase, TransactionCase, tagged
from odoo.tools import file_open, file_path

from odoo.addons.distri_pedido.models.marca import MARCA_IMG

try:
    from pypdf import PdfReader
except ImportError:  # pragma: no cover
    from PyPDF2 import PdfReader

OG_IMAGE = MARCA_IMG + "dn-og-1200x630.png"
ICONO = MARCA_IMG + "dn-icono-192.png"


def meta(html, prop):
    found = re.search(r'<meta (?:property|name)="%s" content="([^"]*)"' % re.escape(prop), html)
    return found and found.group(1)


def image_size(data):
    return Image.open(io.BytesIO(data)).size


@tagged("post_install", "-at_install", "distri_v129_12")
class TestLogoPaginas(HttpCase):

    def _page(self, url):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200, url)
        return res.text

    def _base(self):
        return self.env["ir.config_parameter"].sudo().get_param("web.base.url").rstrip("/")

    def _assert_link_preview(self, html, title):
        image = meta(html, "og:image")
        self.assertRegex(image, r"^https?://[^/]+/")
        self.assertTrue(image.startswith(self._base() + OG_IMAGE), image)
        self.assertEqual(meta(html, "og:image:width"), "1200")
        self.assertEqual(meta(html, "og:image:height"), "630")
        self.assertEqual(meta(html, "og:title"), title)
        self.assertTrue(meta(html, "og:description"))
        self.assertEqual(meta(html, "og:type"), "website")
        self.assertEqual(meta(html, "twitter:card"), "summary_large_image")
        self.assertNotIn("og-distri-neuquen-3.jpg", html)

    def _assert_icons(self, html):
        self.assertRegex(html, r'<link rel="icon" type="image/png" sizes="32x32" href="'
                               + re.escape(MARCA_IMG + "favicon-32.png"))
        self.assertRegex(html, r'<link rel="apple-touch-icon" sizes="180x180" href="'
                               + re.escape(MARCA_IMG + "dn-icono-180.png"))

    def test_pedido_preview_icons_and_avatar(self):
        html = self._page("/pedido")
        self._assert_link_preview(html, "Distri Neuquén · Hacé tu pedido")
        self.assertEqual(meta(html, "og:url"), self._base() + "/pedido")
        self._assert_icons(html)
        avatar = re.search(r'<img class="logo"[^>]*\ssrc="([^"]+)"', html)
        self.assertTrue(avatar and avatar.group(1).startswith(ICONO), avatar and avatar.group(1))
        self.assertNotIn("/web/image/res.company/", html, "no old profile photo")

    def test_home_preview_and_icons(self):
        html = self._page("/")
        title = re.search(r"<title>([^<]+)</title>", html).group(1)
        self._assert_link_preview(html, title)
        self._assert_icons(html)
        self.assertNotIn("iconos/dn-logo.svg", html)

    def test_images_are_public(self):
        """No session: what a link preview robot gets."""
        expected = {
            "dn-og-1200x630.png": (1200, 630),
            "dn-icono-512.png": (512, 512),
            "dn-icono-192.png": (192, 192),
            "dn-icono-180.png": (180, 180),
            "favicon-32.png": (32, 32),
            "favicon-16.png": (16, 16),
            "dn-logo-horizontal-820.png": (820, 222),
        }
        for name, size in expected.items():
            res = self.url_open(MARCA_IMG + name + "?v=129.12")
            self.assertEqual(res.status_code, 200, name)
            self.assertEqual(res.headers["Content-Type"], "image/png", name)
            self.assertEqual(image_size(res.content), size, name)
            self.assertLess(len(res.content), 100_000, name)


@tagged("post_install", "-at_install", "distri_v129_12")
class TestLogoEmpresa(TransactionCase):

    def _migrate(self):
        path = file_path("distri_pedido/migrations/19.0.1.129.12/post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v129_12_post_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "19.0.1.129.11")
        self.env.invalidate_all()

    def test_migration_sets_company_logo_and_favicon(self):
        company = self.env.company.sudo()
        with file_open("web/static/img/favicon.ico", "rb") as core:
            self.env["website"].sudo().search([]).write({"favicon": base64.b64encode(core.read())})
        company.logo = False
        self._migrate()
        self.assertEqual(image_size(base64.b64decode(company.logo)), (820, 222))
        with file_open("web/static/img/favicon.ico", "rb") as core:
            core_ico = base64.b64encode(core.read())
        for website in self.env["website"].sudo().search([]):
            self.assertTrue(website.favicon)
            self.assertNotEqual(website.favicon, core_ico)

    def test_report_header_prints_the_logo(self):
        """The company logo fits the report header: a one-page PDF with it."""
        self._migrate()
        partner = self.env["res.partner"].create({"name": "Cliente Logo V129.12"})
        product = self.env["product.product"].create({"name": "Producto Logo V129.12", "list_price": 10})
        order = self.env["sale.order"].create({
            "partner_id": partner.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 1})],
        })
        report = self.env["ir.actions.report"].with_context(force_report_rendering=True)
        pdf, kind = report._render_qweb_pdf("sale.action_report_saleorder", order.ids)
        self.assertEqual(kind, "pdf")
        reader = PdfReader(io.BytesIO(pdf))
        self.assertEqual(len(reader.pages), 1)
        widths = []
        for page in reader.pages:
            xobjects = page["/Resources"].get("/XObject") or {}
            for ref in xobjects.values():
                obj = ref.get_object()
                if obj.get("/Subtype") == "/Image":
                    widths.append((obj["/Width"], obj["/Height"]))
        self.assertTrue(any(abs(w / h - 820 / 222) < 0.05 for w, h in widths), widths)
