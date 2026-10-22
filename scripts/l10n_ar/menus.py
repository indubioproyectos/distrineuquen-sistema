admin = env.ref('base.user_admin')
M = env['ir.ui.menu'].with_user(admin).with_context(lang='es_ES')
vis = M.search([])  # visible for admin
root = env.ref('account.menu_finance')
paths = sorted(m.complete_name for m in vis if m.complete_name.split('/')[0] == root.with_context(lang='es_ES').name)
open('/tmp/menus_%s.txt' % env.cr.dbname, 'w').write('\n'.join(paths) + '\n')
print(env.cr.dbname, len(paths))
