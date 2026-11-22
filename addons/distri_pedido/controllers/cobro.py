"""«Cobro en efectivo» (19.0.1.92.0): the page /cobro.

The staff finds a customer, sees his account and registers what he received.
The rules and the posting live in models/cobro_efectivo.py
(distri.cash.collection) and, since 19.0.1.128.0, models/cobrar.py
(«Cobrar» in one step: cash plus transfers with their receipts, to «A
cuenta», an invoice of the system or one of Odoo Online); this file only
translates HTTP. Plain HTML forms: works on any phone, the POST carries
Odoo's CSRF token, and the submit button is disabled after the first tap
(the server also refuses a repeat).

19.0.1.128.0 (audit 2026-11-22): only cajero, reparto, vendedor,
administración, gerencia and Sistemas open /cobro and its routes
(`can_collect`); anyone else gets the «no es para tu usuario» page (403).
The receipts of a try that asked a question or was refused stay stored
(«recibo_N»), so they are not attached again.
"""
import json
from datetime import datetime, time, timezone

from werkzeug.exceptions import Forbidden

from odoo import http
from odoo.exceptions import AccessError, UserError
from odoo.http import request

from ..models.caja import is_cashier
from ..models.cliente_provisorio import PROVISIONAL_LABEL, NewCustomerMatches
from ..models.cobrar import COLLECT_DENIED, CREDIT_OK, QUESTIONS, can_collect, question_field
from ..models.cobro_efectivo import DuplicateCashError, OverDebtWarning, RouteCashWarning
from ..models.comprobante_subir import MAX_FILE_BYTES
from ..models.reparto_cliente import AR_TZ, today_ar
from .main import FICHA
from .reparto import SHARED_CSS

# the form value of an Odoo Online invoice (models/cobro_efectivo.py cash_account)
ONLINE_PREFIX = "online-"
# transfer slots on the page (the engine takes up to cobrar.MAX_TRANSFERS)
PAGE_TRANSFERS = 3


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
        if not can_collect(user):
            # 19.0.1.128.0: the module's «no es para tu usuario» page
            # (views/errores.xml), also on these non-website routes
            html = request.env["ir.ui.view"]._render_template("distri_pedido.error_page", {
                "status": 403, "logged": True, "user_name": user.name,
                "home": user._distri_landing() or "/tablero"})
            raise Forbidden(response=request.make_response(html, status=403, headers=[
                ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")]))
        return user

    def _today(self):
        """The user's collections of today: the one-step ones and the /cobro
        cash of his open box not part of one (a new customer's)."""
        user = request.env.user
        start = datetime.combine(today_ar(), time()).replace(tzinfo=AR_TZ).astimezone(
            timezone.utc).replace(tzinfo=None)
        cobros = request.env["distri.cobro"].sudo().search(
            [("user_id", "=", user.id), ("create_date", ">=", start)], limit=10)
        cash = request.env["distri.cash.collection"].cash_today() - cobros.cash_collection_id
        rows = sorted(list(cobros) + list(cash), key=lambda r: r.create_date, reverse=True)
        return rows[:10]

    def _render(self, values, status=200):
        today = self._today()
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
            # 19.0.1.99.0 (audit F4): more than the debt, asked first
            "over_warning": "",
            # 19.0.1.128.0: the v126 warnings of the transfers, asked first
            "credit_warning": "",
            "transfers": [],
            "page_transfers": PAGE_TRANSFERS,
            "cashier": is_cashier(request.env.user),
            "done": None,
            "form": {},
            # 19.0.1.103.0: «Cliente nuevo» (models/cliente_provisorio.py)
            "nuevo": False,
            "matches": [],
            "provisional_label": PROVISIONAL_LABEL,
            "today": today,
            "today_total": sum(r.amount for r in today),
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
    def page(self, q="", cliente=None, hecho=None, efectivo=None, **kw):
        self._check()
        Cash = request.env["distri.cash.collection"]
        values = {"query": q or ""}
        # «hecho»: a one-step collection (19.0.1.128.0); «efectivo»: the
        # cash of a new customer
        model, wanted = ("distri.cobro", hecho) if hecho else ("distri.cash.collection", efectivo)
        if wanted:
            try:
                done = request.env[model].sudo().browse(int(wanted)).exists()
            except (TypeError, ValueError):
                done = request.env[model].browse()
            # only the user's own receipt of what he just registered
            values["done"] = done if done and done.user_id == request.env.user else None
        if kw.get("nuevo"):
            # «Cliente nuevo»: what was searched fills the name or the phone
            text = " ".join((q or "").split())
            digits = sum(ch.isdigit() for ch in text)
            values["nuevo"] = True
            values["form"] = {"telefono": text} if digits >= 6 else {"nombre": text}
            return self._render(values)
        if cliente:
            try:
                values["account"] = self._account(cliente)
            except UserError as exc:
                values["error"] = str(exc.args[0]) if exc.args else str(exc)
        elif q:
            values["results"] = Cash.cash_search(q)
        return self._render(values)

    def _transfers(self, params):
        """The transfer slots of the form: file «comprobante_N» (or the
        receipt stored by an earlier try, «recibo_N»), «importe_transf_N»,
        «giro_N»."""
        files = request.httprequest.files
        out = []
        for n in range(1, PAGE_TRANSFERS + 1):
            storage = files.get("comprobante_%d" % n)
            raw_file = None
            if storage and storage.filename:
                raw_file = (storage.filename, storage.read(MAX_FILE_BYTES + 1))
            out.append({"amount": params.get("importe_transf_%d" % n) or "",
                        "receipt_id": params.get("recibo_%d" % n) or False,
                        "file": raw_file, "rotation": params.get("giro_%d" % n) or 0})
        return out

    @staticmethod
    def _stored_rows(stored):
        return [{"receipt_id": t["receipt"].id, "amount": t["amount"],
                 "name": t["receipt"].media_filename or "comprobante"} for t in stored]

    @http.route("/cobro/registrar", type="http", auth="user", methods=["POST"], sitemap=False)
    def register(self, cliente=None, importe="", factura="", nota="", confirmado="",
                 ruta_ok="", forzar="", deuda_ok="", acreditar_ok="", **kw):
        self._check()
        form = {"importe": importe or "", "factura": factura or "", "nota": nota or "",
                "ruta_ok": ruta_ok == "1", "forzar": forzar == "1", "deuda_ok": deuda_ok == "1",
                CREDIT_OK: acreditar_ok == "1"}
        try:
            account = self._account(cliente)
        except UserError as exc:
            return self._render({"error": exc.args[0] if exc.args else str(exc)}, status=400)
        # 19.0.1.125.0: «online-<id>» is an Odoo Online invoice of the statement
        destination, invoice_id, history_id = "account", False, False
        if str(factura or "").startswith(ONLINE_PREFIX):
            destination, history_id = "online", factura[len(ONLINE_PREFIX):]
        elif factura:
            destination, invoice_id = "lab", factura
        Cobro = request.env["distri.cobro"]
        try:
            # the receipts are stored even if the rest is refused below
            stored = Cobro.collect_receipts(cliente, self._transfers(kw))
        except (UserError, AccessError) as exc:
            return self._render({"account": account, "form": form,
                                 "error": exc.args[0] if exc.args else str(exc)}, status=400)
        values = {"account": account, "form": form, "transfers": self._stored_rows(stored)}
        try:
            # a refusal after the payments were created must leave nothing
            # behind: the response below is a normal one, so the request commits
            with request.env.cr.savepoint():
                cobro = Cobro.collect(
                    cliente, destination, invoice_id=invoice_id, history_id=history_id,
                    cash_amount=importe, transfers=stored, note=nota,
                    confirmed=confirmado == "1", route_ok=ruta_ok == "1", force=forzar == "1",
                    over_ok=deuda_ok == "1", credit_ok=acreditar_ok == "1")
        except QUESTIONS as exc:
            field = question_field(exc)
            key = {"deuda_ok": "over_warning", "confirmado": "duplicate",
                   CREDIT_OK: "credit_warning"}.get(field, "route_warning")
            values[key] = exc.args[0]
            if isinstance(exc, RouteCashWarning):
                values["route_field"] = field
            return self._render(values, status=409)
        except (UserError, AccessError) as exc:
            return self._render(dict(values, error=exc.args[0] if exc.args else str(exc)),
                                status=400)
        return request.redirect("/cobro?hecho=%d" % cobro.id, 303)

    @http.route("/cobro/leer", type="http", auth="user", methods=["POST"], sitemap=False)
    def read_receipt(self, **kw):
        """19.0.1.128.0: the amount read from a chosen receipt (nothing
        stored), with the OCR time cap; JSON."""
        user = request.env.user
        if user._is_public() or user.share or not can_collect(user):
            return self._json({"ok": False, "error": COLLECT_DENIED}, status=403)
        storage = request.httprequest.files.get("archivo")
        raw = storage.read(MAX_FILE_BYTES + 1) if storage and storage.filename else b""
        preview = request.env["distri.whatsapp.message"]._distri_upload_preview(
            raw, kw.get("giro") or 0)
        return self._json({"ok": True, "amount_text": preview.get("amount_text") or "",
                           "amount_sure": bool(preview.get("amount_sure")),
                           "notice": preview.get("notice") or ""})

    def _json(self, data, status=200):
        return request.make_response(json.dumps(data), status=status, headers=[
            ("Content-Type", "application/json; charset=utf-8"), ("Cache-Control", "no-store")])

    @http.route("/cobro/nuevo", type="http", auth="user", methods=["POST"], sitemap=False)
    def register_new(self, nombre="", telefono="", domicilio="", localidad="", importe="",
                     nota="", nuevo_ok="", deuda_ok="", confirmado="", **kw):
        """19.0.1.103.0: «Cliente nuevo»: a provisional customer and the cash
        «a cuenta» on it (models/cliente_provisorio.py)."""
        self._check()
        form = {"nombre": nombre or "", "telefono": telefono or "", "domicilio": domicilio or "",
                "localidad": localidad or "", "importe": importe or "", "nota": nota or "",
                "nuevo_ok": nuevo_ok == "1", "deuda_ok": deuda_ok == "1"}
        values = {"nuevo": True, "form": form}
        try:
            with request.env.cr.savepoint():
                collection = request.env["distri.cash.collection"].cash_register_new(
                    nombre, importe, phone=telefono, street=domicilio, city=localidad, note=nota,
                    new_ok=nuevo_ok == "1", over_ok=deuda_ok == "1",
                    confirmed=confirmado == "1")
        except NewCustomerMatches as exc:
            return self._render(dict(values, matches=exc.matches, error=""), status=409)
        except OverDebtWarning as exc:
            return self._render(dict(values, over_warning=exc.args[0]), status=409)
        except DuplicateCashError as exc:
            return self._render(dict(values, duplicate=exc.args[0]), status=409)
        except (UserError, AccessError) as exc:
            return self._render(dict(values, error=exc.args[0] if exc.args else str(exc)),
                                status=400)
        return request.redirect("/cobro?efectivo=%d" % collection.id, 303)
