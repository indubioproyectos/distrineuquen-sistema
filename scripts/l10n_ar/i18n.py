from odoo.tools.translate import TranslationImporter
A = '/usr/lib/python3/dist-packages/odoo/addons/%s/i18n/es_419.po'
ti = TranslationImporter(env.cr, verbose=False)
for m in ['l10n_ar', 'l10n_latam_invoice_document', 'l10n_latam_base', 'l10n_ar_website_sale', 'l10n_ar_stock']:
    ti.load_file(A % m, 'es_ES')
ti.save(overwrite=False)
env.cr.commit()
print("done")
