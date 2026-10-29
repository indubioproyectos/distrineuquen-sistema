# Seller tags copied from Odoo Online (read only, 28/10/2026): «Italo» is
# exactly Online's ITALO tag (130 customers, matched by ref ODOO-<online id>);
# the lab's old «Italo» on 28 other customers (mostly an alphabetical run of
# A… names) is removed. Every other direct customer gets «Distrineuquén», and
# an Italo customer loses it. Only tags change: no balance moves.
# Input: /in/online_tags.json ({"ITALO": [online ids], ...}).
# Rolls back unless DO_COMMIT=1.
import json
import os

from odoo import Command
from odoo.addons.distri_pedido.models.deudores import direct_customers, seller_tags

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
P = env['res.partner'].sudo().with_context(active_test=False)
italo, distri = seller_tags(env)
assert italo and italo.id == 2 and distri, (italo, distri)
online = json.load(open('/in/online_tags.json'))


def totals():
    partners = P.search([('parent_id', '=', False), ('distri_generic_partner', '=', False)])
    return round(sum(p._distri_account_balance() for p in partners), 2)


before = totals()
want_italo = P.search([('ref', 'in', ['ODOO-%d' % i for i in online['ITALO']])])
assert len(want_italo) == len(online['ITALO']) == 130, len(want_italo)
had_italo = P.search([('category_id', 'in', italo.ids)])
removed = had_italo - want_italo
added = want_italo - had_italo
removed.write({'category_id': [Command.unlink(italo.id)]})
added.write({'category_id': [Command.link(italo.id)]})
want_italo.filtered(lambda p: distri in p.category_id).write({'category_id': [Command.unlink(distri.id)]})
direct = direct_customers(env)
to_distri = direct.filtered(lambda p: italo not in p.category_id and distri not in p.category_id)
to_distri.write({'category_id': [Command.link(distri.id)]})
env.flush_all()
after = totals()
print('REMOVED_ITALO', len(removed), sorted(removed.mapped('name'))[:40])
print('RESULT', {
    'italo_now': P.search_count([('category_id', 'in', italo.ids)]),
    'italo_added': len(added), 'italo_removed': len(removed),
    'distri_added': len(to_distri),
    'distri_now': P.search_count([('category_id', 'in', distri.ids)]),
    'both': P.search_count([('category_id', 'in', italo.ids), ('category_id', 'in', distri.ids)]),
    'direct_italo': len(direct.filtered(lambda p: italo in p.category_id)),
    'balance_total': (before, after),
})
if DO_COMMIT and before == after:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED')
