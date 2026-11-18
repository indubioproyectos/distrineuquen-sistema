"""19.0.1.99.0, run once on the upgrade (audit F14).

/contactus now redirects to the home page (controllers/tienda.py): Odoo's
sample contact page and its «thank you» page leave the sitemap.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    cr.execute("""
        UPDATE website_page SET website_indexed = false
         WHERE url IN ('/contactus', '/contactus-thank-you') AND website_indexed""")
    _logger.warning("19.0.1.99.0: %s sample contact pages out of the sitemap", cr.rowcount)
