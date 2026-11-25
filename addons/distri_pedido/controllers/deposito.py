"""«Depósito» on the phone (19.0.1.109.0, owner-approved mockups «02 Depósito»,
«02 Entrega en depósito», «02 Cierre de caja depósito» and «12 Barra
inferior»): /deposito, on the kit (views/deposito_app.xml,
static/src/deposito/).

    /tablero (staff home)
      /deposito                         today's box, «Para entregar hoy»
                                        (search), «Entregados hoy», the
                                        link to /despacho
        /deposito/entrega/<order>  GET  «Entrega en depósito»
                                        (?mostrador=1 right after a counter
                                        sale)
                                   POST registers it
        /deposito/mostrador        GET  «Venta en mostrador»: quotations of
                                        real customers
          /deposito/mostrador/<order> POST confirm, pack as ordered and
                                        invoice, then the delivery form
        /deposito/reclamar         GET  19.0.1.129.37 (nueva_venta only): «Mis
                                        comprobantes a reclamar», the receipts
                                        «Lo manda después» left at the user's
                                        name
          .../<promise>/subir      POST its receipt, uploaded later
        /deposito/caja             GET  «Cierre de caja depósito» of the
                                        user's box of today
        /deposito/caja/<route>     GET  one depot box (any user's, as the
                                        backend list)
          .../devolucion/<ret>     POST «Recibido en el depósito» / «No volvió»
          .../cerrar               POST refused (19.0.1.109.0, owner: the
                                        cash goes to a cashier, who receives
                                        the box in /caja «Recibir plata»)

Same access as the backend buttons and «Reparto › Cierre de caja
depósito»: administración (gerencia implies it). Every rule is the model's:
the wizard distri.depot.delivery (models/deposito.py) and
distri.route.action_close_depot (models/reparto.py) run as the user, so
their own group checks apply; this file only translates HTTP. The forms
post plain HTML (the answer is a redirect or the page again with the
error); deposito.js sends them with fetch() (X-Distri-Fetch: 1) and gets
JSON, like /caja.

19.0.1.129.37 (distri_pedido.nueva_venta only; off = 129.36): a transfer
asks «Subir comprobante» (files attached on the spot, the staff upload's
pipeline) or «Lo manda después» (at the name of whoever delivers, with the
driver's rules): models/comprobantes_mostrador.py.

Until the module is updated the templates do not exist yet (the Python
code is loaded first): /deposito then sends to the backend and the
«Depósito» tile stays hidden (installed()).
"""
from urllib.parse import urlencode

from werkzeug.exceptions import Forbidden, NotFound

from odoo import Command, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models.caja import is_cashier, parse_amount
from ..models.comprobante_subir import MAX_FILE_BYTES
from ..models.comprobantes_mostrador import (
    ANSWER_LATER, ANSWER_UPLOAD, CTX_UPLOAD, LATER_ANSWER, bad_files_text, check_receipt_files,
    upload_summary)
from ..models.deposito import ADMIN_GROUP, COUNTER_REASON
from ..models.reparto import DEPOT_RECEIPT_ANSWERS, DEPOT_TO_CASHIER, _money
from ..models.reparto_cliente import today_ar
from .caja import _dia, _firmado, _dif_class, _hora
from .kit import depot_nav, kit_context
from .tablero import wants_tablero

LIMIT = 100
TEMPLATE = "distri_pedido.deposito_hub"
# the same question as the backend button «Venta en mostrador»
COUNTER_CONFIRM = ("Se confirma el pedido, se arma completo tal como se pidió, se valida el "
                   "remito y sale la factura. Después se registra la entrega y el cobro. ¿Seguir?")
STOP_CHIP = {"paid": "ok", "already_paid": "ok", "not_paid": "err"}
DELIVERY_CHIP = {"delivered_code": "ok", "delivered_no_code": "warn"}


def is_depot(user):
    """The depot role: who uses «Entrega en depósito», «Venta en mostrador»
    and «Cierre de caja depósito» in the backend."""
    return bool(user) and not user.share and user.has_group(ADMIN_GROUP)


def installed(env):
    """The kit templates of this version are loaded (module updated)."""
    return bool(env.ref(TEMPLATE, raise_if_not_found=False))


def deliverable_domain():
    """Packed and confirmed orders of a real customer, not dispatched and
    on no route (every visit, if any, unloaded back at the depot): what
    sale.order._distri_check_depot_delivery lets through, as a search."""
    return [("state", "=", "sale"), ("distri_armado", "=", "armado"),
            ("partner_id.distri_generic_partner", "=", False),
            ("distri_parcel_dispatched", "=", False),
            # 19.0.1.129.25: «Ya lo armé y ya se entregó» (models/factura_sin_armar.py)
            ("distri_entregado_fuera", "=", False),
            ("distri_stop_ids", "not any", ["|", ("state", "!=", "not_delivered"),
                                            ("unloaded", "=", False)])]


def pickup_count(env):
    """«Lo voy a retirar» orders waiting at the depot: the tile badge."""
    return env["sale.order"].sudo().search_count(
        deliverable_domain() + [("distri_entrega", "=", "retira")])


def _error_text(exc):
    return exc.args[0] if exc.args else str(exc)


def _qty(text):
    """A returned quantity («2», «1,5»); 0 when empty."""
    raw = str(text or "").strip().replace(",", ".")
    if not raw:
        return 0.0
    try:
        value = float(raw)
    except ValueError:
        raise UserError("Revisá la cantidad devuelta: «%s»." % text) from None
    if value < 0:
        raise UserError("La cantidad devuelta no puede ser negativa.")
    return value


class Deposito(http.Controller):

    # ---------------------------------------------------------------- helpers

    def _check(self):
        user = request.env.user
        if not is_depot(user):
            raise Forbidden()
        return user

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _render(self, template, values, status=200):
        user = request.env.user
        base = {
            "dn": kit_context("deposito.css", "deposito.js"),
            "user": user,
            "money": _money,
            "hora": _hora,
            "dia": _dia,
            "firmado": _firmado,
            "dif_class": _dif_class,
            "hoy": today_ar(),
            "inicio": "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero"),
            "csrf_token": request.csrf_token(),
            "error": "",
            "ok": "",
            "form": {},
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _done(self, url, message=""):
        """Success: to `url` (the parent, as a rule) with a message."""
        if message:
            url += ("&" if "?" in url else "?") + urlencode({"ok": message})
        if self._by_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url, 303)

    def _refuse(self, exc, page):
        text = _error_text(exc)
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=400)
        return page(text)

    def _form(self):
        form = request.httprequest.form
        return {key: form.get(key) for key in form}

    def _not_yet(self):
        """Before the module update: the backend action."""
        return request.redirect("/odoo/action-distri_pedido.action_depot_close", 303)

    def _order(self, order_id):
        order = request.env["sale.order"].browse(int(order_id)).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        return order

    # ---------------------------------------------------------------- hub

    def _boxes(self, user):
        Route = request.env["distri.route"].sudo()
        base = [("kind", "=", "depot"), ("driver_id", "=", user.id),
                ("company_id", "=", request.env.company.id)]
        today = Route.search(base + [("date", "=", today_ar())], order="id")
        older = Route.search(base + [("date", "<", today_ar()), ("state", "!=", "settled")],
                             order="date, id")
        return today, older

    def _deliverable(self, q=""):
        domain = deliverable_domain()
        q = (q or "").strip()
        if q:
            domain += ["|", ("name", "ilike", q), ("partner_id", "ilike", q)]
        orders = request.env["sale.order"].search(domain, order="date_order desc, id desc",
                                                  limit=LIMIT)
        # «Lo voy a retirar» first, as they are the ones the customer comes for
        return orders.sorted(lambda o: 0 if o.distri_entrega == "retira" else 1)

    def order_row(self, order):
        o = order.sudo()
        chips = []
        if o.distri_entrega == "retira":
            chips.append(("Retira en depósito", "ok"))
        elif o.distri_encomienda:
            chips.append(("Encomienda", ""))
        if o.distri_delivery_state == "pending":
            chips.append(("Volvió de la ruta", "warn"))
        return {
            "id": o.id,
            "name": o.name,
            "cliente": o.partner_id.name or "",
            "meta": " · ".join(x for x in (
                o.name, "%s %s" % (o.distri_bultos, "bulto" if o.distri_bultos == 1 else "bultos")
                if o.distri_bultos else "", _money(o._distri_amount_to_collect())) if x),
            "chips": chips,
            "retira": o.distri_entrega == "retira",
            "texto": ("%s %s" % (o.name, o.partner_id.name or "")).lower(),
        }

    def _nueva_venta_entry(self):
        from ..models.nueva_venta import nueva_venta_on, salidas_de
        env = request.env
        return nueva_venta_on(env) and "mostrador" in salidas_de(env.user)

    def _v137(self):
        """19.0.1.129.37: «Subir comprobante» / «Lo manda después»."""
        from ..models.nueva_venta import nueva_venta_on
        return nueva_venta_on(request.env)

    def _receipt_files(self):
        """[(filename, bytes)] of the «comprobantes» inputs (a byte over the
        limit is enough to refuse the file)."""
        out = []
        for storage in request.httprequest.files.getlist("comprobantes"):
            if storage and storage.filename:
                out.append((storage.filename[:120], storage.read(MAX_FILE_BYTES + 1)))
        return out

    def _reclamar_row(self, user):
        """«Mis comprobantes a reclamar · N · $ X» (None when there are none)."""
        count, total = request.env["distri.promised.transfer"]._distri_user_summary(user)
        if not count:
            return None
        return {"n": count, "total": total, "url": "/deposito/reclamar"}

    @http.route("/deposito", type="http", auth="user", methods=["GET"], sitemap=False)
    def hub(self, q="", ok="", aviso="", **kw):
        user = self._check()
        if not installed(request.env):
            return self._not_yet()
        today, older = self._boxes(user)
        open_today = today.filtered(lambda r: r.state != "settled")[:1]
        stops = today.stop_ids.sorted(lambda s: (s.visit_received_at or s.create_date, s.id),
                                      reverse=True)
        cobros = today.cash_collection_ids.sorted(lambda c: (c.create_date, c.id), reverse=True)
        rows = [self.order_row(o) for o in self._deliverable(q)]
        menu = request.env.ref("distri_pedido.menu_reparto_despacho", raise_if_not_found=False)
        return self._render("distri_pedido.deposito_hub", {
            "q": q, "ok": ok, "rows": rows,
            "retira": len([r for r in rows if r["retira"]]), "aviso": aviso,
            # 19.0.1.129.35: «Nueva venta» replaces the «Venta en mostrador»
            # entry while distri_pedido.nueva_venta is on (#27 obsolete);
            # /deposito/mostrador itself still answers
            "nueva_venta": self._nueva_venta_entry(),
            # 19.0.1.129.37: the receipts «Lo manda después» left at his name
            "reclamar": self._reclamar_row(user),
            "caja": open_today, "cerradas_hoy": today.filtered(lambda r: r.state == "settled"),
            "viejas": older, "stops": stops, "cobros": cobros,
            "por_caja": is_cashier(user),
            "despacho": bool(menu and menu.id in request.env["ir.ui.menu"]._visible_menu_ids()),
            "stop_chip": STOP_CHIP, "delivery_chip": DELIVERY_CHIP,
            "stop_label": dict(request.env["distri.route.stop"]._fields["state"].selection),
            "delivery_label": dict(
                request.env["distri.route.stop"]._fields["delivery_state"].selection),
            "nav": depot_nav(request.env, "entregas"),
        })

    # ---------------------------------------------------------------- delivery

    def _delivery_action(self, order, mostrador):
        """The backend button: its group and order checks, and the wizard's
        context (default_order_id, default_counter_sale)."""
        if mostrador:
            order = order.with_context(distri_counter_sale=True)
        return order.action_distri_depot_delivery()

    def _entrega(self, order, mostrador=False, error="", form=None, status=200):
        try:
            action = self._delivery_action(order, mostrador)
        except (UserError, AccessError) as exc:
            if status == 200:
                return request.redirect("/deposito?" + urlencode({"aviso": _error_text(exc)}), 303)
            action = {"context": {"default_order_id": order.id,
                                  "default_counter_sale": bool(mostrador)}}
        Wizard = request.env["distri.depot.delivery"].with_context(action["context"])
        # order_id and counter_sale must be asked for: the wizard's
        # default_get computes the amounts from them
        values = Wizard.default_get(["order_id", "counter_sale", "parcels", "order_amount",
                                     "previous_debt",
                                     "counter_present", "code_option", "transfer"])
        o = order.sudo()
        lines = order.order_line.filtered(lambda l: not l.display_type and l.product_uom_qty > 0)
        v137 = self._v137()
        form = form if form is not None else {
            "devolvio": "", "codigo_op": "code", "transferencia": "no",
            "presente": "1" if values.get("counter_present") else "",
            "bultos": str(values.get("parcels") or ""),
        }
        if v137 and form.get("comprobante") not in (ANSWER_UPLOAD, ANSWER_LATER):
            form = dict(form, comprobante=ANSWER_UPLOAD)
        return self._render("distri_pedido.deposito_entrega", {
            "order": order, "mostrador": bool(mostrador), "v": values,
            "saldo": (values.get("order_amount") or 0.0) + (values.get("previous_debt") or 0.0),
            "lineas": [{
                "id": l.id, "name": l.product_id.display_name, "qty": l.product_uom_qty,
                "uom": l.product_uom_id.name or "", "total": l.price_total,
                "unit": (l.price_total / l.product_uom_qty) if l.product_uom_qty else 0.0,
            } for l in lines],
            "has_code": bool(o.distri_receipt_hash),
            "locked": bool(o.distri_receipt_hash and o.distri_receipt_locked),
            "answers": DEPOT_RECEIPT_ANSWERS, "counter_reason": COUNTER_REASON,
            "error": error, "form": form,
            # 19.0.1.129.37 (nueva_venta): «Subir comprobante» / «Lo manda después»
            "v137": v137,
        }, status=status)

    @http.route("/deposito/entrega/<int:order_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def entrega_page(self, order_id, mostrador="", **kw):
        self._check()
        if not installed(request.env):
            return self._not_yet()
        return self._entrega(self._order(order_id), mostrador=mostrador == "1")

    def _wizard_values(self, order, form, mostrador):
        """The page's answers as the wizard's fields (models/deposito.py).
        Only what the page cannot leave to the model: amounts typed the
        Argentine way and the return rows."""
        transfer = form.get("transferencia") if form.get("transferencia") in ("no", "yes") else "no"
        returns_answer = form.get("devolvio") if form.get("devolvio") in ("none", "yes") else False
        values = {
            "returns_answer": returns_answer,
            "code_option": "without_code" if form.get("codigo_op") == "without_code" else "code",
            "code": (form.get("codigo") or "").strip()[:4],
            "cash": parse_amount(request.env, form.get("efectivo"), allow_zero=True),
            "transfer": transfer,
            "no_payment": form.get("no_pago") == "1",
            "comment": " ".join((form.get("comentario") or "").split())[:250] or False,
            "returns_received": form.get("recibido") == "1",
        }
        if transfer == "yes":
            raw = (form.get("importe_transf") or "").strip()
            values["transfer_amount"] = parse_amount(request.env, raw) if raw else 0.0
            answer = form.get("comprobante")
            values["receipt_answer"] = answer if answer in dict(DEPOT_RECEIPT_ANSWERS) else False
        if mostrador:
            values["counter_present"] = form.get("presente") == "1"
            try:
                values["parcels"] = int(form.get("bultos") or 0)
            except ValueError:
                raise UserError("Poné en cuántos bultos quedó el pedido.") from None
        if returns_answer == "yes":
            rows = []
            for line in order.order_line.filtered(
                    lambda l: not l.display_type and l.product_uom_qty > 0):
                quantity = _qty(form.get("dev_%s" % line.id))
                if not quantity:
                    continue
                reason = " ".join((form.get("motivo_%s" % line.id) or "").split())[:120]
                if not reason:
                    raise UserError("Poné el motivo de lo que devolvió (%s)."
                                    % line.product_id.display_name)
                rows.append(Command.create({"order_line_id": line.id, "quantity": quantity,
                                            "reason": reason}))
            values["return_ids"] = rows
        return values

    @http.route("/deposito/entrega/<int:order_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def entrega(self, order_id, mostrador="", **kw):
        self._check()
        order = self._order(order_id)
        mostrador = mostrador == "1"
        form = self._form()
        answers, files, results = dict(form), [], []
        try:
            if self._v137() and form.get("transferencia") == "yes":
                # 19.0.1.129.37: the page's two answers as the wizard's
                if form.get("comprobante") == ANSWER_UPLOAD:
                    files = self._receipt_files()
                    bad = check_receipt_files(files)
                    if bad:
                        raise UserError(bad_files_text(bad))
                    # a valid answer for the wizard; the visit says «photo»
                    answers["comprobante"] = DEPOT_RECEIPT_ANSWERS[0][0]
                elif form.get("comprobante") == ANSWER_LATER:
                    answers["comprobante"] = LATER_ANSWER
                else:
                    raise UserError("Elegí «Subir comprobante» o «Lo manda después».")
            with request.env.cr.savepoint():
                action = self._delivery_action(order, mostrador)
                context = dict(action["context"], **({CTX_UPLOAD: True} if files else {}))
                Wizard = request.env["distri.depot.delivery"].with_context(context)
                wizard = Wizard.create(self._wizard_values(order, answers, mostrador))
                wizard.action_confirm()
                if files:
                    results = request.env["distri.whatsapp.message"]._distri_depot_upload(
                        order, files, amount=wizard.transfer_amount)
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(exc, lambda text: self._entrega(
                order, mostrador=mostrador, error=text, form=form, status=400))
        message = "%s entregado a %s." % (order.name, order.partner_id.name)
        if results:
            message += " Comprobante: %s." % upload_summary(results)
        elif answers.get("comprobante") == LATER_ANSWER and self._v137():
            message += " El comprobante queda a tu cargo hasta que llegue."
        return self._done("/deposito", message)

    # ---------------------------------------------------------------- counter sale

    def _mostrador(self, q="", error="", status=200):
        domain = [("state", "in", ("draft", "sent")), ("distri_armado", "=", "pendiente"),
                  ("partner_id.distri_generic_partner", "=", False)]
        q = (q or "").strip()
        if q:
            domain += ["|", ("name", "ilike", q), ("partner_id", "ilike", q)]
        orders = request.env["sale.order"].search(domain, order="date_order desc, id desc",
                                                  limit=LIMIT)
        orders = orders.filtered(lambda o: o.order_line.filtered(
            lambda l: not l.display_type and l.product_uom_qty))
        from ..models.pedido_whatsapp import is_staff
        from .fichas import record_link
        return self._render("distri_pedido.deposito_mostrador", {
            "q": q, "error": error, "confirmar": COUNTER_CONFIRM,
            "rows": [{
                "id": o.id, "cliente": o.partner_id.name or "",
                "meta": " · ".join((o.name, "%s %s" % (
                    len(o.order_line.filtered(lambda l: not l.display_type)),
                    "producto" if len(o.order_line.filtered(lambda l: not l.display_type)) == 1
                    else "productos"), _money(o.amount_total))),
                "texto": ("%s %s" % (o.name, o.partner_id.name or "")).lower(),
                "sin_wa": o._distri_wa_needs_number(),
                # 19.0.1.129.34: the order's kit screen with ui_v2 on
                "odoo_url": record_link(request.env, "pedido", o.id, "/deposito/mostrador"),
            } for o in orders],
            "tomar": is_staff(request.env.user),
            "nav": depot_nav(request.env, "entregas"),
        }, status=status)

    @http.route("/deposito/mostrador", type="http", auth="user", methods=["GET"], sitemap=False)
    def mostrador_page(self, q="", **kw):
        self._check()
        if not installed(request.env):
            return self._not_yet()
        return self._mostrador(q=q)

    @http.route("/deposito/mostrador/<int:order_id>", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def mostrador(self, order_id, **kw):
        self._check()
        order = self._order(order_id)
        try:
            with request.env.cr.savepoint():
                # the backend button without its WhatsApp question (a wizard
                # this page cannot show yet, batch 3): the list says «Sin
                # WhatsApp» first, and with «pedido_requiere_whatsapp» on the
                # model still refuses a customer without a number
                action = order.action_distri_counter_sale()
                if (action or {}).get("res_model") != "distri.depot.delivery":
                    raise UserError("No se pudo hacer la venta en mostrador desde acá: abrí el "
                                    "presupuesto en el sistema.")
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(exc, lambda text: self._mostrador(error=text, status=400))
        url = "/deposito/entrega/%s?mostrador=1" % order.id
        if self._by_fetch():
            return self._json({"ok": True, "url": url, "down": True})
        return request.redirect(url, 303)

    # ---------------------------------------------------------------- «Mis comprobantes a reclamar»

    def _reclamar(self, ok="", error="", status=200):
        user = request.env.user
        items = request.env["distri.promised.transfer"]._distri_user_items(user)
        rows = [item._distri_user_row() for item in items]
        return self._render("distri_pedido.deposito_reclamar", {
            "rows": rows, "total": sum(r["importe"] for r in rows),
            "ok": ok[:300], "error": error,
            "nav": depot_nav(request.env, "entregas"),
        }, status=status)

    @http.route("/deposito/reclamar", type="http", auth="user", methods=["GET"], sitemap=False)
    def reclamar_page(self, ok="", **kw):
        """19.0.1.129.37: the receipts «Lo manda después» left at the user's
        name (nueva_venta only)."""
        if not self._v137():
            raise NotFound()
        self._check()
        if not installed(request.env):
            return self._not_yet()
        return self._reclamar(ok=ok)

    @http.route("/deposito/reclamar/<int:promise_id>/subir", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def reclamar_subir(self, promise_id, **kw):
        """The receipt of one of them, uploaded now: the same upload as at
        the delivery (it names the order, so it resolves that one)."""
        if not self._v137():
            raise NotFound()
        user = self._check()
        promise = request.env["distri.promised.transfer"].sudo().browse(promise_id).exists()
        if not promise:
            raise NotFound()
        try:
            with request.env.cr.savepoint():
                promise = promise._distri_check_user_item(user)
                files = self._receipt_files()
                bad = check_receipt_files(files)
                if bad:
                    raise UserError(bad_files_text(bad))
                results = request.env["distri.whatsapp.message"]._distri_depot_upload(
                    promise.order_id, files, amount=promise.amount)
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(exc, lambda text: self._reclamar(error=text, status=400))
        return self._done("/deposito/reclamar", "%s: %s." % (
            promise.order_id.name, upload_summary(results) or "nada nuevo"))

    # ---------------------------------------------------------------- closing

    def _box(self, route_id):
        route = request.env["distri.route"].browse(int(route_id)).exists()
        if not route or route.kind != "depot" or route.company_id != request.env.company:
            raise NotFound()
        return route

    def _caja(self, box, ok="", error="", status=200):
        user = request.env.user
        Route = request.env["distri.route"].sudo()
        older = Route.browse()
        if box:
            older = Route.search([("kind", "=", "depot"), ("driver_id", "=", box.driver_id.id),
                                  ("state", "!=", "settled"), ("date", "<", box.date),
                                  ("company_id", "=", request.env.company.id)], order="date, id")
        b = box.sudo()
        cases = dict(request.env["distri.route.case"]._fields["kind"].selection)
        case_states = dict(request.env["distri.route.case"]._fields["state"].selection)
        returns = dict(request.env["distri.route.return"]._fields["settlement_state"].selection)
        return self._render("distri_pedido.deposito_caja", {
            "box": b, "viejas": older, "own": bool(box) and box.driver_id == user,
            # 19.0.1.109.0 (owner): nobody closes a box here. A cashier's
            # own box closes with his «Caja del día» (models/caja.py
            # close_session); any other is received by a cashier in /caja
            # «Recibir plata» (receive_route)
            "con_caja": bool(box) and is_cashier(box.driver_id),
            "recibir": bool(box) and box.state != "settled" and is_cashier(user)
            and box.driver_id != user,
            "a_cajero": DEPOT_TO_CASHIER,
            "abierta": bool(box) and box.state != "settled",
            "pendientes": b.return_ids.filtered(lambda r: r.settlement_state == "pending")
            if b else b,
            "case_kind": cases, "case_state": case_states, "ret_state": returns,
            "stop_chip": STOP_CHIP, "delivery_chip": DELIVERY_CHIP,
            "stop_label": dict(request.env["distri.route.stop"]._fields["state"].selection),
            "delivery_label": dict(
                request.env["distri.route.stop"]._fields["delivery_state"].selection),
            "ok": ok, "error": error,
            "nav": depot_nav(request.env, "entregas"),
        }, status=status)

    @http.route(["/deposito/caja", "/deposito/caja/<int:route_id>"], type="http", auth="user",
                methods=["GET"], sitemap=False)
    def caja_page(self, route_id=None, ok="", **kw):
        user = self._check()
        if not installed(request.env):
            return self._not_yet()
        if route_id:
            box = self._box(route_id)
        else:
            today, older = self._boxes(user)
            box = (today.filtered(lambda r: r.state != "settled")[:1] or older[:1]
                   or today[-1:]).sudo(False)
        return self._caja(box, ok=ok)

    @http.route("/deposito/caja/<int:route_id>/devolucion/<int:return_id>", type="http",
                auth="user", methods=["POST"], sitemap=False)
    def caja_devolucion(self, route_id, return_id, que="", **kw):
        self._check()
        box = self._box(route_id)
        ret = box.return_ids.filtered(lambda r: r.id == return_id)
        if not ret:
            raise NotFound()
        try:
            with request.env.cr.savepoint():
                if que == "recibido":
                    ret.action_received()
                elif que == "no_volvio":
                    ret.action_missing()
                else:
                    raise UserError("Elegí si volvió al depósito o no.")
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(exc, lambda text: self._caja(box, error=text, status=400))
        return self._done("/deposito/caja/%s" % box.id,
                          "Recibido en el depósito." if que == "recibido" else
                          "Quedó como «No volvió»: se abrió un caso.")

    @http.route("/deposito/caja/<int:route_id>/cerrar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def caja_cerrar(self, route_id, **kw):
        """19.0.1.109.0 (owner): always refused by the model, for everybody
        (distri.route.action_close_depot): the cash goes to a cashier."""
        self._check()
        box = self._box(route_id)
        try:
            with request.env.cr.savepoint():
                box.action_close_depot()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(exc, lambda text: self._caja(box, error=text, status=400))
        return self._done("/deposito")
