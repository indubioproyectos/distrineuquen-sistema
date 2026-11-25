"""19.0.1.129.41: «Contactos» on the kit, behind distri_pedido.ui_v2 (OFF:
every route answers 404 and every link is the one of 19.0.1.129.40), so the
daily work never needs Odoo's Contacts app. models/contactos.py has the rules.

    /tablero
      /contactos                 GET   the list: search and filters, one page
                                       at a time (?q=&tipo=&localidad=...)
        /contactos/buscar        GET   JSON: one page of the list (the screen
                                       asks it while typing)
        /contactos/nuevo         GET   «Nuevo contacto»
                                 POST  likely existing customers first, then
                                       19.0.1.129.40's «Nuevo cliente» path
      /clientes/<id>             (controllers/fichas.py) the customer screen
        /clientes/<id>/editar    GET   the form of what this user may change
                                 POST  saved, back to /clientes/<id>
        /clientes/<id>/confirmar POST  «Confirmar» a customer «a confirmar»
        /clientes/<id>/unir      GET   «Unir con…»: who it may be
                                 POST  merged (only without accounting), or
                                       just noted on both

Who does what, exactly as in the backend:
- administración and gerencia (base.group_partner_manager) edit every field
  of the form, written as themselves: the backend's ACLs, record rules,
  constraints and tracking apply. Sistemas too when the backend lets him
  write contacts;
- a seller (internal or external) edits only the WhatsApp, address and town
  of a customer he reaches (his portfolio / his customers), written with
  sudo as «Nueva venta»'s «Datos del cliente» does since 19.0.1.129.40, with
  a chatter note. The backend still gives him no write on contacts;
- packers and drivers: 403, as on the record screens;
- «Nuevo contacto» is 19.0.1.129.40's «Nuevo cliente» (controllers/
  nueva_venta.py crear_cliente): a seller's customer is «a confirmar»;
- «Confirmar» and «Unir con…» are the backend actions of models/
  cliente_provisorio.py, for administración and gerencia. The kit merges
  only a customer with no accounting at all (models/contactos.py
  _distri_merge_refusal): Odoo's partner merge never moves an entry here.
Posts carry the CSRF token (Odoo's check for type="http" POST routes).
"""
import time

from markupsafe import Markup
from werkzeug.exceptions import Forbidden, NotFound

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request
from odoo.tools import plaintext2html

from ..models import contactos as ct
from ..models.reparto import _money
from .fichas import _home, ficha_url
from .kit import kit_context, page_nav, ui_v2_on

WEEKDAYS = ("Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom")
TIPO_LABELS = (("clientes", "Clientes"), ("proveedores", "Proveedores"),
               ("otros", "Otros contactos"), ("todos", "Todos"))
FILTRO_LABELS = (
    ("mios", "Mis clientes"),
    ("deuda", "Con deuda"),
    ("confirmar", "A confirmar"),
    ("sin_wa", "Sin WhatsApp"),
    ("sin_gps", "Sin ubicación"),
    ("mayorista", "Mayoristas"),
    ("archivados", "Archivados"),
)
HECHO = {
    "guardado": "Listo: los datos quedaron guardados.",
    "creado": "Listo: el cliente quedó cargado.",
    "confirmado": "Listo: el cliente quedó confirmado.",
    "unido": "Listo: el cliente nuevo se unió con este.",
    "anotado": "Listo: quedó anotado en los dos clientes. No se unieron.",
}
LOCALIDADES_MAX = 40
UNIR_MAX = 10


def _clip(value, size):
    return " ".join((value or "").split())[:size]


class Contactos(http.Controller):

    # ------------------------------------------------------------ helpers

    def _check(self):
        env = request.env
        if not ui_v2_on(env):
            raise NotFound()
        if not ct.puede_contactos(env.user):
            raise Forbidden()
        return env.user

    def _form(self):
        return request.httprequest.form

    def _render(self, template, values, status=200, parent=None):
        user = request.env.user
        base = {
            "dn": kit_context("deposito.css", "fichas.css", "contactos.css", "contactos.js"),
            "user": user,
            "inicio": _home(user),
            "parent": parent if parent is not None else _home(user),
            "volver": parent if parent is not None else _home(user),
            "nav": page_nav(request.env, "contactos"),
            "csrf_token": request.csrf_token(),
            "ok": "",
            "error": "",
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _partner(self, partner_id):
        """The commercial partner, in the user's environment, when he may
        read it (backend rules) and reach it (external seller); 404
        otherwise, as the record screen."""
        partner = request.env["res.partner"].with_context(active_test=False).browse(
            int(partner_id)).exists()
        if not partner or partner.sudo().commercial_partner_id != partner.sudo():
            raise NotFound()
        if not partner.has_access("read") or partner.sudo().distri_generic_partner:
            raise NotFound()
        if not request.env.user._distri_may_reach_customer(partner):
            raise NotFound()
        return partner

    # ------------------------------------------------------------ the list

    def _params(self, kw):
        user = request.env.user
        todo = ct.ve_todo(user)
        tipo = kw.get("tipo") if todo and kw.get("tipo") in ct.TIPOS else "clientes"
        filtros = {f for f in ct.FILTROS if kw.get(f) in ("1", "on", "true")}
        if not todo:
            filtros -= set(ct.FILTROS_ADMIN)
        if "deuda" in filtros and not self._ve_saldos():
            filtros.discard("deuda")
        try:
            vendedor = int(kw.get("vendedor") or 0) if todo else 0
            page = max(0, int(kw.get("page") or 0))
        except ValueError:
            vendedor, page = 0, 0
        return {"q": _clip(kw.get("q"), ct.MAX_QUERY), "tipo": tipo, "filtros": filtros,
                "localidad": _clip(kw.get("localidad"), 60), "vendedor": vendedor, "page": page}

    @staticmethod
    def _query(params, **extra):
        """The list's URL query of these params (for «Ver más»)."""
        from urllib.parse import urlencode
        data = {"q": params["q"], "localidad": params["localidad"]}
        if params["tipo"] != "clientes":
            data["tipo"] = params["tipo"]
        if params["vendedor"]:
            data["vendedor"] = params["vendedor"]
        data.update({f: "1" for f in sorted(params["filtros"])})
        data.update(extra)
        return urlencode({k: v for k, v in data.items() if v not in ("", None)})

    def _ve_saldos(self):
        from ..models.cuentas_app import app_scope
        return bool(app_scope(request.env))

    def _buscar(self, params):
        Partner = request.env["res.partner"]
        records, total, mas = Partner._distri_contactos_buscar(
            params["q"], params["tipo"], params["filtros"], params["localidad"],
            params["vendedor"], params["page"])
        return self._filas(records), total, mas

    def _filas(self, records):
        """The rows of one page; the balance of the customers whose account
        /cuentas shows the user, computed in one batch for this page only."""
        if not records:
            return []
        partners = records.sudo()
        saldos = {}
        if self._ve_saldos():
            App = request.env["distri.account.app"]
            chosen = App._app_chosen_ids()
            visibles = partners.filtered(lambda p: p.customer_rank > 0 and App._app_allowed(
                p, chosen))
            saldos = request.env["res.partner"].sudo()._distri_account_balances(visibles.ids)
        filas = []
        for p in partners:
            saldo = saldos.get(p.id)
            filas.append({
                "id": p.id,
                "nombre": p.name or "",
                "codigo": p.ref or "",
                "localidad": p.city or "",
                "calle": p.street or "",
                "vendedor": p.user_id.name or "",
                "whatsapp": p.distri_whatsapp or "",
                "saldo": _money(saldo) if saldo is not None else "",
                "deuda": bool(saldo is not None and saldo > 0.005),
                "confirmar": bool(p.distri_cliente_provisorio),
                "archivado": not p.active,
                "url": ficha_url("cliente", p.id, "/contactos"),
            })
        return filas

    def _localidades(self):
        """The towns of the contacts the user reads (record rules apply), the
        most used first, plus the delivery zones."""
        Partner = request.env["res.partner"]
        groups = Partner._read_group(
            [("parent_id", "=", False), ("city", "not in", (False, ""))], ["city"],
            ["__count"], order="__count desc", limit=LOCALIDADES_MAX)
        names = [city for city, _count in groups]
        zonas = request.env["distri.zona"].sudo().search([]).mapped("name")
        seen = {n.strip().lower() for n in names}
        names += [z for z in zonas if z.strip().lower() not in seen]
        return sorted({n.strip() for n in names if n and n.strip()}, key=str.lower)

    def _vendedores(self):
        """The sellers administración may list the portfolio of."""
        group = request.env.ref(ct.SELLER_GROUP)
        users = request.env["res.users"].sudo().search([
            ("share", "=", False), ("all_group_ids", "in", group.ids)], order="name")
        return [(u.id, u.name) for u in users
                if u.etiqueta_cartera_id or u._distri_is_external_seller()
                or not u.has_group(ct.ADMIN_GROUP)]

    @http.route("/contactos", type="http", auth="user", methods=["GET"], sitemap=False)
    def contactos(self, **kw):
        user = self._check()
        params = self._params(kw)
        started = time.perf_counter()
        filas, total, mas = self._buscar(params)
        todo = ct.ve_todo(user)
        filtros = [(k, label) for k, label in FILTRO_LABELS
                   if (todo or k not in ct.FILTROS_ADMIN) and (k != "deuda" or self._ve_saldos())]
        return self._render("distri_pedido.contactos_page", {
            "params": params,
            "filas": filas,
            "total": total,
            "mas": mas,
            "todo": todo,
            "tipos": TIPO_LABELS,
            "filtros": filtros,
            "localidades": self._localidades(),
            "vendedores": self._vendedores() if todo else [],
            "nuevo": True,
            "mas_url": "/contactos?" + self._query(params, page=params["page"] + 1),
            "ms": int((time.perf_counter() - started) * 1000),
        })

    @http.route("/contactos/buscar", type="http", auth="user", methods=["GET"], sitemap=False)
    def buscar(self, **kw):
        self._check()
        params = self._params(kw)
        started = time.perf_counter()
        filas, total, mas = self._buscar(params)
        return request.make_json_response({
            "ok": True, "filas": filas, "total": total, "mas": mas, "page": params["page"],
            "ms": int((time.perf_counter() - started) * 1000),
        }, headers=[("Cache-Control", "no-store")])

    # ------------------------------------------------------------ new

    def _nuevo_page(self, form=None, parecidos=None, error="", status=200):
        form = form or {}
        return self._render("distri_pedido.contactos_nuevo", {
            "form": {k: form.get(k) or "" for k in ("nombre", "wa_area", "wa_numero", "calle",
                                                     "ciudad")},
            "parecidos": parecidos or [],
            "localidades": self._localidades(),
            "error": error,
        }, status=status, parent="/contactos")

    @http.route("/contactos/nuevo", type="http", auth="user", methods=["GET", "POST"],
                sitemap=False)
    def nuevo(self, **kw):
        from .nueva_venta import NuevaVenta, crear_cliente
        user = self._check()
        if request.httprequest.method != "POST":
            return self._nuevo_page({"nombre": _clip(kw.get("nombre"), 120)})
        form = self._form()
        try:
            valores = NuevaVenta()._cliente_valores(form, nuevo=True)
        except UserError as exc:
            return self._nuevo_page(form, error=exc.args[0], status=400)
        if form.get("nuevo_ok") != "1":
            parecidos = request.env["distri.cash.collection"]._cash_matches(
                valores["name"], valores.get("distri_whatsapp") or "")
            parecidos = [p for p in parecidos if not p.get("provisional")] or parecidos
            if parecidos:
                Partner = request.env["res.partner"]
                for p in parecidos:
                    mine = Partner.browse(p["id"])
                    p["url"] = ficha_url("cliente", p["id"], "/contactos") if (
                        mine.has_access("read") and user._distri_may_reach_customer(mine)) else ""
                return self._nuevo_page(form, parecidos=parecidos)
        try:
            socio = crear_cliente(user, valores, "Contactos")
        except (UserError, ValidationError) as exc:
            return self._nuevo_page(form, error=exc.args[0], status=400)
        return request.redirect("%s?desde=/contactos&hecho=creado" % ficha_url(
            "cliente", socio.id), 303)

    # ------------------------------------------------------------ edit

    def _form_values(self, partner, modo):
        """What the form shows: the record's values."""
        from ..models.ar_phone import ar_national, split_ar_national
        p = partner.sudo()
        area = numero = ""
        national = ar_national(p.distri_whatsapp)
        if national:
            area, numero = split_ar_national(national)
        values = {"nombre": p.name or "", "wa_area": area, "wa_numero": numero,
                  "calle": p.street or "", "ciudad": p.city or ""}
        if modo != "todo":
            return values
        entrega = p.child_ids.filtered(lambda c: c.type == "delivery")[:1]
        values.update({
            "codigo": p.ref or "",
            "cuit": p.vat or "",
            "email": p.email or "",
            "telefono": p.phone or "",
            "lat": ("%.6f" % p.partner_latitude) if p.partner_latitude else "",
            "lng": ("%.6f" % p.partner_longitude) if p.partner_longitude else "",
            "dias": [d for d in (p.distri_delivery_days or "").split(",") if d],
            "horarios": p.distri_delivery_hours or "",
            "nota_reparto": p.distri_driver_note or "",
            "lista": str(p.property_product_pricelist.id or ""),
            "vendedor": str(p.user_id.id or ""),
            "cartera": str(self._cartera_de(p).id or ""),
            "vendedor_web": str(p.distri_vendedor_web_id.id or ""),
            "cuenta_corriente": bool(p.distri_cuenta_corriente),
            "notas": self._texto(p.comment),
            "iva": str(p.l10n_ar_afip_responsibility_type_id.id or "")
            if "l10n_ar_afip_responsibility_type_id" in p._fields else "",
            "entrega_calle": entrega.street or "",
            "entrega_ciudad": entrega.city or "",
        })
        return values

    @staticmethod
    def _texto(html):
        from odoo.tools import html2plaintext
        return html2plaintext(html or "").strip() if html else ""

    def _carteras(self):
        """The portfolio tags (res.users.etiqueta_cartera_id) with their seller."""
        users = request.env["res.users"].sudo().search([("etiqueta_cartera_id", "!=", False)])
        tags = {}
        for u in users:
            tags.setdefault(u.etiqueta_cartera_id, []).append(u.name)
        return sorted(((t.id, "%s (%s)" % (t.name, ", ".join(sorted(n))))
                       for t, n in tags.items()), key=lambda x: x[1].lower())

    def _cartera_de(self, partner):
        ids = {tid for tid, _label in self._carteras()}
        return partner.sudo().category_id.filtered(lambda t: t.id in ids)[:1]

    def _opciones(self, partner):
        """The choices of the administración form."""
        from ..models.lista_oculta import ve_lista_oculta
        from ..models.reparto_v2 import reparto_v2_on
        env = request.env
        user = env.user
        listas = env["product.pricelist"].search([], order="name")
        if not ve_lista_oculta(user):
            listas = listas.filtered(lambda l: not l._distri_es_oculta())
        iva = []
        if "l10n_ar_afip_responsibility_type_id" in env["res.partner"]._fields:
            iva = [(r.id, r.name) for r in env["l10n_ar.afip.responsibility.type"].sudo().search(
                [], order="sequence, name")]
        cuenta_corriente = reparto_v2_on(env) and (
            user.has_group(ct.ADMIN_GROUP) or user.has_group(ct.SYSTEM_GROUP))
        return {
            "listas": [(l.id, l.name) for l in listas],
            "vendedores": [(u.id, u.name) for u in env["res.users"].sudo().search(
                [("share", "=", False), ("all_group_ids", "in",
                                         env.ref(ct.SELLER_GROUP).ids)], order="name")],
            "carteras": self._carteras(),
            "vendedores_web": [(v.id, v.display_name) for v in env["distri.vendedor.web"].sudo().search(
                [], order="name")],
            "iva": iva,
            "cuenta_corriente": cuenta_corriente,
            "dias": list(enumerate(WEEKDAYS)),
            "localidades": self._localidades(),
        }

    def _edit_page(self, partner, modo, form=None, error="", status=200):
        here = ficha_url("cliente", partner.id)
        values = {
            "p": partner.sudo(),
            "modo": modo,
            "form": form if form is not None else self._form_values(partner, modo),
            "action": "/clientes/%s/editar" % partner.id,
            "error": error,
        }
        if modo == "todo":
            values.update(self._opciones(partner))
            p = partner.sudo()
            values["mapa"] = ""
            if p.partner_latitude or p.partner_longitude:
                from ..models.datos_entrega import maps_url
                values["mapa"] = maps_url(p.partner_latitude, p.partner_longitude)
        return self._render("distri_pedido.contactos_editar", values, status=status, parent=here)

    @http.route("/clientes/<int:partner_id>/editar", type="http", auth="user",
                methods=["GET", "POST"], sitemap=False)
    def editar(self, partner_id, **kw):
        self._check()
        partner = self._partner(partner_id)
        modo = ct.modo_edicion(request.env, partner)
        if not modo:
            raise Forbidden()
        if request.httprequest.method != "POST":
            return self._edit_page(partner, modo)
        form = self._form()
        try:
            if modo == "todo":
                cambios = self._guardar_todo(partner, form)
            else:
                cambios = self._guardar_vendedor(partner, form)
        except (UserError, ValidationError, AccessError) as exc:
            shown = self._echo(form, modo)
            return self._edit_page(partner, modo, form=shown, error=exc.args[0], status=400)
        if cambios:
            partner.sudo().message_post(body="Datos cambiados por %s en «Contactos»: %s." % (
                request.env.user.name, ", ".join(cambios)))
        return request.redirect("%s?hecho=guardado" % ficha_url("cliente", partner.id), 303)

    def _echo(self, form, modo):
        """The posted values, to show the form again after a refusal."""
        shown = {k: form.get(k) or "" for k in form.keys() if k != "csrf_token"}
        shown["dias"] = form.getlist("dias")
        shown["cuenta_corriente"] = form.get("cuenta_corriente") == "1"
        return shown

    def _guardar_vendedor(self, partner, form):
        """A seller: WhatsApp, address and town, with sudo, as «Nueva venta»'s
        «Datos del cliente» (19.0.1.129.40)."""
        from .nueva_venta import NuevaVenta
        valores = NuevaVenta()._cliente_valores(form, nuevo=False)
        valores = {k: v for k, v in valores.items() if k in ct.SELLER_FIELDS}
        p = partner.sudo()
        valores = {k: v for k, v in valores.items() if (p[k] or False) != (v or False)}
        if not valores:
            return []
        with request.env.cr.savepoint():
            p.write(valores)
        return sorted(p._fields[k].string for k in valores)

    def _guardar_todo(self, partner, form):
        """Administración / gerencia: every field of the form, written as the
        user (his ACLs, the record rules and the model's checks decide).
        Only what changed is written."""
        valores = ct.valores_admin(request.env, partner, form, self._opciones(partner))
        entrega = valores.pop("_entrega", None)
        p = partner.sudo()
        cambios = []
        for key in list(valores):
            current = p[key]
            new = valores[key]
            if p._fields[key].type == "many2one":
                current = current.id or False
            elif p._fields[key].type == "many2many":
                if set(current.ids) == set(new[0][2]):
                    valores.pop(key)
                continue
            elif p._fields[key].type == "html":
                if self._texto(current) == self._texto(new):
                    valores.pop(key)
                continue
            elif p._fields[key].type == "float":
                if round(current or 0.0, 6) == round(new or 0.0, 6):
                    valores.pop(key)
                continue
            if (current or False) == (new or False):
                valores.pop(key)
        if {"partner_latitude", "partner_longitude"} & set(valores):
            valores.update({"distri_location_user_id": request.env.user.id,
                            "distri_location_date": fields.Datetime.now()})
        with request.env.cr.savepoint():
            if valores:
                partner.write(valores)
                cambios += [p._fields[k].string for k in valores
                            if k not in ("distri_location_user_id", "distri_location_date")]
            if entrega is not None and self._guardar_entrega(partner, entrega):
                cambios.append("Dirección de entrega")
        return sorted(set(cambios))

    def _guardar_entrega(self, partner, entrega):
        """The delivery address (a child contact of type «delivery»), as the
        user; an empty one is left as it was."""
        actual = partner.child_ids.filtered(lambda c: c.type == "delivery")[:1]
        calle, ciudad = entrega
        if actual:
            if (actual.street or False, actual.city or False) == (calle, ciudad) or not (
                    calle or ciudad):
                return False
            actual.write({"street": calle, "city": ciudad})
            return True
        if not (calle or ciudad):
            return False
        request.env["res.partner"].create({"parent_id": partner.id, "type": "delivery",
                                           "street": calle, "city": ciudad})
        return True

    # ------------------------------------------------------------ a confirmar

    def _provisorio(self, partner_id):
        from ..models.cliente_provisorio import can_confirm
        self._check()
        if not can_confirm(request.env):
            raise Forbidden()
        partner = self._partner(partner_id)
        return partner

    @http.route("/clientes/<int:partner_id>/confirmar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def confirmar(self, partner_id, **kw):
        partner = self._provisorio(partner_id)
        if partner.sudo().distri_cliente_provisorio:
            partner.action_distri_provisorio_confirmar()
        return request.redirect("%s?hecho=confirmado" % ficha_url("cliente", partner.id), 303)

    def _unir_page(self, partner, q="", error="", status=200):
        src = partner.sudo()
        Cash = request.env["distri.cash.collection"]
        parecidos = [m for m in Cash._cash_matches(src.name, src.distri_whatsapp or src.phone,
                                                   exclude=src) if not m.get("provisional")]
        encontrados = []
        if q:
            records, _total, _mas = request.env["res.partner"]._distri_contactos_buscar(
                q, "todos", (), "", 0, 0, UNIR_MAX + 1)
            encontrados = [{"id": r.id, "name": r.name, "detail": " · ".join(
                x for x in (r.ref, r.city, r.distri_whatsapp or r.phone) if x)}
                for r in records.sudo() if r != src and not r.distri_cliente_provisorio][:UNIR_MAX]
        return self._render("distri_pedido.contactos_unir", {
            "p": src,
            "q": q,
            "parecidos": parecidos,
            "encontrados": encontrados,
            "refusal": partner._distri_merge_refusal(),
            "action": "/clientes/%s/unir" % src.id,
            "error": error,
        }, status=status, parent=ficha_url("cliente", src.id))

    @http.route("/clientes/<int:partner_id>/unir", type="http", auth="user",
                methods=["GET", "POST"], sitemap=False)
    def unir(self, partner_id, q="", **kw):
        partner = self._provisorio(partner_id)
        if not partner.sudo().distri_cliente_provisorio:
            return request.redirect(ficha_url("cliente", partner.id), 303)
        if request.httprequest.method != "POST":
            return self._unir_page(partner, _clip(q, ct.MAX_QUERY))
        form = self._form()
        try:
            target = self._partner(int(form.get("target_id") or 0))
        except (NotFound, ValueError):
            return self._unir_page(partner, error="Elegí con qué cliente se une.", status=400)
        refusal = partner._distri_merge_refusal()
        if refusal:
            if form.get("anotar") != "1":
                return self._unir_page(partner, error=refusal, status=400)
            self._anotar(partner, target)
            return request.redirect("%s?hecho=anotado" % ficha_url("cliente", partner.id), 303)
        try:
            with request.env.cr.savepoint():
                result = partner._distri_provisorio_unir_con(target)
        except (UserError, AccessError) as exc:
            return self._unir_page(partner, error=exc.args[0], status=400)
        return request.redirect("%s?hecho=unido" % ficha_url("cliente", result.id), 303)

    def _anotar(self, partner, target):
        """«Mark and link» without merging: a note on both, nothing else."""
        user = request.env.user
        src, dst = partner.sudo(), target.sudo()
        src.message_post(body=Markup(
            "Según %s es el mismo cliente que <a href=\"%s\">%s</a>. No se unieron desde "
            "«Contactos» porque tiene movimientos de cuenta.") % (
            user.name, ficha_url("cliente", dst.id), dst.display_name))
        dst.message_post(body=Markup(
            "Según %s, el cliente nuevo <a href=\"%s\">%s</a> es este mismo. No se unieron "
            "desde «Contactos» porque tiene movimientos de cuenta.") % (
            user.name, ficha_url("cliente", src.id), src.display_name))
