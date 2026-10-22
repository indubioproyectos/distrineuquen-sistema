import base64
import io
import re
import zipfile
from collections import Counter
from xml.sax.saxutils import escape

from odoo import _, fields, models
from odoo.exceptions import UserError

from .importar_pedido import (
    codigo_agilpedido, nombre_agilpedido, precio_en_unidad, unidades_de_venta,
)

# Las columnas de la plantilla de importacion de AgilPedido, en este orden y
# con estos nombres (la descarga usa «Código» y «Precio salón»; la plantilla,
# sin acento, y acepta las dos). No hay columnas de foto, seccion, stock ni
# unidad.
COLUMNAS = ["Codigo", "Nombre", "Descripción", "Visible", "Precio",
            "Precio salon", "Destacado"]
# El precio va en PESOS con centavos (130000.0 = $ 130.000). Hasta el
# 27/09/2026 iba multiplicado por 100 («AgilPedido guarda centavos»), pero el
# productos.xlsx que descarga AgilPedido trae «Bulto DURACELL AAA X 120U.» a
# 130000.0, lo mismo que dice el WhatsApp del pedido ($ 130,000.00): los
# centavos son del volcado de la tienda, no del Excel (migracion-catalogo/
# README.md). Confirmarlo con la cuenta de prueba.
CENTAVOS = 1
# «Codigo» es numerico, es la clave con que AgilPedido actualiza (codigo
# conocido) o crea (codigo nuevo) el producto, y el mensaje lo trae adelante
# del nombre. El esquema, que el importador lee de vuelta, esta en
# importar_pedido.codigo_agilpedido: n (el «AP-n») para la fila principal,
# n * 10000 + N para otra unidad y n * 10000 + 5000 + N para la fila «May».


def normalizar(texto):
    """Para comparar nombres sin que molesten espacios ni mayusculas."""
    return re.sub(r"\s+", " ", (texto or "").strip().lower())


def _columna(indice):
    """0 -> A, 1 -> B, ... para armar las referencias de celda."""
    letras = ""
    indice += 1
    while indice:
        indice, resto = divmod(indice - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def armar_xlsx(filas):
    """Un xlsx minimo, sin dependencias: solo texto y numeros."""
    def celda(ref, valor):
        if isinstance(valor, (int, float)):
            return '<c r="%s"><v>%s</v></c>' % (ref, valor)
        if not valor:
            return '<c r="%s"/>' % ref
        return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                % (ref, escape(str(valor))))

    cuerpo = []
    for n, fila in enumerate(filas, start=1):
        celdas = "".join(celda("%s%s" % (_columna(i), n), v) for i, v in enumerate(fila))
        cuerpo.append('<row r="%s">%s</row>' % (n, celdas))

    hoja = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>%s</sheetData></worksheet>' % "".join(cuerpo)
    )
    tipos = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>'
    )
    rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>'
    )
    libro = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Productos" sheetId="1" r:id="rId1"/></sheets></workbook>'
    )
    libro_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>'
    )
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", tipos)
        z.writestr("_rels/.rels", rels)
        z.writestr("xl/workbook.xml", libro)
        z.writestr("xl/_rels/workbook.xml.rels", libro_rels)
        z.writestr("xl/worksheets/sheet1.xml", hoja)
    return buffer.getvalue()


class ExportarAgilpedido(models.TransientModel):
    _name = "distri.exportar.agilpedido"
    _description = "Exportar el catálogo con el formato de AgilPedido"

    archivo = fields.Binary(string="Archivo", readonly=True, attachment=False)
    nombre_archivo = fields.Char(string="Nombre", readonly=True)
    resumen = fields.Char(string="Resumen", readonly=True)

    def _filas(self):
        """Una fila por producto y unidad de venta, mas una fila «May ...» por
        precio mayorista.

        En Odoo un producto tiene dos precios. AgilPedido no tiene listas, asi
        que necesita DOS productos: el comun y el «May». El nombre del May es
        exactamente «May » + el nombre de la fila comun, para que despues el
        importador del mensaje de WhatsApp lo reconozca sin adivinar.

        AgilPedido tampoco tiene unidades de venta: un producto que en Odoo se
        vende por bulto va con el nombre de Odoo mas « · Bulto x 120 u.» y el
        precio de UN bulto, calculado igual que al importar el pedido
        (importar_pedido.precio_en_unidad), para que la diferencia de vuelta
        sea cero. Si se vende suelto y por bulto, va una fila por cada unidad.
        Devuelve (filas, {"comunes", "envases", "mays"}).
        """
        Producto = self.env["product.template"]
        productos = Producto.with_context(active_test=False).search(
            [("sale_ok", "=", True)], order="name",
        )
        mayorista, minorista = self.env["sale.order"]._distri_agil_listas()
        con_mayorista = set()
        if mayorista:
            for regla in mayorista.item_ids:
                if regla.product_tmpl_id and regla.compute_price == "fixed":
                    con_mayorista.add(regla.product_tmpl_id.id)

        # Las filas «May» se generan desde el precio mayorista. Los productos
        # que TODAVIA existen en Odoo llamados «May ...» son los que quedan por
        # convertir: si se exportaran los dos, AgilPedido recibiria el mismo
        # producto dos veces.
        generadas = {
            "may %s" % normalizar(p.name)
            for p in productos if p.id in con_mayorista
        }

        filas = [COLUMNAS]
        cuenta = {"comunes": 0, "envases": 0, "mays": 0, "acortados": []}
        for producto in productos:
            if (re.match(r"(?i)^\s*may[\s.]", producto.name)
                    and normalizar(producto.name) in generadas):
                continue
            visible = "Si" if (producto.active and producto.is_published) else "No"
            variante = producto.product_variant_id
            for unidad in unidades_de_venta(producto):
                nombre, acortado = nombre_agilpedido(producto, unidad)
                if minorista and variante:
                    precio = precio_en_unidad(minorista, variante, 1.0, unidad)
                else:
                    precio = producto.list_price
                # «Destacado» es una etiqueta de hasta 15 letras («OFERTA!»):
                # en Odoo no hay ese texto, va vacia
                filas.append([
                    codigo_agilpedido(producto, unidad), nombre, "", visible,
                    round(precio * CENTAVOS, 2), "", "",
                ])
                cuenta["envases" if " · " in nombre else "comunes"] += 1
                if acortado:
                    cuenta["acortados"].append(nombre)
                if producto.id in con_mayorista and variante:
                    nombre_may, acortado = nombre_agilpedido(producto, unidad, prefijo="May ")
                    precio_may = precio_en_unidad(mayorista, variante, 1.0, unidad)
                    filas.append([
                        codigo_agilpedido(producto, unidad, mayorista=True), nombre_may, "",
                        visible, round(precio_may * CENTAVOS, 2), "", "",
                    ])
                    cuenta["mays"] += 1
                    if acortado:
                        cuenta["acortados"].append(nombre_may)
        codigos = Counter(fila[0] for fila in filas[1:])
        repetidos = [codigo for codigo, veces in codigos.items() if veces > 1]
        if repetidos:
            raise UserError(_("Hay códigos repetidos en el archivo: %s") % repetidos[:10])
        return filas, cuenta

    def action_exportar(self):
        self.ensure_one()
        filas, cuenta = self._filas()
        self.write({
            "archivo": base64.b64encode(armar_xlsx(filas)),
            "nombre_archivo": "productos-para-agilpedido-%s.xlsx" % fields.Date.today(),
            "resumen": _(
                "%(comunes)s filas sueltas, %(envases)s por bulto o envase y %(mays)s filas "
                "«May» con el precio mayorista. Las fotos no van en el archivo.",
                comunes=cuenta["comunes"], envases=cuenta["envases"], mays=cuenta["mays"]) + (
                _(" %(n)s nombres pasaban los 50 caracteres de AgilPedido y se acortaron "
                  "(terminan en «…»): %(nombres)s.",
                  n=len(cuenta["acortados"]),
                  nombres="; ".join("«%s»" % nombre for nombre in cuenta["acortados"]))
                if cuenta["acortados"] else ""),
        })
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }
