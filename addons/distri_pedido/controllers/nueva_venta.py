"""«Nueva venta» on the kit (19.0.1.129.30, stage 1 of the owner-approved
proposal «una pantalla, cuatro salidas»; models/nueva_venta.py has the
rules). Behind distri_pedido.nueva_venta, OFF by default: with it off every
route answers 404 and Inicio has no tile.

    /tablero
      /nueva-venta                    GET   the screen: customer, cart, exit
        /nueva-venta/catalogo         GET   JSON: the catalog priced with the
                                            customer's list (?cliente_id=)
        /nueva-venta/pegar            POST  JSON: «Pegar mensaje de AgilPedido»,
                                            the cart read by today's parser
                                            (distri.importar.pedido)
        /nueva-venta/crear            POST  the sale with its exit
        /nueva-venta/hecho/<order>    GET   what was done and what follows

The customer search is /pedido's own (/pedido/clientes: portfolio first,
an external seller only his customers). «A armar» and «Presupuesto» post
the cart to /pedido's own code (controllers/main.py TomaDePedidos.enviar),
so the order is exactly the one «Tomar un pedido» makes; «Mostrador» goes
on to the depot delivery form (/deposito/entrega/<id>?mostrador=1), where
the payment is registered as today. Errors answer JSON (fetch, X-Distri-
Fetch: 1) or the page again.
"""
import hashlib
import json
import re
import uuid
from urllib.parse import urlencode

import psycopg2
from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models import nueva_venta as nv
from ..models.reparto import _money
from .kit import kit_context, staff_nav
from .main import MAX_CANTIDAD, MENSAJE_CLIENTE_AJENO, MENSAJE_DISTRIBUIDORA, TomaDePedidos
from .tablero import wants_tablero

TEMPLATE = "distri_pedido.nueva_venta_page"
GRUPOS_PEGAR = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")
MAX_MENSAJE = 20000
TAG_RE = re.compile(r"<[^>]+>")
SPACES_RE = re.compile(r"\s+")


def puede_pegar(user):
    """«Pegar mensaje de AgilPedido»: the groups of /agilpedido/cargar."""
    return any(user.has_group(g) for g in GRUPOS_PEGAR)


def _error_text(exc):
    return exc.args[0] if exc.args else str(exc)


# 19.0.1.129.32: the second of two simultaneous posts of the same sale
DOBLE_TOQUE = "Esa venta ya se está cargando (se tocó dos veces). Fijate en «Mis ventas»."

class NuevaVenta(http.Controller):

    # ---------------------------------------------------------------- helpers

    def _check(self):
        env = request.env
        if not nv.nueva_venta_on(env):
            raise NotFound()
        user = env.user
        if not nv.puede_nueva_venta(user):
            raise Forbidden()
        return user

    def _pedido(self):
        """/pedido's controller: its catalog, prices and order code."""
        return TomaDePedidos()

    def _by_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, data, status=200):
        return request.make_json_response(data, status=status,
                                          headers=[("Cache-Control", "no-store")])

    def _inicio(self, user):
        return "/tablero" if wants_tablero(user) else (user._distri_landing() or "/tablero")

    def _render(self, template, values, status=200):
        user = request.env.user
        base = {
            "dn": kit_context("nueva_venta.css", "nueva_venta.js"),
            "user": user,
            "inicio": self._inicio(user),
            "csrf_token": request.csrf_token(),
            "nav": staff_nav(request.env, "nuevaventa"),
            "money": _money,
            "error": "",
            "ok": "",
        }
        base.update(values)
        html = request.env["ir.qweb"]._render(template, base)
        return request.make_response(html, status=status, headers=[
            ("Content-Type", "text/html; charset=utf-8"), ("Cache-Control", "no-store")])

    def _refuse(self, text, status=400):
        if self._by_fetch():
            return self._json({"ok": False, "error": text}, status=status)
        return self._page(error=text, status=status)

    def _done(self, url):
        if self._by_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url, 303)

    def _cliente(self, valor):
        """The customer this user may sell to, or a refusal text."""
        cliente = self._pedido()._cliente_elegido(valor)
        if not cliente:
            return cliente, "Elegí el cliente."
        user = request.env.user
        if not user._distri_may_reach_customer(cliente):
            return cliente.browse(), MENSAJE_CLIENTE_AJENO
        from ..models.lista_oculta import solo_vendedor
        if solo_vendedor(user) and cliente._distri_es_distribuidora():
            return cliente.browse(), MENSAJE_DISTRIBUIDORA
        return cliente, ""

    def _lista(self, cliente):
        """The customer's price list: what the order ends up priced with."""
        return cliente.property_product_pricelist or self._pedido()._lista_general()

    # ---------------------------------------------------------------- screen

    def _page(self, error="", status=200):
        user = request.env.user
        salidas = nv.salidas_de(user)
        return self._render(TEMPLATE, {
            "salidas": [s for s in nv.SALIDAS if s[0] in salidas],
            "pegar": puede_pegar(user),
            # one per page shown: the same form posted twice gives one sale
            "envio": uuid.uuid4().hex,
            "motivo": nv.MOTIVO_ENTREGADO,
            "error": error,
        }, status=status)

    @http.route("/nueva-venta", type="http", auth="user", methods=["GET"], sitemap=False)
    def page(self, **kw):
        self._check()
        return self._page()

    # website=True as /pedido and /pedido/enviar: the same company and
    # price list context as the order «Tomar un pedido» makes
    @http.route("/nueva-venta/catalogo", type="http", auth="user", methods=["GET"],
                website=True, sitemap=False)
    def catalogo(self, cliente_id="", **kw):
        """The products /pedido offers, each in its unit of sale and priced
        with the customer's list (as the order will be)."""
        self._check()
        cliente, error = self._cliente(cliente_id)
        if error:
            return self._json({"ok": False, "error": error}, status=403 if cliente_id else 400)
        pedido = self._pedido()
        secciones = pedido._catalogo()
        ids = [r["id"] for _nombre, filas in secciones for r in filas]
        permitidas = pedido._plantillas_del_catalogo(ids)
        lista = self._lista(cliente)
        precios = pedido._precios_en_lote(permitidas, lista)
        vendibles = set(permitidas.ids)
        productos = []
        for seccion, filas in secciones:
            for fila in filas:
                if fila["id"] not in vendibles:
                    continue
                precio = precios.get(fila["id"], 0.0)
                productos.append({
                    "id": fila["id"], "nombre": fila["nombre"], "seccion": seccion or "",
                    "unidad": fila["unidad"], "contiene": fila["contiene"],
                    "precio": precio, "precio_texto": _money(precio) if precio > 0 else "",
                })
        return self._json({
            "ok": True,
            "cliente": {"id": cliente.id, "nombre": cliente.name,
                        "whatsapp": cliente._distri_whatsapp_shown() or "",
                        "domicilio": ", ".join(x for x in (cliente.street, cliente.city) if x),
                        "lista": lista.name or ""},
            "productos": productos,
        })

    # ---------------------------------------------------------------- AgilPedido

    @http.route("/nueva-venta/pegar", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def pegar(self, mensaje="", **kw):
        """«Pegar mensaje de AgilPedido»: today's parser (models/
        importar_pedido.py action_analizar) fills the cart. Only the lines it
        identified come back; the rest are listed to be added by hand."""
        user = self._check()
        if not puede_pegar(user):
            return self._json({"ok": False, "error": "Solo administración y gerencia pegan "
                                                     "mensajes de AgilPedido."}, status=403)
        mensaje = (mensaje or "")[:MAX_MENSAJE]
        try:
            with request.env.cr.savepoint():
                imp = request.env["distri.importar.pedido"].create({"mensaje": mensaje})
                imp.action_analizar()
                renglones, sueltos = self._renglones_pegados(imp)
                # the reading is only for the cart: nothing of it is kept
                imp.unlink()
        except (UserError, AccessError, ValidationError) as exc:
            return self._json({"ok": False, "error": _error_text(exc)}, status=400)
        return self._json({"ok": True, "renglones": renglones, "sin_identificar": sueltos})

    def _renglones_pegados(self, imp):
        """(cart lines {id, cantidad}, names not identified). A line read in
        another unit than the catalog's goes in the catalog's unit when it
        is a whole number of them; otherwise it is listed to check."""
        renglones, sueltos = {}, []
        for linea in imp.linea_ids:
            plantilla = linea.product_tmpl_id
            if not plantilla or not linea.cantidad or linea.cantidad <= 0:
                sueltos.append(linea.nombre_mensaje or "")
                continue
            venta = nv.unidad_de_venta(plantilla)
            unidad = linea.product_uom_id or venta
            cantidad = linea.cantidad
            if unidad != venta:
                cantidad = unidad._compute_quantity(cantidad, venta, round=False)
            if abs(cantidad - round(cantidad)) > 1e-6 or round(cantidad) < 1:
                sueltos.append("%s (revisá la cantidad)" % (linea.nombre_mensaje or plantilla.name))
                continue
            renglones[plantilla.id] = renglones.get(plantilla.id, 0) + int(round(cantidad))
        return [{"id": k, "cantidad": v} for k, v in renglones.items()], sueltos

    # ---------------------------------------------------------------- create

    def _clave(self, envio, renglones, salida, cliente):
        """The key of one form shown (as /pedido's C8): a second post of the
        same form finds the sale of the first."""
        envio = re.sub(r"[^0-9a-f]", "", (envio or "").lower())[:64]
        if len(envio) < 16:
            return ""
        partes = [request.session.sid or "", "nueva-venta", envio, salida, str(cliente.id),
                  json.dumps(sorted((r["id"], r["cantidad"]) for r in renglones)),
                  str(request.env.uid or "")]
        return hashlib.sha256("|".join(partes).encode()).hexdigest()

    def _texto_de_respuesta(self, response):
        """The message of a refusal page of /pedido (_error_simple,
        _error_volver), as plain text."""
        try:
            body = response.get_data(as_text=True)
        except Exception:  # noqa: BLE001 (a lazy response: nothing to read)
            body = ""
        text = SPACES_RE.sub(" ", TAG_RE.sub(" ", body or "")).replace(" Volver", "").strip()
        return text[:400] or "No se pudo cargar la venta."

    def _por_pedido(self, form, cliente, renglones):
        """«A armar» and «Presupuesto»: the order of «Tomar un pedido»,
        through /pedido's own code with what its page would post for this
        customer. Returns (order, error)."""
        pedido = self._pedido()
        domicilio = (form.get("domicilio") or "").strip()[:200]
        telefono = (form.get("telefono") or "").strip()[:40]
        post = {
            "renglones": json.dumps(renglones),
            "csrf_token": form.get("csrf_token") or "",
            "envio": form.get("envio") or "",
            "cliente_id": str(cliente.id),
            "nombre": cliente.name,
            "telefono": telefono,
            "domicilio": domicilio,
            "entrega": form.get("entrega") if form.get("entrega") in ("domicilio", "retira")
            else "domicilio",
            "comentario": form.get("comentario") or "",
            "nota_deposito": form.get("nota_deposito") or "",
            "token": "",
        }
        validos, error = pedido._renglones_validos(post["renglones"], MAX_CANTIDAD)
        if error:
            return None, error
        clave = pedido._clave_envio(post, validos)
        response = pedido.enviar(**post)
        order = request.env["sale.order"].sudo().search(
            [("distri_envio_clave", "=", clave)], limit=1) if clave else None
        if not order:
            return None, self._texto_de_respuesta(response)
        return request.env["sale.order"].browse(order.id), ""

    def _renglones_admin(self, renglones):
        """«Mostrador» and «Ya entregado»: [(template, quantity)] of the
        products /pedido offers (the same rule as its form), or an error."""
        pedido = self._pedido()
        validos, error = pedido._renglones_validos(json.dumps(renglones), MAX_CANTIDAD)
        if error:
            return None, error
        permitidas = pedido._plantillas_del_catalogo(r["id"] for r in validos)
        fuera = [r["id"] for r in validos if r["id"] not in permitidas.ids]
        if fuera:
            return None, ("Estos productos ya no están en el catálogo: %s. Sacalos del carrito."
                          % pedido._nombres_descartados(fuera))
        por_id = {p.id: p for p in permitidas}
        return [(por_id[r["id"]], r["cantidad"]) for r in validos if r["cantidad"]], ""

    def _form(self):
        form = request.httprequest.form
        return {key: form.get(key) for key in form}

    @http.route("/nueva-venta/crear", type="http", auth="user", methods=["POST"],
                website=True, sitemap=False)
    def crear(self, **kw):
        user = self._check()
        form = self._form()
        salida = form.get("salida") or ""
        if salida not in dict((s[0], s) for s in nv.SALIDAS):
            return self._refuse("Elegí cómo sigue la venta.")
        if salida not in nv.salidas_de(user):
            return self._refuse("Esa salida es solo para administración y gerencia.", status=403)
        cliente, error = self._cliente(form.get("cliente_id"))
        if error:
            return self._refuse(error, status=403 if form.get("cliente_id") else 400)
        try:
            renglones = json.loads(form.get("renglones") or "[]")
        except ValueError:
            renglones = None
        if not isinstance(renglones, list) or not renglones:
            return self._refuse("Agregá al menos un producto.")
        try:
            with request.env.cr.savepoint():
                if salida in (nv.ARMAR, nv.PRESUPUESTO):
                    order, error = self._por_pedido(form, cliente, renglones)
                    if error:
                        raise UserError(error)
                    if salida == nv.ARMAR and order.state in ("draft", "sent"):
                        result = order.action_confirm()
                        if isinstance(result, dict):
                            raise UserError("Falta el WhatsApp del cliente para confirmar.")
                    url = "/nueva-venta/hecho/%s" % order.id
                else:
                    url = self._crear_admin(form, cliente, renglones, salida)
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc))
        return self._done(url)

    def _crear_admin(self, form, cliente, renglones, salida):
        """«Mostrador» and «Ya entregado»: a backend quotation and the
        existing step. Returns the URL to go to."""
        lineas, error = self._renglones_admin(renglones)
        if error:
            raise UserError(error)
        if not lineas:
            raise UserError("Agregá al menos un producto.")
        Order = request.env["sale.order"]
        clave = self._clave(form.get("envio"), [{"id": p.id, "cantidad": q} for p, q in lineas],
                            salida, cliente)
        previo = Order.sudo().search([("distri_envio_clave", "=", clave)], limit=1) \
            if clave else Order
        if previo:
            # a double tap: the first post already did it
            return self._url_de(Order.browse(previo.id), salida)
        if salida == nv.ENTREGADO:
            # checked before anything is created
            Order._distri_fecha_entregado(form.get("fecha"))
            if not " ".join((form.get("motivo") or "").split()):
                raise UserError("Poné el motivo de la entrega fuera del sistema.")
        order = Order._distri_nueva_venta_presupuesto(cliente, lineas)
        if clave:
            # 19.0.1.129.32: written to the database at once, so a parallel
            # second tap waits here for the first one and then hits the
            # unique key: a clear message instead of a server error
            try:
                with request.env.cr.savepoint():
                    order.sudo().distri_envio_clave = clave
                    order.sudo().flush_recordset(["distri_envio_clave"])
            except psycopg2.IntegrityError:
                order.sudo().unlink()
                raise UserError(DOBLE_TOQUE) from None
        if salida == nv.MOSTRADOR:
            order._distri_nueva_venta_mostrador()
        else:
            order._distri_nueva_venta_ya_entregado(form.get("fecha"), form.get("motivo"))
        return self._url_de(order, salida)

    def _url_de(self, order, salida):
        if salida == nv.MOSTRADOR and order.sudo().distri_delivery_state != "delivered":
            return "/deposito/entrega/%s?mostrador=1" % order.id
        return "/nueva-venta/hecho/%s" % order.id

    # ---------------------------------------------------------------- done

    @http.route("/nueva-venta/hecho/<int:order_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def hecho(self, order_id, **kw):
        self._check()
        order = request.env["sale.order"].browse(order_id).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        o = order.sudo()
        if o.distri_entregado_fuera:
            titulo, texto = "Venta cargada como entregada", (
                "Quedó entregada el %s (%s), con remito y factura. No entra a ninguna ruta." % (
                    o.distri_entregado_fuera_fecha.strftime("%d/%m/%Y")
                    if o.distri_entregado_fuera_fecha else "", o.distri_entregado_fuera_motivo
                    or ""))
        elif o.state == "sale":
            titulo, texto = "Pedido confirmado", "Va a Armado. La factura sale al terminar el armado."
        elif o.state in ("draft", "sent"):
            titulo, texto = "Presupuesto guardado", "Queda sin confirmar, como un presupuesto."
        else:
            titulo, texto = "Venta", ""
        enviar = ""
        if o.distri_wa_state == "staff" and o._distri_wa_customer_number():
            enviar = self._pedido()._url_enviar_al_cliente(
                o, o.partner_id.name, o._distri_wa_customer_number())
        return self._render("distri_pedido.nueva_venta_hecho", {
            "order": o, "titulo": titulo, "texto": texto, "enviar": enviar,
            "total": _money(o.amount_total),
            "factura": o._distri_factura_viva_texto() if o.state == "sale" else "",
            "mis_ventas": "/mis-ventas?" + urlencode({"q": o.name}),
        })
