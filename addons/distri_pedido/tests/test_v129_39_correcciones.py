"""19.0.1.129.39, findings 4 and 6 of the review of 129.33-129.38:
- turning «Nueva venta» on is refused while «Facturación directa de
  servicios» has no active user (factura_directa.activation_refusal, read
  by scripts/correcciones/v129_30_activar_nueva_venta.sh);
- Odoo's reversal of a posted credit note (undoing a mistaken AJC) is not a
  «factura directa»: administración posts it with the flag on; an invoice
  «reversing» a credit note above its total still is one.
Refusals change nothing: the ledger is compared."""
import os

from odoo import Command
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests import tagged

from odoo.addons.distri_pedido.models.factura_directa import (GRUPO_DIRECTA, SIN_GRUPO,
                                                              activation_refusal)

from . import test_v129_36_facturas as t36


class _FacturasBase(t36.TestV12936Facturas):
    """The 129.36 setup and helpers, without its tests."""


for _name in [n for n in dir(t36.TestV12936Facturas)
              if n.startswith("test_") and callable(getattr(t36.TestV12936Facturas, n))]:
    setattr(_FacturasBase, _name, None)


@tagged("post_install", "-at_install", "distri_v129_39")
class TestV12939Correcciones(_FacturasBase):

    # ------------------------------------------------------------ activation

    def test_activation_refused_while_the_group_is_empty(self):
        group = self.env.ref(GRUPO_DIRECTA)
        self.assertEqual(activation_refusal(self.env), "", "the three of the test are in it")
        members = group.user_ids
        group.sudo().write({"user_ids": [Command.unlink(u.id) for u in members]})
        self.assertEqual(activation_refusal(self.env), SIN_GRUPO)
        self.assertIn("v129_36_facturacion_directa.sh", SIN_GRUPO)
        # an archived member does not count
        self.denis.sudo().write({"group_ids": [Command.link(group.id)]})
        self.denis.sudo().active = False
        self.assertEqual(activation_refusal(self.env), SIN_GRUPO)
        self.denis.sudo().active = True
        self.assertEqual(activation_refusal(self.env), "")

    def test_activation_script_checks_the_group(self):
        script = os.path.join(get_module_path("distri_pedido"), "..", "..", "scripts",
                              "correcciones", "v129_30_activar_nueva_venta.sh")
        if not os.path.exists(script):
            self.skipTest("the scripts folder is not next to the addons here")
        with open(script, encoding="utf-8") as handle:
            text = handle.read()
        self.assertIn("activation_refusal", text)
        self.assertIn("REFUSED", text)
        # the refusal comes before the parameter is written
        self.assertLess(text.index("activation_refusal(env)"), text.index("set_param("))

    # ------------------------------------------------------------ reversal of a credit note

    def test_reversing_an_ajc_credit_note_is_not_a_direct_invoice(self):
        self._flag()
        # a mistaken «Ajuste de cuenta»: one AJC credit note, by Cobros
        wizard = self.env["distri.balance.adjustment.wizard"].with_user(self.cobros).create({
            "partner_id": self.cliente.id, "target_balance": -500.0,
            "motive": "Diferencia con Online", "authorized_by": "Gonzalo"})
        wizard.action_apply()
        ajc = self.env["distri.balance.adjustment"].search(
            [("partner_id", "=", self.cliente.id)]).move_id
        self.assertEqual((ajc.move_type, ajc.state), ("out_refund", "posted"))
        balance = self._ledger()[1]
        # Odoo's «Revertir» on it, by administración (not in the group)
        reversal = self.env["account.move.reversal"].with_user(self.admin).with_context(
            active_model="account.move", active_ids=ajc.ids).create({
                "reason": "Ajuste equivocado", "journal_id": ajc.journal_id.id})
        reversal.refund_moves()
        back = ajc.reversal_move_ids
        self.assertEqual(len(back), 1)
        self.assertEqual(back.move_type, "out_invoice")
        self.assertFalse(back._distri_es_directa())
        if back.state == "draft":
            back.with_user(self.admin).action_post()
        self.assertEqual(back.state, "posted")
        self.assertEqual(self._ledger()[1], round(balance + ajc.amount_total, 2),
                         "the balance is back where it was before the AJC")

    def test_an_invoice_above_the_credit_note_is_still_direct(self):
        self._flag(False)
        refund = self._direct(self.admin, [self._goods_line()], "out_refund")
        refund.with_user(self.admin).action_post()
        self._flag()
        bigger = self._direct(self.admin, [dict(self._goods_line(), price_unit=50000.0)])
        bigger.reversed_entry_id = refund
        same = self._direct(self.admin, [self._goods_line()])
        same.reversed_entry_id = refund
        before = self._ledger()
        self.assertTrue(bigger._distri_es_directa())
        with self.assertRaisesRegex(UserError, "Nueva venta"):
            bigger.with_user(self.admin).action_post()
        self.assertEqual(bigger.state, "draft")
        # the same amount: Odoo's reversal shape, allowed
        self.assertFalse(same._distri_es_directa())
        self.assertEqual(self._ledger(), before)
