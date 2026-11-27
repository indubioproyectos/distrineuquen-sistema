"""19.0.1.129.58: a bank's alphanumeric operation number is kept whole.

Two BPN/BNA transfers «ORD6LEN8MZRK3KZPNM1Y30» and «ORD6LEN8M3YVEYVONM1Y30»
collapsed to the same «683130» (cobros.digits) and the second, legitimate
receipt was refused as «comprobante repetido» (prod, 27/11/2026).

- models/cobros.py operation_key: with letters, the whole code (uppercase,
  without separators); purely numeric (Mercado Pago), its digits as before.
  Used when the card stores the operation and in the duplicate check.
- operation_close: an alphanumeric code is close only to the same code.
- The real duplicate (same code, even written another way) is still refused;
  a Mercado Pago id keeps its one-slip tolerance.
- migrations/19.0.1.129.58: a card stored with the collapsed digits of its
  receipt's alphanumeric operation gets the full code.
Synthetic data only: no real customer, receipt or account.
"""
import importlib.util
import os

from odoo import Command
from odoo.exceptions import UserError
from odoo.modules.module import get_module_path
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingCommon
from odoo.addons.distri_pedido.models.acreditar import PARAM_DRIVER_RULE
from odoo.addons.distri_pedido.models.cobros import digits, operation_close, operation_key
from odoo.addons.distri_pedido.tests.test_reparto import RepartoMixin
from odoo.addons.distri_pedido.tests.test_v126_acreditar import CreditMixin

ORD_A = "ORD6LEN8MZRK3KZPNM1Y30"
ORD_B = "ORD6LEN8M3YVEYVONM1Y30"


@tagged("post_install", "-at_install", "distri_v129_58")
class TestOperationKey(AccountTestInvoicingCommon):

    def test_alphanumeric_codes_keep_their_letters(self):
        self.assertEqual(digits(ORD_A), digits(ORD_B), "the bug: both collapse to 683130")
        self.assertEqual(operation_key(ORD_A), ORD_A)
        self.assertNotEqual(operation_key(ORD_A), operation_key(ORD_B))
        self.assertEqual(operation_key(" ord6len8-mzrk3kzp nm1y30 "), ORD_A)
        self.assertEqual(operation_key("0026a02d-2405-4db6-821e-3bcdbed65d56"),
                         "0026A02D24054DB6821E3BCDBED65D56")

    def test_numeric_operation_keeps_only_its_digits(self):
        for value, key in (("126000000011", "126000000011"), ("1260 0000-0011", "126000000011"),
                           ("Nº 490226851476", "490226851476"), ("Nro. 490226851476", "490226851476"),
                           ("Op: 490226851476", "490226851476"), ("", ""), (None, ""),
                           (False, "")):
            self.assertEqual(operation_key(value), key, value)

    def test_operation_close_never_matches_alphanumeric_codes_by_digits(self):
        self.assertFalse(operation_close(ORD_A, ORD_B))
        self.assertFalse(operation_close(ORD_A, "683130"))
        self.assertFalse(operation_close("683130", ORD_B))
        # one character away: another transfer, no OCR slip for bank codes
        self.assertFalse(operation_close(ORD_A, ORD_A[:-1] + "1"))
        self.assertTrue(operation_close(ORD_A, ORD_A.lower()))
        self.assertTrue(operation_close(ORD_A, "ORD-6LEN8MZRK3KZPNM1Y30"))
        # Mercado Pago ids: unchanged
        self.assertTrue(operation_close("126000000011", "126000000011"))
        self.assertTrue(operation_close("126000000011", "126000000019"))
        self.assertTrue(operation_close("12600000001", "126000000011"))
        self.assertFalse(operation_close("126000000011", "126000000099"))
        self.assertTrue(operation_close("Nº 126000000011", "126000000019"))


@tagged("post_install", "-at_install", "distri_v129_58")
class TestAlphanumericOperationCredit(CreditMixin, RepartoMixin, AccountTestInvoicingCommon):

    ROUTE_ON_ROAD = True

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        from odoo.addons.distri_pedido.tests.test_ocr import TEST_OWN_ACCOUNT
        cls.env["ir.config_parameter"].sudo().set_param("distri_pedido.cuenta_propia",
                                                        TEST_OWN_ACCOUNT)
        cls.env["ir.config_parameter"].sudo().set_param(PARAM_DRIVER_RULE, "")
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id),
            Command.link(cls.env.ref("distri_pedido.grupo_cobros").id),
            Command.link(cls.env.ref("sales_team.group_sale_manager").id),
        ]})
        cls._setup_reparto()
        cls.seller = new_test_user(cls.env, login="vendedor_v129_58", name="Vendedor V12958",
                                   groups="base.group_user,distri_pedido.grupo_vendedor",
                                   company_id=cls.env.company.id,
                                   company_ids=[Command.set(cls.env.company.ids)])
        cls.partner = cls.env["res.partner"].create({"name": "Almacén Prueba V12958",
                                                     "company_type": "company"})
        cls.other_partner = cls.env["res.partner"].create({"name": "Kiosco Prueba V12958",
                                                           "company_type": "company"})

    def test_two_codes_with_the_same_digits_do_not_collide(self):
        first = self._receipt(48000, operation=ORD_A)
        self._credit(first)
        self.assertEqual(self._card(first).state, "confirmed")
        self.assertEqual(self._card(first).operation, ORD_A)
        # another transfer, another payer and amount: the same digits 683130
        second = self._receipt(52500, partner=self.other_partner, operation=ORD_B)
        self.assertEqual(second.distri_receipt_state, "pending")
        self._credit(second)
        card = self._card(second)
        self.assertEqual((card.state, card.operation), ("confirmed", ORD_B))

    def test_same_code_with_close_amount_is_not_a_slip_of_another(self):
        self._credit(self._receipt(48000, operation=ORD_A))
        second = self._receipt(48000, partner=self.other_partner, operation=ORD_B)
        self._credit(second)
        self.assertEqual(self._card(second).state, "confirmed")

    def test_the_same_code_is_still_refused(self):
        first = self._receipt(61000, operation=ORD_A)
        self._credit(first)
        copy = self._receipt(61000, operation=ORD_A)
        self.assertEqual(copy.distri_receipt_state, "duplicate")
        # the same code read another way (lowercase, a dash): still a copy
        other_way = self._receipt(61000, operation="ord6len8-mzrk3kzpnm1y30")
        self.assertEqual(self._card(other_way).operation, ORD_A)
        with self.assertRaisesRegex(UserError, "La operación %s ya se acreditó" % ORD_A):
            self._credit(other_way)

    def test_numeric_mercado_pago_id_as_before(self):
        first = self._receipt(9000, operation="129580000011")
        self._credit(first)
        self.assertEqual(self._card(first).operation, "129580000011")
        spaced = self._receipt(9000, operation="1295 8000 0019")
        self.assertEqual(self._card(spaced).operation, "129580000019")
        with self.assertRaisesRegex(UserError, "ya se acreditó"):
            self._credit(spaced)

    def test_migration_restores_the_full_code(self):
        first = self._receipt(73000, operation=ORD_A)
        numeric = self._receipt(74000, operation="129580000021")
        card, mp_card = self._card(first), self._card(numeric)
        card.write({"operation": digits(ORD_A)})  # as stored before 19.0.1.129.58
        path = os.path.join(get_module_path("distri_pedido"),
                            "migrations", "19.0.1.129.58", "post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v129_58_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        self.env.flush_all()
        module.migrate(self.env.cr, "19.0.1.129.57")
        module.migrate(self.env.cr, "19.0.1.129.57")
        self.env.invalidate_all()
        self.assertEqual(card.operation, ORD_A)
        self.assertEqual(mp_card.operation, "129580000021")
