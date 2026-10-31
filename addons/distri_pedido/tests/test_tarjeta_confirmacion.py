"""19.0.1.63.0: confirmation card of the migration board
(models/tarjeta_confirmacion.py, views/tarjeta_confirmacion.xml). Every
customer, amount and operation here is fictitious."""
from datetime import date, datetime, timedelta

from lxml import etree
from psycopg2 import IntegrityError

from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import tagged
from odoo.tools import mute_logger

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.estado_cuenta import _money

WHEN = datetime(2026, 10, 20, 13, 40)


@tagged("post_install", "-at_install", "distri_tarjeta")
class TestTarjetaConfirmacion(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        Partner = cls.env["res.partner"]
        cls.alpha = Partner.create({"name": "Almacén Tarjeta Alfa", "company_type": "company"})
        cls.beta = Partner.create({"name": "Kiosco Tarjeta Beta", "company_type": "company"})
        cls.Collection = cls.env["distri.mp.collection"]
        Mov = cls.env["distri.cuenta.movimiento"]
        cls.old_invoice = Mov.create({
            "partner_id": cls.alpha.id, "fecha": date(2026, 9, 1), "tipo": "factura",
            "comprobante": "0001/2026/90001", "debe": 30000, "haber": 0})
        cls.new_invoice = Mov.create({
            "partner_id": cls.alpha.id, "fecha": date(2026, 10, 15), "tipo": "factura",
            "comprobante": "0001/2026/90002", "debe": 20000, "haber": 0})
        cls.cash = Mov.create({
            "partner_id": cls.alpha.id, "fecha": date(2026, 10, 1), "tipo": "pago",
            "comprobante": "PCSH1/2026/90001", "debe": 0, "haber": 5000})

    def _card(self, source, amount=12000, partner=None, **extra):
        return self.Collection.create(dict({
            "source_id": source, "date": WHEN, "amount": amount, "method_type": "bank_transfer",
            "method": "cvu_transfer", "payer_name": "PEREZ JUAN CARLOS",
            "payer_cuit": "20980000044", "partner_id": partner.id if partner else False,
            "proposal": "manual" if partner else "none"}, **extra))

    def _receipt(self, card, operation=None, amount=None):
        msg = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.tarjeta.%s" % card.id, "direction": "in",
            "wa_id": "5492990000777", "message_type": "image", "profile_name": "Vendedor Tarjeta",
            "timestamp": card.date + timedelta(minutes=1), "media_mime_type": "image/jpeg",
            "media_id": "1", "media_state": "pending"})
        msg.with_context(distri_receipt_matching=True).write({
            "distri_is_receipt": True,
            "distri_operation": operation if operation is not None else card.source_id,
            "distri_amount": amount if amount is not None else card.amount,
            "distri_payer_name": "Juan Carlos Perez",
            "distri_payer_cuit": "20-98000004-4",
            "distri_receipt_date": card.date,
            "distri_receipt_state": "matched",
            "distri_collection_id": card.id,
        })
        card.sudo().write({"receipt_id": msg.id})
        return msg

    # ------------------------------------------------------------ balance column

    def test_balance_column_shows_newest_first_and_the_balance_after(self):
        card = self._card("960000000001", amount=12000, partner=self.alpha)
        balance = self.alpha._distri_statement()["balance"]
        self.assertEqual(balance, 45000)
        html = str(card.card_balance_html)
        self.assertIn("Saldo actual", html)
        self.assertIn(_money(45000), html)
        self.assertIn("Si se aprueba", html)
        self.assertIn(_money(45000 - 12000), html)
        self.assertIn("Paga: <b>A cuenta</b>", html)
        # newest first: the October invoice before the September one
        self.assertLess(html.index("0001/2026/90002"), html.index("0001/2026/90001"))

        # more than the debt: the customer would be left in credit
        big = self._card("960000000002", amount=60000, partner=self.alpha)
        self.assertIn("queda saldo a favor", str(big.card_balance_html))

        # no customer and no suggestion: said, not hidden
        lost = self._card("960000000003")
        self.assertIn("Sin cliente propuesto", str(lost.card_balance_html))

        # confirmed: the money is already in the balance
        card.action_confirm()
        html = str(card.card_balance_html)
        self.assertIn("ya está en el saldo", html)
        self.assertNotIn("Si se aprueba", html)

    # ------------------------------------------------------------ invoice choice

    def test_chosen_history_invoice_is_cited_in_the_memo(self):
        card = self._card("960000000011", amount=20000, partner=self.alpha)
        self.assertEqual(card.pay_invoice_label, "A cuenta")
        action = card.action_choose_invoice()
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["views"][0][0],
                         self.env.ref("distri_pedido.view_mp_collection_pay_invoice").id)
        card.write({"pay_invoice_id": self.new_invoice.id})
        self.assertIn("0001/2026/90002", card.pay_invoice_label)
        html = str(card.card_balance_html)
        self.assertIn("paga esta", html)
        card.action_confirm()
        memo = card.payment_id.memo
        self.assertTrue(memo.startswith("Mercado Pago 960000000011"), memo)
        self.assertTrue(memo.endswith(" · 0001/2026/90002"), memo)
        # chosen before confirming only
        with self.assertRaises(UserError):
            card.write({"pay_invoice_id": self.old_invoice.id})

    def test_on_account_is_the_default_and_nothing_is_cited(self):
        card = self._card("960000000021", amount=1000, partner=self.alpha)
        card.write({"pay_invoice_id": self.old_invoice.id})
        card.action_pay_on_account()
        self.assertFalse(card.pay_invoice_id)
        card.action_confirm()
        self.assertEqual(card.payment_id.memo, "Mercado Pago 960000000021 · Perez Juan Carlos")

    def test_another_customer_clears_the_choice(self):
        card = self._card("960000000031", partner=self.alpha)
        card.write({"pay_invoice_id": self.old_invoice.id})
        card.write({"partner_id": self.beta.id})
        self.assertFalse(card.pay_invoice_id)
        with self.assertRaises(UserError):
            self._card("960000000032").action_choose_invoice()  # no customer yet

    def test_one_invoice_at_a_time(self):
        lab = self.init_invoice("out_invoice", partner=self.alpha, amounts=[100],
                                invoice_date=date(2026, 10, 1), post=True)
        card = self._card("960000000041", partner=self.alpha)
        self.assertTrue(card.pay_has_lab_invoices)
        card.write({"pay_invoice_id": self.old_invoice.id})
        card.write({"pay_move_id": lab.id})
        self.assertFalse(card.pay_invoice_id, "choosing the lab invoice clears the other")
        with self.assertRaises(IntegrityError), mute_logger("odoo.sql_db"), \
                self.env.cr.savepoint():
            card.write({"pay_invoice_id": self.old_invoice.id, "pay_move_id": lab.id})
            card.flush_recordset()

    def test_chosen_lab_invoice_is_reconciled_first(self):
        older = self.init_invoice("out_invoice", partner=self.alpha, amounts=[1000],
                                  invoice_date=date(2026, 8, 1), post=True)
        newer = self.init_invoice("out_invoice", partner=self.alpha, amounts=[500],
                                  invoice_date=date(2026, 10, 1), post=True)
        card = self._card("960000000051", amount=newer.amount_total, partner=self.alpha)
        card.write({"pay_move_id": newer.id})
        card.action_confirm()
        self.assertIn(newer.name, card.payment_id.memo)
        self.assertIn(newer.payment_state, ("paid", "in_payment"))
        self.assertEqual(older.payment_state, "not_paid")

        # «a cuenta»: the oldest first, as always
        other = self._card("960000000052", amount=100, partner=self.alpha)
        other.action_confirm()
        self.assertEqual(older.payment_state, "partial")

    def test_authorized_delivery_hands_its_choice_to_the_income(self):
        income = self._card("960000000061", amount=20000, partner=self.alpha,
                            proposal="authorized")
        authorized = self.Collection.create({
            "kind": "verify", "date": WHEN, "amount": 20000, "operation": "960000000061",
            "partner_id": self.alpha.id, "state": "authorized", "income_id": income.id})
        authorized.write({"pay_invoice_id": self.new_invoice.id})
        self.assertIn("0001/2026/90002", income.pay_invoice_label)
        income.action_confirm()
        self.assertTrue(income.payment_id.memo.endswith(" · 0001/2026/90002"))
        self.assertIn("ya está en el saldo", str(authorized.card_balance_html))

    def test_invoice_label_in_the_choice(self):
        label = self.new_invoice.with_context(distri_invoice_label=True).display_name
        self.assertEqual(label, "0001/2026/90002 · 15/10/2026 · %s" % _money(20000))
        self.assertEqual(self.new_invoice.display_name, "0001/2026/90002")

    # ------------------------------------------------------------ Mercado Pago column

    def test_report_next_to_the_photo(self):
        card = self._card("960000000071", amount=12000, partner=self.alpha)
        self._receipt(card)
        html = str(card.card_mp_html)
        order = ["Fecha y hora", "Importe", "De", "CUIT / CUIL", "Para",
                 "Operación / N° de transacción", "Medio"]
        positions = [html.index(">%s<" % label) for label in order]
        self.assertEqual(positions, sorted(positions), "the order of a transfer receipt")
        self.assertIn("20/10/2026 10:40", html)  # 13:40 UTC in Argentina
        self.assertIn("20-98000004-4", html)
        self.assertGreaterEqual(html.count("✓ coincide"), 4)  # amount, name, CUIT, operation
        self.assertNotIn("≠ distinto", html)

        other = self._card("960000000072", amount=12000, partner=self.alpha)
        self._receipt(other, amount=11000)
        self.assertIn("≠ distinto", str(other.card_mp_html))

        waiting = self.Collection.create({"kind": "verify", "date": WHEN, "amount": 500})
        html = str(waiting.card_mp_html)
        self.assertIn("Esperando reporte", html)
        self.assertIn("Sin comprobante de WhatsApp", html)
        photo_only = self.Collection.create({"kind": "receipt", "date": WHEN, "amount": 500})
        self.assertIn("Sin movimiento en el reporte", str(photo_only.card_mp_html))

    # ------------------------------------------------------------ the view

    def test_migration_board_opens_the_card_layout(self):
        view = self.env.ref("distri_pedido.view_mp_collection_form_migration")
        action = self.env.ref("distri_pedido.action_migration_board")
        forms = action.view_ids.filtered(lambda v: v.view_mode == "form")
        self.assertEqual(forms.view_id, view)
        arch = etree.fromstring(self.Collection.get_view(view.id, "form")["arch"])
        sheet = arch.find("sheet")
        # the chatter at the bottom of the sheet, not on the side
        self.assertEqual(sheet[-1].tag, "chatter")
        self.assertFalse(arch.xpath("/form/chatter"))
        self.assertEqual(len(arch.xpath("//section[contains(concat(' ', @class, ' '), ' o_distri_card_col ')]")), 3)
        self.assertTrue(arch.xpath("//widget[@name='distri_card_chat']"))
        for name in ("partner_id", "proposal", "proposal_note", "column"):
            self.assertTrue(arch.xpath(
                "//div[@class='o_distri_card_top']//field[@name='%s']" % name), name)
        # the header keeps every button of the collections form and its extensions
        for name in ("action_confirm_board", "action_assign", "action_ignore", "action_tab_chat",
                     "action_online_unlink", "action_unrecorded_sale"):
            self.assertTrue(arch.xpath("//header/button[@name='%s']" % name), name)
        # the other boards keep the collections form
        self.assertEqual(self.Collection.get_view(view_type="form")["id"],
                         self.env.ref("distri_pedido.view_mp_collection_form").id)
        dialog = self.env.ref("distri_pedido.view_mp_collection_pay_invoice")
        arch = etree.fromstring(self.Collection.get_view(dialog.id, "form")["arch"])
        editable = sorted(node.get("name") for node in arch.iter("field")
                          if node.get("readonly") != "1" and node.get("invisible") != "1")
        self.assertEqual(editable, ["pay_invoice_id", "pay_move_id"])
