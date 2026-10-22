import math
import uuid
from datetime import datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from .main import HORA_ARGENTINA, ficha
from .reparto import SHARED_CSS, asset_version

GRUPO_ARMADOR = "distri_pedido.grupo_armador"

# Cuantos productos muestra el buscador de "Agregar producto"
TOPE_BUSQUEDA = 30

# Antiguedad maxima de un presupuesto de la pagina sin tomar en la lista del armador
DIAS_PRESUPUESTO = 7

UTC = ZoneInfo("UTC")


class Armado(http.Controller):
    """La pantalla del armador en el celular.

    Todo corre con sudo(): el armador no tiene permisos de ventas en el
    backend. Por eso cada ruta exige el grupo, y la pantalla no muestra
    precios ni nada de la cuenta del cliente.
    """

    # ---------------------------------------------------------------- datos

    def _exigir_armador(self):
        if not request.env.user.has_group(GRUPO_ARMADOR):
            raise Forbidden()

    def _pedido(self, pedido_id):
        pedido = request.env["sale.order"].sudo().browse(pedido_id).exists()
        if not pedido or not self._visible(pedido):
            raise NotFound()
        return pedido

    def _visible(self, pedido):
        """Los que se pueden armar, y uno ya tomado que despues se cancelo:
        el armador que lo tiene abierto ve el aviso en vez de un error."""
        return pedido._distri_armable() or (
            pedido.state == "cancel" and pedido.distri_armado != "pendiente"
        )

    def _hoy(self):
        return datetime.now(HORA_ARGENTINA).date()

    def _limites_de_hoy(self):
        """Medianoche a medianoche de Argentina, en UTC como guarda Odoo."""
        inicio = datetime.combine(self._hoy(), time(0, 0), tzinfo=HORA_ARGENTINA)
        desde = inicio.astimezone(UTC).replace(tzinfo=None)
        return desde, desde + timedelta(days=1)

    def _hora(self, momento):
        if not momento:
            return ""
        return momento.replace(tzinfo=UTC).astimezone(HORA_ARGENTINA).strftime("%H:%M")

    def _cant(self, numero):
        return "{:g}".format(numero or 0)

    def _base(self, **extra):
        empresa = request.env.company
        datos = {
            "empresa_id": empresa.id,
            # con "unique" Odoo manda el logo con cache larga: el celular no
            # lo vuelve a pedir en cada pantalla
            "logo": "/web/image/res.company/%s/logo?unique=%s" % (
                empresa.id, int(empresa.sudo().write_date.timestamp())),
            "ficha": ficha(),
            # el mismo estilo que la app del repartidor
            "ui_css": "%s?v=%s" % (SHARED_CSS, asset_version()),
            "usuario": request.env.user,
            "cant": self._cant,
            "hora": self._hora,
        }
        datos.update(extra)
        return datos

    def _pedidos_del_dia(self):
        """Sin armar, en armado, y los que se terminaron hoy.

        Los aprobados y tambien los de la pagina que todavia no tienen
        cliente asignado (decision 7): el filtro fino es _distri_para_armar.
        """
        desde, hasta = self._limites_de_hoy()
        pedidos = request.env["sale.order"].sudo().search([
            ("state", "in", ["draft", "sent", "sale"]),
            "|", "|", ("state", "=", "sale"), ("distri_original", "!=", False),
            ("partner_id.distri_generic_partner", "=", True),
            "|", ("distri_armado", "in", ["pendiente", "en_armado"]),
            "&", ("distri_armado_fin", ">=", desde), ("distri_armado_fin", "<", hasta),
        ], order="date_order, id")
        # un presupuesto de la pagina sin tomar entra solo si es de los ultimos
        # DIAS_PRESUPUESTO dias: uno viejo y olvidado no ensucia la lista
        limite = datetime.now() - timedelta(days=DIAS_PRESUPUESTO)
        return pedidos.filtered(lambda p: p._distri_para_armar() and not (
            p.state in ("draft", "sent") and p.distri_armado == "pendiente"
            and p.create_date < limite
        ))

    def _grupos_por_zona(self, pedidos):
        """Las zonas que reparten hoy primero, en el orden de los viajes.

        Despues las que no reparten hoy, y al final "Sin zona": un cliente
        cuya ciudad no esta en la tabla de zonas.
        """
        dia = self._hoy().weekday()
        por_zona = {}
        for pedido in pedidos:
            por_zona.setdefault(pedido.distri_zona_id, []).append(pedido)

        orden_estado = {"en_armado": 0, "pendiente": 1, "armado": 2}
        grupos = []
        for zona, lista in por_zona.items():
            lista.sort(key=lambda p: orden_estado[p.distri_armado])
            if zona:
                hoy = zona.reparte_el(dia)
                grupos.append({
                    "clave": (0 if hoy else 1, zona.viaje, zona.sequence, zona.name),
                    "nombre": zona.name,
                    "detalle": ("Viaje %s" % zona.viaje if hoy
                                else "Hoy no reparte · %s" % zona.dias_texto),
                    "hoy": hoy,
                    "pedidos": lista,
                })
            else:
                grupos.append({
                    "clave": (2,),
                    "nombre": "Sin zona",
                    "detalle": "La ciudad del cliente no está en la tabla de zonas",
                    "hoy": False,
                    "pedidos": lista,
                })
        grupos.sort(key=lambda g: g["clave"])
        return grupos

    # --------------------------------------------------------------- rutas

    @http.route("/armado", type="http", auth="user", website=True, sitemap=False)
    def lista(self, dejado="", **kw):
        self._exigir_armador()
        pedidos = self._pedidos_del_dia()
        return request.render("distri_pedido.armado_lista", self._base(
            grupos=self._grupos_por_zona(pedidos),
            cuantos=len(pedidos),
            hoy=self._hoy(),
            dejado=self._dejado(dejado),
        ))

    def _dejado(self, pedido_id):
        """El pedido que se acaba de dejar para más tarde, para el aviso."""
        try:
            pedido = request.env["sale.order"].sudo().browse(int(pedido_id or 0)).exists()
        except ValueError:
            return None
        if not pedido or pedido.distri_armado != "pendiente" or not self._visible(pedido):
            return None
        return pedido

    @http.route("/armado/pedido/<int:pedido_id>", type="http", auth="user",
                website=True, sitemap=False)
    def pedido(self, pedido_id, q="", error="", **kw):
        self._exigir_armador()
        pedido = self._pedido(pedido_id)
        q = (q or "").strip()
        return request.render("distri_pedido.armado_pedido", self._base(
            pedido=pedido,
            renglones=pedido._distri_renglones_a_armar(),
            puede_tocar=self._puede_tocar(pedido),
            puede_corregir=self._puede_corregir(pedido),
            q=q,
            resultados=self._buscar(pedido, q),
            unidad_de=self._unidad_de_venta,
            nueva_clave=self._nueva_clave,
            error=error,
        ))

    def _nueva_clave(self):
        """Una por boton "Agregar": ver sale.order.distri_agregar."""
        return uuid.uuid4().hex

    def _buscar(self, pedido, q):
        if not q or pedido.distri_armado != "en_armado":
            return request.env["product.template"]
        return request.env["product.template"].sudo().search([
            ("sale_ok", "=", True), ("name", "ilike", q),
        ], order="name", limit=TOPE_BUSQUEDA)

    def _puede_tocar(self, pedido):
        """El que lo tomo lo arma; administracion puede ayudar."""
        return pedido.distri_armado == "en_armado" and pedido.state != "cancel" and (
            pedido.distri_armador_id == request.env.user
            or request.env.user.has_group("distri_pedido.grupo_administracion")
        )

    def _por_que_no(self, pedido):
        """El motivo cuando _puede_tocar da falso."""
        if pedido.state == "cancel":
            return "Este pedido se canceló."
        if pedido.distri_armado == "pendiente":
            return "Primero tocá Tomar pedido."
        if pedido.distri_armado == "armado":
            return "Este pedido ya está armado."
        return "Este pedido lo está armando %s." % pedido.distri_armador_id.name

    def _puede_corregir(self, pedido):
        """Los bultos de un pedido armado: el que lo armo o administracion."""
        return pedido.distri_armado == "armado" and pedido.state != "cancel" and (
            pedido.distri_armador_id == request.env.user
            or request.env.user.has_group("distri_pedido.grupo_administracion")
        )

    def _bultos(self, texto):
        """Entero de 1 o mas; 0 si no se entiende."""
        try:
            return max(int(str(texto or "").strip()), 0)
        except ValueError:
            return 0

    def _volver(self, pedido, error="", ancla=""):
        url = "/armado/pedido/%s" % pedido.id
        if error:
            url += "?" + urlencode({"error": error})
        return request.redirect(url + ancla)

    # Falta y Agregar responden JSON cuando los pide la pantalla con fetch()
    # (encabezado X-Distri-Fetch): el renglon se actualiza en su lugar, sin
    # recargar la pagina. Sin JavaScript el formulario hace el POST comun y
    # vuelve a la pagina. El token CSRF viaja en el formulario en los dos
    # casos y Odoo lo controla antes de llegar aca.

    def _por_fetch(self):
        return request.httprequest.headers.get("X-Distri-Fetch") == "1"

    def _json(self, datos, status=200):
        return request.make_json_response(datos, status=status, headers=[
            ("Cache-Control", "no-store"),
        ])

    def _rechazo(self, pedido, error, status=409, ancla=""):
        if self._por_fetch():
            return self._json({"ok": False, "error": error}, status=status)
        return self._volver(pedido, error, ancla=ancla)

    def _html_renglon(self, pedido, linea):
        return request.env["ir.qweb"]._render("distri_pedido.armado_renglon", {
            "l": linea,
            "pedido": pedido,
            "puede_tocar": self._puede_tocar(pedido),
            "cant": self._cant,
            "csrf_token": request.csrf_token(),
        })

    def _pedido_para_tocar(self, pedido_id):
        """El pedido, o la respuesta de rechazo si el usuario no lo arma."""
        if not request.env.user.has_group(GRUPO_ARMADOR):
            if self._por_fetch():
                return None, self._json({"ok": False, "error": "No tenés permiso de armador."}, 403)
            raise Forbidden()
        pedido = request.env["sale.order"].sudo().browse(pedido_id).exists()
        if not pedido or not self._visible(pedido):
            if self._por_fetch():
                return None, self._json({"ok": False, "error": "El pedido no existe."}, 404)
            raise NotFound()
        if not self._puede_tocar(pedido):
            return None, self._rechazo(pedido, self._por_que_no(pedido))
        return pedido, None

    @http.route("/armado/pedido/<int:pedido_id>/tomar", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def tomar(self, pedido_id, **post):
        self._exigir_armador()
        pedido = self._pedido(pedido_id)
        error = pedido.distri_tomar()
        return self._volver(pedido, error)

    @http.route("/armado/pedido/<int:pedido_id>/falta", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def falta(self, pedido_id, linea_id=0, accion="", faltan=0, **post):
        """Falta N, Faltan todas o Esta completo (deshace el faltante)."""
        pedido, rechazo = self._pedido_para_tocar(pedido_id)
        if rechazo:
            return rechazo
        try:
            linea = request.env["sale.order.line"].sudo().browse(int(linea_id or 0)).exists()
        except ValueError:
            linea = None
        if not linea or linea.order_id != pedido:
            return self._rechazo(pedido, "Ese renglón no es de este pedido.", 422)
        if accion == "deshacer":
            faltante = 0.0
        elif accion == "todas":
            faltante = linea.distri_pedida
        else:
            try:
                faltante = float(str(faltan).replace(",", "."))
            except ValueError:
                faltante = 0.0
            if not math.isfinite(faltante) or faltante <= 0:
                return self._rechazo(pedido, "Poné cuántas unidades faltan.", 422)
        try:
            pedido.distri_marcar(linea, faltante)
        except UserError as exc:
            return self._rechazo(pedido, str(exc))
        if self._por_fetch():
            return self._json({"ok": True, "html": self._html_renglon(pedido, linea)})
        return self._volver(pedido, ancla="#r%s" % linea.id)

    def _unidad_de_venta(self, producto):
        """La misma unidad que ve el cliente en la pantalla de pedidos."""
        return producto.uom_ids[:1] or producto.uom_id

    @http.route("/armado/pedido/<int:pedido_id>/buscar", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def buscar(self, pedido_id, q="", **kw):
        """Los resultados del buscador de Agregar producto, como fragmento HTML."""
        pedido, rechazo = self._pedido_para_tocar(pedido_id)
        if rechazo:
            return rechazo
        q = (q or "").strip()
        html = request.env["ir.qweb"]._render("distri_pedido.armado_resultados", {
            "pedido": pedido,
            "q": q,
            "resultados": self._buscar(pedido, q),
            "unidad_de": self._unidad_de_venta,
            "csrf_token": request.csrf_token(),
            "nueva_clave": self._nueva_clave,
        })
        return request.make_response(html, headers=[
            ("Content-Type", "text/html; charset=utf-8"),
            ("Cache-Control", "no-store"),
        ])

    @http.route("/armado/pedido/<int:pedido_id>/agregar", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def agregar(self, pedido_id, producto_id=0, cantidad=1, clave="", **post):
        pedido, rechazo = self._pedido_para_tocar(pedido_id)
        if rechazo:
            return rechazo
        try:
            plantilla = request.env["product.template"].sudo().browse(int(producto_id or 0)).exists()
        except ValueError:
            plantilla = None
        if (not plantilla or not plantilla.active or not plantilla.sale_ok
                or not plantilla.product_variant_id):
            return self._rechazo(pedido, "Ese producto no se puede vender.", 422)
        try:
            cantidad = float(str(cantidad).replace(",", "."))
        except ValueError:
            cantidad = 0.0
        if not math.isfinite(cantidad) or cantidad <= 0:
            return self._rechazo(pedido, "Poné una cantidad mayor que cero.", 422)
        try:
            linea = pedido.distri_agregar(plantilla.product_variant_id, cantidad,
                                          self._unidad_de_venta(plantilla),
                                          clave=str(clave or "")[:64])
        except UserError as exc:
            return self._rechazo(pedido, str(exc))
        if self._por_fetch():
            return self._json({"ok": True, "html": self._html_renglon(pedido, linea)})
        return self._volver(pedido, ancla="#r%s" % linea.id)

    @http.route("/armado/pedido/<int:pedido_id>/terminar", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def terminar(self, pedido_id, bultos="", **post):
        self._exigir_armador()
        pedido = self._pedido(pedido_id)
        if pedido.distri_armado == "armado" and pedido.distri_armador_id == request.env.user:
            # doble toque: el primero ya lo termino
            return request.redirect("/armado")
        if not self._puede_tocar(pedido):
            return self._volver(pedido, self._por_que_no(pedido))
        try:
            error = pedido.distri_terminar(self._bultos(bultos))
        except UserError as exc:
            error = str(exc)
        if error:
            return self._volver(pedido, error, ancla="#terminar")
        return request.redirect("/armado")

    @http.route("/armado/pedido/<int:pedido_id>/mas-tarde", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def mas_tarde(self, pedido_id, **post):
        """Dejar para más tarde: vuelve a pendiente, libre para cualquiera."""
        self._exigir_armador()
        pedido = self._pedido(pedido_id)
        try:
            error = pedido.distri_dejar_para_mas_tarde()
        except UserError as exc:
            error = str(exc)
        if error:
            return self._volver(pedido, error)
        return request.redirect("/armado?" + urlencode({"dejado": pedido.id}))

    @http.route("/armado/pedido/<int:pedido_id>/bultos", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def bultos(self, pedido_id, bultos="", **post):
        self._exigir_armador()
        pedido = self._pedido(pedido_id)
        if not self._puede_corregir(pedido):
            return self._volver(pedido, "Los bultos los corrige el que armó el pedido.")
        return self._volver(pedido, pedido.distri_corregir_bultos(self._bultos(bultos)))

    @http.route("/armado/faltantes", type="http", auth="user", website=True, sitemap=False)
    def faltantes(self, **kw):
        """La planilla de faltantes del dia, sola: sirve para compras."""
        self._exigir_armador()
        desde, hasta = self._limites_de_hoy()
        lineas = request.env["sale.order.line"].sudo().search([
            ("distri_faltante", ">", 0),
            ("order_id.state", "!=", "cancel"),
            ("order_id.distri_armado_inicio", ">=", desde),
            ("order_id.distri_armado_inicio", "<", hasta),
        ])
        por_producto = {}
        for linea in lineas:
            fila = por_producto.setdefault((linea.product_id, linea.product_uom_id), {
                "producto": linea.product_id,
                "unidad": linea.product_uom_id.name or "",
                "total": 0.0,
                "pedidos": [],
            })
            fila["total"] += linea.distri_faltante
            fila["pedidos"].append({
                "pedido": linea.order_id,
                "faltan": linea.distri_faltante,
            })
        filas = sorted(por_producto.values(), key=lambda f: f["producto"].name or "")
        return request.render("distri_pedido.armado_faltantes", self._base(
            filas=filas,
            hoy=self._hoy(),
        ))
