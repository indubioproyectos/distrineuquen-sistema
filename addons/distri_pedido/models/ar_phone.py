"""Argentine phone numbers for WhatsApp: pure helpers, no models.

Kept apart from pedido_whatsapp.py so other model files can import them
without changing the order in which the sale.order extensions are built
(that order decides which override runs first).
"""
import re

def only_digits(value):
    return re.sub(r"\D", "", str(value or ""))


# Argentine mobiles: area code (2 to 4 digits, without the 0) plus the local
# number (without the 15) always add up to 10 digits. WhatsApp numbers are
# +54 9 + those 10 digits; the 15 is never part of them.
AR_NATIONAL_LENGTH = 10
AR_PREFIX = "+549"
# Only to split a stored number back into area and number for display; the
# canonical number is the same whatever the split.
KNOWN_AREAS = ("2942", "2944", "2948", "2972", "2920", "2940", "2946", "299", "298", "11")


def ar_national(value):
    """The 10 national digits (area without 0 + number without 15) of an
    Argentine phone written in any usual way, or "" when it is not one or
    it is ambiguous.

    Country code, the 9 of mobiles and the leading 0 are dropped. A 15 is
    removed only when 12 digits remain and exactly one area length (2, 3 or
    4) has "15" right after it: "11 15 1515 1234" stays unresolved."""
    text = str(value or "").strip()
    if text.startswith("+") and not only_digits(text).startswith("54"):
        return ""  # another country
    number = only_digits(text).lstrip("0")
    if number.startswith("54") and len(number) >= 12:
        number = number[2:]
        if number.startswith("9") and len(number) in (11, 13):
            number = number[1:]
        number = number.lstrip("0")
    if len(number) == AR_NATIONAL_LENGTH:
        return number
    if len(number) == AR_NATIONAL_LENGTH + 2:
        cuts = [size for size in (2, 3, 4) if number[size:size + 2] == "15"]
        if len(cuts) == 1:
            size = cuts[0]
            return number[:size] + number[size + 2:]
    return ""


def ar_mobile(area, number):
    """Canonical WhatsApp (+549 + 10 digits) from an area code and a local
    number typed apart. Drops non-digits, the 0 of the area and the 15 of
    the number. Raises ValueError with the message for the user."""
    area = only_digits(area).lstrip("0")
    number = only_digits(number)
    if not area:
        raise ValueError("Falta el código de área (sin el 0). Ej.: 299.")
    if not number:
        raise ValueError("Falta el número (sin el 15).")
    if len(area) > 4 or len(area) < 2:
        raise ValueError("El código de área tiene de 2 a 4 dígitos, sin el 0. Ej.: 299, 11, 2942.")
    if number.startswith("15") and len(area) + len(number) == AR_NATIONAL_LENGTH + 2:
        number = number[2:]
    total = len(area) + len(number)
    if total != AR_NATIONAL_LENGTH:
        raise ValueError(
            "Código de área y número suman %s dígitos: tienen que ser 10 "
            "(ej.: 299 y 4222444)." % total)
    return AR_PREFIX + area + number


def split_ar_national(national):
    """(area, number) of 10 national digits, for display."""
    for area in KNOWN_AREAS:
        if national.startswith(area):
            return area, national[len(area):]
    return national[:3], national[3:]


def format_local(number):
    """4222444 -> 422-2444, 54222444 -> 5422-2444."""
    if len(number) <= 4:
        return number
    return "%s-%s" % (number[:-4], number[-4:])


def format_ar_mobile(area, number):
    """«+54 9 299 422-2444» for the given parts (already without 0 / 15)."""
    return "+54 9 %s %s" % (area, format_local(number))


def normalize_whatsapp(value):
    """The value to store in res.partner.distri_whatsapp.

    Argentine numbers become +549 + 10 digits; a number of another country
    written with its + is kept as + and digits. Returns False for an empty
    value and raises ValueError when the number cannot be read for sure."""
    text = str(value or "").strip()
    if not text:
        return False
    national = ar_national(text)
    if national:
        return AR_PREFIX + national
    digits = only_digits(text)
    if text.startswith("+") and not digits.startswith("54") and 8 <= len(digits) <= 15:
        return "+" + digits
    raise ValueError(
        "El WhatsApp «%s» no se puede leer con seguridad. Escribilo como +54 9, el código de "
        "área sin el 0 y el número sin el 15 (10 dígitos en total), por ejemplo "
        "+54 9 299 4222444." % text)


def phone_key(value):
    """The 10 national digits: the wa_id carries 54 and the 9 of mobiles,
    the typed phone usually does not, and may carry the 0 and the 15.
    Falls back to the last 10 digits for what is not a clear Argentine
    number."""
    national = ar_national(value)
    if national:
        return national
    number = only_digits(value)
    return number[-10:] if len(number) >= 8 else ""


def wa_digits(value):
    """Number for a wa.me link (Argentina when no country code)."""
    national = ar_national(value)
    if national:
        return "549" + national
    text = str(value or "").strip()
    number = only_digits(text).lstrip("0")
    if len(number) < 8:
        return ""
    if text.startswith("+") and not number.startswith("54"):
        return number  # another country, written with its +
    if not number.startswith("54"):
        number = "549" + number
    elif len(number) == 12:
        # 54 + area + number: WhatsApp needs the 9 of mobiles
        number = "549" + number[2:]
    return number
