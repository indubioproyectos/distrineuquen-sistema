import json
import math
import uuid
from datetime import datetime, time, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from werkzeug.exceptions import Forbidden, NotFound

from odoo import http
from odoo.exceptions import UserError
from odoo.http import request

from ..models.armado import MAX_BULTOS
from ..models.res_users import ARMADO_ORDENES, ARMADO_VISTAS
from .kit import kit_context, staff_nav
from .main import HORA_ARGENTINA, ficha
from .tablero import wants_tablero

GRUPO_ARMADOR = "distri_pedido.grupo_armador"

# Cuantos productos muestra el buscador de "Agregar producto"
TOPE_BUSQUEDA = 30

# Antiguedad maxima de un presupuesto de la pagina sin tomar en la lista del armador
DIAS_PRESUPUESTO = 7

# 19.0.1.98.0: the order list shows these small orders whole (audit 3b: 15 %
# of the orders have 3 lines or fewer)
RENGLONES_A_LA_VISTA = 3
# «¿Cuántas hay?» offers one button per unit up to this quantity, then a
# number field
TOPE_BOTONES_HAY = 12
# body of /sync: a whole order offline is a few KB
MAX_SYNC = 256 * 1024

UTC = ZoneInfo("UTC")

DIAS = ("Lunes", "Martes", "Miércoles", "Jueves", "Viernes", "Sábado", "Domingo")
MESES = ("enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
         "septiembre", "octubre", "noviembre", "diciembre")


class Armado(http.Controller):
    """La pantalla del armador en el celular.

    Todo corre con sudo(): el armador no tiene permisos de ventas en el
    backend. Por eso cada ruta exige el grupo, y la pantalla no muestra
    precios ni nada de la cuenta del cliente.

    19.0.1.94.0: on the Distrineuquén kit (controllers/kit.py). Navigation
    goes up one level: item -> order -> list -> staff home. Every action
    (tomar, listo, falta, agregar, terminar, más tarde, bultos) answers JSON
    when the page sends it with fetch() (X-Distri-Fetch: 1), so the page
    does not reload; without JavaScript the same forms post and redirect.
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

    def _fecha(self):
        hoy = self._hoy()
        return "%s %s de %s" % (DIAS[hoy.weekday()], hoy.day, MESES[hoy.month - 1])

    def _base(self, seccion="armado", **extra):
        empresa = request.env.company
        datos = {
            "empresa_id": empresa.id,
            # con "unique" Odoo manda el logo con cache larga: el celular no
            # lo vuelve a pedir en cada pantalla
            "icon": "/web/image/res.company/%s/logo?unique=%s" % (
                empresa.id, int(empresa.sudo().write_date.timestamp())),
            "ficha": ficha(),
            # the kit (dn.css, dn.js, sprite) plus this screen's own files
            "dn": kit_context("armado.css", "armado.js"),
            "nav": staff_nav(request.env, seccion),
            # the list's parent: the staff home, for users that have one
            "inicio": "/tablero" if wants_tablero(request.env.user) else "",
            "fecha": self._fecha(),
            "usuario": request.env.user,
            "cant": self._cant,
            "hora": self._hora,
            "foto": self._foto,
            "vista": request.env.user.distri_armado_vista or "tradicional",
            "orden": request.env.user.distri_armado_orden or "alfabetico",
            "vistas": ARMADO_VISTAS,
            "ordenes": ARMADO_ORDENES,
            "plata": self._plata,
            "resumen": self._resumen,
        }
        datos.update(extra)
        return datos

    def _plata(self, importe):
        return "$ {:,.0f}".format(importe or 0).replace(",", ".")

    def _resumen(self, pedido):
        """What the order list shows without opening the order (19.0.1.98.0)."""
        renglones = pedido._distri_renglones_a_armar()
        return {
            "n": len(renglones),
            "importe": self._plata(pedido.amount_total),
            "nota": (pedido.distri_nota_deposito or "").strip(),
            "comentario": (pedido.distri_comentario or "").strip(),
            "renglones": (pedido._distri_renglones_ordenados()[0]["renglones"]
                          if len(renglones) <= RENGLONES_A_LA_VISTA else []),
        }

    def _unidad_rara(self, linea):
        """The line's unit when it is not the usual sale unit of the product
        (e.g. BULTO), shown highlighted in «Tradicional»; else ''."""
        usual = self._unidad_de_venta(linea.product_id.product_tmpl_id)
        return linea.product_uom_id.name if linea.product_uom_id != usual else ""

    def _foto(self, producto):
        """Thumbnail URL; «unique» makes Odoo send it with a long cache."""
        return "/web/image/product.product/%s/image_128?unique=%s" % (
            producto.id, int(producto.sudo().write_date.timestamp()))

    def _render(self, template, values):
        """Live data: never from the browser cache (back lands fresh)."""
        respuesta = request.render(template, values)
        respuesta.headers["Cache-Control"] = "no-store"
        return respuesta

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
    def lista(self, dejado="", terminado="", **kw):
        self._exigir_armador()
        pedidos = self._pedidos_del_dia()
        return self._render("distri_pedido.armado_lista", self._base(
            grupos=self._grupos_por_zona(pedidos),
            cuantos=len(pedidos),
            hoy=self._hoy(),
            dejado=self._dejado(dejado),
            terminado=self._terminado(terminado),
        ))

    def _terminado(self, pedido_id):
        """The order just finished, for «Imprimir etiquetas»."""
        try:
            pedido = request.env["sale.order"].sudo().browse(int(pedido_id or 0)).exists()
        except ValueError:
            return None
        if not pedido or pedido.distri_armado != "armado" or not pedido.distri_bultos:
            return None
        return pedido

    def _pdf(self, report, ids, nombre):
        pdf, _tipo = request.env["ir.actions.report"].sudo()._render_qweb_pdf(report, ids)
        return request.make_response(pdf, headers=[
            ("Content-Type", "application/pdf"),
            ("Content-Disposition", 'inline; filename="%s.pdf"' % nombre),
            ("Cache-Control", "no-store"),
        ])

    @http.route("/armado/pedido/<int:pedido_id>/etiquetas", type="http", auth="user",
                methods=["GET"], sitemap=False)
    def etiquetas(self, pedido_id, **kw):
        """PDF of the package labels: 100 x 50 mm, one page per package
        (models/etiquetas.py). Opened in the browser and printed with the
        label printer at «tamaño real»."""
        self._exigir_armador()
        pedido = self._pedido(pedido_id)
        try:
            paquetes = pedido._distri_packages()
        except UserError as exc:
            return self._volver(pedido, str(exc))
        paquetes._mark_printed()
        return self._pdf("distri_pedido.report_etiquetas_bultos", paquetes.ids,
                         "Etiquetas %s" % pedido.name)

    @http.route("/armado/etiquetas/prueba", type="http", auth="user", methods=["GET"],
                sitemap=False)
    def etiqueta_prueba(self, **kw):
        """Test page: a 100 x 50 mm frame with rulers (calibration)."""
        self._exigir_armador()
        return self._pdf("distri_pedido.report_etiqueta_prueba", request.env.company.ids,
                         "Etiqueta de prueba")

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
        tomado = self._tomar_al_abrir(pedido)
        q = (q or "").strip()
        orden = request.env.user.distri_armado_orden or "alfabetico"
        secciones = pedido._distri_renglones_ordenados(orden)
        renglones = request.env["sale.order.line"].concat(*[s["renglones"] for s in secciones])
        puede_tocar = self._puede_tocar(pedido)
        vista = request.env.user.distri_armado_vista or "tradicional"
        template = ("distri_pedido.armado_pedido_trad" if vista == "tradicional"
                    else "distri_pedido.armado_pedido")
        return self._render(template, self._base(
            pedido=pedido,
            tomado=tomado,
            secciones=secciones,
            unidades=sum(renglones.mapped("distri_pedida")),
            unidad_rara=self._unidad_rara,
            tope_hay=TOPE_BOTONES_HAY,
            renglones=renglones,
            actual=self._actual(renglones) if puede_tocar else None,
            hechos=len(renglones.filtered(lambda l: l.distri_listo or l.distri_faltante)),
            con_faltante=len(renglones.filtered("distri_faltante")),
            puede_tocar=puede_tocar,
            puede_corregir=self._puede_corregir(pedido),
            q=q,
            resultados=self._buscar(pedido, q),
            unidad_de=self._unidad_de_venta,
            nueva_clave=self._nueva_clave,
            error=error,
        ))

    def _tomar_al_abrir(self, pedido):
        """19.0.1.98.0 (audit 3b): opening an order nobody has takes it, one
        tap less. Silent when it cannot (someone is saving it, it cannot be
        packed): the page keeps its «Tomar pedido» button."""
        if pedido.distri_armado != "pendiente" or pedido.state == "cancel":
            return False
        try:
            return not pedido.distri_tomar()
        except UserError:
            return False

    def _actual(self, renglones):
        """The line to pack now: the first one not ticked nor short."""
        return renglones.filtered(lambda l: not l.distri_listo and not l.distri_faltante)[:1]

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
            "actual": None,
            "cant": self._cant,
            "foto": self._foto,
            "dn": kit_context(),
            "csrf_token": request.csrf_token(),
        })

    def _ir(self, url):
        """Done: fetch() gets the next URL as JSON, a plain form a redirect."""
        if self._por_fetch():
            return self._json({"ok": True, "url": url})
        return request.redirect(url)

    def _pedido_para_tocar(self, pedido_id, tocar=True):
        """El pedido, o la respuesta de rechazo si el usuario no lo arma.

        tocar=False only checks the group and that the order is visible
        (tomar, terminar, más tarde and bultos check the rest themselves)."""
        if not request.env.user.has_group(GRUPO_ARMADOR):
            if self._por_fetch():
                return None, self._json({"ok": False, "error": "No tenés permiso de armador."}, 403)
            raise Forbidden()
        pedido = request.env["sale.order"].sudo().browse(pedido_id).exists()
        if not pedido or not self._visible(pedido):
            if self._por_fetch():
                return None, self._json({"ok": False, "error": "El pedido no existe."}, 404)
            raise NotFound()
        if tocar and not self._puede_tocar(pedido):
            return None, self._rechazo(pedido, self._por_que_no(pedido))
        return pedido, None

    def _linea(self, pedido, linea_id):
        try:
            linea = request.env["sale.order.line"].sudo().browse(int(linea_id or 0)).exists()
        except ValueError:
            linea = None
        if not linea or linea.order_id != pedido:
            return None
        return linea

    @http.route("/armado/pedido/<int:pedido_id>/tomar", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def tomar(self, pedido_id, **post):
        pedido, rechazo = self._pedido_para_tocar(pedido_id, tocar=False)
        if rechazo:
            return rechazo
        error = pedido.distri_tomar()
        if error:
            return self._rechazo(pedido, error)
        return self._ir("/armado/pedido/%s" % pedido.id)

    @http.route("/armado/pedido/<int:pedido_id>/falta", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def falta(self, pedido_id, linea_id=0, accion="", faltan=0, **post):
        """Falta N, Faltan todas o Esta completo (deshace el faltante)."""
        pedido, rechazo = self._pedido_para_tocar(pedido_id)
        if rechazo:
            return rechazo
        linea = self._linea(pedido, linea_id)
        if not linea:
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

    @http.route("/armado/pedido/<int:pedido_id>/listo", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def listo(self, pedido_id, linea_id=0, accion="listo", **post):
        """«Listo, N» (19.0.1.94.0): the whole line is in the box. accion
        «deshacer» takes the tick back («Volver a revisar»)."""
        pedido, rechazo = self._pedido_para_tocar(pedido_id)
        if rechazo:
            return rechazo
        linea = self._linea(pedido, linea_id)
        if not linea:
            return self._rechazo(pedido, "Ese renglón no es de este pedido.", 422)
        try:
            pedido.distri_marcar_listo(linea, accion != "deshacer")
        except UserError as exc:
            return self._rechazo(pedido, str(exc))
        if self._por_fetch():
            return self._json({"ok": True, "html": self._html_renglon(pedido, linea)})
        # without JavaScript: back on the next line to pack
        siguiente = self._actual(pedido._distri_renglones_a_armar()) or linea
        return self._volver(pedido, ancla="#r%s" % siguiente.id)

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
    def terminar(self, pedido_id, bultos="", bultos_otro="", **post):
        pedido, rechazo = self._pedido_para_tocar(pedido_id, tocar=False)
        if rechazo:
            return rechazo
        if pedido.distri_armado == "armado" and pedido.distri_armador_id == request.env.user:
            # doble toque: el primero ya lo termino
            return self._ir("/armado")
        if not self._puede_tocar(pedido):
            return self._rechazo(pedido, self._por_que_no(pedido))
        # «Tradicional» without JavaScript: the 1-5 buttons or the «+» field
        cuantos = self._bultos(bultos or bultos_otro)
        if not 1 <= cuantos <= MAX_BULTOS:
            # a typing mistake: the page stays as it is (422, no reload)
            return self._rechazo(pedido, "Poné en cuántos bultos quedó el pedido.", 422,
                                 ancla="#terminar")
        try:
            error = pedido.distri_terminar(cuantos)
        except UserError as exc:
            error = str(exc)
        if error:
            return self._rechazo(pedido, error, ancla="#terminar")
        # «Imprimir etiquetas» right after finishing (19.0.1.33.0)
        return self._ir("/armado?" + urlencode({"terminado": pedido.id}))

    @http.route("/armado/pedido/<int:pedido_id>/mas-tarde", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def mas_tarde(self, pedido_id, **post):
        """Dejar para más tarde: vuelve a pendiente, libre para cualquiera."""
        pedido, rechazo = self._pedido_para_tocar(pedido_id, tocar=False)
        if rechazo:
            return rechazo
        try:
            error = pedido.distri_dejar_para_mas_tarde()
        except UserError as exc:
            error = str(exc)
        if error:
            return self._rechazo(pedido, error)
        return self._ir("/armado?" + urlencode({"dejado": pedido.id}))

    @http.route("/armado/pedido/<int:pedido_id>/bultos", type="http", auth="user",
                methods=["POST"], website=True, sitemap=False)
    def bultos(self, pedido_id, bultos="", **post):
        pedido, rechazo = self._pedido_para_tocar(pedido_id, tocar=False)
        if rechazo:
            return rechazo
        if not self._puede_corregir(pedido):
            return self._rechazo(pedido, "Los bultos los corrige el que armó el pedido.")
        error = pedido.distri_corregir_bultos(self._bultos(bultos))
        if not self._por_fetch():
            return self._volver(pedido, error)
        if error:
            return self._json({"ok": False, "error": error}, status=422)
        return self._json({"ok": True, "bultos": pedido.distri_bultos})

    @http.route("/armado/preferencias", type="http", auth="user", methods=["POST"],
                sitemap=False)
    def preferencias(self, vista="", orden="", volver="", **post):
        """The packer's view and order (screen toggle and «Ajustes»)."""
        self._exigir_armador()
        valores = {}
        if vista in dict(ARMADO_VISTAS):
            valores["distri_armado_vista"] = vista
        if orden in dict(ARMADO_ORDENES):
            valores["distri_armado_orden"] = orden
        if valores:
            request.env.user.sudo().write(valores)
        volver = volver if (volver or "").startswith("/") and not volver.startswith("//") else "/armado"
        if self._por_fetch():
            return self._json({"ok": True, "vista": request.env.user.distri_armado_vista,
                               "orden": request.env.user.distri_armado_orden})
        return request.redirect(volver)

    @http.route("/armado/pedido/<int:pedido_id>/sync", type="http", auth="user",
                methods=["POST"], sitemap=False)
    def sync(self, pedido_id, eventos="[]", **post):
        """«Tradicional» without signal (19.0.1.98.0, audit F6): the ticks,
        «¿Cuántas hay?» and the finish saved on the phone, in order. Every
        event has its UUID: a replay answers «duplicate» and changes nothing
        (models/armado_offline.py)."""
        pedido, rechazo = self._pedido_para_tocar(pedido_id, tocar=False)
        if rechazo:
            return rechazo
        if len(eventos or "") > MAX_SYNC:
            return self._json({"ok": False, "error": "Demasiados cambios juntos."}, 413)
        try:
            lista = json.loads(eventos or "[]")
        except ValueError:
            lista = None
        if not isinstance(lista, list):
            return self._json({"ok": False, "error": "No entendimos lo guardado en el celular."}, 422)
        resultados = pedido.distri_sync_eventos(lista, self._puede_tocar)
        terminado = pedido.distri_armado == "armado"
        return self._json({
            "ok": True,
            "resultados": resultados,
            "armado": pedido.distri_armado,
            "url": "/armado?" + urlencode({"terminado": pedido.id}) if terminado else "",
        })

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
        return self._render("distri_pedido.armado_faltantes", self._base(
            seccion="faltantes",
            filas=filas,
            hoy=self._hoy(),
        ))
