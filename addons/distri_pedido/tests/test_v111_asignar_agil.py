"""19.0.1.111.0: «Asignar cliente» as a kit screen (/revisar/<id>/asignar,
controllers/asignar.py) and «Pedidos de AgilPedido» on the kit (/agilpedido,
controllers/agilpedido.py): the list of the orders that came by WhatsApp,
«Cargar un pedido de AgilPedido» with «Está bien» / «Corregir y recordar»,
and the «AgilPedido» tile on /tablero.

Every action goes through HTTP as the pages send it (fetch,
X-Distri-Fetch: 1) and reuses the models' rules (asignar_cliente.py,
pegar_numero.py, importar_pedido.py, agilpedido_wa.py,
agil_equivalencia.py). Names and phones are made up («V111», 299 000-11xx)."""
import re

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.agilpedido import pedidos_de_hoy
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles, tile_badges
from odoo.addons.distri_pedido.models.agil_tienda import PARAM_CATALOGO_AGIL
from odoo.addons.distri_pedido.models.agilpedido_wa import ORIGEN_AGILPEDIDO_WA, PARAM_AUTO
from odoo.addons.distri_pedido.models.sale_order import SOCIO_SIN_ASIGNAR

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
PASTED = "+54 9 299 000-1181"


def mensaje(*renglones, total):
    """An AgilPedido message in its fixed template (English amounts)."""
    return ("Hola Distri Neuquén! Soy Carla Vcientoonce y quiero hacer el siguiente pedido hacia "
            "*Av. Siempreviva 742, Neuquén*:\n\n%s\n\n*Total: $ %s*\n\nMuchas gracias!") % (
        "\n".join(renglones), "{:,.2f}".format(total))


class _Common(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_v111", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v111", "distri_pedido.grupo_gerencia")
        cls.jefe_ventas = user("jefe_ventas_v111", "sales_team.group_sale_manager")
        cls.vendedor = user("vendedor_v111", "distri_pedido.grupo_vendedor")
        cls.armador = user("armador_v111", "distri_pedido.grupo_armador")
        cls.cajero = user("cajero_v111", "distri_pedido.grupo_cajero")
        Partner = cls.env["res.partner"]
        cls.generico = Partner.search([("distri_generic_partner", "=", True),
                                       ("distri_generic_kind", "=", "retail")], limit=1) or \
            Partner.create({"name": SOCIO_SIN_ASIGNAR, "distri_generic_partner": True,
                            "distri_generic_kind": "retail"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto Asignar V111", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self, path):
        page = self.url_open(path)
        self.assertEqual(page.status_code, 200, page.text[:500])
        return CSRF_RE.search(page.text).group(1)

    def _post(self, path, data=None, page=None):
        data = dict(data or {}, csrf_token=self._csrf(page or path))
        return self.url_open(path, data=data, headers=FETCH, allow_redirects=False)


@tagged("post_install", "-at_install", "distri_v111", "distri_v111_asignar")
class TestAsignarKit(_Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        # the pasted number belongs to one customer only
        cls.holder = Partner.create({"name": "Kiosco Del Numero V111", "phone": "299 000-1181",
                                     "city": "Neuquén", "customer_rank": 1})
        # same word in the name: «≈ Nombre»
        cls.parecido = Partner.create({"name": "Lucerito Gimenez V111", "street": "Belgrano 120",
                                       "city": "Plottier", "customer_rank": 1})
        cls.web = cls.env["sale.order"].sudo().create({
            "partner_id": cls.generico.id,
            "distri_nombre": "Kiosco Lucerito",
            "distri_telefono": "299 000-1199",
            "distri_domicilio": "Belgrano 150, Plottier",
            "order_line": [Command.create({"product_id": cls.product.id, "product_uom_qty": 1,
                                           "price_unit": 900.0, "tax_ids": [Command.clear()]})],
        })
        cls.url = "/revisar/%s/asignar" % cls.web.id

    # ------------------------------------------------------------ access

    def test_access_per_group(self):
        for user in (self.vendedor, self.armador, self.cajero):
            self._login(user)
            self.assertEqual(self.url_open(self.url).status_code, 403, user.login)
            res = self.url_open(self.url, data={"csrf_token": "x", "accion": "asignar",
                                                "partner_id": self.holder.id},
                                headers=FETCH, allow_redirects=False)
            self.assertIn(res.status_code, (400, 403), user.login)
        self.assertTrue(self.web.distri_partner_generic)
        for user in (self.admin, self.gerente, self.jefe_ventas):
            self._login(user)
            page = self.url_open(self.url)
            self.assertEqual(page.status_code, 200, user.login)
            self.assertIn("Asignar cliente", page.text)
            self.assertIn("Lo que escribió el comprador", page.text)
        # a sales manager without administración goes back to the home, not /revisar
        self.assertIn('data-parent="/tablero"', page.text)

    def test_revisar_links_to_the_kit_screen(self):
        self._login(self.admin)
        text = self.url_open("/revisar").text
        self.assertIn('href="/revisar/%s/asignar"' % self.web.id, text)
        self.assertNotIn("action_revisar_asignar_cliente", text)
        self.assertFalse(self.env.ref("distri_pedido.action_revisar_asignar_cliente",
                                      raise_if_not_found=False), "the v108 bridge is gone")
        # «Pegar número» › «Asignar cliente» goes to the kit screen with the number
        res = self._post(self.url, {"pegado": PASTED}, page="/revisar")
        self.assertEqual(res.status_code, 200, res.text)
        url = res.json()["url"]
        self.assertTrue(url.startswith(self.url + "?pegado="), url)
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.text, r'as-cand as-cand--on" data-id="%s"' % self.holder.id)
        self.assertIn("Queda así:", page.text)

    def test_existing_customer_by_pasted_number_saves_the_verified_whatsapp(self):
        self._login(self.admin)
        data = self.url_open("%s/candidatos?pegado=%s" % (self.url, PASTED)).json()
        self.assertEqual(data["elegido"], self.holder.id, "the only customer with it is chosen")
        self.assertEqual(data["preview"], "+54 9 299 000-1181")
        self.assertIn("No es el teléfono que escribió el cliente", data["aviso"])
        self.assertIn("Kiosco Del Numero V111", data["html"])
        res = self._post(self.url, {"accion": "asignar", "modo": "existente",
                                    "partner_id": self.holder.id, "pegado": PASTED,
                                    "pegado_guardar": "1"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertEqual(body["url"], "/revisar?ok=%s&asignado=%s" % (self.web.id, self.web.id))
        self.assertIn("quedó a nombre de Kiosco Del Numero V111", body["message"])
        self.web.invalidate_recordset()
        self.assertEqual(self.web.partner_id, self.holder)
        self.assertIn(self.web.state, ("draft", "sent"), "«Solo asignar» does not approve")
        self.holder.invalidate_recordset()
        self.assertEqual(self.holder.distri_whatsapp, "+5492990001181")
        self.assertTrue(self.holder.distri_whatsapp_confirmed)
        # the list shows what happened; the screen refuses a second time
        self.assertIn("quedó a nombre de Kiosco Del Numero V111",
                      self.url_open(body["url"]).text)
        self.assertEqual(self.url_open(self.url, allow_redirects=False).status_code, 303)

    def test_similar_name_is_a_candidate_with_its_marks(self):
        self._login(self.admin)
        page = self.url_open(self.url).text
        self.assertIn("Lucerito Gimenez V111", page)
        card = page[page.index('data-id="%s"' % self.parecido.id):]
        card = card[:card.index("</article>")]
        self.assertIn("≈ Nombre", card)
        self.assertIn("✓ Ciudad", card)
        self.assertIn("✓ Domicilio", card)
        # «Buscar otro» by name finds it too, chosen when asked
        data = self.url_open("%s/candidatos?busqueda=Lucerito&elegido=%s" % (
            self.url, self.parecido.id)).json()
        self.assertEqual(data["elegido"], self.parecido.id)
        res = self._post(self.url, {"accion": "asignar", "modo": "existente",
                                    "partner_id": self.parecido.id})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(self.web.partner_id, self.parecido)

    def test_new_customer_assign_and_approve(self):
        self._login(self.admin)
        page = self.url_open(self.url + "?modo=nuevo").text
        self.assertIn('value="Kiosco Lucerito"', page, "the typed name fills «Cliente nuevo»")
        res = self._post(self.url, {
            "accion": "aprobar", "modo": "nuevo", "nuevo_nombre": "Kiosco Lucerito Nuevo V111",
            "nuevo_telefono": "299 000-1199", "nuevo_domicilio": "Belgrano 150",
            "nuevo_ciudad": "Plottier", "pegado": "+54 9 299 000-1199", "pegado_guardar": "1"})
        self.assertEqual(res.status_code, 200, res.text)
        nuevo = self.env["res.partner"].search([("name", "=", "Kiosco Lucerito Nuevo V111")])
        self.assertEqual(len(nuevo), 1)
        self.assertEqual(nuevo.city, "Plottier")
        self.assertEqual(self.web.partner_id, nuevo)
        self.assertEqual(self.web.state, "sale", res.json()["message"])
        self.assertTrue(res.json()["aprobado"])
        self.assertIn("y aprobado", res.json()["message"])

    def test_only_assign_versus_assign_and_approve(self):
        self._login(self.gerente)
        otro = self.web.copy({"partner_id": self.generico.id, "distri_nombre": "Otro V111"})
        self.assertIn(otro.state, ("draft", "sent"))
        # the holder gets the verified WhatsApp with the first assignment
        res = self._post(self.url, {"accion": "asignar", "partner_id": self.holder.id,
                                    "pegado": PASTED, "pegado_guardar": "1"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(self.web.state, ("draft", "sent"))
        url = "/revisar/%s/asignar" % otro.id
        res = self._post(url, {"accion": "aprobar", "partner_id": self.holder.id})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(otro.state, "sale", res.json()["message"])

    def test_refusals(self):
        self._login(self.admin)
        res = self._post(self.url, {"accion": "asignar", "modo": "existente"})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Elegí el cliente", res.json()["error"])
        res = self._post(self.url, {"accion": "asignar", "modo": "existente",
                                    "partner_id": self.generico.id})
        self.assertEqual(res.status_code, 400)
        res = self._post(self.url, {"accion": "asignar", "modo": "nuevo", "nuevo_nombre": " "})
        self.assertEqual(res.status_code, 400)
        self.assertIn("nombre del cliente nuevo", res.json()["error"])
        self.assertTrue(self.web.distri_partner_generic)

    def test_backend_wizard_keeps_working(self):
        action = self.web.with_user(self.admin).action_distri_asignar_cliente()
        wizard = self.env["distri.asignar.cliente"].with_user(self.admin).browse(action["res_id"])
        wizard.write({"partner_id": self.holder.id})
        wizard.action_asignar()
        self.assertEqual(self.web.partner_id, self.holder)


@tagged("post_install", "-at_install", "distri_v111", "distri_v111_agil")
class TestAgilKit(_Common):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param(PARAM_CATALOGO_AGIL, "0")
        params.set_param(PARAM_AUTO, "1")
        unidad = cls.env.ref("uom.product_uom_unit")
        Uom = cls.env["uom.uom"]
        cls.bulto12 = Uom.search([("name", "=", "Bulto x 12")], limit=1) or Uom.create({
            "name": "Bulto x 12", "relative_factor": 12.0, "relative_uom_id": unidad.id})
        cls.unidad = unidad
        Tmpl = cls.env["product.template"]
        base = {"type": "consu", "sale_ok": True, "taxes_id": [Command.clear()]}
        cls.yerba = Tmpl.create(dict(base, name="Yerba Vcientoonce Suave 1kg", list_price=3000))
        cls.galletitas = Tmpl.create(dict(base, name="Galletitas Vcientoonce Surtidas 400g",
                                          list_price=1000))
        cls.dulce = Tmpl.create(dict(base, name="Dulce de Leche Vcientoonce Repostero 1kg",
                                     list_price=1000,
                                     uom_ids=[Command.set((unidad | cls.bulto12).ids)]))
        cls.cliente = cls.env["res.partner"].create({"name": "Almacen Cliente V111",
                                                     "customer_rank": 1})
        cls.texto = mensaje(
            "- 2 x Yerba Vcientoonce Suave 1kg ($ 6,000.00)",
            "- 3 x Galletitas Vcientoonce Surtida 400 ($ 3,000.00)",
            "- 1 x Zzqxw Vwkj Hhgf Dulcerito ($ 12,000.00)",
            total=21000)

    def _leer(self, texto=None):
        res = self._post("/agilpedido/cargar", {"mensaje": texto or self.texto})
        self.assertEqual(res.status_code, 200, res.text)
        url = res.json()["url"]
        imp = self.env["distri.importar.pedido"].browse(int(url.rsplit("/", 1)[1]))
        self.assertEqual(self.url_open(url).status_code, 200)
        return imp, url

    def _linea(self, imp, texto):
        return imp.linea_ids.filtered(lambda l: texto in (l.nombre_mensaje or ""))

    # ------------------------------------------------------------ access

    def test_access_per_group(self):
        for user in (self.vendedor, self.armador, self.cajero, self.jefe_ventas):
            self._login(user)
            for path in ("/agilpedido", "/agilpedido/cargar"):
                self.assertEqual(self.url_open(path).status_code, 403, (user.login, path))
        for user in (self.admin, self.gerente):
            self._login(user)
            page = self.url_open("/agilpedido")
            self.assertEqual(page.status_code, 200, user.login)
            self.assertIn("Pedidos de AgilPedido", page.text)
            self.assertIn("Cargar un pedido de AgilPedido", self.url_open("/agilpedido/cargar").text)
        # somebody else's reading is not visible
        imp, url = self._leer()
        self._login(self.admin)
        self.assertIn("Ese pedido ya no está", self.url_open(url).text)
        res = self._post(url + "/crear", page="/agilpedido/cargar")
        self.assertEqual(res.status_code, 404)

    # ------------------------------------------------------------ load

    def test_paste_analyse_correct_and_create(self):
        self._login(self.admin)
        imp, url = self._leer()
        yerba = self._linea(imp, "Yerba")
        galletitas = self._linea(imp, "Galletitas")
        rara = self._linea(imp, "Zzqxw")
        self.assertEqual(yerba.product_tmpl_id, self.yerba)
        self.assertFalse(yerba._distri_para_corregir())
        self.assertEqual(galletitas.product_tmpl_id, self.galletitas)
        self.assertLess(galletitas.parecido, 100)
        self.assertTrue(galletitas._distri_para_corregir(), "a similar name must be confirmed")
        self.assertFalse(rara.product_tmpl_id)
        page = self.url_open(url).text
        self.assertIn("No se encontró el producto", page)
        self.assertIn("Parecido al", page)
        self.assertRegex(page, r'data-ag-final="" disabled="disabled"')

        res = self._post(url + "/datos", {"partner_id": self.cliente.id}, page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(imp.partner_id, self.cliente)
        # unknown and unconfirmed lines block «Crear el presupuesto»
        res = self._post(url + "/crear", page=url)
        self.assertEqual(res.status_code, 400)
        self.assertIn("renglones para corregir", res.json()["error"])
        self.assertFalse(self.env["sale.order"].search([("partner_id", "=", self.cliente.id)]))

        # «Está bien» on the similar one: learned
        res = self._post("%s/renglon/%s/ok" % (url, galletitas.id), page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(galletitas.equivalencia_id)
        self.assertEqual(galletitas.equivalencia_id.product_id, self.galletitas.product_variant_id)
        # «Corregir y recordar» on the unknown one: the dulce, by the 12 pack
        res = self._post("%s/renglon/%s/corregir" % (url, rara.id), {
            "product_id": self.dulce.product_variant_id.id, "uom_id": self.bulto12.id}, page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("aprendido", res.json()["message"])
        self.assertNotIn('data-ag-final="" disabled', res.json()["board"])
        self.assertEqual(rara.product_tmpl_id, self.dulce)
        self.assertEqual(rara.product_uom_id, self.bulto12)
        equivalencia = self.env["distri.agil.equivalencia"].search(
            [("nombre_agil", "=", "Zzqxw Vwkj Hhgf Dulcerito")])
        self.assertEqual(equivalencia.product_id, self.dulce.product_variant_id)
        self.assertEqual(equivalencia.product_uom_id, self.bulto12)

        # «Agregar renglón a mano» (the cigarettes never come in the message)
        res = self._post(url + "/agregar", {"product_id": self.product.id, "cantidad": "2",
                                            "uom_id": ""}, page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(len(imp.linea_ids), 4)

        precio_dulce = imp._distri_precio_unidad(rara)
        self.assertGreater(precio_dulce, 0)
        res = self._post(url + "/crear", page=url)
        self.assertEqual(res.status_code, 200, res.text)
        created = res.json()["url"]
        order = self.env["sale.order"].browse(int(created.rsplit("/", 1)[1]))
        self.assertEqual(order.partner_id, self.cliente)
        self.assertEqual(order.state, "draft")
        self.assertEqual(len(order.order_line), 4)
        dulce = order.order_line.filtered(lambda l: l.product_id == self.dulce.product_variant_id)
        self.assertEqual(dulce.product_uom_id, self.bulto12)
        self.assertEqual(dulce.price_unit, precio_dulce, "the pack price of Odoo, never the message's")
        self.assertIn("PEDIDO CARGADO DESDE AGILPEDIDO", order.note)
        done = self.url_open(created)
        self.assertEqual(done.status_code, 200)
        self.assertIn("se creó el presupuesto %s" % order.name, done.text)

        # the next paste recognises both corrections
        imp2, url2 = self._leer()
        for texto, producto in (("Galletitas", self.galletitas), ("Zzqxw", self.dulce)):
            linea = self._linea(imp2, texto)
            self.assertEqual(linea.product_tmpl_id, producto, texto)
            self.assertTrue(linea.equivalencia_id, texto)
            self.assertFalse(linea._distri_para_corregir(), texto)
        self.assertIn("2 por equivalencia aprendida", imp2.resumen)
        res = self._post(url2 + "/datos", {"partner_id": self.cliente.id}, page=url2)
        self.assertNotIn('data-ag-final="" disabled', res.json()["board"])

    def test_correct_with_a_unit_odoo_does_not_have(self):
        self._login(self.gerente)
        imp, url = self._leer(mensaje("- 2 x Caja Wwqz Dulcerito Kkjh ($ 24,000.00)", total=24000))
        linea = imp.linea_ids
        self.assertFalse(linea.product_tmpl_id)
        res = self._post("%s/renglon/%s/corregir" % (url, linea.id), {
            "product_id": self.dulce.product_variant_id.id, "uom_id": self.unidad.id,
            "otra_cantidad": "1", "factor": "12"}, page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(linea.cantidad, 24.0, "2 of AgilPedido × 12 units")
        self.assertEqual(linea.equivalencia_id.factor, 12.0)
        self.assertEqual(linea._distri_cantidad_agil(), 2.0)
        imp2, _url2 = self._leer(mensaje("- 3 x Caja Wwqz Dulcerito Kkjh ($ 36,000.00)",
                                         total=36000))
        self.assertEqual(imp2.linea_ids.cantidad, 36.0)
        self.assertEqual(imp2.linea_ids.product_tmpl_id, self.dulce)

    def test_products_search_for_the_panel(self):
        self._login(self.admin)
        imp, url = self._leer()
        data = self.url_open(url + "/productos?q=Vcientoonce Repostero&cant=1").json()
        self.assertEqual([p["name"] for p in data["productos"]], [self.dulce.name])
        units = {u["id"]: u["price"] for u in data["productos"][0]["units"]}
        self.assertEqual(set(units), {self.unidad.id, self.bulto12.id})
        self.assertAlmostEqual(units[self.bulto12.id], units[self.unidad.id] * 12, places=2)

    # ------------------------------------------------------------ WhatsApp orders

    def _wa_order(self, texto):
        message = self.env["distri.whatsapp.message"].sudo()
        msg_id = "wamid.v111.%s" % len(texto)
        message._wa_store_webhook({"object": "whatsapp_business_account", "entry": [{
            "id": "100000000000001", "changes": [{"field": "messages", "value": {
                "messaging_product": "whatsapp",
                "metadata": {"display_phone_number": "15551833591",
                             "phone_number_id": "100000000000002"},
                "contacts": [{"profile": {"name": "Carla Vcientoonce"}, "wa_id": "5492990001177"}],
                "messages": [{"from": "5492990001177", "timestamp": "1790000000", "id": msg_id,
                              "type": "text", "text": {"body": texto}}],
            }}]}]})
        order = message.search([("wa_message_id", "=", msg_id)]).distri_agil_order_id
        self.assertEqual(order.distri_origen, ORIGEN_AGILPEDIDO_WA)
        return order

    def test_whatsapp_orders_list_correct_and_tile(self):
        order = self._wa_order(self.texto)
        self.assertEqual(order.distri_agil_por_corregir, 2)
        self._login(self.admin)
        page = self.url_open("/agilpedido").text
        self.assertIn(order.name, page)
        self.assertIn("2 a corregir", page)
        self.assertIn("Cliente: a asignar", page)
        self.assertIn('href="/revisar/%s/asignar?volver=agilpedido"' % order.id, page)
        url = "/agilpedido/pedido/%s" % order.id
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200)
        self.assertIn("Corregir producto", page.text)
        rara = order.order_line.filtered(lambda l: "Zzqxw" in (l.distri_agil_texto or ""))
        galletitas = order.order_line.filtered(lambda l: "Galletitas" in (l.distri_agil_texto or ""))
        res = self._post("%s/renglon/%s/corregir" % (url, rara.id), {
            "product_id": self.dulce.product_variant_id.id, "uom_id": self.bulto12.id}, page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(rara.product_id, self.dulce.product_variant_id)
        self.assertFalse(rara.distri_agil_corregir)
        res = self._post("%s/renglon/%s/ok" % (url, galletitas.id), page=url)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertEqual(order.distri_agil_por_corregir, 0)
        # still under the generic contact: no approval
        res = self._post(url + "/aprobar", page=url)
        self.assertEqual(res.status_code, 400)
        self.assertIn("Asigná el cliente", res.json()["error"])
        # the assignment goes back to this list
        res = self._post("/revisar/%s/asignar" % order.id, {
            "accion": "asignar", "partner_id": self.cliente.id, "volver": "agilpedido"},
            page="/revisar/%s/asignar?volver=agilpedido" % order.id)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json()["url"].startswith("/agilpedido?ok="))

        def urls(user):
            return [t["url"] for t in staff_tiles(self.env(user=user))]

        for user in (self.admin, self.gerente):
            self.assertIn("/agilpedido", urls(user), user.login)
        for user in (self.vendedor, self.armador, self.cajero, self.jefe_ventas):
            self.assertNotIn("/agilpedido", urls(user), user.login)
        env = self.env(user=self.admin)
        tiles = staff_tiles(env)
        tile_badges(env, tiles)
        tile = next(t for t in tiles if t["key"] == "agilpedido")
        self.assertEqual(tile["badge"]["n"], pedidos_de_hoy(env))
        self.assertGreaterEqual(tile["badge"]["n"], 1)
        self.assertTrue(tile["badge"]["text"].endswith(" hoy"))

    def test_driver_expenses_have_no_approve_button(self):
        for xmlid in ("distri_pedido.view_expense_list", "distri_pedido.view_expense_form",
                      "distri_pedido.view_route_form_expenses"):
            arch = self.env.ref(xmlid).arch
            self.assertNotRegex(arch, r'<button[^>]*string="Aprobar', xmlid)
