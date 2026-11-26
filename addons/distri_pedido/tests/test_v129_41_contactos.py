"""19.0.1.129.41: «Contactos» on the kit (behind distri_pedido.ui_v2).

- /contactos: search by name, phone (any format), CUIT, address and code;
  the daily filters; one page per query (never every contact), with the
  balances of that page only, the same as _distri_account_balance.
- /clientes/<id>/editar: administración edits every field the team uses
  (written as the user, with the backend's checks); a seller only WhatsApp,
  address and town of a customer he reaches (19.0.1.129.40's pattern);
  packers and drivers are refused; posts need the CSRF token.
- «Nuevo contacto» is 19.0.1.129.40's «Nuevo cliente».
- «Confirmar» and «Unir con…» for customers «a confirmar»: merged only
  without accounting, otherwise only noted.
- With the flag off every route answers 404 and the links are the old ones.
Every test leaves the ledger exactly as it was. Synthetic data only.
"""
import json
import os
import re
import shutil
import subprocess
from unittest import SkipTest
from unittest.mock import patch

from odoo import Command, fields
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2, role_nav, screen_parents
from odoo.addons.distri_pedido.controllers.tablero import staff_tiles
from odoo.addons.distri_pedido.models import contactos as ct
from odoo.addons.distri_pedido.models.verificacion import _partner_url

CSRF_RE = re.compile(r'(?:name="csrf_token" value|name="csrf-token" content)="([^"]+)"')
CUIT_OK = "20123456786"


class V12941Mixin:
    """The users, customers and helpers of these tests (shared with 129.45)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        # price lists are on in production (the customer's list is edited here)
        cls.env["res.config.settings"].create({"group_product_pricelist": True}).execute()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.tag = cls.env["res.partner.category"].create({"name": "Cartera V141"})
        cls.tag2 = cls.env["res.partner.category"].create({"name": "Cartera Otra V141"})
        cls.admin = user("admin_v141", "distri_pedido.grupo_administracion")
        cls.gerente = user("gerente_v141", "distri_pedido.grupo_gerencia")
        cls.vendedor = user("vendedor_v141", "distri_pedido.grupo_vendedor")
        cls.vendedor.sudo().etiqueta_cartera_id = cls.tag
        cls.otro = user("otro_v141", "distri_pedido.grupo_vendedor")
        cls.otro.sudo().etiqueta_cartera_id = cls.tag2
        cls.externo = user("externo_v141", "distri_pedido.grupo_vendedor")
        cls.externo.sudo().distri_tipo_vendedor = "externo"
        cls.armador = user("armador_v141", "distri_pedido.grupo_armador")
        cls.repartidor = user("repartidor_v141", "distri_pedido.grupo_reparto")
        Partner = cls.env["res.partner"]
        base = {"company_type": "company", "customer_rank": 1}
        cls.pelado = Partner.create(dict(
            base, name="Almacen Pelado V141", category_id=[Command.link(cls.tag.id)],
            user_id=cls.vendedor.id, street="Belgrano 141", city="Plottier", ref="C-0141",
            distri_whatsapp="+5492994140140", vat=CUIT_OK))
        cls.kiosco = Partner.create(dict(
            base, name="Kiosco Sin Datos V141", category_id=[Command.link(cls.tag.id)],
            city="Neuquén", partner_latitude=-38.95, partner_longitude=-68.06))
        cls.ajeno = Partner.create(dict(
            base, name="Kiosco Ajeno V141", category_id=[Command.link(cls.tag2.id)],
            user_id=cls.otro.id, street="Roca 1", city="Plottier"))
        cls.mayorista = Partner.create(dict(
            base, name="Mayorista Grande V141", category_id=[Command.link(cls.tag.id)],
            distri_es_mayorista=True, city="Neuquén"))
        cls.proveedor = Partner.create({"name": "Proveedor Sur V141", "company_type": "company",
                                        "supplier_rank": 1})
        cls.archivado = Partner.create(dict(base, name="Viejo Archivado V141", active=False))
        cls.nuevo = Partner.create(dict(
            base, name="Despensa Nueva V141", category_id=[Command.link(cls.tag.id)],
            user_id=cls.vendedor.id, distri_cliente_provisorio=True,
            distri_provisorio_user_id=cls.vendedor.id,
            distri_provisorio_fecha=fields.Datetime.now(),
            distri_provisorio_nota="Creado en «Nueva venta»"))
        # an external seller's customer: one he created himself
        cls.de_externo = Partner.with_user(cls.externo).sudo().create(dict(
            base, name="Cliente Del Externo V141"))
        # a posted invoice: the balance of the list and «Con deuda»
        cls.invoice = cls.init_invoice("out_invoice", partner=cls.pelado, amounts=[1410],
                                       post=True)

    # ------------------------------------------------------------ helpers

    def _flag(self, on=True):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _login(self, user):
        self.authenticate(user.login, user.login)

    def _ledger(self):
        self.env.flush_all()
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def _buscar(self, **params):
        res = self.url_open("/contactos/buscar?" + "&".join(
            "%s=%s" % (k, v) for k, v in params.items()))
        self.assertEqual(res.status_code, 200, res.text[:300])
        return res.json()

    def _names(self, **params):
        return [f["nombre"] for f in self._buscar(**params)["filas"]]

    def _csrf(self, url):
        page = self.url_open(url)
        self.assertEqual(page.status_code, 200, page.text[:300])
        return CSRF_RE.search(page.text).group(1), page.text

    def _post(self, url, form_url=None, **data):
        csrf, _html = self._csrf(form_url or url)
        data["csrf_token"] = csrf
        res = self.url_open(url, data=data, allow_redirects=False)
        self.env.invalidate_all()
        return res


@tagged("post_install", "-at_install", "distri_v129_41")
class TestV12941Contactos(V12941Mixin, AccountTestInvoicingHttpCommon):

    # ------------------------------------------------------------ flag

    def test_flag_off_answers_404_and_keeps_the_old_links(self):
        self._flag(False)
        before = self._ledger()
        self._login(self.admin)
        for url in ("/contactos", "/contactos/buscar", "/contactos/nuevo",
                    "/clientes/%s/editar" % self.pelado.id, "/clientes/%s/unir" % self.nuevo.id):
            self.assertEqual(self.url_open(url).status_code, 404, url)
        env = self.env(user=self.admin)
        self.assertNotIn("contactos", [t["key"] for t in staff_tiles(env)])
        row = self.nuevo.with_user(self.admin)._distri_provisorio_row()
        self.assertTrue(row["url"].startswith("/odoo/action-distri_pedido.action_clientes_provisorios/"))
        self.assertEqual(_partner_url(env, self.pelado, self.admin),
                         "/odoo/res.partner/%s" % self.pelado.id)
        # on: the kit's screens
        self._flag(True)
        self.assertIn("contactos", [t["key"] for t in staff_tiles(env)])
        tile = [t for t in staff_tiles(env) if t["key"] == "contactos"][0]
        self.assertEqual(tile["url"], "/contactos")
        row = self.nuevo.with_user(self.admin)._distri_provisorio_row()
        self.assertEqual(row["url"], "/clientes/%s?desde=/caja" % self.nuevo.id)
        self.assertEqual(_partner_url(env, self.pelado, self.admin), "/clientes/%s" % self.pelado.id)
        self.assertIn("/caja", screen_parents("/clientes/%s" % self.nuevo.id))
        nav = role_nav(env, "contactos")
        self.assertEqual([i["key"] for i in nav if i["on"]], ["contactos"], "shown, marked")
        self.assertEqual(self.url_open("/contactos").status_code, 200)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ search

    def test_search_by_name_phone_cuit_address_and_code(self):
        self._flag()
        self._login(self.admin)
        self.assertEqual(self._names(q="pelado"), ["Almacen Pelado V141"])
        for typed in ("2994140140", "299%20414%200140", "0299%2015%204140140", "4140140"):
            self.assertIn("Almacen Pelado V141", self._names(q=typed), typed)
        self.assertEqual(self._names(q="20-12345678-6"), ["Almacen Pelado V141"])
        self.assertEqual(self._names(q="belgrano%20141"), ["Almacen Pelado V141"])
        self.assertEqual(self._names(q="C-0141"), ["Almacen Pelado V141"])
        row = self._buscar(q="pelado")["filas"][0]
        self.assertEqual(row["localidad"], "Plottier")
        self.assertEqual(row["whatsapp"], "+5492994140140")
        self.assertEqual(row["vendedor"], self.vendedor.name)
        self.assertEqual(row["url"], "/clientes/%s?desde=/contactos" % self.pelado.id)
        self.assertEqual(row["saldo"], "$ 1.410")
        self.assertTrue(row["deuda"])
        self.assertIn("/contactos", screen_parents("/clientes/%s" % self.pelado.id))
        # the page itself, with the first page drawn by the server
        html = self.url_open("/contactos?q=pelado").text
        self.assertIn('data-ct-id="%s"' % self.pelado.id, html)
        self.assertIn("contactos.js", html)
        self.assertIn('data-parent="/tablero"', html)

    def test_filters(self):
        self._flag()
        self._login(self.admin)
        names = self._names(q="V141")
        self.assertNotIn("Proveedor Sur V141", names, "customers by default")
        self.assertNotIn("Viejo Archivado V141", names)
        self.assertEqual(self._names(q="V141", deuda=1), ["Almacen Pelado V141"])
        self.assertEqual(self._names(q="V141", confirmar=1), ["Despensa Nueva V141"])
        self.assertNotIn("Almacen Pelado V141", self._names(q="V141", sin_wa=1))
        self.assertIn("Kiosco Ajeno V141", self._names(q="V141", sin_wa=1))
        sin_gps = self._names(q="V141", sin_gps=1)
        self.assertNotIn("Kiosco Sin Datos V141", sin_gps)
        self.assertIn("Almacen Pelado V141", sin_gps)
        self.assertEqual(sorted(self._names(q="V141", localidad="plottier")),
                         ["Almacen Pelado V141", "Kiosco Ajeno V141"])
        self.assertEqual(self._names(q="V141", mayorista=1), ["Mayorista Grande V141"])
        self.assertEqual(self._names(q="V141", archivados=1), ["Viejo Archivado V141"])
        self.assertEqual(self._names(q="V141", tipo="proveedores"), ["Proveedor Sur V141"])
        self.assertNotIn("Kiosco Ajeno V141", self._names(q="V141", vendedor=self.vendedor.id))
        self.assertIn("Almacen Pelado V141", self._names(q="V141", vendedor=self.vendedor.id))
        # a seller: his portfolio only, never wholesale, suppliers or archived
        self._login(self.vendedor)
        names = self._names(q="V141")
        self.assertIn("Almacen Pelado V141", names)
        self.assertNotIn("Kiosco Ajeno V141", names, "another portfolio (record rule)")
        self.assertNotIn("Mayorista Grande V141", names, "wholesale: administración")
        self.assertNotIn("Proveedor Sur V141", self._names(q="V141", tipo="proveedores"))
        self.assertNotIn("Viejo Archivado V141", self._names(q="V141", archivados=1))
        self.assertEqual(self._names(q="V141", mios=1, confirmar=1), ["Despensa Nueva V141"])
        # an external seller: only his customers
        self._login(self.externo)
        self.assertEqual(self._names(q="V141"), ["Cliente Del Externo V141"])

    def test_pagination_and_balances_of_the_page_only(self):
        self._flag()
        Partner = self.env["res.partner"]
        Partner.create([{"name": "Lote V141 %02d" % i, "customer_rank": 1,
                         "company_type": "company"} for i in range(70)])
        self._login(self.admin)
        calls = []
        cls = type(Partner)
        original = cls._distri_account_balances

        def spy(self_, ids):
            calls.append(len(list(ids)))
            return original(self_, ids)

        with patch.object(cls, "_distri_account_balances", spy):
            first = self._buscar(q="lote%20v141")
            self.assertEqual((len(first["filas"]), first["total"], first["mas"]),
                             (ct.PAGE_SIZE, 70, True))
            self.assertLessEqual(max(calls), ct.PAGE_SIZE, "balances of the page only")
            last = self._buscar(q="lote%20v141", page=2)
            self.assertEqual((len(last["filas"]), last["mas"]), (10, False))
            ids = [f["id"] for f in first["filas"]] + [f["id"] for f in self._buscar(
                q="lote%20v141", page=1)["filas"]] + [f["id"] for f in last["filas"]]
            self.assertEqual(len(set(ids)), 70, "no row twice, none missing")
        html = self.url_open("/contactos?q=lote+v141").text
        self.assertIn("Ver más", html)
        self.assertIn("page=1", html)
        self.assertEqual(html.count("data-ct-id="), ct.PAGE_SIZE)

    def test_batched_balances_are_the_statement_ones(self):
        before = self._ledger()
        Mov = self.env["distri.cuenta.movimiento"]
        # imported history: one document also live in Odoo (counted once),
        # one only in the history
        Mov.create({"partner_id": self.pelado.id, "fecha": fields.Date.today(),
                    "comprobante": self.invoice.name, "debe": self.invoice.amount_total})
        Mov.create({"partner_id": self.pelado.id, "fecha": fields.Date.today(),
                    "comprobante": "FAC-OLD-1", "debe": 300.0})
        Mov.create({"partner_id": self.kiosco.id, "fecha": fields.Date.today(),
                    "comprobante": "REC-OLD-2", "haber": 125.5})
        partners = self.pelado | self.kiosco | self.ajeno | self.nuevo
        batch = self.env["res.partner"]._distri_account_balances(partners.ids)
        for p in partners:
            self.assertAlmostEqual(batch[p.id], p._distri_account_balance(), places=2, msg=p.name)
        self.assertAlmostEqual(batch[self.pelado.id], 1710.0, places=2)
        self.assertEqual(self.env["res.partner"]._distri_account_balances([]), {})
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ edit

    def test_administracion_edits_everything_as_herself(self):
        self._flag()
        before = self._ledger()
        lista = self.env["product.pricelist"].create({"name": "Lista V141"})
        self._login(self.admin)
        page = self.url_open("/clientes/%s/editar" % self.kiosco.id).text
        for name in ("nombre", "codigo", "cuit", "telefono", "email", "wa_area", "calle", "ciudad",
                     "lat", "lng", "dias", "horarios", "nota_reparto", "lista", "vendedor",
                     "cartera", "notas", "entrega_calle"):
            self.assertIn('name="%s"' % name, page, name)
        res = self._post("/clientes/%s/editar" % self.kiosco.id, nombre="Kiosco Con Datos V141",
                         codigo="C-0999", cuit="20-12345678-6", telefono="0299 15 422-2444",
                         email="kiosco@example.com", wa_area="0299", wa_numero="15 4222444",
                         calle="Mitre 99", ciudad="Neuquén", lat="-38,951234", lng="-68.059999",
                         dias=["0", "2", "4"], horarios="8:00-12:30 17:00-20:30",
                         nota_reparto="Timbre roto", lista=str(lista.id),
                         vendedor=str(self.vendedor.id), cartera=str(self.tag.id),
                         notas="Paga los viernes", entrega_calle="Depósito 5",
                         entrega_ciudad="Centenario")
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertEqual(res.headers["Location"].split("?")[1], "hecho=guardado")
        k = self.kiosco
        self.assertEqual(k.name, "Kiosco Con Datos V141")
        self.assertEqual((k.ref, k.vat, k.email), ("C-0999", CUIT_OK, "kiosco@example.com"))
        self.assertEqual(k.distri_whatsapp, "+5492994222444")
        self.assertTrue(k.phone)
        self.assertEqual((k.street, k.city), ("Mitre 99", "Neuquén"))
        self.assertAlmostEqual(k.partner_latitude, -38.951234, places=6)
        self.assertEqual(k.distri_location_user_id, self.admin)
        self.assertEqual(k.distri_delivery_days, "0,2,4")
        self.assertEqual(k.distri_delivery_hours, "08:00-12:30;17:00-20:30")
        self.assertEqual(k.distri_driver_note, "Timbre roto")
        self.assertEqual(k.property_product_pricelist, lista)
        self.assertEqual(k.user_id, self.vendedor)
        self.assertIn(self.tag, k.category_id)
        self.assertIn("Paga los viernes", k.comment)
        entrega = k.child_ids.filtered(lambda c: c.type == "delivery")
        self.assertEqual((entrega.street, entrega.city), ("Depósito 5", "Centenario"))
        note = k.message_ids.filtered(lambda m: "en «Contactos»" in (m.body or ""))
        self.assertTrue(note)
        # the portfolio is changed, not added: the other tag goes away
        res = self._post("/clientes/%s/editar" % self.kiosco.id, nombre=k.name,
                         cartera=str(self.tag2.id), lista=str(lista.id))
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertEqual(self.kiosco.category_id, self.tag2)
        # gerencia (implies administración) too
        self._login(self.gerente)
        self.assertEqual(self.url_open("/clientes/%s/editar" % self.ajeno.id).status_code, 200)
        self.assertEqual(self._ledger(), before)

    def test_administracion_form_is_validated_like_the_backend(self):
        self._flag()
        self._login(self.admin)
        url = "/clientes/%s/editar" % self.pelado.id
        for data, text in (
                ({"nombre": "ab"}, "nombre"),
                ({"nombre": "Pelado", "cuit": "20-12345678-0"}, "CUIT"),
                ({"nombre": "Pelado", "cuit": "123"}, "CUIT"),
                ({"nombre": "Pelado", "telefono": "12"}, "Teléfono"),
                ({"nombre": "Pelado", "wa_area": "299", "wa_numero": "1"}, "WhatsApp"),
                ({"nombre": "Pelado", "horarios": "tarde"}, "Horarios"),
                ({"nombre": "Pelado", "lat": "-38.9"}, "Ubicación"),
                ({"nombre": "Pelado", "email": "sin-arroba"}, "email"),
                ({"nombre": "Pelado", "lista": "999999"}, "Lista de precios")):
            res = self._post(url, **data)
            self.assertEqual(res.status_code, 400, data)
            self.assertIn(text, res.text, data)
        self.assertEqual(self.pelado.name, "Almacen Pelado V141", "nothing written")
        self.assertEqual(self.pelado.vat, CUIT_OK)
        # an old value left untouched is not re-checked: the rest still saves
        self.pelado.write({"vat": "20-1", "phone": "tel viejo",
                           "distri_whatsapp": False})
        self.env.flush_all()
        self.env.cr.execute("UPDATE res_partner SET distri_whatsapp = 'viejo 12' WHERE id = %s",
                            [self.pelado.id])
        self.env.invalidate_all()
        res = self._post(url, nombre="Almacen Pelado V141", cuit="20-1", telefono="tel viejo",
                         calle="Belgrano 142")
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertEqual((self.pelado.vat, self.pelado.phone, self.pelado.street),
                         ("20-1", "tel viejo", "Belgrano 142"))
        self.assertEqual(self.pelado.distri_whatsapp, "viejo 12", "not shown, so kept")

    def test_seller_edits_only_whatsapp_and_address_of_his_customers(self):
        self._flag()
        before = self._ledger()
        self._login(self.vendedor)
        page = self.url_open("/clientes/%s/editar" % self.pelado.id).text
        self.assertIn('name="wa_area"', page)
        self.assertNotIn('name="nombre"', page)
        self.assertNotIn('name="cuit"', page)
        res = self._post("/clientes/%s/editar" % self.pelado.id, wa_area="299",
                         wa_numero="4141141", calle="Alem 41", ciudad="Neuquén",
                         nombre="Hackeado", cuit="30712345671", lista="1")
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertEqual(self.pelado.distri_whatsapp, "+5492994141141")
        self.assertEqual((self.pelado.street, self.pelado.city), ("Alem 41", "Neuquén"))
        self.assertEqual(self.pelado.name, "Almacen Pelado V141", "only the three fields")
        self.assertEqual(self.pelado.vat, CUIT_OK)
        self.assertTrue(self.pelado.message_ids.filtered(
            lambda m: "en «Contactos»" in (m.body or "")))
        # the backend still gives him no write
        self.assertFalse(self.pelado.with_user(self.vendedor).has_access("write"))
        # refusals: outside his portfolio, wholesale, an external seller on
        # another's customer, packers and drivers, a post without the token
        self.assertEqual(self.url_open("/clientes/%s/editar" % self.ajeno.id).status_code, 404)
        self.assertEqual(self.url_open("/clientes/%s/editar" % self.mayorista.id).status_code, 403)
        csrf, _html = self._csrf("/clientes/%s/editar" % self.pelado.id)
        res = self.url_open("/clientes/%s/editar" % self.ajeno.id, allow_redirects=False,
                            data={"csrf_token": csrf, "calle": "Tomado 1"})
        self.assertEqual(res.status_code, 404)
        res = self.url_open("/clientes/%s/editar" % self.pelado.id, allow_redirects=False,
                            data={"calle": "Sin token 1"})
        self.assertEqual(res.status_code, 400, "CSRF")
        self._login(self.externo)
        self.assertEqual(self.url_open("/clientes/%s/editar" % self.pelado.id).status_code, 404)
        self.assertEqual(self.url_open("/clientes/%s/editar" % self.de_externo.id).status_code,
                         200)
        for user in (self.armador, self.repartidor):
            self._login(user)
            self.assertEqual(self.url_open("/contactos").status_code, 403, user.login)
            self.assertEqual(self.url_open("/clientes/%s/editar" % self.pelado.id).status_code,
                             403, user.login)
        self.env.invalidate_all()
        self.assertEqual(self.ajeno.street, "Roca 1")
        self.assertEqual(self.pelado.street, "Alem 41")
        self.assertEqual(self._ledger(), before)

    def test_customer_screen_offers_edit_and_confirm(self):
        self._flag()
        self._login(self.admin)
        html = self.url_open("/clientes/%s?hecho=guardado" % self.nuevo.id).text
        self.assertIn("data-fi-editar", html)
        self.assertIn("los datos quedaron guardados", html)
        self.assertIn("data-fi-provisorio", html)
        self.assertIn('action="/clientes/%s/confirmar"' % self.nuevo.id, html)
        self.assertIn('href="/clientes/%s/unir"' % self.nuevo.id, html)
        self.assertIn("data-fi-odoo", html, "«Ver en el sistema» stays for administración")
        self._login(self.vendedor)
        html = self.url_open("/clientes/%s" % self.nuevo.id).text
        self.assertIn("Editar WhatsApp y domicilio", html)
        self.assertIn("data-fi-provisorio", html)
        self.assertNotIn("/confirmar", html, "only administración confirms")
        self.assertNotIn("data-fi-odoo", html)
        html = self.url_open("/clientes/%s" % self.mayorista.id).text
        self.assertNotIn("data-fi-editar", html)

    # ------------------------------------------------------------ new

    def test_new_contact_is_the_nueva_venta_path(self):
        self._flag()
        before = self._ledger()
        self._login(self.vendedor)
        res = self._post("/contactos/nuevo", nombre="ab")
        self.assertEqual(res.status_code, 400)
        res = self._post("/contactos/nuevo", nombre="Almacen Pelado Dos")
        self.assertEqual(res.status_code, 200)
        self.assertIn("¿Es alguno de estos?", res.text)
        self.assertIn('href="/clientes/%s?desde=/contactos"' % self.pelado.id, res.text)
        self.assertFalse(self.env["res.partner"].search([("name", "=", "Almacen Pelado Dos")]))
        res = self._post("/contactos/nuevo", nombre="Almacen Pelado Dos", nuevo_ok="1",
                         wa_area="299", wa_numero="4222441", calle="Sarmiento 6",
                         ciudad="Plottier")
        self.assertEqual(res.status_code, 303, res.text[:300])
        nuevo = self.env["res.partner"].search([("name", "=", "Almacen Pelado Dos")])
        self.assertIn("/clientes/%s" % nuevo.id, res.headers["Location"])
        self.assertEqual(nuevo.distri_whatsapp, "+5492994222441")
        self.assertTrue(nuevo.distri_cliente_provisorio)
        self.assertEqual(nuevo.distri_provisorio_nota, "Creado en «Contactos»")
        self.assertEqual((nuevo.user_id, nuevo.create_uid), (self.vendedor, self.vendedor))
        self.assertIn(self.tag, nuevo.category_id)
        self.assertFalse(self.env["res.partner"].with_user(self.vendedor).has_access("create"))
        # administración: not «a confirmar»
        self._login(self.admin)
        res = self._post("/contactos/nuevo", nombre="Distribuidora Nueva V141", nuevo_ok="1")
        self.assertEqual(res.status_code, 303, res.text[:300])
        mio = self.env["res.partner"].search([("name", "=", "Distribuidora Nueva V141")])
        self.assertFalse(mio.distri_cliente_provisorio)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ a confirmar

    def test_confirm_and_merge(self):
        self._flag()
        before = self._ledger()
        # a seller cannot confirm
        self._login(self.vendedor)
        res = self._post("/clientes/%s/confirmar" % self.nuevo.id,
                         form_url="/clientes/%s" % self.nuevo.id)
        self.assertEqual(res.status_code, 403)
        self.assertEqual(self.url_open("/clientes/%s/unir" % self.nuevo.id).status_code, 403)
        self.assertTrue(self.nuevo.distri_cliente_provisorio)
        # merge: a new customer with only a quotation goes into the old one
        product = self.env["product.product"].create({"name": "Producto V141", "list_price": 10})
        order = self.env["sale.order"].create({
            "partner_id": self.nuevo.id,
            "order_line": [Command.create({"product_id": product.id, "product_uom_qty": 1})]})
        self._login(self.admin)
        page = self.url_open("/clientes/%s/unir?q=pelado" % self.nuevo.id).text
        self.assertIn("Unir con este", page)
        self.assertIn('value="%s"' % self.pelado.id, page)
        res = self._post("/clientes/%s/unir" % self.nuevo.id, target_id=str(self.pelado.id))
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertIn("/clientes/%s?hecho=unido" % self.pelado.id, res.headers["Location"])
        self.assertEqual(order.partner_id, self.pelado)
        self.assertFalse(self.nuevo.active)
        self.assertEqual(self.nuevo.distri_provisorio_unido_id, self.pelado)
        self.assertEqual(self._ledger(), before)
        # with accounting on it: never merged, only noted
        otro = self.env["res.partner"].create({
            "name": "Kiosco Con Factura V141", "customer_rank": 1,
            "distri_cliente_provisorio": True})
        invoice = self.init_invoice("out_invoice", partner=otro, amounts=[50], post=True)
        before = self._ledger()
        self.assertIn("movimientos de cuenta", otro._distri_merge_refusal())
        page = self.url_open("/clientes/%s/unir?q=pelado" % otro.id).text
        self.assertIn("data-ct-refusal", page)
        self.assertIn("Anotar que es este", page)
        res = self._post("/clientes/%s/unir" % otro.id, target_id=str(self.pelado.id))
        self.assertEqual(res.status_code, 400)
        self.assertTrue(otro.active and otro.distri_cliente_provisorio)
        res = self._post("/clientes/%s/unir" % otro.id, target_id=str(self.pelado.id),
                         anotar="1")
        self.assertEqual(res.status_code, 303)
        self.assertTrue(otro.active and otro.distri_cliente_provisorio)
        self.assertEqual(invoice.partner_id, otro, "the invoice was not moved")
        self.assertTrue(otro.message_ids.filtered(lambda m: "es el mismo cliente" in (m.body or "")))
        self.assertTrue(self.pelado.message_ids.filtered(lambda m: "es este mismo" in (m.body or "")))
        # «Confirmar»
        res = self._post("/clientes/%s/confirmar" % otro.id, form_url="/clientes/%s" % otro.id)
        self.assertEqual(res.status_code, 303)
        self.assertFalse(otro.distri_cliente_provisorio)
        self.assertEqual(self._ledger(), before)

    # ------------------------------------------------------------ the JS (node)

    def test_js_in_node(self):
        node = shutil.which("node") or shutil.which("nodejs")
        if not node:
            raise SkipTest("node is not installed")
        root = get_module_path("distri_pedido")
        res = subprocess.run([node, os.path.join(root, "tests/data/contactos_v129_41.js"), root],
                             capture_output=True, text=True, timeout=60, check=False)
        self.assertEqual(res.returncode, 0, res.stderr)
        out = json.loads(res.stdout)
        self.assertEqual(out["query"], "q=kiosco%20ana&localidad=Plottier&mios=1&sin_wa=1")
        self.assertEqual(out["queryPage"], "q=x&tipo=proveedores&page=2")
        self.assertEqual(out["fetchedWhileTyping"], 0, "nothing while typing")
        self.assertEqual(out["fetchedAfterPause"], ["/contactos/buscar?q=kios"])
        self.assertEqual(out["filterNow"], 1, "a filter searches at once")
        self.assertEqual(out["drawn"], ["nuevo"], "a late old answer is dropped")
        self.assertEqual(out["moreUrl"], "/contactos/buscar?q=kios&page=1")
        self.assertTrue(out["appended"])
        self.assertTrue(out["sameStateSkipped"])
        self.assertEqual(out["states"][-1], "q=kios")
        row = out["row"]
        self.assertEqual(row["href"], "/clientes/5?desde=/contactos")
        self.assertIn("<img src=x onerror=1>", row["text"], "data stays text")
        self.assertFalse(row["html"])
        self.assertEqual(out["total"], ["1 contacto", "3 contactos"])
