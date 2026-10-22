"""Price lists renamed after the management decision of 29/09/2026.

- «Minorista» -> «General»: the only public list. The word «minorista» is
  no longer used; small shops are «mayoristas».
- «Mayorista» -> «Distribuidoras»: the hidden «May» list (the «Revendedor
  NN» lists are built on it). Only staff and customers flagged
  res.partner.distri_lista_oculta see it on /pedido.
- Customers that already have a hidden list assigned (Distribuidoras or a
  «Revendedor NN» list, flagged «Lista de distribuidoras») get the new
  distri_lista_oculta flag on their company, so they keep seeing their
  prices when they log in. Administración removes it where it does not apply.
- The /proyecto texts that named the old lists are reworded.
- The wholesale generic contact follows: «Pedidos web - lista distribuidoras
  - sin asignar» (only when it still has the old default name).

The code looks the lists up by name (controllers/main.py LISTA_GENERAL and
LISTA_OCULTA). The name is a translated field: it is reset to a single value
so every language shows the new name. A list is left alone when the new name
is already taken.
"""
import json
import logging

from odoo import SUPERUSER_ID, api

from odoo.addons.distri_pedido.controllers.main import LISTA_GENERAL, LISTA_OCULTA
from odoo.addons.distri_pedido.models.res_partner import (
    GENERIC_NAMES, GENERIC_UNLOCK_KEY, GENERIC_WHOLESALE,
)

_logger = logging.getLogger(__name__)

RENAMES = (("Minorista", LISTA_GENERAL), ("Mayorista", LISTA_OCULTA))

# /proyecto shows these texts publicly (data/proyecto.xml, noupdate). Each
# phrase is replaced only while the record still holds it: text edited in
# Odoo stays as it is.
PROJECT_TEXTS = (
    ("project_catalogo", "description",
     "listas Minorista, Mayorista y de los diez revendedores",
     "la lista General (pública), la lista oculta Distribuidoras y las de los diez "
     "revendedores"),
    ("project_pedidos", "description",
     "en /pedido. Los clientes mayoristas ocasionales compran en la sección MAYORISTA, al "
     "final del catálogo.",
     "en /pedido, con la lista mayorista general. Las distribuidoras y los revendedores "
     "autorizados entran con su usuario y ven además su lista."),
    # older wording, still in databases that loaded it before 28/09/2026
    ("project_pedidos", "description",
     "en /pedido. Los distribuidores compran en la sección DISTRIBUIDORAS, al final del "
     "catálogo.",
     "en /pedido, con la lista mayorista general. Las distribuidoras y los revendedores "
     "autorizados entran con su usuario y ven además su lista."),
    ("project_pedidos", "next_step",
     "El gerente decide los precios de la sección DISTRIBUIDORAS y la compra mínima",
     "El gerente decide el precio de la lista oculta cuando el cliente asignado tiene otra "
     "lista y la compra mínima"),
    ("project_pedidos", "next_step",
     "El gerente decide los precios de la sección MAYORISTA y la compra mínima",
     "El gerente decide el precio de la lista oculta cuando el cliente asignado tiene otra "
     "lista y la compra mínima"),
    ("project_precios_costo", "description",
     "Las listas Minorista y Mayorista, públicas,",
     "La lista General, pública,"),
)
OLD_WHOLESALE_GENERIC = "Pedidos web - lista mayorista - sin asignar"


def _ids_named(cr, name):
    cr.execute("""
        SELECT id FROM product_pricelist
         WHERE EXISTS (SELECT 1 FROM jsonb_each_text(name) t WHERE t.value = %s)
         ORDER BY id
    """, [name])
    return [row[0] for row in cr.fetchall()]


def migrate(cr, version):
    for old, new in RENAMES:
        if _ids_named(cr, new):
            _logger.info("distri_pedido: a price list named %r already exists; %r left as is",
                         new, old)
            continue
        ids = _ids_named(cr, old)
        if not ids:
            _logger.info("distri_pedido: no price list named %r", old)
            continue
        if len(ids) > 1:
            _logger.warning("distri_pedido: %s price lists named %r; only %s is renamed",
                            len(ids), old, ids[0])
        cr.execute("UPDATE product_pricelist SET name = %s::jsonb WHERE id = %s",
                   [json.dumps({"en_US": new}), ids[0]])
        _logger.info("distri_pedido: price list %s renamed %r -> %r", ids[0], old, new)

    env = api.Environment(cr, SUPERUSER_ID, {})
    env.invalidate_all()

    for xmlid, field, old, new in PROJECT_TEXTS:
        record = env.ref("distri_pedido.%s" % xmlid, raise_if_not_found=False)
        if record and old in (record[field] or ""):
            record[field] = record[field].replace(old, new)
            _logger.info("distri_pedido: %s.%s reworded", xmlid, field)

    cr.execute("""
        SELECT DISTINCT p.id
          FROM res_partner p
          CROSS JOIN LATERAL jsonb_each_text(p.specific_property_product_pricelist) e
          JOIN product_pricelist pl ON pl.id = e.value::int
         WHERE pl.distri_mayorista
    """)
    partners = env["res.partner"].browse([row[0] for row in cr.fetchall()])
    companies = partners.mapped("commercial_partner_id").filtered(
        lambda p: not p.distri_lista_oculta and not p.distri_generic_partner)
    if companies:
        companies.write({"distri_lista_oculta": True})
        _logger.info("distri_pedido: hidden list enabled for %s customers with a hidden "
                     "list assigned: %s", len(companies), companies.ids)
    generic = env["res.partner"].with_context(active_test=False).search([
        ("distri_generic_partner", "=", True), ("distri_generic_kind", "=", GENERIC_WHOLESALE),
    ])
    for partner in generic.filtered(lambda p: p.name == OLD_WHOLESALE_GENERIC):
        partner.with_context(**{GENERIC_UNLOCK_KEY: True}).write(
            {"name": GENERIC_NAMES[GENERIC_WHOLESALE]})
        _logger.info("distri_pedido: generic contact %s renamed to %r",
                     partner.id, GENERIC_NAMES[GENERIC_WHOLESALE])
