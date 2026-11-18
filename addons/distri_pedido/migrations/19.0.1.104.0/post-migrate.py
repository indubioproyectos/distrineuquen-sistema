"""19.0.1.104.0, run once on the upgrade (public website, phase 1).

The website record still had the retired host as its domain
(https://distrineuquen.piadevs.com, which now only redirects to
distrineuquen.com). Odoo's robots.txt compares that domain with the address
of the request and, as they differ, answered «Disallow: /» on
distrineuquen.com: no search engine could index the new site. The domain
becomes web.base.url (the canonical address, frozen in the system
parameters). The cached sitemaps are dropped so the next /sitemap.xml lists
the new public pages («/», «/revendedor») and no longer /terms.
"""
import logging
from urllib.parse import urlsplit

from odoo import SUPERUSER_ID, api

_logger = logging.getLogger(__name__)

RETIRED_HOST_SUFFIX = ".piadevs.com"


def migrate(cr, version):
    cr.execute("SELECT value FROM ir_config_parameter WHERE key = 'web.base.url'")
    row = cr.fetchone()
    base = (row[0] if row else "").rstrip("/")
    host = urlsplit(base).hostname or ""
    if base and not host.endswith(RETIRED_HOST_SUFFIX):
        cr.execute("SELECT id, domain FROM website WHERE domain ILIKE %s",
                   ("%" + RETIRED_HOST_SUFFIX + "%",))
        for website_id, domain in cr.fetchall():
            if (urlsplit(domain if "//" in domain else "//" + domain).hostname or "").endswith(
                    RETIRED_HOST_SUFFIX):
                cr.execute("UPDATE website SET domain = %s WHERE id = %s", (base, website_id))
                _logger.warning("19.0.1.104.0: website %s domain %s -> %s", website_id, domain, base)
    env = api.Environment(cr, SUPERUSER_ID, {})
    cached = env["ir.attachment"].search([("type", "=", "binary"), ("url", "=like", "/sitemap-%.xml")])
    _logger.warning("19.0.1.104.0: %s cached sitemaps dropped", len(cached))
    cached.unlink()
