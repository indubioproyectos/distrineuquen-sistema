"""Posting the /pedido form in HTTP tests: since 19.0.1.29.0 every visitor
needs the token rendered in the page (controllers/main.py «enviar»)."""
import re

from odoo.addons.distri_pedido.controllers.main import PARAM_PAUSADO
from odoo.addons.distri_pedido.models.agil_tienda import PARAM_CATALOGO_AGIL

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
ENVIO_RE = re.compile(r'name="envio" value="([^"]+)"')


def page_token(case, page="/pedido"):
    match = CSRF_RE.search(case.url_open(page).text)
    return match.group(1) if match else ""


def page_tokens(case, page="/pedido"):
    """(csrf token, form id) of one page shown (19.0.1.93.0: `envio`)."""
    html = case.url_open(page).text
    csrf, envio = CSRF_RE.search(html), ENVIO_RE.search(html)
    return (csrf.group(1) if csrf else ""), (envio.group(1) if envio else "")


def post_pedido(case, data, page="/pedido"):
    """Like the browser: the page first (same session), then the form."""
    data = dict(data)
    if "csrf_token" not in data:
        data["csrf_token"], envio = page_tokens(case, page)
        data.setdefault("envio", envio)
    return case.url_open("/pedido/enviar", data=data)


def abrir_pedidos(env):
    """The order screen open and showing the usual catalog: a copy of
    production may have it paused (PARAM_PAUSADO) or on the AgilPedido copy
    (PARAM_CATALOGO_AGIL). Tests of that copy turn it on themselves."""
    params = env["ir.config_parameter"].sudo()
    params.set_param(PARAM_PAUSADO, "0")
    params.set_param(PARAM_CATALOGO_AGIL, "0")


def ensure_company_chart(env, company=None):
    """19.0.1.129.26 (#109): HTTP tests that run «on the copy's company»
    (caja, cash payments, cuentas) need its chart and a cash journal. A copy
    of production has them; a fresh test database has the generic chart,
    which in Odoo 19 brings no cash journal, so one is created like
    production's CSH1 (rolled back with the test class). No-op when the
    company already has a cash journal."""
    company = company or env.company
    Journal = env["account.journal"].sudo()
    if Journal.search_count([("type", "=", "cash"), ("company_id", "=", company.id)], limit=1):
        return
    if not company.chart_template:
        env["account.chart.template"].try_loading("generic_coa", company=company,
                                                  install_demo=False)
    if not Journal.search_count([("type", "=", "cash"), ("company_id", "=", company.id)],
                                limit=1):
        Journal.create({"name": "Efectivo", "code": "CSH1", "type": "cash",
                        "company_id": company.id})


class PedidoAbiertoMixin:
    """HTTP tests of /pedido: the screen is open, with the usual catalog,
    whatever the database says (19.0.1.91.0). Goes first in the bases, before HttpCase."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        abrir_pedidos(cls.env)
