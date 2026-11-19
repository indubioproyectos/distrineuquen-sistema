"""The orange «ENTORNO DE PRÁCTICA» banner on EVERY HTML page.

The staff pages of distri_pedido are standalone documents (the kit, the
legacy apps, /pedido, the website and the Odoo backend), so instead of
inheriting each template the banner is inserted right after <body> of every
HTML response, once, here. Only while distri_practica.activo = '1'.
"""
import logging
import re

from odoo import models
from odoo.http import request

from ..controllers.main import banner_html, is_active

_logger = logging.getLogger(__name__)

_BODY_RE = re.compile(rb"<body\b[^>]*>", re.IGNORECASE)
_MARK = b'id="dn-practica-banner"'


class IrHttp(models.AbstractModel):
    _inherit = "ir.http"

    @classmethod
    def _post_dispatch(cls, response):
        super()._post_dispatch(response)
        try:
            cls._distri_practica_banner(response)
        except Exception:  # the banner must never break a page
            _logger.exception("distri_practica: banner not inserted")

    @classmethod
    def _distri_practica_banner(cls, response):
        if not request or not getattr(request, "env", None) or not request.db:
            return
        if getattr(response, "direct_passthrough", False) or getattr(response, "is_streamed", False):
            return
        if getattr(response, "mimetype", "") != "text/html" or response.status_code >= 500:
            return
        # pages shown inside an iframe (website editor preview, PDF viewer...)
        if request.httprequest.headers.get("Sec-Fetch-Dest") == "iframe":
            return
        if not is_active(request.env):
            return
        data = response.get_data()
        if _MARK in data:
            return
        match = _BODY_RE.search(data)
        if not match:
            return
        banner = str(banner_html()).encode()
        response.set_data(data[:match.end()] + banner + data[match.end():])
