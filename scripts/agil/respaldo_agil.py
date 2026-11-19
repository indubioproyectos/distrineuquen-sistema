"""Daily backup of the public AgilPedido storefront (helper of respaldo-agil.sh).

Run with `python3 -I`; standard library only. It reads the public page
https://agilpedido.com/distrineuquen (no credentials) and writes, under the
backup root (default /srv/odoo-lab/backups-agil):

    YYYY-MM-DD/tienda.html      the raw page
    YYYY-MM-DD/datos.json       every `serverExports.<key>=<json>;` object
    YYYY-MM-DD/productos.xlsx   AgilPedido's product IMPORT template columns
    YYYY-MM-DD/productos.csv    product -> section and photo file
    YYYY-MM-DD/secciones.csv    sections (name, description, order, photo)
    YYYY-MM-DD/resumen.txt      counts and the diff against the previous backup
    fotos/<sha256>.<ext>        deduplicated photo store shared by every day
    fotos/indice.json           photo link (without the signed query) -> file

Only VISIBLE products are on the public page: hidden ones are not saved.

Safety: when the page cannot be read or parsed, or it has no products, or
fewer than half of the previous backup's, nothing is overwritten. The run
writes YYYY-MM-DD-FALLA/ (raw page + ALERTA.txt), appends a line to
ALERTA.txt in the root and exits 2.

Environment (for tests):
    AGIL_URL      storefront URL (default the DistriNeuquén one)
    AGIL_FUENTE   read the page from this local file instead of downloading
    AGIL_DESTINO  backup root (default /srv/odoo-lab/backups-agil)
"""
import csv
import datetime
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import zipfile
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urlsplit
from xml.sax.saxutils import escape

URL = os.environ.get("AGIL_URL") or "https://agilpedido.com/distrineuquen"
FUENTE = os.environ.get("AGIL_FUENTE") or ""
RAIZ = os.environ.get("AGIL_DESTINO") or "/srv/odoo-lab/backups-agil"
FOTOS = os.path.join(RAIZ, "fotos")
INDICE = os.path.join(FOTOS, "indice.json")

USER_AGENT = "Mozilla/5.0 (compatible; distri-respaldo/1.0)"
PAGINA_TIMEOUT = 60
FOTO_TIMEOUT = 30
FOTO_MAXIMO = 15 * 1024 * 1024
FOTO_HILOS = 4          # polite: at most 4 downloads at a time
REINTENTOS = 2          # retries after the first attempt
MINIMO_PROPORCION = 0.5  # below this share of the previous backup: alert
CONSERVAR = 90          # daily folders kept
MOSTRAR = 30            # names listed per diff group

# AgilPedido's product import template, in this order and with these names
# (same as distri_pedido/models/exportar_agilpedido.py COLUMNAS).
COLUMNAS = ["Codigo", "Nombre", "Descripción", "Visible", "Precio",
            "Precio salon", "Destacado"]

SERVER_EXPORT = re.compile(r"serverExports\.(\w+)\s*=\s*")
DIA = re.compile(r"^\d{4}-\d{2}-\d{2}$")
FALLA = re.compile(r"^(\d{4}-\d{2}-\d{2})-FALLA(-\d+)?$")


class Falla(Exception):
    """The storefront could not be backed up: nothing is overwritten."""


def log(mensaje):
    print("%s %s" % (datetime.datetime.now().isoformat(timespec="seconds"), mensaje),
          flush=True)


def descargar(url, timeout, maximo=None):
    """Bytes of url, with REINTENTOS retries (2 s, 4 s apart)."""
    ultimo = None
    for intento in range(REINTENTOS + 1):
        if intento:
            time.sleep(2 * intento)
        try:
            pedido = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(pedido, timeout=timeout) as respuesta:
                datos = respuesta.read(maximo + 1) if maximo else respuesta.read()
            if maximo and len(datos) > maximo:
                raise ValueError("more than %s bytes" % maximo)
            return datos
        except Exception as exc:  # network, HTTP or size: retry
            ultimo = exc
    raise ultimo


def leer_server_exports(html):
    """{key: value} of every serverExports object in the page (same parsing
    as distri_pedido/models/agil_tienda.leer_feed)."""
    texto = html.decode("utf-8", errors="replace")
    datos, decodificador = {}, json.JSONDecoder()
    for encontrado in SERVER_EXPORT.finditer(texto):
        try:
            valor, _fin = decodificador.raw_decode(texto, encontrado.end())
        except ValueError:
            continue
        datos[encontrado.group(1)] = valor
    productos = datos.get("productos")
    if not isinstance(productos, list) or not all(isinstance(p, dict) for p in productos):
        raise Falla("no se encontró la lista de productos en la página "
                    "(¿cambió el formato de AgilPedido?)")
    if not isinstance(datos.get("secciones"), list):
        datos["secciones"] = []
    return datos


# --- folders ---------------------------------------------------------------

def dias_buenos():
    """Good daily folders (with datos.json), oldest first."""
    if not os.path.isdir(RAIZ):
        return []
    return sorted(n for n in os.listdir(RAIZ)
                  if DIA.match(n) and os.path.isfile(os.path.join(RAIZ, n, "datos.json")))


def anterior(hoy):
    """(folder, serverExports) of the backup to compare with: the newest day
    before today, else today's earlier run; (None, None) when there is none."""
    dias = dias_buenos()
    previos = [d for d in dias if d < hoy] or [d for d in dias if d == hoy]
    for dia in reversed(previos):
        try:
            with open(os.path.join(RAIZ, dia, "datos.json"), encoding="utf-8") as f:
                datos = json.load(f)
            if isinstance(datos.get("productos"), list):
                return dia, datos
        except (OSError, ValueError):
            log("aviso: %s/datos.json ilegible, se ignora" % dia)
    return None, None


def escribir(ruta, contenido):
    modo = "wb" if isinstance(contenido, bytes) else "w"
    with open(ruta, modo, **({} if modo == "wb" else {"encoding": "utf-8"})) as f:
        f.write(contenido)


def registrar_falla(hoy, motivo, html):
    """YYYY-MM-DD-FALLA folder + a line in the root ALERTA.txt + syslog."""
    os.makedirs(RAIZ, exist_ok=True)
    nombre, n = "%s-FALLA" % hoy, 1
    while os.path.exists(os.path.join(RAIZ, nombre)):
        n += 1
        nombre = "%s-FALLA-%s" % (hoy, n)
    carpeta = os.path.join(RAIZ, nombre)
    os.makedirs(carpeta)
    if html:
        escribir(os.path.join(carpeta, "tienda.html"), html)
    ahora = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    escribir(os.path.join(carpeta, "ALERTA.txt"),
             "%s respaldo de AgilPedido FALLÓ\nMotivo: %s\nOrigen: %s\n"
             "No se sobrescribió ni borró ningún respaldo anterior.\n"
             % (ahora, motivo, FUENTE or URL))
    with open(os.path.join(RAIZ, "ALERTA.txt"), "a", encoding="utf-8") as f:
        f.write("%s respaldo de AgilPedido FALLÓ: %s (carpeta %s)\n" % (ahora, motivo, nombre))
    os.chmod(os.path.join(RAIZ, "ALERTA.txt"), 0o600)
    try:
        subprocess.run(["logger", "-p", "user.err", "-t", "respaldo-agil",
                        "respaldo de AgilPedido FALLÓ: %s" % motivo], timeout=10, check=False)
    except Exception:
        pass
    log("FALLA: %s -> %s" % (motivo, nombre))


# --- photos ----------------------------------------------------------------

def clave_foto(url):
    """The link without its signed query: the same photo keeps the same key
    when AgilPedido re-signs it (as agil_tienda.imagen_de)."""
    partes = urlsplit(url)
    return "%s://%s%s" % (partes.scheme, partes.netloc, partes.path)


def extension(datos):
    if datos.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if datos.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if datos[:4] == b"RIFF" and datos[8:12] == b"WEBP":
        return "webp"
    if datos[:6] in (b"GIF87a", b"GIF89a"):
        return "gif"
    if datos[4:12] in (b"ftypavif", b"ftypavis"):
        return "avif"
    return None


def guardar_foto(url):
    """File name in fotos/ of the photo at url (downloaded and stored by
    sha256 unless that file already exists)."""
    datos = descargar(url, FOTO_TIMEOUT, FOTO_MAXIMO)
    ext = extension(datos)
    if not ext:
        raise ValueError("not an image (%s bytes)" % len(datos))
    nombre = "%s.%s" % (hashlib.sha256(datos).hexdigest(), ext)
    ruta = os.path.join(FOTOS, nombre)
    if not os.path.exists(ruta):
        temporal = "%s.%s.part" % (ruta, os.getpid())
        escribir(temporal, datos)
        os.replace(temporal, ruta)
    return nombre


def url_foto(objeto, campos):
    for campo in campos:
        url = objeto.get(campo)
        if isinstance(url, str) and url.startswith(("https://", "http://")):
            return url
    return ""


def bajar_fotos(urls):
    """{url key: file name or ""} for every photo link; already known keys
    whose file exists are not downloaded again."""
    os.makedirs(FOTOS, exist_ok=True)
    try:
        with open(INDICE, encoding="utf-8") as f:
            indice = json.load(f)
        if not isinstance(indice, dict):
            indice = {}
    except (OSError, ValueError):
        indice = {}
    por_clave = {clave_foto(u): u for u in urls if u}
    resultado, faltan = {}, []
    for clave, url in por_clave.items():
        nombre = indice.get(clave)
        if nombre and os.path.isfile(os.path.join(FOTOS, nombre)):
            resultado[clave] = nombre
        else:
            faltan.append((clave, url))

    def una(par):
        clave, url = par
        try:
            return clave, guardar_foto(url), None
        except Exception as exc:
            return clave, "", "%s" % exc

    errores = []
    with ThreadPoolExecutor(max_workers=FOTO_HILOS) as grupo:
        for clave, nombre, error in grupo.map(una, faltan):
            resultado[clave] = nombre
            if nombre:
                indice[clave] = nombre
            else:
                errores.append((clave, error))
    temporal = INDICE + ".part"
    escribir(temporal, json.dumps(indice, indent=1, sort_keys=True))
    os.replace(temporal, INDICE)
    return resultado, len(faltan), errores


# --- outputs ---------------------------------------------------------------

def pesos(centavos):
    """The storefront price comes in CENTAVOS; the import template wants
    PESOS (exportar_agilpedido.py: CENTAVOS = 1, 130000.0 = $ 130.000)."""
    if isinstance(centavos, bool) or not isinstance(centavos, (int, float)):
        return ""
    valor = round(centavos / 100.0, 2)
    return int(valor) if valor == int(valor) else valor


def texto(valor):
    return valor if isinstance(valor, str) else ""


def _columna(indice):
    letras, indice = "", indice + 1
    while indice:
        indice, resto = divmod(indice - 1, 26)
        letras = chr(65 + resto) + letras
    return letras


def armar_xlsx(filas):
    """Minimal one-sheet xlsx, text and numbers only (same approach as
    exportar_agilpedido.armar_xlsx; openpyxl is not on this host)."""
    def celda(ref, valor):
        if isinstance(valor, (int, float)) and not isinstance(valor, bool):
            return '<c r="%s"><v>%s</v></c>' % (ref, valor)
        if not valor:
            return '<c r="%s"/>' % ref
        return ('<c r="%s" t="inlineStr"><is><t xml:space="preserve">%s</t></is></c>'
                % (ref, escape(str(valor))))

    cuerpo = []
    for n, fila in enumerate(filas, start=1):
        celdas = "".join(celda("%s%s" % (_columna(i), n), v) for i, v in enumerate(fila))
        cuerpo.append('<row r="%s">%s</row>' % (n, celdas))
    cabecera = '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    partes = {
        "[Content_Types].xml": cabecera +
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
        '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
        '<Default Extension="xml" ContentType="application/xml"/>'
        '<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>'
        '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
        '</Types>',
        "_rels/.rels": cabecera +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/>'
        '</Relationships>',
        "xl/workbook.xml": cabecera +
        '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
        'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
        '<sheets><sheet name="Productos" sheetId="1" r:id="rId1"/></sheets></workbook>',
        "xl/_rels/workbook.xml.rels": cabecera +
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
        '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/>'
        '</Relationships>',
        "xl/worksheets/sheet1.xml": cabecera +
        '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
        '<sheetData>%s</sheetData></worksheet>' % "".join(cuerpo),
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        for nombre, contenido in partes.items():
            z.writestr(nombre, contenido)
    return buffer.getvalue()


def escribir_csv(ruta, filas):
    """CSV for Excel in Spanish: «;» separator, UTF-8 with BOM."""
    with open(ruta, "w", encoding="utf-8-sig", newline="") as f:
        csv.writer(f, delimiter=";").writerows(filas)


def nombre_producto(p):
    return texto(p.get("nombre")).strip() or "(sin nombre, id %s)" % p.get("id")


def diferencias(previo, actual):
    """(added, removed, price changes) between two product lists, by id."""
    viejos = {str(p.get("id")): p for p in previo if isinstance(p, dict)}
    nuevos = {str(p.get("id")): p for p in actual}
    altas = [nombre_producto(p) for i, p in nuevos.items() if i not in viejos]
    bajas = [nombre_producto(p) for i, p in viejos.items() if i not in nuevos]
    precios = ["%s: $ %s -> $ %s" % (nombre_producto(p), pesos(viejos[i].get("precio")),
                                     pesos(p.get("precio")))
               for i, p in nuevos.items()
               if i in viejos and viejos[i].get("precio") != p.get("precio")]
    return altas, bajas, precios


def tamano(ruta):
    total = 0
    for base, _dirs, archivos in os.walk(ruta):
        for a in archivos:
            try:
                total += os.path.getsize(os.path.join(base, a))
            except OSError:
                pass
    return total


def mb(n):
    return "%.1f MB" % (n / 1048576.0)


# --- retention -------------------------------------------------------------

def podar(hoy):
    """Keep the newest CONSERVAR good days; drop FALLA folders older than
    CONSERVAR days and photos no kept day references."""
    dias = dias_buenos()
    for dia in dias[:-CONSERVAR]:
        shutil.rmtree(os.path.join(RAIZ, dia))
        log("retención: borrada %s" % dia)
    limite = (datetime.date.fromisoformat(hoy)
              - datetime.timedelta(days=CONSERVAR)).isoformat()
    for nombre in os.listdir(RAIZ):
        encontrado = FALLA.match(nombre)
        if encontrado and encontrado.group(1) < limite:
            shutil.rmtree(os.path.join(RAIZ, nombre))
            log("retención: borrada %s" % nombre)

    usadas = set()
    for dia in dias_buenos():
        for archivo in ("productos.csv", "secciones.csv"):
            try:
                with open(os.path.join(RAIZ, dia, archivo), encoding="utf-8-sig",
                          newline="") as f:
                    filas = list(csv.DictReader(f, delimiter=";"))
            except OSError:
                log("aviso: falta %s/%s; no se podan fotos" % (dia, archivo))
                return
            usadas.update(os.path.basename(r.get("Foto") or "") for r in filas)
    usadas.discard("")
    borradas = 0
    for nombre in os.listdir(FOTOS):
        if nombre != "indice.json" and nombre not in usadas:
            os.remove(os.path.join(FOTOS, nombre))
            borradas += 1
    if borradas:
        with open(INDICE, encoding="utf-8") as f:
            indice = json.load(f)
        indice = {k: v for k, v in indice.items() if v in usadas}
        escribir(INDICE + ".part", json.dumps(indice, indent=1, sort_keys=True))
        os.replace(INDICE + ".part", INDICE)
        log("retención: %s fotos sin uso borradas" % borradas)


# --- main ------------------------------------------------------------------

def respaldar(hoy, html):
    datos = leer_server_exports(html)
    productos, secciones = datos["productos"], datos["secciones"]
    dia_previo, previo = anterior(hoy)
    n_previo = len(previo["productos"]) if previo else 0
    if not productos:
        raise Falla("la página no trae ningún producto")
    if previo and len(productos) < MINIMO_PROPORCION * n_previo:
        raise Falla("la página trae %s productos, menos de la mitad que el respaldo %s (%s)"
                    % (len(productos), dia_previo, n_previo))

    temporal = os.path.join(RAIZ, ".%s.tmp" % hoy)
    shutil.rmtree(temporal, ignore_errors=True)
    os.makedirs(temporal)
    escribir(os.path.join(temporal, "tienda.html"), html)
    escribir(os.path.join(temporal, "datos.json"),
             json.dumps(datos, ensure_ascii=False, indent=2))

    foto_producto = {id(p): url_foto(p, ("imagen_url", "imagen_small_url")) for p in productos}
    foto_seccion = {id(s): url_foto(s, ("imagen_url", "imagen_128x128_url"))
                    for s in secciones if isinstance(s, dict)}
    archivos, bajadas, errores = bajar_fotos(list(foto_producto.values())
                                             + list(foto_seccion.values()))

    def archivo_de(url):
        nombre = archivos.get(clave_foto(url)) if url else ""
        return "fotos/%s" % nombre if nombre else ""

    nombres_seccion = {str(s.get("id")): texto(s.get("nombre")).strip()
                       for s in secciones if isinstance(s, dict)}
    filas_xlsx = [COLUMNAS]
    filas_csv = [["Codigo", "Nombre", "Seccion", "Seccion id", "Precio", "Foto",
                  "Tiene foto en AgilPedido"]]
    for p in productos:
        filas_xlsx.append([p.get("id"), texto(p.get("nombre")), texto(p.get("descripcion")),
                           "Si", pesos(p.get("precio")), pesos(p.get("precio_salon")),
                           texto(p.get("destacado"))])
        url = foto_producto[id(p)]
        filas_csv.append([p.get("id"), texto(p.get("nombre")),
                          nombres_seccion.get(str(p.get("seccion_id")), ""),
                          p.get("seccion_id"), pesos(p.get("precio")), archivo_de(url),
                          "Si" if url else "No"])
    escribir(os.path.join(temporal, "productos.xlsx"), armar_xlsx(filas_xlsx))
    escribir_csv(os.path.join(temporal, "productos.csv"), filas_csv)
    filas_secciones = [["Posicion", "Nombre", "Descripcion", "Seccion id", "Productos", "Foto"]]
    cuenta = {}
    for p in productos:
        cuenta[str(p.get("seccion_id"))] = cuenta.get(str(p.get("seccion_id")), 0) + 1
    for s in sorted((s for s in secciones if isinstance(s, dict)),
                    key=lambda s: (s.get("posicion") if isinstance(s.get("posicion"), int)
                                   else 10 ** 6)):
        filas_secciones.append([s.get("posicion"), texto(s.get("nombre")),
                                texto(s.get("descripcion")), s.get("id"),
                                cuenta.get(str(s.get("id")), 0), archivo_de(foto_seccion[id(s)])])
    escribir_csv(os.path.join(temporal, "secciones.csv"), filas_secciones)

    # resumen.txt
    con_foto = sum(1 for p in productos if foto_producto[id(p)])
    sin_archivo = sum(1 for p in productos if foto_producto[id(p)]
                      and not archivo_de(foto_producto[id(p)]))
    lineas = [
        "Respaldo de la tienda AgilPedido (%s)" % (FUENTE or URL),
        "Fecha: %s" % datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "",
        "Productos visibles: %s" % len(productos),
        "Secciones: %s" % len(secciones),
        "Productos con foto: %s (guardadas: %s; no se pudieron bajar: %s)"
        % (con_foto, con_foto - sin_archivo, sin_archivo),
        "Fotos nuevas descargadas en esta corrida: %s" % (bajadas - len(errores)),
        "Ojo: los productos OCULTOS en AgilPedido no aparecen en la página pública "
        "y no están en este respaldo.",
        "",
    ]
    if previo:
        altas, bajas, precios = diferencias(previo["productos"], productos)
        lineas += ["Comparado con el respaldo %s (%s productos):" % (dia_previo, n_previo),
                   "  Agregados: %s" % len(altas), "  Quitados: %s" % len(bajas),
                   "  Cambios de precio: %s" % len(precios), ""]
        for titulo, lista in (("Agregados", altas), ("Quitados", bajas),
                              ("Cambios de precio", precios)):
            if lista:
                lineas.append("%s (primeros %s):" % (titulo, min(MOSTRAR, len(lista))))
                lineas += ["  - %s" % x for x in lista[:MOSTRAR]]
                lineas.append("")
    else:
        lineas += ["Primer respaldo: no hay uno anterior para comparar.", ""]
    if errores:
        lineas.append("Fotos que no se pudieron bajar (se reintenta mañana):")
        lineas += ["  - %s: %s" % e for e in errores[:MOSTRAR]]
        lineas.append("")
    tamano_dia = tamano(temporal)
    lineas += ["Tamaño de la carpeta del día (sin fotos): %s" % mb(tamano_dia),
               "Depósito de fotos compartido: %s archivos, %s"
               % (len([n for n in os.listdir(FOTOS) if n != "indice.json"]), mb(tamano(FOTOS)))]
    escribir(os.path.join(temporal, "resumen.txt"), "\n".join(lineas) + "\n")

    # swap in atomically; an earlier run of today is replaced only now
    final = os.path.join(RAIZ, hoy)
    viejo = None
    if os.path.exists(final):
        viejo = os.path.join(RAIZ, ".%s.old" % hoy)
        shutil.rmtree(viejo, ignore_errors=True)
        os.rename(final, viejo)
    os.rename(temporal, final)
    if viejo:
        shutil.rmtree(viejo)
    log("ok: %s productos, %s secciones, %s fotos nuevas, %s errores de foto -> %s"
        % (len(productos), len(secciones), bajadas - len(errores), len(errores), hoy))
    try:
        podar(hoy)
    except Exception as exc:  # the backup itself is done: only log it
        log("aviso: la retención falló: %r" % exc)
    sys.stdout.write("\n".join(lineas) + "\n")


def main():
    hoy = datetime.date.today().isoformat()
    os.makedirs(RAIZ, exist_ok=True)
    for nombre in os.listdir(RAIZ):  # leftovers of an interrupted run
        ruta = os.path.join(RAIZ, nombre)
        if nombre.startswith(".") and nombre.endswith(".old"):
            original = os.path.join(RAIZ, nombre[1:-4])
            if os.path.exists(original):
                shutil.rmtree(ruta, ignore_errors=True)
            else:  # interrupted mid-swap: put the earlier backup back
                os.rename(ruta, original)
        elif nombre.startswith(".") and nombre.endswith(".tmp"):
            shutil.rmtree(ruta, ignore_errors=True)
    html = b""
    try:
        if FUENTE:
            with open(FUENTE, "rb") as f:
                html = f.read()
        else:
            try:
                html = descargar(URL, PAGINA_TIMEOUT)
            except Exception as exc:
                raise Falla("no se pudo descargar la página: %s" % exc) from exc
        respaldar(hoy, html)
    except Exception as exc:
        shutil.rmtree(os.path.join(RAIZ, ".%s.tmp" % hoy), ignore_errors=True)
        motivo = str(exc) if isinstance(exc, Falla) else "error inesperado: %r" % exc
        registrar_falla(hoy, motivo, html)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
