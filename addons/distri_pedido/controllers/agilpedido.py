"""«Pedidos de AgilPedido» on the kit (19.0.1.111.0, owner-approved mockups
«04 Pedidos de AgilPedido», «04 Cargar pedido de AgilPedido» and, on the PC,
«pc-agil-corregir»).

    /tablero
      /agilpedido                     the orders that came by WhatsApp
                                      (models/agilpedido_wa.py), tabs Todos /
                                      Para corregir / Total no coincide
        /agilpedido/pedido/<id>       one of them: its lines with «Está
                                      bien» / «Corregir producto» and
                                      «Aprobar el pedido»
        /agilpedido/cargar            «Cargar un pedido de AgilPedido»: paste
                                      the message, «Leer el mensaje»
                                      (distri.importar.pedido, a transient
                                      record of the user)
        /agilpedido/cargar/<id>       the lines read, the customer, the list,
                                      the corrections and «Crear el
                                      presupuesto»
        /agilpedido/creado/<order>    the quotation just created

On both line screens «Corregir producto» opens the same panel (a bottom
sheet on the phone, a side column from 1024 px): the product of Odoo by name
or code, «¿Cómo viene en AgilPedido?» (its units of sale) and, when the unit
does not exist in Odoo, how many units 1 of AgilPedido is; «Corregir y
recordar» stores the equivalence (models/agil_equivalencia.py) so the next
message comes right. The rules are the models' (importar_pedido.py,
agilpedido_wa.py, agil_equivalencia.py): this file only translates HTTP.
Every action goes by fetch (X-Distri-Fetch: 1) and answers JSON with the
lines rendered again.

Access: administración and gerencia, the groups of the backend menu
«Transición AgilPedido».
"""
import difflib
from datetime import datetime, timedelta
from urllib.parse import quote

from werkzeug.exceptions import Forbidden, NotFound

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models.agilpedido_wa import ORIGEN_AGILPEDIDO_WA, cantidad_texto, leer_mensaje_agilpedido
from ..models.asignar_cliente import puede_asignar
from ..models.importar_pedido import (
    CATEGORIAS_A_PRESUPUESTAR, catalogo_normalizado, importe_ar, leer_envase, leer_renglon,
    lista_general, lista_mayorista, normalizar, precio_en_unidad, producto_por_codigo, unidad_de_venta,
    unidades_de_venta,
)
from ..models.reparto import _money
from ..models.semaforo import AR_TZ, UTC
from .kit import kit_context
from .revisar import _error_text, _phone, cuando
from .tablero import wants_tablero

PAGE = "distri_pedido.agil_lista"
GRUPOS_AGIL = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")
DIAS_LISTA = 7
LIMIT = 200
LIMIT_LATERAL = 30
MAX_MENSAJE = 20000
TOLERANCIA = 1.0
# words of a pack or a filler that never help to find the product
PALABRAS_RUIDO = {"caja", "pack", "bulto", "bultos", "display", "may", "los", "las", "del", "con"}
TABS = [("todos", "Todos"), ("corregir", "Para corregir"), ("total", "Total no coincide")]


def puede_agil(env):
    return env.su or any(env.user.has_group(g) for g in GRUPOS_AGIL)


def installed(env):
    """The templates of this version are loaded (module updated)."""
    return bool(env.ref(PAGE, raise_if_not_found=False))


def _inicio_hoy():
    """Today's first moment in Argentina, as Odoo stores datetimes (UTC)."""
    local = datetime.now(UTC).astimezone(AR_TZ)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(UTC).replace(tzinfo=None)


def pedidos_de_hoy(env):
    """AgilPedido orders that came by WhatsApp today (the tile's badge)."""
    return env["sale.order"].sudo().search_count([
        ("distri_origen", "=", ORIGEN_AGILPEDIDO_WA), ("create_date", ">=", _inicio_hoy()),
        ("state", "!=", "cancel")])


def _float(value, default=0.0):
    try:
        return float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return default


def _int(value):
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------------- cards

def tarjeta(order):
    """One order of the list (views/agilpedido_app.xml agil_tarjeta)."""
    o = order.sudo()
    generic = bool(o.partner_id.distri_generic_partner)
    nombre = o.distri_nombre or ""
    telefono = _phone(o.distri_telefono) if o.distri_telefono else ""
    if generic:
        cliente = nombre or "Sin nombre"
        sub = " · ".join(v for v in ("Nombre en AgilPedido", telefono) if v)
    else:
        cliente = o.partner_id.display_name
        sub = " · ".join(v for v in (("Nombre en AgilPedido: %s" % nombre) if nombre else "",
                                       telefono) if v)
    cancel = o.state == "cancel"
    dif = o.distri_agil_diferencia if o.distri_agil_total else 0.0
    no_coincide = bool(o.distri_agil_total) and (abs(dif) >= TOLERANCIA or not o.distri_agil_cierra)
    if cancel:
        chip = ("Cancelado", "neutral")
    elif o.distri_agil_por_corregir:
        chip = ("%s a corregir" % o.distri_agil_por_corregir, "err")
    elif no_coincide:
        chip = ("Total no coincide", "warn")
    else:
        chip = ("Suma OK", "ok")
    borrador = o.state in ("draft", "sent")
    return {
        "id": o.id,
        "name": o.name,
        "cuando": "Entró %s" % cuando(o.create_date),
        "cliente": cliente,
        "sub": sub,
        "generic": generic,
        "sugerido": o.distri_agil_cliente_sugerido_id.display_name if generic and
        o.distri_agil_cliente_sugerido_id else "",
        "estado": "Presupuesto" if borrador else ("Aprobado" if o.state == "sale" else "Cancelado"),
        "cancel": cancel,
        "chip": chip,
        "fix": bool(o.distri_agil_por_corregir) and not cancel,
        "no_coincide": no_coincide and not cancel,
        "total_agil": _money(o.distri_agil_total) if o.distri_agil_total else "Sin total",
        "total_odoo": _money(o.amount_total),
        "dif": _money(abs(dif)),
        "dif_warn": abs(dif) >= TOLERANCIA,
        "can_fix": borrador and bool(o.distri_agil_por_corregir),
        "can_asignar": borrador and generic and puede_asignar(order.env),
        "can_abrir": borrador and not generic and not o.distri_agil_por_corregir,
        "url": "/agilpedido/pedido/%s" % o.id,
        "asignar_url": "/revisar/%s/asignar?volver=agilpedido" % o.id,
    }


class AgilPedido(http.Controller):

    # ---------------------------------------------------------------- helpers

    def _check(self):
        user = request.env.user
        if user.share or not puede_agil(request.env) or not installed(request.env):
            raise Forbidden()
        return user

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _html(self, html, status=200):
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _dn(self):
        return kit_context("agilpedido.css", "agilpedido.js")

    def _inicio(self):
        user = request.env.user
        return "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero")

    def _refuse(self, text, status=400, back="/agilpedido"):
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=status)
        return request.redirect("%s?error=%s" % (back, quote(text[:300])), local=True)

    def _wa_orders(self, limit=LIMIT):
        desde = fields.Datetime.now() - timedelta(days=DIAS_LISTA)
        return request.env["sale.order"].search([
            ("distri_origen", "=", ORIGEN_AGILPEDIDO_WA),
            "|", ("create_date", ">=", desde),
            "&", ("state", "in", ("draft", "sent")), ("distri_agil_por_corregir", ">", 0),
        ], order="create_date desc, id desc", limit=limit)

    # ---------------------------------------------------------------- list

    @http.route("/agilpedido", type="http", auth="user", methods=["GET"], sitemap=False)
    def lista(self, ver="todos", ok="", asignado="", sin_wa="", aviso="", error="", **kw):
        self._check()
        cards = [tarjeta(o) for o in self._wa_orders()]
        counts = {"todos": len(cards), "corregir": len([c for c in cards if c["fix"]]),
                  "total": len([c for c in cards if c["no_coincide"]])}
        mensaje = ""
        if ok and asignado:
            from .asignar import asignado_texto
            order = request.env["sale.order"].browse(_int(asignado)).exists()
            if order and not order.distri_partner_generic:
                mensaje = asignado_texto(order, quiso_aprobar=bool(sin_wa))
        elif ok:
            mensaje = "Listo."
        html = request.env["ir.qweb"]._render(PAGE, {
            "dn": self._dn(),
            "cards": cards,
            "counts": counts,
            "tabs": TABS,
            "ver": ver if ver in dict(TABS) else "todos",
            "inicio": self._inicio(),
            "ok": mensaje,
            "error": (error or aviso or "")[:300],
        })
        return self._html(html)

    # ---------------------------------------------------------------- shared panel data

    def _productos(self, q, precio_de, nombre="", limit=8):
        """Products of Odoo for the panel: name or code (also AgilPedido's
        numeric «Código»), with the price of each unit of sale."""
        q = (q or "").strip()[:100]
        if len(q) < 2:
            return []
        env = request.env
        Tmpl = env["product.template"]
        dominio = [("sale_ok", "=", True)]
        if not catalogo_agil_activo(env):
            dominio.append(("distri_agil_temporal", "=", False))
        plantillas = Tmpl.search(dominio + ["|", ("name", "ilike", q), ("default_code", "ilike", q)],
                                 order="name", limit=limit)
        if not plantillas:
            # every word, in any order («dulce leche repostero»)
            palabras = [p for p in normalizar(q).split() if len(p) >= 3 and p not in PALABRAS_RUIDO]
            if palabras:
                plantillas = Tmpl.search(dominio + [("name", "ilike", p) for p in palabras],
                                         order="name", limit=limit)
        if not plantillas:
            # the closest names, as the message reader would propose them
            catalogo = catalogo_normalizado(env)
            claves = {}
            for plantilla, clave in catalogo.items():
                claves.setdefault(clave, plantilla)
            cerca = difflib.get_close_matches(normalizar(q), list(claves), n=limit, cutoff=0.45)
            plantillas = Tmpl.browse([claves[c].id for c in cerca])
        if q.isdigit() and len(q) >= 4:
            por_codigo = producto_por_codigo(env, q).product_tmpl_id
            if por_codigo and por_codigo.sale_ok and por_codigo.active:
                plantillas = por_codigo.with_env(env) | plantillas
        envase = leer_envase(nombre or "")
        contiene = envase and envase["contiene"]
        salida = []
        for plantilla in plantillas[:limit]:
            variante = plantilla.product_variant_id
            if not variante:
                continue
            unidades = []
            for unidad in unidades_de_venta(plantilla):
                precio = precio_de(variante, unidad)
                unidades.append({"id": unidad.id, "name": unidad.name, "price": precio,
                                 "price_text": _money(precio)})
            stock = ""
            if getattr(plantilla, "is_storable", False):
                stock = "stock %s" % cantidad_texto(variante.sudo().qty_available)
            salida.append({
                "id": variante.id,
                "name": plantilla.name,
                "code": variante.default_code or "",
                "stock": stock,
                "units": unidades,
                "unit_default": unidad_de_venta(plantilla, contiene)[0].id,
            })
        return salida

    # ---------------------------------------------------------------- load: paste

    def _importacion(self, imp_id):
        imp = request.env["distri.importar.pedido"].browse(_int(imp_id)).exists()
        if not imp or (imp.create_uid != request.env.user and not request.env.su):
            raise NotFound()
        return imp

    def _linea(self, imp, line_id):
        linea = imp.linea_ids.filtered(lambda l: l.id == _int(line_id))
        if not linea:
            raise NotFound()
        return linea

    def _fila_carga(self, imp, linea):
        precio = imp._distri_precio_unidad(linea)
        sub = precio * linea.cantidad if precio is not None else None
        importe = importe_ar(linea.importe_mensaje) if linea.importe_mensaje else None
        fix = linea._distri_para_corregir()
        chips = []
        if not linea.nombre_mensaje:
            estado = "mano"
        elif fix:
            estado = "fix"
        elif linea.equivalencia_id:
            estado = "aprendida"
            chips.append(("Equivalencia aprendida", "ok"))
        else:
            estado = "ok"
            chips.append(("Coincide 100 %", "ok"))
        if linea.es_mayorista:
            chips.insert(0, ("May", ""))
        plantilla = linea.product_tmpl_id
        unidad = linea.product_uom_id
        limpio = linea._distri_nombre_limpio()
        envase = leer_envase(limpio)
        return {
            "id": linea.id,
            "texto": linea.nombre_mensaje or "",
            "producto": plantilla.name or "",
            "cantidad": cantidad_texto(linea.cantidad),
            "unidad": unidad.name if unidad and plantilla and unidad != plantilla.uom_id else "",
            "cantidad_agil": linea._distri_cantidad_agil(),
            "sub_agil": _money(importe) if importe is not None else "—",
            "importe": importe or 0.0,
            "sub_odoo": _money(sub) if sub is not None else "—",
            "estado": estado,
            "fix": fix,
            "sin_producto": not plantilla,
            "chips": chips,
            "motivo": linea._distri_motivo(),
            "may": bool(linea.es_mayorista),
            "puede_ok": fix and bool(plantilla),
            "product_id": plantilla.product_variant_id.id or 0,
            "uom_id": unidad.id or 0,
            "factor": linea.equivalencia_id.factor if linea.equivalencia_id else 1.0,
            "buscar": plantilla.name or (envase["base"] if envase else limpio),
            "distinto": bool(linea.nombre_mensaje) and limpio != (plantilla.name or ""),
        }

    def _tablero_carga(self, imp):
        filas = [self._fila_carga(imp, l) for l in imp.linea_ids]
        datos = leer_mensaje_agilpedido(imp.mensaje or "")
        total_agil = datos["total"] if datos["total"] is not None else sum(f["importe"] for f in filas)
        total_odoo = 0.0
        for linea in imp.linea_ids:
            precio = imp._distri_precio_unidad(linea)
            total_odoo += (precio or 0.0) * linea.cantidad
        pendientes = len([f for f in filas if f["fix"]])
        bloqueo = ""
        if pendientes:
            bloqueo = "Se crea cuando no quedan renglones para corregir."
        elif not imp.partner_id:
            bloqueo = "Elegí el cliente para crear el presupuesto."
        elif not filas:
            bloqueo = "No hay renglones."
        return {
            "kind": "carga",
            "base": "/agilpedido/cargar/%s" % imp.id,
            "filas": filas,
            "total_agil": _money(total_agil),
            "total_odoo": _money(total_odoo),
            "dif": _money(abs(total_odoo - total_agil)),
            "dif_warn": bool(filas) and abs(total_odoo - total_agil) >= TOLERANCIA,
            "pendientes": pendientes,
            "bloqueo": bloqueo,
            "accion": "Crear el presupuesto",
            "accion_ok": not bloqueo,
        }

    def _board_html(self, board):
        return str(request.env["ir.qweb"]._render("distri_pedido.agil_board", {
            "b": board, "dn": self._dn()}))

    def _resumen(self, imp, board):
        """What the green line above the lines says."""
        texto = imp.resumen or ""
        if board["pendientes"]:
            texto += " Quedan %s para corregir." % board["pendientes"]
        return texto.strip()

    def _sugeridos(self, imp):
        """Customers that may be the buyer (the name of the message): a
        suggestion to tap, never chosen alone."""
        if imp.partner_id:
            return []
        nombre = leer_mensaje_agilpedido(imp.mensaje or "")["nombre"]
        if len((nombre or "").strip()) < 3:
            return []
        socios = request.env["res.partner"]._distri_buscar_clientes(nombre, limit=3)
        return [{"id": s.id, "name": s.display_name,
                 "detail": " · ".join(v for v in (s.city or "", _phone(s.phone) if s.phone else "")
                                      if v)} for s in socios]

    def _carga_page(self, imp=None, error=""):
        listas = request.env["product.pricelist"].search([], order="sequence, id")
        board = self._tablero_carga(imp) if imp and imp.linea_ids else None
        lista_cliente = ""
        if imp and imp.partner_id.property_product_pricelist:
            lista_cliente = imp.partner_id.property_product_pricelist.name
        html = request.env["ir.qweb"]._render("distri_pedido.agil_carga", {
            "dn": self._dn(),
            "imp": imp,
            "b": board,
            "resumen": self._resumen(imp, board) if board else "",
            "sugeridos": self._sugeridos(imp) if board else [],
            "listas": [(p.id, p.name) for p in listas],
            "lista_cliente": lista_cliente or (lista_general(request.env).name or "General"),
            "csrf_token": request.csrf_token(),
            "inicio": self._inicio(),
            "error": error,
        })
        return self._html(html)

    @http.route("/agilpedido/cargar", type="http", auth="user", methods=["GET"], sitemap=False)
    def cargar(self, error="", **kw):
        self._check()
        return self._carga_page(error=(error or "")[:300])

    @http.route("/agilpedido/cargar", type="http", auth="user", methods=["POST"], sitemap=False)
    def leer(self, mensaje="", **kw):
        """«Leer el mensaje»: a new reading of the pasted text."""
        self._check()
        mensaje = (mensaje or "")[:MAX_MENSAJE]
        try:
            with request.env.cr.savepoint():
                imp = request.env["distri.importar.pedido"].create({"mensaje": mensaje})
                imp.action_analizar()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/cargar")
        url = "/agilpedido/cargar/%s" % imp.id
        if self._by_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url, local=True)

    @http.route("/agilpedido/cargar/<int:imp_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def carga(self, imp_id, error="", **kw):
        self._check()
        imp = request.env["distri.importar.pedido"].browse(imp_id).exists()
        if not imp or imp.create_uid != request.env.user:
            return self._carga_page(error="Ese pedido ya no está: pegá el mensaje de nuevo.")
        return self._carga_page(imp, error=(error or "")[:300])

    @http.route("/agilpedido/cargar/<int:imp_id>/leer", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def releer(self, imp_id, mensaje="", **kw):
        """The message edited and read again: the same record."""
        self._check()
        imp = self._importacion(imp_id)
        try:
            with request.env.cr.savepoint():
                imp.mensaje = (mensaje or "")[:MAX_MENSAJE]
                imp.action_analizar()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/cargar/%s" % imp.id)
        return self._carga_done(imp, "Mensaje leído de nuevo.")

    def _carga_done(self, imp, message):
        imp.invalidate_recordset()
        board = self._tablero_carga(imp)
        if self._by_fetch():
            return self._json({"ok": True, "message": message, "board": self._board_html(board),
                               "resumen": self._resumen(imp, board),
                               "cliente": imp.partner_id.display_name or ""})
        return request.redirect("/agilpedido/cargar/%s" % imp.id, local=True)

    @http.route("/agilpedido/cargar/<int:imp_id>/datos", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def datos(self, imp_id, **kw):
        """The customer and the price list (prices change with them)."""
        self._check()
        imp = self._importacion(imp_id)
        valores = {}
        if "partner_id" in kw:
            socio = request.env["res.partner"].browse(_int(kw["partner_id"])).exists()
            if socio and (socio.distri_generic_partner or socio.type != "contact"):
                return self._refuse("Elegí un cliente real, no el contacto genérico.")
            valores["partner_id"] = socio.id or False
        if "pricelist_id" in kw:
            lista = request.env["product.pricelist"].browse(_int(kw["pricelist_id"])).exists()
            valores["pricelist_id"] = lista.id or False
        imp.write(valores)
        return self._carga_done(imp, "Listo.")

    @http.route("/agilpedido/cargar/<int:imp_id>/clientes", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def clientes(self, imp_id, q="", **kw):
        self._check()
        self._importacion(imp_id)
        socios = request.env["res.partner"]._distri_buscar_clientes((q or "")[:100], limit=8)
        return self._json({"ok": True, "clientes": [{
            "id": s.id, "name": s.display_name,
            "detail": " · ".join(v for v in (s.city or "", _phone(s.phone) if s.phone else "",
                                             s.property_product_pricelist.name or "") if v),
        } for s in socios]})

    @http.route("/agilpedido/cargar/<int:imp_id>/productos", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def productos_carga(self, imp_id, q="", may="", cant="1", nombre="", **kw):
        self._check()
        imp = self._importacion(imp_id)
        cantidad = max(_float(cant, 1.0), 1.0)
        mayorista = lista_mayorista(request.env) if may == "1" else None
        lista = imp._distri_lista()

        def precio_de(variante, unidad):
            plantilla = variante.product_tmpl_id
            if may == "1":
                if plantilla.categ_id.name in CATEGORIAS_A_PRESUPUESTAR:
                    return 0.0
                if mayorista:
                    return precio_en_unidad(mayorista, variante, cantidad, unidad)
            if lista:
                return precio_en_unidad(lista, variante, cantidad, unidad)
            return variante.lst_price

        return self._json({"ok": True, "productos": self._productos(q, precio_de, nombre)})

    def _producto_y_unidad(self, kw):
        variante = request.env["product.product"].browse(_int(kw.get("product_id"))).exists()
        if not variante:
            raise UserError("Elegí el producto de Odoo.")
        unidad = request.env["uom.uom"].browse(_int(kw.get("uom_id"))).exists()
        if not unidad:
            unidad = unidad_de_venta(variante.product_tmpl_id)[0]
        factor = _float(kw.get("factor"), 1.0) if kw.get("otra_cantidad") else 1.0
        return variante, unidad, factor

    @http.route("/agilpedido/cargar/<int:imp_id>/renglon/<int:line_id>/<string:accion>",
                type="http", auth="user", methods=["POST"], sitemap=False)
    def renglon_carga(self, imp_id, line_id, accion, **kw):
        """«Está bien» (ok), «Corregir y recordar» (corregir), «Quitar»."""
        self._check()
        imp = self._importacion(imp_id)
        linea = self._linea(imp, line_id)
        try:
            with request.env.cr.savepoint():
                if accion == "ok":
                    linea.action_distri_ok()
                    message = "Listo: queda aprendido para los próximos pedidos."
                elif accion == "corregir":
                    variante, unidad, factor = self._producto_y_unidad(kw)
                    linea._distri_corregir(variante, unidad, factor)
                    message = ("Corregido y aprendido: la próxima vez se carga solo."
                               if linea.nombre_mensaje else "Renglón corregido.")
                elif accion == "quitar":
                    linea.unlink()
                    message = "Renglón quitado."
                else:
                    raise NotFound()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/cargar/%s" % imp.id)
        return self._carga_done(imp, message)

    @http.route("/agilpedido/cargar/<int:imp_id>/agregar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def agregar(self, imp_id, cantidad="1", **kw):
        """«Agregar renglón a mano»."""
        self._check()
        imp = self._importacion(imp_id)
        try:
            with request.env.cr.savepoint():
                variante, unidad, _factor = self._producto_y_unidad(kw)
                imp._distri_agregar(variante, unidad, _float(cantidad, 0.0))
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/cargar/%s" % imp.id)
        return self._carga_done(imp, "Renglón agregado.")

    @http.route("/agilpedido/cargar/<int:imp_id>/crear", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def crear(self, imp_id, **kw):
        """«Crear el presupuesto»: only with no line left to check."""
        self._check()
        imp = self._importacion(imp_id)
        board = self._tablero_carga(imp)
        if board["bloqueo"]:
            return self._refuse(board["bloqueo"], back="/agilpedido/cargar/%s" % imp.id)
        try:
            with request.env.cr.savepoint():
                action = imp.action_crear_presupuesto()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/cargar/%s" % imp.id)
        url = "/agilpedido/creado/%s" % action["res_id"]
        if self._by_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url, local=True)

    @http.route("/agilpedido/creado/<int:order_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def creado(self, order_id, **kw):
        self._check()
        order = request.env["sale.order"].browse(order_id).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        o = order.sudo()
        html = request.env["ir.qweb"]._render("distri_pedido.agil_creado", {
            "dn": self._dn(),
            "o": o,
            "total": _money(o.amount_total),
            "lineas": [{"q": cantidad_texto(l.product_uom_qty),
                        "unidad": l.product_uom_id.name if l.product_uom_id != l.product_id.uom_id else "",
                        "name": l.product_id.name, "sub": _money(l.price_subtotal)}
                       for l in o.order_line if not l.display_type],
            "inicio": self._inicio(),
        })
        return self._html(html)

    # ---------------------------------------------------------------- a WhatsApp order

    def _pedido(self, order_id):
        order = request.env["sale.order"].browse(_int(order_id)).exists()
        if not order or order.distri_origen != ORIGEN_AGILPEDIDO_WA:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        return order

    def _fila_pedido(self, line):
        renglon = leer_renglon(line.distri_agil_texto or "")
        sin_producto = bool(line.distri_agil_sin_producto)
        chips = []
        if not line.distri_agil_texto:
            estado = "mano"
        elif line.distri_agil_corregir:
            estado = "fix"
        elif line.distri_agil_equivalencia_id:
            estado = "aprendida"
            chips.append(("Equivalencia aprendida", "ok"))
        else:
            estado = "ok"
            chips.append(("Coincide 100 %", "ok") if (line.distri_agil_parecido or 0) >= 100
                         else ("Revisado", "ok"))
        if renglon and renglon["es_mayorista"]:
            chips.insert(0, ("May", ""))
        nombre = renglon["nombre"] if renglon else (line.distri_agil_texto or "")
        limpio = renglon["nombre_limpio"] if renglon else nombre
        envase = leer_envase(limpio)
        producto = "" if sin_producto else line.product_id.name
        factor = line.distri_agil_equivalencia_id.factor if line.distri_agil_equivalencia_id else 1.0
        cantidad_agil = renglon["cantidad"] if renglon else line.product_uom_qty
        return {
            "id": line.id,
            "texto": nombre,
            "producto": producto,
            "cantidad": cantidad_texto(line.product_uom_qty),
            "unidad": line.product_uom_id.name if line.product_uom_id and not sin_producto
            and line.product_uom_id != line.product_id.uom_id else "",
            "cantidad_agil": cantidad_agil,
            "sub_agil": _money(line.distri_agil_importe) if line.distri_agil_texto else "—",
            "importe": line.distri_agil_importe or 0.0,
            "sub_odoo": "—" if sin_producto else _money(line.price_subtotal),
            "estado": estado,
            "fix": bool(line.distri_agil_corregir),
            "sin_producto": sin_producto,
            "chips": chips,
            "motivo": (line.distri_agil_motivo or "") if line.distri_agil_corregir else "",
            "may": bool(renglon and renglon["es_mayorista"]),
            "puede_ok": bool(line.distri_agil_corregir) and not sin_producto,
            "product_id": 0 if sin_producto else line.product_id.id,
            "uom_id": line.product_uom_id.id or 0,
            "factor": factor,
            "buscar": producto or (envase["base"] if envase else limpio),
            "distinto": bool(line.distri_agil_texto) and limpio != producto,
        }

    def _tablero_pedido(self, order):
        o = order.sudo()
        lineas = o.order_line.filtered(lambda l: not l.display_type)
        filas = [self._fila_pedido(l) for l in lineas]
        pendientes = len([f for f in filas if f["fix"]])
        borrador = o.state in ("draft", "sent")
        generic = bool(o.partner_id.distri_generic_partner)
        bloqueo = ""
        if not borrador:
            bloqueo = "El pedido ya está %s." % ("aprobado" if o.state == "sale" else "cancelado")
        elif pendientes:
            bloqueo = "Se aprueba cuando no quedan renglones para corregir."
        elif generic:
            bloqueo = "Asigná el cliente para aprobarlo."
        dif = o.amount_total - o.distri_agil_total if o.distri_agil_total else 0.0
        return {
            "kind": "pedido",
            "base": "/agilpedido/pedido/%s" % o.id,
            "filas": filas,
            "total_agil": _money(o.distri_agil_total) if o.distri_agil_total else "Sin total",
            "total_odoo": _money(o.amount_total),
            "dif": _money(abs(dif)),
            "dif_warn": abs(dif) >= TOLERANCIA,
            "pendientes": pendientes,
            "bloqueo": bloqueo,
            "accion": "Aprobar el pedido",
            "accion_ok": not bloqueo,
            "editable": borrador,
            "asignar_url": "/revisar/%s/asignar?volver=agilpedido" % o.id
            if borrador and generic and puede_asignar(order.env) else "",
        }

    def _pedido_done(self, order, message):
        order.invalidate_recordset()
        board = self._tablero_pedido(order)
        if self._by_fetch():
            return self._json({"ok": True, "message": message, "board": self._board_html(board)})
        return request.redirect("/agilpedido/pedido/%s" % order.id, local=True)

    @http.route("/agilpedido/pedido/<int:order_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def pedido(self, order_id, error="", **kw):
        self._check()
        order = self._pedido(order_id)
        o = order.sudo()
        generic = bool(o.partner_id.distri_generic_partner)
        info = [("Nombre en AgilPedido: %s" % o.distri_nombre) if o.distri_nombre else "",
                _phone(o.distri_telefono) if o.distri_telefono else "",
                ("Lista %s" % o.pricelist_id.name) if o.pricelist_id else ""]
        html = request.env["ir.qweb"]._render("distri_pedido.agil_pedido", {
            "dn": self._dn(),
            "o": o,
            "cliente": (o.distri_nombre or "Sin cliente") if generic else o.partner_id.display_name,
            "generic": generic,
            "sugerido": o.distri_agil_cliente_sugerido_id.display_name if generic and
            o.distri_agil_cliente_sugerido_id else "",
            "info": " · ".join(v for v in info if v),
            "b": self._tablero_pedido(order),
            "lateral": [tarjeta(p) for p in self._wa_orders(limit=LIMIT_LATERAL)],
            "csrf_token": request.csrf_token(),
            "inicio": self._inicio(),
            "error": (error or "")[:300],
        })
        return self._html(html)

    @http.route("/agilpedido/pedido/<int:order_id>/productos", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def productos_pedido(self, order_id, q="", may="", cant="1", nombre="", **kw):
        self._check()
        order = self._pedido(order_id)
        cantidad = max(_float(cant, 1.0), 1.0)
        mayorista, minorista = order.env["sale.order"]._distri_agil_listas()

        def precio_de(variante, unidad):
            precio, _sin_regla = order.env["sale.order"]._distri_agil_precio(
                variante, unidad, cantidad, may == "1", mayorista, minorista)
            return precio.get("price_unit", variante.lst_price)

        return self._json({"ok": True, "productos": self._productos(q, precio_de, nombre)})

    @http.route("/agilpedido/pedido/<int:order_id>/renglon/<int:line_id>/<string:accion>",
                type="http", auth="user", methods=["POST"], sitemap=False)
    def renglon_pedido(self, order_id, line_id, accion, **kw):
        """«Está bien» (ok) and «Corregir y recordar» (corregir) on a line of
        an order that came by WhatsApp: the backend's buttons."""
        self._check()
        order = self._pedido(order_id)
        line = order.order_line.filtered(lambda l: l.id == line_id)
        if not line:
            raise NotFound()
        try:
            with request.env.cr.savepoint():
                if accion == "ok":
                    line.action_distri_agil_renglon_ok()
                    message = "Listo: queda aprendido para los próximos pedidos."
                elif accion == "corregir":
                    variante, unidad, factor = self._producto_y_unidad(kw)
                    wizard = request.env["distri.agil.corregir.renglon"].with_context(
                        default_linea_id=line.id).create({
                            "linea_id": line.id, "product_id": variante.id,
                            "product_uom_id": unidad.id,
                            "otra_cantidad": factor != 1.0, "factor": factor})
                    wizard.action_corregir()
                    message = "Corregido y aprendido: la próxima vez se carga solo."
                else:
                    raise NotFound()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/pedido/%s" % order.id)
        return self._pedido_done(order, message)

    @http.route("/agilpedido/pedido/<int:order_id>/aprobar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def aprobar(self, order_id, **kw):
        self._check()
        order = self._pedido(order_id)
        board = self._tablero_pedido(order)
        if board["bloqueo"]:
            return self._refuse(board["bloqueo"], back="/agilpedido/pedido/%s" % order.id)
        try:
            with request.env.cr.savepoint():
                result = order.with_context(distri_wa_ask=True).action_confirm()
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc), back="/agilpedido/pedido/%s" % order.id)
        if isinstance(result, dict) and order.state != "sale":
            return self._refuse("El cliente no tiene WhatsApp cargado: cargalo con «Pegar número» "
                                "en «Pedidos para revisar» y aprobalo.",
                                back="/agilpedido/pedido/%s" % order.id)
        return self._pedido_done(order, "Listo: %s aprobado." % order.name)


def catalogo_agil_activo(env):
    from ..models.agil_tienda import catalogo_agil_activo as activo
    return activo(env)
