"""«Cobro en efectivo» (19.0.1.92.0): the page /cobro.

Any internal user finds a customer, sees his account and registers the
cash he received. The rules and the posting live in models/cobro_efectivo.py
(distri.cash.collection); this file only translates HTTP. Plain HTML forms:
works on any phone, the POST carries Odoo's CSRF token, and the submit
button is disabled after the first tap (the server also refuses a repeat).
"""
from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.caja import is_cashier
from ..models.cobro_efectivo import DuplicateCashError, RouteCashWarning
from .main import FICHA
from .reparto import SHARED_CSS


def _money(amount):
    text = "{:,.2f}".format(amount or 0.0).replace(",", "X").replace(".", ",").replace("X", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return "$ " + text


class CobroEfectivo(http.Controller):

    def _check(self):
        user = request.env.user
        if user._is_public() or user.share:
            raise Forbidden()
        return user

    def _render(self, values, status=200):
        Cash = request.env["distri.cash.collection"]
        today = Cash.cash_today()
        values = dict({
            "empresa": FICHA["nombre"],
            "shared_css": SHARED_CSS,
            "user": request.env.user,
            # back to the user's own screen (/reparto, /cuentas) or the tablero;
            # 19.0.1.97.0: a cashier with his cash open comes from /caja
            "back": self._back(),
            "csrf_token": request.csrf_token(),
            "money": _money,
            "query": "",
            "results": None,
            "account": None,
            "error": "",
            "duplicate": "",
            # 19.0.1.93.0 (C1): the order is on a route, asked first
            "route_warning": "",
            "route_field": "",
            "done": None,
            "form": {},
            "today": today,
            "today_total": sum(today.mapped("amount")),
        }, **values)
        html = request.env["ir.qweb"]._render("distri_pedido.cobro_page", values)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _back(self):
        user = request.env.user
        if is_cashier(user) and request.env["distri.caja"].current_for(user):
            return "/caja"
        return user._distri_landing() or "/tablero"

    def _account(self, partner_id):
        return request.env["distri.cash.collection"].cash_account(partner_id)

    @http.route("/cobro", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, q="", cliente=None, hecho=None, **kw):
        self._check()
        Cash = request.env["distri.cash.collection"]
        values = {"query": q or ""}
        if hecho:
            try:
                done = Cash.sudo().browse(int(hecho)).exists()
            except (TypeError, ValueError):
                done = Cash.browse()
            # only the user's own receipt of what he just registered
            values["done"] = done if done and done.user_id == request.env.user else None
        if cliente:
            try:
                values["account"] = self._account(cliente)
            except UserError as exc:
                values["error"] = str(exc.args[0]) if exc.args else str(exc)
        elif q:
            values["results"] = Cash.cash_search(q)
        return self._render(values)

    @http.route("/cobro/registrar", type="http", auth="user", methods=["POST"], sitemap=False)
    def register(self, cliente=None, importe="", factura="", nota="", confirmado="",
                 ruta_ok="", forzar="", **kw):
        self._check()
        form = {"importe": importe or "", "factura": factura or "", "nota": nota or "",
                "ruta_ok": ruta_ok == "1", "forzar": forzar == "1"}
        try:
            account = self._account(cliente)
        except UserError as exc:
            return self._render({"error": exc.args[0] if exc.args else str(exc)}, status=400)
        try:
            # a refusal after the payment was created must leave nothing behind:
            # the response below is a normal one, so the request commits
            with request.env.cr.savepoint():
                collection = request.env["distri.cash.collection"].cash_register(
                    cliente, importe, invoice_id=factura or False, note=nota,
                    confirmed=confirmado == "1", route_ok=ruta_ok == "1", force=forzar == "1")
        except RouteCashWarning as exc:
            return self._render({"account": account, "form": form,
                                 "route_warning": exc.args[0], "route_field": exc.field},
                                status=409)
        except DuplicateCashError as exc:
            return self._render({"account": account, "form": form,
                                 "duplicate": exc.args[0]}, status=409)
        except (UserError, AccessError) as exc:
            return self._render({"account": account, "form": form,
                                 "error": exc.args[0] if exc.args else str(exc)}, status=400)
        return request.redirect("/cobro?hecho=%d" % collection.id, 303)
