"""19.0.1.129.39, behind distri_pedido.verificacion_v2:
- finding 8 of the review: the 48 h To-Do of a delivery «a confirmar» goes
  by login (distri_pedido.a_confirmar_logins, default «admin,denis»), not
  to whoever joins administración;
- finding 9: queue 7's «Comprobante de otro cliente» with two searches for
  all the rows (same answers as one income at a time);
- owner, 25/11: proposed ties of credited receipts and report incomes
  (models/verificacion_cruces.py): a unique pair of the same amount, same
  day or the day before, is proposed in queue 5 with «Cruzar», which runs
  the standard tie (no new payment); two candidates are not proposed; a
  round amount is proposed (a person clicks); an income set aside as a
  wholesaler's shows a warning and no «Cruzar»; with the flag off nothing.
Every test compares the ledger. Every customer, amount and operation here is
fictitious."""
import re
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import AccessError, UserError
from odoo.tests import tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.models import verificacion_cruces as vc
from odoo.addons.distri_pedido.models import verificacion_tramo2 as v2

from . import test_v129_38_verificacion as t38

_flag = t38._flag
_ledger = t38._ledger


class _BoardBase(t38.TestV12938Board):
    """The 129.38 board setup and helpers, without its tests."""


for _name in [n for n in dir(t38.TestV12938Board)
              if n.startswith("test_") and callable(getattr(t38.TestV12938Board, n))]:
    setattr(_BoardBase, _name, None)


class _ToConfirmBase(t38.TestV12938ToConfirm):
    """The 129.38 «a confirmar» setup and helpers, without its tests."""


for _name in [n for n in dir(t38.TestV12938ToConfirm)
              if n.startswith("test_") and callable(getattr(t38.TestV12938ToConfirm, n))]:
    setattr(_ToConfirmBase, _name, None)


@tagged("post_install", "-at_install", "distri_v129_39")
class TestV12939ToConfirm(_ToConfirmBase):

    def test_escalation_to_the_listed_logins(self):
        _flag(self.env)
        self.env["ir.config_parameter"].sudo().set_param(
            v2.PARAM_CONFIRM_LOGINS, "admin,%s" % self.admin_user.login)
        card, _route, stop = self._to_confirm_stop()
        before = _ledger(self.env)
        Stop = self.env["distri.route.stop"]
        Stop._distri_cron_paid_to_confirm(now=stop.paid_confirm_since + timedelta(hours=49))
        todos = card.activity_ids.filtered(lambda a: a.summary == t38.CONFIRM_SUMMARY)
        self.assertEqual(todos.user_id, (self.env.ref("base.user_admin") | self.admin_user))
        self.assertEqual(_ledger(self.env), before)


class CrucesMixin:

    def _credited_card(self, amount, when, operation=None, partner=None):
        """A receipt credited by Denis (its payment registered), waiting for
        its transfer in the report."""
        msg = self._receipt(amount, partner=partner, user=self.denis, when=when,
                            operation=operation)
        payment = self._credit(msg, user=self.denis, confirmed=True)
        self.assertTrue(payment)
        card = self._card(msg)
        self.assertFalse(card.income_id)
        return card

    def _report_income(self, amount, when, **vals):
        """A report income loaded without the matching (as the 9 of 25/11)."""
        type(self)._operation += 1
        return self._income(str(self._operation), amount, when=when, **vals)

    def _pairs(self):
        return [(c.id, i.id) for c, i in self.Collection._distri_v2_tie_proposals()]


@tagged("post_install", "-at_install", "distri_v129_39")
class TestV12939Board(CrucesMixin, _BoardBase):

    # ------------------------------------------------------------ the 48 h To-Do

    def test_to_confirm_goes_only_to_the_listed_logins(self):
        _flag(self.env)
        Stop = self.env["distri.route.stop"]
        admin = self.env.ref("base.user_admin")
        self.assertEqual(v2.confirm_logins(self.env), ["admin", "denis"])
        users = Stop._distri_admin_users()
        self.assertIn(admin, users)
        for user in (self.denis, self.gonzalo, self.sistemas):
            self.assertNotIn(user, users, "administración alone does not get it")
        params = self.env["ir.config_parameter"].sudo()
        params.set_param(v2.PARAM_CONFIRM_LOGINS, "admin, %s" % self.denis.login)
        self.assertEqual(Stop._distri_admin_users(), (admin | self.denis).sorted("id"))
        # an archived login is skipped
        self.denis.sudo().active = False
        self.assertEqual(Stop._distri_admin_users(), admin)
        self.denis.sudo().active = True
        # none of the logins exists: the 129.37 rule, so nobody is left out
        params.set_param(v2.PARAM_CONFIRM_LOGINS, "nadie_v129_39")
        self.assertIn(self.gonzalo, Stop._distri_admin_users())

    # ------------------------------------------------------------ queue 7 in two searches

    def test_other_customer_in_two_searches_same_answers(self):
        _flag(self.env)
        incomes = self.Collection.browse()
        for n in range(6):
            income = self._income()
            incomes |= income
            if n % 2:
                self._pending_receipt(self.other, income.amount,
                                      distri_operation=income.source_id)
        one_by_one = {i.id: i._distri_v2_other_customer_text() for i in incomes}
        self.assertEqual(sum(1 for t in one_by_one.values() if t), 3)
        Message = type(self.env["distri.whatsapp.message"])
        calls = []
        search = Message.search

        def counting(records, *args, **kwargs):
            calls.append(args[0] if args else kwargs.get("domain"))
            return search(records, *args, **kwargs)
        with patch.object(Message, "search", counting):
            rows = self._rows("sin_comprobante")
        mine = {r["id"]: r["other"] for r in rows if r["id"] in one_by_one}
        self.assertEqual(mine, one_by_one)
        self.assertLessEqual(len(calls), 2, "two searches for all the rows")

    # ------------------------------------------------------------ proposed ties

    def test_unique_pair_is_proposed_and_cruzar_ties_it(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        card = self._credited_card(41340.0, when)
        income = self._report_income(41340.0, when - timedelta(hours=3, minutes=20))
        self.assertEqual(self._pairs(), [(card.id, income.id)])
        rows = self.Collection.with_user(self.denis)._distri_v2_proposal_rows(self.denis)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["amount"], vc._money(41340.0))
        self.assertEqual(row["gap"], "3 h 20 min antes del comprobante")
        self.assertTrue(row["can_tie"])
        self.assertFalse(row["warning"])
        before = _ledger(self.env)
        payments = self.env["account.payment"].search_count([])
        tied = self.Collection.with_user(self.denis)._distri_v2_cruzar(card.id, income.id)
        self.assertEqual(tied, income)
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, card.payment_id, "the card's payment, not a new one")
        self.assertEqual(card.income_id, income)
        self.assertIn("vinculado a mano por %s" % self.denis.name, income.proposal_note)
        self.assertEqual(self.env["account.payment"].search_count([]), payments)
        self.assertEqual(_ledger(self.env), before, "ledger unchanged")
        self.assertEqual(self._pairs(), [], "tied: no longer proposed")
        with self.assertRaisesRegex(UserError, "ya no está propuesto"):
            self.Collection.with_user(self.denis)._distri_v2_cruzar(card.id, income.id)

    def test_previous_day_is_proposed_two_days_is_not(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=10)
        card = self._credited_card(41510.0, when)
        far = self._report_income(41510.0, when - timedelta(days=2, hours=1))
        self.assertEqual(self._pairs(), [], "two days before: not the same transfer")
        near = self._report_income(41510.0, when - timedelta(hours=26))
        if near.date_day == card._distri_credit_day() - timedelta(days=1):
            self.assertEqual(self._pairs(), [(card.id, near.id)])
        self.assertFalse(far.receipt_id)

    def test_two_candidates_are_not_proposed(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        # two incomes for one card
        card = self._credited_card(41680.0, when)
        self._report_income(41680.0, when - timedelta(hours=1))
        self._report_income(41680.5, when - timedelta(hours=2))
        self.assertEqual(self._pairs(), [])
        # two cards for one income
        self._credited_card(41850.0, when - timedelta(minutes=5))
        self._credited_card(41850.0, when - timedelta(minutes=9), partner=self.other)
        self._report_income(41850.0, when - timedelta(hours=1))
        before = _ledger(self.env)
        self.assertEqual(self._pairs(), [])
        self.assertEqual(self.Collection._distri_v2_proposal_rows(self.denis), [])
        self.assertFalse(card.income_id)
        self.assertEqual(_ledger(self.env), before)

    def test_another_operation_or_cuit_is_not_proposed(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        self._credited_card(42020.0, when, operation="712939000777")
        self._report_income(42020.0, when - timedelta(hours=1))
        self.assertEqual(self._pairs(), [], "the receipt's operation number is another one")

    def test_round_amount_is_proposed_and_tied_by_a_person(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        card = self._credited_card(40000.0, when)
        income = self._report_income(40000.0, when - timedelta(hours=2))
        self.assertEqual(self._pairs(), [(card.id, income.id)], "a round amount: a person clicks")
        self.assertEqual(income.state, "draft", "never tied by itself")
        before = _ledger(self.env)
        self.Collection.with_user(self.denis)._distri_v2_cruzar(card.id, income.id)
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, card.payment_id)
        self.assertEqual(_ledger(self.env), before)

    def test_wholesale_set_aside_shows_a_warning_and_no_cruzar(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        card = self._credited_card(42190.0, when)
        income = self._report_income(42190.0, when - timedelta(hours=1))
        income.sudo().write({"state": "apartado", "wholesale_auto": True,
                             "wholesale_label": "Mayorista V129.39"})
        self.assertEqual(self._pairs(), [(card.id, income.id)])
        row = self.Collection._distri_v2_proposal_rows(self.denis)[0]
        self.assertFalse(row["can_tie"])
        self.assertIn("Mayorista fuera del sistema: posible doble acreditación", row["warning"])
        before = _ledger(self.env)
        with self.assertRaisesRegex(UserError, "mayorista"):
            self.Collection.with_user(self.denis)._distri_v2_cruzar(card.id, income.id)
        self.assertEqual(income.state, "apartado")
        self.assertFalse(card.income_id)
        self.assertEqual(_ledger(self.env), before)

    def test_cruzar_needs_cobros(self):
        _flag(self.env)
        when = fields.Datetime.now() - timedelta(minutes=30)
        card = self._credited_card(42360.0, when)
        income = self._report_income(42360.0, when - timedelta(hours=1))
        self.assertFalse(self.Collection._distri_v2_proposal_rows(self.cashier)[0]["can_tie"])
        with self.assertRaises(AccessError):
            self.Collection.with_user(self.cashier)._distri_v2_cruzar(card.id, income.id)
        self.assertEqual(income.state, "draft")

    def test_flag_off_unchanged(self):
        when = fields.Datetime.now() - timedelta(minutes=30)
        card = self._credited_card(42530.0, when)
        income = self._report_income(42530.0, when - timedelta(hours=1))
        before = _ledger(self.env)
        self.assertEqual(self._pairs(), [])
        self.assertEqual(self.Collection._distri_v2_proposal_rows(self.denis), [])
        with self.assertRaises(UserError):
            self.Collection.with_user(self.denis)._distri_v2_cruzar(card.id, income.id)
        self.assertEqual((income.state, card.income_id.id), ("draft", False))
        self.assertEqual(_ledger(self.env), before)


@tagged("post_install", "-at_install", "distri_v129_39")
class TestV12939Routes(CrucesMixin, t38.BoardMixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._setup_board()

    def test_queue_5_shows_the_proposal_and_cruzar_posts(self):
        when = fields.Datetime.now() - timedelta(minutes=30)
        card = self._credited_card(43010.0, when)
        income = self._report_income(43010.0, when - timedelta(hours=1))
        self.authenticate(self.denis.login, self.denis.login)
        self.assertEqual(self.url_open("/verificacion/diferencias").status_code, 404,
                         "flag off")
        _flag(self.env)
        page = self.url_open("/verificacion/diferencias")
        self.assertEqual(page.status_code, 200)
        self.assertIn("Cruces propuestos", page.text)
        self.assertIn('data-cruce="%s-%s"' % (card.id, income.id), page.text)
        self.assertIn("1 h antes del comprobante", page.text)
        self.assertIn(">Cruzar<", page.text)
        token = re.search(r'name="csrf_token" value="([^"]+)"', page.text).group(1)
        before = _ledger(self.env)
        res = self.url_open("/verificacion/cruzar", data={
            "csrf_token": token, "card_id": card.id, "income_id": income.id},
            allow_redirects=False)
        self.assertEqual(res.status_code, 303)
        self.assertIn("/verificacion/diferencias?ok=", res.headers["Location"])
        self.env.invalidate_all()
        self.assertEqual(income.state, "confirmed")
        self.assertEqual(income.payment_id, card.payment_id)
        self.assertEqual(_ledger(self.env), before)
        res = self.url_open("/verificacion/cruzar", data={
            "csrf_token": token, "card_id": card.id, "income_id": income.id},
            allow_redirects=False)
        self.assertIn("/verificacion/diferencias?error=", res.headers["Location"])
        # a cashier has no queue 5
        self.authenticate(self.cashier.login, self.cashier.login)
        res = self.url_open("/verificacion/cruzar", data={
            "csrf_token": token, "card_id": card.id, "income_id": income.id},
            allow_redirects=False)
        self.assertIn(res.status_code, (400, 403))
