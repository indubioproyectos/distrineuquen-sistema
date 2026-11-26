"""Reading payment receipts: photos by local OCR, PDFs by their text.

Customer receipts never leave the server: the OCR is Tesseract with the
Spanish data, installed in the lab's Odoo image (/srv/odoo-lab/image,
Dockerfile). Without Tesseract the photo is kept as it is and the receipt
asks for the amount («Revisar importe»), as before.

Pipeline (WhatsappMessageComprobante._distri_read_receipt in cobros.py):
incoming WhatsApp photo or PDF -> text (ocr_image / PDF text) ->
parse_receipt -> amount, operation number, date, payer name and CUIT,
destination (alias, CVU or CBU) and the bank or wallet -> the receipt's
fields, with a confidence flag on the amount:

- «Leído»: the amount has a label («Monto», «Importe», «Total»,
  «Transferiste»...) or is the only $ amount, every candidate agrees and
  the OCR is clear. It fills the amount.
- «Revisar importe»: several different amounts, a blurry photo, or the
  amount written by the customer differs from the one read. The read
  amount is kept apart («Importe leído») and the administrator confirms it
  («Usar importe leído») or types it.
- An amount typed by the administrator is «Cargado a mano» and nothing read
  later replaces it.

The regular expressions follow the receipts of Mercado Pago, Ualá, Brubank,
Galicia, Santander, Banco Nación, Banco Provincia del Neuquén, Naranja X,
Macro, BBVA, ICBC, Personal Pay, MODO and Cuenta DNI.
"""
import io
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

_logger = logging.getLogger(__name__)

try:  # a thin wrapper around the same tesseract binary
    import pytesseract
except ImportError:  # pragma: no cover - the CLI is used instead
    pytesseract = None

# 26/11/2026 (production, load 50-65 on 4 CPUs): when Odoo killed a cron
# worker past limit_time_real, the tesseract processes it had started kept
# running for 40+ minutes and piled up. Every pass now runs our own
# subprocess: one thread (OMP_THREAD_LIMIT=1) and, on Linux, killed together
# with the worker that started it (PR_SET_PDEATHSIG).
TESSERACT_ENV = dict(os.environ, OMP_THREAD_LIMIT="1")


def _die_with_parent():  # pragma: no cover - runs in the child process
    try:
        import ctypes
        import signal
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass

OCR_LANG = "spa"
# The Mercado Pago app's font makes the Spanish model read «8» as «3». The
# English model gets those right and is only asked when the amount read has a
# «3», and only believed when the two readings differ by 3<->8 digits alone.
OCR_ARBITER_LANG = "eng"
OCR_TIMEOUT = 40
# 19.0.1.128.0 (audit 2026-11-22): a reading done while a person waits (the
# upload screen, «Cobrar», crediting) gives up after this many seconds, so the
# request never reaches Odoo's limit_time_real; the person types the amount.
# The background reading keeps OCR_TIMEOUT per pass.
PARAM_OCR_SECONDS = "distri_pedido.ocr_segundos"
DEFAULT_OCR_SECONDS = 7.0
OCR_SECONDS_MIN, OCR_SECONDS_MAX = 2.0, 30.0
OCR_GAVE_UP = "No se pudo leer el comprobante a tiempo: escribí el importe a mano."
_deadline = threading.local()
# The company's own account (CUIT, holder, CVU/CBU, alias): never the payer.
# Only in the ir.config_parameter distri_pedido.cuenta_propia (comma
# separated), loaded on each server by /srv/odoo-lab/scripts/cargar-datos-empresa.sh
# from a private file: real data never lives in the code (19.0.1.32.0).
PARAM_OWN_ACCOUNT = "distri_pedido.cuenta_propia"
OWN_ACCOUNT_DEFAULT = ""
# Photos narrower than this are enlarged before reading: phone screenshots
# come at 600-1080 px and small text reads better upscaled.
OCR_MIN_WIDTH = 1400
OCR_MAX_SIDE = 3000
# Below this mean word confidence (0-100) the photo is too blurry to trust.
OCR_MIN_CONFIDENCE = 55
MAX_AMOUNT = 100_000_000

CONFIDENCE_HIGH = "high"
CONFIDENCE_LOW = "low"

BANKS = [
    (r"mercado\s*pago", "Mercado Pago"),
    (r"\bual[aá]\b", "Ualá"),
    (r"brubank", "Brubank"),
    (r"naranja\s*x", "Naranja X"),
    (r"galicia", "Banco Galicia"),
    (r"santander", "Santander"),
    (r"provincia\s+del\s+neuqu[eé]n|\bbpn\b", "Banco Provincia del Neuquén"),
    (r"banco\s+(?:de\s+la\s+)?naci[oó]n|\bbna\b", "Banco Nación"),
    (r"cuenta\s*dni|banco\s+provincia\b", "Banco Provincia (Cuenta DNI)"),
    (r"\bmacro\b", "Banco Macro"),
    (r"\bbbva\b|franc[eé]s", "BBVA"),
    (r"\bicbc\b", "ICBC"),
    (r"personal\s*pay", "Personal Pay"),
    (r"\bmodo\b", "MODO"),
    (r"\bprex\b", "Prex"),
    (r"lemon", "Lemon"),
    (r"supervielle", "Banco Supervielle"),
    (r"resimple", "Resimple"),
    (r"\bfepn\b", "FEPN"),
    # a bank transfer whose bank is not named: the clearing house's id, the
    # reference number or the debit line of a home banking receipt
    (r"coelsa|nro\.?\s*de\s*referencia|importe\s+debitado", "Transferencia bancaria"),
]
# Supervielle's app drops the decimal comma AND the thousands dots: «$ 26186000»
_CENTS_HIDDEN = re.compile(r"supervielle", re.IGNORECASE)

MONTHS = {
    "ene": 1, "feb": 2, "mar": 3, "abr": 4, "may": 5, "jun": 6, "jul": 7,
    "ago": 8, "sep": 9, "set": 9, "oct": 10, "nov": 11, "dic": 12,
}

_NUMBER = r"(\d{1,3}(?:[.\s]\d{3})+|\d+)(?:,(\d{1,2})|\s(\d{2})(?![\d.,]))?"
# After the number: no digit, no «/» (a date) and no «-» followed by a digit
# (a range, a phone); a trailing «-» («$12.500-») is fine. «[.,]\d» stops the
# match from backtracking into a shorter number («$1.500.50» is not «$1.500»).
_AMOUNT_END = r"(?![\d/]|-\d|[.,]\d)"
# Between the «$» and the number: up to two words (the Mercado Pago layout of
# 2026 merges the column «Motivo: Varios» into the amount's row: «$ Motivo:
# 253.500 Varios») and a minus sign (a bank's debit notice: «$ -111.600,00»).
_AMOUNT_WITH_SIGN = re.compile(r"(?:\$|\bars|\bar\$)\s*-?\s*" + _NUMBER + _AMOUNT_END,
                               re.IGNORECASE)
# after the words, only a number that looks like money: thousands or 4+ digits
_AMOUNT_AFTER_WORDS = re.compile(
    r"\$\s*(?:[A-Za-zÁÉÍÓÚÑáéíóúñ¡!:]{2,}\s+){1,2}-?\s*"
    r"(\d{1,3}(?:[.\s]\d{3})+|\d{4,})(?:,(\d{1,2})|\s(\d{2})(?![\d.,]))?" + _AMOUNT_END,
    re.IGNORECASE)
# the big «$» of the amount is often read as «S»: only on a line of its own
_AMOUNT_S_LINE = re.compile(r"^\s*S\s*" + _NUMBER + r"\s*$")
_AMOUNT_BARE = re.compile(r"(?<![\d/.,-])" + _NUMBER + r"(?![\d/%]|-\d|[.,]\d)")
_AMOUNT_LABEL = re.compile(
    r"\b(monto(?:\s+(?:transferido|enviado|total|debitado|acreditado))?|importe(?:\s+"
    r"(?:transferido|enviado|total|debitado|acreditado))?|total(?:\s+(?:transferido|pagado))?"
    r"|transferiste|enviaste|pagaste|le\s+pagaste|recibiste|valor|suma\s+de)\b",
    re.IGNORECASE)
# Amounts on these lines are not the payment: balances, fees, taxes.
_NOT_THE_PAYMENT = re.compile(
    r"saldo|comisi[oó]n|impuesto|\biva\b|costo|disponible|l[ií]mite|cargo|inter[eé]s|"
    r"cuota|devoluci[oó]n|cashback|reintegro|\b(?:ca|cc)\s*(?:\$|u\$s)", re.IGNORECASE)

_OPERATION_LABEL = re.compile(
    r"(?:n(?:[°º?o*.]|ro\.?|[uú]mero)\s*(?:de\s*)?(?:operaci[oó]n|transacci[oó]n|comprobante|"
    r"referencia|control|transferencia|movimiento)"
    r"|(?:id|c[oó]digo)\s*(?:de\s*)?(?:la\s*)?(?:operaci[oó]n|transacci[oó]n|transferencia|"
    r"referencia|comprobante|identificaci[oó]n)"
    r"|operaci[oó]n\s*(?:n(?:[°º?o*.]|ro\.?|[uú]mero))?"
    r"|comprobante\s*(?:n(?:[°º?o*.]|ro\.?|[uú]mero))"
    r"|nro\.?\s*(?:de\s*)?(?:control|comprobante|operaci[oó]n)"
    r"|transacci[oó]n|referencia|coelsa\s*id|id\s*op\b\.?)",
    re.IGNORECASE)
_TOKEN = re.compile(r"[A-Za-z0-9][A-Za-z0-9-]{4,40}[A-Za-z0-9]")
# Ualá's «Id. Op.» of 2026 is a UUID of its own: never a Mercado Pago operation
# number (the transfer to the company's Mercado Pago account has another one)
_UUID = re.compile(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", re.IGNORECASE)
# A Mercado Pago operation number without its label (cropped screenshot):
# 11-13 digits starting with 1. Phones with the country code start with 5,
# local ones have 10 digits; CUITs are found before and removed.
_MP_BARE_ID = re.compile(r"(?<![\d-])(1\d{10,12})(?![\d-])")

_DATE_NUM = re.compile(r"\b(\d{1,2})\s*[/.-]\s*(\d{1,2})\s*[/.-]\s*(\d{4}|\d{2})\b")
_DATE_ISO = re.compile(r"\b(20\d{2})-(\d{2})-(\d{2})\b")
# the year is optional («5 de agosto, 14:06»); «12026» is not a year
_DATE_TEXT = re.compile(
    r"\b(\d{1,2})\s*(?:de\s+|/|-|\s)\s*(ene|feb|mar|abr|may|jun|jul|ago|sep|set|oct|nov|dic)"
    r"[a-záéíóú]*\.?\s*(?:de\s+|del\s+|/|-|,|\s)?\s*(?:(?<!\d)(\d{4})(?!\d))?", re.IGNORECASE)
_TIME = re.compile(r"(?<![\d:])([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?::([0-5]\d))?(?![\d])")
# «a las 14:06» with the day lost: the day of the WhatsApp message
_TIME_ONLY = re.compile(r"\ba\s+las\s+([01]?\d|2[0-3])\s*[:.]\s*([0-5]\d)(?![\d])", re.IGNORECASE)

_CUIT = re.compile(r"(?<!\d)(20|23|24|25|26|27|30|33|34)\s*[-.\s]?\s*(\d{8})\s*[-.\s]?\s*(\d)(?!\d)")
_CBU = re.compile(r"(?<!\d)(\d{22})(?!\d)")
_ALIAS = re.compile(r"\balias\s*(?:cbu|cvu)?\s*[:.]?\s*([a-z0-9][a-z0-9-]*(?:\s?\.\s?[a-z0-9-]+){1,3})\b",
                    re.IGNORECASE)

_ORIGIN = re.compile(
    r"\b(origen|desde|ordenante|remitente|pagador|emisor|enviado\s+por|debitad[oa]\s+de|"
    r"cuenta\s+de\s+origen|qui[eé]n\s+env[ií]a|desde\s+tu\s+cuenta|de\s*:|^de$)", re.IGNORECASE)
_DESTINATION = re.compile(
    r"(?i:\b(?:destino|destinatari[oa]|beneficiari[oa]|acreditad[oa]\s+en|enviaste\s+a|"
    r"transferiste\s+a|le\s+pagaste\s+a|qui[eé]n\s+recibe|cuenta\s+de\s+destino|"
    r"para\s*:?$|para\s*:))"
    # «a Carla Sosa» (Naranja X): lowercase «a» and a capitalized name
    r"|^a\s+(?=[A-ZÁÉÍÓÚÑ][a-záéíóúñ])")
_NAME_PREFIX = re.compile(
    r"^\s*(?:de|para|a|titular|nombre|ordenante|emisor|beneficiari[oa]|destinatari[oa]|"
    r"remitente|pagador|desde\s+tu\s+cuenta|enviaste\s+a|transferiste\s+a|le\s+pagaste\s+a)"
    r"\b\s*:?\s*", re.IGNORECASE)
_NOT_A_NAME = re.compile(
    r"banco|mercado|pago|cuit|cuil|cvu|cbu|alias|cuenta|caja|ahorro|corriente|transfer|"
    r"comprobante|operaci|fecha|motivo|varios|origen|destino|monto|importe|total|pesos|"
    r"\bars\b|concepto|referencia|n[uú]mero|c[oó]digo|estado|aprobad|exitos|enviaste|"
    r"recibiste|detalle|compartir|descargar|hora|billetera|wallet|tarjeta|d[eé]bito|"
    r"cr[eé]dito|inmediata|dni|id\b|comisi|servicio|ual[aá]|brubank|galicia|santander|"
    r"naci[oó]n|provincia|naranja|macro|bbva|icbc|personal|modo|prex|lemon|para|desde|"
    r"titular|nombre|beneficiari|destinatari|ordenante|transacci|control|coelsa|"
    r"reintegro|saldo|disponible|"
    # 19.0.1.129.50: the headers of Galicia «Office Banking» («Datos del
    # pagador», «Razón Social», «Office Banking», «Impreso ...»)
    r"\bdatos\s+del?\b|raz[oó]n\s+social|office\s+banking|impreso", re.IGNORECASE)
# 19.0.1.129.50: Galicia «Office Banking» (companies' home banking) names its
# operation «Identificador de la operación: LRBRBFb868», letters and digits
_IDENTIFIER_LABEL = re.compile(
    r"identificador\s+de\s+(?:la\s+)?operaci[oó]n\s*:?\s*([A-Za-z0-9]{6,24})(?![A-Za-z0-9])",
    re.IGNORECASE)
# uppercase only, so a surname like «Sa» is still read as before
_LEGAL_FORM = re.compile(
    r"\s((?:S\.?\s?A\.?\s?S|S\.?\s?R\.?\s?L|S\.?\s?A\.?\s?U|S\.?\s?A|S\.?\s?C\.?\s?A|S\.?\s?H)\.?)$")
_OFFICE_BANKING = re.compile(r"office\s+banking", re.IGNORECASE)


# ---------------------------------------------------------------- helpers


def _plain(text):
    """Lowercase without accents, for keyword checks."""
    text = unicodedata.normalize("NFKD", text or "")
    return "".join(c for c in text if not unicodedata.combining(c)).lower()


def _to_amount(integer, cents):
    try:
        value = float(re.sub(r"[.\s]", "", integer))
    except ValueError:
        return 0.0
    if cents:
        value += float(cents.ljust(2, "0")) / 100
    return value


def cuit_is_valid(cuit):
    cuit = re.sub(r"\D", "", cuit or "")
    if len(cuit) != 11:
        return False
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    total = sum(int(d) * w for d, w in zip(cuit[:10], weights))
    check = 11 - total % 11
    check = 0 if check == 11 else (9 if check == 10 else check)
    return check == int(cuit[10])


def _looks_like_name(line):
    """The person's name on a line («Valeria Gómez», «Para: Valeria Gómez»,
    «Ordenante Valeria Gómez»), or ""."""
    line = line or ""
    if ":" in line and len(line.split(":", 1)[0].split()) <= 4:
        line = line.split(":", 1)[1]
    line = _NAME_PREFIX.sub("", line)
    line = re.sub(r"^[^A-Za-zÁÉÍÓÚÑáéíóúñ]+", "", line).strip(" .,:;-")
    # an icon read as a letter: «<S Juan Carlos Pérez»
    line = re.sub(r"^[A-Za-z]{1}\s+(?=\S+\s+\S+)", "", line)
    if not line or _NOT_A_NAME.search(line):
        return ""
    # 19.0.1.129.50: a company's legal form at the end, «AHYRE NORTE S. A. S.»
    # (Galicia Office Banking): its one-letter parts are not words of the name
    suffix = ""
    legal = _LEGAL_FORM.search(line)
    if legal and legal.start():
        suffix = legal.group(1) + ("." if "." in legal.group(1) else "")
        line = line[:legal.start()].strip()
    words = line.split()
    if not (1 if suffix else 2) <= len(words) <= 6:
        return ""
    # «NELDA/JESICA MUNOZ»: Ualá joins the holder's given names with a slash
    if not all(re.fullmatch(r"[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'.]{2,}(?:/[A-Za-zÁÉÍÓÚÜÑáéíóúüñ'.]{2,})*", w)
               for w in words):
        return ""
    return " ".join(words + ([suffix] if suffix else []))


# ---------------------------------------------------------------- time cap


def ocr_seconds(env):
    """The interactive cap in seconds (PARAM_OCR_SECONDS, default 7, kept
    between OCR_SECONDS_MIN and OCR_SECONDS_MAX)."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_OCR_SECONDS)
    try:
        seconds = float(value) if value not in (None, False, "") else DEFAULT_OCR_SECONDS
    except ValueError:
        seconds = DEFAULT_OCR_SECONDS
    return min(max(seconds, OCR_SECONDS_MIN), OCR_SECONDS_MAX)


@contextmanager
def ocr_deadline(seconds):
    """Every OCR pass inside gives up once `seconds` have passed since the
    block started. Nested blocks keep the earliest deadline (one request, one
    budget). `ocr_gave_up()` tells, inside the block and after it, whether a
    pass was skipped or cut."""
    previous = getattr(_deadline, "at", None)
    at = time.monotonic() + seconds
    _deadline.at = min(at, previous) if previous else at
    if previous is None:
        # the flag of the last block stays readable after it ends
        _deadline.gave_up = False
    try:
        yield
    finally:
        _deadline.at = previous


def ocr_time_left():
    """Seconds left of the current `ocr_deadline`, or None without one."""
    at = getattr(_deadline, "at", None)
    return None if at is None else at - time.monotonic()


def ocr_gave_up():
    return bool(getattr(_deadline, "gave_up", False))


def _pass_timeout():
    """The timeout of the next Tesseract pass, or 0 when the cap is spent."""
    left = ocr_time_left()
    if left is None:
        return OCR_TIMEOUT
    if left < 0.3:
        _deadline.gave_up = True
        return 0
    return min(OCR_TIMEOUT, left)


# ---------------------------------------------------------------- OCR


def ocr_available():
    return bool(shutil.which("tesseract"))


def pdf_images(data, max_pages=3):
    """PNG bytes of the pictures inside a PDF. A scanned receipt, or a bank
    app's «share as PDF», is a PDF with a photo and no text. JPEG pictures
    are used as they come; Flate ones are rebuilt from their pixels, and
    since their colour space may be an array (ICCBased, CalRGB) the bytes
    per pixel tell the mode (8-bit components)."""
    from PIL import Image
    try:
        from pypdf import PdfReader
    except ImportError:
        from PyPDF2 import PdfReader

    def resolved(value):
        return value.get_object() if hasattr(value, "get_object") else value

    pngs = []
    try:
        pages = PdfReader(io.BytesIO(data)).pages[:max_pages]
    except Exception:
        return pngs
    for page in pages:
        # the resources may be an indirect object (Ualá's PDFs)
        resources = resolved(page.get("/Resources")) or {}
        xobjects = resolved(resources.get("/XObject")) or {}
        for ref in xobjects.values():
            try:
                obj = resolved(ref)
                if obj.get("/Subtype") != "/Image":
                    continue
                raw = obj.get_data()
                if obj.get("/Filter") in ("/DCTDecode", "/JPXDecode"):
                    image = Image.open(io.BytesIO(raw))
                else:
                    width, height = int(obj["/Width"]), int(obj["/Height"])
                    mode = {1: "L", 3: "RGB", 4: "CMYK"}.get(len(raw) // (width * height))
                    if not mode:
                        continue
                    image = Image.frombytes(mode, (width, height), raw[:width * height * len(mode)])
                image = image.convert("RGB")
                mask = obj.get("/SMask")
                if mask is not None:
                    # Ualá's «Comprobante de transferencia-N.pdf»: the picture
                    # is all black and the text lives in its alpha channel.
                    # Composed over white it reads as black text on white.
                    image = _compose_over_white(image, resolved(mask))
                buffer = io.BytesIO()
                image.save(buffer, "PNG")
                pngs.append(buffer.getvalue())
            except Exception as exc:  # one unreadable picture never stops the others
                _logger.info("PDF picture not readable (%s)", type(exc).__name__)
    return pngs


def _compose_over_white(image, mask):
    """The RGB picture with its PDF soft mask (an /SMask image XObject, one
    byte per pixel) applied, composed over a white background."""
    from PIL import Image
    width, height = int(mask["/Width"]), int(mask["/Height"])
    raw = mask.get_data()
    if mask.get("/Filter") in ("/DCTDecode", "/JPXDecode"):
        alpha = Image.open(io.BytesIO(raw)).convert("L")
    else:
        if len(raw) < width * height:
            return image
        alpha = Image.frombytes("L", (width, height), raw[:width * height])
    if alpha.size != image.size:
        alpha = alpha.resize(image.size)
    image = image.copy()
    image.putalpha(alpha)
    background = Image.new("RGBA", image.size, (255, 255, 255, 255))
    return Image.alpha_composite(background, image).convert("RGB")


def _prepare(data):
    from PIL import Image, ImageOps

    image = Image.open(io.BytesIO(data))
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA", "P"):
        image = image.convert("RGBA")
        background = Image.new("RGBA", image.size, (255, 255, 255, 255))
        image = Image.alpha_composite(background, image)
    image = image.convert("L")
    width, height = image.size
    scale = 1.0
    if width < OCR_MIN_WIDTH:
        scale = OCR_MIN_WIDTH / float(width)
    if max(width, height) * scale > OCR_MAX_SIDE:
        scale = OCR_MAX_SIDE / float(max(width, height))
    if abs(scale - 1.0) > 0.05:
        image = image.resize((max(1, int(width * scale)), max(1, int(height * scale))),
                             Image.LANCZOS)
    return ImageOps.autocontrast(image)


def _adaptive_bw(image, radius, offset):
    """Black and white by local contrast, for a phone screen photographed with
    another phone: glare and shadows make any single threshold lose the text.
    A pixel darker than its neighbourhood by more than `offset` is text."""
    from PIL import ImageChops, ImageFilter

    big = image.resize((image.width * 2, image.height * 2), _lanczos())
    blur = big.filter(ImageFilter.GaussianBlur(radius))
    return ImageChops.subtract(blur, big).point(lambda p: 0 if p > offset else 255)


def _fixed_bw(image, threshold):
    from PIL import ImageFilter

    big = image.resize((image.width * 2, image.height * 2), _lanczos())
    big = big.filter(ImageFilter.UnsharpMask(2, 150, 3))
    return big.point(lambda p: 255 if p > threshold else 0)


def _lanczos():
    from PIL import Image

    return Image.LANCZOS


# Second readings of a photo (19.0.1.55.0): (how the prepared image is
# changed, Tesseract page mode). Every amount needs two readings that agree;
# these are tried in order until one does. Measured on the photos of the
# WhatsApp group (scripts/ocr-doble-lectura/).
OCR_SECOND_READINGS = (
    (None, 6),
    (lambda image: _adaptive_bw(image, 30, 10), 11),
    (lambda image: _fixed_bw(image, 150), 6),
    (lambda image: _adaptive_bw(image, 15, 15), 11),
)


SECOND_READING_MIN_AMOUNT = 1000


def _tsv_words(image, lang=OCR_LANG, psm=3, timeout=OCR_TIMEOUT):
    """Words of the image: (left, top, width, height, conf, text)."""
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    result = subprocess.run(
        ["tesseract", "stdin", "stdout", "-l", lang, "--psm", str(psm), "tsv"],
        input=buffer.getvalue(), capture_output=True, timeout=timeout, check=True,
        env=TESSERACT_ENV, preexec_fn=_die_with_parent)
    lines = result.stdout.decode("utf-8", "replace").splitlines()
    rows = []
    for line in lines[1:]:
        parts = line.split("\t")
        if len(parts) == 12:
            rows.append((int(parts[6]), int(parts[7]), int(parts[8]), int(parts[9]),
                         float(parts[10]), parts[11]))
    words = []
    for left, top, width, height, conf, text in rows:
        text = (text or "").strip()
        if text and float(conf) >= 0:
            words.append((int(left), int(top), int(width), int(height), float(conf), text))
    return words


def _lines_from_words(words):
    """Group words by visual row, so «Monto ... $ 45.000» stays on one line
    even when the value sits at the far right."""
    lines = []
    for word in sorted(words, key=lambda w: (w[1] + w[3] / 2.0, w[0])):
        center = word[1] + word[3] / 2.0
        for line in lines:
            if abs(line["center"] - center) <= max(line["height"], word[3]) * 0.55:
                line["words"].append(word)
                line["center"] = (line["center"] * (len(line["words"]) - 1) + center) / len(
                    line["words"])
                line["height"] = max(line["height"], word[3])
                break
        else:
            lines.append({"center": center, "height": word[3], "words": [word]})
    lines.sort(key=lambda l: l["center"])
    return "\n".join(" ".join(w[5] for w in sorted(l["words"], key=lambda w: w[0]))
                     for l in lines)


def ocr_image(data, lang=OCR_LANG, change=None, psm=3):
    """(text, mean word confidence 0-100) of a photo. ("", 0.0) if it cannot
    be read or Tesseract is missing. `change` and `psm`: a second reading
    (OCR_SECOND_READINGS)."""
    if not data:
        return "", 0.0
    if not ocr_available():
        _logger.warning("Receipt OCR: tesseract is not installed")
        return "", 0.0
    timeout = _pass_timeout()
    if not timeout:
        return "", 0.0
    try:
        image = _prepare(data)
        if change:
            image = change(image)
        timeout = _pass_timeout()
        if not timeout:
            return "", 0.0
        words = _tsv_words(image, lang, psm, timeout=timeout)
    except Exception as exc:  # a broken photo is still a receipt to look at
        if ocr_time_left() is not None and ocr_time_left() < 0.3:
            _deadline.gave_up = True
            _logger.info("Receipt OCR: gave up at the time cap (%s)", type(exc).__name__)
            return "", 0.0
        _logger.info("Receipt OCR failed (%s: %s)", type(exc).__name__, exc)
        return "", 0.0
    if not words:
        return "", 0.0
    confidence = sum(w[4] for w in words) / len(words)
    return _lines_from_words(words), confidence


def differs_only_by_3_and_8(first, second):
    """Two amounts whose digits are the same except for «3» read as «8» (or
    the other way round) in one or more places."""
    a, b = "%.2f" % first, "%.2f" % second
    if len(a) != len(b) or a == b:
        return False
    return all(x == y or {x, y} == {"3", "8"} for x, y in zip(a, b))


def _same_amount(first, second):
    return bool(first and second) and (abs(first - second) < 0.5
                                       or differs_only_by_3_and_8(first, second))


# Fields a second reading may fill when the first one missed them, only when
# two second readings say the same
_SECOND_READING_FIELDS = ("operation", "date", "payer_cuit", "payer_name", "dest_name",
                          "dest_cuit", "destination", "bank")


def _agreed(values):
    """The value at least two readings gave, or None."""
    for index, value in enumerate(values):
        if value and value in values[index + 1:]:
            return value
    return None


def read_image(data, double_check=True, **parse_kwargs):
    """(text, confidence, fields) of a photo: the Spanish OCR and its parse,
    with the amount arbitrated by the English model when the Spanish one may
    have read an «8» as a «3» (the amount contains a «3»). Measured on real
    Mercado Pago screenshots: 40 of 40 wrong amounts fixed, no right one
    broken. The extra pass only runs when there is a «3» to doubt.

    Double check (19.0.1.55.0): every amount needs a second reading
    (OCR_SECOND_READINGS) that agrees. The first one that agrees ends it;
    with none, the amount stays only as «low» (the administrator decides).
    When the first reading found no amount, the second readings only suggest
    one («low»). A field the first reading missed is taken when two second
    readings agree."""
    text, confidence = ocr_image(data)
    reading = parse_receipt(text, confidence, **parse_kwargs) if text else {}
    amount = reading.get("amount") or 0.0
    if amount and "3" in "%.2f" % amount:
        other_text, other_confidence = ocr_image(data, OCR_ARBITER_LANG)
        other = parse_receipt(other_text, other_confidence, **parse_kwargs) if other_text else {}
        if other.get("amount") and differs_only_by_3_and_8(amount, other["amount"]):
            amount = reading["amount"] = other["amount"]
            reading["amount_arbitrated"] = True
    if not double_check:
        return text, confidence, reading
    seconds = []
    for change, psm in OCR_SECOND_READINGS:
        other_text, other_confidence = ocr_image(data, change=change, psm=psm)
        other = parse_receipt(other_text, other_confidence, **parse_kwargs) \
            if other_text else {}
        seconds.append((other_text, other))
        found = [s.get("amount") or 0.0 for _t, s in seconds]
        if amount and _same_amount(amount, found[-1]):
            break
        if not amount and _agreed(found):
            break
    found = [s.get("amount") or 0.0 for _t, s in seconds]
    if amount:
        if not any(_same_amount(amount, other) for other in found):
            reading["amount_confidence"] = CONFIDENCE_LOW
            reading["amount_unconfirmed"] = True
    else:
        # only a suggestion for the administrator («Usar importe leído»):
        # measured, two second readings can agree on an amount missing its
        # first digit (194.000 read as 94.000); below $1.000 it is noise
        found = [value for value in found if value >= SECOND_READING_MIN_AMOUNT]
        agreed = _agreed(found)
        single = next(iter(found), 0.0)
        if agreed or single:
            reading["amount"] = agreed or single
            reading["amount_confidence"] = CONFIDENCE_LOW
            reading["amount_second_reading"] = True
    for key in _SECOND_READING_FIELDS:
        if not reading.get(key):
            value = _agreed([s.get(key) for _t, s in seconds])
            if value:
                reading[key] = value
    if not text:
        text = max((t for t, _s in seconds), key=len, default="")
    return text, confidence, reading


# ---------------------------------------------------------------- parsing


# Tesseract often reads the thousands dot as a comma: «$1.000,000,00»,
# «$200,000,00», «$1,500» or «$ 45,000». Cents are always two digits, so a
# comma followed by exactly three digits is a thousands separator when another
# separator follows («,000,00») or the number ends there («,500»);
# «1,500,000» is 1.500.000.
_THOUSANDS_COMMA = re.compile(r"(?<=\d),(?=\d{3}(?:[.,]\d|(?!\d)))")


# A dot as the decimal mark (US style, some banks and PDFs). When a number has
# both separators, the LAST one is the decimal mark: «20,000.00» is 20.000,00
# and «1,500.50» is 1.500,50 («1.234,56» is already Argentine). A single dot
# followed by exactly two digits that end an otherwise plain digit run is the
# decimal mark too: «250000.00» is 250.000,00. Thousands always have three
# digits, so «$560.000» and «$83.50000» (Mercado Pago) are never touched.
_US_THOUSANDS = re.compile(r"(?<![\d.,])(\d{1,3}(?:,\d{3})+)\.(\d{2})(?![\d.,])")
_DOTS_ONLY = re.compile(r"(?<![\d.,])(\d{1,3}(?:\.\d{3})+)\.(\d{2})(?![\d.,])")
_PLAIN_DECIMAL_DOT = re.compile(r"(?<![\d.,])(\d+)\.(\d{2})(?![\d.,])")


def _fix_decimal_dot(line):
    line = _US_THOUSANDS.sub(lambda m: "%s,%s" % (m.group(1).replace(",", "."), m.group(2)),
                             line)
    line = _DOTS_ONLY.sub(r"\1,\2", line)
    # «250000.00» is sure; «27.50» may be a blurred «27.500»: written as loose
    # cents («27 50»), which `_value` reads as the same amount but doubtful
    return _PLAIN_DECIMAL_DOT.sub(
        lambda m: "%s%s%s" % (m.group(1), "," if m.group(2) == "00" else " ", m.group(2)),
        line)


def _fix_thousands(line):
    return _THOUSANDS_COMMA.sub(".", line)


# Supervielle PDFs space out the digits of the amount: «$  5 6 0 . 0 0 00 0»
_LONE_DIGIT = re.compile(r"(?<!\S)\d(?!\S)")
_SPACE_IN_NUMBER = re.compile(r"(?<=[\d.])\s+(?=[\d.])")
# Mercado Pago PDFs drop the decimal comma: «$83.50000» is $83.500,00
_LOST_CENTS_COMMA = re.compile(r"(\$\s*\d{1,3}(?:\.\d{3})+)(\d{2})(?!\d)")


def _fix_amount_line(line):
    """Undo the PDF text quirks that break the amount on a «$» line."""
    if "$" not in line:
        return line
    if len(_LONE_DIGIT.findall(line)) >= 4:
        line = _SPACE_IN_NUMBER.sub("", line)
    return _LOST_CENTS_COMMA.sub(r"\1,\2", line)


# The OCR of a photo breaks the thousands of a «$» amount: «$ 250. 500,00»,
# «$ 174.:120,00», «$ 115:350,00», «Transferiste $221. 330 a», «$ 147,.000,00»
# and, in the Mercado Pago layout of 2026, the head repeated: «253. 253.500».
_REPEATED_HEAD = re.compile(r"(\d{1,3})\.\s+(?=\1\.)")
_COMMA_DOT = re.compile(r"(?<=\d),\.(?=\d{3})")
_SPLIT_THOUSANDS = re.compile(r"(\$\s*-?\s*\d{1,3})\s*[.:]\s*:?\s*(\d{3})(?!\d)")
# A thousands dot lost or a digit too many: «$ 1156.250» (1.156.250) and
# «$ 217.2183,00» (217.218): read, but never for sure.
_ODD_GROUPS = re.compile(
    r"\$\s*-?\s*(?:(\d{4,6})\.(\d{3})(?![\d,])|(\d{1,3})\.(\d{4}),(\d{2})(?!\d))")


def _fix_split_thousands(line):
    if "$" not in line:
        return line
    line = _REPEATED_HEAD.sub("", line)
    line = _COMMA_DOT.sub(".", line)
    return _SPLIT_THOUSANDS.sub(r"\1.\2", line)


def _odd_group_amounts(line):
    """(value, doubtful=True) for the amounts with a broken digit group."""
    found = []
    for match in _ODD_GROUPS.finditer(line):
        head, tail, small_head, long_group, cents = match.groups()
        if head:
            found.append((_to_amount(head + tail, ""), True))
        else:
            found.append((_to_amount(small_head + long_group[:3], cents), True))
    return found


def _value(match):
    """(amount, doubtful) of a _NUMBER match. «734.269 45» is read as
    734.269,45 (the OCR lost the comma of the cents) but stays doubtful."""
    integer, cents, loose_cents = match.group(1), match.group(2), match.group(3)
    return _to_amount(integer, cents or loose_cents), bool(loose_cents)


def _signed_value(match, cents_hidden):
    """A «$» amount. With `cents_hidden` (Supervielle) a plain run of 7-9
    digits carries the cents without any separator: «$ 26186000» is
    261.860,00, doubtful."""
    value, doubtful = _value(match)
    integer = match.group(1)
    if cents_hidden and not match.group(2) and not match.group(3) \
            and re.fullmatch(r"\d{7,9}", integer):
        return value / 100.0, True
    return value, doubtful


def _bare_amount(match, rest):
    """A number without «$» after the label counts only when it looks like
    money: cents, a thousands separator or at least four digits. «(9)» in
    «¡Listo! Le transferiste a (9)» does not."""
    integer = match.group(1)
    if match.start() and rest[match.start() - 1] == "(":
        return False
    if match.group(2) or match.group(3) or "." in integer or " " in integer:
        return True
    # a plain run of digits: 4 to 7 of them (more is an operation number)
    return 4 <= len(integer) <= 7


def _amounts(lines, cents_hidden=False):
    """(labeled, signed) amount candidates, as (value, doubtful)."""
    labeled, signed = [], []
    whole = re.compile(r"\s*" + _NUMBER + r"\s*")
    # US-style numbers first: their commas are thousands, not OCR mistakes
    lines = [_fix_decimal_dot(_fix_amount_line(_fix_thousands(_fix_decimal_dot(
        _fix_split_thousands(line))))) for line in lines]
    for index, line in enumerate(lines):
        if _NOT_THE_PAYMENT.search(line):
            continue
        signs = [_signed_value(m, cents_hidden) for m in _AMOUNT_WITH_SIGN.finditer(line)]
        s_line = _AMOUNT_S_LINE.match(line)
        if s_line and not signs:
            signs = [_value(s_line)]
        if not signs:
            signs = [_value(m) for m in _AMOUNT_AFTER_WORDS.finditer(line)]
        if not signs:
            signs = _odd_group_amounts(line)
        signed += signs
        label = _AMOUNT_LABEL.search(line)
        if not label:
            continue
        rest = line[label.end():]
        found = [_signed_value(m, cents_hidden) for m in _AMOUNT_WITH_SIGN.finditer(rest)]
        if not found:
            found = [_value(m) for m in _AMOUNT_AFTER_WORDS.finditer(rest)]
        if not found:
            found = _odd_group_amounts(rest)
        if not found:
            bare = [m for m in _AMOUNT_BARE.finditer(rest) if _bare_amount(m, rest)]
            found = [_value(m) for m in bare[:1]]
        if not found and index + 1 < len(lines) and not _NOT_THE_PAYMENT.search(lines[index + 1]):
            nxt = lines[index + 1]
            found = [_value(m) for m in _AMOUNT_WITH_SIGN.finditer(nxt)]
            s_line = _AMOUNT_S_LINE.match(nxt)
            if not found and s_line:
                found = [_value(s_line)]
            if not found and whole.fullmatch(nxt):
                found = [_value(whole.fullmatch(nxt))]
        labeled += found
    valid = lambda items: [(v, d) for v, d in items if 0 < v < MAX_AMOUNT]
    return valid(labeled), valid(signed)


def _pick_amount(lines, ocr_confidence, cents_hidden=False):
    labeled, signed = _amounts(lines, cents_hidden)
    values = {round(v, 2) for v, _ in labeled + signed}
    if labeled:
        amount, doubtful = labeled[0]
    elif signed:
        # the payment is the big number at the top: the first one
        amount, doubtful = signed[0]
    else:
        return 0.0, None
    confident = len(values) == 1 and not doubtful
    if ocr_confidence is not None and ocr_confidence < OCR_MIN_CONFIDENCE:
        confident = False
    return amount, (CONFIDENCE_HIGH if confident else CONFIDENCE_LOW)


# A number after «DNI», «CUIT» or «CUIL» is a person's, never the operation.
_ID_NUMBER = re.compile(r"\b(?:dni|cuit|cuil|cdi)\b\s*[:#.°º]*\s*[\d.\s-]*", re.IGNORECASE)


def _without_dates_and_ids(line):
    """The line without its dates, times and DNI/CUIT numbers: the label
    «Transacción» followed by «2026-09-26 14:30:00» has no operation."""
    for pattern in (_DATE_ISO, _DATE_NUM, _TIME, _ID_NUMBER):
        line = pattern.sub(" ", line)
    return line


def _operation(lines, bank=""):
    for line in lines:
        identifier = _IDENTIFIER_LABEL.search(line)
        if identifier and re.search(r"\d", identifier.group(1)):
            return identifier.group(1)
    for index, line in enumerate(lines):
        label = _OPERATION_LABEL.search(line)
        if not label:
            continue
        for candidate_line in (line[label.end():], *lines[index + 1:index + 3]):
            candidate_line = _without_dates_and_ids(candidate_line)
            if re.fullmatch(r"[\s:#°º.*-]*\d[\d ]*", candidate_line):
                # «490226851 476»: the OCR split one number in two
                candidate_line = candidate_line.replace(" ", "")
            for token in _TOKEN.findall(candidate_line):
                digits_only = re.sub(r"\D", "", token)
                if len(digits_only) < 6:
                    continue
                if _CUIT.fullmatch(token) or _DATE_NUM.fullmatch(token):
                    continue
                if len(digits_only) == 22:  # a CVU/CBU, not the operation
                    continue
                if _UUID.fullmatch(token):
                    continue
                return token.replace("-", "") if token.replace("-", "").isdigit() else token
    if bank == "Mercado Pago":
        candidates = _bare_mp_ids(lines)
        return candidates[0] if candidates else ""
    if not bank:
        # no bank named (the OCR lost the header and the «Número de operación
        # de Mercado Pago» label, import 20 of 06/11/2026): a bare Mercado
        # Pago id only when it is the only plausible one
        candidates = set(_bare_mp_ids(lines))
        return candidates.pop() if len(candidates) == 1 else ""
    return ""


def _bare_mp_ids(lines):
    """A Mercado Pago screenshot cropped above (or OCR'd without) its
    «Número de operación» label: the 11-13 digit numbers starting with 1
    that are not a CUIT, a CVU, a date or an amount, in reading order."""
    found = []
    for line in lines:
        if "$" in line:
            continue
        line = _CUIT.sub(" ", _without_dates_and_ids(line))
        line = _CBU.sub(" ", line.replace(" ", "")) if _CBU.search(line.replace(" ", "")) else line
        found += _MP_BARE_ID.findall(line)
    return found


def _date(text, reference=None):
    """Local date and time of the receipt (naive, Argentina). `reference` is
    the local moment the WhatsApp message arrived: it gives the year when the
    receipt omits it (never a date after the message) and the day when only
    «a las HH:MM» survived the OCR."""
    day = None
    for match in _DATE_TEXT.finditer(text):
        d, month, year = int(match.group(1)), MONTHS[_plain(match.group(2))[:3]], match.group(3)
        if year:
            year = int(year)
        else:
            year = (reference or datetime.now()).year
            try:
                if reference and datetime(year, month, d) > reference + timedelta(days=1):
                    year -= 1
            except ValueError:
                pass
        try:
            day = datetime(year, month, d)
            break
        except ValueError:
            continue
    if day is None:
        for match in _DATE_NUM.finditer(text):
            d, month, year = (int(g) for g in match.groups())
            if year < 100:
                year += 2000
            try:
                day = datetime(year, month, d)
                break
            except ValueError:
                continue
    if day is None:
        match = _DATE_ISO.search(text)
        if match:
            try:
                day = datetime(*(int(g) for g in match.groups()))
            except ValueError:
                day = None
    if day is None:
        time = _TIME_ONLY.search(text)
        if not (time and reference):
            return None
        return reference.replace(hour=int(time.group(1)), minute=int(time.group(2)),
                                 second=0, microsecond=0)
    time = _TIME.search(text)
    if time:
        day = day.replace(hour=int(time.group(1)), minute=int(time.group(2)),
                          second=int(time.group(3) or 0))
    return day


def own_account_values(text):
    """The company's own identifiers from the parameter's text (comma or
    newline separated: CUIT, holder's name, CVU/CBU, alias), normalized:
    digits for numbers, plain lowercase for names and aliases."""
    values = set()
    for item in re.split(r"[,;\n]", text or ""):
        item = item.strip()
        if not item:
            continue
        number = re.sub(r"\D", "", item)
        if number and len(number) >= 8 and len(number) >= len(item) / 2:
            values.add(number)
        else:
            values.add(" ".join(_plain(item).split()))
    return values


def _is_own(kind, value, own):
    if not (own and value):
        return False
    if kind == "name":
        return " ".join(_plain(value).split()) in own
    return value.lower() in own or re.sub(r"\D", "", value) in own


def _company_sends(items, own):
    """The company is the payer: on the origin side, with somebody else on
    the destination side; or, on a «Origen y destino» receipt, its first
    identifier comes before any other party's and another party's CUIT or
    account follows. A junk line read as a name never makes a party."""
    if any(_is_own(k, v, own) for s, k, v in items if s == "origin") and any(
            not _is_own(k, v, own) for s, k, v in items if s == "destination"):
        return True
    both = [(k, v) for s, k, v in items if s == "both"]
    first_own = next((i for i, (k, v) in enumerate(both) if _is_own(k, v, own)), None)
    first_other = next((i for i, (k, v) in enumerate(both) if not _is_own(k, v, own)), None)
    if first_own is None or (first_other is not None and first_other < first_own):
        return False
    return any(k in ("cuit", "account") and not _is_own(k, v, own)
               for k, v in both[first_own + 1:])


def _sections(lines):
    """The side each line belongs to, parallel to `lines`: "origin" after
    «Origen», «Emisor», «De:»...; "destination" after «Destino», «Para:»...;
    "both" after a line that announces both; None before any marker."""
    out = []
    section = None
    for line in lines:
        origin, destination = _ORIGIN.search(line), _DESTINATION.search(line)
        if origin and destination:
            section = "both"
        elif origin:
            section = "origin"
        elif destination:
            section = "destination"
        out.append(section)
    return out


def _parties(lines, own=None):
    """Payer and destination: name, CUIT and account (alias, CVU or CBU).

    Lines after «Origen», «De:», «Ordenante»... belong to the payer; after
    «Destino», «Para:», «Beneficiario»... to the destination. When one line
    announces both («Origen y destino», Mercado Pago), the first party is
    the payer and the second the destination. The company's own CUIT, name
    and account (`own`) are never the payer: a receipt that names only the
    destination does not turn the company into the payer, and one whose
    payer really is the company is flagged (`payer_own`: a refund).
    """
    items = []  # (section, kind, value)
    for line, section in zip(lines, _sections(lines)):
        for match in _CUIT.finditer(line):
            cuit = "".join(match.groups())
            if cuit_is_valid(cuit):
                items.append((section, "cuit", cuit))
        for match in _CBU.finditer(line.replace(" ", "")):
            items.append((section, "account", match.group(1)))
        for match in _ALIAS.finditer(line):
            items.append((section, "account", re.sub(r"\s", "", match.group(1)).lower()))
        name = _looks_like_name(line)
        if name:
            items.append((section, "name", name))

    def pick(kind, side):
        on_side = [v for s, k, v in items if k == kind and s == side]
        both = [v for s, k, v in items if k == kind and s == "both"]
        anywhere = [v for s, k, v in items if k == kind and s is None]
        if side == "origin":
            on_side, both, anywhere = (
                [v for v in group if not _is_own(kind, v, own)]
                for group in (on_side, both, anywhere))
        if on_side:
            return on_side[0]
        position = 0 if side == "origin" else 1
        if side == "destination" and kind != "name":
            # the company is the receiver: its own identifier, wherever it
            # was read, is the destination
            mine = [v for v in both + anywhere if _is_own(kind, v, own)]
            if mine:
                return mine[0]
        if len(both) > position:
            return both[position]
        if kind == "name":
            return ""
        if len(anywhere) > position:
            return anywhere[position]
        return ""

    # The company sends the money only when it is on the payer side AND
    # somebody else is read on the destination side. The company appearing
    # as the receiver is the normal case («Recibe Gonzalo...», its CVU as
    # «Cuenta destino»), and a receipt that names only the company (no
    # section marker survived the OCR) is a customer's payment too.
    payer_own = bool(own) and _company_sends(items, own)
    return {
        "payer_name": pick("name", "origin"),
        "payer_cuit": pick("cuit", "origin"),
        "payer_own": payer_own,
        "dest_name": pick("name", "destination"),
        "dest_cuit": pick("cuit", "destination"),
        "destination": pick("account", "destination"),
    }


def _bank(lines):
    """The payer's bank or wallet: named in the first lines (the header),
    else anywhere. The destination's lines are left out first: Ualá's
    «Cuenta destino Mercado Pago» is the company's wallet, not the payer's.
    19.0.1.129.50: a Galicia «Office Banking» receipt never names the bank;
    its footer does, after the destination's «Mercado Pago»."""
    if any(_OFFICE_BANKING.search(line) for line in lines):
        return "Banco Galicia"
    payer_side = [line for line, section in zip(lines, _sections(lines))
                  if section != "destination"]
    for candidates in (payer_side, lines):
        for scope in (candidates[:6], candidates):
            text = "\n".join(scope)
            for pattern, name in BANKS:
                if re.search(pattern, text, re.IGNORECASE):
                    return name
    return ""


def parse_receipt(text, ocr_confidence=None, reference=None, own=None):
    """Fields of a receipt from its text (OCR or PDF).

    `ocr_confidence` is the mean word confidence of the OCR (None for a PDF,
    whose text is exact). `reference` is the local (Argentina, naive) moment
    the message arrived, for dates without a year or a day. `own` is the set
    of the company's own identifiers (`own_account_values`). Returns a dict:
    amount, amount_confidence ("high", "low" or None when no amount was
    found), operation, date (naive local), payer_name, payer_cuit, payer_own
    (the company itself paid: a refund), dest_name, dest_cuit, destination,
    bank.
    """
    lines = [re.sub(r"[ \t]+", " ", l).strip() for l in (text or "").splitlines()]
    lines = [l for l in lines if l]
    bank = _bank(lines)
    amount, confidence = _pick_amount(lines, ocr_confidence,
                                      cents_hidden=bool(_CENTS_HIDDEN.search(text or "")))
    result = {
        "amount": amount,
        "amount_confidence": confidence,
        "operation": _operation(lines, bank),
        "date": _date("\n".join(lines), reference),
        "bank": bank,
    }
    result.update(_parties(lines, own))
    return result


def local_to_utc(moment, tz):
    """Naive local datetime -> naive UTC, as Odoo stores it."""
    if not moment:
        return False
    return moment.replace(tzinfo=tz).astimezone(timezone.utc).replace(tzinfo=None)
