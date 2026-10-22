"""Synthetic payment receipts for the OCR tests.

Every image is drawn here with PIL from made-up data, in the layouts of
the usual Argentine bank and wallet receipts. No real customer receipt is
used as a fixture. `degrade` simulates a phone photo or a forwarded
screenshot (smaller, blurred, JPEG, slightly rotated).

Kept free of Odoo imports: /srv/odoo-lab/scripts/l10n_ar/ocr_accuracy.py
uses it too.
"""
import io
import os
import random
from datetime import datetime

from PIL import Image, ImageDraw, ImageFilter, ImageFont

FONT_DIRS = (
    "/usr/share/fonts/truetype/roboto/unhinted/RobotoTTF",
    "/usr/share/fonts/truetype/dejavu",
)
MONTHS = ["enero", "febrero", "marzo", "abril", "mayo", "junio", "julio", "agosto",
          "septiembre", "octubre", "noviembre", "diciembre"]
MONTHS_SHORT = ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct",
                "nov", "dic"]

FIRST = ["Gonzalo", "María", "Juan", "Carla", "Lucía", "Martín", "Sofía", "Diego", "Paula",
         "Hernán", "Valeria", "Nicolás", "Florencia", "Ramiro", "Agustina", "Ezequiel"]
LAST = ["Ejemplar", "Gómez", "Pérez", "Rodríguez", "Fernández", "López", "Martínez",
        "Sánchez", "Romero", "Sosa", "Castañeda", "Arratia", "Bustos", "Quiroga", "Muñoz"]
ALIASES = ["distri.neuquen.mp", "kiosco.rey.pago", "almacen.sol.caja", "sur.mayorista",
           "cobros.distri.nqn"]


def _font(size, bold=False):
    names = ("Roboto-Bold.ttf", "DejaVuSans-Bold.ttf") if bold else (
        "Roboto-Regular.ttf", "DejaVuSans.ttf")
    for folder in FONT_DIRS:
        for name in names:
            path = os.path.join(folder, name)
            if os.path.exists(path):
                return ImageFont.truetype(path, size)
    return ImageFont.load_default(size=size)


def money(amount, cents="auto"):
    """45000.5 -> '45.000,50'; without cents when they are zero (auto)."""
    integer = int(amount)
    text = "{:,}".format(integer).replace(",", ".")
    cents_value = int(round((amount - integer) * 100))
    if cents == "always" or (cents == "auto" and cents_value):
        text += ",%02d" % cents_value
    return text


def cuit_with_check(prefix, body):
    weights = (5, 4, 3, 2, 7, 6, 5, 4, 3, 2)
    digits = "%s%08d" % (prefix, body)
    total = sum(int(d) * w for d, w in zip(digits, weights))
    check = 11 - total % 11
    check = 0 if check == 11 else (9 if check == 10 else check)
    return "%s%d" % (digits, check)


def fmt_cuit(cuit):
    return "%s-%s-%s" % (cuit[:2], cuit[2:10], cuit[10:])


def random_data(rng):
    def person():
        return "%s %s %s" % (rng.choice(FIRST), rng.choice(FIRST), rng.choice(LAST)) \
            if rng.random() < 0.4 else "%s %s" % (rng.choice(FIRST), rng.choice(LAST))

    def cuit():
        while True:
            value = cuit_with_check(rng.choice(("20", "23", "27")), rng.randint(10_000_000, 45_000_000))
            if not value.endswith("10"):
                return value

    amount = rng.choice([rng.randint(1, 400) * 500, rng.randint(1000, 900000),
                         rng.randint(100000, 90000000) / 100.0])
    return {
        "amount": float(amount),
        "operation": str(rng.randint(10 ** 9, 10 ** 12 - 1)),
        "date": datetime(2026, rng.randint(1, 12), rng.randint(1, 28),
                         rng.randint(7, 22), rng.randint(0, 59)),
        "payer_name": person(),
        "payer_cuit": cuit(),
        "payer_cvu": "%022d" % rng.randint(10 ** 20, 10 ** 22 - 1),
        "dest_name": person(),
        "dest_cuit": cuit(),
        "dest_cvu": "%022d" % rng.randint(10 ** 20, 10 ** 22 - 1),
        "dest_alias": rng.choice(ALIASES),
    }


class Canvas:
    def __init__(self, width=720, height=1400, background=(255, 255, 255)):
        self.image = Image.new("RGB", (width, height), background)
        self.draw = ImageDraw.Draw(self.image)
        self.width = width
        self.y = 40
        self.margin = 40

    def text(self, text, size=26, bold=False, color=(40, 40, 50), gap=12, x=None):
        font = _font(size, bold)
        self.draw.text((self.margin if x is None else x, self.y), text, font=font, fill=color)
        self.y += int(size * 1.25) + gap

    def row(self, label, value, size=24, color=(90, 90, 100), value_bold=True):
        """Label on the left, value aligned to the right (two columns)."""
        font = _font(size)
        bold = _font(size, value_bold)
        self.draw.text((self.margin, self.y), label, font=font, fill=color)
        width = self.draw.textlength(value, font=bold)
        self.draw.text((self.width - self.margin - width, self.y), value, font=bold,
                       fill=(30, 30, 40))
        self.y += int(size * 1.25) + 18

    def line(self):
        self.draw.line((self.margin, self.y, self.width - self.margin, self.y),
                       fill=(220, 220, 225), width=2)
        self.y += 24

    def bar(self, color, text, size=30):
        self.draw.rectangle((0, 0, self.width, 110), fill=color)
        self.draw.text((self.margin, 36), text, font=_font(size, True), fill=(255, 255, 255))
        self.y = 150

    def png(self):
        image = self.image.crop((0, 0, self.width, min(self.image.height, self.y + 60)))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


def _date_long(d):
    return "%d/%s/%d a las %02d:%02d." % (d.day, MONTHS[d.month - 1], d.year, d.hour, d.minute)


def mercado_pago(d):
    c = Canvas()
    c.text("mercado pago", 34, True, (0, 40, 120), gap=40)
    c.text("Comprobante de transferencia", 32, True)
    c.text(_date_long(d["date"]), 24, color=(110, 110, 130), gap=30)
    c.line()
    c.text("$ %s" % money(d["amount"]), 56, True, gap=8)
    c.text("Motivo: Varios", 24, color=(110, 110, 130), gap=30)
    c.line()
    c.text("Origen y destino", 22, gap=16)
    for name, cuit, cvu in ((d["payer_name"], d["payer_cuit"], d["payer_cvu"]),
                            (d["dest_name"], d["dest_cuit"], d["dest_cvu"])):
        c.text(name, 26, True, x=90, gap=6)
        c.text("Mercado Pago", 22, color=(110, 110, 130), x=90, gap=4)
        c.text("CUIT/CUIL: %s" % fmt_cuit(cuit), 22, color=(110, 110, 130), x=90, gap=4)
        c.text("CVU: %s" % cvu, 22, color=(110, 110, 130), x=90, gap=28)
    c.line()
    c.text("N.º de operación de Mercado Pago", 22)
    c.text(d["operation"], 26, True)
    return c.png(), {"destination": d["dest_cvu"]}


def uala(d):
    c = Canvas(background=(246, 247, 251))
    c.bar((50, 70, 230), "Ualá")
    c.text("Transferencia enviada", 30, True)
    c.text("$ %s" % money(d["amount"], "always"), 50, True, (50, 70, 230), gap=30)
    c.text("Para", 22, color=(120, 120, 140), gap=2)
    c.text(d["dest_name"], 26, True, gap=2)
    c.text("CUIT %s" % fmt_cuit(d["dest_cuit"]), 22, gap=2)
    c.text("CVU %s" % d["dest_cvu"], 22, gap=24)
    c.text("De", 22, color=(120, 120, 140), gap=2)
    c.text(d["payer_name"], 26, True, gap=2)
    c.text("CUIL %s" % fmt_cuit(d["payer_cuit"]), 22, gap=24)
    c.line()
    c.row("Fecha", "%02d/%02d/%d" % (d["date"].day, d["date"].month, d["date"].year))
    c.row("Hora", "%02d:%02d hs" % (d["date"].hour, d["date"].minute))
    c.row("Nº de operación", d["operation"])
    return c.png(), {"destination": d["dest_cvu"]}


def brubank(d):
    c = Canvas(background=(250, 248, 255))
    c.text("brubank", 34, True, (90, 40, 200), gap=36)
    c.text("¡Listo! Enviaste", 28, True, gap=4)
    c.text("$ %s" % money(d["amount"]), 54, True, gap=30)
    c.line()
    c.text("Para: %s" % d["dest_name"], 26, True, gap=4)
    c.text("CUIT/CUIL %s" % fmt_cuit(d["dest_cuit"]), 22, gap=4)
    c.text("CBU/CVU %s" % d["dest_cvu"], 22, gap=24)
    c.text("Desde: Caja de ahorro en pesos", 22, gap=4)
    c.text("Titular: %s" % d["payer_name"], 22, gap=4)
    c.text("CUIL %s" % fmt_cuit(d["payer_cuit"]), 22, gap=24)
    c.line()
    dt = d["date"]
    c.text("%d de %s de %d - %02d:%02d" % (dt.day, MONTHS[dt.month - 1], dt.year,
                                          dt.hour, dt.minute), 22)
    c.text("Nº de transacción %s" % d["operation"], 22)
    return c.png(), {"destination": d["dest_cvu"]}


def galicia(d):
    c = Canvas()
    c.bar((230, 120, 0), "Galicia")
    c.text("Comprobante de transferencia", 30, True, gap=24)
    dt = d["date"]
    c.row("Fecha", "%02d/%02d/%d" % (dt.day, dt.month, dt.year))
    c.row("Hora", "%02d:%02d" % (dt.hour, dt.minute))
    c.row("Importe", "$ %s" % money(d["amount"], "always"))
    c.line()
    c.row("Ordenante", d["payer_name"])
    c.row("CUIT ordenante", fmt_cuit(d["payer_cuit"]))
    c.row("Cuenta origen", "CA $ 4012-3 123-4")
    c.line()
    c.row("Beneficiario", d["dest_name"])
    c.row("CUIT/CUIL", fmt_cuit(d["dest_cuit"]))
    c.row("CBU destino", d["dest_cvu"], size=20)
    c.line()
    c.row("Número de operación", d["operation"])
    return c.png(), {"destination": d["dest_cvu"]}


def santander(d):
    c = Canvas()
    c.bar((220, 0, 0), "Santander")
    c.text("Transferencia realizada", 30, True, gap=24)
    c.row("Monto transferido", "$ %s" % money(d["amount"], "always"))
    dt = d["date"]
    c.row("Fecha y hora", "%02d/%02d/%d %02d:%02d" % (dt.day, dt.month, dt.year, dt.hour, dt.minute))
    c.line()
    c.text("Cuenta de origen", 22, color=(120, 120, 120), gap=2)
    c.text(d["payer_name"], 26, True, gap=2)
    c.text("CUIL %s" % fmt_cuit(d["payer_cuit"]), 22, gap=20)
    c.text("Cuenta de destino", 22, color=(120, 120, 120), gap=2)
    c.text(d["dest_name"], 26, True, gap=2)
    c.text("CUIT %s" % fmt_cuit(d["dest_cuit"]), 22, gap=2)
    c.text("Alias: %s" % d["dest_alias"], 22, gap=20)
    c.line()
    c.row("Número de comprobante", d["operation"])
    return c.png(), {"destination": d["dest_alias"]}


def bna(d):
    c = Canvas(background=(245, 250, 252))
    c.bar((0, 110, 160), "BNA+  Banco Nación")
    c.text("Transferencia exitosa", 30, True, gap=24)
    c.text("Importe: $ %s" % money(d["amount"], "always"), 30, True, gap=24)
    dt = d["date"]
    c.text("Fecha: %02d/%02d/%d  %02d:%02d" % (dt.day, dt.month, dt.year, dt.hour, dt.minute), 22)
    c.line()
    c.text("Destinatario: %s" % d["dest_name"], 22)
    c.text("CUIT: %s" % fmt_cuit(d["dest_cuit"]), 22)
    c.text("Alias: %s" % d["dest_alias"], 22)
    c.line()
    c.text("Ordenante: %s" % d["payer_name"], 22)
    c.text("CUIL: %s" % fmt_cuit(d["payer_cuit"]), 22)
    c.line()
    c.text("Nro. de control: %s" % d["operation"], 22)
    return c.png(), {"destination": d["dest_alias"]}


def bpn(d):
    c = Canvas()
    c.bar((0, 90, 60), "BPN  Banco Provincia del Neuquén")
    c.text("Constancia de transferencia", 28, True, gap=24)
    c.row("Monto", "$ %s" % money(d["amount"], "always"))
    dt = d["date"]
    c.row("Fecha", "%02d-%02d-%d %02d:%02d" % (dt.day, dt.month, dt.year, dt.hour, dt.minute))
    c.line()
    c.text("Origen", 22, color=(0, 90, 60), gap=2)
    c.text(d["payer_name"], 24, True, gap=2)
    c.text("CUIT/CUIL %s" % fmt_cuit(d["payer_cuit"]), 22, gap=20)
    c.text("Destino", 22, color=(0, 90, 60), gap=2)
    c.text(d["dest_name"], 24, True, gap=2)
    c.text("CUIT/CUIL %s" % fmt_cuit(d["dest_cuit"]), 22, gap=2)
    c.text("CBU %s" % d["dest_cvu"], 22, gap=20)
    c.line()
    c.row("Nro. de operación", d["operation"])
    return c.png(), {"destination": d["dest_cvu"]}


def naranja_x(d):
    c = Canvas(background=(255, 248, 240))
    c.text("Naranja X", 34, True, (250, 90, 0), gap=36)
    c.text("Transferiste", 26, gap=2)
    c.text("$ %s" % money(d["amount"]), 52, True, gap=28)
    c.text("a %s" % d["dest_name"], 26, True, gap=4)
    c.text("CVU %s" % d["dest_cvu"], 22, gap=4)
    c.text("CUIT %s" % fmt_cuit(d["dest_cuit"]), 22, gap=24)
    c.line()
    dt = d["date"]
    c.text("%d %s %d · %02d:%02d h" % (dt.day, MONTHS_SHORT[dt.month - 1], dt.year,
                                       dt.hour, dt.minute), 22)
    c.text("Desde tu cuenta: %s" % d["payer_name"], 22)
    c.text("CUIL %s" % fmt_cuit(d["payer_cuit"]), 22)
    c.text("ID de operación: %s" % d["operation"], 22)
    return c.png(), {"destination": d["dest_cvu"]}


LAYOUTS = {
    "Mercado Pago": mercado_pago,
    "Ualá": uala,
    "Brubank": brubank,
    "Banco Galicia": galicia,
    "Santander": santander,
    "Banco Nación": bna,
    "Banco Provincia del Neuquén": bpn,
    "Naranja X": naranja_x,
}


def render(bank, data):
    """(png bytes, expected fields) of one synthetic receipt."""
    png, extra = LAYOUTS[bank](data)
    expected = {
        "bank": bank,
        "amount": round(data["amount"], 2),
        "operation": data["operation"],
        "date": data["date"],
        "payer_name": data["payer_name"],
        "payer_cuit": data["payer_cuit"],
        "dest_cuit": data["dest_cuit"],
        "destination": extra["destination"],
    }
    return png, expected


def degrade(png, rng, level=1):
    """A phone photo or forwarded screenshot of the receipt.

    level 0: untouched; 1: resized to a phone width and saved as JPEG;
    2: also blurred and slightly rotated.
    """
    if level == 0:
        return png
    image = Image.open(io.BytesIO(png)).convert("RGB")
    width = rng.choice((540, 600, 720, 1080))
    height = int(image.height * width / image.width)
    image = image.resize((width, height), Image.LANCZOS)
    if level >= 2:
        image = image.filter(ImageFilter.GaussianBlur(rng.uniform(0.4, 1.0)))
        image = image.rotate(rng.uniform(-1.5, 1.5), expand=True, fillcolor=(235, 235, 235),
                             resample=Image.BICUBIC)
    buffer = io.BytesIO()
    image.save(buffer, format="JPEG", quality=rng.choice((55, 70, 85)))
    return buffer.getvalue()


def sample(seed=1):
    rng = random.Random(seed)
    return random_data(rng)


# ---------------------------------------------------------------- the driver


def fuel_ticket(amount, station="YPF Ruta 22"):
    """A synthetic fuel station ticket (png bytes) with its total."""
    c = Canvas(width=560, height=900)
    c.text(station, size=30, bold=True)
    c.text("CUIT 30-54668997-9", size=22)
    c.text("Ticket N° 0004-00012345", size=22)
    c.line()
    c.row("Nafta Super 42,5 L", money(amount))
    c.line()
    c.row("TOTAL", "$ " + money(amount), size=30)
    c.text("Gracias por su compra", size=20)
    return c.png()


def carrier_guide(number, amount=None, carrier="El Petróleo S.A."):
    """A synthetic bus carrier parcel guide (png bytes)."""
    c = Canvas(width=620, height=900)
    c.text(carrier, size=30, bold=True)
    c.text("Encomiendas · Terminal Neuquén", size=22)
    c.line()
    c.text("GUIA N° %s" % number, size=32, bold=True)
    c.row("Destino", "Zapala")
    c.row("Bultos", "2")
    if amount:
        c.row("Importe", "$ " + money(amount))
    return c.png()
