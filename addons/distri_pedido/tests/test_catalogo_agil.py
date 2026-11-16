"""TEMPORARY catalog copied from the AgilPedido storefront (19.0.1.89.0,
models/catalogo_agil.py): the sync, its safety brakes, the switch on /pedido,
the same price in every list, the WhatsApp importer and an order with a
temporary product from confirmation to invoice. Synthetic data only."""
import base64
import io
import json
import re
from collections import Counter
from unittest.mock import MagicMock, patch

import requests
from PIL import Image

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL, LISTA_OCULTA
from odoo.addons.distri_pedido.models.agil_tienda import (
    PARAM_CATALOGO_AGIL, FeedInvalido, filas_agil, leer_feed, motivo_para_abortar,
)
from odoo.addons.distri_pedido.models.importar_pedido import (
    catalogo_normalizado, importe_ar, leer_renglon,
)
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin, post_pedido
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

# AgilPedido shows sections by `posicion`, not by name: Zeta goes first
SECCIONES = [
    {"id": 91002, "nombre": "Alfa Pruebagil 😄 ", "posicion": 1},
    {"id": 91001, "nombre": "Zeta Pruebagil ", "posicion": 0},
]
GALLETITA = {"id": 9900001, "nombre": "Galletita Pruebagil 500G", "precio": 123450,
             "seccion_id": 91002}
BULTO = {"id": 9900002, "nombre": "Bulto x 30 u. Galletita Pruebagil", "precio": 3500000,
         "seccion_id": 91002}
MAY = {"id": 9900003, "nombre": "May Galletita Pruebagil 500G", "precio": 100000,
       "seccion_id": 91001}
CIGARRILLO = {"id": 9900004, "nombre": "Cigarrillo Pruebagil 20", "precio": 250000,
              "seccion_id": 91001}
PRODUCTOS = [GALLETITA, BULTO, MAY, CIGARRILLO]


def storefront(productos=None, secciones=None):
    """The storefront page as AgilPedido serves it: data inside a script."""
    return ("<!DOCTYPE html><html><head><script>window.serverExports = {};"
            "serverExports.empresa={\"nombre\": \"Prueba\"};"
            "serverExports.secciones=%s;serverExports.productos=%s;"
            "</script></head><body></body></html>" % (
                json.dumps(SECCIONES if secciones is None else secciones),
                json.dumps(PRODUCTOS if productos is None else productos)))


def cambiar(producto, **valores):
    return dict(producto, **valores)


class AgilMixin:

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # A database that already ran the real sync holds hundreds of
        # temporary products: set them aside (inside the test transaction)
        # so each test starts from the small sample feed alone.
        cls.env["product.template"].with_context(active_test=False).search(
            [("distri_agil_temporal", "=", True)]).write(
            {"distri_agil_temporal": False, "distri_agil_id": False,
             "is_published": False})
        # and the usual catalog, as installed: production may have it on
        cls._switch("0")

    @classmethod
    def _sync(cls, contenido=None, env=None):
        Sinc = (env or cls.env)["distri.agil.sincronizacion"]
        return Sinc._distri_ejecutar(storefront() if contenido is None else contenido,
                                     origen="test")

    @classmethod
    def _temporal(cls, producto):
        return cls.env["product.template"].with_context(active_test=False).search(
            [("distri_agil_id", "=", str(producto["id"]))])

    @classmethod
    def _switch(cls, valor):
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_CATALOGO_AGIL, valor)


@tagged("post_install", "-at_install", "distri_catalogo_agil")
class TestCatalogoAgilSync(AgilMixin, TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Tmpl = cls.env["product.template"]
        # an existing product with the same name: its history is never touched
        cls.viejo = Tmpl.create({"name": "Galletita Pruebagil 500G", "list_price": 999.0,
                                 "type": "consu", "sale_ok": True, "is_published": True,
                                 "taxes_id": [Command.clear()]})
        # an Odoo category with the section name (without its trailing space)
        cls.zeta = cls.env["product.category"].create({"name": "Zeta Pruebagil"})

    # ------------------------------------------------------------ helpers puros

    def test_lee_html_y_json(self):
        feed = leer_feed(storefront())
        self.assertEqual(len(feed["productos"]), 4)
        self.assertEqual(leer_feed(json.dumps(feed))["productos"], feed["productos"])
        self.assertEqual(leer_feed(json.dumps({"serverExports": feed}).encode())["secciones"],
                         SECCIONES)
        filas, rechazados = filas_agil(feed)
        self.assertFalse(rechazados)
        self.assertEqual(filas[0], {"agil_id": "9900001", "nombre": "Galletita Pruebagil 500G",
                                    "precio": 1234.5, "seccion": "Alfa Pruebagil 😄",
                                    "seccion_orden": 1, "orden": 0, "imagen_url": "",
                                    "imagen_clave": ""})
        for malo in ("", "<html>sin datos</html>", "{no es json", '{"otra": 1}'):
            with self.assertRaises(FeedInvalido, msg=malo):
                leer_feed(malo)
        filas, rechazados = filas_agil({"secciones": [], "productos": [
            {"id": 1, "nombre": "", "precio": 100}, {"id": 2, "nombre": "X", "precio": "100"},
            {"id": 3, "nombre": "Y", "precio": 100}, {"id": 3, "nombre": "Z", "precio": 100}]})
        self.assertEqual([f["nombre"] for f in filas], ["Y"])
        self.assertEqual(filas[0]["seccion"], "Otros")
        self.assertEqual(len(rechazados), 3)
        self.assertTrue(motivo_para_abortar(0, 0))
        self.assertTrue(motivo_para_abortar(4, 10))
        self.assertFalse(motivo_para_abortar(5, 10))
        self.assertFalse(motivo_para_abortar(3, 0))

    # ------------------------------------------------------------ sync

    def test_crea_actualiza_y_oculta(self):
        corrida = self._sync()
        self.assertEqual((corrida.estado, corrida.leidos, corrida.creados), ("ok", 4, 4))
        galletita = self._temporal(GALLETITA)
        self.assertEqual(galletita.name, "Galletita Pruebagil 500G")
        self.assertEqual(galletita.list_price, 1234.5)
        self.assertTrue(galletita.distri_agil_temporal and galletita.is_published
                        and galletita.sale_ok)
        self.assertEqual((galletita.distri_agil_seccion, galletita.distri_agil_seccion_orden,
                          galletita.distri_agil_orden), ("Alfa Pruebagil 😄", 1, 0))
        # sold per 1 unit, invoiced on the order, without taxes: like today's
        self.assertEqual(galletita.uom_id, self.env.ref("uom.product_uom_unit"))
        self.assertFalse(galletita.uom_ids)
        self.assertFalse(galletita.taxes_id)
        self.assertEqual((galletita.type, galletita.invoice_policy), ("consu", "order"))
        # «Bulto x 30 u.» is its own row, priced per bulto, factor 1
        bulto = self._temporal(BULTO)
        self.assertEqual((bulto.list_price, bulto.uom_id), (35000.0, galletita.uom_id))
        # the section maps to the existing category; an unknown one is created
        self.assertEqual(self._temporal(MAY).categ_id, self.zeta)
        self.assertTrue(galletita.categ_id.distri_agil_temporal)
        self.assertEqual(galletita.categ_id.name, "Alfa Pruebagil 😄")
        # the existing product with the same name: untouched
        self.assertEqual((self.viejo.name, self.viejo.list_price, self.viejo.is_published),
                         ("Galletita Pruebagil 500G", 999.0, True))
        self.assertFalse(self.viejo.distri_agil_temporal)
        self.assertIn("Creados: 4", corrida.message_ids[:1].body)

        # second run: a price, a name, one row gone and a new one
        nueva = {"id": 9900005, "nombre": "Nueva Pruebagil", "precio": 50000,
                 "seccion_id": 91001}
        corrida = self._sync(storefront([
            cambiar(GALLETITA, precio=130000),
            cambiar(BULTO, nombre="Bulto x 30 u. Galletita Pruebagil NUEVA"),
            CIGARRILLO, nueva]))
        self.assertEqual((corrida.estado, corrida.creados, corrida.precios, corrida.renombrados,
                          corrida.despublicados, corrida.republicados),
                         ("ok", 1, 1, 1, 1, 0))
        self.assertEqual(galletita.list_price, 1300.0)
        self.assertTrue(any("1.234,50 → $ 1.300,00" in (m.body or "")
                            for m in galletita.message_ids))
        self.assertEqual(bulto.name, "Bulto x 30 u. Galletita Pruebagil NUEVA")
        for codigo, _nombre in self.env["res.lang"].get_installed():
            self.assertEqual(bulto.with_context(lang=codigo).name,
                             "Bulto x 30 u. Galletita Pruebagil NUEVA", codigo)
        may = self._temporal(MAY)
        self.assertTrue(may.exists(), "never deleted: it can be in an invoice")
        self.assertFalse(may.is_published)
        self.assertIn("May Galletita Pruebagil 500G", corrida.resumen)
        self.assertIn("Nueva Pruebagil", corrida.resumen)
        self.assertEqual(self.viejo.list_price, 999.0)

        # back in AgilPedido: shown again; nothing else changed
        corrida = self._sync(storefront([cambiar(GALLETITA, precio=130000),
                                         cambiar(BULTO, nombre=bulto.name), MAY, CIGARRILLO,
                                         nueva]))
        self.assertEqual((corrida.creados, corrida.precios, corrida.renombrados,
                          corrida.despublicados, corrida.republicados), (0, 0, 0, 0, 1))
        self.assertTrue(may.is_published)

    def test_aborta_sin_tocar_nada(self):
        self._sync()
        galletita = self._temporal(GALLETITA)
        malos = [
            storefront([]),                       # 0 products
            "<html><body>Mantenimiento</body></html>",
            storefront([cambiar(GALLETITA, precio=1)]),  # 1 of 4 visible: < 50 %
            "",
        ]
        for contenido in malos:
            corrida = self._sync(contenido)
            self.assertEqual(corrida.estado, "error", contenido[:60])
            self.assertTrue(corrida.error)
            self.assertIn("No se aplicó", corrida.message_ids[:1].body)
        self.assertEqual(galletita.list_price, 1234.5)
        temporales = self.env["product.template"].search([("distri_agil_temporal", "=", True),
                                                          ("distri_agil_id", "like", "99000")])
        self.assertEqual(len(temporales), 4)
        self.assertTrue(all(temporales.mapped("is_published")))

        # the live fetch: a network error is a clean error; a page is applied
        Sinc = self.env["distri.agil.sincronizacion"]
        with patch("odoo.addons.distri_pedido.models.catalogo_agil.requests.get",
                   side_effect=requests.ConnectionError("sin red")):
            corrida = Sinc._distri_ejecutar()
        self.assertEqual(corrida.estado, "error")
        self.assertIn("No se pudo descargar", corrida.error)
        respuesta = MagicMock(content=storefront([cambiar(GALLETITA, precio=140000), BULTO, MAY,
                                                  CIGARRILLO]).encode())
        with patch("odoo.addons.distri_pedido.models.catalogo_agil.requests.get",
                   return_value=respuesta) as get:
            corrida = Sinc._distri_ejecutar()
        self.assertEqual((corrida.estado, corrida.precios), ("ok", 1))
        self.assertEqual(get.call_args.kwargs["timeout"], 30)
        self.assertEqual(galletita.list_price, 1400.0)

    def test_id_de_un_producto_definitivo_no_se_toca(self):
        self.viejo.distri_agil_id = str(GALLETITA["id"])
        corrida = self._sync()
        self.assertEqual((corrida.estado, corrida.creados, corrida.fallidos), ("ok", 3, 1))
        self.assertIn("producto definitivo", corrida.resumen)
        self.assertEqual((self.viejo.list_price, self.viejo.distri_agil_temporal), (999.0, False))

    def test_asistente_con_archivo_y_permisos(self):
        admin = new_test_user(self.env, login="admin_agil", name="Administracion Agil",
                              groups="base.group_user,distri_pedido.grupo_administracion")
        archivo = base64.b64encode(json.dumps(leer_feed(storefront())).encode())
        wizard = self.env["distri.agil.sincronizar"].with_user(admin).create(
            {"archivo": archivo, "nombre_archivo": "tienda.json"})
        self.assertFalse(wizard.catalogo_activo)
        accion = wizard.action_actualizar()
        corrida = self.env["distri.agil.sincronizacion"].browse(accion["res_id"])
        self.assertEqual((corrida.estado, corrida.creados, corrida.origen),
                         ("ok", 4, "Archivo tienda.json"))
        # Administración updates; only Gerencia flips the switch
        with self.assertRaises(AccessError):
            wizard.action_mostrar_catalogo_agil()
        gerencia = new_test_user(self.env, login="gerencia_agil", name="Gerencia Agil",
                                 groups="base.group_user,distri_pedido.grupo_gerencia")
        wizard = self.env["distri.agil.sincronizar"].with_user(gerencia).create({})
        wizard.action_mostrar_catalogo_agil()
        self.assertEqual(self.env["ir.config_parameter"].get_param(PARAM_CATALOGO_AGIL), "1")
        wizard.action_volver_catalogo_odoo()
        self.assertEqual(self.env["ir.config_parameter"].get_param(PARAM_CATALOGO_AGIL), "0")
        vendedor = new_test_user(self.env, login="vendedor_agil", name="Vendedor Agil",
                                 groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(AccessError):
            self.env["distri.agil.sincronizar"].with_user(vendedor).create({})

    def test_instalar_no_cambia_nada(self):
        cron = self.env.ref("distri_pedido.cron_catalogo_agil")
        self.assertFalse(cron.active)
        # installed once a day; administración may run it more often since
        # (the record is not updated again), so a copy of production may differ
        self.assertIn(cron.interval_type, ("days", "hours"))
        self.assertEqual(cron.code.strip(), "model._cron_distri_sincronizar()")
        self.assertEqual(self.env.ref("distri_pedido.param_catalogo_agil").key, PARAM_CATALOGO_AGIL)

    def test_mismo_precio_en_todas_las_listas(self):
        Pricelist = self.env["product.pricelist"]
        currency = self.env.company.currency_id.id
        general = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL, "currency_id": currency})
        oculta = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA, "currency_id": currency,
                              "distri_mayorista": True})
        # like the real «Revendedor NN»: everything not listed, from Distribuidoras
        Pricelist.create({"name": "Revendedor Pruebagil", "currency_id": currency,
                          "item_ids": [Command.create({
                              "applied_on": "3_global", "compute_price": "formula",
                              "base": "pricelist", "base_pricelist_id": oculta.id})]})
        self._sync()
        moneda = self.env.company.currency_id
        listas = Pricelist.search([]).filtered(lambda l: l.currency_id == moneda)
        self.assertIn(general, listas)
        temporales = [self._temporal(p) for p in PRODUCTOS]
        for lista in listas:
            for producto, fila in zip(temporales, PRODUCTOS):
                for cantidad in (1.0, 6.0, 120.0):
                    self.assertEqual(
                        lista._get_product_price(producto.product_variant_id, cantidad),
                        fila["precio"] / 100.0, "%s / %s x %s" % (lista.name, producto.name,
                                                                  cantidad))

    def test_importador_whatsapp_prefiere_el_temporal(self):
        self._sync()
        SaleOrder = self.env["sale.order"].sudo()
        mayorista, minorista = SaleOrder._distri_agil_listas()

        def renglon(texto):
            leido = leer_renglon(texto)
            leido["importe_valor"] = importe_ar(leido["importe"])
            catalogo = catalogo_normalizado(SaleOrder.env)
            return SaleOrder._distri_agil_renglon(leido, catalogo, Counter(catalogo.values()),
                                                  mayorista, minorista)

        texto = "- 2 x Galletita Pruebagil 500G ($ 2.469,00)"
        # switch off: today's matching, the temporary copies do not count
        self._switch("0")
        self.assertEqual(renglon(texto)["product_id"], self.viejo.product_variant_id.id)
        self._switch("1")
        valores = renglon(texto)
        galletita = self._temporal(GALLETITA)
        self.assertEqual(valores["product_id"], galletita.product_variant_id.id)
        self.assertEqual(valores["price_unit"], 1234.5)
        self.assertFalse(valores["distri_agil_corregir"])
        # the «May» row is its own product, at its own price
        valores = renglon("- 1 x May Galletita Pruebagil 500G ($ 1.000,00)")
        self.assertEqual(valores["product_id"], self._temporal(MAY).product_variant_id.id)
        self.assertEqual(valores["price_unit"], 1000.0)


@tagged("post_install", "-at_install", "distri_catalogo_agil")
class TestCatalogoAgilPagina(PedidoAbiertoMixin, AgilMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Pricelist = cls.env["product.pricelist"]
        currency = cls.env.company.currency_id.id
        cls.general = Pricelist.search([("name", "=", LISTA_GENERAL)], limit=1) or \
            Pricelist.create({"name": LISTA_GENERAL, "currency_id": currency})
        cls.oculta = Pricelist.search([("name", "=", LISTA_OCULTA)], limit=1) or \
            Pricelist.create({"name": LISTA_OCULTA, "currency_id": currency,
                              "distri_mayorista": True})
        cls.revendedor = Pricelist.create({
            "name": "Revendedor Pruebagil", "currency_id": currency, "distri_mayorista": True,
            "item_ids": [Command.create({"applied_on": "3_global", "compute_price": "formula",
                                         "base": "pricelist",
                                         "base_pricelist_id": cls.oculta.id})]})
        cls.regular = cls.env["product.template"].create({
            "name": "Regular Pruebagil Visible", "list_price": 4321.0, "type": "consu",
            "sale_ok": True, "is_published": True, "taxes_id": [Command.clear()]})
        # a regular product with a «May» price: the DISTRIBUIDORAS section
        cls.env["product.pricelist.item"].create({
            "pricelist_id": cls.oculta.id, "applied_on": "1_product",
            "product_tmpl_id": cls.regular.id, "compute_price": "fixed", "fixed_price": 3333.0})
        cls._sync(storefront(PRODUCTOS + [
            {"id": 9900009, "nombre": "Oculto Pruebagil", "precio": 70000, "seccion_id": 91001}]))
        # hidden in AgilPedido afterwards: unpublished here
        cls._sync()
        cls.galletita = cls._temporal(GALLETITA)
        cls.cigarrillo = cls._temporal(CIGARRILLO)
        cls.oculto = cls.env["product.template"].with_context(active_test=False).search(
            [("distri_agil_id", "=", "9900009")])
        Partner = cls.env["res.partner"]
        for login, valores in (
                ("distri_agil", {"distri_lista_oculta": True,
                                 "property_product_pricelist": cls.general.id}),
                ("revende_agil", {"distri_lista_oculta": True,
                                  "property_product_pricelist": cls.revendedor.id})):
            empresa = Partner.create(dict(valores, name="Empresa %s" % login,
                                          company_type="company"))
            contacto = Partner.create({"name": "Contacto %s" % login, "parent_id": empresa.id,
                                       "type": "contact"})
            new_test_user(cls.env, login=login, groups="base.group_portal",
                          partner_id=contacto.id)

    def _html(self, url="/pedido"):
        res = self.url_open(url)
        self.assertEqual(res.status_code, 200)
        return res.text

    def _fila(self, producto):
        return 'data-id="%s"' % producto.id

    def test_switch_apagado_es_el_catalogo_de_siempre(self):
        self._switch("0")
        html = self._html()
        self.assertIn(self._fila(self.regular), html)
        for temporal in (self.galletita, self.cigarrillo, self.oculto):
            self.assertNotIn(self._fila(temporal), html)
        self.assertNotIn("Alfa Pruebagil", html)
        self.assertNotIn("Alfa Pruebagil", self._html("/"))

    def test_switch_encendido_muestra_solo_agilpedido(self):
        self._switch("1")
        html = self._html()
        self.assertNotIn(self._fila(self.regular), html)
        self.assertNotIn(self._fila(self.oculto), html, "hidden in AgilPedido")
        self.assertIn('data-id="%s" data-precio="1234.5"' % self.galletita.id, html)
        # the cigarette shows its price, like the storefront
        self.assertIn('data-id="%s" data-precio="2500.0"' % self.cigarrillo.id, html)
        self.assertNotRegex(html, r'data-id="%s"[^>]*data-sinprecio="1"' % self.cigarrillo.id)
        # sections with the storefront name and order (by position, not by name)
        self.assertIn("Alfa Pruebagil 😄", html)
        self.assertLess(html.index("Zeta Pruebagil"), html.index("Alfa Pruebagil"))
        # inside a section, the storefront order
        self.assertLess(html.index(self._fila(self._temporal(MAY))),
                        html.index(self._fila(self.cigarrillo)))
        portada = self._html("/")
        self.assertLess(portada.index("Zeta Pruebagil"), portada.index("Alfa Pruebagil"))
        # and back, instantly
        self._switch("0")
        self.assertIn(self._fila(self.regular), self._html())

    def test_distribuidora_y_revendedor_ven_el_precio_de_agilpedido(self):
        self._switch("1")
        for login in ("distri_agil", "revende_agil"):
            self.authenticate(login, login)
            html = self._html()
            self.assertIn('data-id="%s" data-precio="1234.5"' % self.galletita.id, html, login)
            self.assertIn('data-id="%s" data-precio="1000.0"' % self._temporal(MAY).id, html)
            self.assertNotIn("data-may", html, "no DISTRIBUIDORAS section: AgilPedido has none")
            self.assertNotIn("3333", html)

    def test_pedido_con_un_temporal(self):
        self._switch("1")
        post_pedido(self, {
            "renglones": json.dumps([{"id": self.galletita.id, "cantidad": 60},
                                     {"id": self.cigarrillo.id, "cantidad": 1, "may": 1}]),
            "nombre": "Cliente Pruebagil", "telefono": "2995550199", "domicilio": "Calle 1",
        })
        pedido = self.env["sale.order"].search([("distri_nombre", "=", "Cliente Pruebagil")],
                                               order="id desc", limit=1)
        self.assertTrue(pedido)
        precios = {l.product_id.product_tmpl_id: l.price_unit for l in pedido.order_line}
        self.assertEqual(precios, {self.galletita: 1234.5, self.cigarrillo: 2500.0})
        self.assertEqual(pedido.amount_total, 60 * 1234.5 + 2500.0)
        self.assertNotIn("SIN PRECIO", pedido.note or "")
        self.assertTrue(re.search(r"ASIGNAR CLIENTE", str(pedido.note)))


@tagged("post_install", "-at_install", "distri_catalogo_agil")
class TestCatalogoAgilFactura(AgilMixin, RepartoMixin, AccountTestInvoicingCommon):
    """A temporary product goes the whole way: confirmation, the warehouse
    (armado), the automatic invoice and the delivery route."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls.armador = new_test_user(
            cls.env, login="armador_agil", name="Armador Agil",
            groups="base.group_user,distri_pedido.grupo_armador",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cls._setup_reparto()
        cls._sync()
        cls.galletita = cls._temporal(GALLETITA)
        cls.lista = cls.env["product.pricelist"].create({
            "name": "Factura agil", "company_id": cls.env.company.id,
            "currency_id": cls.env.company.currency_id.id})

    def test_confirma_arma_factura_y_reparte(self):
        order = self.env["sale.order"].create({
            "partner_id": self.partner_a.id,
            "pricelist_id": self.lista.id,
            "order_line": [Command.create({"product_id": self.galletita.product_variant_id.id,
                                           "product_uom_qty": 4})],
        })
        linea = order.order_line
        self.assertEqual(linea.price_unit, 1234.5, "the AgilPedido price, from the list")
        self.assertFalse(linea.tax_ids, "no taxes: the price is final")
        order.action_confirm()
        self.assertEqual(order.state, "sale")
        self.assertTrue(order.picking_ids)

        pedido = order.with_user(self.armador).sudo()
        self.assertEqual(pedido.distri_tomar(), "")
        self.assertEqual(pedido.distri_terminar(1), "")
        self.assertTrue(all(p.state == "done" for p in order.picking_ids))
        self.assertEqual(linea.qty_delivered, 4)

        factura = order.invoice_ids.filtered(lambda f: f.move_type == "out_invoice")
        self.assertEqual(len(factura), 1)
        self.assertEqual(factura.state, "posted")
        self.assertEqual(factura.amount_total, 4 * 1234.5)
        self.assertEqual(factura.invoice_line_ids.product_id, self.galletita.product_variant_id)
        self.assertTrue(factura.invoice_line_ids.account_id)
        self.assertEqual(order.invoice_status, "invoiced")

        route = self._route(order)
        self.assertEqual(route.stop_ids.order_amount, 4 * 1234.5)


def png(color):
    buffer = io.BytesIO()
    Image.new("RGB", (8, 8), color).save(buffer, "PNG")
    return buffer.getvalue()


FOTO = "https://fotos.example/obj/galletita"


@tagged("post_install", "-at_install", "distri_catalogo_agil")
class TestCatalogoAgilFotos(AgilMixin, TransactionCase):
    """The storefront photos: downloaded once, again only when AgilPedido
    changes the photo, cleared when it drops it; a bad photo never stops the
    sync. HTTP is mocked."""

    def _sync_fotos(self, productos, respuestas):
        """Sync with `respuestas`: {url without query: bytes or exception}.
        Returns (run, urls requested)."""
        pedidas = []

        def get(url, **kw):
            pedidas.append(url)
            respuesta = respuestas[url.split("?")[0]]
            if isinstance(respuesta, Exception):
                raise respuesta
            return MagicMock(content=respuesta)

        with patch("odoo.addons.distri_pedido.models.catalogo_agil.requests.get",
                   side_effect=get):
            corrida = self._sync(storefront(productos))
        return corrida, pedidas

    def test_foto_al_crear_sin_rebajar_y_reemplazo(self):
        roja, azul = png("red"), png("blue")
        firmada = FOTO + "?X-Amz-Date=20261115&X-Amz-Signature=aaa"
        corrida, pedidas = self._sync_fotos(
            [cambiar(GALLETITA, imagen_url=firmada), BULTO, MAY, CIGARRILLO], {FOTO: roja})
        galletita = self._temporal(GALLETITA)
        self.assertEqual((corrida.estado, corrida.imagenes, corrida.imagenes_fallidas,
                          corrida.sin_imagen), ("ok", 1, 0, 3))
        self.assertEqual(pedidas, [firmada], "the signed link, as AgilPedido gives it")
        self.assertTrue(galletita.image_1920)
        self.assertEqual(galletita.distri_agil_imagen_url, FOTO)
        primera = galletita.image_1920

        # re-signed link, same photo: not downloaded again
        corrida, pedidas = self._sync_fotos(
            [cambiar(GALLETITA, imagen_url=FOTO + "?X-Amz-Signature=bbb"), BULTO, MAY,
             CIGARRILLO], {FOTO: azul})
        self.assertEqual((pedidas, corrida.imagenes), ([], 0))
        self.assertEqual(galletita.image_1920, primera)

        # another photo: replaced
        otra = "https://fotos.example/obj/galletita-nueva"
        corrida, pedidas = self._sync_fotos(
            [cambiar(GALLETITA, imagen_url=otra + "?s=1"), BULTO, MAY, CIGARRILLO], {otra: azul})
        self.assertEqual(corrida.imagenes, 1)
        self.assertNotEqual(galletita.image_1920, primera)
        self.assertEqual(galletita.distri_agil_imagen_url, otra)

        # no photo in AgilPedido any more: cleared
        corrida, pedidas = self._sync_fotos(PRODUCTOS, {})
        self.assertFalse(galletita.image_1920)
        self.assertFalse(galletita.distri_agil_imagen_url)

    def test_foto_fallida_se_cuenta_y_sigue(self):
        viejo = self.env["product.template"].create({
            "name": "Definitivo Pruebagil", "type": "consu", "taxes_id": [Command.clear()],
            "distri_agil_id": str(CIGARRILLO["id"]), "image_1920": base64.b64encode(png("green"))})
        foto_viejo = viejo.image_1920
        rota = "https://fotos.example/obj/rota"
        basura = "https://fotos.example/obj/basura"
        corrida, pedidas = self._sync_fotos([
            cambiar(GALLETITA, imagen_url=FOTO),
            cambiar(BULTO, imagen_url=rota),
            cambiar(MAY, imagen_small_url=basura),
            cambiar(CIGARRILLO, imagen_url="https://fotos.example/obj/definitivo"),
        ], {FOTO: png("red"), rota: requests.Timeout("lenta"), basura: b"no es una imagen"})
        self.assertEqual((corrida.estado, corrida.creados, corrida.imagenes,
                          corrida.imagenes_fallidas), ("ok", 3, 1, 2))
        self.assertIn("Bulto x 30 u. Galletita Pruebagil", corrida.resumen)
        self.assertTrue(self._temporal(GALLETITA).image_1920)
        bulto = self._temporal(BULTO)
        self.assertFalse(bulto.image_1920)
        self.assertFalse(bulto.distri_agil_imagen_url, "retried on the next run")
        # the definitive product: neither downloaded nor touched
        self.assertNotIn("https://fotos.example/obj/definitivo", pedidas)
        self.assertEqual(viejo.image_1920, foto_viejo)
