"""19.0.1.129.4: the core database pages are a static Odoo page («Odoo»
title, logo, privacy links) that core renders even with list_db off, only
to say it is disabled. With list_db off they answer 404 (our error page)."""
from werkzeug.exceptions import NotFound

from odoo import http
from odoo.tools import config

from odoo.addons.web.controllers.database import Database


class DistriDatabase(Database):

    @http.route()
    def selector(self, **kw):
        if not config["list_db"]:
            raise NotFound()
        return super().selector(**kw)

    @http.route()
    def manager(self, **kw):
        if not config["list_db"]:
            raise NotFound()
        return super().manager(**kw)
