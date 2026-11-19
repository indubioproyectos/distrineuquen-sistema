"""«Asignar cliente» on the phone (19.0.1.111.0, owner-approved mockup «03
Asignar cliente»): /revisar/<order>/asignar.

    /revisar/<id>/asignar              GET   the screen: what the buyer typed,
                                             «Pegar número», «Buscar otro»,
                                             the candidates with their match
                                             marks and «Cliente nuevo»
    /revisar/<id>/asignar/candidatos   GET   the candidates again for a pasted
                                             number or another search (JSON
                                             with the list rendered)
    /revisar/<id>/asignar              POST  with `accion` (asignar | aprobar):
                                             «Solo asignar» / «Asignar y
                                             aprobar»; without it, the URL of
                                             this screen (what the v108 page
                                             asks before going on)

Same access as the backend wizard (models/asignar_cliente.py puede_asignar:
administración, gerencia, sales managers). Every rule is the wizard's:
the candidates and their marks (_distri_candidatos_pedido, _fila_candidato),
«Pegar número» (models/pegar_numero.py) and the assignment itself
(_asignar: re-pricing, release of the hold, approval, the verified
WhatsApp). The screen builds an unsaved wizard to read and a saved one to
assign; this file only translates HTTP. The backend wizard keeps working.
"""
from urllib.parse import quote, urlencode

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models.ar_phone import format_ar_national, phone_match_key, read_pasted_phone
from ..models.asignar_cliente import puede_asignar, town_from_name
from ..models.reparto import _money
from ..models.revisar import puede_revisar
from .kit import kit_context
from .revisar import _error_text, _phone
from .tablero import wants_tablero

PAGE = "distri_pedido.asignar_page"
# marks of the wizard (MARCAS) -> (symbol, chip kind)
MARCA_CHIP = {"si": ("✓", "ok"), "parcial": ("≈", "warn"), "no": ("✗", "err"),
              "falta": ("", "neutral")}
VOLVER = {"agilpedido": "/agilpedido"}
MAX_TEXTO = 200


def installed(env):
    """The templates of this version are loaded (module updated)."""
    return bool(env.ref(PAGE, raise_if_not_found=False))


def asignar_url(order_id, pegado="", volver=""):
    query = {k: v for k, v in (("pegado", (pegado or "").strip()), ("volver", volver)) if v}
    return "/revisar/%s/asignar%s" % (order_id, ("?" + urlencode(query)) if query else "")


def asignado_texto(order, quiso_aprobar=False):
    """The line the list shows after the assignment."""
    order = order.sudo()
    texto = "Listo: %s quedó a nombre de %s" % (order.name, order.partner_id.display_name)
    if order.state == "sale":
        return texto + " y aprobado."
    if quiso_aprobar:
        return texto + (", pero no se aprobó: el cliente no tiene WhatsApp. Cargalo con «Pegar "
                        "número» y aprobalo.")
    return texto + "."


def _chip(marca, label):
    symbol, kind = MARCA_CHIP.get(marca or "falta", MARCA_CHIP["falta"])
    if marca in (False, None, "falta"):
        return {"text": "Sin dato", "kind": "neutral"}
    return {"text": "%s %s" % (symbol, label), "kind": kind}


def _clip(value):
    return (value or "").strip()[:MAX_TEXTO]


class AsignarCliente(http.Controller):

    # ---------------------------------------------------------------- helpers

    def _check(self):
        user = request.env.user
        if user.share or not puede_asignar(request.env) or not installed(request.env):
            raise Forbidden()
        return user

    def _order(self, order_id):
        order = request.env["sale.order"].browse(int(order_id)).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        return order

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _dn(self):
        return kit_context("revisar.css", "asignar.css", "asignar.js")

    def _parent(self, volver):
        """Where «Volver» goes: the list it came from."""
        if volver in VOLVER:
            return VOLVER[volver]
        if puede_revisar(request.env):
            return "/revisar"
        user = request.env.user
        return "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero")

    def _wizard_model(self, order):
        return request.env["distri.asignar.cliente"].with_context(default_order_id=order.id)

    def _lectura(self, order, pegado="", busqueda=None, elegido=0):
        """An unsaved wizard with the screen's values, and who is chosen:
        the pasted number searches and picks its only customer, like the
        backend's «Buscar este número»."""
        Wizard = self._wizard_model(order)
        values = Wizard.default_get(list(Wizard._fields))
        values["order_id"] = order.id
        national, _error = read_pasted_phone(pegado)
        if (pegado or "").strip():
            values["pegado"] = _clip(pegado)
        if busqueda is not None and busqueda.strip():
            values["busqueda"] = _clip(busqueda)
        elif national:
            values["busqueda"] = format_ar_national(national)
            typed = values.get("nuevo_telefono") or ""
            if not typed.strip() or phone_match_key(typed) == phone_match_key(
                    order.sudo().distri_telefono or ""):
                values["nuevo_telefono"] = format_ar_national(national)
        chosen = request.env["res.partner"]
        if elegido:
            chosen = request.env["res.partner"].browse(int(elegido)).exists()
        elif national:
            found = request.env["res.partner"]._distri_clientes_por_telefono(national)
            if len(found) == 1:
                chosen = found
        if not chosen and values.get("partner_id"):
            chosen = request.env["res.partner"].browse(values["partner_id"]).exists()
        if chosen and (chosen.distri_generic_partner or chosen.type != "contact"):
            chosen = request.env["res.partner"]
        values["partner_id"] = chosen.id or False
        return Wizard.new(values), chosen

    def _fila(self, wizard, socio, elegido):
        """One candidate card (views/asignar_app.xml asignar_candidato)."""
        fila = wizard._fila_candidato(socio)
        socio = socio.sudo()
        telefonos = [t for t in (socio.phone, socio.distri_whatsapp) if t]
        pegado = read_pasted_phone(wizard.pegado)[0]
        if pegado and any(phone_match_key(t) == pegado for t in telefonos):
            # the number pasted from the chat is the buyer's phone too
            fila["m_telefono"] = "si"
        # the same number written twice (phone and WhatsApp) shows once
        vistos, unicos = set(), []
        for t in telefonos:
            key = phone_match_key(t) or t
            if key not in vistos:
                vistos.add(key)
                unicos.append(_phone(t))
        ciudad = socio.city or town_from_name(socio.name) or ""
        detalle = ["CUIT %s" % socio.vat if socio.vat else "CUIT sin dato"]
        if fila["lista"]:
            detalle.append("Lista %s" % fila["lista"])
        compras = "Sin compras"
        if fila["compras"]:
            compras = "Compras %s" % fila["compras"]
            if fila["ultima_compra"]:
                compras += " · última %s" % fila["ultima_compra"].strftime("%d/%m/%y")
        return {
            "id": socio.id,
            "name": socio.name or "",
            "elegido": socio.id == elegido,
            "puntaje": fila["puntaje"],
            "nombre": _chip(fila["m_nombre"], "Nombre") if fila["m_nombre"] in ("si", "parcial") else None,
            "telefono": " / ".join(unicos),
            "telefono_chip": _chip(fila["m_telefono"], "Teléfono"),
            "domicilio": socio.street or "",
            "domicilio_chip": _chip(fila["m_domicilio"], "Domicilio"),
            "ciudad": ciudad,
            "ciudad_chip": _chip(fila["m_ciudad"], "Ciudad"),
            "detalle": " · ".join(detalle),
            "saldo": "Saldo %s · %s" % (_money(fila["saldo"]), compras),
            "ficha": "/odoo/res.partner/%s" % socio.id,
        }

    def _filas(self, wizard, chosen):
        # the unsaved wizard holds its records as NewId: the real ones
        socios = wizard.candidato_ids._origin
        if chosen and chosen not in socios:
            socios = chosen | socios
        filas = [self._fila(wizard, s, chosen.id) for s in socios]
        filas.sort(key=lambda f: (not f["elegido"], -f["puntaje"], f["name"]))
        return filas

    def _pegado(self, wizard):
        if not (wizard.pegado or "").strip():
            return {"preview": "", "error": "", "aviso": ""}
        return {"preview": wizard.pegado_preview or "", "error": wizard.pegado_error or "",
                "aviso": wizard.pegado_aviso or ""}

    def _lista_html(self, filas, order):
        return request.env["ir.qweb"]._render("distri_pedido.asignar_candidatos", {
            "filas": filas, "o": order, "dn": self._dn()})

    def _web(self, order):
        """«Lo que escribió el comprador»."""
        o = order.sudo()
        return {
            "nombre": o.distri_nombre or "Sin nombre",
            "telefono": _phone(o.distri_telefono) if o.distri_telefono else "",
            "domicilio": " · ".join(v for v in (o.distri_domicilio or "",) if v),
            "agil": o.distri_origen == "agilpedido_wa",
            "sugerido": o.distri_agil_cliente_sugerido_id.display_name
            if o.distri_agil_cliente_sugerido_id else "",
        }

    def _no_generic(self, order, volver):
        """The order already has its customer (or was cancelled)."""
        texto = ("El pedido %s está cancelado." % order.name if order.state == "cancel"
                 else "El pedido %s ya tiene el cliente asignado: %s." % (
                     order.name, order.sudo().partner_id.display_name))
        return request.redirect("%s?aviso=%s" % (self._parent(volver), quote(texto)), local=True)

    # ---------------------------------------------------------------- page

    @http.route("/revisar/<int:order_id>/asignar", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def page(self, order_id, pegado="", busqueda=None, elegido="", modo="existente", volver="",
             error="", **kw):
        self._check()
        order = self._order(order_id)
        if not order.distri_partner_generic or order.state == "cancel":
            return self._no_generic(order, volver)
        try:
            elegido = int(elegido or 0)
        except ValueError:
            elegido = 0
        wizard, chosen = self._lectura(order, pegado, busqueda, elegido)
        filas = self._filas(wizard, chosen)
        nuevo = {k: kw.get(k) if kw.get(k) is not None else (wizard[k] or "")
                 for k in ("nuevo_nombre", "nuevo_telefono", "nuevo_domicilio", "nuevo_ciudad",
                           "nuevo_cuit")}
        lista_id = kw.get("nuevo_lista_id") or wizard.nuevo_lista_id._origin.id or ""
        listas = request.env["product.pricelist"].search([], order="sequence, id")
        o = order.sudo()
        html = request.env["ir.qweb"]._render(PAGE, {
            "dn": self._dn(),
            "o": o,
            "amount": _money(o.amount_total),
            "web": self._web(order),
            "mayorista": bool(wizard.mayorista),
            "pegado": wizard.pegado or "",
            "pegado_lectura": self._pegado(wizard),
            "busqueda": wizard.busqueda or "",
            "filas": filas,
            "elegido": chosen.id or "",
            "modo": "nuevo" if modo == "nuevo" else "existente",
            "nuevo": nuevo,
            "lista_id": str(lista_id),
            "listas": [(str(p.id), p.name) for p in listas],
            "puede_aprobar": o.state in ("draft", "sent"),
            "parent": self._parent(volver),
            "volver": volver if volver in VOLVER else "",
            "csrf_token": request.csrf_token(),
            "error": error,
        })
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    @http.route("/revisar/<int:order_id>/asignar/candidatos", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def candidatos(self, order_id, pegado="", busqueda=None, elegido="", **kw):
        self._check()
        order = self._order(order_id)
        if not order.distri_partner_generic:
            return self._json({"ok": False, "error": "Este pedido ya tiene el cliente asignado."},
                              status=409)
        try:
            elegido = int(elegido or 0)
        except ValueError:
            elegido = 0
        wizard, chosen = self._lectura(order, pegado, busqueda, elegido)
        filas = self._filas(wizard, chosen)
        return self._json({
            "ok": True,
            **self._pegado(wizard),
            "busqueda": wizard.busqueda or "",
            "elegido": chosen.id or 0,
            "nuevo_telefono": wizard.nuevo_telefono or "",
            "count": len(filas),
            "html": str(self._lista_html(filas, order)),
        })

    # ---------------------------------------------------------------- assign

    @http.route("/revisar/<int:order_id>/asignar", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def asignar(self, order_id, accion="", pegado="", volver="", **kw):
        user = request.env.user
        if user.share or not puede_asignar(request.env):
            raise Forbidden()
        order = self._order(order_id)
        if not accion:
            return self._bridge(order, pegado)
        if not installed(request.env):
            raise Forbidden()
        if not order.distri_partner_generic:
            return self._refuse(order, "Este pedido ya tiene el cliente asignado.", volver, 409)
        modo = "nuevo" if kw.get("modo") == "nuevo" else "existente"
        try:
            partner_id = int(kw.get("partner_id") or 0)
        except ValueError:
            partner_id = 0
        try:
            lista_id = int(kw.get("nuevo_lista_id") or 0)
        except ValueError:
            lista_id = 0
        aprobar = accion == "aprobar" and order.state in ("draft", "sent")
        values = {
            "order_id": order.id,
            "modo": modo,
            "partner_id": partner_id if modo == "existente" and partner_id else False,
            "busqueda": _clip(kw.get("busqueda")),
            "pegado": _clip(pegado),
            "pegado_guardar": bool(kw.get("pegado_guardar")),
            "nuevo_nombre": _clip(kw.get("nuevo_nombre")),
            "nuevo_telefono": _clip(kw.get("nuevo_telefono")),
            "nuevo_domicilio": _clip(kw.get("nuevo_domicilio")),
            "nuevo_ciudad": _clip(kw.get("nuevo_ciudad")),
            "nuevo_cuit": _clip(kw.get("nuevo_cuit")),
            "nuevo_lista_id": lista_id or False,
        }
        if values["partner_id"] and not request.env["res.partner"].browse(
                values["partner_id"]).exists():
            return self._refuse(order, "Ese cliente ya no existe: elegí otro.", volver)
        if values["nuevo_lista_id"] and not request.env["product.pricelist"].browse(
                values["nuevo_lista_id"]).exists():
            values["nuevo_lista_id"] = False
        try:
            with request.env.cr.savepoint():
                wizard = self._wizard_model(order).create(values)
                if aprobar:
                    wizard.action_asignar_y_aprobar()
                else:
                    wizard.action_asignar()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(order, _error_text(exc), volver)
        order.invalidate_recordset()
        message = asignado_texto(order, quiso_aprobar=aprobar)
        url = "%s?ok=%s&asignado=%s%s" % (self._parent(volver), order.id, order.id,
                                          "&sin_wa=1" if aprobar and order.state != "sale" else "")
        if self._by_fetch():
            return self._json({"ok": True, "message": message, "url": url,
                               "aprobado": order.state == "sale"})
        return request.redirect(url, local=True)

    def _refuse(self, order, text, volver, status=400):
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=status)
        return request.redirect("%s%serror=%s" % (
            asignar_url(order.id, volver=volver), "&" if volver else "?", quote(text)), local=True)

    def _bridge(self, order, pegado):
        """The v108 page asks where «Asignar cliente» goes: this screen once
        the module is updated, the backend wizard until then."""
        if not order.distri_partner_generic or order.state == "cancel":
            text = "Este pedido ya tiene el cliente asignado."
            if self._by_fetch():
                return self._json({"ok": False, "error": text}, status=400)
            return request.redirect("/revisar", local=True)
        if not installed(request.env):
            from .revisar import asignar_backend
            return asignar_backend(order, pegado, self._by_fetch())
        url = asignar_url(order.id, pegado)
        if self._by_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url, local=True)
