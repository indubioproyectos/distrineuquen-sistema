# One-off (27/10/2026, distri_pedido 19.0.1.41.0): wholesalers out of the
# catch-up. odoo shell body, run after the module upgrade:
#   NAME=distri-mayoristas scripts/l10n_ar/lab.sh odoo shell -d distri_lab \
#     --no-http --log-level=warn < scripts/mayoristas-2026-10-27/marcar_mayoristas_body.py
#   DO_COMMIT=1 ... the same, to save.
# 1. «Es mayorista» on the customers named by management (WHOLESALE_REFS).
# 2. A payer rule (Cobros > Pagadores mayoristas) per CUIT of WHOLESALE_CUITS:
#    the pending Mercado Pago payments of that CUIT without receipt, with no
#    customer or proposed for a wholesaler, go to «Mayoristas apartados». A
#    payment proposed for another (retail) customer stays on the board.
#    Confirmed payments are never touched (only counted).
#    For the customers of CUITS_FROM_CONFIRMED_CARDS, the payer CUITs of
#    their confirmed Mercado Pago cards get a rule too (printed in the dry
#    run as «cuits_found», to check before DO_COMMIT=1).
#    Every card of a rule's CUIT that is confirmed, tied to an Online payment
#    or pending and proposed for a retail customer, is listed («cards_to_decide») and never moved.
# 3. ARCA_NAMES: legal names missing from the padrón file, upserted into
#    distri.arca.name (the padrón loader does not overwrite them).
# 4. «Revisión por cliente» rebuilt: those customers leave it.
# Dry run by default: prints what it would do and rolls back.
import json
import os

DO_COMMIT = os.environ.get('DO_COMMIT') == '1'
# ---- edit these lists: only wholesalers confirmed by management ----
WHOLESALE_REFS = [
    'ODOO-610',  # JORGE USTIAS - MAYORISTA
    'ODOO-509',  # ANDRES MAYORISTA
    'ODOO-795',  # PABLO MAYORISTA SENILLOSA
    'ODOO-597',  # JORGE AREVALO - MAYORISTA
    'ODOO-598',  # LUIS 25 DE MAYO - MAYORISTA
    'ODOO-696',  # CRISTIAN CORREA - MAYORISTA
    'ODOO-668',  # FEDE SPEED (Federico San Martín)
    'ODOO-496',  # COBOS MAYORISTA
    'ODOO-677',  # PATRICIO MAYORISTA
    'ODOO-656',  # DISTRIBUIDORA LA IMPERIAL MAYORISTA
    'ODOO-578',  # DARIO MAYORISTA
    'ODOO-300',  # LEIVA ARIEL - MAYORISTA
    'ODOO-667',  # ANTONIO SAAVEDRA PLOTIER - MAYORISTA
]
# their payer CUITs come from the MP cards the lab confirmed for them
CUITS_FROM_CONFIRMED_CARDS = ['ODOO-677', 'ODOO-656', 'ODOO-578', 'ODOO-300', 'ODOO-667']
WHOLESALE_CUITS = {
    # CUIT: (account label, note)
    '20368165884': ('Federico San Martín (FEDE SPEED)',
                    'Mayorista según ale, 27/10/2026. Alias de Mercado Pago «Dulzuras de oro».'),
    '20340880871': ('COBOS MAYORISTA',
                    'Revendedor según ale, 27/10/2026. Alias de Mercado Pago «Desp benjamin».'),
    '20314567901': ('MORA CHAVARRIA EDINSON EDUARDO',
                    'Mayorista según ale, 27/10/2026. Alias de Mercado Pago «Kiosco fullopen 24 RaC».'),
    '20272792047': ('OLMEDO NORBERTO',
                    'Mayorista según ale, 27/10/2026. Alias de Mercado Pago «La suerte». '
                    'No se marcó ODOO-582: no está confirmado que sea el mismo.'),
    '27273685796': ('OLEA CRISTINA DEL CARMEN',
                    'Mayorista según ale, 27/10/2026. Alias de Mercado Pago «Kiosco Gaby».'),
}
# payer CUITs never turned into rules here, even if found on confirmed cards
EXCLUDED_CUITS = {
    '27481454935': 'Alma Julieta Leiva: 61 payments, only 2 on LEIVA ARIEL; left out by ale, 27/10/2026',
}
# legal names of payers that the ARCA padrón file does not bring
ARCA_NAMES = {
    '20368165884': 'SAN MARTIN FEDERICO',
    '20314567901': 'MORA CHAVARRIA EDINSON EDUARDO',
    '23336502829': 'FERNANDEZ PATRICIO JOSE',
}
# -------------------------------------------------------------------------
REFS = WHOLESALE_REFS

Partner = env['res.partner'].sudo().with_context(active_test=False)
result = {'marked': [], 'already': [], 'missing': []}
for ref in REFS:
    partner = Partner.search([('ref', '=', ref)], limit=1)
    if not partner:
        result['missing'].append(ref)
        continue
    if partner.distri_es_mayorista:
        result['already'].append(ref)
        continue
    partner.write({'distri_es_mayorista': True})
    partner.message_post(body='Marcado «Es mayorista»: fuera de la puesta al día (27/10/2026).')
    result['marked'].append(ref)

env.flush_all()
Rule = env['distri.mp.wholesale.rule'].sudo().with_context(active_test=False)
Collection = env['distri.mp.collection'].sudo()
cuits = dict(WHOLESALE_CUITS)
result['cuits_found'] = {}
for ref in CUITS_FROM_CONFIRMED_CARDS:
    partner = Partner.search([('ref', '=', ref)], limit=1)
    if not partner:
        continue
    cards = Collection.search([('kind', '=', 'income'), ('state', '=', 'confirmed'),
                               ('payer_cuit', '!=', False),
                               ('partner_id', 'child_of', partner.commercial_partner_id.id)])
    found = sorted(set(cards.mapped('payer_cuit')))
    result['cuits_found'][ref] = {c: len(cards.filtered(lambda k: k.payer_cuit == c)) for c in found}
    for cuit in found:
        if cuit in EXCLUDED_CUITS:
            continue
        cuits.setdefault(cuit, (partner.name[:60], 'CUIT de pagos confirmados para %s (%s), '
                                'mayorista según ale, 27/10/2026.' % (partner.name, ref)))
result['rules'] = {}
for cuit, (label, note) in cuits.items():
    rule = Rule.search([('cuit', '=', cuit)], limit=1)
    if rule and not rule.active:
        rule.write({'active': True})
    if not rule:
        rule = Rule.create({'cuit': cuit, 'label': label, 'note': note})
    rule._compute_conflict()
    moved = rule._distri_apply()
    # never moved here: confirmed, tied to a payment of Online, or proposed
    # for a retail customer
    keep = Collection.search([('kind', '=', 'income'), ('payer_cuit', '=', cuit),
                              '|', '|', ('state', '=', 'confirmed'),
                              ('online_payment_id', '!=', False),
                              '&', ('state', '=', 'draft'), ('partner_id', '!=', False)])
    result.setdefault('cards_to_decide', {})[cuit] = [
        {'card': c.name, 'state': c.state, 'date': str(c.date_day), 'amount': c.amount,
         'customer': '%s %s' % (c.partner_id.ref or '', c.partner_id.name or ''),
         'payment': c.payment_id.name or '',
         'online': c.online_payment_id.name or c.online_payment_id.move_name or ''}
        for c in keep]
    result['rules'][cuit] = {
        'conflict': rule.conflict_note or False,
        'set_aside': len(moved),
        'amount_set_aside': round(sum(moved.mapped('amount')), 2),
        'left_on_board': Collection.search_count([
            ('kind', '=', 'income'), ('state', '=', 'draft'), ('payer_cuit', '=', cuit)]),
        'confirmed_untouched': Collection.search_count([
            ('kind', '=', 'income'), ('state', '=', 'confirmed'), ('payer_cuit', '=', cuit)]),
    }

Arca = env['distri.arca.name'].sudo()
result['arca_names'] = {}
for cuit, name in ARCA_NAMES.items():
    row = Arca.search([('cuit', '=', cuit)], limit=1)
    if row and row.name == name:
        result['arca_names'][cuit] = 'already'
    elif row:
        row.write({'name': name})
        result['arca_names'][cuit] = 'updated'
    else:
        Arca.create({'cuit': cuit, 'name': name})
        result['arca_names'][cuit] = 'created'

env['distri.balance.review'].sudo()._distri_refresh()
reviewed = env['distri.balance.review'].sudo().search([]).mapped('partner_id.ref')
result['still_in_review'] = sorted(set(REFS) & set(reviewed))
print('RESULT', json.dumps(result, ensure_ascii=False, indent=1, default=str))
if DO_COMMIT:
    env.cr.commit()
    print('COMMITTED')
else:
    env.cr.rollback()
    print('NOT COMMITTED (DO_COMMIT is not 1)')
