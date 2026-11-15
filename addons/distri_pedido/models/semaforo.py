"""Risk semaphore of «Pedidos para revisar» (models/revisar.py, 19.0.1.85.0).

Pure helpers, no models: revisar.py gathers the facts of each order and
this file turns them into a colour and the reasons the manager reads.
Kept apart so the rules are easy to read and to test, and so no model
module has to import another one (sale.order overrides are built in
import order).
"""
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

AR_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
UTC = ZoneInfo("UTC")

RED, YELLOW, GREEN = "rojo", "amarillo", "verde"

SEMAFORO = [
    (RED, "🔴 Rojo"),
    (YELLOW, "🟡 Amarillo"),
    (GREEN, "🟢 Verde"),
]
# Sort key of the list: red first, then yellow, then green
SEMAFORO_ORDEN = {RED: 1, YELLOW: 2, GREEN: 3}

# A customer created less than this before the order is new
DIAS_CLIENTE_NUEVO = 30
# The order is unusually big above this many times the customer's average
FACTOR_IMPORTE = 2.0
# How many previous orders or invoices make the average
ULTIMOS = 10
# With fewer previous confirmed sale orders than this the average uses the
# invoices (Odoo and imported history): the lab has years of invoices and
# almost no sale orders yet
MIN_PEDIDOS_PARA_PROMEDIO = 3

MOTIVO_SIN_CLIENTE = "Sin cliente asignado"
MOTIVO_NUEVO = "Cliente nuevo"
MOTIVO_SIN_COMPRAS = "Sin compras anteriores"
MOTIVO_SIN_WHATSAPP = "Sin WhatsApp"
MOTIVO_IMPORTE = "Importe alto"
# 19.0.1.88.0: the WhatsApp with the code came from a username, no phone
MOTIVO_WA_SIN_TELEFONO = "WhatsApp sin teléfono"

SEPARADOR = " · "


def promedio_base(pedidos, facturas):
    """Amounts the average is made of: the previous confirmed sale orders
    when there are enough, otherwise the invoices, otherwise whatever there
    is. Both lists come newest first."""
    if len(pedidos) >= MIN_PEDIDOS_PARA_PROMEDIO or not facturas:
        base = pedidos
    else:
        base = facturas
    return list(base)[:ULTIMOS]


def evaluar(sin_cliente=False, cliente_nuevo=False, sin_compras=False,
            sin_whatsapp=False, importe=0.0, montos_previos=(), wa_sin_telefono=False):
    """(colour, reasons text) from the facts of one order.

    Red: no real customer, a new customer or one that never bought.
    Yellow: no WhatsApp on file, the order's WhatsApp came without a phone
    (WhatsApp username), or more than FACTOR_IMPORTE times the average of
    the previous orders or invoices. Green otherwise.
    """
    if sin_cliente:
        motivos = [MOTIVO_SIN_CLIENTE] + ([MOTIVO_WA_SIN_TELEFONO] if wa_sin_telefono else [])
        return RED, SEPARADOR.join(motivos)
    rojos, amarillos = [], []
    if wa_sin_telefono:
        amarillos.append(MOTIVO_WA_SIN_TELEFONO)
    if cliente_nuevo:
        rojos.append(MOTIVO_NUEVO)
    if sin_compras:
        rojos.append(MOTIVO_SIN_COMPRAS)
    if sin_whatsapp:
        amarillos.append(MOTIVO_SIN_WHATSAPP)
    montos = [m for m in montos_previos if m and m > 0]
    if montos:
        promedio = sum(montos) / len(montos)
        if importe > FACTOR_IMPORTE * promedio:
            amarillos.append(MOTIVO_IMPORTE)
    color = RED if rojos else YELLOW if amarillos else GREEN
    return color, SEPARADOR.join(rojos + amarillos)


def inicio_del_dia_utc(dias_atras=0, ahora=None):
    """Argentine midnight `dias_atras` days ago, naive UTC as Odoo stores it."""
    ahora = ahora or datetime.now(UTC)
    if ahora.tzinfo is None:
        ahora = ahora.replace(tzinfo=UTC)
    dia = ahora.astimezone(AR_TZ).date() - timedelta(days=dias_atras)
    inicio = datetime.combine(dia, time(0, 0), tzinfo=AR_TZ)
    return inicio.astimezone(UTC).replace(tzinfo=None)


def link_whatsapp(digitos):
    return "https://wa.me/%s" % digitos if digitos else ""
