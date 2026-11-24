"""19.0.1.129.18 (pendiente 78): «Mis ventas», each seller's orders and how
they go (models/mis_ventas.py, controllers/mis_ventas.py). Read only; a
seller gets only his own orders whatever the request asks, Gerencia gets
everyone's and can pick a seller; portal and public users get nothing."""
import re
from datetime import datetime, timedelta

from odoo import Command
from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers import tablero
from odoo.addons.distri_pedido.models import mis_ventas

# Wednesday 25/11/2026 12:00 in Argentina
NOW = datetime(2026, 11, 25, 15, 0)


class MisVentasDatos:

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, **company)

        cls.vendedor = user("vendedor_mv18", "distri_pedido.grupo_vendedor")
        cls.otro = user("otro_vendedor_mv18", "distri_pedido.grupo_vendedor")
        cls.gerente = user("gerente_mv18", "distri_pedido.grupo_gerencia")
        cls.armador = user("armador_mv18", "distri_pedido.grupo_armador")
        cls.portal = new_test_user(cls.env, login="portal_mv18", groups="base.group_portal")
        cls.product = cls.env["product.product"].create({
            "name": "Yerba Mis Ventas V18", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        Partner = cls.env["res.partner"]
        cls.almacen = Partner.create({"name": "Almacén Las Lilas MV18", "customer_rank": 1})
        cls.kiosco = Partner.create({"name": "Kiosco El Faro MV18", "customer_rank": 1})
        cls.hoy = cls._order(cls.vendedor, cls.almacen, NOW - timedelta(hours=1), 1000.0)
        cls.lunes = cls._order(cls.vendedor, cls.kiosco, datetime(2026, 11, 23, 13, 0), 500.0)
        cls.del_mes = cls._order(cls.vendedor, cls.almacen, datetime(2026, 11, 3, 13, 0), 300.0)
        cls.viejo = cls._order(cls.vendedor, cls.almacen, datetime(2026, 10, 20, 13, 0), 200.0)
        cls.cancelado = cls._order(cls.vendedor, cls.kiosco, NOW - timedelta(hours=2), 700.0)
        cls.cancelado.sudo().write({"state": "cancel"})
        cls.ajeno = cls._order(cls.otro, cls.kiosco, NOW - timedelta(hours=3), 900.0)

    @classmethod
    def _order(cls, seller, partner, when, price):
        order = cls.env["sale.order"].sudo().create({
            "partner_id": partner.id,
            "user_id": seller.id,
            "order_line": [Command.create({"product_id": cls.product.id, "product_uom_qty": 2,
                                           "price_unit": price / 2, "tax_ids": [Command.clear()]})],
        })
        order.write({"date_order": when})
        return order

    def buscar(self, user, **kw):
        kw.setdefault("now", NOW)
        return mis_ventas.buscar(self.env(user=user), **kw)

    @staticmethod
    def names(data):
        return {r["name"] for r in data["rows"]}


@tagged("post_install", "-at_install", "distri_v129_18")
class TestMisVentas(MisVentasDatos, TransactionCase):

    def test_seller_sees_only_his_orders(self):
        data = self.buscar(self.vendedor, periodo="mes")
        self.assertEqual(self.names(data),
                         {self.hoy.name, self.lunes.name, self.del_mes.name, self.cancelado.name})
        self.assertFalse(data["gerencia"])
        # asking for another seller changes nothing
        forced = self.buscar(self.vendedor, periodo="mes", vendedor=self.otro.id)
        self.assertEqual(self.names(forced), self.names(data))
        self.assertNotIn(self.ajeno.name, self.names(forced))
        other = self.buscar(self.otro, periodo="mes")
        self.assertEqual(self.names(other), {self.ajeno.name})

    def test_gerencia_sees_everyone_and_filters_by_seller(self):
        data = self.buscar(self.gerente, periodo="mes")
        self.assertTrue(data["gerencia"])
        self.assertLessEqual({self.hoy.name, self.ajeno.name}, self.names(data))
        solo = self.buscar(self.gerente, periodo="mes", vendedor=str(self.otro.id))
        self.assertEqual(self.names(solo), {self.ajeno.name})
        self.assertIn(self.vendedor, mis_ventas.vendedores(self.env))
        self.assertNotIn(self.armador, mis_ventas.vendedores(self.env))

    def test_the_customer_choice_wins_over_the_salesperson(self):
        """«Mi vendedor es»: an order the customer gave to a seller counts
        for that seller even when the salesperson is somebody else, and only
        for him."""
        web = self.env["distri.vendedor.web"].sudo().create(
            {"name": "Otro MV18", "user_id": self.otro.id})
        elegido = self._order(self.vendedor, self.almacen, NOW - timedelta(hours=4), 400.0)
        elegido.sudo().write({"distri_vendedor_web_id": web.id})
        self.assertIn(elegido.name, self.names(self.buscar(self.otro, periodo="mes")))
        self.assertNotIn(elegido.name, self.names(self.buscar(self.vendedor, periodo="mes")))
        solo = self.buscar(self.gerente, periodo="mes", vendedor=self.otro.id)
        self.assertEqual(self.names(solo), {self.ajeno.name, elegido.name})
        row = next(r for r in solo["rows"] if r["name"] == elegido.name)
        self.assertEqual(row["vendedor"], self.otro.name)
        # a choice with no user linked leaves the salesperson
        sin_usuario = self.env["distri.vendedor.web"].sudo().create({"name": "Mario MV18"})
        elegido.sudo().write({"distri_vendedor_web_id": sin_usuario.id})
        self.assertIn(elegido.name, self.names(self.buscar(self.vendedor, periodo="mes")))

    def test_periods(self):
        self.assertEqual(self.names(self.buscar(self.vendedor, periodo="hoy")),
                         {self.hoy.name, self.cancelado.name})
        self.assertEqual(self.names(self.buscar(self.vendedor, periodo="semana")),
                         {self.hoy.name, self.cancelado.name, self.lunes.name})
        self.assertNotIn(self.viejo.name, self.names(self.buscar(self.vendedor, periodo="mes")))
        # an unknown period is the default one
        self.assertEqual(self.buscar(self.vendedor, periodo="siempre")["periodo"],
                         mis_ventas.DEFAULT_PERIODO)

    def test_search_by_customer_or_number(self):
        by_customer = self.buscar(self.vendedor, periodo="mes", q="las lilas")
        self.assertEqual(self.names(by_customer), {self.hoy.name, self.del_mes.name})
        number = re.sub(r"\D", "", self.lunes.name).lstrip("0")
        by_number = self.buscar(self.vendedor, periodo="mes", q=self.lunes.name)
        self.assertEqual(self.names(by_number), {self.lunes.name})
        self.assertIn(self.lunes.name, self.names(self.buscar(self.vendedor, periodo="mes", q=number)))
        # never another seller's order, even searching its customer
        self.assertNotIn(self.ajeno.name,
                         self.names(self.buscar(self.vendedor, periodo="mes", q="el faro")))

    def test_status_filter_and_totals(self):
        data = self.buscar(self.vendedor, periodo="hoy")
        self.assertEqual(data["totales"]["n"], 1, "the cancelled one is not counted")
        self.assertEqual(data["totales"]["importe"], "$ 1.000")
        self.assertEqual(data["totales"]["cancelados"], 1)
        cancelados = self.buscar(self.vendedor, periodo="hoy", estado="cancelados")
        self.assertEqual(self.names(cancelados), {self.cancelado.name})
        sin_confirmar = self.buscar(self.vendedor, periodo="hoy", estado="sin_confirmar")
        self.assertEqual(self.names(sin_confirmar), {self.hoy.name})

    def test_card_shows_armado_shortages_delivery_and_payment(self):
        self.hoy.sudo().action_confirm()
        self.hoy.sudo().write({"date_order": NOW - timedelta(hours=1), "distri_armado": "armado"})
        self.hoy.order_line.sudo().write({"distri_faltante": 1.0})
        row = mis_ventas.fila(self.hoy)
        self.assertEqual(row["estado"], "Confirmado")
        self.assertEqual(row["armado"], "Armado")
        self.assertEqual(row["faltantes"], ["1 × Yerba Mis Ventas V18"])
        self.assertTrue(row["entrega"])
        self.assertEqual(row["pago"], "Sin facturar")
        draft = mis_ventas.fila(self.lunes)
        self.assertEqual((draft["estado"], draft["armado"], draft["pago"]), ("Sin confirmar", "", ""))
        cancelled = mis_ventas.fila(self.cancelado)
        self.assertTrue(cancelled["cancelado"])
        self.assertEqual(cancelled["amount_raw"], 0.0)

    def test_who_can_open_it(self):
        self.assertTrue(mis_ventas.puede_ver_mis_ventas(self.vendedor))
        self.assertTrue(mis_ventas.puede_ver_mis_ventas(self.gerente))
        self.assertFalse(mis_ventas.puede_ver_mis_ventas(self.armador))
        self.assertFalse(mis_ventas.puede_ver_mis_ventas(self.portal))
        titles = [t["title"] for t in tablero.staff_tiles(self.env(user=self.vendedor))]
        self.assertIn("Mis ventas", titles)
        self.assertNotIn("Mis ventas",
                         [t["title"] for t in tablero.staff_tiles(self.env(user=self.armador))])
        self.assertTrue(self.vendedor._distri_ajustes()["mis_ventas"])
        self.assertFalse(self.armador._distri_ajustes()["mis_ventas"])


@tagged("post_install", "-at_install", "distri_v129_18")
class TestMisVentasPagina(MisVentasDatos, HttpCase):

    def test_seller_page(self):
        self.hoy.sudo().write({"date_order": datetime.now() - timedelta(minutes=5)})
        self.ajeno.sudo().write({"date_order": datetime.now() - timedelta(minutes=5)})
        self.authenticate(self.vendedor.login, self.vendedor.login)
        res = self.url_open("/mis-ventas?periodo=hoy&vendedor=%s" % self.otro.id)
        self.assertEqual(res.status_code, 200)
        self.assertIn(self.hoy.name, res.text)
        self.assertNotIn(self.ajeno.name, res.text)
        self.assertNotIn('id="mv-vendedor"', res.text, "no seller picker for a seller")
        box = re.search(r'<input[^>]*id="mv-q"[^>]*>', res.text).group(0)
        for attr in ('type="search"', 'enterkeyhint="search"', "data-mv-buscar"):
            self.assertIn(attr, box)
        self.assertRegex(res.text, r'src="/dn/mis_ventas\.js\?v=[0-9a-f]{12}"')
        # the search of the server too (shared link, no JavaScript)
        res = self.url_open("/mis-ventas?periodo=hoy&q=zzz-nada")
        self.assertNotIn(self.hoy.name, res.text)

    def test_manager_page_has_the_seller_picker(self):
        self.ajeno.sudo().write({"date_order": datetime.now() - timedelta(minutes=5)})
        self.authenticate(self.gerente.login, self.gerente.login)
        res = self.url_open("/mis-ventas?periodo=hoy")
        self.assertEqual(res.status_code, 200)
        self.assertIn('id="mv-vendedor"', res.text)
        self.assertIn(self.ajeno.name, res.text)

    def test_portal_and_public_get_nothing(self):
        self.authenticate(self.portal.login, self.portal.login)
        res = self.url_open("/mis-ventas?periodo=mes")
        self.assertEqual(res.status_code, 403)
        self.assertNotIn(self.hoy.name, res.text)
        self.authenticate(self.armador.login, self.armador.login)
        self.assertEqual(self.url_open("/mis-ventas").status_code, 403)
        self.authenticate(None, None)
        res = self.url_open("/mis-ventas", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.assertIn("/web/login", res.headers.get("Location", ""))
