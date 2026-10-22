"""Posting the /pedido form in HTTP tests: since 19.0.1.29.0 every visitor
needs the token rendered in the page (controllers/main.py «enviar»)."""
import re

CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')


def page_token(case, page="/pedido"):
    match = CSRF_RE.search(case.url_open(page).text)
    return match.group(1) if match else ""


def post_pedido(case, data, page="/pedido"):
    """Like the browser: the page first (same session), then the form."""
    data = dict(data)
    if "csrf_token" not in data:
        data["csrf_token"] = page_token(case, page)
    return case.url_open("/pedido/enviar", data=data)
