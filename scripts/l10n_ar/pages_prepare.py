# THROWAWAY CLONE ONLY: one empty order per customer so every customer's
# account page (/pedido/<token>/cuenta) can be fetched and grepped.
import json, os
assert env.cr.dbname.endswith('_flow'), "only on the throwaway clone"
snap = json.load(open('/scripts/out/snap_after.json'))
SO = env['sale.order'].sudo()
out = {}
for pid in snap['balances']:
    p = env['res.partner'].browse(int(pid))
    o = SO.create({'partner_id': p.id})
    o._portal_ensure_token()
    out[pid] = dict(token=o.access_token, balance=round(p._distri_account_balance(), 2), name=p.name)
json.dump(out, open('/scripts/out/pages.json', 'w'))
env.cr.commit()
print("orders", len(out))
