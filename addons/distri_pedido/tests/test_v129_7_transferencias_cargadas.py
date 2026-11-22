"""19.0.1.129.7: «Transferencias cargadas» in «Auditoría de cobros».

- Content: credited receipts (also a customer's receipt credited by an
  approver, also annulled) and the staff uploads not credited yet; never a
  report income, a customer's receipt nobody credited or a credit undone.
- Newest first by the moment it was loaded or credited.
- The filters of the search view (user, today, yesterday, week, month,
  range, audit state, «no cruzadas», «con anomalía») and the group totals.
- Only Administración, Gerencia and Sistemas: no menu and an access error
  for vendedor and cajero.
- The links of the row and the entry point from «Resumen diario por persona».
- A bounded number of queries for the list, whatever the number of rows.
Synthetic data only.
"""
import io
from datetime import timedelta

from dateutil.relativedelta import relativedelta
from lxml import etree
from PIL import Image

from odoo import Command, fields
from odoo.exceptions import AccessError
from odoo.tests import new_test_user, tagged
from odoo.tools.safe_eval import safe_eval

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
from odoo.addons.distri_pedido.models.reparto_cliente import today_ar
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin, _local

AUDIT_MENU = "distri_pedido.menu_cobros_auditoria"
LIST_MENU = "distri_pedido.menu_cobros_transferencias_cargadas"


@tagged("post_install", "-at_install", "distri_v129_7")
class TestV1297TransferenciasCargadas(CreditMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        params = cls.env["ir.config_parameter"].sudo()
        params.set_param("distri_pedido.cuenta_propia", TEST_OWN_ACCOUNT)
        params.set_param(PARAM_DRIVER_RULE, "")
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        cls.seller = user("vendedor_v1297", "distri_pedido.grupo_vendedor")
        cls.cashier = user("cajero_v1297", "distri_pedido.grupo_cajero")
        cls.admin_user = user("admin_v1297", "distri_pedido.grupo_administracion")
        cls.manager = user("gerente_v1297", "distri_pedido.grupo_gerencia")
        cls.sysadmin = user("sistemas_v1297", "base.group_system")
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V1297",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Prueba V1297",
                                                           "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Report = cls.env["distri.mp.report"]
        cls.Loaded = cls.env["distri.loaded.transfer"]

    # ------------------------------------------------------------ helpers

    def _report(self, *rows):
        header = ("TRANSACTION_DATE;SOURCE_ID;PAYMENT_METHOD_TYPE;TRANSACTION_TYPE;"
                  "TRANSACTION_AMOUNT;PAYER_NAME;PAYMENT_METHOD;PAYER_ID_TYPE;PAYER_ID_NUMBER")
        lines = [header]
        for source, when, amount, cuit in rows:
            local = _local(when).strftime("%Y-%m-%dT%H:%M:%S.000-03:00")
            lines.append(";".join([local, source, "bank_transfer", "SETTLEMENT",
                                   "%.2f" % amount, "PAGADOR PRUEBA", "bank_transfer",
                                   "CUIT" if cuit else "", cuit or ""]))
        CreditMixin.seq += 1
        return self.Report._import_file("v1297-%s.csv" % CreditMixin.seq,
                                        ("\n".join(lines) + "\n").encode())

    def _credited(self, amount, operation=None, user=None, partner=None, when=None):
        msg = self._receipt(amount, operation=operation, partner=partner, user=user, when=when)
        self._credit(msg, user=user, confirmed=True)
        return self._card(msg)

    image_seq = 0

    def _receipt(self, amount, **kwargs):
        """An image of its own: the shared seeds may match receipts other
        suites left in the copy."""
        TestV1297TransferenciasCargadas.image_seq += 1
        seq = self.image_seq
        buffer = io.BytesIO()
        Image.new("RGB", (40 + seq % 60, 25 + seq // 60 % 60), (seq % 256, 129, 7)).save(
            buffer, format="PNG")
        kwargs.setdefault("raw", buffer.getvalue())
        return super()._receipt(amount, **kwargs)

    def _rows(self, cards, domain=None):
        """The list rows of `cards` (the database holds other data). The
        rows share the cards' ids: what the test changed is read again."""
        self.Loaded.invalidate_model()
        return self.Loaded.search([("card_id", "in", cards.ids)] + list(domain or []))

    def _filter(self, name, uid=None):
        """The domain of the search view filter `name`, as the web client
        evaluates it (the Argentine day as context_today)."""
        view = self.env.ref("distri_pedido.view_loaded_transfer_search")
        node = etree.fromstring(view.arch).xpath("//filter[@name='%s']" % name)[0]
        return safe_eval(node.get("domain"), {
            "context_today": today_ar, "relativedelta": relativedelta,
            "uid": uid or self.env.uid})

    def _set_credited_at(self, card, moment):
        card.sudo().write({"credited_at": moment})

    # ------------------------------------------------------------ content

    def test_content_inclusion_and_exclusion(self):
        staff = self._credited(10100, user=self.seller)
        pending_msg = self._receipt(10200, user=self.seller)
        pending = self._card(pending_msg)
        customer_msg = self._receipt(10300, customer=True)
        customer = self._card(customer_msg)
        approved_msg = self._receipt(10400, customer=True, partner=self.other_partner)
        self._credit(approved_msg, user=self.admin_user, confirmed=True)
        approved = self._card(approved_msg)
        undone_msg = self._receipt(10500, customer=True)
        self._credit(undone_msg, user=self.admin_user, confirmed=True)
        undone = self._card(undone_msg)
        undone.sudo().action_undo()
        self.assertFalse(undone.credited_at)
        self._report(("971297000001", fields.Datetime.now() - timedelta(minutes=30), 10600, ""))
        income = self.Collection.sudo().search([("source_id", "=", "971297000001")])
        self.assertEqual(income.kind, "income")

        cards = staff | pending | customer | approved | undone | income
        rows = self._rows(cards)
        self.assertEqual(rows.card_id, staff | pending | approved)
        by_card = {r.card_id: r for r in rows}
        self.assertEqual(by_card[staff].load_state, "credited")
        self.assertEqual(by_card[staff].loaded_by_id, self.seller)
        self.assertEqual(by_card[staff].loaded_at, staff.credited_at)
        self.assertEqual(by_card[staff].payment_id, staff.payment_id)
        self.assertEqual(by_card[staff].partner_id, self.partner)
        self.assertEqual(by_card[staff].amount, 10100)
        self.assertEqual(by_card[staff].destination, "A cuenta")
        self.assertEqual(by_card[staff].audit_state, "pendiente")
        self.assertFalse(by_card[staff].without_receipt)
        self.assertTrue(by_card[staff].receipt_image)
        # the staff upload not credited yet: who uploaded it, when
        self.assertEqual(by_card[pending].load_state, "pending")
        self.assertEqual(by_card[pending].loaded_by_id, self.seller)
        self.assertEqual(by_card[pending].loaded_at, pending_msg.create_date)
        self.assertFalse(by_card[pending].audit_state)
        # a customer's receipt credited by administración: who credited it
        self.assertEqual(by_card[approved].loaded_by_id, self.admin_user)
        self.assertEqual(by_card[approved].partner_id, self.other_partner)

        # annulled in the audit: stays, with its state
        staff.sudo().write({"state": "ignored", "audit_state": "anulado"})
        row = self._rows(staff)
        self.assertEqual(row.audit_state, "anulado")
        self.assertTrue(row.with_anomaly, "an annulled payment counts as an anomaly")
        # a cancelled upload leaves
        pending.sudo().write({"state": "ignored"})
        self.assertFalse(self._rows(pending))

    def test_destination_invoice(self):
        invoice = self._invoice_v1297(20000)
        msg = self._receipt(20000, user=self.seller)
        card = self._card(msg)
        card.sudo().pay_move_id = invoice
        self._credit(msg, user=self.seller, confirmed=True)
        row = self._rows(card)
        self.assertEqual(row.destination_kind, "lab")
        self.assertEqual(row.destination, "Factura %s" % invoice.name)
        self.assertEqual(row.pay_move_id, invoice)

    def _invoice_v1297(self, amount):
        invoice = self.env["account.move"].create({
            "move_type": "out_invoice", "partner_id": self.partner.id,
            "invoice_date": fields.Date.today(),
            "invoice_line_ids": [Command.create({
                "name": "mercadería", "quantity": 1, "price_unit": amount,
                "tax_ids": [Command.clear()]})],
        })
        invoice.action_post()
        return invoice

    # ------------------------------------------------------------ ordering

    def test_newest_first(self):
        now = fields.Datetime.now()
        old = self._credited(11100)
        new = self._credited(11200)
        middle = self._credited(11300)
        self._set_credited_at(old, now - timedelta(hours=3))
        self._set_credited_at(new, now - timedelta(minutes=5))
        self._set_credited_at(middle, now - timedelta(hours=1))
        pending = self._card(self._receipt(11400, user=self.seller))
        self.env.cr.execute("UPDATE distri_whatsapp_message SET create_date = %s WHERE id = %s",
                            [now - timedelta(hours=2), pending.receipt_id.id])
        self.env.invalidate_all()
        rows = self._rows(old | new | middle | pending)
        self.assertEqual(rows.mapped("card_id"), new | middle | pending | old)
        self.assertEqual(rows.mapped("loaded_at"), sorted(rows.mapped("loaded_at"), reverse=True))

    # ------------------------------------------------------------ filters

    def test_user_and_date_filters(self):
        now = fields.Datetime.now()
        today = self._credited(12100, user=self.seller)
        yesterday = self._credited(12200, user=self.seller)
        old = self._credited(12300, user=self.cashier)
        self._set_credited_at(today, now)
        self._set_credited_at(yesterday, now - timedelta(days=1))
        self._set_credited_at(old, now - timedelta(days=40))
        cards = today | yesterday | old
        rows = {r.card_id: r for r in self._rows(cards)}
        self.assertEqual(rows[today].loaded_day, today_ar())
        self.assertEqual(rows[yesterday].loaded_day, today_ar() - timedelta(days=1))

        def found(domain):
            return self._rows(cards, domain).card_id

        # by user: the field and «Cargadas por mí»
        self.assertEqual(found([("loaded_by_id", "=", self.seller.id)]), today | yesterday)
        self.assertEqual(found(self._filter("mine", uid=self.cashier.id)), old)
        # today, yesterday, this week, this month
        self.assertEqual(found(self._filter("today")), today)
        self.assertEqual(found(self._filter("yesterday")), yesterday)
        monday = today_ar() - timedelta(days=today_ar().weekday())
        week = today | (yesterday if today_ar() - timedelta(days=1) >= monday else
                        self.Collection.browse())
        self.assertEqual(found(self._filter("this_week")), week)
        month = today | (yesterday if (today_ar() - timedelta(days=1)).day < today_ar().day
                         else self.Collection.browse())
        self.assertEqual(found(self._filter("this_month")), month)
        # a custom range («Desde el día», «Hasta el día»)
        start = today_ar() - timedelta(days=45)
        end = today_ar() - timedelta(days=1)
        self.assertEqual(found([("loaded_day", ">=", start), ("loaded_day", "<=", end)]),
                         yesterday | old)
        # by customer
        self.assertEqual(found([("partner_id", "=", self.partner.id)]), cards)
        self.assertEqual(found([("partner_id", "=", self.other_partner.id)]),
                         self.Collection.browse())

    def test_audit_state_matched_and_anomaly_filters(self):
        verified = self._credited(13100, operation="971297000101")
        missing = self._credited(13200, operation="971297000102")
        self._report(("971297000101", fields.Datetime.now() - timedelta(minutes=1), 13100, ""))
        self.assertEqual(verified.audit_state, "verificado")
        self.assertEqual(missing.audit_state, "sin_transferencia")
        pending = self._card(self._receipt(13300, user=self.seller))
        cards = verified | missing | pending

        def found(name):
            return self._rows(cards, self._filter(name)).card_id

        self.assertEqual(found("audit_verificado"), verified)
        self.assertEqual(found("audit_sin_transferencia"), missing)
        self.assertEqual(found("audit_pendiente"), self.Collection.browse())
        self.assertEqual(found("not_matched"), missing | pending)
        self.assertEqual(found("with_anomaly"), missing)
        self.assertEqual(found("credited"), verified | missing)
        self.assertEqual(found("pending_upload"), pending)
        rows = {r.card_id: r for r in self._rows(cards)}
        # crossed with the report: the income and its date, ✓ «Pago confirmado»
        self.assertEqual(rows[verified].income_id, verified.income_id)
        self.assertEqual(rows[verified].income_date, verified.income_id.date)
        self.assertTrue(rows[verified].matched)
        self.assertTrue(rows[verified].confirmed)
        self.assertEqual(rows[verified].confirmed_mark, "✓")
        self.assertFalse(rows[missing].confirmed)
        self.assertFalse(rows[missing].confirmed_mark)
        anomaly = self.env["distri.mp.anomaly"].sudo().search(
            [("card_id", "=", missing.id), ("kind", "=", "sin_transferencia")])
        self.assertEqual(rows[missing].anomaly_id, anomaly)
        # «sin comprobante»: the receipt is gone
        missing.sudo().receipt_id = False
        self.assertEqual(self._rows(missing).receipt_flag, "Sin comprobante")
        self.assertEqual(self._rows(cards, self._filter("without_receipt")).card_id, missing)

    def test_group_sums(self):
        self._credited(14100, user=self.seller)
        self._credited(14200, user=self.seller)
        self._credited(14300, user=self.cashier)
        cards = self.Collection.sudo().search([("amount", "in", (14100, 14200, 14300)),
                                               ("partner_id", "=", self.partner.id)])
        domain = [("card_id", "in", cards.ids)]
        groups = dict(self.Loaded._read_group(domain, ["loaded_by_id"], ["amount:sum"]))
        self.assertEqual(groups, {self.seller: 28300, self.cashier: 14300})
        by_day = self.Loaded._read_group(domain, ["loaded_day:day"], ["amount:sum", "__count"])
        self.assertEqual([(total, count) for _day, total, count in by_day], [(42600, 3)])
        by_state = dict(self.Loaded._read_group(domain, ["audit_state"], ["amount:sum"]))
        self.assertEqual(by_state, {"pendiente": 42600})
        # the list sums the amount (with the group totals)
        arch = etree.fromstring(self.env.ref("distri_pedido.view_loaded_transfer_list").arch)
        self.assertEqual(arch.xpath("//field[@name='amount']")[0].get("sum"), "Total")

    # ------------------------------------------------------------ access

    def test_access_only_audit_groups(self):
        card = self._credited(15100)
        for user in (self.seller, self.cashier):
            with self.assertRaises(AccessError):
                self.Loaded.with_user(user).search([("card_id", "=", card.id)])
            visible = self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()
            self.assertNotIn(self.env.ref(LIST_MENU).id, visible)
            self.assertNotIn(self.env.ref(AUDIT_MENU).id, visible)
        for user in (self.admin_user, self.manager, self.sysadmin):
            self.assertEqual(self.Loaded.with_user(user).search(
                [("card_id", "=", card.id)]).card_id, card)
            visible = self.env["ir.ui.menu"].with_user(user)._visible_menu_ids()
            self.assertIn(self.env.ref(LIST_MENU).id, visible)
        # read-only, for everybody
        row = self.Loaded.with_user(self.manager).search([("card_id", "=", card.id)])
        with self.assertRaises(AccessError):
            row.write({"amount": 1})
        # «Auditoría de cobros» opens a menu: the anomalies and this list
        menu = self.env.ref(AUDIT_MENU)
        self.assertFalse(menu.action)
        self.assertEqual(self.env.ref("distri_pedido.menu_cobros_auditoria_anomalias").action,
                         self.env.ref("distri_pedido.action_mp_anomaly"))
        self.assertIn(self.env.ref(LIST_MENU), menu.child_id)

    # ------------------------------------------------------------ links

    def test_links_and_daily_summary_entry_point(self):
        card = self._credited(16100, operation="971297000201", user=self.seller)
        self._report(("971297999999", fields.Datetime.now(), 1.23, ""))
        self.assertEqual(card.audit_state, "sin_transferencia")
        row = self.Loaded.with_user(self.manager).search([("card_id", "=", card.id)])
        action = row.action_view_card()
        self.assertEqual((action["res_model"], action["res_id"]), ("distri.mp.collection", card.id))
        action = row.action_view_payment()
        self.assertEqual((action["res_model"], action["res_id"]),
                         ("account.payment", card.payment_id.id))
        anomaly = self.env["distri.mp.anomaly"].sudo().search(
            [("card_id", "=", card.id), ("kind", "=", "sin_transferencia")])
        action = row.action_view_anomalies()
        self.assertEqual(action["res_model"], "distri.mp.anomaly")
        shown = [action["res_id"]] if action.get("res_id") else action["domain"][0][2]
        self.assertIn(anomaly.id, shown)
        action = row.action_view_statement()
        self.assertEqual(action["res_model"], "distri.estado.cuenta")
        wizard = self.env["distri.estado.cuenta"].browse(action["res_id"])
        self.assertEqual(wizard.partner_id, self.partner)
        with self.assertRaises(AccessError):
            row.with_user(self.seller).action_view_card()

        # «Resumen diario por persona» › «Ver transferencias»
        other = self._credited(16200, user=self.cashier)
        summary = self.env["distri.credit.summary"]._build(today_ar(), self.env.company)
        line = summary.line_ids.filtered(lambda l: l.user_id == self.seller)
        action = line.with_user(self.manager).action_view_transfers()
        self.assertEqual(action["res_model"], "distri.loaded.transfer")
        rows = self.Loaded.search(action["domain"])
        self.assertIn(card, rows.card_id)
        self.assertNotIn(other, rows.card_id)
        self.assertEqual(len(rows), line.count)
        self.assertEqual(sum(rows.mapped("amount")), line.amount)
        self.assertEqual(len(rows.filtered("with_anomaly")), line.anomaly_count)

    # ------------------------------------------------------------ speed

    def test_list_query_count_is_bounded(self):
        for i in range(8):
            # apart by more than the tolerance: two receipts of the same
            # amount and minute are copies; cents: no income of the copy
            self._credited(17000.37 + 100 * i, user=self.seller if i % 2 else self.cashier,
                           partner=self.partner if i % 3 else self.other_partner)
        cards = self.Collection.sudo().search([("amount", ">=", 17000), ("amount", "<", 17800),
                                               ("kind", "=", "verify")])
        self.assertEqual(len(cards), 8)
        names = {"fields": {"display_name": {}}}
        spec = {
            "loaded_at": {}, "loaded_by_id": names, "partner_id": names, "amount": {},
            "currency_id": names, "operation": {}, "destination": {}, "payment_id": names,
            "load_state": {}, "audit_state": {}, "receipt_flag": {}, "income_id": names,
            "income_date": {}, "confirmed_mark": {}, "with_anomaly": {}, "uploaded_by_id": names,
            "receipt_image": {},
        }
        Loaded = self.Loaded.with_user(self.manager).with_context(bin_size=True)
        domain = [("card_id", "in", cards.ids)]

        def count(limit):
            self.env.invalidate_all()
            self.env.flush_all()
            before = self.env.cr.sql_log_count
            result = Loaded.web_search_read(domain, spec, limit=limit)
            used = self.env.cr.sql_log_count - before
            self.assertEqual(len(result["records"]), limit)
            return used

        Loaded.web_search_read(domain, spec, limit=1)  # warm the rule and access caches
        few, many = count(2), count(8)
        self.assertEqual(few, many, "no query per row")
        self.assertLessEqual(many, 25)
