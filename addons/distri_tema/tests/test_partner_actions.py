from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

from odoo.addons.distri_tema.models.res_partner import whatsapp_digits


@tagged("post_install", "-at_install")
class TestPartnerQuickActions(TransactionCase):

    def test_whatsapp_digits(self):
        cases = {
            "+54 299 327-2591": "5492993272591",
            "+54 9 299 327-2591": "5492993272591",
            "0299 15 327-2591": "5492993272591",
            "299 4123456": "5492994123456",
            "011 15 4123-4567": "5491141234567",
            "+1 415 555 0100": "14155550100",
            "": "",
            False: "",
        }
        for phone, expected in cases.items():
            with self.subTest(phone=phone):
                self.assertEqual(whatsapp_digits(phone), expected)

    def test_actions(self):
        partner = self.env["res.partner"].create({
            "name": "Kiosco de prueba", "phone": "+54 299 327-2591",
            "street": "Linares 347", "city": "Neuquén",
        })
        self.assertEqual(partner.action_distri_call()["url"], "tel:+542993272591")
        self.assertEqual(partner.action_distri_whatsapp()["url"], "https://wa.me/5492993272591")
        directions = partner.action_distri_directions()
        self.assertTrue(directions["url"].startswith("https://www.google.com/maps/dir/?api=1&destination="))
        self.assertIn("Linares%20347", directions["url"])

    def test_actions_without_data(self):
        partner = self.env["res.partner"].create({"name": "Sin datos"})
        with self.assertRaises(UserError):
            partner.action_distri_whatsapp()
        with self.assertRaises(UserError):
            partner.action_distri_directions()
