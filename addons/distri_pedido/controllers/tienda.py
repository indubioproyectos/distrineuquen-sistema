from werkzeug.exceptions import NotFound

from odoo import http
from odoo.http import request
from odoo.addons.web.controllers.home import Home

try:
    from odoo.addons.website_sale.controllers.main import WebsiteSale
    from odoo.addons.website_sale.controllers.cart import Cart
except ImportError:  # sin website_sale no hay nada que ocultar
    WebsiteSale = Cart = None


if WebsiteSale is not None:

    class TiendaOculta(WebsiteSale):
        """La tienda que trae Odoo confunde: todo va a la pantalla de pedidos."""

        # 19.0.1.99.0 (audit F14): the sitemap drops the shop URLs in
        # models/ir_http.py (WebsiteSitemap); sitemap=False alone does not,
        # website_sale's own rules keep theirs
        @http.route(sitemap=False)
        def shop(self, **kw):
            return request.redirect("/pedido")

        @http.route(sitemap=False)
        def product(self, *args, **kw):
            return request.redirect("/pedido")


    class CarritoOculto(Cart):

        @http.route()
        def cart(self, **kw):
            return request.redirect("/pedido")


# What the login page sends a user to when they did not ask for a page:
# nothing, or the backend home (Odoo adds "/odoo?" when the session expired
# on the home page).
HOME_REDIRECTS = {"", "/", "/odoo", "/odoo?", "/web", "/web?", "/odoo/", "/web#"}


class InicioComerciante(Home):
    """Al entrar, el comercio va al catalogo, no al portal generico de Odoo.

    19.0.1.120.0: the whole staff (packers and drivers included) starts on
    Inicio (/tablero), with a tile for each screen it may open; a seller
    locked to the accounts app who neither packs nor delivers, on /cuentas.
    """

    @http.route()
    def web_login(self, redirect=None, **kw):
        """19.0.1.78.0: Odoo's own login logic, our clean page (only user and
        password; no website header, reset link or database selector)."""
        response = super().web_login(redirect=redirect, **kw)
        if getattr(response, "is_qweb", False) and response.template == "web.login":
            response.template = "distri_pedido.login_page"
        return response

    def _login_redirect(self, uid, redirect=None):
        usuario = request.env["res.users"].sudo().browse(uid)
        if not redirect:
            if usuario.distri_debe_cambiar_clave:
                return "/mi-clave"
            if usuario.has_group("base.group_portal"):
                return "/pedido"
        if (redirect or "") in HOME_REDIRECTS:
            landing = usuario._distri_landing()
            if landing:
                return landing
        return super()._login_redirect(uid, redirect=redirect)

    @http.route()
    def web_client(self, s_action=None, **kw):
        """The backend home without a menu or record: the staff goes to Inicio
        (/tablero), a seller locked to the accounts app to /cuentas."""
        uid = request.session.uid
        path = request.httprequest.path.rstrip("/")
        bare_home = path in ("/odoo", "/web") and not kw and not s_action \
            and not request.httprequest.query_string
        user = request.env["res.users"].sudo().browse(uid) if uid else None
        # a seller locked to the accounts app: every backend page, any menu or
        # record. 19.0.1.120.0: the bare home is decided below, so a locked
        # seller who also packs or delivers lands on Inicio, not on /cuentas
        if user and not bare_home and user._distri_seller_only_accounts():
            return request.redirect("/cuentas", 303)
        if user and bare_home:
            landing = user._distri_landing()
            if landing:
                return request.redirect(landing, 303)
            # 19.0.1.76.0: the rest of the staff starts on our tablero, not on Discuss
            if not user.share:
                return request.redirect("/tablero", 303)
        return super().web_client(s_action=s_action, **kw)


try:
    from odoo.addons.website.controllers.main import Website
except ImportError:
    Website = None


if Website is not None:

    class SitioLimpio(Website):
        """19.0.1.99.0 (audit F14): the stock pages of the website module
        that showed sample data or the installed apps."""

        @http.route(sitemap=False)
        def website_info(self, **kwargs):
            if not request.env.user.has_group("base.group_system"):
                raise NotFound()
            return super().website_info(**kwargs)


class ContactoReal(http.Controller):
    """/contactus was Odoo's sample page (invented address, +1 555 phone,
    sample e-mail): the real contact is the home page and WhatsApp."""

    @http.route(["/contactus", "/contactus-thank-you"], type="http", auth="public",
                website=True, sitemap=False)
    def contactus(self, **kw):
        return request.redirect("/", 301)
