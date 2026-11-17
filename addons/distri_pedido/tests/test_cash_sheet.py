import base64
from datetime import date

from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged
from odoo.tools import mute_logger

from odoo.addons.distri_pedido.models.cash_sheet import collector_code, fila_position

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
XLSX = b"PK\x03\x04 not really an xlsx"
DAY = date(2031, 11, 5)


@tagged("post_install", "-at_install", "distri_cash_sheet")
class TestCashSheet(HttpCase):
    """Cash sheets (models/cash_sheet.py) and their view in «Cuentas
    (celular)» (models/cuentas_app.py _app_cash_sheet): rows linked to lab
    payments without touching them, the loader's auto-link, and a seller
    who never sees the rows or the originals of other customers."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company
        Category = cls.env["res.partner.category"]
        cls.tag_a = Category.create({"name": "Cartera Planillas A"})
        cls.tag_b = Category.create({"name": "Cartera Planillas B"})

        def user(login, groups, tag=None):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups=groups, company_id=company.id,
                                company_ids=[Command.set(company.ids)])
            if tag:
                new.etiqueta_cartera_id = tag
            return new

        cls.admin_u = user("planillas_admin", "base.group_user,distri_pedido.grupo_administracion")
        cls.system_u = user("planillas_sistema", "base.group_user,base.group_system")
        cls.seller_a = user("planillas_vendedor_a", "base.group_user,distri_pedido.grupo_vendedor",
                            cls.tag_a)
        cls.seller_b = user("planillas_vendedor_b", "base.group_user,distri_pedido.grupo_vendedor",
                            cls.tag_b)

        Partner = cls.env["res.partner"]
        cls.pa = Partner.create({"name": "PLANILLA CLIENTE A", "customer_rank": 1,
                                 "category_id": [Command.link(cls.tag_a.id)]})
        cls.pa2 = Partner.create({"name": "PLANILLA CLIENTE A DOS", "customer_rank": 1,
                                  "category_id": [Command.link(cls.tag_a.id)]})
        cls.pb = Partner.create({"name": "PLANILLA CLIENTE B", "customer_rank": 1,
                                 "category_id": [Command.link(cls.tag_b.id)]})
        cls.cash = cls.env["account.journal"].search(
            [("type", "=", "cash"), ("company_id", "=", company.id)], limit=1)
        cls.pay_a = cls._pay(cls.pa, 1500.0, "Efectivo · cobró SIMON · planilla 05/11 · 0001/2026/99501")
        cls.pay_b = cls._pay(cls.pb, 2500.0, "Efectivo · cobró SIMON · planilla 05/11 · 0001/2026/99502")

        Sheet = cls.env["distri.cash.sheet"].sudo()
        cls.sheet = Sheet.create({
            "name": "2026.11.05 - NEUQUEN - SIMON", "collector": "SIMON", "date": DAY,
            "origin": "xlsx", "source_key": "xlsx:test:Sheet1", "line_ids": [
                Command.create({"sequence": 10, "source_key": "M900001", "client_raw": "CLIENTE A",
                                "partner_id": cls.pa.id, "invoice_ref": "0001/2026/99501",
                                "amount_invoiced": 2000.0, "amount_paid": 1500.0,
                                "method": "efectivo", "row_date": DAY}),
                Command.create({"sequence": 20, "source_key": "M900002", "client_raw": "CLIENTE B",
                                "partner_id": cls.pb.id, "invoice_ref": "0001/2026/99502",
                                "amount_invoiced": 2500.0, "amount_paid": 2500.0,
                                "method": "efectivo", "row_date": DAY}),
                Command.create({"sequence": 30, "source_key": "M900003", "client_raw": "NAFTA",
                                "amount_paid": -800.0, "row_kind": "gasto_cobrador",
                                "row_date": DAY}),
                Command.create({"sequence": 40, "source_key": "M900004", "client_raw": "TOTAL",
                                "amount_paid": 3200.0, "row_kind": "resumen", "after_total": True,
                                "row_date": DAY}),
            ]})
        cls.line_a, cls.line_b, cls.line_fuel, cls.line_total = cls.sheet.line_ids.sorted("sequence")
        cls.photo = cls.sheet._attach("foto.png", PNG, "image/png")
        cls.xlsx = cls.sheet._attach("2026.11.05 - NEUQUEN - SIMON.xlsx", XLSX,
                                     "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")

    @classmethod
    def _pay(cls, partner, amount, memo, when=DAY):
        payment = cls.env["account.payment"].create({
            "payment_type": "inbound", "partner_type": "customer", "partner_id": partner.id,
            "amount": amount, "journal_id": cls.cash.id, "date": when, "memo": memo})
        payment.action_post()
        return payment

    def _row_key(self, partner, payment):
        return next(r["line_key"] for r in partner._distri_statement_rows()
                    if r["res_model"] == "account.move" and r["res_id"] == payment.move_id.id)

    def _sheet_of(self, user, partner, payment):
        app = self.env["distri.account.app"].with_user(user)
        return app.app_line(partner.id, self._row_key(partner, payment))["cash"]["sheet"]

    # ------------------------------------------------------------ model

    def test_totals_labels_and_helpers(self):
        self.assertEqual(self.sheet.line_count, 4)
        self.assertEqual(self.sheet.total_paid, 4000.0, "only customer rows above the total")
        self.assertTrue(self.sheet.has_photo)
        self.assertEqual(self.sheet.collector_label, "Simón")
        self.assertEqual(collector_code(" Simón "), "SIMON")
        self.assertEqual(fila_position("s0511-2-01"), (2, 1, True))
        self.assertEqual(fila_position("s0511-07"), (1, 7, False))
        self.assertEqual(fila_position("r445"), (1, 445, False))
        self.assertFalse(self.sheet._attach("foto.png", PNG, "image/png"), "attached once")
        self.assertEqual(len(self.sheet.attachment_ids), 2)

    def test_one_payment_one_row(self):
        self.line_a.payment_id = self.pay_a
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.cr.savepoint():
            self.line_b.payment_id = self.pay_a
            self.env.flush_all()
        with mute_logger("odoo.sql_db"), self.assertRaises(IntegrityError), self.cr.savepoint():
            self.env["distri.cash.sheet"].sudo().create({
                "name": "otra", "collector": "SIMON", "date": DAY, "source_key": "xlsx:test:Sheet1"})
            self.env.flush_all()

    def test_access_rights(self):
        with self.assertRaises(AccessError):
            self.env["distri.cash.sheet"].with_user(self.seller_a).search([])
        with self.assertRaises(AccessError):
            self.env["distri.cash.sheet.line"].with_user(self.seller_a).search([])
        admin_sheet = self.sheet.with_user(self.admin_u)
        self.assertEqual(admin_sheet.line_ids, self.sheet.line_ids)
        with self.assertRaises(AccessError):
            admin_sheet.write({"note": "no"})
        with self.assertRaises(AccessError):
            self.line_a.with_user(self.admin_u).write({"payment_id": self.pay_a.id})
        self.sheet.with_user(self.system_u).write({"note": "Settings sí"})

    # ------------------------------------------------------------ loader

    def test_loader_row_creates_and_reuses_its_sheet(self):
        pay = self._pay(self.pa, 700.0, "Efectivo · cobró SIMON · planilla 06/11 · 0001/2026/99601",
                        date(2031, 11, 6))
        before = (pay.state, pay.amount, pay.move_id.state, len(pay.move_id.line_ids))
        Sheet = self.env["distri.cash.sheet"]
        item = {"fila": "s0611-2-01", "clave": "distri_cobro_ef_test_1", "fecha": "2031-11-06",
                "repartidor": "SIMON", "factura": "0001/2026/99601", "efectivo": 700.0,
                "importe_factura": 900.0, "cliente_planilla": "CLIENTE A ESCRITO",
                "lab_partner": self.pa.id}
        line = Sheet._link_loader_row(item, pay)
        self.assertEqual(line.payment_id, pay)
        self.assertEqual(line.sheet_id.source_key, "carga:SIMON:2031-11-06:2")
        self.assertEqual(line.sheet_id.origin, "transcription")
        self.assertEqual(line.sheet_id.name, "Simón · 06/11/2031 · planilla 2")
        self.assertEqual((line.sequence, line.partner_id, line.invoice_ref),
                         (1, self.pa, "0001/2026/99601"))
        self.assertEqual(Sheet._link_loader_row(item, pay), line, "idempotent")
        other = self._pay(self.pa2, 300.0, "x", date(2031, 11, 6))
        second = Sheet._link_loader_row(dict(item, fila="s0611-2-02", clave="distri_cobro_ef_test_2",
                                             efectivo=300.0, lab_partner=self.pa2.id), other)
        self.assertEqual(second.sheet_id, line.sheet_id, "same collector, day and sheet index")
        first_sheet = Sheet._link_loader_row(dict(item, fila="s0611-03", clave="distri_cobro_ef_test_3"))
        self.assertNotEqual(first_sheet.sheet_id, line.sheet_id)
        self.assertFalse(first_sheet.payment_id)
        self.assertEqual(before, (pay.state, pay.amount, pay.move_id.state, len(pay.move_id.line_ids)),
                         "linking never touches the payment")
        # an undone load (payment cancelled) and loaded again: the row moves to the new payment
        pay.action_draft()
        pay.action_cancel()
        again = self._pay(self.pa, 700.0, "x", date(2031, 11, 6))
        self.assertEqual(Sheet._link_loader_row(item, again), line)
        self.assertEqual(line.payment_id, again)

    def test_loader_row_on_a_day_of_an_excel_sheet(self):
        Sheet = self.env["distri.cash.sheet"]
        item = {"fila": "r900", "clave": "distri_cobro_ef_test_x", "fecha": DAY.isoformat(),
                "repartidor": "Simon", "factura": "", "efectivo": 2500.0, "lab_partner": self.pb.id}
        self.assertFalse(Sheet._link_loader_row(item), "without payment the Excel already has the row")
        line = Sheet._link_loader_row(item, self.pay_b)
        self.assertEqual(line, self.line_b)
        self.assertEqual(line.link_kind, "importe")
        archive = Sheet._link_loader_row(dict(item, fila="M900001", clave="distri_cobro_ef_test_y",
                                              efectivo=1500.0, lab_partner=self.pa.id), self.pay_a)
        self.assertEqual(archive, self.line_a, "a row of the archive is linked by its mov id")
        self.assertEqual(archive.link_kind, "clave")

    def test_link_by_memo(self):
        Sheet = self.env["distri.cash.sheet"]
        self.assertEqual(Sheet._memo_parts(self.pay_a), ("SIMON", DAY, "0001/2026/99501"))
        self.assertEqual(Sheet._link_by_memo(self.pay_a), self.line_a)
        self.assertEqual(self.line_a.link_kind, "memo")
        self.assertFalse(Sheet._link_by_memo(self._pay(self.pa, 10.0, "Efectivo · cobró SIMON · "
                                                       "planilla 05/11 · 0001/2026/00000")))
        late = self._pay(self.pa, 10.0, "Efectivo · cobró JORGE · planilla 30/12 · sin factura",
                         date(2032, 1, 2))
        self.assertEqual(Sheet._memo_parts(late), ("JORGE", date(2031, 12, 30), ""))

    # ------------------------------------------------------------ app

    def test_admin_sees_the_whole_sheet(self):
        self.line_a.payment_id = self.pay_a
        sheet = self._sheet_of(self.admin_u, self.pa, self.pay_a)
        self.assertEqual(sheet["id"], self.sheet.id)
        self.assertEqual((sheet["collector"], sheet["date"]), ("Simón", "2031-11-05"))
        self.assertEqual([r["client"] for r in sheet["rows"]], ["CLIENTE A", "CLIENTE B", "NAFTA", "TOTAL"])
        self.assertEqual([r["hit"] for r in sheet["rows"]], [True, False, False, False])
        self.assertEqual(sheet["hidden_count"], 0)
        self.assertEqual(sheet["total"], 4000.0)
        self.assertEqual({f["kind"] for f in sheet["files"]}, {"image", "xlsx"})
        data, mimetype, name = self.env["distri.account.app"].with_user(self.admin_u).app_sheet_file(
            self.sheet.id, self.photo.id)
        self.assertEqual((data, mimetype, name), (PNG, "image/png", "foto.png"))

    def test_seller_sees_the_sheet_but_not_a_distributors_row(self):
        """19.0.1.92.0: sellers see the whole sheet (they chase old missing
        receipts); only a row of a distributor stays hidden from a seller
        without Administración, and then the originals are not offered."""
        self.line_a.payment_id = self.pay_a
        self.line_b.payment_id = self.pay_b
        app = self.env["distri.account.app"].with_user(self.seller_a)
        sheet = self._sheet_of(self.seller_a, self.pa, self.pay_a)
        self.assertEqual([r["client"] for r in sheet["rows"]], ["CLIENTE A", "CLIENTE B", "NAFTA", "TOTAL"])
        self.assertEqual(sheet["hidden_count"], 0)
        self.assertEqual(sheet["total"], 4000.0)
        self.assertEqual({f["kind"] for f in sheet["files"]}, {"image", "xlsx"})
        self.assertEqual(app.app_sheet_file(self.sheet.id, self.photo.id)[0], PNG)

        self.pb.distri_lista_oculta = True
        sheet = self._sheet_of(self.seller_a, self.pa, self.pay_a)
        self.assertEqual([r["client"] for r in sheet["rows"]], ["CLIENTE A", "NAFTA", "TOTAL"])
        self.assertEqual(sheet["hidden_count"], 1)
        self.assertEqual(sheet["total"], 1500.0)
        self.assertEqual(sheet["total_label"], "Total de los clientes que ves")
        self.assertEqual(sheet["files"], [], "a photo shows every row")
        payload = str(sheet)
        self.assertNotIn("CLIENTE B", payload)
        self.assertNotIn("2500", payload)
        for attachment in (self.photo, self.xlsx):
            with self.assertRaises(AccessError):
                app.app_sheet_file(self.sheet.id, attachment.id)
        with self.assertRaises(AccessError):  # an attachment of another sheet
            own = self.env["distri.cash.sheet"].sudo().create({
                "name": "propia", "collector": "ITALO", "date": DAY, "source_key": "xlsx:test:own",
                "line_ids": [Command.create({"client_raw": "A2", "partner_id": self.pa2.id,
                                             "amount_paid": 50.0})]})
            app.app_sheet_file(own.id, self.photo.id)

    def test_http_routes(self):
        self.line_a.payment_id = self.pay_a
        url = "/cuentas/api/sheet_file?sheet=%d&file=%d" % (self.sheet.id, self.photo.id)
        self.authenticate(self.seller_a.login, self.seller_a.login)
        # 19.0.1.92.0: the seller sees the whole sheet and its originals
        self.assertEqual(self.url_open(url, allow_redirects=False).status_code, 200)
        line = self.url_open("/cuentas/api/line?id=%d&key=%s" % (
            self.pa.id, self._row_key(self.pa, self.pay_a))).json()
        self.assertTrue(line["ok"])
        self.assertEqual(line["data"]["cash"]["sheet"]["hidden_count"], 0)
        self.pb.distri_lista_oculta = True
        self.assertEqual(self.url_open(url, allow_redirects=False).status_code, 403)
        self.authenticate(self.admin_u.login, self.admin_u.login)
        res = self.url_open(url, allow_redirects=False)
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.headers["Content-Type"], "image/png")
        self.assertEqual(res.content, PNG)
        res = self.url_open("/cuentas/api/sheet_file?sheet=%d&file=%d" % (self.sheet.id, self.xlsx.id),
                            allow_redirects=False)
        self.assertEqual(res.headers["Content-Type"], "application/octet-stream")
        self.assertIn("attachment", res.headers["Content-Disposition"])
