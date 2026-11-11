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

        @http.route()
        def shop(self, **kw):
            return request.redirect("/pedido")

        @http.route()
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

    El armador y el repartidor van directo a su pantalla (/armado y
    /reparto): no usan el backend. Administración, gerencia y los
    administradores del sistema siguen entrando a Odoo.
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
        """The backend home without a menu or record: armador and repartidor
        go to their screen."""
        uid = request.session.uid
        path = request.httprequest.path.rstrip("/")
        if uid and path in ("/odoo", "/web") and not kw and not s_action \
                and not request.httprequest.query_string:
            user = request.env["res.users"].sudo().browse(uid)
            landing = user._distri_landing()
            if landing:
                return request.redirect(landing, 303)
            # 19.0.1.76.0: the rest of the staff starts on our tablero, not on Discuss
            if not user.share:
                return request.redirect("/tablero", 303)
        return super().web_client(s_action=s_action, **kw)
