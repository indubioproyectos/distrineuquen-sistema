"""19.0.1.9.0: driver expenses and parcels to the interior.

- Wholesale price lists («Mayorista» and the «Revendedor NN» lists) are
  marked «Lista mayorista»: their parcels are paid by the customer.
- Open orders get «Encomienda», carrier and who pays by the new rule (the
  Zapala and Cutral Có zones are created by data/encomiendas.xml after the
  new fields were first computed).
"""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    Pricelist = env["product.pricelist"].with_context(active_test=False)
    wholesale = Pricelist.search([]).filtered(
        lambda p: (p.name or "").strip().lower() == "mayorista"
        or (p.name or "").strip().lower().startswith("revendedor"))
    wholesale.write({"distri_mayorista": True})
    orders = env["sale.order"].search([("state", "in", ("draft", "sent", "sale"))])
    orders._distri_recompute_parcel_rule()
    env.flush_all()
