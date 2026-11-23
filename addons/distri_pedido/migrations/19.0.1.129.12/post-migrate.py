"""19.0.1.129.12: the new brand logo on the company (res.company.logo: report
headers, invoices, remitos, backend). Once, on this upgrade: a logo the owner
uploads later stays (models/marca.py _distri_set_brand_logo)."""
from odoo import SUPERUSER_ID, api


def migrate(cr, version):
    if not version:
        return
    env = api.Environment(cr, SUPERUSER_ID, {})
    env["res.company"]._distri_set_brand_logo()
