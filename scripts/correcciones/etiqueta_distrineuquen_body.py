# Tags every direct customer that is not Italo's with «Distrineuquén»
# (19.0.1.47.0, models/deudores.py). Direct customer: commercial partner
# with customer rank, receivable lines or imported history; not a generic
# contact; not a reseller or wholesaler (revision_cliente.is_reseller).
# Archived customers too (a debt is a debt). Only adds a tag: no balance moves.
#   NAME=distri-etiquetas DO_COMMIT=1 scripts/l10n_ar/lab.sh odoo shell -d distri_lab \
#     --no-http --log-level=warn < scripts/correcciones/etiqueta_distrineuquen_body.py
# Rolls back unless DO_COMMIT=1.
import os

from odoo import Command
from odoo.addons.distri_pedido.models.deudores import direct_customers, seller_tags
from odoo.addons.distri_pedido.models.revision_cliente import is_reseller

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
P = env['res.partner'].sudo().with_context(active_test=False)
italo, distri = seller_tags(env)
assert italo and italo.id == 2, italo
assert distri, 'run the module upgrade first (19.0.1.47.0)'


def totals():
    """(net sum, sum of the debts) of every commercial partner's balance."""
    partners = P.search([('parent_id', '=', False), ('distri_generic_partner', '=', False)])
    balances = [p._distri_account_balance() for p in partners]
    return round(sum(balances), 2), round(sum(b for b in balances if b > 0), 2)


before_total = totals()
commercial = P.search([('parent_id', '=', False), ('distri_generic_partner', '=', False)])
resellers = commercial.filtered(is_reseller)
direct = direct_customers(env)
with_italo = direct.filtered(lambda p: italo in p.category_id)
already = direct.filtered(lambda p: distri in p.category_id and italo not in p.category_id)
to_tag = direct - with_italo - already
to_tag.write({'category_id': [Command.link(distri.id)]})
after_total = totals()
assert before_total == after_total, (before_total, after_total)
print('RESULT', {
    'direct_customers': len(direct),
    'italo': len(with_italo),
    'italo_tag_total_partners': P.search_count([('category_id', 'in', italo.ids)]),
    'italo_tag_resellers': len(resellers.filtered(lambda p: italo in p.category_id)),
    'tagged_now': len(to_tag),
    'already_distrineuquen': len(already),
    'distrineuquen_total': P.search_count([('category_id', 'in', distri.ids)]),
    'resellers_excluded': len(resellers),
    'resellers_excluded_active': len(resellers.filtered('active')),
    'totals_net_debts': after_total,
})
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
