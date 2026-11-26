"""19.0.1.129.52 (owner, 26/11/2026), idempotent (a second run changes
nothing):

1. Price lists (models/listas_distri.py _asegurar_estructura): «Distribuidoras»
   (same record) is renamed «May farmacia y varios»; every system list gets
   its distri_clave; the May list gets a global rule based on General (what
   has no «May» price takes the General price) and a 0 rule per cigarette
   category (a cigarette without a price in the customer's list stays «a
   presupuestar»); «Lista 2 + May farmacia» is created; «Lista 3» and «Lista
   4» are reactivated, all three based on the May list.
2. Each «May X - lista 3/4» product is linked to its cigarette when the match
   is clear (_sugerir_vinculos); the rest are linked by hand on /listas.
3. The group «Listas y accesos de distribuidores» gets the users with login
   gonzalo and admin (by login, never by id), if they exist.
4. Internal users get Odoo's notifications in the inbox, not by e-mail.

No invoice, payment, entry or partner price list is written here.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.distri_pedido.models.listas_distri import GRUPO_LISTAS, LOGINS_GRUPO_LISTAS
from odoo.addons.distri_pedido.models.notificaciones import poner_bandeja

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    _listas, hecho = env["distri.listas"]._asegurar_estructura()
    for linea in hecho:
        _logger.info("19.0.1.129.52: %s", linea)
    for producto, cigarrillo in env["distri.listas"]._sugerir_vinculos():
        _logger.info("19.0.1.129.52: «%s» (id %s) linked to «%s» (id %s)",
                     producto.name, producto.id, cigarrillo.name, cigarrillo.id)
    grupo = env.ref(GRUPO_LISTAS, raise_if_not_found=False)
    if grupo:
        usuarios = env["res.users"].search([("login", "in", list(LOGINS_GRUPO_LISTAS)),
                                            ("share", "=", False)])
        nuevos = usuarios - grupo.user_ids
        if nuevos:
            grupo.write({"user_ids": [(4, u.id) for u in nuevos]})
        _logger.info("19.0.1.129.52: group %s: added %s", GRUPO_LISTAS, nuevos.mapped("login"))
    internos = poner_bandeja(env["res.users"].with_context(active_test=False).search(
        [("share", "=", False)]))
    _logger.info("19.0.1.129.52: %s internal users switched to inbox notifications (%s)",
                 len(internos), internos.mapped("login"))
