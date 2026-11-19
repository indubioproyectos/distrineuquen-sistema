"""19.0.1.108.0: «Pedidos para revisar» on the phone (/revisar,
controllers/revisar.py) and the new «Inicio» (/tablero with count badges).

Same access as the backend action: administración and gerencia. Every
action goes through HTTP as the page sends it (fetch, X-Distri-Fetch: 1)
and reuses the model's rules (models/revisar.py, models/pegar_numero.py)."""
import re
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.revisar import cuando
from odoo.addons.distri_pedido.controllers.tablero import (
    my_route_today, staff_tiles, tile_badges)
from odoo.addons.distri_pedido.models.ar_phone import MSG_SIN_NUMERO
from odoo.addons.distri_pedido.models.sale_order import SOCIO_SIN_ASIGNAR

FETCH = {"X-Distri-Fetch": "1"}
CSRF_RE = re.compile(r'name="csrf-token" content="([^"]+)"')
PASTED = "+54 9 299 000-1081"


@tagged("post_install", "-at_install", "distri_v108", "distri_v108_revisar")
class TestRevisarApp(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.admin = user("admin_rv108", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_rv108", "distri_pedido.grupo_gerencia")
        cls.armador = user("armador_rv108", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_rv108", "distri_pedido.grupo_reparto")
        cls.vendedor = user("vendedor_rv108", "distri_pedido.grupo_vendedor")
        cls.cajero = user("cajero_rv108", "distri_pedido.grupo_cajero")
        Partner = cls.env["res.partner"]
        cls.generico = Partner.search([("distri_generic_partner", "=", True)], limit=1) or \
            Partner.create({"name": SOCIO_SIN_ASIGNAR, "distri_generic_partner": True,
                            "distri_generic_kind": "retail"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto Revisar V108", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        # red: a customer created today that never bought, no WhatsApp
        cls.nuevo = Partner.create({"name": "Almacen Nuevo V108", "customer_rank": 1})
        # yellow: an old customer with history and no WhatsApp
        cls.sin_wa = Partner.create({"name": "Despensa Sin WA V108", "customer_rank": 1})
        # green: old, with history and WhatsApp
        cls.habitual = Partner.create({"name": "Maxikiosco Habitual V108", "customer_rank": 1,
                                       "distri_whatsapp": "+5492994222444"})
        for partner in (cls.sin_wa, cls.habitual):
            cls.env.cr.execute("UPDATE res_partner SET create_date = %s WHERE id = %s",
                               (fields.Datetime.now() - timedelta(days=400), partner.id))
            partner.invalidate_recordset(["create_date"])
            cls.env["distri.cuenta.movimiento"].sudo().create([{
                "partner_id": partner.id, "tipo": "factura", "debe": 1000.0,
                "fecha": fields.Date.today() - timedelta(days=10 + i),
                "comprobante": "FA-V108-%s-%s" % (partner.id, i)} for i in range(3)])
        # «Ya tienen este número»
        cls.holder = Partner.create({"name": "Kiosco Ya Lo Tiene V108", "phone": "299 000-1081",
                                     "city": "Neuquén", "customer_rank": 1})
        cls.rojo = cls._order(cls.nuevo, telefono="299 000-2222", code="K7P3")
        cls.amarillo = cls._order(cls.sin_wa)
        cls.verde = cls._order(cls.habitual)
        cls.web = cls._order(cls.generico, nombre="Kiosco de la Web V108", telefono="299 000-3333")

    @classmethod
    def _order(cls, partner, telefono="", code="", nombre=""):
        order = cls.env["sale.order"].sudo().create({
            "partner_id": partner.id,
            "distri_nombre": nombre or False,
            "distri_telefono": telefono or False,
            "order_line": [Command.create({"product_id": cls.product.id, "product_uom_qty": 1,
                                           "price_unit": 900.0, "tax_ids": [Command.clear()]})],
        })
        if code:
            order.distri_wa_code = code
        return order

    # ------------------------------------------------------------ helpers

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _csrf(self):
        page = self.url_open("/revisar")
        self.assertEqual(page.status_code, 200)
        return CSRF_RE.search(page.text).group(1)

    def _post(self, path, data=None, fetch=True):
        data = dict(data or {}, csrf_token=self._csrf())
        return self.url_open(path, data=data, headers=FETCH if fetch else None,
                             allow_redirects=False)

    # ------------------------------------------------------------ access

    def test_access_per_group(self):
        for user in (self.armador, self.repartidor, self.vendedor, self.cajero):
            self._login(user)
            self.assertEqual(self.url_open("/revisar").status_code, 403, user.login)
            res = self.url_open("/revisar/%s/liberar" % self.rojo.id, data={"csrf_token": "x"},
                                headers=FETCH, allow_redirects=False)
            self.assertIn(res.status_code, (400, 403), user.login)
            self.assertFalse(self.rojo.distri_retenido)
        for user in (self.admin, self.gerente):
            self._login(user)
            page = self.url_open("/revisar")
            self.assertEqual(page.status_code, 200, user.login)
            self.assertIn("Pedidos para revisar", page.text)

    def test_page_renders_semaphore_and_reasons(self):
        self.assertEqual(self.rojo.distri_semaforo, "rojo")
        self.assertEqual(self.amarillo.distri_semaforo, "amarillo")
        self.assertEqual(self.verde.distri_semaforo, "verde")
        self._login(self.gerente)
        text = self.url_open("/revisar").text
        for order in (self.rojo, self.amarillo, self.verde, self.web):
            self.assertIn('id="rv-%s"' % order.id, text)
        self.assertRegex(text, r'class="rv-card rv-card--rojo"[^>]*id="rv-%s"' % self.rojo.id)
        self.assertIn("Cliente nuevo · Sin compras anteriores", text)
        self.assertIn('class="rv-why rv-why--y">Sin WhatsApp<', text)
        self.assertIn("Sin cliente asignado", text)
        self.assertIn("rv-card--compact", text, "green cards fold to one line")
        self.assertIn("Kiosco de la Web V108", text, "typed name of an order without customer")
        self.assertIn("Escribió: 299 000-3333", text)
        self.assertIn("$ 900", text)
        self.assertIn('href="/odoo/sale.order/%s"' % self.rojo.id, text, "«Abrir en Odoo»")
        # red first
        self.assertLess(text.index('id="rv-%s"' % self.rojo.id),
                        text.index('id="rv-%s"' % self.verde.id))
        for tab in ("Todos", "Rojos", "Amarillos", "Retenidos"):
            self.assertIn('data-ver="%s"' % tab.lower(), text)
        self.assertNotIn("Aprobar", text, "owner: no «Aprobar» on the new screens")
        rojos = self.url_open("/revisar?ver=rojos").text
        self.assertRegex(rojos, r'id="rv-%s"[^>]*hidden' % self.verde.id)

    def test_copiar_codigo_markup(self):
        self._login(self.admin)
        text = self.url_open("/revisar").text
        line = "Pedido %s · código K7P3" % self.rojo.name
        self.assertIn('data-rv-copiar="%s"' % line, text)
        self.assertIn('<span class="rv-code__txt">%s</span>' % line, text)

    def test_when_text(self):
        now = fields.Datetime.now()
        self.assertTrue(cuando(now).startswith("hoy "))
        self.assertTrue(cuando(now - timedelta(days=1)).startswith("ayer "))
        self.assertEqual(cuando(False), "")

    # ------------------------------------------------------------ retener / liberar

    def test_retener_with_reason_and_liberar(self):
        self._login(self.admin)
        res = self._post("/revisar/%s/retener" % self.rojo.id, {"motivo": "  "})
        self.assertEqual(res.status_code, 400)
        self.assertIn("Escribí por qué retenés el pedido", res.json()["error"])
        self.assertFalse(self.rojo.distri_retenido)

        res = self._post("/revisar/%s/retener" % self.rojo.id,
                         {"motivo": "Pedido muy grande, confirmar por teléfono"})
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"])
        self.assertIn("Pedido retenido.", body["card"])
        self.assertIn("Motivo: Pedido muy grande, confirmar por teléfono", body["card"])
        self.assertIn("data-rv-liberar", body["card"])
        self.assertNotIn("data-rv-retener", body["card"])
        self.rojo.invalidate_recordset()
        self.assertTrue(self.rojo.distri_retenido)
        self.assertEqual(self.rojo.distri_retenido_por_id, self.admin)
        self.assertIn("retuvo el pedido", " ".join(self.rojo.message_ids.mapped(
            lambda m: str(m.body or ""))))
        res = self._post("/revisar/%s/retener" % self.rojo.id, {"motivo": "otra vez"})
        self.assertIn("ya está retenido", res.json()["error"])
        page = self.url_open("/revisar?ver=retenidos").text
        self.assertNotRegex(page, r'id="rv-%s"[^>]*hidden' % self.rojo.id)

        res = self._post("/revisar/%s/liberar" % self.rojo.id)
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("liberado", res.json()["message"])
        self.assertIn("data-rv-retener", res.json()["card"])
        self.rojo.invalidate_recordset()
        self.assertFalse(self.rojo.distri_retenido)

    def test_retener_without_javascript(self):
        self._login(self.gerente)
        page = self.url_open("/revisar?retener=%s" % self.amarillo.id).text
        self.assertIn('action="/revisar/%s/retener"' % self.amarillo.id, page)
        res = self._post("/revisar/%s/retener" % self.amarillo.id, {"motivo": "Revisar"},
                         fetch=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/revisar?ok=", res.headers["Location"])
        self.assertTrue(self.amarillo.distri_retenido)

    def test_liberar_refuses_an_anonymous_order_without_customer(self):
        self.web._distri_retener_sin_cliente()
        self.assertTrue(self.web.distri_retenido_sin_cliente)
        self._login(self.admin)
        res = self._post("/revisar/%s/liberar" % self.web.id)
        self.assertEqual(res.status_code, 400)
        self.assertIn("asignale el cliente", res.json()["error"])
        self.assertTrue(self.web.distri_retenido)

    # ------------------------------------------------------------ pegar número

    def test_pegar_numero_reads_warns_and_saves(self):
        self._login(self.admin)
        res = self.url_open("/revisar/%s/numero?pegado=%s" % (self.rojo.id, "‪" + PASTED + "‬"))
        data = res.json()
        self.assertEqual(data["preview"], "+54 9 299 000-1081")
        self.assertEqual((data["area"], data["numero"]), ("299", "000-1081"))
        self.assertIn("No es el teléfono que escribió el cliente en el pedido", data["aviso"])
        self.assertIn("299 000-2222", data["aviso"])
        self.assertIn(self.holder.name, [c["name"] for c in data["clientes"]])
        self.assertFalse(data["generic"])
        # typed boxes, no paste
        data = self.url_open("/revisar/%s/numero?area=0299&numero=15%%20000-1081"
                             % self.rojo.id).json()
        self.assertEqual(data["preview"], "+54 9 299 000-1081")
        bad = self.url_open("/revisar/%s/numero?pegado=Juan" % self.rojo.id).json()
        self.assertEqual(bad["error"], MSG_SIN_NUMERO)
        res = self._post("/revisar/%s/numero" % self.rojo.id, {"pegado": "Juan"})
        self.assertEqual(res.status_code, 400)
        self.assertFalse(self.nuevo.distri_whatsapp)

        res = self._post("/revisar/%s/numero" % self.rojo.id,
                         {"pegado": PASTED, "area": "299", "numero": "0001081"})
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn("WhatsApp verificado", res.json()["message"])
        self.nuevo.invalidate_recordset()
        self.assertEqual(self.nuevo.distri_whatsapp, "+5492990001081")
        self.assertTrue(self.nuevo.distri_whatsapp_confirmed)
        self.assertEqual(self.nuevo.distri_whatsapp_user_id, self.admin)
        # the customer has it now: «Ya lo tiene», and no «Sin WhatsApp» any more
        data = self.url_open("/revisar/%s/numero?pegado=%s" % (self.rojo.id, PASTED)).json()
        self.assertTrue(data["del_cliente"])
        self.assertNotIn("Sin WhatsApp", res.json()["card"])

    def test_pegar_numero_needs_the_group(self):
        self._login(self.vendedor)
        res = self.url_open("/revisar/%s/numero?pegado=%s" % (self.rojo.id, PASTED))
        self.assertEqual(res.status_code, 403)

    def test_order_without_customer_continues_to_asignar_cliente(self):
        """19.0.1.111.0: «Asignar cliente» is the kit screen (test_v111)."""
        self._login(self.admin)
        text = self.url_open("/revisar").text
        self.assertIn('href="/revisar/%s/asignar"' % self.web.id, text)
        data = self.url_open("/revisar/%s/numero?pegado=%s" % (self.web.id, PASTED)).json()
        self.assertTrue(data["generic"])
        res = self._post("/revisar/%s/asignar" % self.web.id, {"pegado": PASTED})
        self.assertEqual(res.status_code, 200, res.text)
        url = res.json()["url"]
        self.assertEqual(url, "/revisar/%s/asignar?pegado=%%2B54+9+299+000-1081" % self.web.id)
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200)
        self.assertRegex(page.text, r'as-cand as-cand--on" data-id="%s"' % self.holder.id,
                         "the only customer with it is chosen")
        # an order with its customer has no «Asignar cliente»
        res = self._post("/revisar/%s/asignar" % self.rojo.id)
        self.assertEqual(res.status_code, 400)

    # ------------------------------------------------------------ tablero

    def test_tablero_tiles_and_badges_per_group(self):
        def urls(user):
            return [t["url"] for t in staff_tiles(self.env(user=user))]

        for user in (self.admin, self.gerente):
            self.assertIn("/revisar", urls(user), user.login)
            self.assertIn("/pedido", urls(user), user.login)
        for user in (self.cajero, self.armador, self.repartidor):
            self.assertNotIn("/revisar", urls(user), user.login)
            self.assertNotIn("/pedido", urls(user), user.login)
        self.assertIn("/pedido", urls(self.vendedor))
        self.assertNotIn("/revisar", urls(self.vendedor))
        self.assertIn("/caja", urls(self.cajero))
        self.assertNotIn("/caja", urls(self.gerente), "only the cashier group has «Caja del día»")

        env = self.env(user=self.gerente)
        tiles = staff_tiles(env)
        banner = tile_badges(env, tiles)
        by_key = {t["key"]: t for t in tiles}
        rojos = self.env["sale.order"].search_count([
            ("distri_revisar_en_lista", "=", True), ("distri_semaforo", "=", "rojo"),
            ("distri_retenido", "=", False), ("distri_armado", "=", "pendiente")])
        self.assertGreaterEqual(rojos, 2, "the new customer's and the web order")
        self.assertEqual(by_key["revisar"]["badge"]["n"], rojos)
        self.assertEqual(by_key["revisar"]["badge"]["kind"], "err")
        self.assertIn("%s pedidos en rojo esperan revisión" % rojos, banner["text"])
        self.assertEqual(banner["url"], "/revisar?ver=rojos")
        # a held order leaves the count
        self.rojo.with_user(self.gerente).distri_retener("Revisar")
        tiles = staff_tiles(env)
        tile_badges(env, tiles)
        self.assertEqual({t["key"]: t for t in tiles}["revisar"]["badge"]["n"], rojos - 1)

        # the cashier: no red banner, «Abierta» once the cash is open
        env = self.env(user=self.cajero)
        tiles = staff_tiles(env)
        self.assertIsNone(tile_badges(env, tiles))
        self.assertIsNone({t["key"]: t for t in tiles}["caja"]["badge"])
        self.env["distri.caja"].with_user(self.cajero).open_session(1000)
        tiles = staff_tiles(env)
        tile_badges(env, tiles)
        self.assertEqual({t["key"]: t for t in tiles}["caja"]["badge"]["text"], "Abierta")
        # gerencia's «Historial de cajas» counts the open sessions
        env = self.env(user=self.gerente)
        tiles = staff_tiles(env)
        tile_badges(env, tiles)
        historial = {t["key"]: t for t in tiles}["historial"]["badge"]
        self.assertTrue(historial and historial["n"] >= 1)

    def test_tablero_stays_cheap(self):
        env = self.env(user=self.gerente)
        staff_tiles(env)  # warm the menu cache, as any request after the first
        env.invalidate_all()
        before = self.env.cr.sql_log_count
        tiles = staff_tiles(env)
        tile_badges(env, tiles)
        my_route_today(env)
        queries = self.env.cr.sql_log_count - before
        self.assertLessEqual(queries, 25, "the tablero must stay one cheap request")

    def test_tablero_page(self):
        self._login(self.gerente)
        page = self.url_open("/tablero")
        self.assertEqual(page.status_code, 200)
        text = page.text
        self.assertIn('href="/revisar"', text)
        self.assertIn('href="/pedido"', text)
        self.assertIn("Tomar un pedido", text)
        self.assertIn("pedidos en rojo esperan revisión antes de armarse", text)
        self.assertIn('href="/revisar?ver=rojos"', text)
        self.assertRegex(text, r'class="tab-badge dn-num tab-badge--err">\d+ rojos( · [^<]*)?<')
        self.assertIn("Todas las aplicaciones de Odoo", text)
        self.assertIn('href="/deposito"', text, "19.0.1.109.0: the «Depósito» tile")
        self.assertNotIn("Aprobar", text)
        self._login(self.cajero)
        text = self.url_open("/tablero").text
        self.assertIn('href="/caja"', text)
        self.assertNotIn('href="/revisar"', text)
        self.assertNotIn("pedidos en rojo", text)
