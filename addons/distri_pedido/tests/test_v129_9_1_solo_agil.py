"""19.0.1.129.10: the AgilPedido section «Vent espec Distribuidoras + $
10.000.000» stays in AgilPedido only: /pedido never lists its products, and
products without a section still show (models/agil_tienda.py
SECCIONES_SOLO_AGIL). Synthetic data only."""
from odoo.tests import HttpCase, tagged

from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_catalogo_agil import (
    GALLETITA, PRODUCTOS, SECCIONES, AgilMixin, storefront,
)

DISTRIBUIDORAS = {"id": 91009, "nombre": "Vent espec Distribuidoras + $ 10.000.000 ",
                  "posicion": 2}
SOLO_AGIL = {"id": 9900031, "nombre": "Caja Pruebagil Distribuidora x 100", "precio": 9900000,
             "seccion_id": 91009}
SUELTO = {"id": 9900032, "nombre": "Cerveza Pruebagil Suelta 710", "precio": 1500000}


@tagged("post_install", "-at_install", "distri_v129_10_web", "distri_catalogo_agil")
class TestAgilSoloAgil(PedidoAbiertoMixin, AgilMixin, HttpCase):

    def test_distributors_section_is_not_listed(self):
        self._sync(storefront(PRODUCTOS + [SOLO_AGIL, SUELTO], SECCIONES + [DISTRIBUIDORAS]))
        solo, suelto = self._temporal(SOLO_AGIL), self._temporal(SUELTO)
        # synced and published like any other row: only /pedido hides it
        self.assertTrue(solo.is_published)
        self._switch("1")
        html = self.url_open("/pedido").text
        self.assertNotIn('data-id="%s"' % solo.id, html)
        self.assertNotIn("Vent espec Distribuidoras", html)
        self.assertIn('data-id="%s"' % suelto.id, html)
        self.assertIn('data-id="%s"' % self._temporal(GALLETITA).id, html)

    def test_administracion_still_sees_it(self):
        self._sync(storefront(PRODUCTOS + [SOLO_AGIL, SUELTO], SECCIONES + [DISTRIBUIDORAS]))
        solo = self._temporal(SOLO_AGIL)
        self._switch("1")
        admin = self.env["res.users"].create({
            "name": "Admin Pruebagil", "login": "admin.pruebagil", "password": "admin.pruebagil.1",
            "group_ids": [(4, self.env.ref("distri_pedido.grupo_administracion").id)]})
        self.authenticate(admin.login, "admin.pruebagil.1")
        html = self.url_open("/pedido").text
        self.assertIn('data-id="%s"' % solo.id, html)
        seller = self.env["res.users"].create({
            "name": "Vende Pruebagil", "login": "vende.pruebagil", "password": "vende.pruebagil.1",
            "group_ids": [(4, self.env.ref("distri_pedido.grupo_vendedor").id)]})
        self.authenticate(seller.login, "vende.pruebagil.1")
        self.assertNotIn('data-id="%s"' % solo.id, self.url_open("/pedido").text)
