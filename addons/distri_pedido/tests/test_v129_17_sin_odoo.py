"""19.0.1.129.17: what 19.0.1.129.4 left showing «Odoo».

- no <meta name="generator" content="Odoo"> on the website pages;
- the tab icon of every page (backend, login, staff apps) and the icons of
  the home-screen apps are the brand icon, not the previous mark;
- portal documents without «Connect with your software!» («¿Quieres
  importar este documento a Odoo?», «Empieza en Odoo»);
- every translated text of the web client and the website says
  «DistriNeuquén» (static/src/js/marca_textos.js, both bundles).

The module's own «Odoo Online» texts (the previous system) stay: rows of the
imported history may show it, so the page checks skip that phrase.
"""
import re

from lxml import etree

from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import file_open

from .test_v129_4_marca import odoo_mentions, visible_text

MARCA = "/distri_pedido/static/src/img/marca/"
GENERATOR = re.compile(r"<meta[^>]+name=\"generator\"[^>]*>", re.I)
ODOO_ONLINE = re.compile(r"\bodoo online\b", re.I)


def new_system_mentions(page):
    """«Odoo» on `page` that is not the name of the previous system."""
    return odoo_mentions(ODOO_ONLINE.sub("sistema anterior", page))


@tagged("post_install", "-at_install", "distri_v129_17")
class TestSinOdooPaginas(HttpCase):

    def assert_clean(self, url, status=200):
        response = self.url_open(url)
        self.assertEqual(response.status_code, status, url)
        self.assertEqual(new_system_mentions(response.text), [], url)
        self.assertFalse(GENERATOR.search(response.text), url)
        self.assertNotIn("odoo.com", response.text.lower(), url)
        return response

    def test_public_pages_have_no_odoo(self):
        for url in ("/web/login", "/", "/pedido", "/web/reset_password"):
            self.assert_clean(url)

    def test_login_uses_the_brand_icon(self):
        page = self.assert_clean("/web/login").text
        self.assertIn('rel="icon" type="image/png" href="%sfavicon-32.png' % MARCA, page)
        self.assertIn('rel="apple-touch-icon" href="%sdn-icono-180.png' % MARCA, page)
        self.assertNotIn("img/iconos/app-192.png", page)

    def test_backend_uses_the_brand_icon(self):
        new_test_user(self.env, login="v12917_staff", password="v12917_staff_pw1",
                      groups="base.group_user,sales_team.group_sale_salesman")
        self.authenticate("v12917_staff", "v12917_staff_pw1")
        page = self.url_open("/odoo/discuss").text
        self.assertIn('rel="shortcut icon" href="%sfavicon-32.png' % MARCA, page)
        self.assertIn('rel="apple-touch-icon" href="%sdn-icono-180.png' % MARCA, page)
        self.assertEqual(new_system_mentions(page), [])
        self.assertFalse(GENERATOR.search(page))

    def test_app_manifests_use_the_brand_icon(self):
        for url in ("/tablero/manifest.webmanifest", "/cuentas/manifest.webmanifest",
                    "/web/manifest.webmanifest"):
            data = self.url_open(url).json()
            sources = [icon["src"] for icon in data["icons"]]
            self.assertTrue(sources, url)
            self.assertTrue(all(src.startswith(MARCA + "dn-icono-") for src in sources), (url, sources))
            self.assertNotRegex(data.get("name", ""), r"(?i)\bodoo\b")


@tagged("post_install", "-at_install", "distri_v129_17")
class TestSinOdooServidor(TransactionCase):

    def arch(self, xmlid):
        return etree.tostring(self.env.ref(xmlid)._get_combined_arch(), encoding="unicode")

    def test_portal_documents_without_connect_your_software(self):
        for xmlid in ("sale.sale_order_portal_template", "purchase.portal_my_purchase_order"):
            arch = self.arch(xmlid)
            self.assertNotIn("connect_software_modal", arch, xmlid)
            self.assertNotIn("odoo.com", arch, xmlid)
            self.assertNotRegex(visible_text("<div>%s</div>" % arch), r"\bOdoo\b", xmlid)

    def test_website_layout_without_generator(self):
        self.assertNotIn('name="generator"', self.arch("web.frontend_layout"))

    def test_translated_texts_carry_the_brand(self):
        for bundle in ("web.assets_backend", "web.assets_frontend"):
            files = self.env["ir.qweb"]._get_asset_bundle(bundle).files
            urls = [f.get("url") or f.get("filename") or "" for f in files]
            self.assertTrue(any(u.endswith("distri_pedido/static/src/js/marca_textos.js") for u in urls),
                            bundle)
        with file_open("distri_pedido/static/src/js/marca_textos.js") as f:
            source = f.read()
        self.assertIn("patch(TranslatedString.prototype", source)
        # links (Odoo.com) are left alone, plain words are replaced
        rule = re.search(r"replace\(/(.+)/g, BRAND\)", source).group(1)
        self.assertEqual(rule, r"\bOdoo\b(?!\.?\w)")
        word = re.compile(rule)
        self.assertEqual(word.sub("DistriNeuquén", "Instale Odoo"), "Instale DistriNeuquén")
        self.assertEqual(word.sub("DistriNeuquén", "Mi cuenta de Odoo.com"), "Mi cuenta de Odoo.com")
