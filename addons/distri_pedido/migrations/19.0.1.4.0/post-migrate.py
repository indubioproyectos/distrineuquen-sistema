"""Flag the generic "Pedidos web" contacts (models/res_partner.py).

Until 19.0.1.3.0 they were found by name, so renaming one re-labeled every
unassigned web order. They are found here by their current name once, get
the distri_generic_partner flag and a stable XML id, and from then on the
module only looks at the flag.
"""
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.distri_pedido.models.res_partner import (
    GENERIC_NAMES, GENERIC_RETAIL, GENERIC_WHOLESALE,
)

_logger = logging.getLogger(__name__)

XMLIDS = {
    GENERIC_RETAIL: "socio_web_sin_asignar",
    GENERIC_WHOLESALE: "socio_web_mayorista",
}


def _find(env, kind):
    Partner = env["res.partner"].with_context(active_test=False)
    partner = Partner.search([
        ("distri_generic_partner", "=", True), ("distri_generic_kind", "=", kind),
    ], order="id", limit=1)
    if partner:
        return partner
    partner = env.ref("distri_pedido.%s" % XMLIDS[kind], raise_if_not_found=False)
    if partner:
        return partner
    candidates = Partner.search([("name", "=ilike", GENERIC_NAMES[kind])], order="id")
    if len(candidates) > 1:
        # the one holding the web orders is the real one
        counts = {p.id: env["sale.order"].search_count([("partner_id", "=", p.id)]) for p in candidates}
        candidates = candidates.sorted(lambda p: (-counts[p.id], p.id))
    return candidates[:1]


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    for kind, xmlid in XMLIDS.items():
        partner = _find(env, kind)
        if not partner:
            _logger.warning("distri_pedido: no generic contact found for %s (%r); "
                            "one is created with the next web order",
                            kind, GENERIC_NAMES[kind])
            continue
        partner.write({"distri_generic_partner": True, "distri_generic_kind": kind,
                       "active": True})
        env["ir.model.data"]._update_xmlids([{
            "xml_id": "distri_pedido.%s" % xmlid, "record": partner, "noupdate": True,
        }])
        _logger.info("distri_pedido: generic contact %s -> res.partner %s (%s)",
                     kind, partner.id, partner.name)

    # Leftover of the rename incident: the retail generic kept the city and
    # country typed for "CASTAÑEDA JUAN" when the name was restored by hand.
    retail = env.ref("distri_pedido.socio_web_sin_asignar", raise_if_not_found=False)
    if retail and retail.city == "Loma Verde" and not retail.street:
        retail.write({"city": False, "country_id": False})
        _logger.info("distri_pedido: cleared the leftover address of res.partner %s", retail.id)
