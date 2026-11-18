"""19.0.1.104.0: the public website, phase 1 (owner decisions of 17/11/2026).

Home «/»: the DN mark, calls to action, catalog categories and brands
(owner review 18/11/2026: no Tabacalera Sarandí; Red Point, Master and Kiel
first; no Colibrí), delivery zones, contact from the
WhatsApp setting and the verbatim tobacco notice; no shop, cart or Odoo
sample content. /revendedor without a code is the public page. The sitemap
lists the new public pages only. Synthetic data only."""
import os
import re

from odoo.tests import HttpCase, tagged
from odoo.tools import file_path

from odoo.addons.distri_pedido.controllers import sitio
from odoo.addons.distri_pedido.controllers.kit import FILES

AVISO = ("EL FUMAR ES PERJUDICIAL PARA LA SALUD. Venta de productos de tabaco exclusiva a comercios "
         "y a mayores de 18 años (Ley 26.687). Prohibida su venta a menores de 18 años.")


def texto(html):
    """The visible text of a page, with single spaces."""
    html = re.sub(r"(?is)<(script|style)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()


@tagged("post_install", "-at_install", "distri_v104", "distri_sitio")
class TestSitioPublico(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.whatsapp_empresa", "5492990000104")
        params.set_param("distri_pedido.telefono_empresa", "2990000105")
        Zona = cls.env["distri.zona"].sudo()
        Zona.search([]).write({"active": False})
        Zona.create({"name": "Ciudad Prueba V104", "viaje": "1", "dia_martes": True,
                     "dia_jueves": True})
        Zona.create({"name": "Pueblo Encomienda V104", "viaje": "2", "encomienda": True})
        Zona.create({"name": "Retiro V104", "viaje": "2", "retira_deposito": True})

    def _home(self):
        res = self.url_open("/")
        self.assertEqual(res.status_code, 200)
        return res.text

    def test_public_pages_answer(self):
        for url in ("/", "/revendedor", "/pedido", "/web/login", "/sitemap.xml", "/robots.txt",
                    "/dn/sitio.css"):
            self.assertEqual(self.url_open(url).status_code, 200, url)
        self.assertEqual(self.url_open("/esto-no-existe-v104").status_code, 404)

    def test_no_sarandi_and_featured_brands_first(self):
        """Owner review 18/11/2026: no Tabacalera Sarandí band, card or name;
        Red Point, Master and Kiel first in «Marcas que distribuimos», as
        regular tiles of the brands grid."""
        for url in ("/", "/revendedor"):
            html = self.url_open(url).text
            self.assertNotIn("sarand", html.lower(), url)
            self.assertNotIn("colibr", html.lower(), url)
        html = self._home()
        body = html[html.index("<body"):]
        self.assertLess(body.index('class="cab"'), body.index("<main"), "the header comes first")
        marcas = body[body.index('id="marcas"'):]
        # same tiles as the other brands, first in the grid, no featured block
        self.assertNotIn("destacad", html)
        grid = marcas[marcas.index('class="logos"'):]
        alts = re.findall(r'alt="([^"]+)"', grid)
        self.assertEqual(alts[:3], ["Red Point", "Master", "Kiel"])
        for marca in ("Jugazo", "Coca-Cola", "Fernet Branca", "Fernandito"):
            self.assertIn(marca, texto(marcas) + marcas, marca)

    def test_brand_mark_and_no_old_wordmark(self):
        html = self._home()
        self.assertIn("/distri_pedido/static/src/img/iconos/dn-logo.svg", html)
        self.assertNotIn("KIOSCOS &amp; ALMACENES", html)
        self.assertNotIn("p01-447", html)
        self.assertIn('content="#10243f"', html)

    def test_legal_notice_verbatim(self):
        for url in ("/", "/revendedor"):
            page = texto(self.url_open(url).text)
            self.assertIn(AVISO, page, url)

    def test_calls_to_action(self):
        html = self._home()
        self.assertRegex(html, r'href="/pedido"[^>]*>Hacé tu pedido<')
        self.assertRegex(html, r'href="/revendedor"[^>]*>Soy distribuidora<')
        self.assertNotIn("Quiero ser revendedor", html)
        self.assertIn("https://wa.me/5492990000104?text=", html)
        self.assertIn("299 000-0104", texto(html), "the WhatsApp number as text")
        self.assertIn('href="tel:+542990000105"', html)
        # the staff login is in the footer only
        self.assertEqual(html.count('href="/web/login"'), 1)
        self.assertLess(html.index("<footer"), html.index('href="/web/login"'))

    def test_content_sections(self):
        page = texto(self._home())
        for titulo in [c[1] for c in sitio.CATEGORIAS]:
            self.assertIn(titulo, page)
        for titulo in ("Farmacia · Venta libre", "Gaseosas", "Vinos", "Jugos"):
            self.assertIn(titulo, page)
        self.assertNotIn("Medicamentos", page)
        self.assertNotIn(">Bebidas<", self._home())
        # owner review: no price lists on the public site
        for frase in ("listas de precios", "tu lista", "lista mayorista general",
                      "Las compras más chicas"):
            self.assertNotIn(frase, page)
        self.assertIn("Ciudad Prueba V104", page)
        self.assertIn("Martes, jueves", page)
        self.assertIn("Por encomienda: Pueblo Encomienda V104", page)
        self.assertNotIn("Retiro V104", page, "pick-up zones are not delivery zones")
        self.assertIn("Compra mínima $ 70.000", page)

    def test_seo_basics(self):
        html = self._home()
        self.assertIn('<html lang="es-AR">', html)
        self.assertRegex(html, r"<title>[^<]*Distri Neuquén[^<]*</title>")
        self.assertRegex(html, r'<meta name="description" content="[^"]{60,}"')
        self.assertIn('<meta name="robots" content="index,follow"', html)
        self.assertIn('property="og:image" content="', html)
        self.assertIn("og-distri-neuquen-2.jpg", html)
        self.assertIn("portada2-1280.webp", html)
        self.assertIn('property="og:locale" content="es_AR"', html)
        self.assertIn('rel="canonical"', html)
        self.assertIn('"@type": "WholesaleStore"', html)

    def test_no_shop_odoo_or_sample_content(self):
        for url in ("/", "/revendedor"):
            html = self.url_open(url).text
            lower = html.lower()
            for bad in ("/shop", "o_wsale", "/my/cart", "o_header", "powered by", "odoo",
                        "555-555", "my company", "somos un equipo", "yourcompany",
                        "/contactus", "/terms"):
                self.assertNotIn(bad, lower, "%s: %s" % (url, bad))

    def test_images_exist_and_are_optimised(self):
        html = self._home()
        urls = set(re.findall(r'/distri_pedido/static/src/sitio/img/[\w.-]+', html))
        self.assertGreater(len(urls), 40)
        for url in urls:
            path = file_path(url.lstrip("/"))
            self.assertLess(os.path.getsize(path), 220 * 1024, url)
        self.assertIn('type="image/webp"', html)
        self.assertIn('loading="lazy"', html)
        self.assertIn('fetchpriority="high"', html)

    def test_css_served_by_the_kit(self):
        self.assertIn("sitio.css", FILES)
        res = self.url_open("/dn/sitio.css")
        self.assertIn("text/css", res.headers["Content-Type"])
        self.assertIn(".logo-txt", res.text)

    def test_sitemap_lists_the_new_pages_only(self):
        self.env["ir.attachment"].sudo().search([("url", "=like", "/sitemap-%.xml")]).unlink()
        res = self.url_open("/sitemap.xml")
        self.assertEqual(res.status_code, 200)
        locs = re.findall(r"<loc>([^<]+)</loc>", res.text)
        paths = {re.sub(r"^https?://[^/]+", "", loc) for loc in locs}
        self.assertIn("/", paths)
        self.assertIn("/revendedor", paths)
        for path in paths:
            self.assertFalse(path.startswith(("/shop", "/contactus", "/terms", "/website/info",
                                              "/pedido/")), path)

    def test_reseller_public_page(self):
        res = self.url_open("/revendedor")
        self.assertEqual(res.status_code, 200)
        page = texto(res.text)
        # v106: only for other distributors; stores order on /pedido
        self.assertIn("¿Sos distribuidora?", page)
        self.assertIn("Si sos una distribuidora y querés trabajar con nosotros", page)
        self.assertIn("kiosco o almacén", page)
        self.assertIn('href="/pedido">Hacé tu pedido<', res.text)
        self.assertNotIn("revendedor", page.lower().replace("/revendedor", ""))
        self.assertIn("https://wa.me/5492990000104?text=Hola%21%20Soy%20de%20una%20distribuidora",
                      res.text)
        self.assertNotIn("/web/signup", res.text)
        self.assertIn('class="cab"', res.text)
        for frase in ("listas de precios", "tu lista", "lista mayorista general"):
            self.assertNotIn(frase, page)

    def test_phone_text(self):
        self.assertEqual(sitio.telefono_visible("5492994646085"), "299 464-6085")
        self.assertEqual(sitio.telefono_visible("2995797248"), "299 579-7248")
        self.assertEqual(sitio.telefono_visible("123"), "123")

    def test_migration_moves_the_retired_domain(self):
        import importlib.util
        path = file_path("distri_pedido/migrations/19.0.1.104.0/post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v104_post_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        params = self.env["ir.config_parameter"].sudo()
        params.set_param("web.base.url", "https://sitio-prueba-v104.example")
        website = self.env["website"].sudo().create({"name": "Sitio V104",
                                                      "domain": "https://viejo.piadevs.com"})
        other = self.env["website"].sudo().create({"name": "Otro V104",
                                                    "domain": "https://otro-v104.example"})
        module.migrate(self.env.cr, "19.0.1.103.0")
        self.env.invalidate_all()
        self.assertEqual(website.domain, "https://sitio-prueba-v104.example")
        self.assertEqual(other.domain, "https://otro-v104.example")
