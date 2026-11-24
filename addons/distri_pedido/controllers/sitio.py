"""The public website of Distri Neuquén (19.0.1.104.0, owner decisions of 17/11/2026).

«/» is the home page: the DN brand header, the cover, who we are, product
categories and brands of the catalog (Red Point, Master and Kiel first), how to order («Hacé tu pedido» -> /pedido, «Soy distribuidora» ->
/revendedor, only for other distributors), delivery zones (distri.zona), contact and the tobacco legal
notice in the footer. /revendedor without an invitation code shows the
public «¿Sos distribuidora?» page of controllers/alta_revendedor.py with
the same layout.

Everything lives in distri_pedido, next to the data it shows (the WhatsApp
setting, the zones, FICHA, the order page and the reseller signup) and the
overrides that already clean the public site (controllers/tienda.py, the
own 403/404 and the sitemap in models/ir_http.py). distri_tema styles the
backend and depends neither on website nor on distri_pedido.

Pages are standalone QWeb documents (views/sitio.xml): no website layout, so
no shop, cart, sample footer or Odoo branding. Styles: static/src/sitio/
sitio.css, served by the kit route (/dn/sitio.css, immutable, versioned).
Images: static/src/sitio/img (WebP with a JPEG/PNG fallback, built by
/srv/odoo-lab/scripts/sitio/imagenes.py); SITIO_V versions their URLs.
"""
import json
from datetime import date

from markupsafe import Markup

from odoo import http
from odoo.http import request

from ..models.marca import marca_publica
from .kit import kit_url
from .main import COMPRA_MINIMA, ficha
from .reparto import fonts

# query of the site's images: bump it when static/src/sitio/img changes
# (Odoo serves static files with a one-week cache, Cloudflare too)
SITIO_V = "?v=107"
IMG = "/distri_pedido/static/src/sitio/img/"
LOGO = "/distri_pedido/static/src/img/iconos/"
LOGO_V = "?v=95"

# The verbatim legal notice for tobacco products (owner, 17/11/2026).
AVISO_TABACO_TITULO = "EL FUMAR ES PERJUDICIAL PARA LA SALUD."
AVISO_TABACO = ("Venta de productos de tabaco exclusiva a comercios y a mayores de 18 años "
                "(Ley 26.687). Prohibida su venta a menores de 18 años.")

# Sections of the December 2024 catalog: (image name, title, examples).
# 19.0.1.104.0 owner review: one «Farmacia · Venta libre» section and
# «Bebidas» split into Gaseosas, Vinos and Jugos.
CATEGORIAS = [
    # 19.0.1.129.16 (owner, pendiente 55): no brands in the cigarettes card
    ("cigarrillos", "Cigarrillos", "Las marcas más vendidas, en atado y en caja"),
    ("tabaco", "Tabaco", "Van Kiff, Cuatro Leguas"),
    ("insumos", "Insumos para armar", "Papelillos, filtros y máquinas"),
    ("golosinas", "Golosinas", "Alfajores, chicles, caramelos y chocolates"),
    ("galletitas", "Galletitas", "Dulces, saladas y bizcochos"),
    ("gaseosas", "Gaseosas", "Coca-Cola y más"),
    ("vinos", "Vinos", "Tintos, blancos y espumantes"),
    ("jugos", "Jugos", "Jugazo y más"),
    ("farmacia", "Farmacia · Venta libre", "Analgésicos y antigripales"),
    ("perfumeria", "Perfumería", "Higiene personal y cuidado"),
    ("varios", "Varios", "Bazar, librería y regalería"),
    ("encendedores", "Encendedores", "Bic, Candela, Shark y recargas"),
    ("afeitadoras", "Afeitadoras", "Gillette y hojas de afeitar"),
    ("baterias", "Baterías", "Duracell y Energizer"),
    ("pegamentos", "Pegamentos", "Poxipol, Poxilina, La Gotita"),
]
# 19.0.1.122.0 (owner): sections kept in CATEGORIAS but not shown as a card
# on the home page, by their stable key (the image name), never by title.
# /pedido lists the real product categories and is not affected.
CATEGORIAS_OCULTAS_INICIO = frozenset({"farmacia"})


def categorias_inicio():
    """The category cards of the home page."""
    return [dict(imagen("cat-" + key, "jpg"), titulo=titulo, ejemplos=ejemplos)
            for key, titulo, ejemplos in CATEGORIAS if key not in CATEGORIAS_OCULTAS_INICIO]


# Brands (owner review of 18/11/2026): the best-known cigarette brands
# first, with the same tile as the rest (cut from the catalog pages); then
# the logos found in the catalog; then brands without a logo in the
# catalog, as wordmark tiles until the owner sends official logos.
MARCAS = [
    ("logo-red-point", "Red Point"), ("logo-master", "Master"), ("logo-kiel", "Kiel"),
    ("logo-bold", "Bold"), ("logo-parnor", "ParNor"), ("logo-trio", "Trio"),
    ("logo-club-social", "Club Social"), ("logo-rocklets", "Rocklets"), ("logo-halls", "Halls"),
    ("logo-beldent", "Beldent"), ("logo-jugazo", "Jugazo"),
]
MARCAS_TEXTO = ["Coca-Cola", "Fernet Branca", "Fernandito"]
# more brands of the catalog, as text
OTRAS_MARCAS = ["Bonafide", "Terrabusi", "Oreo", "Bagley", "Billiken", "Duracell", "Energizer",
                "Gillette", "Bic", "Poxipol", "La Gotita", "OCB", "Colgate", "Sedal", "Rexona",
                "Bayaspirina", "Tafirol"]

TEXTO_CONSULTA = "Hola! Quiero hacer una consulta."
TEXTO_REVENDEDOR = "Hola! Soy de una distribuidora y quiero trabajar con Distri Neuquén. Mi empresa es: "


def foto(name, widths, ext="jpg"):
    """srcset of one photo in several widths (WebP and a fallback)."""
    webp = ", ".join("%s%s-%d.webp%s %dw" % (IMG, name, w, SITIO_V, w) for w in widths)
    other = ", ".join("%s%s-%d.%s%s %dw" % (IMG, name, w, ext, SITIO_V, w) for w in widths)
    return {"webp": webp, "srcset": other,
            "src": "%s%s-%d.%s%s" % (IMG, name, widths[-1], ext, SITIO_V)}


def imagen(name, ext):
    """One fixed-size image: WebP and its fallback."""
    return {"webp": "%s%s.webp%s" % (IMG, name, SITIO_V), "src": "%s%s.%s%s" % (IMG, name, ext, SITIO_V)}


def telefono_visible(digits):
    """«299 464-6085» for an Argentine number in any usual form, else as is."""
    from ..models.ar_phone import ar_national, format_local, split_ar_national
    national = ar_national(digits)
    if not national:
        return digits
    area, number = split_ar_national(national)
    return "%s %s" % (area, format_local(number))


def telefono_tel(value):
    """«tel:» link of the public phone (+54 and the 10 national digits)."""
    from ..models.ar_phone import ar_national
    national = ar_national(value)
    return "tel:+54" + national if national else ""


def zonas_publicas(env):
    """Delivery zones for the home page: own delivery with its days, and the
    towns served by courier («encomienda»). Zones whose customers pick up at
    the depot are not listed. [] when there are none."""
    reparto, encomienda = [], []
    for zona in env["distri.zona"].sudo().search([]):
        if zona.encomienda:
            encomienda.append(zona.name)
        elif not zona.retira_deposito and zona.dias_texto != "sin días":
            dias = zona.dias_texto
            reparto.append({"ciudad": zona.name, "dias": dias[:1].upper() + dias[1:]})
    return {"reparto": reparto, "encomienda": encomienda}


def contexto_sitio(page_title, description, path):
    """Values shared by every page of the public site (views/sitio.xml
    sitio_layout)."""
    env = request.env
    settings = env["distri.pedido.whatsapp.settings"].sudo()
    whatsapp = settings.company_number()
    datos = ficha()
    company = env.company.sudo()
    base = (env["ir.config_parameter"].sudo().get_param("web.base.url")
            or request.httprequest.host_url).rstrip("/")
    direccion = ", ".join(x for x in (company.street, company.street2, company.city) if x)
    marca = marca_publica(base)
    return {
        "titulo": page_title,
        "descripcion": description,
        "base_url": base,
        "canonical": base + path,
        "css": kit_url("sitio.css"),
        "font": (fonts() or [""])[0],
        "img": IMG,
        "v": SITIO_V,
        "logo": LOGO,
        "logo_v": LOGO_V,
        # 19.0.1.129.12: the link preview is the new brand logo
        "marca": marca,
        "og_image": marca["og_image"],
        "empresa": datos["nombre"],
        "ficha": datos,
        "whatsapp": whatsapp,
        "whatsapp_texto": telefono_visible(whatsapp) if whatsapp else "",
        "whatsapp_url": settings.company_url(TEXTO_CONSULTA) if whatsapp else "",
        "whatsapp_revendedor": settings.company_url(TEXTO_REVENDEDOR) if whatsapp else "",
        "telefono_tel": telefono_tel(datos["telefono"]),
        "telefono_texto": telefono_visible(datos["telefono"]) if datos["telefono"] else "",
        "email": company.email or "",
        "direccion": direccion,
        "aviso_titulo": AVISO_TABACO_TITULO,
        "aviso": AVISO_TABACO,
        "anio": date.today().year,
        "path": path,
    }


def jsonld(values):
    """schema.org data of the business for search engines (home page)."""
    zonas = values["zonas"]
    data = {
        "@context": "https://schema.org",
        "@type": "WholesaleStore",
        "name": values["empresa"],
        "description": values["descripcion"],
        "url": values["base_url"] + "/",
        "logo": values["base_url"] + values["marca"]["icono_512"],
        "image": values["og_image"],
        "address": {"@type": "PostalAddress", "addressLocality": values["ficha"]["localidad"],
                    "addressRegion": "Neuquén", "addressCountry": "AR"},
        "areaServed": [z["ciudad"] for z in zonas["reparto"]] + zonas["encomienda"],
    }
    if values["whatsapp"]:
        data["telephone"] = "+" + values["whatsapp"]
    if values["email"]:
        data["email"] = values["email"]
    if values["direccion"]:
        data["address"]["streetAddress"] = values["direccion"]
    # inside <script>: no "</" may close it early
    return Markup(json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))


class SitioPublico(http.Controller):

    @http.route("/", type="http", auth="public", website=True, sitemap=True)
    def inicio(self, **kw):
        values = contexto_sitio(
            "Distri Neuquén · Distribuidora mayorista para kioscos y almacenes en Neuquén",
            # 19.0.1.127.0 (owner): no brands nor «farmacia» in the description
            "Distribuidora mayorista en Neuquén. Cigarrillos, tabaco, golosinas, galletitas, "
            "gaseosas, vinos y más para kioscos y almacenes de Neuquén y la región. "
            "Hacé tu pedido online.",
            "/")
        values.update({
            "portada": foto("portada3", (640, 960, 1280)),
            "comercio": foto("comercio", (480, 800)),
            "reparto_foto": foto("reparto", (480, 800)),
            "categorias": categorias_inicio(),
            "marcas_texto": MARCAS_TEXTO,
            "marcas": [dict(imagen(key, "png"), nombre=nombre) for key, nombre in MARCAS],
            "otras_marcas": OTRAS_MARCAS,
            "zonas": zonas_publicas(request.env),
            "compra_minima_texto": "{:,.0f}".format(COMPRA_MINIMA).replace(",", "."),
        })
        values["jsonld"] = jsonld(values)
        return request.render("distri_pedido.inicio", values)
