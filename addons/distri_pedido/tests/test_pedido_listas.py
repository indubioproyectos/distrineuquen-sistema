"""Public and hidden price lists on /pedido (management decision of 29/09/2026).

The public sees only the General list («mayorista general», never the word
«minorista»). The hidden list (Distribuidoras: «May» prices and cigarettes to
be priced) reaches the page and is accepted on submit only for Gerencia,
Administración and logged-in customers whose company is flagged
distri_lista_oculta. Sellers see only the General list, like any shop, and do
not handle distributors (rule of 30/09/2026, models/lista_oculta.py). Prices
are always computed on the server. Synthetic data only.
"""
import json
import re

from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import HttpCase, JsonRpcException, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import (
    CATEGORIAS_A_PRESUPUESTAR, DISTRIBUIDORAS, LISTA_GENERAL, LISTA_OCULTA,
    MENSAJE_DISTRIBUIDORA,
)
from odoo.addons.distri_pedido.models.alta_revendedor import invite_code
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido

PRECIO_GENERAL = 5123.0
PRECIO_MAY = 3777.77          # shown as "3.778", sent as data-precio="3777.77"
PRECIO_CIGARRILLO = 8888.0
PRECIO_REVENDEDOR = 7654.0


@tagged("post_install", "-at_install", "distri_pedido_listas")
class TestPedidoListas(PedidoAbiertoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Pricelist = cls.env["product.pricelist"]
        currency = cls.env.company.currency_id.id
        # 19.0.1.129.26 (#109): production has «Listas de precios» on; a fresh
        # database does not
        cls.env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        cls.general = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL, "currency_id": currency})
        cls.oculta = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA, "currency_id": currency,
                              "distri_mayorista": True})
        Categ = cls.env["product.category"]
        cls.categ_cig = Categ.search([("name", "=", CATEGORIAS_A_PRESUPUESTAR[0])], limit=1) or \
            Categ.create({"name": CATEGORIAS_A_PRESUPUESTAR[0]})
        base = {"type": "consu", "sale_ok": True, "is_published": True,
                "taxes_id": [Command.clear()]}
        Tmpl = cls.env["product.template"]
        cls.remedio = Tmpl.create(dict(base, name="Remedio Pruebalistas x 10",
                                       list_price=PRECIO_GENERAL))
        cls.cigarrillo = Tmpl.create(dict(base, name="Cigarrillo Pruebalistas 20",
                                          list_price=PRECIO_CIGARRILLO,
                                          categ_id=cls.categ_cig.id))
        cls.env["product.pricelist.item"].create({
            "pricelist_id": cls.oculta.id, "applied_on": "1_product",
            "product_tmpl_id": cls.remedio.id, "compute_price": "fixed",
            "fixed_price": PRECIO_MAY,
        })
        # a reseller list built on the hidden one, with its cigarette price
        cls.revendedor = Pricelist.create({
            "name": "Revendedor Pruebalistas", "currency_id": currency, "distri_mayorista": True,
            "item_ids": [
                Command.create({"applied_on": "1_product", "product_tmpl_id": cls.cigarrillo.id,
                                "compute_price": "fixed", "fixed_price": PRECIO_REVENDEDOR}),
                Command.create({"applied_on": "3_global", "compute_price": "formula",
                                "base": "pricelist", "base_pricelist_id": cls.oculta.id}),
            ],
        })
        Partner = cls.env["res.partner"]
        # authorized distributor with the General list: sees the section
        cls.distribuidora = Partner.create({
            "name": "Distribuidora Pruebalistas", "company_type": "company",
            "distri_lista_oculta": True, "property_product_pricelist": cls.general.id})
        # authorized reseller with its own hidden list: sees its prices
        cls.revendedora = Partner.create({
            "name": "Revendedora Pruebalistas", "company_type": "company",
            "distri_lista_oculta": True, "property_product_pricelist": cls.revendedor.id})
        # a hidden list assigned but no authorization: sees the General list only
        cls.kiosco = Partner.create({
            "name": "Kiosco Pruebalistas", "company_type": "company",
            "property_product_pricelist": cls.revendedor.id})
        cls._portal("distri_listas", cls.distribuidora)
        cls._portal("revende_listas", cls.revendedora)
        cls._portal("kiosco_listas", cls.kiosco)
        cls.cartera = cls.env["res.partner.category"].create({"name": "Cartera Pruebalistas"})
        # a shop of the seller's portfolio, with the General list
        cls.almacen = Partner.create({
            "name": "Almacen Pruebalistas", "company_type": "company",
            "category_id": [Command.link(cls.cartera.id)],
            "property_product_pricelist": cls.general.id})
        cls.vendedor = new_test_user(cls.env, login="vendedor_listas", name="Vendedor Listas",
                                     groups="base.group_user,distri_pedido.grupo_vendedor")
        cls.vendedor.etiqueta_cartera_id = cls.cartera
        # the distributor is in the portfolio too: even so the seller cannot
        # give it a hidden list or handle its orders
        cls.distribuidora.category_id = [Command.link(cls.cartera.id)]
        cls.administracion = new_test_user(
            cls.env, login="admin_listas", name="Administracion Listas",
            groups="base.group_user,distri_pedido.grupo_administracion")
        cls.gerencia = new_test_user(
            cls.env, login="gerencia_listas", name="Gerencia Listas",
            groups="base.group_user,distri_pedido.grupo_gerencia")

    @classmethod
    def _portal(cls, login, company):
        # the contact of the company logs in: the flag is read on the company
        contacto = cls.env["res.partner"].create({
            "name": "Contacto %s" % login, "parent_id": company.id, "type": "contact"})
        new_test_user(cls.env, login=login, groups="base.group_portal",
                      partner_id=contacto.id)

    # ------------------------------------------------------------ helpers

    def _pedido_html(self, url="/pedido"):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200)
        return res.text

    def _assert_publica(self, html):
        """The page of a visitor without authorization."""
        self.assertNotIn("inorista", html.lower())
        self.assertNotIn(DISTRIBUIDORAS["nombre"], html)
        self.assertNotIn(DISTRIBUIDORAS["detalle"], html)
        self.assertNotIn("Cigarrillos y tabaco a presupuestar", html)
        self.assertNotIn("data-may", html)
        self.assertNotIn('name="modo"', html)
        # 19.0.1.129.35: the CSRF token (hex + "o" + timestamp) and the form
        # id (`envio`, hex) are random and may contain these digits
        # (…o1827147654): they are taken out before looking for the prices
        sin_tokens = re.sub(r"[0-9a-f]{16,}(o\d+)?", "", html)
        for precio in ("3777.77", "3.778", "7654", "7.654", "8888", "8.888"):
            self.assertNotIn(precio, sin_tokens)
        # the General price is there, and the cigarette has no public price
        self.assertIn('data-id="%s" data-precio="%s"' % (self.remedio.id, PRECIO_GENERAL), html)
        # a zero price is not even rendered: QWeb drops the falsy attribute
        self.assertRegex(html, r'data-id="%s"(\s+data-precio="0(\.0)?")?\s+data-sinprecio="1"'
                         % self.cigarrillo.id)

    def _enviar(self, **extra):
        data = {
            "renglones": json.dumps([
                {"id": self.remedio.id, "cantidad": 20, "may": 1},
                {"id": self.cigarrillo.id, "cantidad": 1, "may": 1},
            ]),
            "nombre": "Comprador Pruebalistas %s" % len(extra),
            "telefono": "2995550101", "domicilio": "Calle Falsa 123", "entrega": "domicilio",
        }
        data.update(extra)
        res = post_pedido(self, data)
        self.assertEqual(res.status_code, 200)
        return self.env["sale.order"].search(
            [("order_line.product_id", "=", self.remedio.product_variant_id.id)],
            order="id desc", limit=1)

    def _csrf(self):
        return re.search(r'name="csrf_token" value="([^"]+)"',
                         self._pedido_html()).group(1)

    def _pedido_de_distribuidora(self):
        pedido = self.env["sale.order"].create({
            "partner_id": self.distribuidora.id, "pricelist_id": self.oculta.id,
            "order_line": [Command.create({"product_id": self.remedio.product_variant_id.id,
                                           "product_uom_qty": 1})],
        })
        pedido._portal_ensure_token()
        return pedido

    def _precio(self, pedido, plantilla):
        return pedido.order_line.filtered(
            lambda l: l.product_id.product_tmpl_id == plantilla).price_unit

    # -------------------------------------------------------------- tests

    def test_anonymous_page_shows_only_the_general_list(self):
        html = self._pedido_html()
        self._assert_publica(html)
        # 19.0.1.129.15: the owner removed the «lista mayorista general» line
        self.assertNotIn("Precios de la lista mayorista general", html)

    def _invite(self):
        return self.env["distri.reseller.invite"].with_user(self.gerencia).create({})

    def _invite_url(self):
        return "/revendedor?c=%s" % invite_code(self.env)

    def test_reseller_signup_is_private(self):
        """19.0.1.93.0: no public «Soy revendedor» on /pedido; the signup
        only with the invitation link; noindex. 19.0.1.104.0: without the
        code /revendedor is the public «¿Sos distribuidora?» page of the
        website, with no signup link and nothing in the session."""
        html = self._pedido_html()
        self._assert_publica(html)
        self.assertNotIn("Soy revendedor", html)
        self.assertNotIn('href="/revendedor', html)
        login = self.url_open("/web/login?redirect=%2Fpedido")
        self.assertEqual(login.status_code, 200)
        self.assertNotIn("Revendedores y distribuidoras", login.text)
        code = invite_code(self.env)
        # 19.0.1.129.42: the wrong code always differs from the real one (it
        # used to be «last char -> x», the real code when it already ended in
        # x); the upper-case variant only when it is another text
        wrong = code[:-1] + ("y" if code.endswith("x") else "x")
        urls = ["/revendedor", "/revendedor?c=", "/revendedor?c=%s" % wrong]
        if code.upper() != code:
            urls.append("/revendedor?c=%s" % code.upper())
        self.assertNotEqual(wrong, code)
        for url in urls:
            res = self.url_open(url)
            self.assertEqual(res.status_code, 200, url)
            self.assertIn("¿Sos distribuidora?", res.text, url)
            self.assertNotIn("/web/signup", res.text, url)
            self.assertNotIn("Crear cuenta con mi correo", res.text, url)
        # no free signup without the invitation in the session
        self.env["ir.config_parameter"].sudo().set_param("auth_signup.invitation_scope", "b2c")
        self.assertEqual(self.url_open("/web/signup?redirect=%2Fmi-perfil").status_code, 404)
        res = self.url_open("/web/signup", data={
            "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', login.text).group(1),
            "login": "sininvitacion@prueba.example", "name": "Sin Invitacion",
            "password": "Clave-Segura-2026", "confirm_password": "Clave-Segura-2026"})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(self.env["res.users"].search([("login", "=", "sininvitacion@prueba.example")]))

        res = self.url_open(self._invite_url())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers.get("X-Robots-Tag"), "noindex, nofollow")
        page = res.text
        self.assertIn('name="robots" content="noindex,nofollow"', page)
        self.assertIn("Crear cuenta con mi correo", page)
        self.assertIn('href="/web/signup?redirect=%2Fmi-perfil"', page)
        self.assertIn('href="/web/login?redirect=%2Fpedido"', page)
        number = self.env["distri.pedido.whatsapp.settings"].sudo().company_number()
        self.assertIn("https://wa.me/%s?text=" % number, page)
        self.assertNotIn("Entrar con Google", page, "the provider is disabled")
        self.assertEqual(self.url_open("/web/signup?redirect=%2Fmi-perfil").status_code, 200)

    def test_invite_link_for_managers_and_regenerate(self):
        """«Invitar revendedor»: the link and the WhatsApp message; a new code
        turns the old link into the public page (19.0.1.104.0; a 404 before)."""
        invite = self._invite()
        old = invite_code(self.env)
        self.assertTrue(invite.link.endswith("/revendedor?c=%s" % old), invite.link)
        self.assertGreaterEqual(len(old), 16)
        self.assertIn(invite.link, invite.message)
        self.assertIn("no lo compartas", invite.message)
        with self.assertRaises(AccessError):
            self.env["distri.reseller.invite"].with_user(self.vendedor).create({})
        with self.assertRaises(AccessError):
            invite.with_user(self.vendedor).action_regenerate()
        action = self.env["res.partner"].with_user(self.administracion) \
            .action_distri_reseller_invite()
        self.assertEqual(action["res_model"], "distri.reseller.invite")
        invite.action_regenerate()
        new = invite_code(self.env)
        self.assertNotEqual(new, old)
        res = self.url_open("/revendedor?c=%s" % old)
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("Crear cuenta con mi correo", res.text)
        res = self.url_open("/revendedor?c=%s" % new)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Crear cuenta con mi correo", res.text)

    def test_signup_needs_the_current_invitation(self):
        """The code is kept in the session; a regenerated code closes the
        signup of that session too (email and Google use the same check)."""
        self.env["ir.config_parameter"].sudo().set_param("auth_signup.invitation_scope", "b2c")
        self.url_open(self._invite_url())
        page = self.url_open("/web/signup?redirect=%2Fmi-perfil")
        self.assertEqual(page.status_code, 200)
        self._invite().action_regenerate()
        res = self.url_open("/web/signup", data={
            "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1),
            "login": "vencida@prueba.example", "name": "Invitacion Vencida",
            "password": "Clave-Segura-2026", "confirm_password": "Clave-Segura-2026"})
        self.assertEqual(res.status_code, 404)
        self.assertFalse(self.env["res.users"].search([("login", "=", "vencida@prueba.example")]))
        # outside an HTTP request (shell, scripts) nothing changes
        self.env["res.users"].sudo().signup({
            "login": "desdeshell@prueba.example", "name": "Desde Shell",
            "password": "Clave-Segura-2026"})
        self.assertTrue(self.env["res.users"].search([("login", "=", "desdeshell@prueba.example")]))

    def test_google_button_only_while_the_provider_is_enabled(self):
        google = self.env.ref("auth_oauth.provider_google")
        google.write({"enabled": True, "client_id": "prueba-123.apps.googleusercontent.com"})
        page = self._pedido_html(self._invite_url())
        self.assertIn("Entrar con Google", page)
        self.assertIn("https://accounts.google.com/o/oauth2/auth?", page)
        self.assertIn("client_id=prueba-123.apps.googleusercontent.com", page)
        self.assertIn("auth_oauth%2Fsignin", page)
        google.client_id = False
        self.assertNotIn("Entrar con Google", self._pedido_html(self._invite_url()))

    # ------------------------------------------------ self-signup (19.0.1.92.0)

    PROFILE = {"comercio": "Kiosco Autoalta", "contacto": "Juan Autoalta",
               "cuit": "20-12345678-6", "telefono": "299 4222444",
               "direccion": "Calle Falsa 742", "localidad": "Plottier"}

    def _signup(self, email):
        self.env["ir.config_parameter"].sudo().set_param("auth_signup.invitation_scope", "b2c")
        # 19.0.1.93.0: only after the invitation link
        self.assertEqual(self.url_open(self._invite_url()).status_code, 200)
        page = self.url_open("/web/signup?redirect=%2Fmi-perfil")
        self.assertEqual(page.status_code, 200)
        res = self.url_open("/web/signup", data={
            "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1),
            "login": email, "name": "Juan Autoalta", "password": "Clave-Segura-2026",
            "confirm_password": "Clave-Segura-2026", "redirect": "/mi-perfil"})
        self.assertEqual(res.status_code, 200)
        self.assertTrue(res.url.endswith("/mi-perfil"), res.url)
        user = self.env["res.users"].search([("login", "=", email)])
        self.assertTrue(user.share)
        return user, res.text

    def _profile(self, html, **values):
        data = dict(self.PROFILE, **values)
        data["csrf_token"] = re.search(r'name="csrf_token" value="([^"]+)"', html).group(1)
        return self.url_open("/mi-perfil", data=data)

    def test_signup_profile_pending_then_enabled(self):
        user, html = self._signup("autoalta@prueba.example")
        partner = user.partner_id
        self.assertEqual(partner.distri_alta_estado, "perfil")
        self.assertIn("Mi perfil de revendedor", html)
        # /pedido sends him to the profile until it is complete
        res = self.url_open("/pedido", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertTrue(res.headers["Location"].endswith("/mi-perfil"))

        res = self._profile(html)
        self.assertTrue(res.url.endswith("/mi-perfil?listo=1"), res.url)
        self.assertIn("Recibimos tus datos", res.text)
        self.assertIn("Avisar por WhatsApp", res.text)
        self.assertIn("CUIT%3A%2020123456786", res.text)
        partner.invalidate_recordset()
        self.assertEqual(partner.distri_alta_estado, "pendiente")
        self.assertEqual((partner.name, partner.distri_alta_contacto, partner.vat, partner.city),
                         ("Kiosco Autoalta", "Juan Autoalta", "20123456786", "Plottier"))
        self.assertEqual(partner.distri_whatsapp, "+5492994222444")
        self.assertFalse(partner.distri_lista_oculta)
        # 19.0.1.93.0: one activity for the partner, for a Gerencia user
        activity = partner.activity_ids
        self.assertEqual(len(activity), 1)
        self.assertEqual(activity.summary, "Alta de revendedor pendiente: Kiosco Autoalta")
        self.assertTrue(activity.user_id.has_group("distri_pedido.grupo_gerencia"))
        # still pending: he may correct his data, without a second activity
        res = self._profile(self.url_open("/mi-perfil").text, direccion="Calle Falsa 744")
        self.assertTrue(res.url.endswith("/mi-perfil?listo=1"), res.url)
        partner.invalidate_recordset()
        self.assertEqual(partner.street, "Calle Falsa 744")
        self.assertEqual(len(partner.activity_ids), 1)
        self.assertIn(partner, self.env["res.partner"].search(
            [("distri_alta_estado", "=", "pendiente")]), "listed in «Revendedores pendientes»")

        # pending: the catalog, with the General list only (no hidden price)
        html = self._pedido_html()
        self._assert_publica(html)
        self.assertNotIn("Soy revendedor / Soy distribuidora", html)

        # Gerencia enables him with his list
        action = partner.with_user(self.administracion).action_distri_enable_reseller()
        wizard = self.env[action["res_model"]].with_user(self.administracion).with_context(
            action["context"]).create({"pricelist_id": self.revendedor.id})
        wizard.action_confirm()
        self.assertEqual(partner.distri_alta_estado, "habilitado")
        self.assertTrue(partner.distri_lista_oculta)
        self.assertEqual(partner.property_product_pricelist, self.revendedor)
        self.assertFalse(partner.activity_ids.filtered(
            lambda a: (a.summary or "").startswith("Alta de revendedor")))
        self.assertIn("habilitado", partner.message_ids[:1].body)
        self.assertNotIn(partner, self.env["res.partner"].search(
            [("distri_alta_estado", "=", "pendiente")]))
        self.assertIn("(lista Revendedor Pruebalistas)", self._pedido_html())

    def test_profile_validation_and_reject(self):
        # many submissions in a row on purpose: not the rate limit under test
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.perfil_limite_sesion_10min", "100")
        user, html = self._signup("rechazo@prueba.example")
        for values, message in (({"cuit": "123"}, "11 números"),
                                ({"cuit": "20123456780"}, "no es válido"),
                                ({"comercio": "  "}, "Completá todos los datos"),
                                ({"telefono": "12"}, "no se puede leer")):
            res = self._profile(html, **values)
            self.assertEqual(res.status_code, 200)
            self.assertIn(message, res.text, values)
            user.partner_id.invalidate_recordset()
            self.assertEqual(user.partner_id.distri_alta_estado, "perfil", values)
            html = res.text
        self._profile(html)
        partner = user.partner_id
        partner.invalidate_recordset()
        self.assertEqual(partner.distri_alta_estado, "pendiente")
        with self.assertRaises(AccessError):
            partner.with_user(self.vendedor).action_distri_reject_reseller()
        partner.with_user(self.gerencia).action_distri_reject_reseller()
        self.assertEqual(partner.distri_alta_estado, "rechazado")
        self.assertFalse(partner.distri_lista_oculta)
        self._assert_publica(self._pedido_html())
        # 19.0.1.93.0 (E2E-C Q7): no self re-submit after a rejection
        page = self.url_open("/mi-perfil")
        self.assertIn("no fue aprobada", page.text)
        self.assertNotIn("Guardar mis datos", page.text)
        # the read-only page has no form: the token of any page will do
        res = self._profile(self._pedido_html(), comercio="Kiosco Reintento")
        self.assertEqual(res.status_code, 403)
        partner.invalidate_recordset()
        self.assertEqual(partner.distri_alta_estado, "rechazado")
        self.assertEqual(partner.name, "Kiosco Autoalta")
        # until gerencia re-opens it
        with self.assertRaises(AccessError):
            partner.with_user(self.vendedor).action_distri_reopen_reseller()
        partner.with_user(self.gerencia).action_distri_reopen_reseller()
        self.assertEqual(partner.distri_alta_estado, "perfil")
        res = self._profile(self.url_open("/mi-perfil").text, comercio="Kiosco Reintento")
        self.assertTrue(res.url.endswith("/mi-perfil?listo=1"), res.url)
        partner.invalidate_recordset()
        self.assertEqual(partner.distri_alta_estado, "pendiente")

    def test_profile_locked_outside_the_signup_flow(self):
        """19.0.1.93.0 (E2E-C C5/C6): an ordinary portal customer and an
        enabled reseller see name and CUIT read-only and cannot change them."""
        self.revendedora.write({"distri_alta_estado": "habilitado", "vat": "20123456786"})
        for login, company in (("kiosco_listas", self.kiosco),
                               ("revende_listas", self.revendedora)):
            self.authenticate(login, login)
            page = self.url_open("/mi-perfil")
            self.assertEqual(page.status_code, 200)
            self.assertIn("Para cambiar estos datos escribinos por WhatsApp", page.text)
            self.assertNotIn("Guardar mis datos", page.text)
            name = company.name
            res = self._profile(self._pedido_html(), comercio="NOMBRE CAMBIADO",
                                cuit="20-18304945-4")
            self.assertEqual(res.status_code, 403, login)
            company.invalidate_recordset()
            self.assertEqual(company.name, name, login)
            self.assertNotEqual(company.vat, "20183049454", login)
            self.assertEqual(company.distri_alta_estado,
                             "habilitado" if company == self.revendedora else False)

    def test_profile_refuses_a_cuit_already_used(self):
        """19.0.1.93.0 (E2E-C C7)."""
        self.env["res.partner"].create({"name": "Cliente Con CUIT", "vat": "20-12345678-6"})
        user, html = self._signup("cuitrepetido@prueba.example")
        res = self._profile(html)
        self.assertEqual(res.status_code, 200)
        self.assertIn("Ese CUIT ya está registrado", res.text)
        user.partner_id.invalidate_recordset()
        self.assertEqual(user.partner_id.distri_alta_estado, "perfil")
        self.assertFalse(user.partner_id.activity_ids)

    def test_profile_submissions_are_rate_limited(self):
        self.env["ir.config_parameter"].sudo().set_param(
            "distri_pedido.perfil_limite_sesion_10min", "2")
        user, html = self._signup("limite@prueba.example")
        for _n in range(2):
            res = self._profile(html, cuit="123")
            self.assertEqual(res.status_code, 200)
            html = res.text
        res = self._profile(html)
        self.assertEqual(res.status_code, 429)
        self.assertIn("muchos envíos seguidos", res.text)
        user.partner_id.invalidate_recordset()
        self.assertEqual(user.partner_id.distri_alta_estado, "perfil")

    def test_crear_acceso_web_users_skip_the_profile(self):
        tienda = self.env["res.partner"].create({"name": "Almacen Acceso Web", "ref": "AW-1"})
        tienda.with_user(self.administracion).action_crear_acceso_web()
        self.assertTrue(tienda.distri_usuario_id)
        self.assertFalse(tienda.distri_alta_estado)

    def test_logged_in_visitors_do_not_get_the_reseller_button(self):
        for login in ("kiosco_listas", "distri_listas", "vendedor_listas", "admin_listas"):
            self.authenticate(login, login)
            self.assertNotIn("Soy revendedor / Soy distribuidora", self._pedido_html(), login)

    def test_reseller_logs_in_and_lands_back_on_its_prices(self):
        res = self.url_open("/web/login", data={
            "login": "revende_listas", "password": "revende_listas", "redirect": "/pedido",
            "type": "password",
            "csrf_token": re.search(r'name="csrf_token" value="([^"]+)"', self.url_open(
                "/web/login?redirect=%2Fpedido").text).group(1)})
        self.assertTrue(res.url.endswith("/pedido"), res.url)
        self.assertIn("(lista Revendedor Pruebalistas)", res.text)

    def test_account_link_follows_the_customer_flag(self):
        """19.0.1.92.0: «Ver mi cuenta corriente» on the catalog only when the
        account is shown to the customer (models/cuenta_visible.py)."""
        self.authenticate("kiosco_listas", "kiosco_listas")
        self.assertNotIn("Ver mi cuenta corriente", self._pedido_html())
        self.kiosco.distri_cuenta_visible = True
        self.assertIn('href="/mi-cuenta"', self._pedido_html())

    def test_anonymous_home_page_does_not_announce_the_hidden_list(self):
        html = self._pedido_html("/")
        self.assertNotIn("inorista", html.lower())
        self.assertNotIn("sección <b>Mayorista</b>", html)
        self.assertNotIn(DISTRIBUIDORAS["nombre"], html)

    def test_crafted_hidden_mode_post_gets_general_prices(self):
        pedido = self._enviar(modo="mayorista")
        self.assertEqual(self._precio(pedido, self.remedio), PRECIO_GENERAL)
        # cigarettes have no public price: in at zero, priced by the seller
        self.assertEqual(self._precio(pedido, self.cigarrillo), 0.0)
        self.assertEqual(pedido.pricelist_id, self.general)
        self.assertEqual(pedido.partner_id.distri_generic_kind, "retail")
        self.assertFalse(any(r["may"] for r in json.loads(pedido.distri_original)))
        self.assertNotIn(LISTA_OCULTA, pedido.note)
        # editing it with the link: still no hidden section, quantities kept
        pedido._portal_ensure_token()
        html = self._pedido_html("/pedido/%s/modificar" % pedido.access_token)
        self._assert_publica(html)

    def test_authorized_customer_sees_the_hidden_section(self):
        self.authenticate("distri_listas", "distri_listas")
        html = self._pedido_html()
        self.assertIn(DISTRIBUIDORAS["nombre"], html)
        self.assertIn("Cigarrillos y tabaco a presupuestar", html)
        self.assertIn('data-precio="%s"' % PRECIO_MAY, html)
        self.assertIn("data-may", html)
        self.assertNotIn("inorista", html.lower())
        pedido = self._enviar()
        self.assertAlmostEqual(self._precio(pedido, self.remedio), PRECIO_MAY, places=2)
        self.assertEqual(self._precio(pedido, self.cigarrillo), 0.0)
        self.assertEqual(pedido.pricelist_id, self.oculta)
        self.assertEqual(pedido.partner_id.distri_generic_kind, "wholesale")

    def test_authorized_reseller_sees_its_own_list(self):
        self.authenticate("revende_listas", "revende_listas")
        html = self._pedido_html()
        self.assertIn("(lista Revendedor Pruebalistas)", html)
        self.assertIn('data-id="%s" data-precio="%s"' % (self.cigarrillo.id, PRECIO_REVENDEDOR),
                      html)
        self.assertIn('data-id="%s" data-precio="%s"' % (self.remedio.id, PRECIO_MAY), html)
        # its prices are already in the catalog: no separate section
        self.assertNotIn("data-may", html)

    def test_unauthorized_logged_in_customer_sees_the_general_list(self):
        self.authenticate("kiosco_listas", "kiosco_listas")
        html = self._pedido_html()
        self._assert_publica(html)
        self.assertIn("(lista %s)" % LISTA_GENERAL, html)
        pedido = self._enviar(modo="mayorista")
        self.assertEqual(self._precio(pedido, self.remedio), PRECIO_GENERAL)
        self.assertEqual(self._precio(pedido, self.cigarrillo), 0.0)
        self.assertEqual(pedido.pricelist_id, self.general)

    def test_gerencia_and_administracion_see_the_hidden_section(self):
        for login in ("admin_listas", "gerencia_listas"):
            self.authenticate(login, login)
            html = self._pedido_html()
            self.assertIn(DISTRIBUIDORAS["nombre"], html, login)
            self.assertIn('data-precio="%s"' % PRECIO_MAY, html, login)

    def test_seller_sees_only_the_general_list(self):
        self.authenticate("vendedor_listas", "vendedor_listas")
        self._assert_publica(self._pedido_html())

    def test_crafted_hidden_post_by_a_seller_gets_general_prices(self):
        self.authenticate("vendedor_listas", "vendedor_listas")
        pedido = self._enviar(csrf_token=self._csrf(), cliente_id=str(self.almacen.id))
        self.assertEqual(pedido.partner_id, self.almacen)
        self.assertEqual(pedido.user_id, self.vendedor)
        self.assertEqual(pedido.pricelist_id, self.general)
        self.assertEqual(self._precio(pedido, self.remedio), PRECIO_GENERAL)
        self.assertFalse(any(r["may"] for r in json.loads(pedido.distri_original)))
        # the seller's own order screens: General prices, no hidden section
        html = self._pedido_html("/pedido/%s/modificar" % pedido.access_token)
        self._assert_publica(html)
        html = self._pedido_html("/pedido/%s" % pedido.access_token)
        for precio in ("3.778", "7.654"):
            self.assertNotIn(precio, html)

    def test_seller_cannot_take_or_open_a_distributor_order(self):
        self.authenticate("vendedor_listas", "vendedor_listas")
        csrf = self._csrf()
        # not offered in the customer search, refused if posted anyway
        nombres = [f["nombre"] for f in
                   self.url_open("/pedido/clientes?q=Pruebalistas").json()]
        self.assertIn(self.almacen.name, nombres)
        for socio in (self.distribuidora, self.revendedora, self.kiosco):
            self.assertNotIn(socio.name, nombres)
        antes = self.env["sale.order"].search_count([])
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.remedio.id, "cantidad": 1}]),
            "csrf_token": csrf, "cliente_id": str(self.revendedora.id)})
        self.assertEqual(res.status_code, 403)
        self.assertIn(MENSAJE_DISTRIBUIDORA, res.text)
        self.assertEqual(self.env["sale.order"].search_count([]), antes)
        # a distributor's order: not shown, not edited, not repriced
        pedido = self._pedido_de_distribuidora()
        total = pedido.amount_total
        for url in ("/pedido/%s", "/pedido/%s/modificar", "/pedido/%s/cuenta"):
            res = self.url_open(url % pedido.access_token)
            self.assertEqual(res.status_code, 403, url)
            self.assertIn(MENSAJE_DISTRIBUIDORA, res.text)
            self.assertNotIn("3.778", res.text)
        res = post_pedido(self, {
            "renglones": json.dumps([{"id": self.remedio.id, "cantidad": 9}]),
            "csrf_token": csrf, "token": pedido.access_token})
        self.assertEqual(res.status_code, 403)
        pedido.invalidate_recordset()
        self.assertEqual(pedido.amount_total, total)
        self.assertEqual(pedido.order_line.product_uom_qty, 1)
        # the distributor itself still opens it
        self.authenticate("distri_listas", "distri_listas")
        self.assertEqual(self.url_open("/pedido/%s" % pedido.access_token).status_code, 200)

    def test_seller_cannot_read_hidden_lists_or_distributor_orders(self):
        pedido = self._pedido_de_distribuidora()
        Pricelist = self.env["product.pricelist"].with_user(self.vendedor)
        listas = Pricelist.search([])
        self.assertIn(self.general, listas)
        self.assertNotIn(self.oculta, listas)
        self.assertNotIn(self.revendedor, listas)
        items = self.env["product.pricelist.item"].with_user(self.vendedor).search([])
        self.assertFalse(items & (self.oculta.item_ids | self.revendedor.item_ids))
        with self.assertRaises(AccessError):
            self.oculta.item_ids.with_user(self.vendedor).read(["fixed_price"])
        Order = self.env["sale.order"].with_user(self.vendedor)
        self.assertNotIn(pedido, Order.search([]))
        with self.assertRaises(AccessError):
            pedido.with_user(self.vendedor).read(["amount_total"])
        with self.assertRaises(AccessError):
            pedido.order_line.with_user(self.vendedor).read(["price_unit"])
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            pedido.with_user(self.vendedor).write({"note": "cambio"})
        # Gerencia and Administración see everything
        for usuario in (self.administracion, self.gerencia):
            self.assertIn(self.oculta, self.env["product.pricelist"].with_user(usuario).search([]))
            self.assertIn(pedido, self.env["sale.order"].with_user(usuario).search([]))
            pedido.with_user(usuario).read(["amount_total"])

    def test_seller_cannot_assign_a_hidden_list(self):
        # a plain seller cannot write contacts at all; one that was also given
        # «Contactos: creación» still cannot assign a hidden list
        with self.assertRaises(AccessError):
            self.almacen.with_user(self.vendedor).write(
                {"property_product_pricelist": self.oculta.id})
        con_contactos = new_test_user(
            self.env, login="vendedor_contactos_listas",
            groups="base.group_user,base.group_partner_manager,distri_pedido.grupo_vendedor")
        con_contactos.etiqueta_cartera_id = self.cartera
        socio = self.almacen.with_user(con_contactos)
        for lista in (self.oculta, self.revendedor):
            with self.assertRaisesRegex(UserError, "Gerencia y Administración"):
                socio.write({"property_product_pricelist": lista.id})
        socio.write({"property_product_pricelist": self.general.id})
        self.assertEqual(self.almacen.property_product_pricelist, self.general)
        Order = self.env["sale.order"].with_user(self.vendedor)
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            Order.create({"partner_id": self.almacen.id, "pricelist_id": self.oculta.id})
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            Order.create({"partner_id": self.distribuidora.id})
        propio = Order.create({"partner_id": self.almacen.id})
        self.assertEqual(propio.pricelist_id, self.general)
        with self.assertRaisesRegex(UserError, "lo gestiona Administración"):
            propio.write({"pricelist_id": self.revendedor.id})
        # Gerencia and Administración assign it
        self.almacen.with_user(self.administracion).write(
            {"property_product_pricelist": self.revendedor.id})
        self.assertEqual(self.almacen.property_product_pricelist, self.revendedor)
        propio.with_user(self.gerencia).write({"pricelist_id": self.oculta.id})
        self.assertEqual(propio.pricelist_id, self.oculta)

    # ------------------------------------ customers and the data API

    def _usuario(self, login):
        return self.env["res.users"].search([("login", "=", login)])

    def _visibles(self, usuario):
        listas = self.env["product.pricelist"].with_user(usuario).search([])
        items = self.env["product.pricelist.item"].with_user(usuario).search_read(
            [], ["pricelist_id", "fixed_price"])
        return listas, {i["pricelist_id"][0] for i in items}

    def test_customers_read_only_public_and_own_authorized_lists(self):
        oculta_item = self.oculta.item_ids[:1]
        revendedor_item = self.revendedor.item_ids[:1]
        casos = (
            # (user, own hidden list it may read)
            (self._usuario("kiosco_listas"), None),          # hidden list, not authorized
            (self._usuario("distri_listas"), None),          # authorized, General list
            (self.env.ref("base.public_user"), None),        # visitor
            (self._usuario("revende_listas"), self.revendedor),
        )
        for usuario, propia in casos:
            listas, con_items = self._visibles(usuario)
            self.assertIn(self.general, listas, usuario.login)
            self.assertNotIn(self.oculta, listas, usuario.login)
            self.assertNotIn(self.oculta.id, con_items, usuario.login)
            with self.assertRaises(AccessError):
                oculta_item.with_user(usuario).read(["fixed_price"])
            if propia:
                self.assertIn(propia, listas)
                self.assertIn(propia.id, con_items)
                revendedor_item.with_user(usuario).read(["fixed_price"])
            else:
                self.assertNotIn(self.revendedor, listas, usuario.login)
                self.assertNotIn(self.revendedor.id, con_items, usuario.login)
                with self.assertRaises(AccessError):
                    revendedor_item.with_user(usuario).read(["fixed_price"])
            # a price asked for with a hidden list in the context
            variante = self.remedio.product_variant_id.with_user(usuario).with_context(
                pricelist=self.oculta.id)
            try:
                precio = variante.get_contextual_price()
            except AccessError:
                precio = None
            self.assertNotAlmostEqual(precio or 0.0, PRECIO_MAY, places=2)

    def test_customer_authorization_is_applied_at_once(self):
        kiosco = self._usuario("kiosco_listas")
        self.assertNotIn(self.revendedor, self._visibles(kiosco)[0])
        # Administración flags the company: its own hidden list becomes readable
        self.kiosco.with_user(self.administracion).write({"distri_lista_oculta": True})
        self.assertIn(self.revendedor, self._visibles(kiosco)[0])
        self.kiosco.with_user(self.administracion).write(
            {"property_product_pricelist": self.general.id})
        self.assertNotIn(self.revendedor, self._visibles(kiosco)[0])

    def test_customer_cannot_read_other_customers_orders(self):
        pedido = self._pedido_de_distribuidora()
        kiosco = self._usuario("kiosco_listas")
        self.assertNotIn(pedido, self.env["sale.order"].with_user(kiosco).search([]))
        self.assertFalse(self.env["sale.order.line"].with_user(kiosco).search(
            [("order_id", "=", pedido.id)]))
        with self.assertRaises(AccessError):
            pedido.order_line.with_user(kiosco).read(["price_unit"])
        # the distributor reads its own order
        pedido.with_user(self._usuario("distri_listas")).read(["amount_total"])

    def test_json_rpc_as_customer_gets_no_hidden_prices(self):
        self.authenticate("kiosco_listas", "kiosco_listas")

        def call_kw(model, method, args, kwargs=None):
            return self.make_jsonrpc_request("/web/dataset/call_kw", {
                "model": model, "method": method, "args": args, "kwargs": kwargs or {}})

        items = call_kw("product.pricelist.item", "search_read", [[]],
                        {"fields": ["pricelist_id", "fixed_price"]})
        ocultas = {self.oculta.id, self.revendedor.id}
        self.assertFalse([i for i in items if i["pricelist_id"][0] in ocultas])
        for precio in (PRECIO_MAY, PRECIO_REVENDEDOR):
            self.assertNotIn(precio, [i["fixed_price"] for i in items])
        nombres = [p["name"] for p in call_kw("product.pricelist", "search_read", [[]],
                                              {"fields": ["name"]})]
        self.assertIn(LISTA_GENERAL, nombres)
        self.assertNotIn(LISTA_OCULTA, nombres)
        self.assertNotIn(self.revendedor.name, nombres)
        with self.assertRaises(JsonRpcException):
            call_kw("product.pricelist.item", "read",
                    [self.oculta.item_ids.ids, ["fixed_price"]])
        try:
            precio = call_kw("product.product", "get_contextual_price",
                             [[self.remedio.product_variant_id.id]],
                             {"context": {"pricelist": self.oculta.id}})
        except JsonRpcException:
            precio = None
        self.assertNotAlmostEqual(precio or 0.0, PRECIO_MAY, places=2)
        # the page keeps working for this customer
        self._assert_publica(self._pedido_html())

    def test_only_administracion_can_flag_a_customer(self):
        with self.assertRaises(AccessError):
            self.kiosco.with_user(self.vendedor).write({"distri_lista_oculta": True})
        self.kiosco.with_user(self.administracion).write({"distri_lista_oculta": True})
        self.assertTrue(self.kiosco.distri_lista_oculta)
