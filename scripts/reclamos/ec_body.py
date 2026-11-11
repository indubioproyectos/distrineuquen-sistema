import json, base64
refs = [25,147,21,335,47,46,12,24,26,519,49,133,396,62,217,158,427,561,152,60]
rep = env.ref("distri_pedido.report_estado_cuenta")
res = {}
for r in refs:
    p = env['res.partner'].search([('ref','=','ODOO-%d' % r)])
    if len(p) != 1:
        print('RESULT', r, 'partners', len(p)); continue
    w = env['distri.estado.cuenta'].create({'partner_id': p.id})
    pdf, _ = env['ir.actions.report']._render_qweb_pdf(rep, w.ids)
    fn = 'ODOO-%d.pdf' % r
    open('/out/' + fn, 'wb').write(pdf)
    res['ODOO-%d' % r] = {'name': p.name, 'saldo': p._distri_account_balance(), 'stmt': w.balance, 'cand': w.candidate_amount, 'file': fn}
    print('RESULT', r, p.name, res['ODOO-%d' % r]['saldo'], w.balance, w.candidate_amount)
open('/out/saldos.json','w').write(json.dumps(res, ensure_ascii=False))
env.cr.rollback()
