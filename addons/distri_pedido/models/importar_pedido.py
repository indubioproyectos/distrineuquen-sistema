import difflib
import re
import unicodedata

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .. controllers.main import CATEGORIAS_A_PRESUPUESTAR, lista_general, lista_oculta  # noqa: F401 (lista_general re-exported)

# La linea de producto del mensaje de AgilPedido. El formato sale de su propio
# codigo: `- ${cantidad} x ${nombre} (${subtotal})`.
RENGLON = re.compile(r"^\s*[-*•]\s*(\d+(?:[.,]\d+)?)\s*x\s*(.+?)\s*\(([^()]*)\)\s*$")
# Debajo de cuanto parecido dejamos de proponer un producto y pedimos ayuda.
PARECIDO_MINIMO = 0.72
# AgilPedido no tiene listas de precios: el precio mayorista lo mete en el
# NOMBRE, con el prefijo "May ". "May Actron 400 x 10" no es otro producto,
# es el mismo Actron a precio mayorista.
PREFIJO_MAYORISTA = re.compile(r"^\s*may[\s.]+", re.IGNORECASE)
# AgilPedido muestra el «Codigo» del producto ADELANTE del nombre, asi que
# puede venir en el renglon: «- 2 x 2512690120 Duracell AAA · Bulto x 120 u.»,
# «[251269] ...», «251269 - ...». Se saca antes de leer el nombre. Cuatro
# digitos o mas (ningun nombre del catalogo empieza asi); entre corchetes o
# parentesis, cualquier largo.
CODIGO_ADELANTE = re.compile(
    r"^\s*(?:\[\s*(?P<c1>\d+)\s*\]|\(\s*(?P<c2>\d+)\s*\)|#?(?P<c3>\d{4,})(?=[\s\-–—·:|.]))"
    r"\s*(?:[\-–—·:|.]\s*)?")
# El nombre que el exportador tuvo que acortar para los 50 caracteres de
# AgilPedido termina en «…» (o «...»): se busca el unico producto que empieza asi.
ACORTADO = re.compile(r"\s*(?:…|\.\.\.)\s*$")


def normalizar(texto):
    """Sin acentos, sin mayusculas y sin puntuacion, para poder comparar."""
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


# ---------------------------------------------------------------------------
# El envase en el nombre. La MISMA regla que la migracion del catalogo
# (migracion-catalogo/unidades_de_venta.py y su README, «Segunda parte»):
#
# - bulto/bultos/pack/caja/display Y una cantidad: el «x N» es la unidad de
#   venta («Bulto DURACELL AAA X 120U.» = «Duracell AAA» en «Bulto x 120»);
# - la cantidad puede ir adelante («Bulto x 30 u. Pitusas Chocolate 160 g.»)
#   o atras («... X 120U.», «Agua Alvura 500 cc. pack 12u.»);
# - «10 bultos» / «75 Bultos» adelante es la compra por cantidad, no un envase;
# - el «x N» sin palabra de envase es la presentacion («Actron 400 x 10»):
#   NO es un envase;
# - los cinco confirmados con la empresa el 22/09/2026, donde la palabra es
#   como viene el producto: «Bi Caja Regaliz ...» y «RED POINT ECO PACK 20».
ENVASE = re.compile(r"\b(bultos?|pack|caja|display)\b", re.IGNORECASE)
CANTIDAD_ENVASE = re.compile(r"x\s*(\d+)\s*(u\.?|un\.?|unid\.?|cartones?)?\s*$", re.IGNORECASE)
ENVASE_ADELANTE = re.compile(
    r"^\s*(bultos?|pack|caja|display)\s+x\s*(\d+)\s*(u\.?|un\.?|unid\.?)?\s+", re.IGNORECASE)
ENVASE_ATRAS = re.compile(r"\b(bultos?|pack|caja|display)\s*(\d+)\s*(u\.?|un\.?|unid\.?)\s*$",
                          re.IGNORECASE)
# La convencion del catalogo exportado para AgilPedido (exportar_agilpedido.py):
# «Duracell AAA · Bulto x 120 u.». La palabra puede ser cualquiera de las de
# envase, o «Cartón»: la unidad se elige por la cantidad, no por la palabra.
SUFIJO_EXPORTADO = re.compile(
    r"\s*·\s*(bultos?|pack|caja|display|cart[oó]n(?:es)?)\s*x\s*(\d+)\s*(?:u\.?|un\.?|unid\.?)?\s*$",
    re.IGNORECASE)
PREFIJO_POR_CANTIDAD = re.compile(r"^\s*\d+\s+(bultos?|pack)\s+", re.IGNORECASE)
RUIDO_ENVASE = re.compile(r"^\s*(bultos?|pack|caja|display)\s+", re.IGNORECASE)
ENVASES_QUE_NO_SON = ("bi caja regaliz", "red point eco pack 20")


def leer_envase(nombre):
    """{"base": nombre sin el envase, "contiene": N} si el nombre trae un
    envase con su cantidad; si no, None."""
    nombre = nombre or ""
    exportado = SUFIJO_EXPORTADO.search(nombre)
    if exportado:
        # el nombre que se exporta a AgilPedido: lo de antes del «·» es el
        # nombre exacto de Odoo, tal cual (aunque diga «Bulto» o «x 24»)
        base = nombre[:exportado.start()].strip()
        if int(exportado.group(2)) > 0 and normalizar(base):
            return {"base": base, "contiene": int(exportado.group(2))}
        return None
    clave = normalizar(nombre)
    if (not ENVASE.search(nombre) or PREFIJO_POR_CANTIDAD.match(nombre)
            or clave.startswith(ENVASES_QUE_NO_SON)):
        return None
    for patron in (ENVASE_ADELANTE, CANTIDAD_ENVASE, ENVASE_ATRAS):
        encontrado = patron.search(nombre)
        if encontrado:
            contiene = int(encontrado.group(2) if patron is not CANTIDAD_ENVASE
                           else encontrado.group(1))
            base = nombre[:encontrado.start()] + nombre[encontrado.end():]
            base = re.sub(r"\s{2,}", " ", RUIDO_ENVASE.sub("", base)).strip(" .-")
            if contiene > 0 and normalizar(base):
                return {"base": base, "contiene": contiene}
    return None


def palabra_envase(unidad):
    """«Bulto x 120» -> «Bulto», «Cartón x 25» -> «Cartón»."""
    palabra = (unidad.name or "").split(" ")[0]
    return palabra if re.match(r"(?i)^(bultos?|pack|caja|display|cart[oó]n)$", palabra) else "Bulto"


LARGO_NOMBRE_AGILPEDIDO = 50

# ---------------------------------------------------------------------------
# El «Codigo» de AgilPedido. Es numerico, es la clave con que AgilPedido
# actualiza o crea el producto al importar el Excel, y el mensaje lo manda
# adelante del nombre: «- 1 x 251269 - Duracell AAA · Bulto x 120 u. (...)».
#
#   n                          la fila principal del producto: n es el numero
#                              del codigo interno «AP-n» (5 o 6 cifras hoy:
#                              39055 a 252286); sin «AP-», 9000000 + el id de
#                              la plantilla. La unidad es su unidad de venta
#                              principal (el bulto, si solo va por bulto).
#   n * 10000 + N              otra unidad de venta del mismo producto, de N
#                              unidades sueltas (1 = suelto, 12 = «Bulto x 12»)
#   n * 10000 + 5000 + N       la fila «May» (precio Mayorista) en esa unidad
#
# Las filas n * 10000 + ... tienen 9 cifras o mas y las principales menos de
# 9: no se pisan y se leen de vuelta sin ambiguedad mientras n tenga entre 5
# y 8 cifras (10.000 <= n < 10^8). Hoy los «AP-n» van de 39055 a 252286 y
# los sin «AP-» empiezan en 9000000.
CODIGO_EXTENDIDO = 10 ** 8
CODIGO_MAYORISTA = 5000
SIN_CODIGO_AP = 9000000


def numero_agilpedido(plantilla):
    """n: el numero del «AP-n», o 9000000 + el id si no tiene."""
    codigo = (plantilla.product_variant_id.default_code or "").strip()
    encontrado = re.match(r"^AP-(\d+)$", codigo)
    return int(encontrado.group(1)) if encontrado else SIN_CODIGO_AP + plantilla.id


def codigo_agilpedido(plantilla, unidad=None, mayorista=False):
    """El «Codigo» de la fila del producto en esa unidad (ver arriba)."""
    n = numero_agilpedido(plantilla)
    principal = unidad_de_venta(plantilla)[0]
    if not mayorista and (not unidad or unidad == principal):
        return n
    contiene = int(round((unidad or principal).relative_factor or 1.0))
    return n * 10000 + (CODIGO_MAYORISTA if mayorista else 0) + contiene


def leer_codigo(codigo):
    """«251269» -> (251269, False, None); «2512695120» -> (251269, True, 120)."""
    numero = int(codigo)
    if numero < CODIGO_EXTENDIDO:
        return numero, False, None
    n, resto = divmod(numero, 10000)
    return n, resto >= CODIGO_MAYORISTA, (resto % CODIGO_MAYORISTA) or None


def producto_por_codigo(env, codigo):
    """La variante del codigo de AgilPedido, aunque este archivada."""
    n, _mayorista, _contiene = leer_codigo(codigo)
    Producto = env["product.product"].sudo().with_context(active_test=False)
    if SIN_CODIGO_AP < n < CODIGO_EXTENDIDO:
        return Producto.search([("product_tmpl_id", "=", n - SIN_CODIGO_AP)], limit=1)
    return Producto.search([("default_code", "=", "AP-%s" % n)], limit=1)


def mismo_nombre(nombre, nombre_odoo):
    """El nombre del mensaje es el de Odoo (o su version acortada con «…»)."""
    acortado = ACORTADO.search(nombre or "")
    buscado = normalizar(nombre[:acortado.start()] if acortado else nombre)
    clave = normalizar(nombre_odoo)
    return bool(buscado) and (clave.startswith(buscado) if acortado else clave == buscado)


def identificar_por_codigo(env, renglon):
    """Lo primero que se mira: el codigo de AgilPedido adelante del nombre.
    None si el renglon no trae codigo o el codigo no es de ningun producto.
    Si no, {variante, unidad, coincide, es_mayorista, vendible, nombre_distinto}:

    - la unidad sale del « · Bulto x N u.» del nombre, o del codigo, o es la
      principal del producto; `coincide` es False si el producto no se vende
      en esa unidad;
    - «May» en el nombre (o el codigo de la fila «May») es precio Mayorista,
      salvo que el producto se llame asi;
    - `vendible` es False si el producto esta archivado o no se vende;
    - `nombre_distinto`: el nombre del mensaje no es el de Odoo (manda el
      codigo, pero se avisa)."""
    if not renglon.get("codigo"):
        return None
    variante = producto_por_codigo(env, renglon["codigo"])
    if not variante:
        return None
    plantilla = variante.product_tmpl_id
    _n, mayorista_codigo, contiene_codigo = leer_codigo(renglon["codigo"])
    envase = leer_envase(renglon["nombre_limpio"])
    nombre = envase["base"] if envase else renglon["nombre_limpio"]
    es_mayorista = mayorista_codigo or renglon["es_mayorista"]
    if renglon["es_mayorista"] and not mayorista_codigo:
        entero = leer_envase(renglon["nombre"])
        entero = entero["base"] if entero else renglon["nombre"]
        if mismo_nombre(entero, plantilla.name):
            # «May Pañuelitos Elite Bulto 35»: el «May» es parte del nombre
            es_mayorista, nombre = False, entero
    unidad, coincide = unidad_de_venta(plantilla, (envase and envase["contiene"]) or contiene_codigo)
    return {
        "variante": variante, "unidad": unidad, "coincide": coincide,
        "es_mayorista": es_mayorista,
        "vendible": bool(variante.active and plantilla.active and plantilla.sale_ok),
        "nombre_distinto": not mismo_nombre(nombre, plantilla.name),
    }


def nombre_agilpedido(plantilla, unidad, prefijo=""):
    """El nombre con que el producto va a AgilPedido: `prefijo` («May »), el
    de Odoo tal cual, y si se vende por envase, « · Bulto x 120 u.». El
    importador lo lee de vuelta con leer_envase.

    AgilPedido acepta 50 caracteres: si no entra, se acorta el nombre de Odoo
    (nunca el envase) y se marca con «…»; el importador busca el unico
    producto que empieza asi. Devuelve (nombre, acortado)."""
    contiene = round(unidad.relative_factor or 1.0, 4) if unidad else 1.0
    sufijo = ""
    if unidad and contiene > 1:
        sufijo = " · %s x %s u." % (palabra_envase(unidad), ("%g" % contiene).replace(".", ","))
    nombre = "%s%s%s" % (prefijo, plantilla.name, sufijo)
    if len(nombre) <= LARGO_NOMBRE_AGILPEDIDO:
        return nombre, False
    lugar = LARGO_NOMBRE_AGILPEDIDO - len(prefijo) - len(sufijo) - 1
    base = plantilla.name[:lugar].rstrip(" .,-·")
    return "%s%s…%s" % (prefijo, base, sufijo), True


def identificar(renglon, catalogo):
    """El producto de un renglon del mensaje, sin equivalencias. Devuelve
    {producto, parecido, envase, por_envase, es_mayorista, repetidos}:

    1. el nombre exacto (sin «May»);
    2. el envase del nombre (« · Bulto x 120 u.» del catalogo exportado, o la
       regla de la migracion) con el nombre base exacto;
    3. si el nombre empieza con «May» y hay un producto que se llama asi
       («May Pañuelitos Elite Bulto 35»), ese, a precio comun;
    4. el mas parecido, que se marca para corregir.

    Si hay varios productos con el mismo nombre (el suelto y el del bulto,
    «Pitusas Black x 130g.» y «Pitusas Black x 130g»), decide la unidad: el
    que tiene el envase del nombre, o sin envase el que se vende suelto.
    `repetidos` > 1 es que siguio habiendo mas de uno."""
    candidatos = [(renglon["nombre_limpio"], renglon["es_mayorista"])]
    if renglon["es_mayorista"]:
        candidatos.append((renglon["nombre"], False))
    for nombre, es_mayorista in candidatos:
        envase = leer_envase(nombre)
        contiene = envase and envase["contiene"]
        for buscado, por_envase in ((nombre, False), (envase and envase["base"], True)):
            iguales = exactos(buscado, catalogo) if buscado else []
            if iguales:
                iguales = elegir_por_unidad(iguales, contiene)
                return {"producto": iguales[0], "parecido": 1.0, "envase": envase,
                        "por_envase": por_envase, "es_mayorista": es_mayorista,
                        "repetidos": len(iguales)}
    producto, parecido = buscar_producto(renglon["nombre_limpio"], catalogo)
    return {"producto": producto, "parecido": parecido,
            "envase": leer_envase(renglon["nombre_limpio"]), "por_envase": False,
            "es_mayorista": renglon["es_mayorista"], "repetidos": 1}


def exactos(nombre, catalogo):
    """Los productos con ese nombre exacto (normalizado). Si el nombre viene
    acortado por el exportador («Galletitas Surtidas Pa…»), los que empiezan
    exactamente asi."""
    acortado = ACORTADO.search(nombre or "")
    buscado = normalizar(nombre[:acortado.start()] if acortado else nombre)
    if not buscado:
        return []
    if acortado:
        return [producto for producto, clave in catalogo.items() if clave.startswith(buscado)]
    return [producto for producto, clave in catalogo.items() if clave == buscado]


def elegir_por_unidad(productos, contiene=None):
    """Entre productos con el mismo nombre, el unico que se vende en la unidad
    del renglon: la del envase, o sin envase, la unidad suelta. Si no queda
    uno solo, todos (y el renglon se marca)."""
    if len(productos) <= 1:
        return productos
    if contiene:
        aptos = [p for p in productos if unidad_de_venta(p, contiene)[1]]
    else:
        aptos = [p for p in productos if p.uom_id in unidades_de_venta(p)]
    return aptos if len(aptos) == 1 else productos


def unidades_de_venta(plantilla):
    """Las unidades en que se vende el producto. Si declara unidades de venta
    propias son esas y nada mas (la cerveza que solo va por bulto no se
    pide por lata); si no, la de stock. Igual que la pagina."""
    return plantilla.uom_ids or plantilla.uom_id


def unidad_de_venta(plantilla, contiene=None):
    """(unidad, coincide) para el renglon. Con `contiene` (el envase del
    nombre), la unidad del producto con esa cantidad; si no tiene ninguna,
    coincide es False. Sin `contiene`, la primera unidad de venta, como la
    pagina (controllers/main.py, _unidad_de_venta)."""
    unidades = unidades_de_venta(plantilla)
    if contiene:
        for unidad in unidades:
            if round(unidad.relative_factor or 1.0, 4) == contiene:
                return unidad, True
        return unidades[:1], False
    return unidades[:1], True


def precio_en_unidad(lista, variante, cantidad, unidad=None):
    """El precio de Odoo por UNA unidad del renglon (el bulto, si es por
    bulto). Como la pagina, la lista se consulta por unidad suelta y se
    multiplica por lo que trae el envase; la cantidad que se consulta es la
    del renglon en unidades sueltas, para que apliquen las reglas por
    cantidad («10 bultos»).

    El precio del bulto se redondea al centavo: la migracion del catalogo
    guardo el precio por unidad como precio del bulto / N con 4 decimales
    (Duracell AAA: 130.000 / 120 = 1.083,3333), y sin redondear 6 bultos
    darian $ 779.999,98 en lugar de $ 780.000."""
    precio = lista._get_product_price(variante, cantidad or 1.0)
    if unidad and variante.uom_id and unidad != variante.uom_id:
        contiene = unidad._compute_quantity(1.0, variante.uom_id, round=False)
        moneda = lista.currency_id or variante.currency_id
        precio = moneda.round(
            lista._get_product_price(variante, (cantidad or 1.0) * contiene) * contiene)
    return precio


# ---------------------------------------------------------------------------
# Funciones compartidas: las usan el asistente de pegar el mensaje (abajo) y
# la carga automatica de los WhatsApp de AgilPedido (agilpedido_wa.py).

def importe_ar(texto):
    """'$ 60.680,00' o '$ 60,680.00' -> 60680.0. Sin numero devuelve None.

    AgilPedido manda los importes en formato ingles ('$ 1,339,590.00'),
    pero un mensaje escrito o corregido a mano puede venir en formato
    argentino ('$ 60.680,00'). Con los dos separadores, el ultimo es el
    decimal. Con uno solo, es decimal si aparece una vez y lo siguen uno o
    dos digitos ('1500.50', '1,5'); si no, es de miles ('1.500', '10,800').
    """
    crudo = re.sub(r"[^\d.,]", "", texto or "").strip(".,")
    if not re.search(r"\d", crudo):
        return None
    if "," in crudo and "." in crudo:
        decimal = "," if crudo.rfind(",") > crudo.rfind(".") else "."
        miles = "." if decimal == "," else ","
        crudo = crudo.replace(miles, "").replace(decimal, ".")
    elif "," in crudo or "." in crudo:
        separador = "," if "," in crudo else "."
        if crudo.count(separador) == 1 and len(crudo.rsplit(separador, 1)[1]) in (1, 2):
            crudo = crudo.replace(separador, ".")
        else:
            crudo = crudo.replace(separador, "")
    try:
        return float(crudo)
    except ValueError:
        return None


def leer_renglon(linea):
    """Un renglon de producto del mensaje, o None si la linea no lo es."""
    encontrado = RENGLON.match(linea or "")
    if not encontrado:
        return None
    cantidad, nombre, importe = encontrado.groups()
    codigo = CODIGO_ADELANTE.match(nombre)
    if codigo and nombre[codigo.end():].strip():
        nombre = nombre[codigo.end():]
    es_mayorista = bool(PREFIJO_MAYORISTA.match(nombre))
    return {
        "texto": (linea or "").strip(),
        "cantidad": float(cantidad.replace(",", ".")),
        "codigo": codigo and (codigo.group("c1") or codigo.group("c2") or codigo.group("c3")),
        "nombre": nombre,
        "nombre_limpio": PREFIJO_MAYORISTA.sub("", nombre) if es_mayorista else nombre,
        "importe": importe.strip(),
        "es_mayorista": es_mayorista,
    }


def catalogo_normalizado(env):
    productos = env["product.template"].search([("sale_ok", "=", True)])
    return {p: normalizar(p.name) for p in productos}


def buscar_producto(nombre, catalogo):
    """El producto que mas se parece, con cuanto se parece."""
    buscado = normalizar(nombre)
    if not buscado:
        return False, 0.0
    for producto, clave in catalogo.items():
        if clave == buscado:
            return producto, 1.0
    mejor, puntaje = False, 0.0
    for producto, clave in catalogo.items():
        comparador = difflib.SequenceMatcher(None, buscado, clave)
        # las cotas rapidas nunca son menores que ratio(): si no superan al
        # mejor, el resultado es el mismo sin calcular la comparacion entera
        if comparador.real_quick_ratio() <= puntaje or comparador.quick_ratio() <= puntaje:
            continue
        p = comparador.ratio()
        if p > puntaje:
            mejor, puntaje = producto, p
    return mejor, puntaje


def lista_mayorista(env):
    """The hidden list with the «May» prices (named Distribuidoras since
    29/09/2026; it was «Mayorista»)."""
    return lista_oculta(env)


def precio_renglon(plantilla, cantidad, es_mayorista, mayorista, unidad=None):
    """Lo que el precio agrega a los valores del renglon, con las reglas de
    «May». Devuelve (valores, sin_regla): sin_regla es un «May» cuyo producto
    no tiene precio mayorista cargado en Odoo. Con `unidad` (un bulto), el
    precio es el de esa unidad."""
    if not es_mayorista:
        return {}, False
    if plantilla.categ_id.name in CATEGORIAS_A_PRESUPUESTAR:
        # cigarrillos y tabaco mayoristas: no hay precio publico, el
        # gerente los valoriza al asignarle la lista al cliente
        return {"price_unit": 0.0}, False
    if not mayorista:
        return {}, False
    # el precio sale de la lista Distribuidoras, aunque el resto del pedido se
    # valorice con otra
    precio = precio_en_unidad(mayorista, plantilla.product_variant_id, cantidad, unidad)
    sin_regla = not mayorista.item_ids.filtered(lambda i: i.product_tmpl_id == plantilla)
    return {"price_unit": precio}, sin_regla


class ImportarPedido(models.TransientModel):
    _name = "distri.importar.pedido"
    _description = "Cargar un pedido de AgilPedido pegando el mensaje"

    mensaje = fields.Text(
        string="Mensaje de WhatsApp",
        help="Pegá el mensaje tal como llegó, sin editarlo.",
    )
    linea_ids = fields.One2many(
        "distri.importar.pedido.linea", "importacion_id", string="Renglones",
    )
    partner_id = fields.Many2one("res.partner", string="Cliente")
    pricelist_id = fields.Many2one("product.pricelist", string="Lista de precios")
    resumen = fields.Char(string="Resumen", readonly=True)

    # ----------------------------------------------------------- analisis

    def _catalogo_normalizado(self):
        return catalogo_normalizado(self.env)

    def _buscar_producto(self, nombre, catalogo):
        """El producto que mas se parece, con cuanto se parece."""
        return buscar_producto(nombre, catalogo)

    def action_analizar(self):
        self.ensure_one()
        if not self.mensaje:
            raise UserError(_("Pegá primero el mensaje."))
        catalogo = self._catalogo_normalizado()
        Equivalencia = self.env["distri.agil.equivalencia"].sudo()
        renglones, sueltas = [], 0
        for linea in self.mensaje.splitlines():
            renglon = leer_renglon(linea)
            if not renglon:
                if linea.strip():
                    sueltas += 1
                continue
            valores = {
                "cantidad": renglon["cantidad"],
                "nombre_mensaje": renglon["nombre"],
                "importe_mensaje": renglon["importe"],
                "es_mayorista": renglon["es_mayorista"],
            }
            # primero el codigo de AgilPedido adelante del nombre
            por_codigo = identificar_por_codigo(self.env, renglon)
            if por_codigo and por_codigo["vendible"] and por_codigo["coincide"]:
                valores.update({
                    "product_tmpl_id": por_codigo["variante"].product_tmpl_id.id,
                    "product_uom_id": por_codigo["unidad"].id,
                    "es_mayorista": por_codigo["es_mayorista"],
                    "parecido": 100,
                })
                renglones.append((0, 0, valores))
                continue
            # despues, lo que el personal ya enseñó (agil_equivalencia.py)
            equivalencia = Equivalencia._distri_buscar(renglon["nombre_limpio"])
            if equivalencia and equivalencia._distri_vendible():
                valores.update({
                    "cantidad": renglon["cantidad"] * equivalencia.factor,
                    "product_tmpl_id": equivalencia.product_id.product_tmpl_id.id,
                    "product_uom_id": equivalencia.product_uom_id.id,
                    "parecido": 100,
                    "equivalencia_id": equivalencia.id,
                })
                renglones.append((0, 0, valores))
                continue
            identificado = identificar(renglon, catalogo)
            producto, parecido = identificado["producto"], identificado["parecido"]
            envase = identificado["envase"]
            valores["es_mayorista"] = identificado["es_mayorista"]
            unidad = False
            if producto and parecido >= PARECIDO_MINIMO:
                unidad, coincide = unidad_de_venta(producto, envase and envase["contiene"])
                if not coincide:
                    # el envase del nombre no es ninguna unidad del producto
                    parecido = min(parecido, PARECIDO_MINIMO - 0.01)
            valores.update({
                "product_tmpl_id": producto.id if producto and parecido >= PARECIDO_MINIMO else False,
                "product_uom_id": unidad.id if unidad and parecido >= PARECIDO_MINIMO else False,
                "parecido": round(parecido * 100),
            })
            renglones.append((0, 0, valores))
        if not renglones:
            raise UserError(_(
                "No encontré ningún renglón de producto. Tienen que venir "
                "con la forma «- 2 x Nombre del producto ($ 1.000,00)»."
            ))
        self.linea_ids.unlink()
        self.linea_ids = renglones
        dudosos = len(self.linea_ids.filtered(lambda l: not l.product_tmpl_id))
        mayoristas = len(self.linea_ids.filtered("es_mayorista"))
        aprendidos = len(self.linea_ids.filtered("equivalencia_id"))
        self.resumen = _(
            "%(total)s renglones leídos, %(dudosos)s sin identificar, "
            "%(may)s a precio mayorista.",
            total=len(self.linea_ids), dudosos=dudosos, may=mayoristas)
        if aprendidos:
            self.resumen += _(" %s por equivalencia aprendida.") % aprendidos
        return self._volver()

    # ------------------------------------------------------------ alta

    def action_crear_presupuesto(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError(_("Elegí el cliente antes de crear el presupuesto."))
        sin_identificar = self.linea_ids.filtered(lambda l: not l.product_tmpl_id)
        if sin_identificar:
            raise UserError(_(
                "Hay %s renglones sin producto asignado. Elegilos a mano o "
                "borralos antes de crear el presupuesto."
            ) % len(sin_identificar))

        mayorista = lista_mayorista(self.env)
        lineas, sin_regla = [], []
        for linea in self.linea_ids:
            variante = linea.product_tmpl_id.product_variant_id
            if not variante:
                continue
            unidad = linea.product_uom_id
            if unidad not in unidades_de_venta(linea.product_tmpl_id):
                unidad = unidad_de_venta(linea.product_tmpl_id)[0]
            valores = {
                "product_id": variante.id,
                "product_uom_qty": linea.cantidad,
                "product_uom_id": unidad.id,
            }
            precio, falta_regla = precio_renglon(
                linea.product_tmpl_id, linea.cantidad, linea.es_mayorista, mayorista, unidad)
            lista = self.pricelist_id or self.partner_id.property_product_pricelist
            if not precio and lista and unidad != variante.uom_id:
                # por bulto: el precio del bulto de la lista, como la página
                precio = {"price_unit": precio_en_unidad(lista, variante, linea.cantidad, unidad)}
            valores.update(precio)
            if falta_regla:
                sin_regla.append(linea.product_tmpl_id.name)
            lineas.append((0, 0, valores))
        if sin_regla:
            raise UserError(_(
                "Estos productos vinieron con «May» pero no tienen precio "
                "mayorista cargado en Odoo. Cargáselo primero:\n\n- %s"
            ) % "\n- ".join(sin_regla))
        valores = {
            "partner_id": self.partner_id.id,
            "order_line": lineas,
            "note": "PEDIDO CARGADO DESDE AGILPEDIDO\n\n%s" % (self.mensaje or ""),
        }
        if self.pricelist_id:
            valores["pricelist_id"] = self.pricelist_id.id
        pedido = self.env["sale.order"].create(valores)
        self.linea_ids.filtered(
            lambda l: l.equivalencia_id
            and l.equivalencia_id.product_id.product_tmpl_id == l.product_tmpl_id
        ).equivalencia_id._distri_registrar_uso()
        return {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "res_id": pedido.id,
            "view_mode": "form",
        }

    def _volver(self):
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
        }


class ImportarPedidoLinea(models.TransientModel):
    _name = "distri.importar.pedido.linea"
    _description = "Renglón leído del mensaje de AgilPedido"
    _order = "id"

    importacion_id = fields.Many2one("distri.importar.pedido", required=True, ondelete="cascade")
    cantidad = fields.Float(string="Cantidad", default=1.0)
    nombre_mensaje = fields.Char(string="Como vino en el mensaje", readonly=True)
    importe_mensaje = fields.Char(string="Importe del mensaje", readonly=True)
    product_tmpl_id = fields.Many2one("product.template", string="Producto")
    parecido = fields.Integer(string="Coincidencia %", readonly=True)
    es_mayorista = fields.Boolean(
        string="«May»",
        help="Vino con el prefijo «May» en AgilPedido: se valoriza con la lista Distribuidoras.",
    )
    product_uom_id = fields.Many2one(
        "uom.uom", string="Unidad",
        domain="[('id', 'in', unidades_ids)]",
        help="La unidad de venta del producto: si se vende por bulto, la cantidad es de bultos.",
    )
    unidades_ids = fields.Many2many("uom.uom", compute="_compute_unidades_ids")
    equivalencia_id = fields.Many2one(
        "distri.agil.equivalencia", string="Equivalencia", readonly=True,
        help="El producto salió de una equivalencia aprendida al corregir otro pedido.",
    )

    @api.depends("product_tmpl_id")
    def _compute_unidades_ids(self):
        for linea in self:
            linea.unidades_ids = unidades_de_venta(linea.product_tmpl_id) if linea.product_tmpl_id \
                else self.env["uom.uom"]

    @api.onchange("product_tmpl_id")
    def _onchange_product_tmpl_id_unidad(self):
        for linea in self:
            if linea.product_tmpl_id and linea.product_uom_id not in unidades_de_venta(
                    linea.product_tmpl_id):
                linea.product_uom_id = unidad_de_venta(linea.product_tmpl_id)[0]

    @api.depends("product_tmpl_id")
    def _compute_display_name(self):
        for linea in self:
            linea.display_name = linea.nombre_mensaje or ""
