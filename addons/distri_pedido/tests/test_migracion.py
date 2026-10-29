"""19.0.1.41.0: migration board, receipt viewer and new-tab buttons
(models/migracion.py). Every customer, amount and operation here is
fictitious."""
from datetime import date, datetime, timedelta

from lxml import etree

from odoo import Command, fields
from odoo.exceptions import UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.migracion import (
    PARAM_CUTOFF, account_url, names_match, online_sync_state,
)
from odoo.addons.distri_pedido.models.puesta_al_dia import PARAM_ONLINE_SYNC

WHEN = datetime(2026, 10, 20, 15, 0)


@tagged("post_install", "-at_install", "distri_migracion")
class TestMigracion(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        Partner = cls.env["res.partner"]
        cls.alpha = Partner.create({"name": "Almacén Migración Alfa", "company_type": "company",
                                    "ref": "ODOO-980001"})
        cls.beta = Partner.create({"name": "Kiosco Migración Beta", "company_type": "company",
                                   "ref": "ODOO-980002"})
        cls.env["distri.payer.identity"].create({"kind": "cuit", "value": "20980000011",
                                                 "partner_id": cls.alpha.id})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Mov = cls.env["distri.cuenta.movimiento"]
        cls.params = cls.env["ir.config_parameter"].sudo()
        cls.params.set_param(PARAM_CUTOFF, "2026-09-15")
        # both customers owe enough: no approval leaves them in credit
        for partner in (cls.alpha, cls.beta):
            cls.Mov.create({"partner_id": partner.id, "fecha": date(2026, 8, 1),
                            "comprobante": "0001-M%s" % partner.id, "tipo": "factura",
                            "debe": 100000, "haber": 0})

    def setUp(self):
        super().setUp()
        self._synced(hours=1)

    # ------------------------------------------------------------ helpers

    def _synced(self, hours):
        self.params.set_param(PARAM_ONLINE_SYNC, fields.Datetime.to_string(
            fields.Datetime.now() - timedelta(hours=hours)))

    def _card(self, source, amount=1000, when=WHEN, partner=None, cuit="20980000011",
              proposal="cuit"):
        return self.Collection.create({
            "source_id": source, "date": when, "amount": amount, "method_type": "bank_transfer",
            "payer_cuit": cuit or False, "partner_id": partner.id if partner else False,
            "proposal": proposal if partner else "none"})

    def _receipt(self, card, operation=None, amount=None, partner=None,
                 source="Nota de Vendedor Prueba junto a la foto", stamp=None, body="",
                 sender="Vendedor Prueba"):
        msg = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.migracion.%s.%s" % (card.id, operation or "x"),
            "direction": "in", "wa_id": "5492990000999", "message_type": "image",
            "profile_name": sender, "body": body, "timestamp": card.date + timedelta(minutes=1),
            "media_mime_type": "image/jpeg", "media_id": "1", "media_state": "pending"})
        msg.with_context(distri_receipt_matching=True).write({
            "distri_is_receipt": True,
            "distri_operation": operation if operation is not None else card.source_id,
            "distri_amount": amount if amount is not None else card.amount,
            "distri_receipt_date": stamp or card.date,
            "distri_receipt_state": "matched",
            "distri_collection_id": card.id,
            "distri_partner_id": partner.id if partner else False,
            "distri_partner_source": source if partner else False,
        })
        card.sudo().write({"receipt_id": msg.id})
        card._migration_recompute()
        return msg

    def _approve(self, cards, mode="single"):
        action = self.env["distri.migration.approve"]._open(cards, mode=mode)
        return self.env["distri.migration.approve"].browse(action["res_id"])

    # ------------------------------------------------------------ boxes

    def test_boxes_follow_the_evidence(self):
        ready = self._card("970000000001", partner=self.alpha)
        self._receipt(ready, partner=self.alpha)
        self.assertEqual(ready.money_grade, "A")
        self.assertEqual(ready.customer_agreement, "agree")
        self.assertIn("CUIT del reporte", ready.customer_sources)
        self.assertIn("Nota del vendedor", ready.customer_sources)
        self.assertEqual(ready.migration_box, "to_approve")
        self.assertTrue(ready.migration_in_period)

        # the operation number proves the transfer, not the customer: one
        # source only is a review
        single = self._card("970000000002", partner=self.alpha)
        self._receipt(single)
        self.assertEqual(single.money_grade, "A")
        self.assertEqual(single.customer_agreement, "single")
        self.assertEqual(single.migration_box, "review")

        # the photo names another customer
        other = self._card("970000000003", partner=self.alpha)
        self._receipt(other, partner=self.beta)
        self.assertEqual(other.customer_agreement, "disagree")
        self.assertEqual(other.migration_box, "review")

        # same amount, stamp 1 minute away, no operation read: B
        close = self._card("970000000004", partner=self.alpha)
        self._receipt(close, operation="", partner=self.alpha,
                      stamp=close.date + timedelta(minutes=1))
        self.assertEqual(close.money_grade, "B")
        self.assertEqual(close.migration_box, "to_approve")
        # another amount: D, never approvable
        far = self._card("970000000005", partner=self.alpha)
        self._receipt(far, operation="", amount=1500, partner=self.alpha)
        self.assertEqual(far.money_grade, "D")
        self.assertEqual(far.migration_box, "review")

        # no operation, no stamp, but the payer read on the receipt is MP's
        named = self._card("970000000009", partner=self.alpha)
        named.sudo().payer_name = "PEREZ JUAN CARLOS"
        self._receipt(named, operation="", partner=self.alpha, stamp=named.date + timedelta(hours=5))
        named.receipt_id.with_context(distri_receipt_matching=True).distri_payer_name = "Juan Carlos Pérez"
        named._migration_recompute()
        self.assertEqual(named.money_grade, "N")
        self.assertEqual(named.migration_box, "to_approve")
        self.assertFalse(names_match("Juan Gómez", "PEREZ JUAN CARLOS"))

        # a receipt and no customer at all
        nobody = self._card("970000000006", cuit="")
        self._receipt(nobody)
        self.assertEqual(nobody.customer_agreement, "none")
        self.assertEqual(nobody.migration_box, "no_customer")

        # MP movement without receipt, even with a known customer
        bare = self._card("970000000007", partner=self.alpha)
        self.assertEqual(bare.money_grade, "none")
        self.assertEqual(bare.migration_box, "missing_receipt")
        self.assertIn("comprobante", bare.receipt_request_text)

        # a WhatsApp receipt without movement: waiting, never a payment
        waiting = self.Collection.create({"kind": "receipt", "date": WHEN, "amount": 900})
        self.assertEqual(waiting.migration_box, "waiting_money")

        old = self._card("970000000008", partner=self.alpha, when=datetime(2026, 9, 1, 12, 0))
        self._receipt(old, partner=self.alpha)
        self.assertFalse(old.migration_in_period)

        ready.action_confirm()
        self.assertEqual(ready.migration_box, "confirmed")

    def test_duplicates_block_both_cards(self):
        first = self._card("970000000101", partner=self.alpha)
        second = self._card("970000000102", partner=self.alpha)
        self._receipt(first, operation="970000000555", partner=self.alpha)
        self._receipt(second, operation="970000000555", partner=self.alpha)
        (first + second)._migration_recompute()
        for card in first + second:
            self.assertTrue(card.migration_duplicate)
            self.assertEqual(card.migration_box, "duplicate")
        wizard = self._approve(first + second)
        self.assertEqual(wizard.ok_count, 0)
        self.assertTrue(all("duplicado" in p for p in wizard.line_ids.mapped("problem")))

        # a photo whose operation is another movement: both sides
        target = self._card("970000000103", partner=self.alpha)
        claims = self._card("970000000104", partner=self.alpha)
        self._receipt(target, partner=self.alpha)
        self._receipt(claims, operation="970000000103", partner=self.alpha)
        (target + claims)._migration_recompute()
        self.assertTrue(claims.migration_duplicate)
        self.assertTrue(target.migration_duplicate)
        with self.assertRaises(UserError):
            self._review_line(target).action_confirm_card()

        # once one is discarded, the other is free again
        claims.sudo().write({"state": "ignored", "ignore_reason": "receipt_error"})
        target._migration_recompute()
        self.assertFalse(target.migration_duplicate)
        self.assertEqual(target.migration_box, "to_approve")

    # ------------------------------------------------------------ the hard rule

    def test_movement_without_receipt_never_reduces_the_debt(self):
        with_photo = self._card("970000000201", amount=3000, partner=self.alpha)
        self._receipt(with_photo, partner=self.alpha)
        bare = self._card("970000000202", amount=5000, partner=self.alpha,
                          when=WHEN + timedelta(hours=1))
        review = self._review(self.alpha)
        photo_line, bare_line = self._review_line(with_photo), self._review_line(bare)
        self.assertTrue(photo_line.applied)
        self.assertFalse(bare_line.applied)
        self.assertEqual(photo_line.balance_after, 97000)
        self.assertEqual(bare_line.balance_after, 97000, "the balance stays")
        self.assertEqual(review.final_balance, 97000)
        self.assertEqual(review.no_receipt_amount, 5000)
        self.assertEqual(review.whatif_balance, 92000)
        # not by the review, not by one approval, not by a batch
        with self.assertRaises(UserError):
            bare_line.action_confirm_card()
        wizard = self._approve(bare)
        self.assertEqual(wizard.ok_count, 0)
        self.assertIn("sin comprobante", wizard.line_ids.problem)
        wizard.action_approve()
        summary = self.env["distri.migration.summary"].create({})
        batch = self.env["distri.migration.approve"].browse(
            summary.action_batch_approve()["res_id"])
        self.assertNotIn(bare, batch.line_ids.mapped("collection_id"))
        self.assertEqual(bare.state, "draft")
        self.assertFalse(bare.payment_id)
        # the migration board only lists receipt cards
        board = self.Collection.search(summary._period_domain())
        self.assertNotIn(bare, board)
        self.assertIn(with_photo, board)
        # the receipt-backed card is confirmed by the review, the board's way
        photo_line.action_confirm_card()
        self.assertEqual(with_photo.state, "confirmed")
        self.assertTrue(with_photo.payment_id)

    # ------------------------------------------------------------ guards

    def test_batch_needs_a_fresh_sync_and_the_top_box(self):
        ready = self._card("970000000301", partner=self.alpha)
        self._receipt(ready, partner=self.alpha)
        weak = self._card("970000000302", partner=self.alpha)
        self._receipt(weak)
        self._synced(hours=30)
        self.assertTrue(online_sync_state(self.env)["stale"])
        stale = self._approve(ready + weak, mode="batch")
        self.assertEqual(stale.ok_count, 0)
        self.assertTrue(stale.sync_stale)
        stale.action_approve()
        self.assertEqual(ready.state, "draft")

        self._synced(hours=1)
        wizard = self._approve(ready + weak, mode="batch")
        self.assertEqual(wizard.ok_count, 1)
        blocked = wizard.line_ids.filtered("problem")
        self.assertEqual(blocked.collection_id, weak)
        self.assertIn("Listo para confirmar", blocked.problem)
        wizard.action_approve()
        self.assertEqual(ready.state, "confirmed")
        self.assertTrue(ready.payment_id)
        self.assertEqual(weak.state, "draft")
        self.assertTrue(ready.message_ids.filtered(
            lambda m: "tablero de migración" in (m.body or "")))
        # a single approval of the review card is a person's decision
        single = self._approve(weak)
        self.assertEqual(single.ok_count, 1)

    def test_batch_skips_a_credit_balance_and_old_cards(self):
        Partner = self.env["res.partner"]
        fresh = Partner.create({"name": "Despensa Migración Sin Deuda",
                                "company_type": "company"})
        self.env["distri.payer.identity"].create({"kind": "cuit", "value": "20980000022",
                                                 "partner_id": fresh.id})
        credit = self._card("970000000401", partner=fresh, cuit="20980000022")
        self._receipt(credit, partner=fresh)
        old = self._card("970000000402", partner=self.alpha, when=datetime(2026, 9, 1, 12, 0))
        self._receipt(old, partner=self.alpha)
        self.assertEqual(credit.migration_box, "to_approve")
        wizard = self._approve(credit + old, mode="batch")
        self.assertEqual(wizard.ok_count, 0)
        problems = dict(zip(wizard.line_ids.mapped("collection_id"),
                            wizard.line_ids.mapped("problem")))
        self.assertIn("saldo a favor", problems[credit])
        self.assertIn("fecha de corte", problems[old])
        # one by one the person sees the warning and decides
        single = self._approve(credit)
        self.assertEqual(single.ok_count, 1)
        self.assertIn("saldo a favor", single.line_ids.warning)

    # ------------------------------------------------------------ Online

    def test_sync_is_recorded_and_its_links_are_listed(self):
        self.params.set_param(PARAM_ONLINE_SYNC, "")
        card = self._card("970000000501", amount=4321, partner=self.alpha)
        result = self.env["distri.online.payment"]._distri_load([{
            "online_id": 980501, "name": "PBNK1/2026/98501", "move_name": "PBNK1/2026/98501",
            "date": date(2026, 10, 20), "amount": 4321, "partner_id": 980001,
            "journal_id": 13, "journal_name": "Banco", "memo": "", "state": "paid"}])
        self.assertEqual(result["by_amount"], 1)
        sync = online_sync_state(self.env)
        self.assertFalse(sync["stale"])
        self.assertIn("Última sincronización con Online", sync["label"])
        self.assertEqual(card.online_payment_id.linked_at, sync["at"])
        self.assertEqual(card.migration_box, "online")
        summary = self.env["distri.migration.summary"].create({})
        self.assertEqual(summary.links_count, 1)
        listed = self.Collection.search(summary.action_open_links()["domain"])
        self.assertEqual(listed, card)
        review = self.env["distri.balance.review"].new({})
        self.assertIn("Última sincronización", review.online_sync_label)

    # ------------------------------------------------------------ viewer and tabs

    def test_receipt_viewer_shows_the_whatsapp_message(self):
        card = self._card("970000000601", partner=self.alpha)
        msg = self._receipt(card, partner=self.alpha, body="Pago de Almacén Alfa",
                            sender="Vendedor Visor")
        self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.migracion.nota", "direction": "in",
            "wa_id": msg.wa_id, "message_type": "text", "profile_name": "Vendedor Visor",
            "body": "es del almacén de la esquina", "timestamp": msg.timestamp + timedelta(
                minutes=5)})
        html = str(card.receipt_context_html)
        self.assertIn("Vendedor Visor", html)
        self.assertIn("Pago de Almacén Alfa", html)
        self.assertIn("es del almacén de la esquina", html)
        action = card.action_receipt_viewer()
        self.assertEqual(action["target"], "new")
        self.assertEqual(action["views"][0][0],
                         self.env.ref("distri_pedido.view_mp_collection_receipt_viewer").id)
        line = self._review_line(card)
        self.assertEqual(line.action_receipt_viewer()["res_id"], card.id)

    def test_receipt_viewer_lets_type_only_the_amount(self):
        view = self.env.ref("distri_pedido.view_mp_collection_receipt_viewer")
        arch = etree.fromstring(self.env["distri.mp.collection"].get_view(view.id, "form")["arch"])
        self.assertNotEqual(arch.get("edit"), "0")
        editable = [node.get("name") for node in arch.iter("field")
                    if node.get("readonly") != "1" and node.get("invisible") != "1"]
        self.assertEqual(editable, ["receipt_amount"])
        self.assertEqual(arch.xpath("//field[@name='receipt_amount']")[0].get("readonly"),
                         "state != 'draft' or not receipt_id")

    def test_buttons_open_a_new_tab(self):
        card = self._card("970000000701", partner=self.alpha)
        self._receipt(card, partner=self.alpha)
        action = card.action_tab_card()
        self.assertEqual(action, {"type": "ir.actions.act_url", "target": "new",
                                  "url": "/odoo/distri.mp.collection/%s" % card.id})
        self.assertEqual(card.action_tab_account()["url"],
                         "/odoo/%s/action-distri_pedido.action_partner_account_tab" % self.alpha.id)
        self.assertEqual(account_url(self.alpha), card.action_tab_account()["url"])
        with self.assertRaises(UserError):
            card.action_tab_payment()
        with self.assertRaises(UserError):
            card.action_tab_receipt()  # no file attached in the test
        line = self._review_line(card)
        tab = line.action_view_card()
        self.assertEqual((tab["type"], tab["target"]), ("ir.actions.act_url", "new"))
        self.assertEqual(tab["url"], "/odoo/distri.mp.collection/%s" % card.id)
        review = line.review_id
        tab = review.action_view_account()
        self.assertEqual((tab["type"], tab["target"]), ("ir.actions.act_url", "new"))
        line.action_confirm_card()
        line = self._review_line(card)
        tab = line.action_view_payment()
        self.assertEqual(tab["url"], "/odoo/account.payment/%s" % card.payment_id.id)
        self.assertEqual(tab["target"], "new")
        # the server action behind «Cuenta corriente» opens that customer's account
        server = self.env.ref("distri_pedido.action_partner_account_tab")
        result = server.with_context(active_model="res.partner", active_id=self.alpha.id,
                                     active_ids=[self.alpha.id]).run()
        self.assertEqual(result["res_model"], "distri.cuenta.movimiento")
        self.assertIn(("partner_id", "=", self.alpha.id), result["domain"])

    # ------------------------------------------------------------ scope

    def test_review_covers_all_retail_debt_and_skips_wholesalers(self):
        Partner = self.env["res.partner"]
        quiet = Partner.create({"name": "Almacén Migración Sin Pagos", "company_type": "company"})
        wholesale = Partner.create({"name": "Mayorista Migración", "company_type": "company"})
        for partner in (quiet, wholesale):
            self.Mov.create({"partner_id": partner.id, "fecha": date(2026, 8, 1),
                             "comprobante": "0001-Q%s" % partner.id, "tipo": "factura",
                             "debe": 7000, "haber": 0})
        wholesale.distri_es_mayorista = True
        Review = self.env["distri.balance.review"]
        Review._distri_refresh()
        review = Review.search([("partner_id", "=", quiet.id)])
        self.assertTrue(review, "debt in Online and nothing new: still reviewed")
        self.assertEqual(review.lab_balance, 7000)
        self.assertEqual(review.state, "pending")
        self.assertFalse(Review.search([("partner_id", "=", wholesale.id)]))
        review.action_mark_customer_reviewed()
        Review._distri_refresh(quiet)
        self.assertEqual(review.state, "reviewed")
        # a wholesaler's card is never approved from the migration board
        self.env["distri.payer.identity"].create({"kind": "cuit", "value": "20980000033",
                                                 "partner_id": wholesale.id})
        card = self._card("970000000801", partner=wholesale, cuit="20980000033")
        self._receipt(card, partner=wholesale)
        wizard = self._approve(card)
        self.assertEqual(wizard.ok_count, 0)
        self.assertIn("revendedor", wizard.line_ids.problem)

    # ------------------------------------------------------------ helpers (review)

    def _review(self, partner):
        Review = self.env["distri.balance.review"]
        Review._distri_refresh(partner)
        return Review.search([("partner_id", "=", partner.id)])

    def _review_line(self, card):
        review = self._review(card.partner_id)
        return review.line_ids.filtered(lambda l: l.collection_id == card)
