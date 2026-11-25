"""19.0.1.129.39, finding 2 (and 11) of the review of 129.33-129.38: the
charge of «Lo manda después» could be dodged by uploading any file. With
distri_pedido.nueva_venta on, every depot pending receipt of a transfer now
carries the user-charge mark whatever the receipt answer («photo» on the
page, «forwarded» of the backend wizard, «not_yet»):
- a photo administración rejects reopens the item, which then expires into
  the charge of who delivered;
- a «forwarded» answer that never brings the receipt expires the same way;
- a real receipt still resolves the item before its deadline (no charge),
  and the report closes it by itself.
Only pending receipts and cases are written: the ledger is compared."""
from datetime import timedelta

from odoo import fields
from odoo.tests import tagged

from odoo.addons.distri_pedido.models.nueva_venta import PARAM_NUEVA_VENTA

from . import test_v129_37_comprobantes as t37

_png = t37._png


class _DepotBase(t37.TestV12937Comprobantes):
    """The 129.37 setup and helpers, without its tests (the module is
    imported, not the class, so the loader does not run them twice)."""


for _name in [n for n in dir(t37.TestV12937Comprobantes)
              if n.startswith("test_") and callable(getattr(t37.TestV12937Comprobantes, n))]:
    setattr(_DepotBase, _name, None)


@tagged("post_install", "-at_install", "distri_v129_39")
class TestV12939Deposito(_DepotBase):

    def _expire(self, promise):
        promise.captured_at = fields.Datetime.now() - timedelta(days=3)
        self.Promise._distri_cron_expire()

    def test_photo_then_rejected_expires_into_the_charge(self):
        self._flag()
        order = self._packed()
        self._login(self.admin)
        res = self._deliver(order, [("cualquier-cosa.png", _png(), "image/png")])
        self.assertTrue(res.json()["ok"], res.text)
        self.assertEqual(self._stop(order).receipt_answer, "photo")
        promise = self._promise(order)
        self.assertTrue(promise.distri_user_charge, "marked whatever the answer")
        self.assertEqual(promise.state, "fulfilled", "the upload resolved it, as before")
        before = self._ledger()
        # administración rejects the file: the item reopens, still marked
        msg = self._receipts(order)
        msg.action_distri_dismiss_receipt()
        self.assertIn(promise.state, ("open", "overdue"))
        self.assertTrue(promise.distri_user_charge)
        self._login(self.admin)
        self.assertIn(order.name, self.url_open("/deposito/reclamar").text)
        self._expire(promise)
        self.assertEqual(promise.state, "overdue")
        case = promise.charge_case_id
        self.assertEqual((case.kind, case.state, case.driver_id), ("receipt", "open", self.admin))
        self.assertIn("Queda a cargo de %s" % self.admin.name, case.candidates)
        # Mercado Pago brings the transfer: it closes by itself
        self.Promise._distri_fulfil_for(self.partner, fields.Datetime.now(), 380)
        self.assertEqual(promise.state, "fulfilled")
        self.assertEqual(case.state, "closed")
        self.assertEqual(self._ledger(), before, "only the pending receipt and its charge")

    def test_photo_that_stays_valid_is_never_charged(self):
        self._flag()
        order = self._packed()
        self._login(self.admin)
        self._deliver(order, [("bien.png", _png(), "image/png")])
        promise = self._promise(order)
        before = self._ledger()
        self._expire(promise)
        self.assertEqual(promise.state, "fulfilled")
        self.assertFalse(promise.charge_case_id)
        self.assertEqual(self._ledger(), before)

    def test_forwarded_answer_expires_into_the_charge(self):
        self._flag()
        order = self._packed()
        action = order.with_user(self.admin).action_distri_depot_delivery()
        Wizard = self.env["distri.depot.delivery"].with_user(self.admin).with_context(
            action["context"])
        wizard = Wizard.create({"returns_answer": "none", "code_option": "without_code",
                                "transfer": "yes", "transfer_amount": 380.0,
                                "receipt_answer": "forwarded"})
        wizard.action_confirm()
        self.assertEqual(self._stop(order).receipt_answer, "forwarded")
        promise = self._promise(order)
        self.assertTrue(promise.distri_user_charge)
        self.assertEqual(promise.driver_id, self.admin)
        before = self._ledger()
        self._expire(promise)
        self.assertEqual(promise.state, "overdue")
        self.assertEqual(promise.charge_case_id.state, "open")
        self.assertEqual(self._ledger(), before)

    def test_flag_off_unchanged(self):
        self._flag(False)
        order = self._packed()
        action = order.with_user(self.admin).action_distri_depot_delivery()
        Wizard = self.env["distri.depot.delivery"].with_user(self.admin).with_context(
            action["context"])
        Wizard.create({"returns_answer": "none", "code_option": "without_code",
                       "transfer": "yes", "transfer_amount": 380.0,
                       "receipt_answer": "forwarded"}).action_confirm()
        promise = self._promise(order)
        self.assertFalse(promise.distri_user_charge)
        self.assertEqual(self.env["ir.config_parameter"].sudo().get_param(PARAM_NUEVA_VENTA),
                         "0")
