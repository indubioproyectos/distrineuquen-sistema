"""TEMPORARY catalog on /pedido: an exact copy of the AgilPedido storefront
(19.0.1.89.0, owner's decision of 15/11/2026).

This week /pedido shows exactly what AgilPedido shows (names, prices,
sections, visibility) without touching the existing products (their invoice
history stays as it is). Each storefront row becomes its own temporary
product sold per 1 unit at the row price; it keeps the AgilPedido id in
`distri_agil_id` so that next week it can be merged into its definitive
product.

The switch is the parameter distri_pedido.catalogo_agil (models/agil_tienda.py):
'1' shows only the temporary products, '0' the usual catalog without them.
"""
import base64
import logging
from concurrent.futures import ThreadPoolExecutor

import requests
from markupsafe import Markup, escape

from odoo import Command, api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import float_compare

from .agil_tienda import (
    AGIL_TIMEOUT, AGIL_URL, IMAGEN_HILOS, IMAGEN_MAXIMO, IMAGEN_TIMEOUT, PARAM_AGIL_URL, PARAM_CATALOGO_AGIL, FeedInvalido,
    catalogo_agil_activo, clave, filas_agil, leer_feed, motivo_para_abortar,
)

_logger = logging.getLogger(__name__)

# How many names the summary lists per kind; the counts are always complete.
DETALLE_MAXIMO = 40


class ProductTemplateAgil(models.Model):
    _inherit = "product.template"

    distri_agil_id = fields.Char(
        string="Id de AgilPedido", index=True, copy=False, readonly=True,
        help="El id del producto en la tienda de AgilPedido. La actualización lo usa para "
             "encontrar el producto; la semana próxima sirve para unirlo al definitivo.")
    distri_agil_temporal = fields.Boolean(
        string="Temporal de AgilPedido", index=True, copy=False, readonly=True,
        help="Copia exacta de un renglón de la tienda de AgilPedido. Se muestra en /pedido "
             "solo con el catálogo de AgilPedido encendido.")
    distri_agil_seccion = fields.Char(string="Sección en AgilPedido", copy=False, readonly=True)
    distri_agil_seccion_orden = fields.Integer(string="Orden de la sección", copy=False,
                                               readonly=True)
    distri_agil_orden = fields.Integer(string="Orden en AgilPedido", copy=False, readonly=True)
    distri_agil_imagen_url = fields.Char(
        string="Foto de AgilPedido", copy=False, readonly=True,
        help="De dónde salió la foto (sin la firma del enlace). Se vuelve a bajar solo si "
             "AgilPedido cambia la foto.")

    # NULL (no id) repeats freely; a set id belongs to one product only
    _distri_agil_id_unique = models.Constraint(
        "UNIQUE(distri_agil_id)", "Ese id de AgilPedido ya es de otro producto.")


class ProductCategoryAgil(models.Model):
    _inherit = "product.category"

    distri_agil_temporal = fields.Boolean(
        string="Sección temporal de AgilPedido", copy=False, readonly=True,
        help="Creada por la actualización desde AgilPedido para una sección que no existía "
             "en Odoo.")


class AgilSincronizacion(models.Model):
    """One run of «Actualizar desde AgilPedido»: what changed, or why nothing did."""
    _name = "distri.agil.sincronizacion"
    _description = "Actualización del catálogo temporal desde AgilPedido"
    _inherit = ["mail.thread"]
    _order = "id desc"

    name = fields.Char(string="Actualización", required=True, readonly=True)
    origen = fields.Char(string="Origen", readonly=True)
    estado = fields.Selection([("ok", "Aplicada"), ("error", "Sin cambios: error")],
                              string="Resultado", required=True, readonly=True)
    leidos = fields.Integer(string="Productos en AgilPedido", readonly=True)
    creados = fields.Integer(string="Creados", readonly=True)
    precios = fields.Integer(string="Cambios de precio", readonly=True)
    renombrados = fields.Integer(string="Renombrados", readonly=True)
    despublicados = fields.Integer(string="Ocultados", readonly=True)
    republicados = fields.Integer(string="Vueltos a mostrar", readonly=True)
    fallidos = fields.Integer(string="Renglones con error", readonly=True)
    imagenes = fields.Integer(string="Fotos bajadas", readonly=True)
    imagenes_fallidas = fields.Integer(string="Fotos con error", readonly=True)
    sin_imagen = fields.Integer(string="Sin foto en AgilPedido", readonly=True)
    resumen = fields.Text(string="Detalle", readonly=True)
    error = fields.Text(string="Error", readonly=True)

    # ------------------------------------------------------------ entrada

    @api.model
    def _distri_descargar(self):
        """The storefront HTML. Raises FeedInvalido with a clear message."""
        url = (self.env["ir.config_parameter"].sudo().get_param(PARAM_AGIL_URL)
               or AGIL_URL).strip()
        try:
            respuesta = requests.get(url, timeout=AGIL_TIMEOUT, headers={
                "User-Agent": "Mozilla/5.0 (compatible; catalogo-sync)"})
            respuesta.raise_for_status()
        except requests.RequestException as exc:
            raise FeedInvalido("No se pudo descargar la tienda de AgilPedido (%s). "
                               "No se tocó el catálogo." % exc) from exc
        return respuesta.content

    @api.model
    def _distri_ejecutar(self, contenido=None, origen=None):
        """Download (or read `contenido`, an uploaded HTML/JSON) and apply.
        Always leaves a run record; on any error nothing is changed."""
        if not origen:
            origen = "Archivo subido" if contenido is not None else "Tienda de AgilPedido"
        try:
            with self.env.cr.savepoint():
                if contenido is None:
                    contenido = self._distri_descargar()
                return self._distri_aplicar(leer_feed(contenido), origen)
        except (FeedInvalido, UserError) as exc:
            mensaje = str(exc.args[0] if exc.args else exc)
            _logger.warning("AgilPedido catalog sync aborted: %s", mensaje)
            corrida = self.sudo().create({
                "name": self._distri_nombre(), "origen": origen, "estado": "error",
                "error": mensaje,
            })
            corrida.message_post(body=Markup("<b>No se aplicó.</b> %s") % mensaje)
            return corrida

    @api.model
    def _cron_distri_sincronizar(self):
        self._distri_ejecutar(origen="Tienda de AgilPedido (automática)")

    def _distri_nombre(self):
        ahora = fields.Datetime.context_timestamp(self, fields.Datetime.now())
        return "AgilPedido %s" % ahora.strftime("%d/%m/%Y %H:%M")

    # ------------------------------------------------------------ aplicar

    @api.model
    def _distri_aplicar(self, feed, origen):
        filas, rechazados = filas_agil(feed)
        Tmpl = self.env["product.template"].sudo().with_context(
            active_test=False, lang="en_US", mail_create_nolog=True)
        temporales = Tmpl.search([("distri_agil_temporal", "=", True)])
        visibles_hoy = len(temporales.filtered(lambda t: t.active and t.is_published))
        motivo = motivo_para_abortar(len(filas), visibles_hoy)
        if motivo:
            raise UserError(motivo)

        por_id = {t.distri_agil_id: t for t in temporales if t.distri_agil_id}
        ocupados = set(Tmpl.search([("distri_agil_id", "!=", False),
                                    ("distri_agil_temporal", "=", False)]).mapped("distri_agil_id"))
        idiomas = self.env["res.lang"].get_installed()
        moneda = self.env.company.currency_id
        categorias = {}
        detalle = {"creados": [], "precios": [], "renombrados": [], "republicados": [],
                   "despublicados": [], "imagenes": [], "imagenes_fallidas": [],
                   "sin_imagen": [],
                   "fallidos": ["%s: %s" % r for r in rechazados]}
        con_foto = []

        for fila in filas:
            if fila["agil_id"] in ocupados:
                # already merged into a definitive product: never touch it
                detalle["fallidos"].append("%s (%s): el id ya es de un producto definitivo"
                                           % (fila["nombre"], fila["agil_id"]))
                continue
            try:
                with self.env.cr.savepoint():
                    categoria = self._distri_categoria(fila["seccion"], categorias)
                    producto = por_id.get(fila["agil_id"])
                    if producto:
                        self._distri_actualizar(producto, fila, categoria, idiomas, moneda,
                                                detalle)
                    else:
                        producto = Tmpl.create(self._distri_valores_nuevo(fila, categoria))
                        detalle["creados"].append("%s — $ %s" % (fila["nombre"],
                                                                 _plata(fila["precio"])))
                con_foto.append((producto, fila))
            except Exception as exc:  # noqa: BLE001 (one bad row never stops the rest)
                _logger.exception("AgilPedido row %s failed", fila["agil_id"])
                detalle["fallidos"].append("%s (%s): %s" % (fila["nombre"], fila["agil_id"], exc))

        en_feed = {f["agil_id"] for f in filas}
        for producto in temporales:
            if producto.distri_agil_id not in en_feed and producto.is_published:
                # hidden in AgilPedido: hidden here, never deleted (invoices)
                producto.is_published = False
                detalle["despublicados"].append(producto.name)

        self._distri_imagenes(con_foto, detalle)
        return self._distri_registrar(origen, len(filas), detalle)

    @api.model
    def _distri_imagenes(self, pares, detalle):
        """The storefront photo of each temporary product (never of a
        definitive one: `pares` only holds the products this sync upserted).
        Downloaded only when AgilPedido changed it; cleared when AgilPedido
        dropped it. A failed photo is counted and retried on the next run,
        it never stops the sync."""
        bajar = []
        for producto, fila in pares:
            if not fila["imagen_clave"]:
                detalle["sin_imagen"].append(fila["nombre"])
                if producto.distri_agil_imagen_url:
                    producto.write({"image_1920": False, "distri_agil_imagen_url": False})
                continue
            if producto.distri_agil_imagen_url == fila["imagen_clave"] and producto.image_1920:
                continue
            bajar.append((producto, fila))
        if not bajar:
            return
        # only the HTTP part runs in threads; the ORM writes stay in this one
        with ThreadPoolExecutor(max_workers=IMAGEN_HILOS) as pool:
            descargas = list(pool.map(_bajar_imagen, [f["imagen_url"] for _p, f in bajar]))
        for (producto, fila), (contenido, error) in zip(bajar, descargas):
            if not error:
                try:
                    with self.env.cr.savepoint():
                        producto.write({"image_1920": base64.b64encode(contenido),
                                        "distri_agil_imagen_url": fila["imagen_clave"]})
                    detalle["imagenes"].append(fila["nombre"])
                    continue
                except Exception as exc:  # noqa: BLE001 (not an image, etc.)
                    error = str(exc.args[0] if exc.args else exc)
            detalle["imagenes_fallidas"].append("%s: %s" % (fila["nombre"], error))

    @api.model
    def _distri_categoria(self, seccion, cache):
        """The product category of an AgilPedido section: the existing one with
        the same name (accents, emojis and case aside), or a new one marked
        temporary. Only for accounting and stock: /pedido groups the
        temporary catalog by `distri_agil_seccion`, the exact section name."""
        llave = clave(seccion)
        if llave not in cache:
            Categoria = self.env["product.category"].sudo()
            iguales = Categoria.search([]).filtered(lambda c: clave(c.name) == llave)
            categoria = (iguales.filtered(lambda c: not c.distri_agil_temporal)[:1]
                         or iguales[:1])
            if not categoria:
                categoria = Categoria.create({"name": seccion, "distri_agil_temporal": True})
            cache[llave] = categoria
        return cache[llave]

    @api.model
    def _distri_valores_nuevo(self, fila, categoria):
        """A temporary product configured like the published ones of today:
        goods, invoiced on the ordered quantity, per Unit (factor 1, no sale
        units), no taxes (prices are final), sold and published."""
        return {
            "name": fila["nombre"],
            "list_price": fila["precio"],
            "distri_agil_id": fila["agil_id"],
            "distri_agil_temporal": True,
            "distri_agil_seccion": fila["seccion"],
            "distri_agil_seccion_orden": fila["seccion_orden"],
            "distri_agil_orden": fila["orden"],
            "categ_id": categoria.id,
            "type": "consu",
            "is_storable": True,
            "invoice_policy": "order",
            "uom_id": self.env.ref("uom.product_uom_unit").id,
            "uom_ids": [Command.clear()],
            "sale_ok": True,
            "purchase_ok": True,
            "is_published": True,
            "taxes_id": [Command.clear()],
            "supplier_taxes_id": [Command.clear()],
        }

    @api.model
    def _distri_actualizar(self, producto, fila, categoria, idiomas, moneda, detalle):
        valores = {}
        notas = []
        if float_compare(producto.list_price, fila["precio"],
                         precision_rounding=moneda.rounding) != 0:
            detalle["precios"].append("%s: $ %s → $ %s" % (
                fila["nombre"], _plata(producto.list_price), _plata(fila["precio"])))
            notas.append("Precio de AgilPedido: $ %s → $ %s" % (
                _plata(producto.list_price), _plata(fila["precio"])))
            valores["list_price"] = fila["precio"]
        if not producto.is_published or not producto.active or not producto.sale_ok:
            detalle["republicados"].append(fila["nombre"])
            valores.update(is_published=True, active=True, sale_ok=True)
        for campo, valor in (("distri_agil_seccion", fila["seccion"]),
                             ("distri_agil_seccion_orden", fila["seccion_orden"]),
                             ("distri_agil_orden", fila["orden"]),
                             ("categ_id", categoria.id)):
            actual = producto[campo].id if campo == "categ_id" else producto[campo]
            if actual != valor:
                valores[campo] = valor
        nombres = {codigo: producto.with_context(lang=codigo).name for codigo, _n in idiomas}
        if any(nombre != fila["nombre"] for nombre in nombres.values()):
            anterior = nombres.get("en_US") or next(iter(nombres.values()), "")
            detalle["renombrados"].append("%s → %s" % (anterior, fila["nombre"]))
            notas.append("Nombre en AgilPedido: %s → %s" % (anterior, fila["nombre"]))
            valores["name"] = fila["nombre"]
        if valores:
            producto.write(valores)
        if "name" in valores:
            # the same name in every language, so no visitor sees an old one
            producto.update_field_translations(
                "name", {codigo: fila["nombre"] for codigo, _n in idiomas})
        if notas:
            producto.message_post(body=Markup("<br/>").join(escape(n) for n in notas))

    @api.model
    def _distri_registrar(self, origen, leidos, detalle):
        cuentas = {tipo: len(nombres) for tipo, nombres in detalle.items()}
        titulos = (("creados", "Creados"), ("precios", "Cambios de precio"),
                   ("renombrados", "Renombrados"), ("despublicados", "Ocultados (no están en "
                                                                     "AgilPedido)"),
                   ("republicados", "Vueltos a mostrar"), ("fallidos", "Con error"),
                   ("imagenes", "Fotos bajadas"), ("imagenes_fallidas", "Fotos con error"),
                   ("sin_imagen", "Sin foto en AgilPedido"))
        # long lists that only need their count
        solo_cuenta = {"imagenes", "sin_imagen"}
        partes = ["Productos en AgilPedido: %s" % leidos]
        partes += ["%s: %s" % (titulo, cuentas[tipo]) for tipo, titulo in titulos]
        resumen = [" · ".join(partes)]
        for tipo, titulo in titulos:
            nombres = detalle[tipo]
            if not nombres or tipo in solo_cuenta or (
                    tipo == "creados" and len(nombres) > DETALLE_MAXIMO):
                continue
            resumen.append("")
            resumen.append("%s:" % titulo)
            resumen += ["- %s" % n for n in nombres[:DETALLE_MAXIMO]]
            if len(nombres) > DETALLE_MAXIMO:
                resumen.append("- … y %s más" % (len(nombres) - DETALLE_MAXIMO))
        corrida = self.sudo().create({
            "name": self._distri_nombre(), "origen": origen, "estado": "ok",
            "leidos": leidos, "creados": cuentas["creados"], "precios": cuentas["precios"],
            "renombrados": cuentas["renombrados"], "despublicados": cuentas["despublicados"],
            "republicados": cuentas["republicados"], "fallidos": cuentas["fallidos"],
            "imagenes": cuentas["imagenes"], "imagenes_fallidas": cuentas["imagenes_fallidas"],
            "sin_imagen": cuentas["sin_imagen"],
            "resumen": "\n".join(resumen),
        })
        corrida.message_post(body=Markup("<br/>").join(escape(l) for l in resumen))
        _logger.info("AgilPedido catalog sync: %s", resumen[0])
        return corrida


def _bajar_imagen(url):
    """(bytes, "") or (None, error) for one photo. Runs in a worker thread:
    plain HTTP only, no ORM."""
    try:
        respuesta = requests.get(url, timeout=IMAGEN_TIMEOUT, headers={
            "User-Agent": "Mozilla/5.0 (compatible; catalogo-sync)"})
        respuesta.raise_for_status()
        contenido = respuesta.content
    except requests.RequestException as exc:
        return None, "no se pudo bajar (%s)" % exc.__class__.__name__
    if not contenido:
        return None, "llegó vacía"
    if len(contenido) > IMAGEN_MAXIMO:
        return None, "pesa más de %s MB" % (IMAGEN_MAXIMO // (1024 * 1024))
    return contenido, ""


def _plata(monto):
    return "{:,.2f}".format(monto or 0).replace(",", "X").replace(".", ",").replace("X", ".")


class AgilSincronizarWizard(models.TransientModel):
    """«Actualizar desde AgilPedido»: the live storefront, or a saved file."""
    _name = "distri.agil.sincronizar"
    _description = "Actualizar el catálogo temporal desde AgilPedido"

    archivo = fields.Binary(string="Archivo (opcional)",
                            help="La página de AgilPedido guardada (HTML) o su JSON, para "
                                 "probar sin conexión. Vacío: se lee la tienda en vivo.")
    nombre_archivo = fields.Char(string="Nombre del archivo")
    catalogo_activo = fields.Boolean(
        string="/pedido muestra el catálogo de AgilPedido",
        default=lambda self: catalogo_agil_activo(self.env), readonly=True)
    temporales = fields.Integer(
        string="Productos temporales visibles",
        default=lambda self: self.env["product.template"].sudo().search_count(
            [("distri_agil_temporal", "=", True), ("is_published", "=", True)]),
        readonly=True)

    def _distri_check_permiso(self, grupo="distri_pedido.grupo_administracion"):
        usuario = self.env.user
        if not (usuario.has_group(grupo) or usuario.has_group("base.group_system")):
            raise AccessError("Solo Gerencia o Administración actualizan el catálogo de "
                              "AgilPedido.")

    def action_actualizar(self):
        self.ensure_one()
        self._distri_check_permiso()
        contenido = None
        if self.archivo:
            contenido = base64.b64decode(self.archivo)
        corrida = self.env["distri.agil.sincronizacion"].sudo()._distri_ejecutar(
            contenido, origen=("Archivo %s" % (self.nombre_archivo or "subido"))
            if contenido is not None else None)
        return {
            "type": "ir.actions.act_window",
            "res_model": "distri.agil.sincronizacion",
            "res_id": corrida.id,
            "view_mode": "form",
            "target": "current",
        }

    def _distri_cambiar_catalogo(self, valor):
        self.ensure_one()
        self._distri_check_permiso("distri_pedido.grupo_gerencia")
        self.env["ir.config_parameter"].sudo().set_param(PARAM_CATALOGO_AGIL, valor)
        return {"type": "ir.actions.client", "tag": "reload"}

    def action_mostrar_catalogo_agil(self):
        return self._distri_cambiar_catalogo("1")

    def action_volver_catalogo_odoo(self):
        return self._distri_cambiar_catalogo("0")


class SaleOrderCatalogoAgil(models.Model):
    """The WhatsApp orders of AgilPedido (agilpedido_wa.py): with the temporary
    catalog on, a row whose name is exactly a temporary product's name is that
    product, at its own price, before codes, learned equivalences or
    similarity."""
    _inherit = "sale.order"

    @api.model
    def _distri_agil_temporal_exacto(self, renglon):
        Tmpl = self.env["product.template"].sudo()
        dominio = [("distri_agil_temporal", "=", True), ("sale_ok", "=", True)]
        nombre = (renglon.get("nombre") or "").strip()
        if not nombre:
            return Tmpl
        exacto = Tmpl.search(dominio + [("name", "=", nombre)], order="is_published desc, id",
                             limit=1)
        if exacto:
            return exacto
        # same name except accents, case or spacing (WhatsApp formatting)
        llave = clave(nombre)
        iguales = Tmpl.search(dominio + [("name", "=ilike", nombre.split()[0] + "%")],
                              order="is_published desc, id").filtered(
            lambda t: clave(t.name) == llave)
        return iguales[:1]

    @api.model
    def _distri_agil_renglon(self, renglon, catalogo, repetidos, mayorista, minorista):
        if not catalogo_agil_activo(self.env):
            return super()._distri_agil_renglon(renglon, catalogo, repetidos, mayorista,
                                                minorista)
        plantilla = self._distri_agil_temporal_exacto(renglon)
        if not plantilla or not plantilla.product_variant_id:
            return super()._distri_agil_renglon(renglon, catalogo, repetidos, mayorista,
                                                minorista)
        variante = plantilla.product_variant_id
        lista = minorista or mayorista
        precio = (lista._get_product_price(variante, renglon["cantidad"] or 1.0)
                  if lista else plantilla.list_price)
        return {
            "product_uom_qty": renglon["cantidad"],
            "distri_agil_texto": renglon["texto"],
            "distri_agil_importe": renglon["importe_valor"] or 0.0,
            "product_id": variante.id,
            "product_uom_id": plantilla.uom_id.id,
            "price_unit": precio,
            "distri_agil_parecido": 100,
            "distri_agil_nota": "Catálogo temporal de AgilPedido (id %s)" % (
                plantilla.distri_agil_id or "?"),
            "distri_agil_corregir": False,
            "distri_agil_motivo": False,
        }

