import hashlib
import hmac
import json
import logging
import math
import re
import secrets
from datetime import date
from urllib.parse import quote
from zoneinfo import ZoneInfo

from psycopg2 import IntegrityError

from odoo import http
from odoo.exceptions import ConcurrencyError
from odoo.http import request

from .proyecto import project_strip, staff_only

_logger = logging.getLogger(__name__)
# 19.0.1.129.40: context key «Nueva venta» sets before calling enviar() for a
# customer on file (see _datos_faltantes)
CTX_DATOS_DE_LA_FICHA = "distri_nv_datos_de_la_ficha"

# Mercado Pago ids are plain integers; anything else is rejected outright.
MP_ID = re.compile(r"^\d{1,20}$")


def _mp_error_class():
    # imported lazily: models/importar_pedido.py imports this module, so a
    # top-level import of the models package here would be circular
    from ..models.mercado_pago import MercadoPagoError
    return MercadoPagoError


def _ya_se_esta_armando():
    # lazy for the same reason as _mp_error_class
    from ..models.armado import YA_SE_ESTA_ARMANDO
    return YA_SE_ESTA_ARMANDO

def _comprobante_cliente():
    # lazy for the same reason as _mp_error_class (19.0.1.103.0)
    from ..models import comprobante_cliente
    return comprobante_cliente


# 19.0.1.103.0: what /pedido/<token> says after «Subir comprobante»
# (?comprobante=<code>); (kind, text). «estado» shows the order's reason.
CUSTOMER_UPLOAD_NOTICES = {
    "ok": ("ok", "Recibimos tu comprobante · lo revisa administración."),
    "repetido": ("ok", "Este comprobante ya lo teníamos: no hace falta subirlo de nuevo."),
    "falta": ("error", "Elegí la foto o el PDF del comprobante."),
    "grande": ("error", "El archivo pesa más de 10 MB. Mandá una captura o una foto más liviana."),
    "tipo": ("error", "Subí una foto (JPG, PNG o WEBP) o un PDF del comprobante."),
    "muchos": ("error", "Ya subiste %d comprobantes hoy para este pedido. Si tenés que mandar "
                        "otro, escribinos por WhatsApp."),
    "limite": ("error", "Probaste muchas veces seguidas. Esperá unos minutos y probá de nuevo."),
    "importe": ("error", "Revisá el importe: escribilo como 15.000 o 15.000,50, o dejalo vacío."),
}

# Alias al que transfiere el cliente. Parametro del sistema, lo cambia el
# administrador en Ajustes > Tecnico > Parametros del sistema.
PARAM_ALIAS = "distri_pedido.alias_transferencia"

# Switch that pauses the order screen (/pedido and /pedido/enviar) while its
# catalog is out of date: "1" pauses it for everyone except system
# administrators. Settings > Technical > System Parameters, no deploy needed.
PARAM_PAUSADO = "distri_pedido.pedido_pausado"

# Web orders without a customer go under a generic contact, one per list:
# orders with MAYORISTA-section lines use the wholesale one, so the administrator
# tells them apart at a glance. The generics are found by flag
# (res.partner._distri_generic_partner_for), never by name.

# Numero que recibe los pedidos: parametro distri_pedido.whatsapp_empresa
# (models/pedido_whatsapp.py), que se cambia en «WhatsApp de pedidos». El de
# prueba y el oficial se cargan con /srv/odoo-lab/scripts/cargar-datos-empresa.sh:
# ningun numero real vive en el codigo.


def _wa():
    # lazy import, same reason as _mp_error_class
    from ..models import pedido_whatsapp
    return pedido_whatsapp


def _ar_phone():
    # lazy import, same reason as _mp_error_class (pure helpers, no models)
    from ..models import ar_phone
    return ar_phone

# Compra minima, igual que en AgilPedido. Since 19.0.1.91.0 it applies to
# EVERY order through /pedido (delivery and pickup, customers and staff) and
# /pedido/enviar enforces it; smaller purchases are made at the store, and the
# administrator loads them as a quotation from the backend (never blocked).
COMPRA_MINIMA = 70000.0

# /pedido/enviar (19.0.1.29.0): what a real order can be. 19.0.1.102.0: an
# order of ~120 products was refused, so the line cap is above the whole
# catalog (about 1,221 active products in 2026-11) and only stops garbage.
# A line is about 30 characters of JSON ({"id":"12345","cantidad":10000})
# and about twice that url-encoded: 2,000 lines fit the text and body caps
# with a wide margin (Odoo itself takes forms up to 10 MB).
MAX_RENGLONES = 2000
MAX_CANTIDAD = 10000
MAX_RENGLONES_TEXTO = MAX_RENGLONES * 125
MAX_CUERPO = 1024 * 1024
# 19.0.1.96.0 (audit F1): tighter caps for the public page (visitors and
# logged-in customers; staff keep MAX_CANTIDAD and no total cap). Real data
# (distri_lab, 2026-11): the biggest invoiced line is 200 units and the
# biggest invoice $ 24.4 M (median $ 176,000). "0" in a parameter turns
# that cap off.
PARAM_MAX_UNIDADES = "distri_pedido.pedido_max_unidades"
MAX_UNIDADES_PUBLICO = 1000
PARAM_MAX_TOTAL = "distri_pedido.pedido_max_total"
MAX_TOTAL_PUBLICO = 30000000.0
# the values of sale.order distri_entrega the form may send
ENTREGAS = ("domicilio", "retira")


def unidades_enteras(cantidad):
    """A quantity of /pedido in whole units (19.0.1.91.0): the screen only
    sells whole units, so 0.001 is 0 (dropped) and 2.5 is 3."""
    return int(math.floor(cantidad + 0.5))

# Ficha del comercio en la cabecera. Es la misma informacion, en el mismo
# orden, que hoy ve el cliente en AgilPedido: localidad, horario de reparto,
# leyenda de envios y telefono. Se edita aca.
FICHA = {
    "nombre": "Distri Neuquén",
    "localidad": "Neuquén",
    # `horario` replica textualmente lo que el cliente lee hoy en AgilPedido,
    # y por eso se mantiene en la pantalla de pedidos. En la portada, que es
    # sitio propio, decir "solo WhatsApp" contradice lo que se esta mostrando.
    "horario": "Sólo WhatsApp -Reparto lunes a viernes de 9 a 19hs",
    "reparto": "Reparto de lunes a viernes, 9 a 19 h",
    "leyenda": "Envíos al interior",
    # parametro distri_pedido.telefono_empresa (ficha())
    "telefono": "",
}
PARAM_TELEFONO = "distri_pedido.telefono_empresa"


def ficha():
    """FICHA with the company's public phone from its parameter (loaded by
    /srv/odoo-lab/scripts/cargar-datos-empresa.sh; never in the code)."""
    telefono = request.env["ir.config_parameter"].sudo().get_param(PARAM_TELEFONO) or ""
    return dict(FICHA, telefono=telefono.strip())

# La seccion de la lista oculta va al final de la lista de categorias, como un
# desplegable mas, en la misma ubicacion que el cliente ya conoce de
# AgilPedido (alli se llama "Vent espec Distribuidoras + $ 10.000.000").
# Decision de gerencia del 29/09/2026: es solo para distribuidoras y
# revendedores autorizados; el publico nunca la ve (ni en el HTML).
DISTRIBUIDORAS = {
    "nombre": "DISTRIBUIDORAS",
    "detalle": "Lista para distribuidoras y revendedores",
    "minimo": "Compra m\u00ednima $ 10.000.000",
}

MESES = ["ene.", "feb.", "mar.", "abr.", "may.", "jun.",
         "jul.", "ago.", "sep.", "oct.", "nov.", "dic."]

# Odoo guarda las fechas en UTC; al cliente se le muestran en hora argentina
HORA_ARGENTINA = ZoneInfo("America/Argentina/Buenos_Aires")
# movimientos por pagina en la cuenta corriente del cliente
POR_PAGINA = 50

# Price lists (management decision of 29/09/2026). The word «minorista» is no
# longer used: kiosks and almacenes are «mayoristas» and buy with the General
# list, the only public one (it was called «Minorista»). The hidden list
# Distribuidoras (it was called «Mayorista»: the «May» prices, mostly
# FARMACIA) and the «Revendedor NN» lists built on it are only seen by
# Gerencia and Administración (rule of 30/09/2026: sellers see the General
# list only, models/lista_oculta.py) and by customers flagged
# res.partner.distri_lista_oculta. Looked up by name
# here and in models/importar_pedido.py and models/agilpedido_wa.py.
LISTA_GENERAL = "General"
MENSAJE_DISTRIBUIDORA = "Pedido de distribuidora: lo gestiona Administración."
# 19.0.1.129.20: an external seller only takes orders for his customers
MENSAJE_CLIENTE_AJENO = ("Ese cliente no es tuyo: solo podés tomar pedidos de tus clientes. Si "
                         "es nuevo, cargá el pedido sin elegir cliente.")
LISTA_OCULTA = "Distribuidoras"

# Cigarrillos y tabaco no tienen precio publico: cada revendedor tiene el suyo
# negociado. En la lista General el renglon entra sin precio y el vendedor lo
# valoriza al asignarle el cliente.
CATEGORIAS_A_PRESUPUESTAR = ("CIGARRILLOS", "Tabaco VAN KIFF")


def lista_general(env):
    """The public price list. Never falls back to a hidden one: without the
    General list the prices are the products' list prices."""
    Pl = env["product.pricelist"].sudo()
    return (Pl.search([("name", "=", LISTA_GENERAL)], limit=1)
            or Pl.search([("distri_mayorista", "=", False),
                          ("name", "!=", LISTA_OCULTA)], limit=1))


def lista_oculta(env):
    """The hidden wholesale list («May» prices) of distributors and resellers."""
    return env["product.pricelist"].sudo().search([("name", "=", LISTA_OCULTA)], limit=1)


class TomaDePedidos(http.Controller):
    """Carga de pedidos en una sola pantalla, sin registracion."""

    # ---------------------------------------------------------------- datos

    def _lista_general(self):
        return lista_general(request.env)

    def _lista_oculta(self):
        return lista_oculta(request.env)

    # ------------------------------------------------- acceso del comercio

    def _debe_cambiar_clave(self):
        """La clave se la mandamos nosotros: la primera vez tiene que cambiarla."""
        usuario = request.env.user
        return not usuario._is_public() and usuario.distri_debe_cambiar_clave

    def _personal(self):
        """Vendedor, administracion o gerencia con la sesion abierta."""
        return _wa().is_staff(request.env.user)

    def _ve_secciones_solo_agil(self):
        """19.0.1.129.10: whoever may see the hidden list also sees on /pedido
        the AgilPedido sections kept for AgilPedido only
        (agil_tienda.SECCIONES_SOLO_AGIL)."""
        # the same visitors that see the hidden list (_calcular_autorizado):
        # Gerencia, Administración, Sistemas and the customers flagged
        # «Distribuidora / revendedor (lista oculta)» that log in
        return self._autorizado_lista_oculta()

    def _solo_vendedor(self):
        """Seller without Administración: sees the General list only and does
        not handle distributors (models/lista_oculta.py)."""
        from ..models.lista_oculta import solo_vendedor
        return solo_vendedor(request.env.user)

    def _bloqueado_para_vendedor(self, pedido):
        """A distributor's order opened or edited by a seller: not shown."""
        return bool(pedido) and self._solo_vendedor() and pedido._distri_de_distribuidora()

    def _whatsapp_empresa(self):
        return request.env["distri.pedido.whatsapp.settings"].sudo().company_number()

    def _socio_del_usuario(self):
        """El comercio del usuario logueado, si entro con su acceso."""
        if request.env.user._is_public():
            return False
        return request.env.user.partner_id

    def _acceso(self):
        """(authorized, own list) of the visitor, computed once per request:
        the catalog asks for every row."""
        acceso = getattr(request, "_distri_acceso", None)
        if acceso is None:
            autorizado = self._calcular_autorizado()
            acceso = (autorizado, self._calcular_lista_propia(autorizado))
            request._distri_acceso = acceso
        return acceso

    def _calcular_autorizado(self):
        """Whether the visitor may see the hidden list (commercial policy).

        Among staff only Gerencia, Administración and the system
        administrator see it (rule of 30/09/2026): a seller works with small
        shops and sees the General list, like any shop. A customer sees it
        only when logged in and its company is flagged «Distribuidora /
        revendedor (lista oculta)» by Administración. Everyone else sees the
        General list only.
        """
        usuario = request.env.user
        if usuario._is_public():
            return False
        if self._personal():
            return not self._solo_vendedor()
        socio = usuario.partner_id.sudo().commercial_partner_id
        return bool(socio.distri_lista_oculta)

    def _calcular_lista_propia(self, autorizado):
        """La lista asignada en la ficha de un cliente autorizado que entro.

        Solo si no es la General: con la General asignada, el autorizado ve
        la General y ademas la seccion de la lista oculta, como el personal.
        Un cliente sin autorizacion nunca usa su lista asignada aca: si tiene
        una oculta, mostrarla filtraria esos precios. El personal no compra:
        ve la General y el pedido toma la lista del cliente que elige."""
        vacia = request.env["product.pricelist"]
        if not autorizado or self._personal():
            return vacia
        socio = request.env.user.partner_id.sudo().commercial_partner_id
        lista = socio.property_product_pricelist
        if not lista or lista == self._lista_general():
            return vacia
        return lista

    def _autorizado_lista_oculta(self):
        return self._acceso()[0]

    def _lista_propia(self):
        return self._acceso()[1]

    def _lista_del_visitante(self):
        """La lista de precios que corresponde a quien esta mirando.

        Un revendedor autorizado que entro con su usuario ve SUS precios;
        todos los demas, la General. Nunca sale de lo que manda el visitante.
        """
        return self._lista_propia() or self._lista_general()

    def _ve_lista_oculta(self):
        """The DISTRIBUIDORAS section (hidden «May» prices and cigarettes to
        be priced) is rendered and accepted only for authorized visitors that
        are not already seeing their own list."""
        return self._autorizado_lista_oculta() and not self._lista_propia()

    def _catalogo_agil(self):
        """True while /pedido shows the TEMPORARY copy of the AgilPedido
        storefront (models/catalogo_agil.py) instead of the usual catalog."""
        from ..models.agil_tienda import catalogo_agil_activo
        return catalogo_agil_activo(request.env)

    def _dominio_catalogo(self):
        """Only the temporary AgilPedido products with the switch on; without
        them otherwise, so a synced copy never shows in the usual catalog."""
        return [("distri_agil_temporal", "=", self._catalogo_agil())]

    def _sin_precio(self, producto):
        """Los cigarrillos no tienen precio publico.

        Si el visitante tiene lista propia el precio SI se muestra: ahi el
        precio del revendedor existe y esta cargado. A temporary AgilPedido
        product always shows its price, like the storefront it copies.
        """
        if self._lista_propia() or producto.distri_agil_temporal:
            return False
        return producto.categ_id.name in CATEGORIAS_A_PRESUPUESTAR

    def _unidad_de_venta(self, producto):
        """La unidad en la que el cliente compra este producto.

        Si el producto declara unidades de venta propias, manda la primera:
        una cerveza que solo se vende por bulto no debe poder pedirse por lata.
        """
        if producto.uom_ids:
            return producto.uom_ids[0]
        return producto.uom_id

    def _catalogo(self):
        """Productos publicados, agrupados por categoria y ordenados.

        Cada renglon lleva su unidad de venta y el precio DE ESA UNIDAD, no el
        de la unidad de stock: el comerciante ve el precio del bulto.
        """
        agil = self._catalogo_agil()
        if agil:
            # the AgilPedido sections, in the storefront order
            from ..models.agil_tienda import ORDEN_AGIL, SECCIONES_SOLO_AGIL, SIN_SECCION_ORDEN
            orden = ORDEN_AGIL
            # 19.0.1.129.10: sections that only AgilPedido shows; Administración
            # and Gerencia still see them (owner, 23/11/2026)
            solo_agil = [] if self._ve_secciones_solo_agil() else [
                ("distri_agil_seccion", "not =ilike", "%s%%" % nombre)
                for nombre in SECCIONES_SOLO_AGIL]
        else:
            orden = "categ_id, name"
            solo_agil = []
        productos = request.env["product.template"].sudo().search(
            [("sale_ok", "=", True), ("is_published", "=", True)] + self._dominio_catalogo()
            + solo_agil,
            order=orden,
        )
        lista = self._lista_del_visitante()
        precios = self._precios_en_lote(productos, lista)
        secciones = {}
        for producto in productos:
            if agil:
                # 19.0.1.129.9: no section in AgilPedido: first, with no
                # heading (the template shows the name "" without one)
                seccion = ("" if producto.distri_agil_seccion_orden <= SIN_SECCION_ORDEN
                           else producto.distri_agil_seccion or "Otros")
            else:
                seccion = producto.categ_id.name or "Sin seccion"
            secciones.setdefault(seccion, []).append(
                self._renglon(producto, lista, precios.get(producto.id))
            )
        if agil:
            return list(secciones.items())
        return sorted(secciones.items())

    def _precios_en_lote(self, productos, lista):
        """El precio de cada producto, pedido en lote y no de a uno.

        Pedirlos uno por uno cuesta ~1 segundo para 556 productos, porque cada
        llamada resuelve la lista entera. Agrupados por unidad de venta son
        menos de diez llamadas y baja a centesimas.
        """
        por_factor = {}
        for producto in productos:
            factor = self._unidad_de_venta(producto).relative_factor or 1.0
            por_factor.setdefault(factor, []).append(producto)

        precios = {}
        for factor, grupo in por_factor.items():
            variantes = self.env["product.product"].browse([
                p.product_variant_id.id for p in grupo if p.product_variant_id
            ])
            if not variantes:
                continue
            mapa = lista._get_products_price(variantes, factor)
            for producto in grupo:
                variante = producto.product_variant_id
                if variante:
                    precios[producto.id] = mapa.get(variante.id, 0.0) * factor
        return precios

    def _renglon(self, producto, lista, precio=None, sin_precio=None):
        """Un producto tal como lo ve la pantalla, con su unidad de venta."""
        unidad = self._unidad_de_venta(producto)
        factor = unidad.relative_factor or 1.0
        if sin_precio is None:
            sin_precio = self._sin_precio(producto)
        if sin_precio:
            final = 0.0
        elif precio is not None:
            final = precio
        else:
            # el precio unitario puede bajar por cantidad, asi que se pide el
            # precio para la cantidad que representa una unidad de venta
            final = lista._get_product_price(
                producto.product_variant_id, factor
            ) * factor
        return {
            "id": producto.id,
            "nombre": producto.name,
            "precio": final,
            "precio_texto": self._plata(final),
            "sin_precio": sin_precio,
            "unidad": unidad.name,
            "por_bulto": factor > 1,
            "contiene": int(factor) if factor > 1 else 0,
            "vence": self._vence(producto.destacado_vence),
        }

    def _plata(self, monto):
        """Miles con punto, como se lee aca y como ya lo escribe la barra."""
        return "{:,.0f}".format(monto or 0).replace(",", ".")

    def _importe_para_copiar(self, monto):
        """El importe para pegar en la app del banco: sin $ ni puntos de miles.

        Sin centavos va entero ("12345"). Con centavos lleva coma decimal
        ("12345,50"), como se escribe en Argentina: con punto, una app en
        español puede leer "12345.50" como 1.234.550.
        """
        centavos = int(round((monto or 0) * 100))
        if centavos % 100 == 0:
            return str(centavos // 100)
        return "%d,%02d" % (centavos // 100, centavos % 100)

    def _plata_exacta(self, monto):
        """Como _plata, pero con centavos cuando los hay: es lo que se paga."""
        centavos = int(round((monto or 0) * 100))
        entero = self._plata(centavos // 100)
        if centavos % 100 == 0:
            return entero
        return "%s,%02d" % (entero, centavos % 100)

    def _alias_transferencia(self):
        return (request.env["ir.config_parameter"].sudo().get_param(PARAM_ALIAS) or "").strip()

    def _como_pagar(self, pedido):
        """El recuadro "Cómo pagar" de la página del pedido.

        Aparece recién con la factura emitida: antes el importe todavía puede
        cambiar por los faltantes del armado. El importe es el saldo de las
        facturas, así que un pago parcial ya registrado se descuenta.
        """
        if pedido.state == "cancel":
            return {"facturado": False, "pagado": False}
        facturas = pedido._distri_facturas_emitidas()
        if not facturas:
            return {"facturado": False, "pagado": False}
        saldo = pedido._distri_importe_a_pagar()
        moneda = facturas[:1].currency_id
        if moneda.compare_amounts(saldo, 0) <= 0:
            return {"facturado": True, "pagado": True}
        return {
            "facturado": True,
            "pagado": False,
            "importe": self._plata_exacta(saldo),
            "importe_copiar": self._importe_para_copiar(saldo),
            "alias": self._alias_transferencia(),
            # el cliente ve el remito, nunca el numero de factura
            "remitos": pedido._distri_remitos_texto(),
        }

    def _vence(self, fecha):
        """"24 nov. 2026", como lo escribe la pantalla que el cliente conoce."""
        if not fecha:
            return ""
        return "%s %s %s" % (fecha.day, MESES[fecha.month - 1], fecha.year)

    def _destacados(self):
        """Ofertas relampago: sueltas, arriba de todo y sin categoria."""
        productos = request.env["product.template"].sudo().search(
            [("destacado", "=", True), ("sale_ok", "=", True),
             ("is_published", "=", True)] + self._dominio_catalogo(),
            order="destacado_vence, name",
        )
        lista = self._lista_del_visitante()
        return [self._renglon(producto, lista) for producto in productos]

    def _productos_lista_oculta(self):
        """Los que tienen un precio «May» propio cargado en la lista oculta.

        Only for authorized visitors: for anyone else this returns nothing,
        so the hidden prices never reach the page. No se filtra por categoria
        a proposito: el criterio es tener regla en la lista Distribuidoras
        (hoy casi todos de FARMACIA); si manana le cargan precio a otro,
        aparece solo.
        """
        lista = self._lista_oculta() if self._ve_lista_oculta() else False
        if not lista or self._catalogo_agil():
            # the AgilPedido copy has its «May» rows as products of their own
            return []
        plantillas = lista.item_ids.mapped("product_tmpl_id").filtered(
            lambda p: p.active and p.sale_ok and p.is_published and not p.distri_agil_temporal
        )
        precios = self._precios_en_lote(plantillas, lista)
        renglones = []
        for producto in plantillas.sorted("name"):
            # ningun renglon sale "a confirmar": aca el precio siempre
            # existe, que para eso esta la regla
            renglon = self._renglon(producto, lista, precios.get(producto.id),
                                    sin_precio=False)
            renglon["mayorista"] = True
            renglones.append(renglon)
        return renglones

    def _cigarrillos_a_presupuestar(self):
        """Los cigarrillos entran por la seccion DISTRIBUIDORAS sin precio.

        Only for authorized visitors. Cada revendedor tiene su precio
        negociado: el renglon entra en cero y el vendedor lo valoriza cuando
        le asigna la lista del cliente.
        """
        lista = self._lista_oculta() if self._ve_lista_oculta() else False
        if not lista or self._catalogo_agil():
            return []
        productos = request.env["product.template"].sudo().search(
            [("categ_id.name", "in", list(CATEGORIAS_A_PRESUPUESTAR)),
             ("sale_ok", "=", True), ("is_published", "=", True),
             ("distri_agil_temporal", "=", False)],
            order="name",
        )
        renglones = []
        for producto in productos:
            # van sin precio a proposito: no hace falta calcularlo
            renglon = self._renglon(producto, lista, 0.0, sin_precio=True)
            renglon["mayorista"] = True
            renglones.append(renglon)
        return renglones

    def _seguimiento(self, pedido):
        """Lo que el cliente pidio contra lo que quedo en el pedido.

        El deposito saca faltantes y cambia marcas al preparar. Sin esto el
        cliente se entera cuando llega el reparto.
        """
        try:
            original = json.loads(pedido.distri_original or "[]")
        except ValueError:
            original = []
        pedido_por_producto = {
            int(r["id"]): float(r["cantidad"]) for r in original if r.get("id")
        }

        filas = []
        vistos = set()
        for linea in pedido.order_line:
            producto = linea.product_id
            vistos.add(producto.id)
            pedida = pedido_por_producto.get(producto.id)
            quedo = float(linea.product_uom_qty)
            if pedida is None and not quedo:
                # lo saco el cliente de un pedido ya aprobado: Odoo no deja
                # borrar el renglon y queda en cero
                continue
            if linea.distri_por_telefono:
                estado = "telefono"
            elif pedida is None:
                estado = "agregado"
            elif abs(pedida - quedo) < 0.001:
                estado = "igual"
            elif not quedo:
                # el armador marco "faltan todas"
                estado = "sacado"
            else:
                estado = "cambio"
            filas.append({
                "nombre": producto.name,
                "unidad": linea.product_uom_id.name or "",
                "pedida": pedida,
                "quedo": quedo,
                "precio": self._plata(linea.price_unit),
                "subtotal": self._plata(linea.price_subtotal),
                "sin_precio": not linea.price_unit,
                "estado": estado,
            })

        # los que el cliente pidio y ya no estan: faltantes o reemplazos
        faltantes = [p for p in pedido_por_producto if p not in vistos]
        if faltantes:
            Producto = request.env["product.product"].sudo()
            for producto in Producto.browse(faltantes).exists():
                filas.append({
                    "nombre": producto.name,
                    "unidad": "",
                    "pedida": pedido_por_producto[producto.id],
                    "quedo": 0.0,
                    "precio": "",
                    "subtotal": "",
                    "sin_precio": False,
                    "estado": "sacado",
                })

        hubo_cambios = any(f["estado"] != "igual" for f in filas)
        return {
            "filas": filas,
            "hubo_cambios": hubo_cambios,
            "sabemos_el_original": bool(original),
        }

    def _cliente_asignado(self, pedido):
        """El comercio real, si el gerente ya lo asigno.

        Mientras el pedido esta a nombre de un socio generico no hay cuenta
        corriente que mostrar: todavia no se sabe de quien es el pedido.
        """
        socio = pedido.partner_id
        if not socio or socio.sudo().distri_generic_partner:
            return False
        return socio

    def _cuenta_visible(self, socio):
        """Whether the account of `socio` is shown on this page (19.0.1.92.0,
        models/cuenta_visible.py). The customer sees it only when
        administración turned it on; staff (internal users) always do."""
        if not socio:
            return False
        usuario = request.env.user
        if not usuario._is_public() and not usuario.share:
            return True
        return socio._distri_account_visible()

    def _cuenta_de(self, socio, pagina=1, desde=False, hasta=False):
        """La cuenta corriente completa del comercio, paginada.

        Decision de ale (22/09/2026): registro unico, el cliente ve todo el
        historico y no hay fecha de corte. Las filas salen del estado de
        cuenta (models/estado_cuenta.py): la historia de Odoo Online, lo que
        registra este Odoo y las devoluciones informadas por el repartidor,
        con el saldo de cada momento. Un documento que quedo en las dos
        fuentes cuenta una sola vez. El cliente ve el remito, nunca el
        numero de factura (customer_type y customer_ref).

        Lo mas nuevo primero. `pagina` es un numero o "todo".

        19.0.1.112.0: `desde` / `hasta` limit the movements to a range (the
        public statement link); the balance on top is still today's and,
        with `desde`, the last row is the balance before it.
        """
        datos = socio.sudo()._distri_statement(desde, hasta)
        filas = list(reversed(datos["rows"]))
        saldo = datos["balance"]
        if desde:
            filas.append({
                "date": desde, "customer_ref": "Todo lo anterior a esta fecha",
                "customer_type": "Saldo anterior", "provisional": False,
                "debit": 0.0, "credit": 0.0, "balance": datos["opening"],
            })
        cuantos = len(filas)
        paginas = max(1, -(-cuantos // POR_PAGINA))
        if pagina == "todo":
            desde, hasta, actual = 0, cuantos, 0
        else:
            try:
                actual = min(max(int(pagina or 1), 1), paginas)
            except (TypeError, ValueError):
                actual = 1
            desde = (actual - 1) * POR_PAGINA
            hasta = desde + POR_PAGINA
        return {
            "socio": socio,
            "saldo": self._plata(abs(saldo)),
            "a_favor": saldo < 0 and abs(saldo) >= 0.01,
            "al_dia": abs(saldo) < 0.01,
            "a_controlar": self._plata(datos["provisional"]) if datos["provisional"] else "",
            "movimientos": [
                {
                    "fecha": f["date"].strftime("%d/%m/%Y") if f["date"] else "",
                    "comprobante": f["customer_ref"],
                    "tipo": f["customer_type"],
                    "provisorio": f["provisional"],
                    # 19.0.1.126.0: a credited receipt tied to the Mercado Pago report
                    "confirmado": bool(f.get("confirmed")),
                    "debe": self._plata(f["debit"]) if f["debit"] else "",
                    "haber": self._plata(f["credit"]) if f["credit"] else "",
                    "saldo": ("- " if f["balance"] < -0.004 else "") + self._plata(abs(f["balance"])),
                }
                for f in filas[desde:hasta]
            ],
            "cuantos": cuantos,
            "pagina": actual,
            "paginas": paginas,
            "desde": desde + 1 if cuantos else 0,
            "hasta": min(hasta, cuantos),
        }

    def _cantidades_por_seccion(self, pedido):
        """Las cantidades del pedido, separadas por la seccion de donde salieron.

        Hay productos que estan en dos lugares: en su categoria al precio de
        la lista General y en DISTRIBUIDORAS al precio «May». Si se precargan
        en los dos, al editar se le duplican al cliente sin que toque nada.
        A visitor who cannot see the hidden section gets those quantities in
        the regular catalog (priced with the General list when saved).
        """
        try:
            foto = json.loads(pedido.distri_original or "[]")
        except ValueError:
            foto = []

        if foto:
            ve_oculta = self._ve_lista_oculta()
            comunes, mayoristas = {}, {}
            Variante = request.env["product.product"].sudo()
            for renglon in foto:
                variante = Variante.browse(int(renglon["id"])).exists()
                if not variante:
                    continue
                destino = mayoristas if renglon.get("may") and ve_oculta else comunes
                plantilla = variante.product_tmpl_id.id
                destino[plantilla] = destino.get(plantilla, 0) + int(renglon["cantidad"])
            return comunes, mayoristas

        # pedidos viejos, de antes de la foto: todo a su categoria
        return {
            linea.product_id.product_tmpl_id.id: int(linea.product_uom_qty)
            for linea in pedido.order_line
        }, {}

    def _marca_pedido(self):
        """19.0.1.129.12: brand icons and the link preview of /pedido."""
        # here: models imports this module (models/importar_pedido.py)
        from ..models.marca import marca_publica
        base = (request.env["ir.config_parameter"].sudo().get_param("web.base.url")
                or request.httprequest.host_url).rstrip("/")
        return dict(marca_publica(base), pagina=base + "/pedido")

    def _contexto_pantalla(self, pedido=False, cantidades=None, token="",
                           cantidades_may=None):
        socio = self._socio_del_usuario()
        # a logged-in customer (not staff) sees which list is priced
        cliente = socio if socio and not self._personal() else False
        return {
            # al editar, el cliente confirma lo que ya cargo en vez de
            # volver a escribirlo
            "datos": {
                "nombre": pedido.distri_nombre or "" if pedido else "",
                "telefono": pedido.distri_telefono or "" if pedido else "",
                "domicilio": pedido.distri_domicilio or "" if pedido else "",
                "entrega": pedido.distri_entrega or "domicilio" if pedido else "domicilio",
                "encomienda": bool(pedido and pedido.distri_encomienda_pedida),
                "enc_nombre": pedido.distri_encomienda_nombre or "" if pedido else "",
                "enc_dni": pedido.distri_encomienda_dni or "" if pedido else "",
                "enc_localidad": pedido.distri_encomienda_localidad or "" if pedido else "",
                # 19.0.1.98.0: comment, depot note and «Mi vendedor es» (the
                # order's, else the logged-in customer's default)
                "comentario": pedido.distri_comentario or "" if pedido else "",
                "nota_deposito": pedido.distri_nota_deposito or "" if pedido else "",
                "vendedor_web_id": (pedido.distri_vendedor_web_id.id if pedido else 0) or (
                    socio.commercial_partner_id.sudo().distri_vendedor_web_id.id
                    if socio and not self._personal() else 0) or 0,
            },
            "vendedores": request.env["distri.vendedor.web"]._distri_para_pagina(),
            "localidades_encomienda": self._localidades_encomienda(),
            "secciones": self._catalogo(),
            "compra_minima": COMPRA_MINIMA,
            "pedido": pedido,
            "cantidades": cantidades or {},
            "cantidades_may": cantidades_may or {},
            "token": token,
            "empresa_id": request.env.company.id,
            "marca": self._marca_pedido(),
            "lista_propia": self._lista_del_visitante().name or "" if cliente else "",
            "socio_logueado": socio,
            # «Ver mi cuenta corriente» (19.0.1.92.0: only when it is shown to him)
            "mostrar_cuenta": bool(cliente) and self._cuenta_visible(cliente),
            "quien": cliente.name if cliente else "",
            # el personal elige el cliente del pedido (solo en pedidos nuevos)
            "personal": self._personal(),
            "vendedor": request.env.user.name if self._personal() else "",
            # every visitor: /pedido/enviar checks it (19.0.1.29.0)
            "csrf": request.csrf_token(),
            # 19.0.1.93.0 (E2E-C C8): the id of this form, see _clave_envio
            "envio": secrets.token_hex(12),
            "ficha": ficha(),
            "distribuidoras": DISTRIBUIDORAS,
            "destacados": self._destacados(),
            # empty unless the visitor is authorized: the template renders
            # the DISTRIBUIDORAS section only when there is something in it
            "mayoristas": self._productos_lista_oculta(),
            "presupuestar": self._cigarrillos_a_presupuestar(),
            # 19.0.1.92.0: before sending (models/sale_order.py STOCK_NOTICE)
            "leyenda_stock": request.env["sale.order"]._distri_stock_notice(),
            # 19.0.1.129.40: the cart kept on the device for a new order
            # (static/src/js/pedido_borrador.js), per user and price list
            **self._contexto_borrador(pedido),
            **self._contexto_ui_v2(),
        }

    def _contexto_borrador(self, pedido):
        """19.0.1.129.40: the key of the device draft of a new order (who
        and which price list, so a visitor never gets a logged-in cart nor
        another list's) and its script; nothing while changing an order."""
        if pedido:
            return {"borrador_clave": "", "borrador_js": ""}
        from .kit import kit_url
        usuario = request.env.user
        quien = "visitante" if usuario._is_public() else str(usuario.id)
        return {"borrador_clave": "%s:%s" % (quien, self._lista_del_visitante().id or 0),
                "borrador_js": kit_url("pedido_borrador.js")}

    def _contexto_ui_v2(self):
        """19.0.1.129.29: /pedido with the kit's look while distri_pedido.ui_v2
        is on (kit/pedido_v2.css; the same steps, fields and post for every
        visitor), and the role's bottom bar for the staff under the order
        bar. Off: nothing changes."""
        from .kit import kit_context, role_nav, ui_v2_on
        if not ui_v2_on(request.env):
            return {"ui_v2": False, "nav": []}
        return {"ui_v2": True, "dn": kit_context("pedido_v2.css"),
                "nav": role_nav(request.env, "pedido") if self._personal() else []}

    def _localidades_encomienda(self):
        """Towns suggested for «Soy del interior»: the zones that ship by
        parcel. The customer can type any other town."""
        nombres = request.env["distri.zona"].sudo().search(
            [("encomienda", "=", True)]).mapped("name")
        for nombre in ("Zapala", "Cutral Có"):
            if nombre not in nombres:
                nombres.append(nombre)
        return nombres

    def _datos_encomienda(self, post):
        """(values, error) of the «Soy del interior» form. Without the box
        ticked, the order is not a parcel requested by the customer."""
        from ..models.encomienda import normalize_dni
        if not post.get("encomienda"):
            return {"distri_encomienda_pedida": False}, ""
        nombre = " ".join((post.get("enc_nombre") or "").split())[:120]
        dni = normalize_dni(post.get("enc_dni"))
        localidad = " ".join((post.get("enc_localidad") or "").split())[:80]
        if not nombre:
            return {}, "Poné el nombre completo de quien retira la encomienda."
        if not dni:
            return {}, "El DNI tiene que tener 7 u 8 números."
        if not localidad:
            return {}, "Poné la localidad de la terminal donde retirás."
        return {
            "distri_encomienda_pedida": True,
            "distri_encomienda_nombre": nombre,
            "distri_encomienda_dni": dni,
            "distri_encomienda_localidad": localidad,
        }, ""

    def _encomienda_cliente(self, pedido):
        """What the customer sees of their parcel on the order page."""
        pedido = pedido.sudo()
        if not pedido.distri_encomienda:
            return False
        parcel = pedido.distri_parcel_ids[-1:]
        datos = {
            "localidad": pedido._distri_parcel_town(),
            "retira": pedido.distri_encomienda_retira,
            "despachada": bool(parcel),
            "paga": ("Envío pago" if (pedido.distri_envio_paga or "distri") == "distri"
                     else "Envío a pagar en destino"),
        }
        if parcel:
            datos.update({
                "transportista": parcel.carrier_id.name or "",
                "guia": parcel.guide_number or "",
                "fecha": self._hora_local(parcel.dispatched_at)[:10],
                "paga": ("Envío pago" if parcel.payer == "distri"
                         else "Envío a pagar en destino"),
                "descarga": bool(parcel.photo),
            })
        return datos

    def _socio_sin_asignar(self, mayorista=False):
        """Los pedidos entran a nombre de un socio generico, uno por lista."""
        return request.env["res.partner"].sudo()._distri_generic_partner_for(mayorista)

    def _editable(self, pedido):
        """Se puede tocar hasta que el armador lo toma, aprobado o no.

        Antes habia un corte fijo a las 08:00, que ademas se comparaba contra
        la hora UTC y cortaba a las 05:00. La regla vive en el modelo
        (sale.order._distri_editable) porque el backend la usa tambien.
        """
        return pedido._distri_editable()

    def _por_token(self, token):
        pedido = request.env["sale.order"].sudo().search(
            [("access_token", "=", token)], limit=1
        )
        return pedido

    # ------------------------------------------------------------- mensajes

    def _base_url(self):
        base = request.env["ir.config_parameter"].sudo().get_param("web.base.url")
        return (base or request.httprequest.host_url).rstrip("/")

    def _link_edicion(self, pedido):
        """The customer's order link: /p/<token> (19.0.1.129.3). The team lands
        on the backend order, anyone else on «Tu pedido» (/pedido/<access_token>,
        controllers/pedido_link.py)."""
        return pedido.sudo()._distri_link_url()

    def _url_whatsapp_empresa(self, texto):
        return request.env["distri.pedido.whatsapp.settings"].sudo().company_url(texto)

    def _espera_whatsapp(self, pedido):
        """El paso que falta mientras el pedido espera el WhatsApp del cliente."""
        pedido = pedido.sudo()
        if not pedido or pedido.state == "cancel" or pedido.distri_wa_state != "waiting":
            return False
        texto = self._mensaje_whatsapp(
            pedido, pedido.distri_nombre, pedido.distri_telefono, pedido.distri_domicilio,
            pedido.distri_entrega or "domicilio",
            mayorista=bool(pedido.partner_id.distri_generic_kind == "wholesale"))
        return {"url": self._url_whatsapp_empresa(texto), "codigo": pedido.distri_wa_code}

    def _cuenta_visible_al_cliente(self, pedido):
        """The order's customer sees his account (19.0.1.92.0): the WhatsApp
        texts promise it only then. Never for the generic contact."""
        socio = pedido.sudo().partner_id
        return bool(socio) and socio._distri_account_visible()

    def _texto_enviar_al_cliente(self, pedido, nombre):
        """«Enviar al cliente» (19.0.1.129.6): the same text as the company's
        WhatsApp, so the customer checks the order line by line as with
        AgilPedido. The link label promises the account only when he sees it."""
        from ..models.pedido_link import LINK_LABEL_WITH_ACCOUNT  # lazy: see _mp_error_class
        label = LINK_LABEL_WITH_ACCOUNT if self._cuenta_visible_al_cliente(pedido) else None
        return pedido.sudo()._distri_company_message_default(link_label=label)

    def _url_enviar_al_cliente(self, pedido, nombre, numero):
        """«Enviar al cliente»: el WhatsApp del vendedor, con el chat del
        cliente (o para elegirlo, si no hay numero)."""
        numero = _wa().wa_digits(numero)
        return "https://wa.me/%s?text=%s" % (
            numero, quote(self._texto_enviar_al_cliente(pedido, nombre)))

    def _mensaje_whatsapp(self, pedido, nombre, telefono, domicilio,
                          entrega="domicilio", mayorista=False):
        """The order's text for the company's WhatsApp (19.0.1.129.5: field
        lines, blocks split by a blank line, money without cents, the code
        line and the /p/ link at the end). Built by the order:
        models/enviar_empresa.py _distri_company_message."""
        return pedido.sudo()._distri_company_message(
            nombre, telefono, domicilio, entrega, mayorista=mayorista)

    def _url_enviado_empresa(self, pedido):
        """Where the page posts when «Enviar a la empresa» / «Enviar por
        WhatsApp» is tapped (sets distri_enviado_empresa)."""
        return "/pedido/%s/enviado-empresa" % pedido.sudo().access_token

    @http.route("/pedido/<string:token>/enviado-empresa", type="http", auth="public",
                methods=["POST"], csrf=False, sitemap=False)
    def enviado_empresa(self, token, **kw):
        """19.0.1.129.5: the company's WhatsApp was opened with the order.

        The page posts it on tap (the link itself opens wa.me). The order's
        access token is the credential, as on every /pedido/<token> page;
        it only sets a flag, so no CSRF token is needed."""
        pedido = self._por_token(token)
        if not pedido or pedido.state == "cancel":
            return request.make_json_response({"ok": False}, status=404)
        pedido.sudo()._distri_mark_sent_to_company()
        return request.make_json_response({"ok": True})

    # --------------------------------------------------------------- rutas

    # 19.0.1.104.0: the home page «/» lives in controllers/sitio.py (public site).

    @http.route("/pedido", type="http", auth="public", website=True, sitemap=False)
    def pantalla(self, **kw):
        if self._pedido_pausado():
            return self._pantalla_pausada()
        if self._debe_cambiar_clave():
            return request.redirect("/mi-clave")
        if self._falta_perfil_revendedor():
            return request.redirect("/mi-perfil")
        return request.render("distri_pedido.pantalla", self._contexto_pantalla())

    def _falta_perfil_revendedor(self):
        """A reseller who signed up alone and has not completed «Mi perfil de
        revendedor» yet (19.0.1.92.0, models/alta_revendedor.py)."""
        usuario = request.env.user
        return (usuario.share and not usuario._is_public()
                and usuario.partner_id.sudo().commercial_partner_id.distri_alta_estado == "perfil")

    @http.route("/presentacion", type="http", auth="public", website=True, sitemap=False)
    @staff_only
    def presentacion(self, **kw):
        """Una pagina para mandar por WhatsApp a la gerencia.

        Cuenta el proyecto en el celular y termina en un boton que entra a la
        plataforma de verdad: la idea es que no se quede leyendo, que pruebe.
        """
        return request.render("distri_pedido.presentacion", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("pedidos"),
            "ficha": ficha(),
        })

    @http.route("/presentacion/cobranza", type="http", auth="public", website=True, sitemap=False)
    @staff_only
    def presentacion_cobranza(self, **kw):
        """La propuesta de cobranza en ruta, en el mismo formato que /presentacion."""
        return request.render("distri_pedido.presentacion_cobranza", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("cobranza"),
            "ficha": ficha(),
        })

    @http.route("/presentacion/reparto", type="http", auth="public", website=True, sitemap=False)
    @staff_only
    def presentacion_reparto(self, **kw):
        """La app del repartidor, para mostrarles a los repartidores antes de usarla."""
        return request.render("distri_pedido.presentacion_reparto", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("reparto"),
            "ficha": ficha(),
        })

    @http.route("/presentacion/presupuesto", type="http", auth="public", website=True, sitemap=False)
    @staff_only
    def presentacion_presupuesto(self, **kw):
        """El presupuesto: Odoo Online contra Odoo Community propio y el servicio."""
        return request.render("distri_pedido.presentacion_presupuesto", {
            "empresa_id": request.env.company.id,
            "franja": project_strip("presupuesto"),
            "ficha": ficha(),
        })

    @http.route("/privacidad", type="http", auth="public", website=True, sitemap=False)
    def privacidad(self, **kw):
        """Politica de privacidad publica; Meta la pide para publicar la app de WhatsApp."""
        from ..models.reparto_gps import gps_settings
        return request.render("distri_pedido.privacidad", {
            "empresa": request.env.company,
            # 19.0.1.35.0: how long the drivers' coordinates are kept
            "gps_meses": gps_settings(request.env)["retention"],
        })

    @http.route("/mi-clave", type="http", auth="user", website=True, sitemap=False)
    def mi_clave(self, **post):
        """Cambio de contraseña del comercio."""
        usuario = request.env.user
        datos = {
            "empresa_id": request.env.company.id,
            "ficha": ficha(),
            "obligatorio": usuario.distri_debe_cambiar_clave,
            "login": usuario.login,
            "error": "",
        }
        if request.httprequest.method == "POST":
            actual = (post.get("actual") or "").strip()
            nueva = (post.get("nueva") or "").strip()
            repetida = (post.get("repetida") or "").strip()
            if len(nueva) < 8:
                datos["error"] = "La contraseña nueva tiene que tener 8 caracteres o más."
            elif nueva != repetida:
                datos["error"] = "Las dos contraseñas nuevas no coinciden."
            else:
                try:
                    usuario.change_password(actual, nueva)
                except Exception:
                    datos["error"] = "La contraseña actual no es correcta."
                else:
                    usuario.sudo().distri_debe_cambiar_clave = False
                    # 19.0.1.98.0: staff reach it from «Ajustes»: back home
                    return request.redirect("/mi-cuenta" if usuario.share else "/tablero")
        return request.render("distri_pedido.mi_clave", datos)

    @http.route("/mi-cuenta", type="http", auth="user", website=True, sitemap=False)
    def mi_cuenta(self, **kw):
        """La cuenta corriente del comercio que entro con su usuario."""
        if self._debe_cambiar_clave():
            return request.redirect("/mi-clave")
        socio = self._socio_del_usuario()
        if not socio:
            return request.redirect("/pedido")
        datos = {
            "pedido": False,
            "empresa_id": request.env.company.id,
            "ficha": ficha(),
            "whatsapp": self._whatsapp_empresa(),
            "token": "",
        }
        return self._pagina_cuenta(socio, datos, "/mi-cuenta", kw.get("pagina"))

    def _pagina_cuenta(self, socio, datos, url_cuenta, pagina):
        """The account page, or the neutral notice while it is hidden from
        the customer (19.0.1.92.0): no balance and no movements."""
        if self._cuenta_visible(socio):
            datos.update(self._cuenta_de(socio, pagina))
            datos.update({"oculta": False, "url_cuenta": url_cuenta})
        else:
            datos.update({"socio": socio, "oculta": True})
        return request.render("distri_pedido.cuenta_corriente", datos)

    @http.route("/pedido/<string:token>/cuenta", type="http", auth="public",
                website=True, sitemap=False)
    def cuenta(self, token, **kw):
        """La cuenta corriente, con el mismo link del pedido.

        Decision de ale (22/09/2026): se asume el riesgo de que el mensaje de
        WhatsApp se reenvie, y el mensaje avisa que el link es personal.
        """
        pedido = self._por_token(token)
        if not pedido:
            return request.redirect("/pedido")
        if self._bloqueado_para_vendedor(pedido):
            return self._error_simple(MENSAJE_DISTRIBUIDORA, status=403)
        cliente = self._cliente_asignado(pedido)
        if not cliente:
            return request.redirect("/pedido/%s" % token)
        datos = {
            "pedido": pedido,
            "empresa_id": request.env.company.id,
            "ficha": ficha(),
            "whatsapp": self._whatsapp_empresa(),
            "token": token,
        }
        return self._pagina_cuenta(cliente, datos, "/pedido/%s/cuenta" % token,
                                   kw.get("pagina"))

    @http.route("/estado-de-cuenta/<string:token>", type="http", auth="public",
                methods=["GET"], website=True, sitemap=False)
    def estado_compartido(self, token, **kw):
        """The statement a seller sent by WhatsApp from /cuentas (19.0.1.112.0,
        models/estado_cuenta.py share_token): the same page as «Mi cuenta»,
        read-only, only that customer, only the range of the link and only
        until it expires. While the account is hidden from the customer the
        page shows the neutral notice, like his own links."""
        from ..models.estado_cuenta import read_share_token
        enlace = read_share_token(request.env, token)
        if not enlace:
            return self._error_simple(
                "Este link venció o no es válido. Pedile a tu vendedor que te mande el estado "
                "de cuenta de nuevo.", status=404)
        socio = enlace["partner"]
        desde, hasta = enlace["date_from"], enlace["date_to"]
        datos = {
            "pedido": False,
            "empresa_id": request.env.company.id,
            "ficha": ficha(),
            "whatsapp": self._whatsapp_empresa(),
            "token": "",
            # shown by cuenta_corriente_compartida (views/cuentas.xml)
            "compartido": {
                "desde": desde.strftime("%d/%m/%Y") if desde else "",
                "hasta": hasta.strftime("%d/%m/%Y") if hasta else "",
                "vence": enlace["expires"].strftime("%d/%m/%Y"),
            },
        }
        if self._cuenta_visible(socio):
            datos.update(self._cuenta_de(socio, kw.get("pagina"), desde, hasta))
            datos.update({"oculta": False, "url_cuenta": "/estado-de-cuenta/%s" % token})
        else:
            datos.update({"socio": socio, "oculta": True})
        response = request.render("distri_pedido.cuenta_corriente", datos)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Robots-Tag"] = "noindex, nofollow"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @http.route("/pedido/<string:token>", type="http", auth="public",
                website=True, sitemap=False)
    def editar(self, token, **kw):
        """«Tu pedido»: the order link always lands here, editable or not.

        The link used to open the catalog straight away while the order was
        still editable, and customers took it for a NEW order. Now the catalog
        in edit mode is only reached with «Modificar mi pedido», which is shown
        while the order can still be changed (sale.order._distri_editable).
        Links already sent to customers keep working: they land here.
        """
        if self._debe_cambiar_clave():
            return request.redirect("/mi-clave")
        pedido = self._por_token(token)
        if not pedido:
            return request.redirect("/pedido")
        if self._bloqueado_para_vendedor(pedido):
            return self._error_simple(MENSAJE_DISTRIBUIDORA, status=403)
        editable = self._editable(pedido)
        datos = {
            "pedido": pedido,
            "empresa_id": request.env.company.id,
            "ficha": ficha(),
            "total": self._plata(pedido.amount_total),
            "whatsapp": self._whatsapp_empresa(),
            # el cliente quiso guardar un cambio y el armador ya lo tenia
            "llego_tarde": kw.get("tomado") == "1" and pedido.distri_armado != "pendiente",
            "ya_se_esta_armando": _ya_se_esta_armando(),
            "cancelado": pedido.state == "cancel",
            "cliente": self._cliente_asignado(pedido),
            "token": pedido.access_token,
            "leyenda_stock": pedido._distri_stock_notice(),
            # «Ver mi cuenta corriente» and the amounts owed (19.0.1.92.0)
            "mostrar_cuenta": self._cuenta_visible(self._cliente_asignado(pedido)),
            # not "editable": website's render context already uses that name
            # (the page editor flag) and overrides it
            "puede_modificar": editable,
            "url_modificar": self._url_modificar(pedido) if editable else "",
        }
        datos.update(self._seguimiento(pedido))
        datos["reparto"] = pedido._distri_route_status() if not datos["cancelado"] else False
        # «Datos para la entrega»: only for a real customer (not «Pedidos web»)
        datos["url_entrega"] = ("/pedido/%s/entrega" % pedido.access_token
                                if pedido._distri_delivery_data_open() else "")
        datos["aviso_cliente"] = (pedido.distri_customer_notice_text
                                  if pedido._distri_notice_open() else "")
        datos["mp"] = self._estado_mp(pedido, kw)
        datos["pago"] = self._como_pagar(pedido)
        datos["encomienda"] = self._encomienda_cliente(pedido)
        datos["espera"] = self._espera_whatsapp(pedido)
        datos["enviado_url"] = self._url_enviado_empresa(pedido)
        datos["guardado"] = kw.get("guardado") == "1" and editable
        # 19.0.1.99.0 (audit F7): the code the customer tells the driver
        datos["codigo_recibido"] = self._codigo_recibido(pedido)
        # 19.0.1.93.0 (E2E-C C11): lines left out of the change just saved
        try:
            datos["descartados"] = int(kw.get("descartados") or 0) if datos["guardado"] else 0
        except ValueError:
            datos["descartados"] = 0
        datos["avisar_cambio"] = (
            self._url_avisar_cambio(pedido)
            if datos["guardado"] and not datos["espera"] else False)
        # «Mandar comprobante»: the order number identifies the customer
        datos["comprobante"] = self._url_whatsapp_empresa(
            "Comprobante del pedido %s" % pedido.name)
        # «Cliente en rojo» (models/comprobante_pendiente.py): only with the
        # setting on, and only for the customer himself (not the generic one).
        # 19.0.1.92.0: nothing while his account is hidden from him
        datos["deuda"] = (pedido._distri_red_notice()
                          if datos["cliente"] and datos["mostrar_cuenta"]
                          and not datos["cancelado"] else False)
        # 19.0.1.103.0: «Subir comprobante de transferencia»
        datos["subir_comprobante"] = self._subir_comprobante(pedido, kw.get("comprobante"))
        return request.render("distri_pedido.seguimiento", datos)

    def _subir_comprobante(self, pedido, codigo):
        """The upload block of the order page: open or not (and why), the
        notice of the last post and the receipts uploaded from this page.
        False when there is nothing to show."""
        modulo = _comprobante_cliente()
        abierto, motivo = pedido._distri_customer_upload_open()
        filas = request.env["distri.whatsapp.message"].sudo()._distri_customer_rows(pedido)
        if not abierto and not filas:
            return False
        aviso = None
        if codigo == "estado":
            aviso = ("error", motivo) if motivo else None
        elif codigo in CUSTOMER_UPLOAD_NOTICES:
            tipo, texto = CUSTOMER_UPLOAD_NOTICES[codigo]
            if "%d" in texto:
                texto = texto % modulo.customer_max_per_day(request.env)
            aviso = (tipo, texto)
        return {
            "abierto": abierto,
            "motivo": motivo,
            "filas": filas,
            "aviso": aviso,
            "max_mb": modulo.CUSTOMER_MAX_MB,
            "url": "/pedido/%s/comprobante" % pedido.access_token,
        }

    @http.route("/pedido/<string:token>/comprobante", type="http", auth="public",
                methods=["POST"], website=True, sitemap=False)
    def subir_comprobante(self, token, **post):
        """«Subir comprobante de transferencia» from the order page
        (models/comprobante_cliente.py). Always back to the order page with
        ?comprobante=<code>: the page says what happened."""
        modulo = _comprobante_cliente()
        pedido = self._por_token(token)
        if not pedido or self._bloqueado_para_vendedor(pedido):
            return self._error_simple("No encontramos ese pedido.", status=404)
        volver = "/pedido/%s" % pedido.access_token

        def ir(codigo):
            return request.redirect("%s?comprobante=%s#subir-comprobante" % (volver, codigo), 303)

        if request.env["distri.rate.hit"].sudo()._distri_check(
                "comprobante_cliente", request.httprequest.remote_addr or "",
                request.session.sid or "", limits=modulo.CUSTOMER_UPLOAD_LIMITS):
            return ir("limite")
        archivo = request.httprequest.files.get("archivo")
        if not archivo or not archivo.filename:
            return ir("falta")
        raw = archivo.read(modulo.CUSTOMER_MAX_BYTES + 1)
        try:
            resultado = request.env["distri.whatsapp.message"].sudo()._distri_customer_upload(
                pedido, archivo.filename, raw, post.get("importe"))
        except modulo.CustomerUploadError as exc:
            return ir(exc.code)
        return ir("ok" if resultado["status"] == "nuevo" else "repetido")

    def _codigo_recibido(self, pedido):
        """The order's receipt code for its own page (the token already
        proved who is asking), while it can still be delivered."""
        pedido = pedido.sudo()
        if pedido.state == "cancel" or pedido.distri_delivery_state in ("delivered", "dispatched"):
            return ""
        return pedido.distri_receipt_code or ""

    def _url_avisar_cambio(self, pedido):
        """«Avisar el cambio por WhatsApp» after saving an edit.

        The confirmation page used to offer the edited order's message to
        the company; it stays available, but the order is already saved.
        Not for the seller's orders: those go to the customer, not to us.
        """
        pedido = pedido.sudo()
        if pedido.state == "cancel" or pedido.distri_wa_state == "staff":
            return False
        try:
            original = json.loads(pedido.distri_original or "[]")
        except ValueError:
            original = []
        texto = self._mensaje_whatsapp(
            pedido, pedido.distri_nombre, pedido.distri_telefono, pedido.distri_domicilio,
            pedido.distri_entrega or "domicilio",
            # same rule as when saving: one DISTRIBUIDORAS-section line makes it wholesale
            mayorista=any(r.get("may") for r in original))
        return self._url_whatsapp_empresa(texto)

    def _url_modificar(self, pedido):
        return "/pedido/%s/modificar" % pedido.access_token

    @http.route("/pedido/<string:token>/modificar", type="http", auth="public",
                website=True, sitemap=False)
    def modificar(self, token, **kw):
        """«Modificar mi pedido»: the catalog in edit mode, preloaded.

        Once the order can no longer be changed it goes back to «Tu pedido»,
        which explains why.
        """
        if self._debe_cambiar_clave():
            return request.redirect("/mi-clave")
        pedido = self._por_token(token)
        if not pedido:
            return request.redirect("/pedido")
        if self._bloqueado_para_vendedor(pedido):
            return self._error_simple(MENSAJE_DISTRIBUIDORA, status=403)
        if not self._editable(pedido):
            return request.redirect("/pedido/%s" % pedido.access_token)
        cantidades, cantidades_may = self._cantidades_por_seccion(pedido)
        datos = self._contexto_pantalla(
            pedido=pedido, cantidades=cantidades,
            cantidades_may=cantidades_may, token=pedido.access_token,
        )
        # «Volver sin cambios»: back to «Tu pedido» without saving anything
        datos["url_volver"] = "/pedido/%s" % pedido.access_token
        return request.render("distri_pedido.pantalla", datos)

    @http.route("/pedido/<string:token>/entrega", type="http", auth="public",
                methods=["GET", "POST"], website=True, sitemap=False)
    def entrega(self, token, **post):
        """«Datos para la entrega»: days and hours in which the shop receives,
        its location from the customer's phone and a notice for this order
        (models/datos_entrega.py). Only with the order's own link, for a real
        customer and a live order; otherwise back to «Tu pedido». The POST is
        CSRF-checked by Odoo (csrf=True is the default). Nothing is sent to
        the customer: what they did shows on these pages.
        """
        if self._debe_cambiar_clave():
            return request.redirect("/mi-clave")
        pedido = self._por_token(token)
        if not pedido:
            return request.redirect("/pedido")
        if not pedido._distri_delivery_data_open():
            return request.redirect("/pedido/%s" % pedido.access_token)
        error, formulario = "", {}
        if request.httprequest.method == "POST":
            accion = post.get("accion")
            if accion == "horarios":
                dias = [d for d in range(7) if post.get("dia_%s" % d)]
                rangos = [(post.get("desde_%s" % i, "")[:5], post.get("hasta_%s" % i, "")[:5])
                          for i in range(4)]
                error = pedido._distri_customer_set_hours(dias, rangos)
                formulario = {"dias": set(dias), "rangos": [list(r) for r in rangos
                                                            if r[0] or r[1]]}
            elif accion == "ubicacion":
                error = pedido._distri_customer_set_location(
                    post.get("lat"), post.get("lng"), post.get("precision"))
            elif accion == "aviso":
                error = pedido._distri_customer_set_notice(
                    post.get("tipo"), (post.get("texto") or "")[:1000])
                formulario = {"tipo": post.get("tipo"), "texto": (post.get("texto") or "")[:300]}
            elif accion == "borrar_aviso":
                error = pedido._distri_customer_clear_notice()
            else:
                error = "No entendimos qué querías guardar. Probá de nuevo."
            if not error:
                if accion == "aviso":
                    return request.redirect("/pedido/%s#aviso" % pedido.access_token)
                return request.redirect("/pedido/%s/entrega?listo=%s#%s" % (
                    pedido.access_token, accion, "ubicacion" if accion == "ubicacion" else
                    "aviso" if accion == "borrar_aviso" else "horarios"))
        return request.render("distri_pedido.datos_entrega",
                              self._contexto_entrega(pedido, error, post, formulario))

    def _contexto_entrega(self, pedido, error, post, formulario):
        from ..models.datos_entrega import (
            MAX_ACCURACY_M, NOTICE_KINDS, NOTICE_MAX, contact_hours_form,
        )
        contacto = pedido._distri_delivery_contact()
        dias, rangos = contact_hours_form(contacto)
        if formulario.get("rangos") is not None:
            dias = formulario["dias"]
            rangos = formulario["rangos"] + [["", ""]] * max(0, 2 - len(formulario["rangos"]))
        listos = {
            "horarios": "Listo, guardamos tus días y horarios. El repartidor los ve en su celular.",
            "ubicacion": "Listo, recibimos tu ubicación. Gracias.",
            "borrar_aviso": "Listo, borramos tu aviso.",
        }
        return {
            "pedido": pedido,
            "token": pedido.access_token,
            "empresa_id": request.env.company.id,
            "ficha": ficha(),
            "whatsapp": self._whatsapp_empresa(),
            "nombre": contacto._distri_app_name(),
            "domicilio": ", ".join(p for p in (contacto.street, contacto.street2) if p),
            "recibe": contacto.distri_delivery_text or "",
            "dias": dias,
            "nombres_dias": ["Lun", "Mar", "Mié", "Jue", "Vie", "Sáb", "Dom"],
            "rangos": rangos,
            "tiene_ubicacion": contacto._distri_has_location(),
            "ubicacion_cliente": self._hora_local(contacto.distri_customer_location_date),
            "precision_maxima": MAX_ACCURACY_M,
            "puede_avisar": pedido._distri_notice_open(),
            "tipos_aviso": NOTICE_KINDS,
            "aviso_tipo": formulario.get("tipo") or pedido.distri_customer_notice_kind or "",
            "aviso_texto": (formulario["texto"] if "texto" in formulario
                            else pedido.distri_customer_notice or ""),
            "aviso_actual": pedido.distri_customer_notice_text or "",
            "aviso_maximo": NOTICE_MAX,
            "error": error,
            "error_en": post.get("accion") if error else "",
            "listo": listos.get(post.get("listo") or "", ""),
            "listo_en": post.get("listo") or "",
        }

    @http.route("/pedido/<string:token>/guia", type="http", auth="public",
                website=True, sitemap=False)
    def guia(self, token, **kw):
        """«Descargar comprobante de guía»: the photo of the carrier's guide,
        only with the order's own link and only once it was dispatched."""
        pedido = self._por_token(token) if token else None
        if not pedido or not token:
            return request.not_found()
        parcel = pedido.sudo().distri_parcel_ids[-1:]
        if not parcel or not parcel.photo:
            return request.not_found()
        import base64
        data = base64.b64decode(parcel.photo)
        mimetype = "image/png" if data.startswith(b"\x89PNG") else "image/jpeg"
        nombre = "guia-%s-%s.%s" % (pedido.name, parcel.guide_number or "encomienda",
                                    "png" if mimetype == "image/png" else "jpg")
        return request.make_response(data, headers=[
            ("Content-Type", mimetype),
            ("Content-Disposition", 'attachment; filename="%s"' % re.sub(r"[^\w.-]", "_", nombre)),
            ("Cache-Control", "private, no-store"),
            ("X-Content-Type-Options", "nosniff"),
        ])

    def _renglones_validos(self, crudo, maximo=MAX_CANTIDAD):
        """The order lines posted by /pedido, checked (19.0.1.29.0): a JSON
        list of at most MAX_RENGLONES objects with a numeric product id and a
        whole quantity from 1 to `maximo`. 19.0.1.96.0 (audit F1): a
        fraction, zero or a negative is refused instead of rounded (the page
        only sends whole units above zero). Returns (lines, error)."""
        if len(crudo or "") > MAX_RENGLONES_TEXTO:
            return None, "El pedido es demasiado grande."
        try:
            renglones = json.loads(crudo or "[]")
        except ValueError:
            return None, "El pedido llegó incompleto."
        if not isinstance(renglones, list):
            return None, "El pedido llegó incompleto."
        if len(renglones) > MAX_RENGLONES:
            return None, "El pedido tiene demasiados productos (máximo %s)." % MAX_RENGLONES
        limpios = []
        for renglon in renglones:
            if not isinstance(renglon, dict):
                return None, "El pedido llegó incompleto."
            try:
                producto = int(renglon.get("id"))
                cantidad = float(renglon.get("cantidad") or 0)
            except (TypeError, ValueError):
                return None, "El pedido llegó incompleto."
            if not math.isfinite(cantidad) or cantidad < 0 or producto <= 0:
                return None, "El pedido llegó incompleto."
            if cantidad > maximo:
                return None, ("La cantidad de un producto es demasiado grande (máximo %s). Para "
                              "más, escribinos por WhatsApp." % maximo)
            if cantidad <= 0 or not cantidad.is_integer():
                return None, ("La cantidad de cada producto tiene que ser un número entero, "
                              "1 o más.")
            limpios.append(dict(renglon, id=producto, cantidad=unidades_enteras(cantidad)))
        return limpios, None

    def _tope(self, clave, defecto):
        """A cap of the public page from ir.config_parameter: the default
        when unset or unreadable, 0 (or less) = no cap."""
        valor = request.env["ir.config_parameter"].sudo().get_param(clave)
        try:
            return float(valor) if valor not in (None, False, "") else defecto
        except (TypeError, ValueError):
            return defecto

    def _maximo_unidades(self):
        """Units per line: MAX_CANTIDAD for staff, the tighter public cap
        for everyone else (never above MAX_CANTIDAD)."""
        if self._personal():
            return MAX_CANTIDAD
        tope = self._tope(PARAM_MAX_UNIDADES, MAX_UNIDADES_PUBLICO)
        return int(min(tope, MAX_CANTIDAD)) if tope > 0 else MAX_CANTIDAD

    def _error_total_maximo(self, total):
        """Above the public total cap (staff never): not created nor changed."""
        if self._personal():
            return None
        tope = self._tope(PARAM_MAX_TOTAL, MAX_TOTAL_PUBLICO)
        if tope <= 0 or total <= tope:
            return None
        return self._error_volver(
            "El pedido suma $ %s y por la página se pueden enviar hasta $ %s. Escribinos por "
            "WhatsApp y lo cargamos nosotros." % (self._plata(total), self._plata(tope)))

    @staticmethod
    def _datos_faltantes(nombre, telefono, domicilio, entrega, encomienda, ficha=False):
        """The page's own required fields (templates.xml, submit of #frm),
        now also checked on the server (19.0.1.96.0, audit F1): name and
        phone always; the address for a home delivery that is not a parcel
        (a parcel has its own fields, _datos_encomienda). Returns the
        message, or "" when nothing is missing. 19.0.1.129.40: `ficha` = a
        customer on file picked by staff in «Nueva venta»: his record is
        the contact, so neither the phone nor the address is required."""
        faltan = []
        if not nombre:
            faltan.append("el nombre del comercio")
        if not telefono and not ficha:
            faltan.append("un teléfono de contacto")
        if (entrega == "domicilio" and not encomienda.get("distri_encomienda_pedida")
                and not domicilio and not ficha):
            faltan.append("el domicilio de entrega")
        if not faltan:
            return ""
        lista = faltan[0] if len(faltan) == 1 else "%s y %s" % (", ".join(faltan[:-1]), faltan[-1])
        return "Faltan tus datos: poné %s." % lista

    def _plantillas_del_catalogo(self, ids):
        """The products /pedido/enviar accepts (19.0.1.91.0): only what the
        catalog on screen can show (_catalogo, _destacados, DISTRIBUIDORAS):
        sellable, published, active, not a service (shipping, «Venta sin
        detalle») and of the catalog currently on (_dominio_catalogo)."""
        return request.env["product.template"].sudo().search(
            [("id", "in", list(ids)), ("sale_ok", "=", True), ("is_published", "=", True),
             ("type", "!=", "service")] + self._dominio_catalogo())

    def _error_compra_minima(self, total):
        """Below COMPRA_MINIMA: the order is not created nor changed."""
        texto = ("La compra mínima por la página es de $ %s (este pedido suma $ %s). Las "
                 "compras más chicas se pueden pedir en el local." % (
                     self._plata(COMPRA_MINIMA), self._plata(total)))
        if self._personal():
            texto += " Administración las carga como presupuesto desde el sistema."
        return self._error_volver(texto)

    def _error_volver(self, texto, status=400):
        """An error that keeps the cart: «Volver» goes back to the page."""
        return request.make_response(
            "<!DOCTYPE html><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width'><p style='font:16px sans-serif;padding:1rem'>%s "
            "<a href='javascript:history.back()'>Volver</a></p>" % texto,
            headers=[("Content-Type", "text/html; charset=utf-8")], status=status)

    def _limite_superado(self):
        """Rate limit of /pedido/enviar (models/limites.py). Staff with their
        session open are not limited: a seller loads many orders in a row."""
        if self._personal():
            return False
        return request.env["distri.rate.hit"].sudo()._distri_check(
            "pedido", request.httprequest.remote_addr, request.session.sid)

    def _recargar_pedido(self, renglones, post):
        """The page expired (invalid token): the same page again, with the
        cart and the data already typed, and a fresh token."""
        def numero(valor):
            return int(valor) if float(valor).is_integer() else valor
        cantidades = {r["id"]: numero(r["cantidad"]) for r in renglones if not r.get("may")}
        cantidades_may = {r["id"]: numero(r["cantidad"]) for r in renglones if r.get("may")}
        datos = self._contexto_pantalla(cantidades=cantidades, cantidades_may=cantidades_may,
                                        token=(post.get("token") or "").strip())
        for campo in ("nombre", "telefono", "domicilio"):
            datos["datos"][campo] = (post.get(campo) or "").strip()[:200]
        datos["recarga"] = True
        return request.render("distri_pedido.pantalla", datos, status=403)

    @http.route("/pedido/enviar", type="http", auth="public",
                methods=["POST"], csrf=False, website=True, sitemap=False)
    def enviar(self, **post):
        """The order form of /pedido (customers, visitors and staff).

        Odoo's own CSRF check is off for this route (csrf=False) and done
        here instead, for everyone, with the token rendered in the page: an
        invalid or expired one shows the page again with the cart, instead
        of Odoo's error page that loses it. See docs «Seguridad» of
        documentacion/integraciones."""
        if self._pedido_pausado():
            return self._pantalla_pausada()
        if (request.httprequest.content_length or 0) > MAX_CUERPO:
            return self._error_simple("El pedido es demasiado grande.", status=413)
        renglones, error = self._renglones_validos(post.get("renglones"),
                                                   self._maximo_unidades())
        if error:
            return self._error_simple(error, status=400)
        if not renglones:
            return request.redirect("/pedido")
        limite = self._limite_superado()
        if limite:
            _logger.warning("/pedido/enviar: limit %s reached", limite)
            return self._error_simple(
                "Recibimos muchos pedidos seguidos desde esta conexión. Esperá unos minutos y "
                "volvé a enviarlo, o escribinos por WhatsApp.", status=429)
        if not request.validate_csrf(post.get("csrf_token") or ""):
            return self._recargar_pedido(renglones, post)

        nombre = (post.get("nombre") or "").strip()
        telefono = (post.get("telefono") or "").strip()
        domicilio = (post.get("domicilio") or "").strip()
        entrega = (post.get("entrega") or "domicilio").strip()
        # 19.0.1.98.0: optional comment, «Mi vendedor es», staff depot note
        extra = self._datos_extra(post)
        # 19.0.1.93.0 (E2E-C C9): an unknown value was a 500
        if entrega not in ENTREGAS:
            return self._error_volver(
                "No entendimos cómo querés recibir el pedido. Volvé y elegí envío a domicilio "
                "o retiro en el depósito.")

        # Staff taking the order: the session user is the seller, and the
        # customer can be chosen (the token was checked above, for everyone).
        personal = self._personal()
        cliente = request.env["res.partner"]
        if personal:
            cliente = self._cliente_elegido(post.get("cliente_id"))
            if cliente and not request.env.user._distri_may_reach_customer(cliente):
                # 19.0.1.129.20: an external seller, a customer not his
                return self._error_simple(MENSAJE_CLIENTE_AJENO, status=403)
            if cliente and self._solo_vendedor() and cliente._distri_es_distribuidora():
                # never repriced with a list the seller cannot see
                return self._error_simple(MENSAJE_DISTRIBUIDORA, status=403)
            if cliente and not telefono:
                # 19.0.1.115.0 (owner): a customer with a phone on file needs
                # none typed; a new one or one without a phone still does
                telefono = self._telefono_en_ficha(cliente)
        como_llega = "LO RETIRA EN EL DEPOSITO" if entrega == "retira" else "ENVIO A DOMICILIO"

        encomienda, error_encomienda = self._datos_encomienda(post)
        if error_encomienda:
            return self._error_volver(error_encomienda)
        if encomienda.get("distri_encomienda_pedida"):
            # parcel to the terminal: never «Lo voy a retirar»
            entrega = "domicilio"
            como_llega = "ENCOMIENDA A LA TERMINAL DE %s" % encomienda[
                "distri_encomienda_localidad"].upper()

        Tmpl = request.env["product.template"].sudo()
        # Prices always come from the lists the visitor is entitled to, never
        # from the request: a legacy `modo` field is ignored, and a `may` row
        # counts only for an authorized visitor and only for a product that
        # really is in the DISTRIBUIDORAS section. Anything else is priced
        # like a regular row (General list, cigarettes without price).
        lista_mayorista = (self._lista_oculta() if self._ve_lista_oculta()
                           else request.env["product.pricelist"])
        con_precio_may = set(lista_mayorista.item_ids.mapped("product_tmpl_id").ids)
        lista_visitante = self._lista_del_visitante()
        lineas = []
        sin_precio = []
        de_distribuidoras = []
        # alcanza con UN renglon de la seccion, con precio o a presupuestar,
        # para que el pedido entero sea de la lista Distribuidoras
        hay_distribuidoras = False
        # only what the catalog on screen offers (19.0.1.91.0)
        permitidas = set(self._plantillas_del_catalogo(r["id"] for r in renglones).ids)
        # 19.0.1.93.0 (E2E-C C11): what is no longer in the catalog is left
        # out, and the customer is told so
        descartados = []
        for renglon in renglones:
            plantilla = Tmpl.browse(renglon["id"])
            variante = plantilla.product_variant_id if renglon["id"] in permitidas else None
            if not variante:
                descartados.append(renglon["id"])
                continue
            unidad = self._unidad_de_venta(plantilla)
            valores = {
                "product_id": variante.id,
                "product_uom_qty": float(renglon["cantidad"]),
                "product_uom_id": unidad.id,
            }
            # a temporary AgilPedido product is never a DISTRIBUIDORAS row:
            # its «May» copies are products of their own, at their own price
            es_may = (bool(renglon.get("may")) and bool(lista_mayorista)
                      and not plantilla.distri_agil_temporal)
            if es_may and plantilla.categ_id.name in CATEGORIAS_A_PRESUPUESTAR:
                # a presupuestar: el gerente le pone el precio al asignarle
                # la lista del revendedor
                valores["price_unit"] = 0.0
                sin_precio.append(plantilla.name)
                hay_distribuidoras = True
                valores["distri_may"] = True
            elif es_may and plantilla.id in con_precio_may:
                # vino de la seccion DISTRIBUIDORAS: lleva su precio propio,
                # aunque el resto del pedido se valorice con otra lista
                factor = unidad.relative_factor or 1.0
                valores["price_unit"] = lista_mayorista._get_product_price(
                    variante, factor
                ) * factor
                de_distribuidoras.append(plantilla.name)
                hay_distribuidoras = True
                valores["distri_may"] = True
            elif self._sin_precio(plantilla):
                # entra en cero a proposito: el precio sale de la lista del
                # cliente cuando el gerente se lo asigna
                valores["price_unit"] = 0.0
                sin_precio.append(plantilla.name)
            else:
                # renglon comun: se fija el precio de SU lista, para que la
                # lista de la cotizacion no se lo cambie despues
                factor = unidad.relative_factor or 1.0
                valores["price_unit"] = lista_visitante._get_product_price(
                    variante, factor
                ) * factor
            lineas.append((0, 0, valores))
        ids_descartados = descartados
        descartados = self._nombres_descartados(ids_descartados)
        if not lineas:
            return self._error_volver(
                "Los productos de tu pedido ya no están en el catálogo: %s. Volvé y elegí otros."
                % descartados)

        token = (post.get("token") or "").strip()
        pedido = self._por_token(token) if token else None
        if self._bloqueado_para_vendedor(pedido):
            return self._error_simple(MENSAJE_DISTRIBUIDORA, status=403)
        if pedido:
            # el mismo bloqueo que usa el armador al tomarlo: o gana el cambio
            # del cliente o gana el armador, nunca los dos a la vez
            pedido._distri_bloquear()
        if pedido and not self._editable(pedido):
            # el armador ya lo tomo, o ya se entrego: antes esto creaba una
            # cotizacion nueva en silencio y quedaban dos
            if pedido.distri_armado != "pendiente":
                return request.redirect("/pedido/%s?tomado=1" % token)
            return request.redirect("/pedido/%s" % token)
        # the minimum purchase, new orders and changes alike,
        # after the refusals above (19.0.1.91.0):
        # only the priced lines count, a line «a confirmar» has no total yet
        total = sum(v["product_uom_qty"] * v["price_unit"] for _, _, v in lineas)
        if total < COMPRA_MINIMA:
            return self._error_compra_minima(total)
        # 19.0.1.96.0 (audit F1): the page's own required fields, checked
        # here too (a form posted outside the page skipped them), and a cap
        # on the total of the public page
        # 19.0.1.129.40 (owner): «Nueva venta» with a customer on file sends
        # his order with what his record has; the phone and the address are
        # not asked again (controllers/nueva_venta.py sets the context, never
        # the post, so the public page keeps every check)
        ficha = bool(cliente) and personal and bool(request.env.context.get(CTX_DATOS_DE_LA_FICHA))
        faltan = self._datos_faltantes(nombre, telefono, domicilio, entrega, encomienda,
                                       ficha=ficha)
        if faltan:
            return self._error_volver(faltan)
        # 19.0.1.102.0: the typed phone, checked and stored as +549 + 10 digits
        if ficha and not telefono:
            telefono_visto, error_telefono = "", ""
        else:
            telefono, telefono_visto, error_telefono = self._telefono_revisado(
                telefono, cliente, pedido)
        if error_telefono:
            return self._error_volver(error_telefono)
        demasiado = self._error_total_maximo(total)
        if demasiado:
            return demasiado

        nota = (
            "PEDIDO DESDE LA PAGINA - ASIGNAR CLIENTE\n"
            f"{como_llega}\n"
            f"Nombre: {nombre or '(sin dato)'}\n"
            f"Telefono: {telefono_visto or '(sin dato)'}\n"
            f"Domicilio: {domicilio or '(sin dato)'}"
        )
        if encomienda.get("distri_encomienda_pedida"):
            nota += "\nRetira en la terminal: %s, DNI %s" % (
                encomienda["distri_encomienda_nombre"], encomienda["distri_encomienda_dni"])
        if de_distribuidoras:
            nota += (
                "\n\nCON PRECIO DE LA SECCION DISTRIBUIDORAS (%s renglones): se "
                "valorizaron con la lista %s." % (len(de_distribuidoras), LISTA_OCULTA)
            )
        if sin_precio:
            nota += (
                "\n\nSIN PRECIO (%s renglones de cigarrillos): al asignar el "
                "cliente se valorizan con su lista." % len(sin_precio)
            )

        hay_mayorista = hay_distribuidoras
        original = json.dumps([
            {
                "id": valores["product_id"],
                "cantidad": valores["product_uom_qty"],
                "sin_precio": "price_unit" in valores and not valores["price_unit"],
                "may": valores.get("distri_may", False),
            }
            for _, _, valores in lineas
        ])
        for _, _, valores in lineas:
            valores.pop("distri_may", None)

        if pedido and self._editable(pedido):
            cambios = {
                "order_line": lineas, "note": nota, "distri_original": original,
                "distri_nombre": nombre, "distri_telefono": telefono,
                "distri_domicilio": domicilio, "distri_entrega": entrega,
                "distri_modificado": True,
            }
            cambios.update(encomienda)
            cambios.update(extra)
            # si todavia nadie le asigno el cliente, el socio generico sigue
            # el contenido del pedido; si ya lo asignaron, no se pisa
            socio_correcto = self._socio_sin_asignar(hay_mayorista)
            asignado = not pedido.partner_id.sudo().distri_generic_partner
            if not asignado:
                cambios["partner_id"] = socio_correcto.id
                if hay_mayorista and lista_mayorista:
                    cambios["pricelist_id"] = lista_mayorista.id
            pedido._distri_reemplazar_renglones([v for _, _, v in cambios.pop("order_line")])
            pedido.write(cambios)
            if asignado:
                # con el cliente asignado el precio sale de SU lista, igual
                # que cuando el gerente se lo asigno (sale_order.write)
                pedido._recompute_prices()
            # back to «Tu pedido» with «Listo, actualizamos tu pedido»; the
            # WhatsApp step, if still missing, is shown there
            return request.redirect("/pedido/%s?guardado=1%s" % (
                pedido.access_token,
                "&descartados=%d" % len(ids_descartados) if ids_descartados else ""))
        else:
            valores_pedido = {
                "partner_id": self._socio_sin_asignar(hay_mayorista).id,
                "order_line": lineas,
                "note": nota,
                "distri_original": original,
                "distri_nombre": nombre,
                "distri_telefono": telefono,
                "distri_domicilio": domicilio,
                "distri_entrega": entrega,
                **encomienda,
                **extra,
                # si trae renglones de la seccion DISTRIBUIDORAS la cotizacion queda
                # en esa lista: si no, el gerente ve "General" y al tocar
                # una cantidad Odoo le recalcula el precio con la lista equivocada
                "pricelist_id": (lista_mayorista.id if hay_mayorista and lista_mayorista
                                 else lista_visitante.id),
                # a logged-out order never gets a salesperson
                "user_id": False,
            }
            if personal:
                valores_pedido.update(self._valores_del_personal(cliente, nombre, telefono))
            # 19.0.1.93.0 (E2E-C C8): the same form posted twice (double tap,
            # a retry of the browser) returns the first order
            clave = self._clave_envio(post, renglones)
            previo = request.env["sale.order"].sudo().search(
                [("distri_envio_clave", "=", clave)], limit=1) if clave else None
            if previo:
                return self._pagina_gracias(previo, nombre, telefono_visto, domicilio, entrega,
                                            hay_mayorista, descartados)
            valores_pedido["distri_envio_clave"] = clave or False
            try:
                with request.env.cr.savepoint():
                    pedido = request.env["sale.order"].sudo().create(valores_pedido)
            except IntegrityError:
                # the twin request committed first: Odoo retries this one,
                # which then finds that order above
                raise ConcurrencyError("/pedido/enviar: the same form is being saved")
            pedido._portal_ensure_token()
            if not personal and request.env.user._is_public():
                # 19.0.1.96.0 (audit F1): an anonymous order waits out of the
                # packing until administración assigns the customer and
                # releases it («Pedidos para revisar», models/revisar.py)
                pedido._distri_retener_sin_cliente()
            if cliente:
                # same repricing as when the administrator assigns the customer
                pedido.with_context(distri_armado=True)._recompute_prices()
                self._guardar_whatsapp_del_cliente(cliente, telefono)
            pedido._distri_wa_start(staff=personal)
            # 19.0.1.99.0 (audit F7): the receipt code exists from the start
            # and the customer sees it on the confirmation and his order page
            pedido._distri_ensure_receipt_code()
        return self._pagina_gracias(pedido, nombre, telefono_visto, domicilio, entrega,
                                    hay_mayorista, descartados)

    def _telefono_revisado(self, telefono, cliente, pedido):
        """19.0.1.102.0: the phone of the form, with the rules of the page
        (models/ar_phone.py check_ar_phone). Returns (value to store, value
        to show, error). A good one is stored as +549 + 10 digits, the
        number WhatsApp links need. The phone already stored for the
        customer (staff picked him and the page filled it in, or the order
        being changed is already his) is never refused, whatever its old
        format: only what is typed is checked."""
        ar_phone = _ar_phone()
        nacional, error = ar_phone.check_ar_phone(telefono)
        if not error:
            return (ar_phone.AR_PREFIX + nacional, ar_phone.format_ar_national(nacional), "")
        if telefono in self._telefonos_guardados(cliente, pedido):
            return telefono, telefono, ""
        return telefono, telefono, error

    @staticmethod
    def _telefono_en_ficha(cliente):
        """The phone on file of the customer staff picked (his WhatsApp or
        phone, or his company's), or ""."""
        socio = cliente.sudo()
        return (socio._distri_whatsapp_shown()
                or socio.commercial_partner_id._distri_whatsapp_shown() or "").strip()

    @staticmethod
    def _telefonos_guardados(cliente, pedido):
        """The phones already on file for the customer of this order."""
        socios = cliente.sudo()
        if pedido and not pedido.sudo().partner_id.distri_generic_partner:
            socios |= pedido.sudo().partner_id
        socios |= socios.commercial_partner_id
        guardados = set(socios.mapped("distri_whatsapp")) | set(socios.mapped("phone"))
        return {(t or "").strip() for t in guardados} - {""}

    def _datos_extra(self, post):
        """19.0.1.98.0: the order's comment, «Mi vendedor es» and, for staff,
        the note for the depot. Unknown or inactive sellers are ignored;
        staff orders take the seller linked to the staff user."""
        def texto(campo):
            return " ".join((post.get(campo) or "").replace("\r", "").split(" "))[:500].strip()

        valores = {"distri_comentario": texto("comentario") or False}
        Vendedor = request.env["distri.vendedor.web"].sudo()
        if self._personal():
            valores["distri_nota_deposito"] = texto("nota_deposito") or False
            vendedor = Vendedor._distri_del_usuario(request.env.user)
            if vendedor:
                valores["distri_vendedor_web_id"] = vendedor.id
            return valores
        try:
            vendedor = Vendedor.browse(int(post.get("vendedor_web_id") or 0)).exists()
        except (TypeError, ValueError):
            vendedor = Vendedor
        valores["distri_vendedor_web_id"] = vendedor.id if vendedor and vendedor.active else False
        socio = self._socio_del_usuario()
        if socio and valores["distri_vendedor_web_id"]:
            # a logged-in customer: pre-selected next time
            socio.commercial_partner_id.sudo().distri_vendedor_web_id = vendedor
        return valores

    def _clave_envio(self, post, renglones):
        """C8: session, the form's id (`envio`, one per page shown) and the
        cart: a second post of the same form gives the same key. A page
        rendered before 19.0.1.93.0 has no id: no key, as before."""
        envio = re.sub(r"[^0-9a-f]", "", (post.get("envio") or "").lower())[:64]
        if len(envio) < 16:
            return ""
        partes = [request.session.sid or "", envio,
                  json.dumps(sorted((r["id"], r["cantidad"], bool(r.get("may")))
                                    for r in renglones)),
                  post.get("entrega") or "", post.get("cliente_id") or "",
                  str(request.env.uid or "")]
        return hashlib.sha256("|".join(partes).encode()).hexdigest()

    def _nombres_descartados(self, ids):
        """C11: «Alcohol x 500 ml, 1 producto más» for the lines left out."""
        if not ids:
            return ""
        plantillas = request.env["product.template"].sudo().with_context(
            active_test=False).browse(ids).exists().filtered("sale_ok")
        nombres = plantillas.mapped("name")
        otros = len(ids) - len(nombres)
        if otros:
            nombres.append("%s %s" % (otros, "producto" if otros == 1 else "productos")
                           if not nombres else
                           "%s %s más" % (otros, "producto" if otros == 1 else "productos"))
        return ", ".join(nombres)

    def _pagina_gracias(self, pedido, nombre, telefono, domicilio, entrega, hay_mayorista,
                        descartados=""):
        datos = {
            "pedido": pedido,
            "empresa_id": request.env.company.id,
            "descartados": descartados,
            "link_edicion": self._link_edicion(pedido),
            "mp_habilitado": request.env["distri.mp.payment"].sudo()._mp_link_enabled(),
            "paga_por_transferencia": bool(self._alias_transferencia()),
            "espera": False,
            "personal": False,
            "enviado_url": self._url_enviado_empresa(pedido),
            "codigo_recibido": self._codigo_recibido(pedido),
        }
        if pedido.distri_wa_state == "staff":
            # the seller's order is already identified: it goes to the customer
            nombre_cliente = pedido.partner_id.name if not pedido.partner_id.distri_generic_partner \
                else nombre
            datos.update({
                "personal": True,
                "cliente_nombre": nombre_cliente,
                "cliente_numero": pedido._distri_wa_customer_number(),
                "enviar_al_cliente": self._url_enviar_al_cliente(
                    pedido, nombre_cliente, pedido._distri_wa_customer_number()),
                # 19.0.1.129.5: every order reaches the company's WhatsApp
                "enviar_a_la_empresa": self._url_whatsapp_empresa(self._mensaje_whatsapp(
                    pedido, nombre_cliente, telefono or pedido._distri_company_phone(),
                    domicilio or pedido._distri_company_address(), entrega,
                    mayorista=hay_mayorista or pedido._distri_company_wholesale())),
                "enviado_empresa": pedido.distri_enviado_empresa,
            })
        else:
            texto = self._mensaje_whatsapp(pedido, nombre, telefono, domicilio,
                                           entrega, mayorista=hay_mayorista)
            datos["whatsapp"] = self._url_whatsapp_empresa(texto)
            datos["espera"] = pedido.distri_wa_state == "waiting"
        return request.render("distri_pedido.gracias", datos)

    def _pedido_pausado(self):
        """True while the order screen is paused (PARAM_PAUSADO), except for
        system administrators, who keep it to check it."""
        if request.env["ir.config_parameter"].sudo().get_param(PARAM_PAUSADO) != "1":
            return False
        return not request.env.user.has_group("base.group_system")

    def _pantalla_pausada(self):
        return request.make_response(
            "<!DOCTYPE html><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width'><title>Pedidos en pausa</title>"
            "<p style='font:16px sans-serif;padding:1rem'>La toma de pedidos online está "
            "en pausa por unos días mientras actualizamos el catálogo. Para hacer un "
            "pedido, comunicate con tu vendedor.</p>",
            headers=[("Content-Type", "text/html; charset=utf-8")], status=503)

    def _error_simple(self, texto, status=400):
        return request.make_response(
            "<!DOCTYPE html><meta charset='utf-8'><meta name='viewport' "
            "content='width=device-width'><p style='font:16px sans-serif;padding:1rem'>%s "
            "<a href='/pedido'>Volver</a></p>" % texto,
            headers=[("Content-Type", "text/html; charset=utf-8")], status=status)

    def _cliente_elegido(self, valor):
        """The customer picked by staff, if it is a real one. Whether this
        user may take orders for it is checked by the caller
        (res.users._distri_may_reach_customer, 19.0.1.129.20)."""
        try:
            socio_id = int(valor or 0)
        except (TypeError, ValueError):
            socio_id = 0
        if not socio_id:
            return request.env["res.partner"]
        socio = request.env["res.partner"].sudo().browse(socio_id).exists()
        if not socio or socio.distri_generic_partner or not socio.active:
            return request.env["res.partner"]
        return socio

    def _valores_del_personal(self, cliente, nombre, telefono):
        usuario = request.env.user
        valores = {}
        if cliente:
            valores["partner_id"] = cliente.id
            lista = cliente.property_product_pricelist
            if lista:
                valores["pricelist_id"] = lista.id
        # the seller who took it is the salesperson; administración and
        # gerencia keep Odoo's rule (the customer's salesperson, else them)
        if usuario.has_group("distri_pedido.grupo_administracion"):
            valores["user_id"] = (cliente.user_id or cliente.commercial_partner_id.user_id
                                  or usuario).id
        else:
            valores["user_id"] = usuario.id
        return valores

    def _guardar_whatsapp_del_cliente(self, cliente, telefono):
        """A customer without WhatsApp gets the phone the seller typed, in
        the canonical format; a number that cannot be read for sure is not
        stored (the confirmation asks for it)."""
        if cliente.distri_whatsapp:
            return
        try:
            numero = _wa().normalize_whatsapp(telefono)
        except ValueError:
            return
        if not numero:
            return
        cliente.sudo().write({"distri_whatsapp": numero})
        cliente.sudo().message_post(body="WhatsApp cargado por %s al tomar un pedido: %s." % (
            request.env.user.name, numero))

    @http.route("/pedido/clientes", type="http", auth="user", methods=["GET"],
                website=True, sitemap=False)
    def clientes(self, q="", **kw):
        """Customer search for staff on /pedido: their portfolio first, and
        every customer when they type."""
        if not self._personal():
            return request.make_response("[]", headers=[("Content-Type", "application/json")],
                                         status=403)
        usuario = request.env.user
        Partner = request.env["res.partner"].sudo()
        base = [("distri_generic_partner", "=", False), ("parent_id", "=", False),
                ("active", "=", True)]
        # 19.0.1.129.20: an external seller gets only his customers, also
        # when he types (no «otros»)
        propios = usuario._distri_customer_ids_limit()
        if propios is not None:
            base.append(("id", "in", list(propios)))
        cartera = usuario.etiqueta_cartera_id
        mios_dominio = ["|", ("user_id", "=", usuario.id),
                        ("category_id", "in", cartera.ids or [0])]
        texto = " ".join((q or "").split())[:60]
        if texto:
            dominio = base + ["|", "|", ("name", "ilike", texto), ("vat", "ilike", texto),
                              ("distri_whatsapp", "ilike", texto)]
            if propios is not None:
                mios = Partner.search(dominio, limit=30, order="name")
                otros = Partner.browse()
            else:
                mios = Partner.search(dominio + mios_dominio, limit=20, order="name")
                otros = Partner.search(dominio + [("id", "not in", mios.ids)],
                                       limit=max(0, 30 - len(mios)), order="name")
        elif propios is not None:
            mios = Partner.search(base, limit=50, order="name")
            otros = Partner.browse()
        else:
            mios = Partner.search(base + mios_dominio, limit=50, order="name")
            otros = Partner.browse()
        if self._solo_vendedor():
            # distributors and resellers are handled by Administración
            mios = mios.filtered(lambda s: not s._distri_es_distribuidora())
            otros = otros.filtered(lambda s: not s._distri_es_distribuidora())
        filas = [{
            "id": socio.id,
            "nombre": socio.name,
            "ciudad": socio.city or "",
            "domicilio": socio.street or "",
            "whatsapp": socio._distri_whatsapp_shown(),
            "mio": socio in mios,
        } for socio in mios | otros]
        return request.make_response(json.dumps(filas), headers=[
            ("Content-Type", "application/json"), ("Cache-Control", "no-store")])

    # ------------------------------------------------------- Mercado Pago

    @staticmethod
    def _hora_local(momento):
        """UTC datetime from the database, shown in Argentina time."""
        if not momento:
            return ""
        local = momento.replace(tzinfo=ZoneInfo("UTC")).astimezone(HORA_ARGENTINA)
        return local.strftime("%d/%m/%Y %H:%M")

    def _estado_mp(self, pedido, kw):
        """What the tracking page shows about paying with Mercado Pago.

        When the customer comes back from Checkout Pro, only `payment_id` is
        taken from the query string; the payment itself is fetched from MP.
        """
        Pago = request.env["distri.mp.payment"].sudo()
        estado = {"boton": False, "pagos": [], "devoluciones": [], "aviso": "", "error": ""}
        if not Pago._mp_enabled():
            return estado

        pago_id = (kw.get("payment_id") or kw.get("collection_id") or "").strip()
        if pago_id and MP_ID.match(pago_id) and pedido.sudo().distri_mp_reference:
            try:
                pago = Pago._mp_sync_payment(pago_id, "return")
            except _mp_error_class():
                pago = Pago
                estado["aviso"] = ("Todavía no pudimos confirmar tu pago. "
                                   "Volvé a abrir este enlace en unos minutos.")
            if pago and pago.order_id == pedido:
                if pago.status in ("pending", "in_process", "authorized"):
                    estado["aviso"] = ("Tu pago está pendiente de acreditación. "
                                       "Te lo vamos a mostrar acá cuando se apruebe.")
                elif pago.status in ("rejected", "cancelled"):
                    estado["aviso"] = ("El pago no se aprobó. Podés intentar de "
                                       "nuevo con otro medio de pago.")
        if kw.get("mp") == "error":
            estado["error"] = ("No pudimos abrir Mercado Pago en este momento. "
                               "Probá de nuevo en unos minutos.")

        aprobados = pedido._distri_mp_approved_payments()
        estado["pagos"] = [{
            "fecha": self._hora_local(p.date_approved),
            "monto": self._plata(p.amount),
            "id": p.payment_id,
        } for p in aprobados]
        if aprobados:
            estado["aviso"] = ""
        # MP has no refund date on the payment we store; write_date is when the
        # refunded status reached us, which is minutes after the refund.
        devueltos = pedido.sudo().distri_mp_payment_ids.filtered(
            lambda p: p.status in ("refunded", "charged_back"))
        estado["devoluciones"] = [{
            "fecha": self._hora_local(p.write_date),
            "monto": self._plata(p.amount),
            "id": p.payment_id,
        } for p in devueltos]
        estado["boton"] = not self._editable(pedido) and pedido._distri_mp_payable()
        return estado

    @http.route("/pedido/<string:token>/pagar", type="http", auth="public",
                methods=["POST"], website=True, sitemap=False)
    def pagar(self, token, **post):
        """Create (or reuse) the Checkout Pro preference and go pay.

        The amount always comes from the order, never from the request.
        """
        pedido = self._por_token(token)
        if not pedido:
            return request.redirect("/pedido")
        volver = "/pedido/%s" % pedido.access_token
        if not request.env["distri.mp.payment"].sudo()._mp_link_enabled():
            # link apagado (cobra comision): el cliente paga por transferencia
            # con los datos de la pagina del pedido
            return request.redirect(volver)
        if self._editable(pedido) or not pedido._distri_mp_payable():
            return request.redirect(volver)
        try:
            url = pedido._distri_mp_checkout_url(self._base_url())
        except _mp_error_class() as exc:
            _logger.warning("Mercado Pago checkout for %s failed: %s", pedido.name, exc)
            return request.redirect(volver + "?mp=error")
        return request.redirect(url, local=False)

    def _firma_mp_valida(self, secreto, data_id):
        """x-signature: ts=...,v1=HMAC-SHA256(secret, manifest)."""
        cabeceras = request.httprequest.headers
        partes = {}
        for trozo in (cabeceras.get("x-signature") or "").split(","):
            clave, _, valor = trozo.strip().partition("=")
            partes[clave.strip()] = valor.strip()
        ts, v1 = partes.get("ts", ""), partes.get("v1", "")
        if not ts or not v1:
            return False
        manifiesto = ""
        if data_id:
            manifiesto += "id:%s;" % data_id.lower()
        request_id = cabeceras.get("x-request-id") or ""
        if request_id:
            manifiesto += "request-id:%s;" % request_id
        manifiesto += "ts:%s;" % ts
        esperado = hmac.new(secreto.encode(), manifiesto.encode(), hashlib.sha256).hexdigest()
        return hmac.compare_digest(esperado, v1)

    @http.route("/pedido/mp/webhook", type="http", auth="public", methods=["POST"],
                csrf=False, sitemap=False, save_session=False)
    def mp_webhook(self, **kw):
        """Mercado Pago notifications. Only the payment id is used; the
        payment is always re-fetched from the MP API."""
        args = request.httprequest.args
        cuerpo = {}
        crudo = request.httprequest.get_data(cache=False)[:65536]
        if crudo:
            try:
                cuerpo = json.loads(crudo)
            except ValueError:
                cuerpo = {}
        if not isinstance(cuerpo, dict):
            cuerpo = {}
        datos = cuerpo.get("data") if isinstance(cuerpo.get("data"), dict) else {}

        tipo = str(args.get("type") or args.get("topic")
                   or cuerpo.get("type") or cuerpo.get("topic") or "")
        pago_id = str(args.get("data.id") or args.get("id")
                      or datos.get("id") or "").strip()

        if tipo != "payment":
            _logger.info("Mercado Pago notification of type %r ignored", tipo[:40])
            return request.make_response("ignored", status=200)
        if not MP_ID.match(pago_id):
            _logger.info("Mercado Pago notification with malformed id rejected")
            return request.make_response("bad request", status=400)

        Pago = request.env["distri.mp.payment"].sudo()
        secreto = Pago._mp_webhook_secret()
        if not secreto:
            # without the secret nothing can be verified: rejected (19.0.1.29.0;
            # before, the signature check was skipped). Mercado Pago retries.
            _logger.warning("Mercado Pago notification for %s rejected: no webhook secret "
                            "configured (distri_pedido.mp_webhook_secret)", pago_id)
            return request.make_response("webhook secret not configured", status=401)
        if not self._firma_mp_valida(secreto, args.get("data.id") or pago_id):
            _logger.warning("Mercado Pago notification for %s with invalid signature", pago_id)
            return request.make_response("invalid signature", status=401)
        if not Pago._mp_enabled():
            return request.make_response("ignored", status=200)

        try:
            pago = Pago._mp_sync_payment(pago_id, "webhook")
        except _mp_error_class():
            # transient: let Mercado Pago retry later
            return request.make_response("retry", status=503)
        _logger.info("Mercado Pago notification for payment %s: %s", pago_id,
                     pago.status if pago else "not ours / not found")
        return request.make_response("ok", status=200)
