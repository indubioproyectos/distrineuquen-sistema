"""19.0.1.129.46: marks the existing «… - lista 3» / «… - Lista 4» products
(models/lista_personal.py marcar_por_nombre) so Administración, Gerencia and
Sistemas get them in «Nueva venta» and /pedido. Only the new field is
written: prices, publication and every other field stay as they are."""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.distri_pedido.models.lista_personal import marcar_por_nombre

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    marcados = marcar_por_nombre(env)
    for valor, plantillas in sorted(marcados.items()):
        _logger.info("19.0.1.129.46: %s products marked %s (ids %s)",
                     len(plantillas), valor, plantillas.ids)
