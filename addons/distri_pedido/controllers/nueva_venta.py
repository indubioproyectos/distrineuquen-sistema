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
        /nueva-venta/productos        GET   19.0.1.129.40: /pedido's catalog
                                            screen as a full-screen product
                                            picker (?cliente_id=)
        /nueva-venta/cliente          POST  19.0.1.129.40: JSON, «Nuevo cliente»
        /nueva-venta/cliente/<id>     POST  19.0.1.129.40: JSON, «Datos del
                                            cliente» (WhatsApp, address)

The customer search is /pedido's own (/pedido/clientes: portfolio first,
an external seller only his customers). «A armar» and «Presupuesto» post
the cart to /pedido's own code (controllers/main.py TomaDePedidos.enviar),
so the order is exactly the one «Tomar un pedido» makes; «Mostrador» goes
on to the depot delivery form (/deposito/entrega/<id>?mostrador=1), where
the payment is registered as today. Errors answer JSON (fetch, X-Distri-
Fetch: 1) or the page again.

19.0.1.129.35 (the rest of the plan's Etapa 4, same flag):
- the customer first (#59): once picked, his last orders can be repeated
  («Repetir»: their lines that are still in his catalog, in the catalog's
  unit) and, when /cuentas shows him to the user, a link to his account;
  the cart, the customer and the answers are kept on the phone as a draft
  until the sale is loaded (static/src/nueva_venta/nueva_venta.js);
- «¿Cuál es el WhatsApp del cliente?» when he has none, instead of refusing
  at the confirmation: the number is stored as the backend wizard stores it
  (models/nueva_venta.py guardar_whatsapp_nueva_venta), or «No tiene
  WhatsApp» while distri_pedido.pedido_requiere_whatsapp is off;
- «Va por encomienda» for «A armar» and «Presupuesto»: /pedido's own
  «Soy del interior» fields, posted to its own code;
- #56: «Nombre y apellido» of who ordered and «Registrar la ubicación» (the
  phone's GPS at the shop; it becomes the customer's location only when he
  has none).
The «Distribuidoras» section of /pedido is left out on purpose: the plan
does not define it for «Nueva venta» and the customer's list already prices
a distributor.

19.0.1.129.40 («Nueva venta más rápida», owner 25/11; same flag):
- a customer on file needs nothing else: the phone and the address of the
  order come from his record (controllers/main.py _datos_faltantes `ficha`),
  and the WhatsApp is asked only at an exit that confirms while
  distri_pedido.pedido_requiere_whatsapp is on (with it off, no answer =
  confirmed without WhatsApp, with the chatter note of 129.35);
- «Datos del cliente» (WhatsApp, address, town) and «Nuevo cliente» in a
  sheet of the same page, written as the user (his ACLs and record rules:
  nothing is widened), so the draft stays as it was;
- «Elegir productos» opens /pedido's own catalog screen (template
  distri_pedido.pantalla, its rows, search and steppers) priced with the
  customer's list; «Confirmar productos» writes the cart into the draft on
  the phone and goes back to /nueva-venta at the exit step;
- the same product posted twice is one line with the sum (juntar_renglones).
"""
import hashlib
import json
import re
import uuid
from urllib.parse import urlencode

import psycopg2
from werkzeug.exceptions import Forbidden, NotFound

from odoo import fields, http
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.http import request

from ..models import nueva_venta as nv
from ..models.reparto import _money
from .kit import kit_context, staff_nav, ui_v2_on
from .main import (CTX_DATOS_DE_LA_FICHA, MAX_CANTIDAD, MENSAJE_CLIENTE_AJENO,
                   MENSAJE_DISTRIBUIDORA, TomaDePedidos)
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


def crear_cliente(user, valores, donde):
    """19.0.1.129.40 «Nuevo cliente» (19.0.1.129.41: shared with «Contactos»,
    controllers/contactos.py). The backend ACLs are not widened (a seller
    still cannot create contacts there): like /cobro's «Cliente nuevo»
    (models/cliente_provisorio.py) the kit creates this one minimal customer
    with sudo, keeping the user as its creator. A seller's customer carries
    his portfolio tag and him as salesperson (an external seller's is his as
    its creator) and is «Cliente nuevo · a confirmar» for administración.
    `donde` names the screen in the notes. UserError / ValidationError go to
    the caller."""
    valores = dict(valores, company_type="company", customer_rank=1)
    if not user.has_group("distri_pedido.grupo_administracion"):
        valores.update({"user_id": user.id, "distri_cliente_provisorio": True,
                        "distri_provisorio_user_id": user.id,
                        "distri_provisorio_fecha": fields.Datetime.now(),
                        "distri_provisorio_nota": "Creado en «%s»" % donde})
        if user.etiqueta_cartera_id:
            valores["category_id"] = [(4, user.etiqueta_cartera_id.id)]
    with request.env.cr.savepoint():
        # sudo keeps the uid: create_uid is the user
        socio = request.env["res.partner"].sudo().create(valores)
        socio.message_post(body="Cliente creado por %s en «%s»." % (user.name, donde))
    return socio


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

    def _wa_requerido(self):
        return request.env["distri.pedido.whatsapp.settings"].requires_whatsapp()

    @staticmethod
    def _sin_whatsapp(cliente):
        """No WhatsApp on file (the res.partner field, his or his company's):
        what the confirmation would ask (pedido_whatsapp.py)."""
        socio = cliente.sudo()
        return not (socio.distri_whatsapp or socio.commercial_partner_id.distri_whatsapp)

    def _anteriores(self, cliente, vendibles):
        """#59 «Repetir»: the customer's last orders the user may read, each
        with its lines that are still in this catalog, in the catalog's unit
        (a quantity that is not a whole number of it is left out)."""
        Order = request.env["sale.order"]
        try:
            orders = Order.search([("partner_id", "child_of", cliente.id),
                                   ("state", "in", ("draft", "sent", "sale"))],
                                  order="date_order desc, id desc", limit=nv.REPETIR_ULTIMOS)
        except AccessError:
            return []
        result = []
        for order in orders.sudo():
            renglones, fuera = {}, 0
            for line in order.order_line.filtered(lambda l: not l.display_type):
                plantilla = line.product_id.product_tmpl_id
                if plantilla.id not in vendibles or line.product_uom_qty <= 0:
                    fuera += 1
                    continue
                venta = nv.unidad_de_venta(plantilla)
                cantidad = line.product_uom_qty
                if line.product_uom_id and line.product_uom_id != venta:
                    cantidad = line.product_uom_id._compute_quantity(cantidad, venta, round=False)
                if abs(cantidad - round(cantidad)) > 1e-6 or round(cantidad) < 1:
                    fuera += 1
                    continue
                renglones[plantilla.id] = renglones.get(plantilla.id, 0) + int(round(cantidad))
            if not renglones:
                continue
            result.append({
                "nombre": order.name,
                "fecha": order.date_order.strftime("%d/%m/%Y") if order.date_order else "",
                "total": _money(order.amount_total),
                "renglones": [{"id": k, "cantidad": v} for k, v in renglones.items()],
                "fuera": fuera,
            })
        return result

    def _cuenta_url(self, cliente):
        """/cuentas on the customer, when the accounts app shows him to the
        user (the order screen's rule, controllers/fichas.py)."""
        from .fichas import Fichas
        return Fichas()._account_link(cliente.sudo().commercial_partner_id)

    def _ficha_url(self, cliente):
        """19.0.1.129.40: the kit's customer screen (/clientes/<id>, ui_v2),
        when the user may open it; «Volver» comes back to «Nueva venta»."""
        from .fichas import Fichas, may_use
        from .kit import ui_v2_on
        if not ui_v2_on(request.env) or not may_use(request.env.user):
            return ""
        return Fichas()._customer_link(cliente, "/nueva-venta")

    def _cliente_json(self, cliente, lista=None):
        """What the screen shows and edits of the picked customer."""
        socio = cliente.sudo()
        sin_wa = self._sin_whatsapp(cliente)
        area = numero = ""
        if sin_wa:
            # a phone on file is offered as the start (the backend wizard's rule)
            from ..models.ar_phone import ar_national, split_ar_national
            for value in (socio.phone, socio.commercial_partner_id.phone):
                national = ar_national(value)
                if national:
                    area, numero = split_ar_national(national)
                    break
        lista = lista if lista is not None else self._lista(cliente)
        return {"id": cliente.id, "nombre": cliente.name,
                "whatsapp": cliente._distri_whatsapp_shown() or "",
                "domicilio": ", ".join(x for x in (socio.street, socio.city) if x),
                "lista": lista.name or "",
                # 19.0.1.129.35
                "sin_whatsapp": sin_wa, "wa_area": area, "wa_numero": numero,
                "cuenta": self._cuenta_url(cliente),
                # 19.0.1.129.40: «Datos del cliente»
                "calle": socio.street or "", "ciudad": socio.city or "",
                "ficha": self._ficha_url(cliente)}

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
            # 19.0.1.129.35: the draft on the phone is per user
            "uid": user.id,
            "wa_requerido": self._wa_requerido(),
            "localidades": self._pedido()._localidades_encomienda(),
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
        lista = self._lista(cliente)
        secciones, vendibles = self._catalogo_del_cliente(lista)
        productos = []
        for seccion, filas in secciones:
            for fila in filas:
                precio = fila["precio"]
                productos.append({
                    "id": fila["id"], "nombre": fila["nombre"], "seccion": seccion or "",
                    "unidad": fila["unidad"], "contiene": fila["contiene"],
                    "precio": precio, "precio_texto": _money(precio) if precio > 0 else "",
                })
        return self._json({
            "ok": True,
            "cliente": self._cliente_json(cliente, lista),
            "productos": productos,
            "anteriores": self._anteriores(cliente, vendibles),
        })

    def _catalogo_del_cliente(self, lista, secciones=None):
        """/pedido's sections with only what «Nueva venta» sells (no
        services, the catalog on now) and each row repriced with the
        customer's list: ([(section, rows)], set of template ids). A row
        without a price there is «precio a confirmar» (sin_precio)."""
        pedido = self._pedido()
        secciones = pedido._catalogo() if secciones is None else secciones
        ids = [r["id"] for _nombre, filas in secciones for r in filas]
        permitidas = pedido._plantillas_del_catalogo(ids)
        precios = pedido._precios_en_lote(permitidas, lista)
        vendibles = set(permitidas.ids)
        result = []
        for seccion, filas in secciones:
            nuevas = []
            for fila in filas:
                if fila["id"] not in vendibles:
                    continue
                precio = precios.get(fila["id"], 0.0)
                nuevas.append(dict(fila, precio=precio, precio_texto=pedido._plata(precio),
                                   sin_precio=precio <= 0))
            if nuevas:
                result.append((seccion, nuevas))
        return result, vendibles

    # ---------------------------------------------------------------- picker

    @http.route("/nueva-venta/productos", type="http", auth="user", methods=["GET"],
                website=True, sitemap=False)
    def productos(self, cliente_id="", **kw):
        """19.0.1.129.40: «Elegir productos» on the whole screen: /pedido's
        own catalog page (template distri_pedido.pantalla: rows, search,
        sections, steppers and bar) with the customer's list prices, and
        «Confirmar productos» instead of «Ver pedido»
        (static/src/nueva_venta/nueva_venta_productos.js puts the cart of the
        draft on the phone in it and back). It never posts an order."""
        user = self._check()
        cliente, error = self._cliente(cliente_id)
        if error:
            return request.redirect("/nueva-venta", 303)
        pedido = self._pedido()
        lista = self._lista(cliente)
        values = pedido._contexto_pantalla()
        secciones, _vendibles = self._catalogo_del_cliente(lista, values["secciones"])
        from .kit import kit_url
        values.update({
            "secciones": secciones,
            # the offers, «Distribuidoras» and cigarettes «a presupuestar» are
            # not in «Nueva venta»'s catalog (their products are in their
            # sections, priced with the customer's list)
            "destacados": [], "mayoristas": [], "presupuestar": [],
            "lista_propia": lista.name or "", "quien": cliente.name,
            "socio_logueado": False, "mostrar_cuenta": False,
            "picker": {"cliente_id": cliente.id, "cliente": cliente.name, "uid": user.id,
                       "volver": "/nueva-venta?volver=1",
                       "js": kit_url("nueva_venta_productos.js")},
        })
        return request.render("distri_pedido.pantalla", values,
                              headers={"Cache-Control": "no-store"})

    # ---------------------------------------------------------------- customer

    def _cliente_valores(self, form, nuevo):
        """The checked values of «Nuevo cliente» / «Datos del cliente», or
        UserError. The WhatsApp as the backend wizard stores it."""
        valores = {}
        if nuevo:
            nombre = " ".join((form.get("nombre") or "").split())[:120]
            if len(nombre) < 3:
                raise UserError("Escribí el nombre del cliente (del comercio o de la persona).")
            valores["name"] = nombre
        area, numero = form.get("wa_area") or "", form.get("wa_numero") or ""
        if area.strip() or numero.strip():
            from ..models.ar_phone import ar_mobile
            try:
                valores["distri_whatsapp"] = ar_mobile(area, numero)
            except ValueError as exc:
                raise UserError("WhatsApp del cliente: %s" % exc) from None
        for campo, clave, largo in (("street", "calle", 120), ("city", "ciudad", 60)):
            if clave in form:
                valores[campo] = " ".join((form.get(clave) or "").split())[:largo] or False
        return valores

    @http.route("/nueva-venta/cliente", type="http", auth="user", methods=["POST"],
                website=True, sitemap=False)
    def cliente_nuevo(self, **kw):
        """19.0.1.129.40: «Nuevo cliente»: name, and optionally WhatsApp,
        address and town. The backend ACLs are not widened (a seller still
        cannot create contacts there): like /cobro's «Cliente nuevo»
        (models/cliente_provisorio.py) the kit creates this one minimal
        customer with sudo, keeping the user as its creator. A seller's
        customer carries his portfolio tag and him as salesperson (an
        external seller's is his as its creator) and is «Cliente nuevo · a
        confirmar» for administración, as /cobro's. Likely existing
        customers he may reach are offered first (/cobro's rule)."""
        user = self._check()
        form = self._form()
        try:
            valores = self._cliente_valores(form, nuevo=True)
        except UserError as exc:
            return self._json({"ok": False, "error": _error_text(exc)}, status=400)
        if form.get("nuevo_ok") != "1":
            telefono = valores.get("distri_whatsapp") or ""
            parecidos = request.env["distri.cash.collection"]._cash_matches(
                valores["name"], telefono)
            parecidos = [p for p in parecidos if not p.get("provisional")] or parecidos
            if parecidos:
                return self._json({"ok": True, "parecidos": parecidos})
        try:
            socio = crear_cliente(user, valores, "Nueva venta")
        except (UserError, ValidationError) as exc:
            return self._json({"ok": False, "error": _error_text(exc)}, status=400)
        cliente, error = self._cliente(str(socio.id))
        if error:
            return self._json({"ok": False, "error": error}, status=403)
        return self._json({"ok": True, "cliente": {"id": cliente.id, "nombre": cliente.name}})

    @http.route("/nueva-venta/cliente/<int:partner_id>", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def cliente_editar(self, partner_id, **kw):
        """19.0.1.129.40: «Datos del cliente»: his WhatsApp, address and
        town. Only for a customer the user may read (the backend's record
        rules: a seller his portfolio, an external seller his customers)
        and reach (_cliente); these three fields are then written with
        sudo, as 129.35 already stores the WhatsApp answer
        (guardar_whatsapp_nueva_venta), with a chatter note. The backend
        ACLs are not widened. The answer is the screen's customer again."""
        user = self._check()
        cliente, error = self._cliente(str(partner_id))
        if error:
            return self._json({"ok": False, "error": error}, status=403)
        try:
            valores = self._cliente_valores(self._form(), nuevo=False)
        except UserError as exc:
            return self._json({"ok": False, "error": _error_text(exc)}, status=400)
        socio = request.env["res.partner"].browse(cliente.commercial_partner_id.id)
        if not socio.has_access("read"):
            return self._json({"ok": False, "error": "No podés cambiar los datos de este "
                                                     "cliente: pedíselo a administración."},
                              status=403)
        if valores:
            try:
                with request.env.cr.savepoint():
                    socio.sudo().write(valores)
            except (UserError, ValidationError) as exc:
                return self._json({"ok": False, "error": _error_text(exc)}, status=400)
            socio.sudo().message_post(body="Datos cambiados por %s en «Nueva venta»: %s." % (
                user.name, ", ".join(sorted(socio._fields[k].string for k in valores))))
        return self._json({"ok": True, "cliente": self._cliente_json(cliente)})

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
        socio = cliente.sudo()
        # 19.0.1.129.40: what is left empty comes from his record
        domicilio = ((form.get("domicilio") or "").strip()
                     or ", ".join(x for x in (socio.street, socio.city) if x))[:200]
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
        if form.get("encomienda") == "1":
            # 19.0.1.129.35: /pedido's «Soy del interior» fields, as its page posts them
            post.update({"encomienda": "1", "enc_nombre": form.get("enc_nombre") or "",
                         "enc_dni": form.get("enc_dni") or "",
                         "enc_localidad": form.get("enc_localidad") or ""})
        validos, error = pedido._renglones_validos(post["renglones"], MAX_CANTIDAD)
        if error:
            return None, error
        clave = pedido._clave_envio(post, validos)
        # 19.0.1.129.40: the customer on file is the contact (main.py _datos_faltantes)
        request.update_context(**{CTX_DATOS_DE_LA_FICHA: True})
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
        # 19.0.1.129.40: the same product twice is one line with the sum
        renglones = nv.juntar_renglones(renglones)
        respuesta, error = self._respuesta_whatsapp(form, cliente, salida)
        if error:
            return self._refuse(error)
        try:
            with request.env.cr.savepoint():
                if respuesta == "numero":
                    # stored first: the order and its confirmation see it
                    nv.guardar_whatsapp_nueva_venta(request.env, cliente, form.get("wa_area"),
                                                    form.get("wa_numero"), None)
                if salida in (nv.ARMAR, nv.PRESUPUESTO):
                    order, error = self._por_pedido(form, cliente, renglones)
                    if error:
                        raise UserError(error)
                    self._extras(order, form)
                    if salida == nv.ARMAR and order.state in ("draft", "sent"):
                        result = order.action_confirm()
                        if isinstance(result, dict):
                            raise UserError("Falta el WhatsApp del cliente para confirmar.")
                    url = "/nueva-venta/hecho/%s" % order.id
                else:
                    url, order = self._crear_admin(form, cliente, renglones, salida)
                if (respuesta == "sin" and order and order.state == "sale"
                        and self._sin_whatsapp(cliente)):
                    order.message_post(body="Confirmado sin WhatsApp del cliente por %s "
                                            "(«Nueva venta»)." % user.name)
        except (UserError, AccessError, ValidationError) as exc:
            return self._refuse(_error_text(exc))
        return self._done(url)

    def _respuesta_whatsapp(self, form, cliente, salida):
        """19.0.1.129.35: the answer to «¿Cuál es el WhatsApp del cliente?»
        (asked only when he has none): ("numero" | "sin" | "", error). The
        exits that confirm need an answer; «Presupuesto» does not. «No tiene
        WhatsApp» is accepted only while the WhatsApp is not required, the
        backend wizard's rule («Confirmar sin WhatsApp»)."""
        if not self._sin_whatsapp(cliente):
            return "", ""
        if form.get("wa_area") or form.get("wa_numero"):
            from ..models.ar_phone import ar_mobile
            try:
                ar_mobile(form.get("wa_area"), form.get("wa_numero"))
            except ValueError as exc:
                return "", "WhatsApp del cliente: %s" % exc
            return "numero", ""
        if salida == nv.PRESUPUESTO:
            return "", ""
        if self._wa_requerido():
            if form.get("sin_whatsapp") == "1":
                return "", ("Para confirmar la venta hace falta el WhatsApp del cliente: "
                            "poné el código de área y el número.")
            return "", "El cliente no tiene WhatsApp: poné el código de área y el número."
        # 19.0.1.129.40 (owner): while it is not required, picking the
        # customer is enough; no answer = «No tiene WhatsApp» of 129.35
        return "sin", ""

    def _extras(self, order, form):
        """#56: who ordered and the shop's GPS (models/nueva_venta.py)."""
        avisos = order._distri_nueva_venta_extras(
            form.get("contacto"), form.get("lat"), form.get("lng"), form.get("precision"))
        for aviso in avisos:
            order.message_post(body="«Nueva venta»: %s" % aviso)

    def _crear_admin(self, form, cliente, renglones, salida):
        """«Mostrador» and «Ya entregado»: a backend quotation and the
        existing step. Returns (the URL to go to, the order made now)."""
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
            return self._url_de(Order.browse(previo.id), salida), Order.browse()
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
        self._extras(order, form)
        if salida == nv.MOSTRADOR:
            order._distri_nueva_venta_mostrador()
        else:
            order._distri_nueva_venta_ya_entregado(form.get("fecha"), form.get("motivo"))
        return self._url_de(order, salida), order

    def _url_de(self, order, salida):
        # 19.0.1.129.42 (ui_v2): a counter sale comes back to its done page
        # (?mostrador=1) with «Cobrar ahora» / «Entregar» where pending
        v2 = ui_v2_on(request.env)
        if salida == nv.MOSTRADOR and order.sudo().distri_delivery_state != "delivered":
            return "/deposito/entrega/%s?mostrador=1%s" % (order.id, "&nv=1" if v2 else "")
        if salida == nv.MOSTRADOR and v2:
            return "/nueva-venta/hecho/%s?mostrador=1" % order.id
        return "/nueva-venta/hecho/%s" % order.id

    # ---------------------------------------------------------------- done

    @http.route("/nueva-venta/hecho/<int:order_id>", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def hecho(self, order_id, mostrador="", **kw):
        self._check()
        order = request.env["sale.order"].browse(order_id).exists()
        if not order:
            raise NotFound()
        try:
            order.check_access("read")
        except AccessError:
            raise NotFound() from None
        # 19.0.1.129.45: the backend's «Personal Orders» rule lets a seller
        # read orders without salesperson; the page reads the rest with sudo,
        # so a seller only reaches his own or his customers' (as /pedidos/<id>)
        if not request.env.user._distri_may_reach_record(order):
            raise NotFound()
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
        pasos = {}
        if mostrador == "1" and ui_v2_on(request.env) and o.state == "sale":
            titulo, texto, pasos = self._pasos_mostrador(o)
        return self._render("distri_pedido.nueva_venta_hecho", {
            "order": o, "titulo": titulo, "texto": texto, "enviar": enviar,
            "total": _money(o.amount_total),
            "factura": o._distri_factura_viva_texto() if o.state == "sale" else "",
            "mis_ventas": "/mis-ventas?" + urlencode({"q": o.name}),
            # 19.0.1.129.42 (ui_v2): the next steps of a counter sale
            "pasos": pasos,
        })

    def _pasos_mostrador(self, o):
        """19.0.1.129.42 (ui_v2): «Mostrador»'s done page. «Entregar» while
        the depot delivery is pending (for the depot role), «Cobrar ahora»
        while its invoice has something to pay (for whoever collects).
        Read only: each button's own screen checks its access again."""
        from ..models.cobrar import can_collect
        from .deposito import is_depot
        user = request.env.user
        entregado = o.distri_delivery_state == "delivered"
        facturas = o.invoice_ids.filtered(
            lambda m: m.move_type == "out_invoice" and m.state == "posted")
        debe = sum(facturas.mapped("amount_residual"))
        pasos = {}
        if not entregado and is_depot(user):
            pasos["entregar"] = "/deposito/entrega/%s?mostrador=1&nv=1" % o.id
        if debe > 0.005 and can_collect(user):
            pasos["cobrar"] = "/cobro?" + urlencode({"cliente": o.partner_id.commercial_partner_id.id})
            pasos["debe"] = _money(debe)
        if entregado:
            texto = "Entregada en el depósito, con su factura."
        else:
            texto = "Confirmada, armada y facturada. Falta registrar la entrega en el depósito."
        if pasos.get("cobrar"):
            texto += " Queda por cobrar %s." % pasos["debe"]
        return "Venta en mostrador", texto, pasos
