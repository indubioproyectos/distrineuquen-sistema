"""19.0.1.129.8: «Transferencias» inside /cuentas (models/cuentas_transferencias.py,
controllers/cuentas.py /cuentas/api/transfers and transfer_receipt).

- Access: administración, gerencia and sistemas read it; cajero, repartidor
  and vendedor get 403 from the API (and no entry flag in the debtors).
- Filters: period (today, yesterday, a custom range), person and state
  chips («No cruzadas», «Con anomalía», «Sin comprobante»); the count and
  amount of the filter and the subtotal per person.
- Newest first, 50 per page with «Ver más».
- The receipt (image or PDF) only through a row the user can read.
Synthetic data only, in ranges no real row uses (2001, 2002).
"""
import base64
from datetime import datetime, timedelta

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.reparto_cliente import today_ar

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==")
PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
RANGE = "period=custom&date_from=2001-03-01&date_to=2001-03-31"


@tagged("post_install", "-at_install", "distri_v129_8")
class TestV1298CuentasTransferencias(HttpCase):

    seq = 0

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = cls.env.company

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups, company_id=company.id,
                                 company_ids=[Command.set(company.ids)])

        cls.manager = user("gerente_v1298", "distri_pedido.grupo_gerencia")
        cls.admin_u = user("admin_v1298", "distri_pedido.grupo_administracion")
        cls.sysadmin = user("sistemas_v1298", "base.group_system")
        cls.cashier = user("cajero_v1298", "distri_pedido.grupo_cajero")
        cls.driver = user("reparto_v1298", "distri_pedido.grupo_reparto")
        cls.seller = user("vendedor_v1298", "distri_pedido.grupo_vendedor")
        Partner = cls.env["res.partner"]
        cls.pa = Partner.create({"name": "TRANSFERENCIAS V1298 A", "customer_rank": 1})
        cls.pb = Partner.create({"name": "TRANSFERENCIAS V1298 B", "customer_rank": 1})
        Card = cls.env["distri.mp.collection"].sudo()
        # March 2001: the seller loads three, the driver two
        cls.c1 = cls._card(cls.seller, cls.pa, 10000.40, "2001-03-05 15:00:00", matched=True,
                           audit="verificado")
        cls.c2 = cls._card(cls.seller, cls.pb, 2500.0, "2001-03-06 15:00:00", audit="diferencia",
                           file="pdf")
        cls.c3 = cls._card(cls.seller, cls.pa, 700.0, "2001-03-07 15:00:00", file=None)
        cls.c4 = cls._card(cls.driver, cls.pb, 1300.0, "2001-03-08 15:00:00", matched=True,
                           audit="verificado")
        cls.c5 = cls._card(cls.driver, cls.pa, 999.0, "2001-04-02 15:00:00", matched=True)
        # today and yesterday (Argentina, noon) by the cashier
        today = today_ar()
        cls.today_card = cls._card(cls.cashier, cls.pa, 4000.0, "%s 15:00:00" % today)
        cls.yesterday_card = cls._card(cls.cashier, cls.pb, 6000.0,
                                       "%s 15:00:00" % (today - timedelta(days=1)))
        cls.Card = Card

    @classmethod
    def _card(cls, user, partner, amount, when, matched=False, audit="pendiente", file="image"):
        cls.seq += 1
        Message = cls.env["distri.whatsapp.message"].sudo()
        message = False
        if file:
            raw, mime = (PNG, "image/png") if file == "image" else (PDF, "application/pdf")
            attachment = cls.env["ir.attachment"].sudo().create({
                "name": "comprobante-v1298-%d.%s" % (cls.seq, "png" if file == "image" else "pdf"),
                "raw": raw, "mimetype": mime})
            message = Message.create({
                "wa_message_id": "wamid.v1298.%d" % cls.seq, "direction": "in",
                "message_type": "image" if file == "image" else "document",
                "media_mime_type": mime, "attachment_id": attachment.id})
        Card = cls.env["distri.mp.collection"].sudo()
        income = Card.create({
            "kind": "income", "source_id": "991298%06d" % cls.seq, "amount": amount,
            "date": when, "partner_id": partner.id, "state": "confirmed"}) if matched else Card
        return Card.create({
            "kind": "verify", "amount": amount, "date": when, "partner_id": partner.id,
            "state": "confirmed", "credited_at": when, "credited_by_id": user.id,
            "receipt_id": message and message.id, "income_id": income.id or False,
            "audit_state": audit, "operation": "OP-V1298-%d" % cls.seq})

    # ------------------------------------------------------------ helpers

    def _get(self, url):
        return self.url_open(url, allow_redirects=False)

    def _data(self, query):
        res = self._get("/cuentas/api/transfers?" + query)
        self.assertEqual(res.status_code, 200, res.text)
        body = res.json()
        self.assertTrue(body["ok"], body)
        return body["data"]

    # ------------------------------------------------------------ access

    def test_access_by_group(self):
        for user in (self.manager, self.admin_u, self.sysadmin):
            self.authenticate(user.login, user.login)
            debtors = self._get("/cuentas/api/debtors").json()["data"]
            self.assertTrue(debtors["can_see_transfers"], user.login)
            data = self._data(RANGE)
            self.assertEqual(data["count"], 4, user.login)
            receipt = self._get("/cuentas/api/transfer_receipt?id=%d" % self.c1.id)
            self.assertEqual(receipt.status_code, 200, user.login)
        for user in (self.cashier, self.driver, self.seller):
            self.authenticate(user.login, user.login)
            res = self._get("/cuentas/api/transfers?" + RANGE)
            self.assertEqual(res.status_code, 403, user.login)
            self.assertFalse(res.json()["ok"])
            self.assertNotIn("rows", res.json())
            self.assertEqual(self._get(
                "/cuentas/api/transfer_receipt?id=%d" % self.c1.id).status_code, 403, user.login)
        # the seller and the driver open /cuentas, without the entry
        for user in (self.driver, self.seller):
            self.authenticate(user.login, user.login)
            debtors = self._get("/cuentas/api/debtors").json()["data"]
            self.assertFalse(debtors["can_see_transfers"], user.login)
        # the model refuses too, whatever the client sends
        from odoo.exceptions import AccessError
        with self.assertRaises(AccessError):
            self.env["distri.account.app"].with_user(self.seller).app_transfers(period="month")
        self.authenticate(None, None)
        self.assertNotEqual(self._get("/cuentas/api/transfers").status_code, 200)

    # ------------------------------------------------------------ list and filters

    def test_list_newest_first_with_totals(self):
        self.authenticate(self.manager.login, self.manager.login)
        data = self._data(RANGE)
        self.assertEqual([r["id"] for r in data["rows"]],
                         [self.c4.id, self.c3.id, self.c2.id, self.c1.id])
        self.assertEqual(data["count"], 4)
        self.assertAlmostEqual(data["amount"], 10000.40 + 2500 + 700 + 1300)
        self.assertFalse(data["more"])
        first = data["rows"][-1]
        self.assertEqual(first["date"], "2001-03-05")
        self.assertEqual(first["time"], "12:00")  # Argentina
        self.assertEqual(first["loaded_by"], self.seller.name)
        self.assertEqual(first["partner"], self.pa.name)
        self.assertEqual(first["partner_id"], self.pa.id)
        self.assertEqual(first["audit_state"], "verificado")
        self.assertTrue(first["matched"])
        self.assertEqual(first["file_kind"], "image")
        c2 = data["rows"][2]
        self.assertFalse(c2["matched"])
        self.assertTrue(c2["with_anomaly"])
        self.assertEqual(c2["file_kind"], "pdf")
        c3 = data["rows"][1]
        self.assertTrue(c3["without_receipt"])
        self.assertFalse(c3["has_file"])
        # per person, highest amount first
        self.assertEqual([(p["id"], p["count"]) for p in data["by_person"]],
                         [(self.seller.id, 3), (self.driver.id, 1)])
        self.assertAlmostEqual(data["by_person"][0]["amount"], 13200.40)
        people = {p["id"] for p in data["people"]}
        self.assertTrue({self.seller.id, self.driver.id, self.cashier.id} <= people)

    def test_filters_by_user_state_and_date(self):
        self.authenticate(self.admin_u.login, self.admin_u.login)
        data = self._data(RANGE + "&user=%d" % self.driver.id)
        self.assertEqual([r["id"] for r in data["rows"]], [self.c4.id])
        self.assertAlmostEqual(data["amount"], 1300.0)
        data = self._data(RANGE + "&user=%d&state=not_matched" % self.seller.id)
        self.assertEqual({r["id"] for r in data["rows"]}, {self.c2.id, self.c3.id})
        self.assertEqual(data["count"], 2)
        self.assertAlmostEqual(data["amount"], 3200.0)
        data = self._data(RANGE + "&state=anomaly")
        self.assertEqual([r["id"] for r in data["rows"]], [self.c2.id])
        data = self._data(RANGE + "&state=no_receipt")
        self.assertEqual([r["id"] for r in data["rows"]], [self.c3.id])
        # the range end is inclusive and April stays out
        data = self._data("period=custom&date_from=2001-03-06&date_to=2001-03-07")
        self.assertEqual({r["id"] for r in data["rows"]}, {self.c2.id, self.c3.id})
        data = self._data("period=custom&date_from=2001-04-01&date_to=2001-04-30")
        self.assertEqual([r["id"] for r in data["rows"]], [self.c5.id])
        # today / yesterday / week / month (the cashier's own rows)
        cashier = "&user=%d" % self.cashier.id
        self.assertEqual([r["id"] for r in self._data("period=today" + cashier)["rows"]],
                         [self.today_card.id])
        self.assertEqual([r["id"] for r in self._data("period=yesterday" + cashier)["rows"]],
                         [self.yesterday_card.id])
        month = self._data("period=month" + cashier)
        expected = {self.today_card.id}
        if today_ar().day > 1:
            expected.add(self.yesterday_card.id)
        self.assertEqual({r["id"] for r in month["rows"]}, expected)
        week = self._data("period=week" + cashier)
        self.assertIn(self.today_card.id, {r["id"] for r in week["rows"]})
        # a reversed range is refused
        res = self._get("/cuentas/api/transfers?period=custom&date_from=2001-03-10"
                        "&date_to=2001-03-01")
        self.assertEqual(res.status_code, 400)

    def test_pages_of_fifty(self):
        for n in range(55):
            self._card(self.cashier, self.pa, 100.0 + n,
                       (datetime(2002, 5, 1, 15) + timedelta(minutes=n)).strftime(
                           "%Y-%m-%d %H:%M:%S"))
        self.authenticate(self.manager.login, self.manager.login)
        query = "period=custom&date_from=2002-05-01&date_to=2002-05-01"
        page = self._data(query)
        self.assertEqual(len(page["rows"]), 50)
        self.assertTrue(page["more"])
        self.assertEqual(page["count"], 55)
        self.assertAlmostEqual(page["rows"][0]["amount"], 154.0)
        self.assertIn("people", page)
        rest = self._data(query + "&offset=50")
        self.assertEqual(len(rest["rows"]), 5)
        self.assertFalse(rest["more"])
        self.assertNotIn("people", rest)
        self.assertFalse({r["id"] for r in page["rows"]} & {r["id"] for r in rest["rows"]})

    # ------------------------------------------------------------ receipt

    def test_receipt_image_pdf_and_missing(self):
        self.authenticate(self.manager.login, self.manager.login)
        image = self._get("/cuentas/api/transfer_receipt?id=%d" % self.c1.id)
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.headers["Content-Type"], "image/png")
        self.assertEqual(image.content, PNG)
        pdf = self._get("/cuentas/api/transfer_receipt?id=%d" % self.c2.id)
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf.headers["Content-Type"], "application/pdf")
        self.assertIn("inline", pdf.headers["Content-Disposition"])
        self.assertEqual(self._get(
            "/cuentas/api/transfer_receipt?id=%d" % self.c3.id).status_code, 403)
        self.assertEqual(self._get("/cuentas/api/transfer_receipt?id=abc").status_code, 403)

    def test_read_only(self):
        """Reading the section writes nothing on the cards."""
        self.authenticate(self.manager.login, self.manager.login)
        before = self.c1.read(["write_date", "state", "audit_state"])
        self._data(RANGE)
        self._get("/cuentas/api/transfer_receipt?id=%d" % self.c1.id)
        self.c1.invalidate_recordset()
        self.assertEqual(self.c1.read(["write_date", "state", "audit_state"]), before)
        res = self.url_open("/cuentas/api/transfers", data={"period": "today"})
        self.assertNotEqual(res.status_code, 200)
