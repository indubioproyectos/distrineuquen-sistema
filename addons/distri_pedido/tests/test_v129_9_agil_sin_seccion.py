"""19.0.1.129.9: AgilPedido products without a section show FIRST on /pedido,
above every section and with no heading, the way the storefront shows them
(models/agil_tienda.py SIN_SECCION_ORDEN). Synthetic data only."""
from odoo.tests import HttpCase, tagged

from odoo.addons.distri_pedido.models.agil_tienda import SIN_SECCION_ORDEN, filas_agil
from odoo.addons.distri_pedido.tests.common_pedido import PedidoAbiertoMixin
from odoo.addons.distri_pedido.tests.test_catalogo_agil import (
    BULTO, CIGARRILLO, GALLETITA, MAY, PRODUCTOS, SECCIONES, AgilMixin, storefront,
)

# no seccion_id, like «Cerveza Corona 710 x 12 u.»; last in the feed
SUELTO = {"id": 9900021, "nombre": "Cerveza Pruebagil 710 x 12 u.", "precio": 1500000}


@tagged("post_install", "-at_install", "distri_v129_9", "distri_catalogo_agil")
class TestAgilSinSeccion(PedidoAbiertoMixin, AgilMixin, HttpCase):

    def test_feed_puts_it_before_every_section(self):
        filas, _rechazados = filas_agil({"secciones": SECCIONES,
                                         "productos": PRODUCTOS + [SUELTO]})
        suelto = filas[-1]
        self.assertEqual(suelto["seccion_orden"], SIN_SECCION_ORDEN)
        self.assertLess(suelto["seccion_orden"], min(s["posicion"] for s in SECCIONES))

    def test_pedido_shows_it_first_without_heading(self):
        self._sync(storefront(PRODUCTOS + [SUELTO]))
        suelto = self._temporal(SUELTO)
        self.assertEqual(suelto.distri_agil_seccion_orden, SIN_SECCION_ORDEN)
        # a row stored by the old rule (9999) is fixed by the next sync
        suelto.sudo().distri_agil_seccion_orden = 9999
        self._sync(storefront(PRODUCTOS + [SUELTO]))
        self.assertEqual(suelto.distri_agil_seccion_orden, SIN_SECCION_ORDEN)

        self._switch("1")
        res = self.url_open("/pedido")
        self.assertEqual(res.status_code, 200)
        html = res.text
        fila = html.index('data-id="%s"' % suelto.id)
        # first: above every section heading and every sectioned product
        self.assertLess(fila, html.index("Zeta Pruebagil"))
        for producto in (GALLETITA, BULTO, MAY, CIGARRILLO):
            self.assertLess(fila, html.index('data-id="%s"' % self._temporal(producto).id))
        # in an always-open block, with no «Otros» heading
        bloque = html.rindex('<div class="bloque', 0, fila)
        self.assertTrue(html.startswith('<div class="bloque siempre"', bloque))
        self.assertNotIn("Otros", html[bloque:fila])
        # the sections keep the storefront order (position, then feed order)
        self.assertLess(html.index("Zeta Pruebagil"), html.index("Alfa Pruebagil"))
        self.assertLess(html.index('data-id="%s"' % self._temporal(MAY).id),
                        html.index('data-id="%s"' % self._temporal(CIGARRILLO).id))
