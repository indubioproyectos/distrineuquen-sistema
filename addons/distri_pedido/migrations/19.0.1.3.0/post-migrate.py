"""Remitos como R-NNNNN (models/remito.py, numerar_remitos)."""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.distri_pedido.models.remito import numerar_remitos

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    _logger.info("distri_pedido: %s entregas renombradas a R-NNNNN", numerar_remitos(env))
