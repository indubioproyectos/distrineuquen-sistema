"""19.0.1.127.0: a package of small improvements.

- The packer on the label («Armó: Juan»), in the driver's stop (packed_by,
  packed_at, missing, also the fully missing lines) and on the backend
  «Armado» tab (/armado's sentence).
- «Buscar cliente o pedido» on /armado, outside the refreshed list.
- Receipt photos with zoom and «Foto en pestaña nueva»; the amount is not
  typed in the editable list.
- Home text and meta description without brands nor «farmacia».
- The «Reparto» tile is «Rendiciones y casos»; «Planificación (PC)» is gone.
- Self-dispatch: administración and gerencia control their own route's load
  (noted on the route), a plain driver never.
- Customer suggestion by «0001/2026/07963» and a bare 5-digit tail, lab
  invoices and Online history, never «S00241».
Synthetic data only.
"""
import re
from datetime import datetime

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.controllers import tablero
from odoo.addons.distri_pedido.models.paquete_v127 import invoice_suggestions
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin

HOME_SUBTITLE = ("Cigarrillos, tabaco, golosinas, galletitas, gaseosas, vinos, y mucho más. "
                 "Hacé tu pedido desde el celular, a cualquier hora, y te lo llevamos el día "
                 "de reparto de tu zona.")
HOME_DESCRIPTION = ("Distribuidora mayorista en Neuquén. Cigarrillos, tabaco, golosinas, "
                    "galletitas, gaseosas, vinos y más para kioscos y almacenes de Neuquén y la "
                    "región. Hacé tu pedido online.")


def _user(env, login, name, groups):
    return new_test_user(env, login=login, name=name, groups="base.group_user," + groups,
                         company_id=env.company.id, company_ids=[Command.set(env.company.ids)])


@tagged("post_install", "-at_install", "distri_v127")
class TestPackedBy(RepartoMixin, AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls._setup_reparto()
        cls.packer = _user(cls.env, "juan_armador_v127", "Juan Pérez V127",
                           "distri_pedido.grupo_armador")
        cls.customer = cls.env["res.partner"].create({"name": "KIOSCO ARMADO V127",
                                                      "company_type": "company"})
        cls.coca = cls.env["product.product"].create({
            "name": "Coca 2,25 V127", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})

    def _packed(self, packer=True):
        order = self._order(self.customer, self.product_a, qty=10)
        order.sudo().write({
            "distri_bultos": 3,
            "distri_armador_id": self.packer.id if packer else False,
            # 13:42 UTC = 10:42 in Argentina
            "distri_armado_fin": datetime(2026, 11, 20, 13, 42),
        })
        return order

    def test_label_prints_the_packer_first_name(self):
        order = self._packed()
        package = order._distri_packages()[:1]
        self.assertEqual(package._label_values()["packer"], "Juan")
        html, _kind = self.env["ir.actions.report"]._render_qweb_html(
            "distri_pedido.report_etiquetas_bultos", package.ids)
        text = " ".join(re.sub(r"<[^>]+>", " ", html.decode()).split())
        self.assertIn("Armó: Juan", text)
        nobody = self._packed(packer=False)._distri_packages()[:1]
        self.assertEqual(nobody._label_values()["packer"], "")
        html, _kind = self.env["ir.actions.report"]._render_qweb_html(
            "distri_pedido.report_etiquetas_bultos", nobody.ids)
        self.assertNotIn('class="ped armo"', html.decode())

    def test_driver_payload_packer_and_missing(self):
        order = self._packed()
        order.sudo().write({"order_line": [Command.create({
            "product_id": self.coca.id, "product_uom_qty": 2, "price_unit": 100,
            "tax_ids": [Command.clear()]})]})
        short, gone = order.order_line.filtered(lambda l: not l.display_type)
        short.sudo().write({"product_uom_qty": 8, "distri_faltante": 2})
        # fully missing: the quantity is 0, the payload's «lines» leaves it out
        gone.sudo().write({"product_uom_qty": 0, "distri_faltante": 2})
        route = self._route(order)
        payload = route.stop_ids._app_payload()
        self.assertEqual(payload["packed_by"], "Juan")
        self.assertEqual(payload["packed_at"], "10:42")
        self.assertEqual(sorted((m["product"], m["qty"]) for m in payload["missing"]),
                         sorted([(short.product_id.display_name, 2.0), ("Coca 2,25 V127", 2.0)]))
        self.assertNotIn(gone.id, [line["id"] for line in payload["lines"]])

    def test_backend_banner_sentence(self):
        order = self._packed()
        self.assertEqual(order.distri_armado_resumen,
                         "Armado por Juan Pérez V127 a las 10:42, en 3 bultos.")
        order.sudo().distri_bultos = 1
        self.assertTrue(order.distri_armado_resumen.endswith("en 1 bulto."))
        order.sudo().distri_armado = "en_armado"
        self.assertFalse(order.distri_armado_resumen)
        arch = self.env.ref("distri_pedido.view_order_form_armado").arch_db
        self.assertIn('name="distri_armado_resumen"', arch)


@tagged("post_install", "-at_install", "distri_v127")
class TestViewsAndPages(HttpCase):

    def test_armado_has_the_search_outside_the_refreshed_list(self):
        packer = _user(self.env, "armador_busca_v127", "Armador Busca V127",
                       "distri_pedido.grupo_armador")
        self.authenticate(packer.login, packer.login)
        res = self.url_open("/armado")
        self.assertEqual(res.status_code, 200)
        html = res.text
        search = re.search(r'<input[^>]*type="search"[^>]*>', html)
        self.assertTrue(search, "the search input is on the page")
        self.assertIn('placeholder="Buscar cliente o pedido"', search.group(0))
        self.assertLess(html.index(search.group(0)), html.index('id="lista"'),
                        "above the list and outside it: the refresh keeps the query")
        with open(self._static("armado/armado.js")) as f:
            js = f.read()
        self.assertIn("data-arm-buscar", js)
        self.assertIn("box.innerHTML = fresh.innerHTML; filtrar();", js)

    def _static(self, path):
        from odoo.tools.misc import file_path
        return file_path("distri_pedido/static/src/" + path)

    def test_receipt_photo_zoom_and_new_tab(self):
        for xmlid in ("distri_pedido.view_receipt_form", "distri_pedido.view_whatsapp_message_form"):
            arch = self.env.ref(xmlid).arch_db
            self.assertIn("'size': [0, 900], 'zoom': true", arch, xmlid)
            self.assertIn('name="action_tab_receipt"', arch, xmlid)
            self.assertIn("Foto en pestaña nueva", arch, xmlid)
        listing = self.env.ref("distri_pedido.view_receipt_list").arch_db
        self.assertIn('<field name="distri_amount" readonly="1"/>', listing)
        msg = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.v127.tab", "wa_id": "5492990000127", "direction": "in",
            "message_type": "image"})
        with self.assertRaisesRegex(UserError, "no tiene foto"):
            msg.action_tab_receipt()
        attachment = self.env["ir.attachment"].sudo().create({
            "name": "c.png", "raw": b"\x89PNG v127", "res_model": msg._name, "res_id": msg.id})
        msg.attachment_id = attachment
        self.assertEqual(msg.action_tab_receipt(),
                         {"type": "ir.actions.act_url", "url": "/web/content/%s" % attachment.id,
                          "target": "new"})

    def test_home_text_and_meta_description(self):
        res = self.url_open("/")
        self.assertEqual(res.status_code, 200)
        html = res.text
        subtitle = re.search(r'<p class="portada__bajada">(.*?)</p>', html, re.S).group(1)
        self.assertEqual(" ".join(subtitle.split()), HOME_SUBTITLE)
        self.assertIn('<meta name="description" content="%s"' % HOME_DESCRIPTION, html)
        self.assertIn('"description": "%s"' % HOME_DESCRIPTION, html)
        # untouched: the «Quiénes somos» text and the cigarette brands
        self.assertIn("farmacia y perfumería", html)
        self.assertIn("Red Point, Master, Kiel, West", html)

    def test_rendiciones_tile_and_no_planificacion_menu(self):
        admin = _user(self.env, "admin_tile_v127", "Admin Tile V127",
                      "distri_pedido.grupo_administracion")
        tiles = tablero.staff_tiles(self.env(user=admin))
        [tile] = [t for t in tiles if t["key"] == "reparto"]
        settle = self.env.ref("distri_pedido.menu_reparto_settle")
        self.assertEqual((tile["title"], tile["subtitle"]),
                         ("Rendiciones y casos", "Rendir rutas, casos, cargos y ajustes"))
        self.assertEqual(tile["url"], "/odoo?menu_id=%s" % settle.id)
        root = self.env.ref("distri_pedido.menu_reparto_root")
        apps = tablero.other_apps(self.env(user=admin), tablero.tile_roots(self.env, tiles))
        self.assertNotIn(root.name, [a["title"] for a in apps], "no second «Reparto» app")
        self.assertFalse(self.env.ref("distri_pedido.menu_reparto_planificacion",
                                      raise_if_not_found=False))
        page = self.env.ref("distri_pedido.rutas_app").arch_db
        self.assertIn("/odoo/action-distri_pedido.action_route_settle", page)
        self.assertIn(">Rendiciones<", page)


@tagged("post_install", "-at_install", "distri_v127")
class TestSelfDispatch(RepartoMixin, HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_reparto()
        cls.denis = _user(cls.env, "denis_v127", "Denis V127", "distri_pedido.grupo_administracion")
        cls.gonzalo = _user(cls.env, "gonzalo_v127", "Gonzalo V127", "distri_pedido.grupo_gerencia")
        cls.product = cls.env["product.product"].create({
            "name": "Producto autodespacho V127", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        cls.customer = cls.env["res.partner"].create({
            "name": "KIOSCO SUELTO V127", "street": "Calle 127", "city": "Fuera de zona V127"})

    def _notes(self, route):
        return route.message_ids.filtered(lambda m: "Autodespacho:" in (m.body or ""))

    def test_administracion_controls_his_own_load_noted_once(self):
        route = self._route(self._order(self.customer, self.product, qty=2)
                            | self._order(self.customer, self.product, qty=1), driver=self.denis)
        first, second = route.stop_ids
        first.with_user(self.denis).sudo(False).action_dispatch_check()
        second.with_user(self.denis).sudo(False).action_dispatch_check()
        self.assertTrue(all(route.stop_ids.mapped("dispatch_checked")))
        self.assertEqual(route.stop_ids.dispatch_checked_by_id, self.denis)
        notes = self._notes(route)
        self.assertEqual(len(notes), 1, "one note per route, not per tick")
        self.assertIn("Autodespacho: Denis V127 controló su propia carga", notes.body)
        self.assertTrue(route.with_user(self.denis)._despacho_payload()["editable"])

    def test_gerencia_marks_his_own_route_controlled(self):
        route = self._route(self._order(self.customer, self.product, qty=2), driver=self.gonzalo)
        self._sync([self._event(route, "load", {"stop_id": s.id, "loaded": True})
                    for s in route.stop_ids], user=self.gonzalo)
        route.stop_ids.with_user(self.gonzalo).sudo(False).action_dispatch_check()
        route.with_user(self.gonzalo).sudo(False).action_mark_controlled()
        self.assertEqual(route.departure_controlled_by_id, self.gonzalo)
        self.assertEqual(len(self._notes(route)), 1)
        self.assertIn("Gonzalo V127", self._notes(route).body)

    def test_plain_driver_keeps_the_double_control(self):
        route = self._route(self._order(self.customer, self.product, qty=2))
        stop = route.stop_ids
        with self.assertRaises(UserError):
            stop.with_user(self.driver).sudo(False).action_dispatch_check()
        # even through a sudo path the route's own driver is refused
        with self.assertRaisesRegex(UserError, "otra persona"):
            stop.with_user(self.driver).sudo().action_dispatch_check()
        self.assertFalse(stop.dispatch_checked)
        self.assertFalse(self._notes(route))
        payload = route.with_user(self.driver).sudo()._despacho_payload()
        self.assertFalse(payload["editable"])
        self.assertFalse(payload["self_dispatch"])


@tagged("post_install", "-at_install", "distri_v127")
class TestInvoiceNumberSuggestion(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.partner = cls.env["res.partner"].create({"name": "ALMACEN FACTURA V127",
                                                     "company_type": "company"})
        cls.other = cls.env["res.partner"].create({"name": "KIOSCO FACTURA V127",
                                                   "company_type": "company"})
        cls.History = cls.env["distri.cuenta.movimiento"].sudo()
        cls.Message = cls.env["distri.whatsapp.message"]

    def _free_tail(self):
        """A 5-digit tail no lab invoice nor Online row ends in (the copy has
        real numbers)."""
        for n in range(99999, 90000, -1):
            tail = "%05d" % n
            pattern = "%%/%s" % tail
            if not self.History.search_count([("comprobante", "=like", pattern)]) and \
                    not self.env["account.move"].sudo().search_count([("name", "=like", pattern)]):
                return tail
        self.fail("no free tail")

    def _lab_invoice(self, partner, number):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": partner.id, "invoice_date": "2026-11-20",
            "invoice_line_ids": [Command.create({"name": "mercadería", "quantity": 1,
                                                 "price_unit": 1000,
                                                 "tax_ids": [Command.clear()]})]})
        invoice.action_post()
        # our numbering «0001/2026/NNNNN» (the test company's journal has another code)
        self.env.cr.execute("UPDATE account_move SET name = %s WHERE id = %s", (number, invoice.id))
        invoice.invalidate_recordset(["name"])
        return invoice

    def _reasons(self, text):
        return [(s["id"], s["reason"])
                for s in self.Message._distri_partner_suggestions(text=text)
                if s["reason"].startswith("por factura")]

    def test_full_number_lab_invoice_and_online_row(self):
        self._lab_invoice(self.partner, "0001/2026/07963")
        self.assertIn((self.partner.id, "por factura 0001/2026/07963"),
                      self._reasons("Pago factura 0001/2026/07963 gracias"))
        self.History.create({"partner_id": self.other.id, "fecha": "2025-06-01",
                             "comprobante": "0001/2025/07963", "tipo": "factura",
                             "debe": 5000.0})
        self.assertIn((self.other.id, "por factura 0001/2025/07963"),
                      self._reasons("fact 0001/2025/07963"))

    def test_bare_tail_names_one_customer(self):
        tail = self._free_tail()
        self.History.create({"partner_id": self.partner.id, "fecha": "2026-10-01",
                             "comprobante": "0001/2026/%s" % tail, "tipo": "factura",
                             "debe": 5000.0})
        self.assertEqual(self._reasons("Transferencia fact %s" % tail),
                         [(self.partner.id, "por factura 0001/2026/%s" % tail)])
        # a payment row with that tail is not an invoice
        self.History.create({"partner_id": self.other.id, "fecha": "2026-10-02",
                             "comprobante": "PBNK1/2026/%s" % tail, "tipo": "pago",
                             "haber": 5000.0})
        self.assertEqual(len(self._reasons("fact %s" % tail)), 1)
        # the same tail on another customer's lab invoice: ambiguous, nothing
        self._lab_invoice(self.other, "0001/2025/%s" % tail)
        self.assertEqual(self._reasons("fact %s" % tail), [])

    def test_order_number_and_amounts_are_not_tails(self):
        tail = self._free_tail()
        self.History.create({"partner_id": self.partner.id, "fecha": "2026-10-01",
                             "comprobante": "0001/2026/%s" % tail, "tipo": "factura",
                             "debe": 5000.0})
        for text in ("S%s" % tail, "$ %s" % tail, "%s,50" % tail, "AB%s" % tail):
            self.assertEqual(invoice_suggestions(self.env, text), [], text)
        self.assertEqual(self._reasons("Pedido S00241"), [])
