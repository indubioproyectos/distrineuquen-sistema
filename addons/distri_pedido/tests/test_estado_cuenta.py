import re
from datetime import date, timedelta

from odoo import Command, fields
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.main import POR_PAGINA
from odoo.addons.distri_pedido.models.res_users import DEFAULT_TZ


@tagged("post_install", "-at_install", "distri_estado_cuenta")
class TestEstadoCuenta(HttpCase):
    """One statement: Online history + this Odoo + reported returns
    (models/estado_cuenta.py)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.armador = new_test_user(
            cls.env, login="armador_estado", name="Armador Estado",
            groups="base.group_user,distri_pedido.grupo_armador")
        cls.admin_user = new_test_user(
            cls.env, login="admin_estado", name="Admin Estado",
            groups="base.group_user,distri_pedido.grupo_administracion")
        cls.partner = cls.env["res.partner"].create({"name": "KIOSCO ESTADO"})
        cls.product = cls.env["product.product"].create({
            "name": "Producto estado", "list_price": 100, "type": "consu",
            "taxes_id": [Command.clear()]})
        pricelist = cls.env["product.pricelist"].create({
            "name": "Estado test", "currency_id": cls.env.company.currency_id.id})
        cls.order = cls.env["sale.order"].create({
            "partner_id": cls.partner.id,
            "pricelist_id": pricelist.id,
            "order_line": [Command.create({
                "product_id": cls.product.id, "product_uom_qty": 4,
                "price_unit": 250.0, "tax_ids": [Command.clear()]})],
        })
        cls.order.action_confirm()
        cls.order._portal_ensure_token()
        pedido = cls.order.with_user(cls.armador).sudo()
        assert pedido.distri_tomar() == ""
        assert pedido.distri_terminar(1) == ""
        cls.invoice = cls.order.distri_factura_auto_id
        cls.remito = cls.order._distri_remitos()
        Mov = cls.env["distri.cuenta.movimiento"].sudo()
        cls.historia = Mov.create([
            {"partner_id": cls.partner.id, "fecha": "2025-04-04",
             "comprobante": "0001/2025/00001", "concepto": "0001/2025/00001",
             "tipo": "factura", "debe": 1500.0},
            {"partner_id": cls.partner.id, "fecha": "2025-05-10",
             "comprobante": "PBNK1/2025/00009", "concepto": "Manual Payment: 0001/2025/00001",
             "tipo": "pago", "haber": 400.0},
            # also alive in Odoo: counts once, the Odoo one wins
            {"partner_id": cls.partner.id, "fecha": fields.Date.today(),
             "comprobante": cls.invoice.name, "tipo": "factura",
             "debe": cls.invoice.amount_total},
        ])

    def test_full_history_sorted_with_running_balance(self):
        data = self.partner._distri_statement()
        rows = data["rows"]
        self.assertEqual(len(rows), 3, "the duplicated invoice counts once")
        self.assertEqual([r["date"] for r in rows], sorted(r["date"] for r in rows))
        self.assertEqual(rows[0]["number"], "0001/2025/00001")
        self.assertEqual(rows[0]["doc_type"], "Factura")
        self.assertEqual(rows[0]["source"], "online")
        self.assertEqual([r["balance"] for r in rows], [1500.0, 1100.0, 2100.0])
        odoo_row = rows[2]
        self.assertEqual(odoo_row["source"], "odoo")
        self.assertEqual(odoo_row["number"], self.invoice.name)
        self.assertEqual(odoo_row["remito"], self.remito.name)
        self.assertEqual(data["balance"], self.partner._distri_account_balance())

    def test_archived_payment_is_out_even_with_archived_records_shown(self):
        """A payment archived as repeated (01/11/2026) never counts, also when the screen
        opens with archived records included (the candidates list of the wizard)."""
        repeated = self.env["distri.cuenta.movimiento"].sudo().create({
            "partner_id": self.partner.id, "fecha": "2025-05-10", "comprobante": "PCSH1/2025/00099",
            "concepto": "repetido · archivado en el lab el 01/11/2026", "tipo": "pago", "haber": 400.0,
            "active": False})
        balance = self.partner._distri_account_balance()
        loose = self.partner.with_context(active_test=False)
        self.assertEqual(loose._distri_account_balance(), balance)
        self.assertNotIn(repeated.comprobante, [r["number"] for r in loose._distri_statement_rows()])
        wizard = self.env["distri.estado.cuenta"].with_context(active_test=False).create(
            {"partner_id": self.partner.id})
        self.assertEqual(wizard.balance, balance)
        self.assertNotIn(repeated.comprobante, str(wizard.statement_html))

    def test_owed_invoice_found_by_the_review_is_red(self):
        """An invoice the review found unpaid is shown in red with what is owed (01/11/2026);
        nothing is inferred for the other rows."""
        invoice = self.historia[0]
        invoice.write({"distri_owed": True, "distri_owed_amount": 1100.0,
                       "distri_owed_note": "pagó $400 de $1.500"})
        rows = self.partner._distri_statement_rows()
        owed = [r for r in rows if r.get("owed")]
        self.assertEqual([r["number"] for r in owed], ["0001/2025/00001"])
        self.assertEqual(owed[0]["owed"], 1100.0)
        html = str(self.partner.distri_statement_html)
        self.assertIn("table-danger", html)
        self.assertIn("Facturas adeudadas (revisadas): 1", html)
        self.assertEqual(html.count("o_distri_estado_adeudada'"), 1)
        # unmarked: no red at all
        invoice.write({"distri_owed": False})
        self.assertNotIn("table-danger", str(self.partner._distri_statement_table(
            self.partner._distri_statement())))

    def test_date_filter_keeps_the_opening_balance(self):
        data = self.partner._distri_statement(date(2025, 5, 1), date(2025, 12, 31))
        self.assertEqual(len(data["rows"]), 1)
        self.assertEqual(data["opening"], 1500.0)
        self.assertEqual(data["closing"], 1100.0)
        self.assertEqual(data["balance"], 2100.0)

    def test_backend_tab_button_and_pdf(self):
        partner = self.partner.with_user(self.admin_user)
        html = str(partner.distri_statement_html)
        self.assertIn(self.invoice.name, html, "staff see the invoice number")
        self.assertIn(self.remito.name, html, "and the remito")
        self.assertIn("0001/2025/00001", html)
        self.assertEqual(partner.distri_statement_balance, 2100.0)
        action = partner.action_distri_estado_cuenta()
        wizard = self.env["distri.estado.cuenta"].with_user(self.admin_user).browse(action["res_id"])
        wizard.date_from = date(2025, 5, 1)
        self.assertEqual(wizard.opening, 1500.0)
        self.assertEqual(wizard.balance, 2100.0)
        report = self.env["ir.actions.report"].with_user(self.admin_user)
        html, _kind = report._render_qweb_html("distri_pedido.report_estado_cuenta", wizard.ids)
        html = html.decode()
        self.assertIn("Estado de cuenta", html)
        self.assertIn("Saldo anterior", html)
        self.assertIn("PBNK1/2025/00009", html)
        self.assertIn("KIOSCO ESTADO", html)

    def test_customer_page_full_history_without_invoice_numbers(self):
        res = self.url_open("/pedido/%s/cuenta" % self.order.access_token)
        self.assertEqual(res.status_code, 200)
        html = res.text
        self.assertIn(self.remito.name, html)
        self.assertNotIn(self.invoice.name, html)
        self.assertNotIn("0001/2025/00001", html)
        self.assertNotIn("Factura", html)
        self.assertIn("Movimientos 1 a 3 de 3", html)

    def test_customer_page_is_paginated(self):
        Mov = self.env["distri.cuenta.movimiento"].sudo()
        start = date(2025, 6, 1)
        Mov.create([{"partner_id": self.partner.id, "fecha": start + timedelta(days=i),
                     "comprobante": "0001/2025/%05d" % (100 + i), "tipo": "factura",
                     "debe": 10.0} for i in range(POR_PAGINA + 10)])
        total = POR_PAGINA + 13
        url = "/pedido/%s/cuenta" % self.order.access_token
        first = self.url_open(url).text
        self.assertEqual(len(re.findall(r'class="mov"', first)), POR_PAGINA)
        self.assertIn("Movimientos 1 a %d de %d" % (POR_PAGINA, total), first)
        self.assertIn("?pagina=2", first)
        self.assertIn("Ver todo", first)
        second = self.url_open(url + "?pagina=2").text
        self.assertEqual(len(re.findall(r'class="mov"', second)), total - POR_PAGINA)
        everything = self.url_open(url + "?pagina=todo").text
        self.assertEqual(len(re.findall(r'class="mov"', everything)), total)
        self.assertNotIn("0001/2025/00100", everything)
        # the oldest movement is on the last page, with its running balance
        self.assertIn("04/04/2025", second)

    def test_balance_equals_the_statement_for_every_customer(self):
        partners = self.env["distri.cuenta.movimiento"].sudo().search([]).partner_id
        for partner in partners[:200]:
            self.assertAlmostEqual(partner._distri_statement()["balance"],
                                   partner._distri_account_balance(), places=2,
                                   msg=partner.display_name)

    def test_como_pagar_before_delivery(self):
        """Paying before the delivery is fine: «Cómo pagar» shows once invoiced."""
        self.assertEqual(self.order.distri_delivery_state, "unassigned")
        res = self.url_open("/pedido/%s" % self.order.access_token)
        self.assertIn('id="como-pagar"', res.text)
        self.assertIn("Cómo pagar", res.text)
        self.assertIn("Tu pedido está armado", res.text)
        self.assertNotIn(self.invoice.name, res.text)


@tagged("post_install", "-at_install", "distri_estado_cuenta")
class TestZonaHoraria(HttpCase):

    def test_new_users_start_in_argentina(self):
        internal = new_test_user(self.env, login="tz_interno", groups="base.group_user")
        portal = new_test_user(self.env, login="tz_portal", groups="base.group_portal")
        self.assertEqual(internal.tz, DEFAULT_TZ)
        self.assertEqual(portal.tz, DEFAULT_TZ)
        other = new_test_user(self.env, login="tz_otro", groups="base.group_user",
                              tz="Europe/Madrid")
        self.assertEqual(other.tz, "Europe/Madrid", "an explicit zone is kept")

    def test_users_without_zone_get_argentina(self):
        user = new_test_user(self.env, login="tz_sin", groups="base.group_user")
        user.tz = False
        public = self.env.ref("base.public_user")
        public.sudo().tz = False
        self.env["res.users"]._distri_set_argentina_tz()
        self.assertEqual(user.tz, DEFAULT_TZ)
        self.assertEqual(public.tz, DEFAULT_TZ)

    def test_dates_follow_argentina(self):
        from datetime import datetime
        from odoo.tests import freeze_time
        user = new_test_user(self.env, login="tz_fecha", groups="base.group_user")
        with freeze_time("2026-09-27 01:30:00"):  # 22:30 of the 26th in Neuquén
            env = self.env(user=user)
            self.assertEqual(str(fields.Date.context_today(env["res.partner"])), "2026-09-26")
            local = fields.Datetime.context_timestamp(env["res.partner"], datetime(2026, 9, 27, 1, 30))
            self.assertEqual(local.strftime("%d/%m %H:%M"), "26/09 22:30")


@tagged("post_install", "-at_install", "distri_estado_cuenta")
class TestTextosCliente(HttpCase):

    def test_final_screen_says_received(self):
        view = self.env.ref("distri_pedido.gracias")
        arch = view.arch_db
        self.assertIn("Recibimos tu pedido", arch)
        self.assertNotIn("Tu pedido está armado", arch)
