"""19.0.1.31.0: «Mayoristas apartados» and the uncluttered board.
Synthetic CUITs and names only."""
from datetime import timedelta

from odoo import Command, fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon

CUIT_REV = "20999000099"     # a revendedor's own payer
CUIT_REV2 = "27999000088"
CUIT_RETAIL = "20999000011"  # a retail customer's CUIT


@tagged("post_install", "-at_install", "distri_mayoristas")
class TestMayoristas(AccountTestInvoicingCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Rule = cls.env["distri.mp.wholesale.rule"]
        cls.retail = cls.env["res.partner"].create({
            "name": "Almacén Minorista Prueba", "vat": CUIT_RETAIL, "company_type": "company"})
        cls.seller = new_test_user(
            cls.env, login="vendedor_mayoristas", name="Vendedor Mayoristas",
            groups="base.group_user,distri_pedido.grupo_vendedor",
            company_id=cls.env.company.id, company_ids=[Command.set(cls.env.company.ids)])
        cls.seq = 0

    def _income(self, cuit="", name="", amount=1000.0, days_ago=1):
        type(self).seq += 1
        card = self.Collection.create({
            "kind": "income", "source_id": "99%010d" % self.seq, "amount": amount,
            "date": fields.Datetime.now() - timedelta(days=days_ago),
            "payer_cuit": cuit, "payer_name": name, "method_type": "bank_transfer",
            "transaction_type": "SETTLEMENT",
        })
        card._propose()
        return card

    def _mark(self, cards, remember=False, label=False):
        action = cards.action_mark_wholesale()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create(
            {"remember": remember, "label": label})
        wizard.action_apply()
        return wizard

    # ------------------------------------------------------------ marking

    def test_mark_one_payment_out_of_the_system(self):
        card = self._income(CUIT_REV, "PAGADOR DE REVENDEDOR")
        self.assertEqual(card.column, "unidentified")
        self._mark(card)
        self.assertEqual(card.state, "apartado")
        self.assertFalse(card.partner_id)
        self.assertEqual(card.wholesale_by_id, self.env.user)
        self.assertTrue(card.wholesale_at)
        self.assertFalse(card.payment_id)
        self.assertFalse(self.Rule.search([("cuit", "=", CUIT_REV)]), "not remembered")
        with self.assertRaises(UserError):
            card.action_confirm()
        # reversible
        card.action_unmark_wholesale()
        self.assertEqual(card.state, "draft")
        self.assertFalse(card.wholesale_by_id)

    def test_set_aside_payments_are_out_of_alerts_and_daily_close(self):
        card = self._income(CUIT_REV, amount=5000)
        other = self._income("", "OTRO PAGADOR", amount=700)
        self.env.cr.execute("UPDATE distri_mp_collection SET create_date = %s WHERE id IN %s",
                            (fields.Datetime.now() - timedelta(days=5), (card.id, other.id)))
        self.Collection.invalidate_model(["create_date"])
        self._mark(card)
        alerted = self.Collection._cron_alerts()
        self.assertNotIn(card, alerted)
        self.assertIn(other, alerted)
        day = self.env["distri.mp.day"].create({"date": card.date_day})
        day.invalidate_recordset()
        self.assertEqual(day.apartado_total, 5000)
        self.assertEqual(day.apartado_count, 1)
        self.assertEqual(day.difference, 700, "only the retail one is a difference")
        self.assertEqual(day.pending_count, 1)

    def test_remember_cuit_sets_aside_the_others_and_future_ones(self):
        first = self._income(CUIT_REV, amount=100)
        second = self._income(CUIT_REV, amount=200)
        self._mark(first, remember=True, label="Revendedor de ejemplo")
        rule = self.Rule.search([("cuit", "=", CUIT_REV)])
        self.assertEqual(rule.label, "Revendedor de ejemplo")
        self.assertFalse(rule.conflict)
        self.assertEqual(second.state, "apartado", "the other pending payment followed")
        self.assertTrue(second.wholesale_auto)
        self.assertEqual(second.wholesale_label, "Revendedor de ejemplo")
        # a future payment of that CUIT, on import
        later = self._income(CUIT_REV, amount=300)
        later._distri_apply_wholesale_rules()
        self.assertEqual(later.state, "apartado")
        self.assertEqual(rule.payment_count, 3)
        self.assertEqual(rule.payment_total, 600)

    def test_import_sets_aside_a_remembered_payer(self):
        # the fixture's 900000000003 is paid by 20999000029 (nobody's customer)
        self.Rule.create({"cuit": "20-99900002-9", "label": "Distribuidora de ejemplo"})
        # and 900000000001 by 20999000011, the retail customer's CUIT
        self.Rule.create({"cuit": CUIT_RETAIL, "label": "Mal cargado"})
        from odoo.addons.distri_pedido.tests.test_cobros import _fixture
        self.env["distri.mp.report"]._import_file("mp_report_25col.csv",
                                                  _fixture("mp_report_25col.csv"))
        out = self.Collection.search([("source_id", "=", "900000000003")])
        self.assertEqual(out.state, "apartado")
        self.assertTrue(out.wholesale_auto)
        self.assertEqual(out.wholesale_label, "Distribuidora de ejemplo")
        retail = self.Collection.search([("source_id", "=", "900000000001")])
        self.assertEqual(retail.partner_id, self.retail)
        self.assertNotEqual(retail.state, "apartado", "a customer's CUIT is never set aside")

    def test_a_retail_customer_cuit_is_never_set_aside_automatically(self):
        rule = self.Rule.create({"cuit": CUIT_RETAIL, "label": "Error de carga"})
        self.assertTrue(rule.conflict)
        self.assertIn("Almacén Minorista Prueba", rule.conflict_note)
        card = self._income(CUIT_RETAIL)
        self.assertEqual(card.partner_id, self.retail, "the customer link wins")
        card._distri_apply_wholesale_rules()
        rule._distri_apply()
        self.assertEqual(card.state, "draft")
        # by hand it can be set aside, and the wizard warns
        action = card.action_mark_wholesale()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({})
        self.assertIn(CUIT_RETAIL, wizard.conflict_note)

    def test_undo_rule_brings_its_payments_back(self):
        first = self._income(CUIT_REV2)
        second = self._income(CUIT_REV2)
        self._mark(first | second, remember=True)
        rule = self.Rule.search([("cuit", "=", CUIT_REV2)])
        rule.action_undo()
        self.assertFalse(rule.active)
        self.assertEqual((first | second).mapped("state"), ["draft", "draft"])
        later = self._income(CUIT_REV2)
        later._distri_apply_wholesale_rules()
        self.assertEqual(later.state, "draft", "an archived rule does nothing")

    def test_bank_transfer_without_cuit_is_marked_one_by_one(self):
        card = self._income("", "TITULAR SIN CUIT")
        wizard = self._mark(card, remember=True)
        self.assertEqual(wizard.without_cuit, 1)
        self.assertEqual(card.state, "apartado")
        self.assertFalse(self.Rule.search([("cuit", "in", ("", False))]))

    # ------------------------------------------------------------ board

    def test_unknown_payers_grouped_by_payer(self):
        for amount in (100, 200, 300):
            self._income(CUIT_REV2, "REVENDEDOR LEJANO", amount=amount)
        self._income("", "Titular Sin Cuit", amount=50)
        self._income("", "TITULAR SIN CUIT", amount=60)
        Bucket = self.env["distri.mp.payer.bucket"]
        row = Bucket.search([("payer_cuit", "=", CUIT_REV2)])
        self.assertEqual(len(row), 1)
        self.assertEqual(row.payment_count, 3)
        self.assertEqual(row.total, 600)
        self.assertTrue(row.last_date)
        by_name = Bucket.search([("payer_key", "=", "name:titular sin cuit")])
        self.assertEqual(by_name.payment_count, 2)
        self.assertEqual(len(by_name._payments()), 2)
        # «Es de mayorista» from the row takes all its payments
        action = row.action_mark_wholesale()
        wizard = self.env[action["res_model"]].with_context(action["context"]).create({})
        self.assertEqual(wizard.count, 3)

    def test_default_board_hides_unknown_and_set_aside(self):
        known = self._income(CUIT_RETAIL, amount=1000)
        # an odd amount: a real receipt of a round amount in the copy would pair
        unknown = self._income(CUIT_REV, amount=2017)
        apart = self._income(CUIT_REV2, amount=4000)
        self._mark(apart)
        self.assertEqual(known.column, "confirm")
        action = self.env.ref("distri_pedido.action_mp_board")
        self.assertIn("search_default_to_act", action.context)
        counters = self.Collection.distri_board_counters()
        self.assertGreaterEqual(counters["act"]["count"], 1)
        self.assertGreaterEqual(counters["unknown"]["count"], 1)
        self.assertGreaterEqual(counters["apartados"]["total"], 4000)
        # the «Para actuar» domain of the default filter
        to_act = ["&", ("column", "!=", "unidentified"), "|", "|", ("state", "=", "draft"),
                  ("report_review", "=", True), "&", ("state", "=", "authorized"),
                  ("income_id", "=", False)]
        shown = self.Collection.search(to_act)
        self.assertIn(known, shown)
        self.assertNotIn(unknown, shown)
        self.assertNotIn(apart, shown)
        # «Asignar cliente» only appears as a column when something is in it
        groups = self.Collection.formatted_read_group(
            to_act + [("id", "=", known.id)], ["column"], ["__count"])
        self.assertNotIn("unidentified", [g["column"] for g in groups])

    def test_only_administration_uses_it(self):
        card = self._income(CUIT_REV)
        with self.assertRaises(UserError):
            card.with_user(self.seller).action_mark_wholesale()
        with self.assertRaises(UserError):
            self.Collection.with_user(self.seller).distri_board_counters()
        with self.assertRaises(AccessError):
            self.Rule.with_user(self.seller).search([])
