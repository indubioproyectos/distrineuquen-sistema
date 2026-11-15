"""Pure helpers of the TEMPORARY AgilPedido catalog (19.0.1.89.0).

No Odoo models here on purpose: the model overrides of this module build in
import order, so the parsing lives apart and models/catalogo_agil.py, the
controller and the importers import it.

The public storefront (https://agilpedido.com/<shop>) embeds its data in the
HTML as `serverExports.<key>=<json>;`. Only the visible products are there:
a product hidden in AgilPedido is simply missing from `productos`. The price
comes in CENTAVOS. Sections are shown by `posicion`; inside a section the
products keep the order of the feed.
"""
import json
import re
import unicodedata
from urllib.parse import urlsplit

# '1': /pedido shows only the temporary AgilPedido products; '0' (default):
# the usual catalog, without them.
PARAM_CATALOGO_AGIL = "distri_pedido.catalogo_agil"
PARAM_AGIL_URL = "distri_pedido.agil_url"
AGIL_URL = "https://agilpedido.com/distrineuquen"
AGIL_TIMEOUT = 30
# Never wipe the catalog with a broken fetch: below this share of the temporary
# products visible today, the sync aborts.
MINIMO_PROPORCION = 0.5
SIN_SECCION = "Otros"
# /pedido and the backend order the temporary catalog like the storefront
ORDEN_AGIL = "distri_agil_seccion_orden, distri_agil_seccion, distri_agil_orden, id"

# product photos: one download at a time is capped in time and size
IMAGEN_TIMEOUT = 20
IMAGEN_MAXIMO = 10 * 1024 * 1024
IMAGEN_HILOS = 4

SERVER_EXPORT = re.compile(r"serverExports\.(\w+)\s*=\s*")


class FeedInvalido(ValueError):
    """The storefront could not be read: nothing is changed."""


def catalogo_agil_activo(env):
    """True when /pedido shows the temporary AgilPedido catalog."""
    valor = env["ir.config_parameter"].sudo().get_param(PARAM_CATALOGO_AGIL) or "0"
    return valor.strip() == "1"


def clave(texto):
    """Without accents, emojis, case or punctuation: «Billiken 😄» = «Billiken»."""
    texto = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", " ", texto.lower()).strip()


def imagen_de(producto):
    """(url, key) of a storefront product photo, or ("", "") without one.

    `imagen_url` is the full photo, a signed object-storage link whose query
    (date, signature) changes when AgilPedido re-signs it; the key is the link
    without the query, so the same photo is not downloaded again."""
    for campo in ("imagen_url", "imagen_small_url"):
        url = producto.get(campo)
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            partes = urlsplit(url)
            return url, "%s://%s%s" % (partes.scheme, partes.netloc, partes.path)
    return "", ""


def leer_feed(contenido):
    """{"secciones": [...], "productos": [...]} from the storefront HTML or
    from a JSON (the serverExports object, as saved for offline tests).
    Raises FeedInvalido with a clear message when it cannot be read."""
    if isinstance(contenido, bytes):
        contenido = contenido.decode("utf-8", errors="replace")
    texto = (contenido or "").strip()
    if not texto:
        raise FeedInvalido("La página de AgilPedido llegó vacía.")
    if texto[:1] in "{[":
        try:
            datos = json.loads(texto)
        except ValueError as exc:
            raise FeedInvalido("El archivo no es un JSON válido (%s)." % exc) from exc
        if isinstance(datos, dict) and isinstance(datos.get("serverExports"), dict):
            datos = datos["serverExports"]
    else:
        datos = {}
        decodificador = json.JSONDecoder()
        for encontrado in SERVER_EXPORT.finditer(texto):
            try:
                valor, _fin = decodificador.raw_decode(texto, encontrado.end())
            except ValueError:
                continue
            datos[encontrado.group(1)] = valor
    if not isinstance(datos, dict) or not isinstance(datos.get("productos"), list):
        raise FeedInvalido(
            "No se encontró la lista de productos en la página de AgilPedido: puede haber "
            "cambiado su formato. No se tocó el catálogo.")
    secciones = datos.get("secciones")
    return {"secciones": secciones if isinstance(secciones, list) else [],
            "productos": datos["productos"]}


def filas_agil(feed):
    """(rows, rejected) of the feed, as Odoo stores them.

    Each row: agil_id (text), nombre (exact), precio (pesos), seccion (name
    without the trailing spaces AgilPedido keeps), seccion_orden and orden.
    A product without id, name or a valid price is rejected with its reason,
    never guessed."""
    secciones = {}
    for indice, seccion in enumerate(feed.get("secciones") or []):
        if not isinstance(seccion, dict) or seccion.get("id") is None:
            continue
        try:
            posicion = int(seccion.get("posicion"))
        except (TypeError, ValueError):
            posicion = 1000 + indice
        nombre = (seccion.get("nombre") or "").strip() or SIN_SECCION
        secciones[str(seccion["id"])] = (nombre, posicion)

    filas, rechazados, vistos = [], [], set()
    for orden, producto in enumerate(feed.get("productos") or []):
        if not isinstance(producto, dict):
            rechazados.append(("?", "renglón ilegible"))
            continue
        agil_id = str(producto.get("id") or "").strip()
        nombre = producto.get("nombre") if isinstance(producto.get("nombre"), str) else ""
        if not agil_id or not nombre.strip():
            rechazados.append((agil_id or "?", "sin id o sin nombre"))
            continue
        if agil_id in vistos:
            rechazados.append((agil_id, "id repetido en la página"))
            continue
        precio = producto.get("precio")
        if isinstance(precio, bool) or not isinstance(precio, (int, float)) or precio < 0:
            rechazados.append((agil_id, "precio inválido: %r" % (precio,)))
            continue
        vistos.add(agil_id)
        imagen_url, imagen_clave = imagen_de(producto)
        seccion, seccion_orden = secciones.get(str(producto.get("seccion_id")),
                                               (SIN_SECCION, 9999))
        filas.append({
            "agil_id": agil_id,
            "nombre": nombre,
            "precio": round(precio / 100.0, 2),
            "seccion": seccion,
            "seccion_orden": seccion_orden,
            "orden": orden,
            "imagen_url": imagen_url,
            "imagen_clave": imagen_clave,
        })
    return filas, rechazados


def motivo_para_abortar(cantidad_feed, visibles_hoy):
    """Why the sync must not apply this feed, or "" when it can."""
    if cantidad_feed <= 0:
        return ("AgilPedido no devolvió ningún producto: no se tocó el catálogo. "
                "Reintentá más tarde.")
    if visibles_hoy and cantidad_feed < visibles_hoy * MINIMO_PROPORCION:
        return ("AgilPedido devolvió %s productos y hoy hay %s visibles: menos de la mitad, "
                "puede ser una falla de la página. No se tocó el catálogo."
                % (cantidad_feed, visibles_hoy))
    return ""
