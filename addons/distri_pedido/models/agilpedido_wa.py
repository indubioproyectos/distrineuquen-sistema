"""Pedidos de AgilPedido que llegan por WhatsApp: se cargan solos en borrador.

Transicion (27/09/2026): unos 15 dias antes de abrir la pagina nueva, los
clientes siguen pidiendo por AgilPedido, que manda el pedido como texto al
WhatsApp de la empresa. El aviso de WhatsApp (whatsapp.py) guarda cada
mensaje una vez; si el texto tiene la forma del mensaje de AgilPedido, aca se
crea el presupuesto en borrador para que el personal practique el circuito
nuevo con pedidos reales. NO se le responde nada al cliente.

Orden de las reglas sobre un texto que llega (pedido_whatsapp.py):
1. el codigo de un pedido de la pagina («Pedido S00164 · código 7K3P") gana
   siempre, con o sin el interruptor;
2. despues, si el interruptor esta prendido, el mensaje de AgilPedido; nunca
   el texto de la pagina (19.0.1.129.3: tiene la misma forma, pero lleva la
   linea del codigo o el link /p/, es_mensaje_de_la_pagina);
3. al final, la propuesta por telefono de un pedido que espera el WhatsApp.

El pedido entra como cualquier pedido web sin cliente: a nombre del contacto
generico, y el cliente se asigna con «Asignar cliente». Si el telefono es de
un solo cliente conocido, queda como sugerencia, nunca asignado solo: una
asignacion equivocada es peor que ninguna (plan de integracion, seccion 3).

Interruptor: parametro `distri_pedido.agilpedido_wa_auto` ("1" si falta),
tambien en «Transición AgilPedido › WhatsApp de pedidos».
"""
import json
import logging
import re
from collections import Counter

from psycopg2 import IntegrityError

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import plaintext2html

from .importar_pedido import (
    PARECIDO_MINIMO, catalogo_normalizado, identificar, identificar_por_codigo, importe_ar,
    leer_envase, leer_renglon, lista_general, lista_mayorista, precio_en_unidad, precio_renglon, unidad_de_venta, unidades_de_venta,
)
# pedido_whatsapp loads before this file (models/__init__.py): importing its
# pure helper does not change the order sale.order overrides are built in
from .pedido_whatsapp import find_web_codes

_logger = logging.getLogger(__name__)

PARAM_AUTO = "distri_pedido.agilpedido_wa_auto"
ORIGEN_AGILPEDIDO_WA = "agilpedido_wa"
ORIGENES = [(ORIGEN_AGILPEDIDO_WA, "Entró por WhatsApp (AgilPedido)")]
PRODUCTO_A_CORREGIR = "producto_agilpedido_a_corregir"
# Diferencia de redondeo que se tolera entre la suma del mensaje y su total.
TOLERANCIA = 1.0
MOTIVO_SIN_MAYORISTA = "Vino con «May» y no tiene precio mayorista cargado en el sistema"


def cantidad_texto(valor):
    """120.0 -> '120', 1.5 -> '1,5'"""
    return ("%.4f" % (valor or 0.0)).rstrip("0").rstrip(".").replace(".", ",")


def equivale_texto(factor, unidad, variante):
    """Lo que es 1 de AgilPedido: «1 = Bulto x 120 (120 u.)», «1 = 3 u.»."""
    if not unidad or unidad == variante.uom_id:
        return "1 = %s u." % cantidad_texto(factor)
    sueltas = unidad._compute_quantity(1.0, variante.uom_id, round=False) * factor
    if factor == 1:
        return "1 = %s (%s u.)" % (unidad.name, cantidad_texto(sueltas))
    return "1 = %s × %s (%s u.)" % (cantidad_texto(factor), unidad.name, cantidad_texto(sueltas))

# ------------------------------------------------------------------ lectura
# La plantilla fija de AgilPedido (verificada en el codigo de su tienda,
# plan_integracion_distri_neuquen.md seccion 6, y en los textos de su pagina):
#
#   Hola {empresa}! Soy {nombre} y quiero hacer el siguiente pedido hacia *{domicilio}*:
#   (o «... para retirar por el local en *{local}*:»)
#
#   - {cantidad} x {producto} ($ {subtotal})
#
#   *Total: $ {total}*
#
#   Comentario: {texto libre}
#
#   Muchas gracias!

# caracteres invisibles que agrega WhatsApp al copiar o reenviar
INVISIBLES = re.compile("[​-‏‪-‮⁠﻿]")
# la marca de un reenvio escrita en el texto («Reenviado», «Forwarded»)
MARCA_REENVIO = re.compile(r"^\W*(reenviado|forwarded)\W*$", re.IGNORECASE)
# el encabezado de un chat copiado: «[27/9 10:15] Juan Perez: »
PREFIJO_COPIA = re.compile(
    r"^\s*\[\d{1,2}[/.-]\d{1,2}(?:[/.-]\d{2,4})?,?\s+\d{1,2}:\d{2}(?::\d{2})?"
    r"(?:\s*[ap]\.?\s?m\.?)?\]\s*[^:\n]{1,60}:\s*", re.IGNORECASE)
CABECERA = re.compile(
    r"\bsoy\s*(?P<nombre>.*?)\s+y\s+quiero\s+hacer\s+el\s+siguiente\s+pedido\b(?P<resto>.*)$",
    re.IGNORECASE)
HACIA = re.compile(r"^\s*hacia\s+(?P<lugar>.*?)\s*:?\s*$", re.IGNORECASE)
RETIRA = re.compile(r"^\s*para\s+retirar\s+por\s+el\s+local\s+en\s+(?P<lugar>.*?)\s*:?\s*$",
                    re.IGNORECASE)
TOTAL = re.compile(r"^\s*total(?:\s+(?:del\s+)?pedido)?\s*:?\s*(?P<importe>\$?\s*[\d.,]+)\s*$",
                   re.IGNORECASE)
ENVIO = re.compile(r"^\s*env[ií]o\s+a\s+domicilio\s*:?\s*\(?(?P<importe>\$?\s*[\d.,]+)\)?\s*$",
                   re.IGNORECASE)
COMENTARIO = re.compile(r"^\s*comentario\s*:\s*(?P<texto>.*)$", re.IGNORECASE)
GRACIAS = re.compile(r"^\s*muchas\s+gracias\b", re.IGNORECASE)


# The page's own message (controllers/main.py _mensaje_whatsapp) reads like
# AgilPedido's since 19.0.1.129.3 («Soy *X* y quiero hacer el siguiente
# pedido ...», «- 2 x ... ($ ...)», «*Total: $ ...*»). Its order code line or
# its /p/<token> link (24 letters and digits, models/pedido_link.py) mark it
# as ours: never a new AgilPedido draft, even when its order is not found.
LINK_DE_LA_PAGINA = re.compile(r"/p/[A-Za-z0-9]{24}(?![A-Za-z0-9])")


def es_mensaje_de_la_pagina(texto):
    return bool(find_web_codes(texto) or LINK_DE_LA_PAGINA.search(texto or ""))


def _sin_formato(linea):
    """Sin los *negrita* / _cursiva_ / ~tachado~ de WhatsApp y con un solo espacio."""
    return " ".join(re.sub(r"[*_~]", " ", linea).split())


def _limpiar(texto):
    """Las lineas del mensaje sin marcas de reenvio ni encabezados de copia."""
    texto = INVISIBLES.sub("", (texto or "").replace("\r\n", "\n").replace("\r", "\n"))
    texto = texto.replace(" ", " ").replace(" ", " ")
    lineas, reenviado = [], False
    for linea in texto.split("\n"):
        if MARCA_REENVIO.match(linea):
            reenviado = True
            continue
        if PREFIJO_COPIA.match(linea):
            linea = PREFIJO_COPIA.sub("", linea, count=1)
        lineas.append(linea)
    return lineas, reenviado


def leer_mensaje_agilpedido(texto):
    """Los datos del mensaje de AgilPedido. `es_pedido` dice si el texto
    tiene la forma del mensaje: al menos un renglon de producto valido y
    ademas el encabezado («... quiero hacer el siguiente pedido») o el total.
    Un renglon suelto que se parezca no alcanza."""
    lineas, reenviado = _limpiar(texto)
    datos = {
        "nombre": "", "domicilio": "", "entrega": False, "comentario": "",
        "total": None, "envio": None, "renglones": [], "cabecera": False,
        "reenviado": reenviado, "es_pedido": False, "suma": 0.0, "cierra": None,
    }
    antes, comentario, en_comentario = [], [], False
    for linea in lineas:
        renglon = leer_renglon(linea)
        if renglon:
            renglon["importe_valor"] = importe_ar(renglon["importe"])
            datos["renglones"].append(renglon)
            en_comentario = False
            continue
        plana = _sin_formato(linea)
        if not datos["renglones"] and not datos["cabecera"]:
            antes.append(plana)
            cabecera = CABECERA.search(" ".join(antes))
            if cabecera:
                datos["cabecera"] = True
                datos["nombre"] = cabecera.group("nombre").strip(" ,.!")
                resto = cabecera.group("resto")
                hacia, retira = HACIA.match(resto), RETIRA.match(resto)
                if hacia:
                    datos.update(entrega="domicilio", domicilio=hacia.group("lugar").strip())
                elif retira:
                    datos.update(entrega="retira", domicilio=retira.group("lugar").strip())
            continue
        total, envio = TOTAL.match(plana), ENVIO.match(plana)
        if total:
            datos["total"] = importe_ar(total.group("importe"))
            en_comentario = False
        elif envio:
            datos["envio"] = importe_ar(envio.group("importe"))
            en_comentario = False
        elif COMENTARIO.match(plana):
            en_comentario = True
            comentario.append(COMENTARIO.match(plana).group("texto"))
        elif GRACIAS.match(plana):
            en_comentario = False
        elif en_comentario and plana:
            comentario.append(plana)
    datos["comentario"] = "\n".join(c for c in comentario if c).strip()
    validos = [r for r in datos["renglones"] if r["importe_valor"] is not None and r["cantidad"] > 0]
    datos["es_pedido"] = bool(validos) and (datos["cabecera"] or datos["total"] is not None)
    datos["suma"] = sum(r["importe_valor"] or 0.0 for r in datos["renglones"]) + (datos["envio"] or 0.0)
    if datos["total"] is not None:
        # control de integridad del plan: si se leyo mal un renglon, no cierra
        datos["cierra"] = abs(datos["suma"] - datos["total"]) <= TOLERANCIA
    return datos


class DistriWhatsappSettingsAgil(models.AbstractModel):
    _inherit = "distri.pedido.whatsapp.settings"

    @api.model
    def agilpedido_auto(self):
        value = (self.env["ir.config_parameter"].sudo().get_param(PARAM_AUTO) or "1").strip()
        return value.lower() not in ("0", "false", "no", "off")


class DistriWhatsappConfigAgil(models.TransientModel):
    _inherit = "distri.pedido.whatsapp.config"

    agilpedido_auto = fields.Boolean(
        string="Cargar solos los pedidos de AgilPedido",
        default=lambda self: self.env["distri.pedido.whatsapp.settings"].agilpedido_auto(),
        help="Activado: cada WhatsApp con el pedido de AgilPedido se carga solo como "
             "presupuesto en borrador, a nombre de «a asignar». No se le responde nada al "
             "cliente. El código de los pedidos de la página se reconoce igual, esté como esté.")

    def action_save(self):
        result = super().action_save()
        self.env["ir.config_parameter"].sudo().set_param(
            PARAM_AUTO, "1" if self.agilpedido_auto else "0")
        return result


class SaleOrderAgilpedido(models.Model):
    _inherit = "sale.order"

    distri_origen = fields.Selection(ORIGENES, string="Origen", copy=False, readonly=True,
                                     index=True)
    distri_agil_message_id = fields.Many2one(
        "distri.whatsapp.message", string="WhatsApp de AgilPedido", copy=False, readonly=True,
        index=True, ondelete="set null")
    distri_agil_texto = fields.Text(string="Mensaje de AgilPedido", copy=False, readonly=True)
    distri_agil_total = fields.Monetary(string="Total de AgilPedido", copy=False, readonly=True,
                                        currency_field="currency_id")
    distri_agil_diferencia = fields.Monetary(
        string="Diferencia con AgilPedido", currency_field="currency_id",
        compute="_compute_distri_agil_diferencia", store=True,
        help="Total del sistema menos el total que mostró AgilPedido. Una diferencia grande "
             "suele ser un producto mal elegido o un precio distinto.")
    distri_agil_cierra = fields.Boolean(
        string="El mensaje suma su total", copy=False, readonly=True,
        help="La suma de los renglones del mensaje da el total que dice AgilPedido. Si no, "
             "puede faltar un renglón o haberse leído mal.")
    distri_agil_por_corregir = fields.Integer(
        string="Renglones para corregir", compute="_compute_distri_agil_por_corregir",
        store=True)
    distri_agil_cliente_sugerido_id = fields.Many2one(
        "res.partner", string="Cliente sugerido por el teléfono", copy=False, readonly=True,
        ondelete="set null",
        help="El número que mandó el WhatsApp es de este cliente. Es una sugerencia: el "
             "cliente se asigna con «Asignar cliente».")
    distri_agil_cliente = fields.Char(string="Cliente (AgilPedido)",
                                      compute="_compute_distri_agil_cliente")

    _distri_agil_message_unique = models.Constraint(
        "UNIQUE(distri_agil_message_id)", "Ese WhatsApp de AgilPedido ya tiene su pedido.")

    @api.depends("amount_total", "distri_agil_total")
    def _compute_distri_agil_diferencia(self):
        for pedido in self:
            pedido.distri_agil_diferencia = (
                pedido.amount_total - pedido.distri_agil_total if pedido.distri_agil_total else 0.0)

    @api.depends("order_line.distri_agil_corregir")
    def _compute_distri_agil_por_corregir(self):
        for pedido in self:
            pedido.distri_agil_por_corregir = len(pedido.order_line.filtered("distri_agil_corregir"))

    @api.depends("partner_id", "distri_agil_cliente_sugerido_id")
    def _compute_distri_agil_cliente(self):
        for pedido in self:
            if not pedido.partner_id.distri_generic_partner:
                pedido.distri_agil_cliente = pedido.partner_id.display_name
            elif pedido.distri_agil_cliente_sugerido_id:
                pedido.distri_agil_cliente = "A asignar (sugerido: %s)" % (
                    pedido.distri_agil_cliente_sugerido_id.display_name)
            else:
                pedido.distri_agil_cliente = "A asignar"

    # ------------------------------------------------------------ reglas

    def _distri_armable(self):
        # con un producto sin corregir el armador armaria otra cosa
        if self.distri_agil_por_corregir:
            return False
        return super()._distri_armable()

    def action_confirm(self):
        for pedido in self:
            if pedido.distri_agil_por_corregir:
                raise UserError(
                    "El pedido %s tiene %s renglones de AgilPedido para corregir. Elegí el "
                    "producto de cada uno (o tocá «Está bien» si el propuesto es el correcto) "
                    "antes de aprobarlo." % (pedido.name, pedido.distri_agil_por_corregir))
        return super().action_confirm()

    # ------------------------------------------------------------ alta

    @api.model
    def _distri_agil_producto_a_corregir(self):
        """El producto que ocupa el renglon que no se pudo identificar.

        Se crea la primera vez que hace falta, como el contacto genérico: no
        se vende, no se publica y no entra al catálogo de AgilPedido."""
        xmlid = "distri_pedido.%s" % PRODUCTO_A_CORREGIR
        producto = self.env.ref(xmlid, raise_if_not_found=False)
        if producto:
            return producto.sudo()
        producto = self.env["product.product"].sudo().create({
            "name": "PRODUCTO A CORREGIR (AgilPedido)",
            "type": "service",
            "list_price": 0.0,
            "sale_ok": False,
            "purchase_ok": False,
            "is_published": False,
            "taxes_id": [fields.Command.clear()],
            "description_sale": "Renglón de AgilPedido sin producto identificado. Elegí el "
                                "producto correcto en el pedido.",
        })
        self.env["ir.model.data"].sudo().create({
            "module": "distri_pedido", "name": PRODUCTO_A_CORREGIR, "model": "product.product",
            "res_id": producto.id, "noupdate": True})
        return producto

    @api.model
    def _distri_agil_forma_de_pedido(self, texto):
        """El texto tiene la forma del mensaje de AgilPedido (con o sin el
        interruptor): lo usa la búsqueda del código de los pedidos web."""
        return leer_mensaje_agilpedido(texto)["es_pedido"]

    @api.model
    def _distri_agil_desde_mensaje(self, message):
        """Un texto que llegó al número de la empresa: si es el pedido de
        AgilPedido, su presupuesto en borrador. Devuelve el pedido (el ya
        creado si el mensaje se procesa de nuevo) o un recordset vacío."""
        Order = self.sudo()
        message = message.sudo()
        if message.direction != "in" or message.message_type != "text" or not message.body:
            return Order.browse()
        if not self.env["distri.pedido.whatsapp.settings"].agilpedido_auto():
            return Order.browse()
        datos = leer_mensaje_agilpedido(message.body)
        if not datos["es_pedido"] or es_mensaje_de_la_pagina(message.body):
            return Order.browse()
        existente = Order.search([("distri_agil_message_id", "=", message.id)], limit=1)
        if existente:
            return existente
        try:
            with self.env.cr.savepoint():
                return Order._distri_agil_crear(message, datos)
        except IntegrityError:
            # dos entregas del mismo aviso a la vez: gano la otra
            return Order.search([("distri_agil_message_id", "=", message.id)], limit=1)

    @api.model
    def _distri_agil_crear(self, message, datos):
        env = self.sudo().env
        catalogo = catalogo_normalizado(env)
        repetidos = Counter(catalogo.values())
        mayorista, minorista = self._distri_agil_listas()
        lineas = []
        for renglon in datos["renglones"]:
            lineas.append(fields.Command.create(
                self._distri_agil_renglon(renglon, catalogo, repetidos, mayorista, minorista)))
        hay_mayorista = any(r["es_mayorista"] for r in datos["renglones"])
        seller = env["res.users"]._distri_seller_by_phone(message.wa_id)
        reenviado = datos["reenviado"] or self._distri_agil_payload_reenviado(message)
        sugerido = env["res.partner"]
        if message.wa_id and not seller:
            clientes = env["res.partner"]._distri_partners_by_phone(message.wa_id)
            if len(clientes) == 1:
                sugerido = clientes
        telefono = "+%s" % message.wa_id if message.wa_id and not seller else ""
        nombre = datos["nombre"] or message.profile_name or ""
        valores = {
            "partner_id": env["res.partner"]._distri_generic_partner_for(hay_mayorista).id,
            "order_line": lineas,
            "note": plaintext2html(self._distri_agil_nota(datos, nombre, telefono, seller)),
            "distri_origen": ORIGEN_AGILPEDIDO_WA,
            "distri_agil_message_id": message.id,
            "distri_agil_texto": message.body,
            "distri_agil_total": datos["total"] or 0.0,
            "distri_agil_cierra": bool(datos["cierra"]),
            "distri_agil_cliente_sugerido_id": sugerido.id,
            "distri_nombre": nombre,
            "distri_telefono": telefono,
            "distri_domicilio": datos["domicilio"],
            "distri_entrega": datos["entrega"],
            # 19.0.1.98.0: the message's «Comentario:» is the customer comment
            "distri_comentario": datos["comentario"] or False,
            # a logged-out order never gets a salesperson (same as the page)
            "user_id": False,
        }
        # con renglones «May» el presupuesto queda en la lista Distribuidoras,
        # como el pedido de la página que trae renglones de esa sección
        lista = mayorista if hay_mayorista and mayorista else minorista
        if lista:
            valores["pricelist_id"] = lista.id
        pedido = self.sudo().with_context(distri_agil_importando=True).create(valores)
        message.distri_agil_order_id = pedido
        pedido.message_post(
            body=self._distri_agil_aviso(pedido, message, datos, seller, reenviado, sugerido),
            author_id=self.env.ref("base.partner_root").id)
        return pedido

    @api.model
    def _distri_agil_listas(self):
        """(Distribuidoras, General): the hidden «May» list and the public one
        (named «Mayorista» and «Minorista» until 29/09/2026)."""
        env = self.sudo().env
        return lista_mayorista(env), lista_general(env)

    @api.model
    def _distri_agil_precio(self, variante, unidad, cantidad, es_mayorista, mayorista, minorista):
        """El precio de Odoo del renglón, nunca el del mensaje: «May» con la
        lista Distribuidoras, el resto con la General; por bulto si va por bulto.
        Devuelve (valores, sin_regla) como precio_renglon."""
        plantilla = variante.product_tmpl_id
        precio, sin_regla = precio_renglon(plantilla, cantidad, es_mayorista, mayorista, unidad)
        if not precio and minorista:
            # renglón común: el precio público, aunque el presupuesto quede en
            # la lista Distribuidoras por traer renglones «May» (igual que la página)
            precio = {"price_unit": precio_en_unidad(minorista, variante, cantidad, unidad)}
        return precio, sin_regla

    @api.model
    def _distri_agil_renglon(self, renglon, catalogo, repetidos, mayorista, minorista):
        """Los valores del renglón del presupuesto para un renglón del mensaje.

        Orden: 1) el código de AgilPedido adelante del nombre («251269 -
        ...», importar_pedido.identificar_por_codigo); 2) la equivalencia
        aprendida (agil_equivalencia.py); 3) el nombre exacto; 4) el envase
        del nombre («Bulto X X 120U.» o «X · Bulto x 120 u.» = «X» en «Bulto x
        120»), con el nombre base exacto; 5) el más parecido, marcado."""
        cantidad = renglon["cantidad"]
        valores = {
            "product_uom_qty": cantidad,
            "distri_agil_texto": renglon["texto"],
            "distri_agil_importe": renglon["importe_valor"] or 0.0,
        }
        por_codigo = identificar_por_codigo(self.env, renglon)
        if por_codigo:
            return self._distri_agil_renglon_codigo(
                valores, renglon, por_codigo, mayorista, minorista)
        equivalencia = self.env["distri.agil.equivalencia"]._distri_buscar(renglon["nombre_limpio"])
        if equivalencia and equivalencia._distri_vendible():
            return self._distri_agil_renglon_equivalencia(
                valores, renglon, equivalencia, mayorista, minorista)
        identificado = identificar(renglon, catalogo)
        producto, parecido = identificado["producto"], identificado["parecido"]
        envase, por_envase = identificado["envase"], identificado["por_envase"]
        es_mayorista = identificado["es_mayorista"]
        valores["distri_agil_parecido"] = round(parecido * 100)
        roto = ""
        if equivalencia:
            roto = "La equivalencia aprendida (%s) ya no se puede vender" % (
                equivalencia.product_id.display_name)
        if not producto or parecido < PARECIDO_MINIMO:
            motivo = roto or "No se encontró el producto"
            if producto:
                motivo += " (lo más parecido: %s, %s %%)" % (producto.name, round(parecido * 100))
            valores.update({
                "product_id": self._distri_agil_producto_a_corregir().id,
                "name": "A CORREGIR: %s" % renglon["texto"],
                "price_unit": 0.0,
                "distri_agil_corregir": True,
                "distri_agil_motivo": motivo,
            })
            return valores
        unidad, coincide = unidad_de_venta(producto, envase and envase["contiene"])
        motivo = ""
        if roto:
            motivo = roto + ": confirmá el producto"
        elif parecido < 1.0:
            motivo = "Parecido al %s %%: confirmá que sea este producto" % round(parecido * 100)
        elif identificado["repetidos"] > 1:
            motivo = "Hay %s productos con este nombre: elegí el correcto" % (
                identificado["repetidos"])
        elif not coincide:
            motivo = "El nombre dice envase de %s y el producto se vende en %s: elegí la unidad" % (
                envase["contiene"], ", ".join(unidades_de_venta(producto).mapped("name")))
        elif por_envase:
            valores["distri_agil_nota"] = "Por envase: %s" % unidad.name
        variante = producto.product_variant_id
        precio, sin_regla = self._distri_agil_precio(
            variante, unidad, cantidad, es_mayorista, mayorista, minorista)
        if sin_regla and not motivo:
            motivo = MOTIVO_SIN_MAYORISTA
        valores.update(precio, product_id=variante.id, product_uom_id=unidad.id,
                       distri_agil_corregir=bool(motivo), distri_agil_motivo=motivo or False)
        return valores

    @api.model
    def _distri_agil_renglon_codigo(self, valores, renglon, por_codigo, mayorista, minorista):
        """El renglón por el código de AgilPedido: manda el código, aunque el
        nombre no sea el de Odoo (se avisa). Si el producto ya no se vende o
        no se vende en esa unidad, se marca para corregir."""
        variante, unidad = por_codigo["variante"], por_codigo["unidad"]
        valores["distri_agil_parecido"] = 100
        if not por_codigo["vendible"]:
            valores.update({
                "product_id": self._distri_agil_producto_a_corregir().id,
                "name": "A CORREGIR: %s" % renglon["texto"],
                "price_unit": 0.0,
                "distri_agil_corregir": True,
                "distri_agil_motivo": "El código %s es de «%s», que ya no se vende: elegí el "
                                      "producto" % (renglon["codigo"], variante.display_name),
            })
            return valores
        precio, sin_regla = self._distri_agil_precio(
            variante, unidad, renglon["cantidad"], por_codigo["es_mayorista"], mayorista,
            minorista)
        motivo = ""
        if not por_codigo["coincide"]:
            motivo = "El nombre dice otro envase y el producto se vende en %s: elegí la unidad" % (
                ", ".join(unidades_de_venta(variante.product_tmpl_id).mapped("name")))
        elif sin_regla:
            motivo = MOTIVO_SIN_MAYORISTA
        nota = "Por código %s" % renglon["codigo"]
        if por_codigo["nombre_distinto"]:
            nota += "; en AgilPedido el nombre no es el del sistema"
        valores.update(precio, product_id=variante.id, product_uom_id=unidad.id,
                       distri_agil_nota=nota, distri_agil_corregir=bool(motivo),
                       distri_agil_motivo=motivo or False)
        return valores

    @api.model
    def _distri_agil_renglon_equivalencia(self, valores, renglon, equivalencia, mayorista,
                                          minorista):
        """El renglón con lo que el personal ya enseñó: el producto y la
        unidad aprendidos, la cantidad por el factor y el precio de Odoo. No
        se marca (salvo un «May» sin precio mayorista, como siempre)."""
        variante = equivalencia.product_id
        cantidad = renglon["cantidad"] * equivalencia.factor
        precio, sin_regla = self._distri_agil_precio(
            variante, equivalencia.product_uom_id, cantidad, renglon["es_mayorista"], mayorista,
            minorista)
        equivalencia._distri_registrar_uso()
        valores.update(precio, product_id=variante.id, product_uom_qty=cantidad,
                       product_uom_id=equivalencia.product_uom_id.id,
                       distri_agil_parecido=100,
                       distri_agil_equivalencia_id=equivalencia.id,
                       distri_agil_nota="Por equivalencia: %s" % equivalencia._distri_texto(),
                       distri_agil_corregir=sin_regla,
                       distri_agil_motivo=MOTIVO_SIN_MAYORISTA if sin_regla else False)
        return valores

    @api.model
    def _distri_agil_payload_reenviado(self, message):
        """Meta marca los reenvíos en el aviso: context.forwarded."""
        try:
            contexto = json.loads(message.payload or "{}").get("context") or {}
        except (ValueError, AttributeError):
            return False
        return bool(isinstance(contexto, dict) and (
            contexto.get("forwarded") or contexto.get("frequently_forwarded")))

    @api.model
    def _distri_agil_nota(self, datos, nombre, telefono, seller):
        entrega = ("Retira en el local: %s" % (datos["domicilio"] or "(sin dato)")
                   if datos["entrega"] == "retira"
                   else "Domicilio: %s" % (datos["domicilio"] or "(sin dato)"))
        lineas = [
            "PEDIDO DE AGILPEDIDO POR WHATSAPP - ASIGNAR CLIENTE",
            "Nombre: %s" % (nombre or "(sin dato)"),
            "Telefono: %s" % (telefono or "(sin dato)"),
            entrega,
        ]
        if seller:
            lineas.append("Reenviado desde el WhatsApp del vendedor %s" % seller.name)
        if datos["envio"]:
            lineas.append("Envio a domicilio en AgilPedido: $ %s" % _plata(datos["envio"]))
        if datos["comentario"]:
            lineas += ["", "Comentario: %s" % datos["comentario"]]
        return "\n".join(lineas)

    @api.model
    def _distri_agil_aviso(self, pedido, message, datos, seller, reenviado, sugerido):
        numero = "+%s" % message.wa_id if message.wa_id else "un número sin dato"
        partes = ["Entró por WhatsApp (AgilPedido) desde %s: %s renglones, %s para corregir." % (
            numero, len(pedido.order_line), pedido.distri_agil_por_corregir)]
        aprendidos = len(pedido.order_line.filtered("distri_agil_equivalencia_id"))
        if aprendidos:
            partes.append("%s por equivalencia aprendida." % aprendidos)
        if reenviado:
            partes.append("El mensaje llegó reenviado.")
        if seller:
            partes.append("El número es del vendedor %s: no se usa para buscar al cliente." %
                          seller.name)
        if sugerido:
            partes.append("Por el teléfono parece ser %s. Confirmalo en «Asignar cliente»." %
                          sugerido.display_name)
        if datos["total"] is None:
            partes.append("El mensaje no trae el total.")
        elif not datos["cierra"]:
            partes.append("Ojo: la suma de los renglones del mensaje ($ %s) no da el total de "
                          "AgilPedido ($ %s). Puede faltar un renglón." % (
                              _plata(datos["suma"]), _plata(datos["total"])))
        return " ".join(partes)

    def action_distri_agil_ver_mensaje(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "distri.whatsapp.message",
            "res_id": self.distri_agil_message_id.id,
            "view_mode": "form",
        }


def _plata(valor):
    """1234.5 -> '1.234,50'"""
    return ("{:,.2f}".format(valor or 0.0)).replace(",", "X").replace(".", ",").replace("X", ".")


class SaleOrderLineAgilpedido(models.Model):
    _inherit = "sale.order.line"

    distri_agil_texto = fields.Char(string="Como vino en AgilPedido", copy=False, readonly=True)
    distri_agil_importe = fields.Monetary(string="Subtotal en AgilPedido", copy=False,
                                          readonly=True, currency_field="currency_id")
    distri_agil_parecido = fields.Integer(string="Coincidencia %", copy=False, readonly=True)
    distri_agil_corregir = fields.Boolean(
        string="Para corregir", copy=False, readonly=True,
        help="El renglón de AgilPedido no se identificó con seguridad. Se limpia al elegir "
             "el producto, o con «Está bien» si el propuesto es el correcto.")
    distri_agil_motivo = fields.Char(string="Qué revisar", copy=False, readonly=True)
    distri_agil_nota = fields.Char(
        string="Equivalencia", copy=False, readonly=True,
        help="Cómo se tradujo el renglón de AgilPedido: por una equivalencia aprendida, por el "
             "envase del nombre o por la corrección del personal.")
    distri_agil_equivalencia_id = fields.Many2one(
        "distri.agil.equivalencia", string="Equivalencia aprendida", copy=False, readonly=True,
        index="btree_not_null", ondelete="set null")
    distri_agil_sin_producto = fields.Boolean(compute="_compute_distri_agil_sin_producto")

    @api.depends("product_id")
    def _compute_distri_agil_sin_producto(self):
        a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR, raise_if_not_found=False)
        for linea in self:
            linea.distri_agil_sin_producto = bool(a_corregir) and linea.product_id == a_corregir

    def write(self, vals):
        if ("product_id" not in vals or self.env.context.get("distri_agil_importando")
                or self.env.context.get("distri_agil_corrigiendo")):
            return super().write(vals)
        antes = {linea.id: linea.product_id for linea in self if linea.distri_agil_corregir}
        result = super().write(vals)
        if antes:
            a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR,
                                      raise_if_not_found=False)
            corregidas = self.filtered(lambda l: l.id in antes and l.product_id != antes[l.id]
                                       and l.product_id != a_corregir)
            corregidas._distri_agil_unidad_de_venta()
            corregidas._distri_agil_marcar_corregidas()
        return result

    def _distri_agil_unidad_de_venta(self):
        """Se eligió el producto a mano: si solo se vende por bulto y quedó en
        otra unidad (Odoo propone la de stock), va a su unidad de venta, con
        el precio de Odoo de esa unidad, como en la página."""
        mayorista, minorista = self.env["sale.order"]._distri_agil_listas()
        for linea in self:
            plantilla = linea.product_id.product_tmpl_id
            if linea.product_uom_id in unidades_de_venta(plantilla):
                continue
            renglon = leer_renglon(linea.distri_agil_texto or "")
            envase = renglon and leer_envase(renglon["nombre_limpio"])
            unidad = unidad_de_venta(plantilla, envase and envase["contiene"])[0]
            precio, _sin_regla = self.env["sale.order"]._distri_agil_precio(
                linea.product_id, unidad, linea.product_uom_qty,
                bool(renglon and renglon["es_mayorista"]), mayorista, minorista)
            valores = dict(precio, product_uom_id=unidad.id)
            if "price_unit" in valores:
                valores["technical_price_unit"] = valores["price_unit"]
            linea.sudo().with_context(distri_agil_corrigiendo=True).write(valores)

    def _distri_agil_marcar_corregidas(self, aprender=True):
        for linea in self:
            texto = "Renglón de AgilPedido corregido por %s: «%s» → %s." % (
                self.env.user.name, linea.distri_agil_texto or "", linea.product_id.display_name)
            if aprender:
                equivalencia = linea._distri_agil_aprender()
                if equivalencia:
                    texto += " %s" % linea._distri_agil_aprendido_texto(equivalencia)
            linea.order_id.message_post(body=texto)
        self.sudo().write({"distri_agil_corregir": False, "distri_agil_motivo": False})

    def _distri_agil_aprender(self, factor=None):
        """La equivalencia del nombre de este renglón (agil_equivalencia.py).
        Solo para un renglón que el personal corrigió; nunca el producto «A
        CORREGIR»."""
        self.ensure_one()
        renglon = leer_renglon(self.distri_agil_texto or "")
        if not renglon or not self.product_id or self.distri_agil_sin_producto:
            return self.env["distri.agil.equivalencia"]
        equivalencia = self.env["distri.agil.equivalencia"]._distri_aprender(
            renglon["nombre_limpio"], self.product_id, self.product_uom_id, factor=factor,
            pedido=self.order_id)
        if equivalencia:
            self.sudo().write({"distri_agil_equivalencia_id": equivalencia.id})
        return equivalencia

    @api.model
    def _distri_agil_aprendido_texto(self, equivalencia):
        return "Queda aprendido para los próximos pedidos: «%s»: %s de %s." % (
            equivalencia.nombre_agil, equivalencia._distri_texto(),
            equivalencia.product_id.display_name)

    def action_distri_agil_renglon_ok(self):
        """«Está bien»: el producto propuesto es el correcto."""
        a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR,
                                  raise_if_not_found=False)
        for linea in self:
            if linea.product_id == a_corregir:
                raise UserError("Este renglón no tiene producto: elegí el producto correcto.")
            if linea.order_id.state not in ("draft", "sent"):
                raise UserError("El pedido ya está aprobado.")
        marcadas = self.filtered("distri_agil_corregir")
        # un «May» sin precio mayorista ya tenía el producto bien: no hay nada
        # que aprender, solo se acepta el precio
        solo_precio = marcadas.filtered(lambda l: l.distri_agil_motivo == MOTIVO_SIN_MAYORISTA)
        (marcadas - solo_precio)._distri_agil_marcar_corregidas()
        solo_precio._distri_agil_marcar_corregidas(aprender=False)
        return True

    def action_distri_agil_corregir(self):
        """Abre «Corregir producto» para este renglón."""
        self.ensure_one()
        if self.order_id.state not in ("draft", "sent"):
            raise UserError("El pedido ya está aprobado.")
        return {
            "type": "ir.actions.act_window",
            "name": "Corregir producto de AgilPedido",
            "res_model": "distri.agil.corregir.renglon",
            "view_mode": "form",
            "target": "new",
            "context": {"default_linea_id": self.id},
        }

    def _distri_agil_corregir_con_factor(self, variante, unidad, factor, renglon):
        """Lo que hace «Corregir producto»: el producto y la unidad elegidos,
        la cantidad de AgilPedido por el factor, el precio de Odoo con las
        reglas de siempre, sin marca, y la equivalencia aprendida."""
        self.ensure_one()
        mayorista, minorista = self.env["sale.order"]._distri_agil_listas()
        cantidad = renglon["cantidad"] * factor
        precio, sin_regla = self.env["sale.order"]._distri_agil_precio(
            variante, unidad, cantidad, renglon["es_mayorista"], mayorista, minorista)
        antes = "%s × %s" % (cantidad_texto(self.product_uom_qty), self.product_id.display_name)
        valores = dict(precio, product_id=variante.id, product_uom_id=unidad.id,
                       product_uom_qty=cantidad)
        if "price_unit" in valores:
            valores["technical_price_unit"] = valores["price_unit"]
        self.with_context(distri_agil_corrigiendo=True).write(valores)
        equivalencia = self._distri_agil_aprender(factor=factor)
        self.sudo().write({
            "distri_agil_corregir": sin_regla,
            "distri_agil_motivo": MOTIVO_SIN_MAYORISTA if sin_regla else False,
            "distri_agil_nota": "Corregido: %s" % equivale_texto(factor, unidad, variante),
        })
        texto = "Renglón de AgilPedido corregido por %s: «%s» (%s) → %s %s de %s." % (
            self.env.user.name, self.distri_agil_texto or "", antes, cantidad_texto(cantidad),
            unidad.name, variante.display_name)
        if equivalencia:
            texto += " %s" % self._distri_agil_aprendido_texto(equivalencia)
        if sin_regla:
            texto += " Falta el precio mayorista en el sistema: el renglón sigue marcado."
        self.order_id.message_post(body=texto)
        return True


class WhatsappMessageAgilpedido(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_agil_order_id = fields.Many2one(
        "sale.order", string="Pedido de AgilPedido", readonly=True, index=True,
        ondelete="set null")


class DistriAsignarClienteAgil(models.TransientModel):
    _inherit = "distri.asignar.cliente"

    agil_sugerido_id = fields.Many2one(
        related="order_id.distri_agil_cliente_sugerido_id", string="Sugerido por el teléfono")
    agil_origen = fields.Selection(related="order_id.distri_origen")

    @api.model
    def default_get(self, fields_list):
        valores = super().default_get(fields_list)
        pedido = self.env["sale.order"].browse(valores.get("order_id"))
        sugerido = pedido.exists() and pedido.distri_agil_cliente_sugerido_id
        if sugerido and "partner_id" in fields_list and not sugerido.distri_generic_partner:
            # la sugerencia queda elegida; el administrador la confirma o cambia
            valores["partner_id"] = sugerido.id
        return valores

    @api.depends("busqueda", "order_id")
    def _compute_candidatos(self):
        super()._compute_candidatos()
        for asistente in self:
            sugerido = asistente.order_id.distri_agil_cliente_sugerido_id
            if sugerido:
                asistente.candidato_ids |= sugerido
