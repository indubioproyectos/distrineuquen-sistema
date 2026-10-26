"""Collections board: Mercado Pago transfers confirmed by the administrator.

Design: docs cobranza/README.md, decisions 10 and 13, «Conciliación con
Mercado Pago» and «Comprobantes pendientes» (comprobante_pendiente.py).

- The Mercado Pago report «Todas las transacciones» (settlement report, CSV
  separated by semicolons) is imported by hand (button «Importar reporte») or
  picked up from the SFTP folder by a cron. Each operation is loaded once, by
  its SOURCE_ID. Only incoming money becomes a card: approved payments
  (SETTLEMENT, CREDIT) with a positive amount and a payment method. Yields
  (rows without a payment method) and own outgoing payments are skipped.
- Every incoming transfer becomes a draft collection with a proposed
  customer: by the payer's CUIT (the customer's own or one learned before),
  by a Mercado Pago payment link of an order, by a WhatsApp receipt (same
  operation number, or same amount and a close time) or by a promised
  transfer of the same amount. The proposal decides the column of the board:
  «Confirmado contra reporte MP», «Revisar» or «Asignar cliente».
- A receipt received by WhatsApp that the next report does not bring becomes
  a card of the last column, «Sin transferencia».
- Nothing is registered in a customer's account unless the report brings the
  transfer. A receipt waiting in «Esperando reporte MP» seen in the Mercado
  Pago app can only be authorized for delivery («Visto en MP: autorizar
  entrega», state «authorized»): no payment. The report that brings its
  operation proposes the income with that customer; one that covers its
  moment without it sends the card to «Sin transferencia», with an alert.
- Nothing reaches accounting until the administrator confirms: then the
  collection becomes a customer payment in the «Mercado Pago» bank journal,
  applied oldest invoice first. The amount comes from Mercado Pago and is
  never edited. The payer's CUIT and the receipt's phone are learned on the
  customer, and the customer's promised transfers are fulfilled.
- «Cierre diario»: per day, the incoming transfers of the report against
  what was confirmed in the journal (plus what was set aside). A difference
  raises an alert.

The server keeps no Mercado Pago key (decision 13): the report arrives by
SFTP. Creating the SFTP user that can only drop files is a server task
outside this module (see README).
"""
import base64
import csv
import io
import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.modules import module as odoo_module
from odoo.tools.pdf import PdfReader

from .ocr_comprobante import (
    CONFIDENCE_HIGH, CONFIDENCE_LOW, OWN_ACCOUNT_DEFAULT, PARAM_OWN_ACCOUNT, local_to_utc,
    ocr_image, own_account_values, parse_receipt, pdf_images, read_image,
)
from .reparto import _money
from .reparto_cliente import AR_TZ, friendly_case, today_ar

_logger = logging.getLogger(__name__)

ADMIN_GROUP = "distri_pedido.grupo_administracion"

MP_JOURNAL_CODE = "MP"
MP_JOURNAL_NAME = "Mercado Pago"

PARAM_FOLDER = "distri_pedido.mp_carpeta_reportes"
DEFAULT_FOLDER = "/var/lib/odoo/mercadopago/entrada"
DONE_FOLDER = "procesados"
ERROR_FOLDER = "errores"
MAX_REPORT_BYTES = 20 * 1024 * 1024

REQUIRED_COLUMNS = ("SOURCE_ID", "TRANSACTION_TYPE", "TRANSACTION_AMOUNT", "TRANSACTION_DATE")
INCOMING_TYPES = ("SETTLEMENT", "CREDIT")
REVERSAL_TYPES = ("REFUND", "CHARGEBACK", "DISPUTE")

# Amount and time window of a receipt against a transfer: the photo usually
# comes minutes after the transfer, sometimes the next morning.
AMOUNT_TOLERANCE = 1.0
# «Reporte completo»: the report reaches 23:59 of the day
REPORT_DAY_END_MARGIN = timedelta(minutes=1)
RECEIPT_BEFORE = timedelta(hours=2)
RECEIPT_AFTER = timedelta(hours=24)
# When the receipt carries its own date and time (read from the photo), its
# transfer is looked for around THAT moment, whatever day the photo arrived:
# customers send old receipts days or months later. 99 % of the receipts of
# the Mercado Pago app, Naranja X, BNA, Galicia, BBVA and Ualá are stamped
# within 2 minutes of the transfer.
RECEIPT_DATE_WINDOW = timedelta(days=1)
RECEIPT_TIME_CLOSE = timedelta(minutes=2)
# A transfer claimed at a delivery can be paid a while before the visit.
PROMISE_BEFORE = timedelta(hours=24)
PROMISE_AFTER = timedelta(hours=72)
PROMISE_NO_RECEIPT = timedelta(hours=2)
PROMISE_COLLECTION = timedelta(hours=48)
# A text message with the order number next to a photo identifies it.
RECEIPT_TEXT_WINDOW = timedelta(minutes=30)
SELLER_TEXT_WINDOW = timedelta(minutes=3)
# the seller's note that names the customer may come a while after the photo
SELLER_NOTE_WINDOW = timedelta(minutes=10)
# receipt fields shown on its «Esperando reporte MP» card
VERIFY_CARD_FIELDS = {"distri_partner_id", "distri_amount", "distri_operation",
                      "distri_payer_name", "distri_payer_cuit", "distri_receipt_state",
                      "distri_is_receipt", "distri_partner_source", "distri_conflict"}
# Receipt states that never wait for the report nor register anything.
RECEIPT_CLOSED_STATES = ("dismissed", "duplicate", "registered", "outgoing")
TWO_CUSTOMERS = "Mismo comprobante enviado para dos clientes: %s y %s"
_CUIT_RE = re.compile(r"\b(\d{2})[-. ]?(\d{8})[-. ]?(\d)\b")

# Columns of the board, named by what the administrator has to do, from the
# most likely valid to the one that needs most attention. The order of the
# selection is the order of the kanban. The keys are stored (field `column`);
# only the labels are shown.
COLUMNS = [
    ("confirm", "Confirmado contra reporte MP"),
    ("verify", "Esperando reporte MP"),
    ("review", "Revisar"),
    ("unidentified", "Asignar cliente"),
    ("no_money", "Sin transferencia"),
]
# The columns that need a person: highlighted on the board and, when a card
# waits there too long, an activity for the administration (`_cron_alerts`).
PRIORITY_COLUMNS = ("unidentified", "no_money")
PARAM_ALERT_HOURS = "distri_pedido.cobros_alerta_horas"
DEFAULT_ALERT_HOURS = 24
ALERT_SUMMARY = "Cobro sin resolver en el tablero"
REVERSAL_SUMMARY = "Devolución o contracargo de un cobro confirmado"

# A short lesson per column (what it is, why the card is there, what to do):
# the «i» of the column header (static/src/js/cobros_board.js) reads it
# through `distri_board_columns`.
COLUMN_HELP = {
    "confirm": "La transferencia está en el reporte de Mercado Pago y el pagador ya es conocido: "
               "su CUIT pagó antes para este cliente (o vino por el link de pago del pedido, o "
               "se autorizó a mano la entrega con ese número de operación). Si además llegó el "
               "comprobante por WhatsApp, también coincide. Solo hace falta una mirada rápida: "
               "cliente e importe. Por eso se pueden confirmar todas juntas: al confirmar se "
               "registra el pago.",
    "verify": "El comprobante llegó por WhatsApp y espera el reporte de Mercado Pago. Desde acá "
              "no se registra ningún pago: cuando se cargue el reporte, la transferencia pasa "
              "sola a «Confirmado contra reporte MP» (o a «Revisar», si el pagador todavía no es "
              "conocido) y se registra al confirmarla. Si hay que entregar mercadería antes, "
              "buscá la operación en la app de Mercado Pago y tocá «Visto en MP: autorizar "
              "entrega»: la tarjeta queda «Autorizado para entregar · falta el reporte», sin "
              "tocar el saldo del cliente.",
    "review": "La transferencia está en el reporte de Mercado Pago, pero todavía no se pudo "
              "establecer con seguridad de qué cliente es: el CUIT no es conocido, hay varios "
              "clientes posibles, el cliente salió solo de la foto o del texto del vendedor, o "
              "el importe no coincide. Necesita más atención: verificá el cliente antes de "
              "confirmar. Al confirmar se registra el pago.",
    "unidentified": "Transferencias del reporte sin ninguna pista de quién pagó: ni CUIT "
                    "conocido, ni comprobante, ni pedido. Elegí el cliente: el sistema aprende "
                    "ese CUIT y la próxima vez lo propone solo. Al confirmar se registra el pago.",
    "no_money": "Comprobantes sin transferencia: el reporte que cubre ese momento no trae la "
                "plata. Puede ser una foto recibida por WhatsApp, o una entrega autorizada a mano "
                "(«Mercadería autorizada sin transferencia»: la mercadería ya salió, es lo más "
                "urgente). Puede ser una acreditación posterior (transferencia programada o "
                "demorada), un comprobante enviado a otra cuenta o un comprobante falso. Desde "
                "acá no se registra ningún pago: consultá con el vendedor y con el gerente antes "
                "de descartarlo; si la plata llega en otro reporte, se resuelve sola.",
}

PROPOSALS = [
    ("cuit", "CUIT conocido"),
    ("link", "Link de pago del pedido"),
    ("operation", "Número de operación del comprobante"),
    ("photo", "Foto por WhatsApp"),
    ("promise", "Comprobante pendiente del reparto"),
    ("several", "CUIT de varios clientes"),
    ("manual", "Asignado por el administrador"),
    ("verified", "Verificado a mano en Mercado Pago"),
    ("authorized", "Entrega autorizada a mano"),
    ("none", "Sin propuesta"),
]
# Columns an income of the report can land in (the summary of an import).
INCOME_COLUMNS = ("confirm", "review", "unidentified")
# before 19.0.1.21.0 «Verificado en MP» registered the payment at once
HAND_VERIFIED_MISSING = ("Pago registrado a mano con el sistema anterior, sin transferencia en el "
                         "reporte de Mercado Pago: revisalo")
HAND_VERIFIED_MISSING_HELP = (
    "Antes, «Verificado en MP» registraba el pago en el momento. Este pago quedó registrado en la "
    "cuenta del cliente, pero el reporte que cubre ese momento no trae la transferencia. Buscá la "
    "operación en la app de Mercado Pago: si llegó en otro reporte, tocá «Revisado»; si no "
    "existe, tocá «Deshacer» para anular el pago y avisá al gerente.")
# «Visto en MP: autorizar entrega»: nothing is registered until the report
# brings the transfer. The label is the badge of the card (views/cobros.xml).
AUTHORIZED_LABEL = "Autorizado para entregar · falta el reporte"
AUTHORIZED_MISSING = "Mercadería autorizada sin transferencia en el reporte de Mercado Pago"
AUTHORIZED_MISSING_HELP = (
    "Se autorizó entregar la mercadería al ver la transferencia en la app de Mercado Pago, pero "
    "el reporte que cubre ese momento no la trae. No hay ningún pago registrado. Buscá la "
    "operación en la app y avisá al gerente: si llega en un reporte posterior, su ingreso queda "
    "propuesto con este cliente para confirmarlo; si no existe, reclamá el pago al cliente.")
AUTHORIZED_WAITING = "Mercadería autorizada y todavía sin reporte de Mercado Pago"
AUTHORIZED_WAITING_HELP = (
    "Se autorizó entregar la mercadería al ver la transferencia en la app de Mercado Pago, pero "
    "todavía no se importó un reporte que traiga esa operación. No hay ningún pago registrado. "
    "Importá el reporte más reciente: si trae la operación, su ingreso queda propuesto con este "
    "cliente para confirmarlo; si no la trae, la tarjeta pasa a «Sin transferencia».")

IGNORE_REASONS = [
    ("own", "Movimiento propio de la empresa"),
    ("not_customer", "No es el pago de un cliente"),
    ("returned", "Se le devolvió la plata"),
    ("receipt_error", "Foto: error o comprobante repetido"),
    ("receipt_late", "Foto: la plata llegó en otro reporte"),
    ("receipt_fake", "Foto: comprobante falso"),
    ("not_receipt", "No es un comprobante"),
    ("other", "Otro motivo"),
]

_AMOUNT_RE = re.compile(r"\$\s*([0-9]{1,3}(?:[.\s][0-9]{3})+(?:,[0-9]{1,2})?|[0-9]+(?:,[0-9]{1,2})?)")
_OPERATION_RE = re.compile(
    r"(?:operaci[oó]n|transacci[oó]n|comprobante|referencia|n[uú]mero)\D{0,40}?(\d{9,16})",
    re.IGNORECASE)
_ORDER_RE = re.compile(r"\bS\d{5,}\b")
_RECEIPT_WORDS = re.compile(r"comprobante|transfer|pag(?:o|ué|ue)|mercado\s*pago|cvu|alias",
                            re.IGNORECASE)


_GENERATED_RE = re.compile(r"(20\d{2})-(\d{2})-(\d{2})-(\d{2})(\d{2})(\d{2})")


def report_generated_at(filename):
    """«settlement_v2-100000001-manual-2026-09-24-235946.csv» -> the moment
    Mercado Pago generated it (Argentina time) as naive UTC, or False."""
    match = _GENERATED_RE.search(filename or "")
    if not match:
        return False
    try:
        local = datetime(*(int(g) for g in match.groups()), tzinfo=AR_TZ)
    except ValueError:
        return False
    return local.astimezone(timezone.utc).replace(tzinfo=None)


def _ar_time(moment):
    """HH:MM in Argentina of a naive UTC datetime."""
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%H:%M del %d/%m")


def _check_admin(env):
    if not (env.su or env.user.has_group(ADMIN_GROUP)):
        raise UserError("Solo administración y gerencia confirman cobros.")


def digits(value):
    return re.sub(r"\D", "", str(value or ""))


def own_account(env):
    """The company's own CUIT, holder, CVU/CBU and alias, normalized
    (ir.config_parameter distri_pedido.cuenta_propia, comma separated)."""
    text = env["ir.config_parameter"].sudo().get_param(PARAM_OWN_ACCOUNT)
    return own_account_values(text or OWN_ACCOUNT_DEFAULT)


def operation_close(read, source):
    """The operation number read from a photo is the transfer's, allowing
    one OCR slip: one digit wrong, or one digit dropped or added at the end
    (Mercado Pago ids have 11-12 digits). Together with the same amount it
    counts as an operation match."""
    read, source = digits(read), digits(source)
    if not (read and source):
        return False
    if read == source:
        return True
    if not 10 <= len(read) <= 13 or not 10 <= len(source) <= 13:
        return False
    if len(read) == len(source):
        return sum(a != b for a, b in zip(read, source)) == 1
    if abs(len(read) - len(source)) == 1:
        return read.startswith(source) or source.startswith(read)
    return False


def format_cuit(value):
    value = digits(value)
    if len(value) == 11:
        return "%s-%s-%s" % (value[:2], value[2:10], value[10:])
    return value


def parse_mp_datetime(value):
    """'2026-09-25T11:34:47.000-03:00' -> naive UTC. The report uses the
    zone it was configured with (GMT-03 in the design, GMT-04 by default)."""
    value = (value or "").strip()
    if not value:
        return False
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=AR_TZ)
    return moment.astimezone(timezone.utc).replace(tzinfo=None)


def local_day(moment):
    """Argentine calendar day of a naive UTC datetime."""
    if not moment:
        return False
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).date()


def local_hour(moment):
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%H:%M")


def _elapsed(delta):
    """'10 min', '3 h', '2 días': how long a card has been waiting."""
    minutes = max(int(delta.total_seconds() // 60), 0)
    if minutes < 60:
        return "%s min" % minutes
    if minutes < 48 * 60:
        return "%s h" % (minutes // 60)
    return "%s días" % (minutes // (24 * 60))


def local_when(moment):
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%d/%m %H:%M")


def parse_amount_ar(text):
    """'$ 45.000', '$45000,50' -> float. Only amounts written after a '$'."""
    found = []
    for match in _AMOUNT_RE.finditer(text or ""):
        raw = match.group(1).replace(" ", "").replace(".", "").replace(",", ".")
        try:
            found.append(float(raw))
        except ValueError:
            continue
    found = [a for a in found if a > 0]
    return max(found) if found else 0.0


def parse_operation(text):
    match = _OPERATION_RE.search(text or "")
    return match.group(1) if match else ""


# «Todas las transacciones» downloaded by hand from Mercado Pago comes as .xlsx
# with Spanish headers (settlement_v2-…xlsx); the scheduled report uses the
# technical ones. Both end up as the technical names (19.0.1.38.0).
SPANISH_COLUMNS = {
    "ID DE OPERACIÓN EN MERCADO PAGO": "SOURCE_ID",
    "TIPO DE OPERACIÓN": "TRANSACTION_TYPE",
    "VALOR DE LA COMPRA": "TRANSACTION_AMOUNT",
    "FECHA DE ORIGEN": "TRANSACTION_DATE",
    "TIPO DE MEDIO DE PAGO": "PAYMENT_METHOD_TYPE",
    "MEDIO DE PAGO": "PAYMENT_METHOD",
    "PAGADOR": "PAYER_NAME",
    "TIPO DE IDENTIFICACIÓN DEL PAGADOR": "PAYER_ID_TYPE",
    "NÚMERO DE IDENTIFICACIÓN DEL PAGADOR": "PAYER_ID_NUMBER",
    "DETALLE DE LA VENTA": "SALE_DETAIL",
    "DETALLE DEL SEGMENTO": "SEGMENT_DETAIL",
    "COMISIONES + IVA": "FEE_AMOUNT",
}
# TIPO DE OPERACIÓN of the Spanish file -> TRANSACTION_TYPE
SPANISH_TYPES = (("pago aprobado", "SETTLEMENT"), ("devoluci", "REFUND"),
                 ("contracargo", "CHARGEBACK"), ("disputa", "DISPUTE"), ("reclamo", "DISPUTE"))


def _cell_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer() and abs(value) >= 1e6:
        return str(int(value))  # an operation number stored as a number
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value).strip()


def _xlsx_rows(data):
    """Rows of the first sheet of an .xlsx, the first row being the header."""
    try:
        import openpyxl
        book = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception:
        raise UserError("No se pudo leer el archivo .xlsx.") from None
    sheet = book.worksheets[0]
    header, rows = None, []
    for values in sheet.iter_rows(values_only=True):
        cells = [_cell_text(v) for v in values]
        if header is None:
            if any(cells):
                header = [c.upper() for c in cells]
            continue
        if any(cells):
            rows.append(dict(zip(header, cells)))
    book.close()
    return header or [], rows


def _technical_row(row):
    """A row with Spanish headers, as the technical ones."""
    out = {}
    for key, value in row.items():
        name = SPANISH_COLUMNS.get(key)
        if name:
            out[name] = value
    kind = (row.get("TIPO DE OPERACIÓN") or "").lower()
    out["TRANSACTION_TYPE"] = next((t for word, t in SPANISH_TYPES if word in kind),
                                   kind.upper().replace(" ", "_"))
    return out


def read_report(data):
    """Rows of a Mercado Pago report as dicts. Raises UserError if the file
    is not one."""
    if isinstance(data, bytes) and data[:2] == b"PK":
        header, rows = _xlsx_rows(data)
        if "SOURCE_ID" not in header and "ID DE OPERACIÓN EN MERCADO PAGO" in header:
            rows = [_technical_row(r) for r in rows]
            header = list(REQUIRED_COLUMNS)
        missing = [c for c in REQUIRED_COLUMNS if c not in header]
        if missing:
            raise UserError("No parece un reporte «Todas las transacciones» de Mercado Pago: "
                            "faltan las columnas %s." % ", ".join(missing))
        return rows
    if isinstance(data, bytes):
        text = None
        for encoding in ("utf-8-sig", "latin-1"):
            try:
                text = data.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    else:
        text = data
    if not text or not text.strip():
        raise UserError("El archivo está vacío.")
    first = text.splitlines()[0]
    delimiter = ";" if first.count(";") >= first.count(",") else ","
    reader = csv.DictReader(io.StringIO(text), delimiter=delimiter)
    header = [(h or "").strip().upper() for h in (reader.fieldnames or [])]
    missing = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing:
        raise UserError("No parece un reporte «Todas las transacciones» de Mercado Pago: "
                        "faltan las columnas %s." % ", ".join(missing))
    reader.fieldnames = header
    rows = []
    for row in reader:
        rows.append({k: (v or "").strip() for k, v in row.items() if k})
    return rows


# ====================================================================== identity


class DistriPayerIdentity(models.Model):
    """A CUIT or phone that belongs to a customer.

    A shop pays from up to three accounts with different holders, and sends
    receipts from several phones. Each one is assigned once and from then on
    proposes the customer by itself.
    """

    _name = "distri.payer.identity"
    _description = "CUIT o teléfono de un cliente"
    _order = "partner_id, kind, id"
    _rec_name = "value"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True,
                                 ondelete="cascade", index=True)
    kind = fields.Selection([("cuit", "CUIT de quien paga"), ("phone", "Teléfono")],
                            string="Tipo", required=True)
    value = fields.Char(string="Número", required=True, index=True,
                        help="Solo dígitos. Teléfono: los últimos 10 dígitos.")
    holder = fields.Char(string="Titular")
    learned_by_id = fields.Many2one("res.users", string="Asignó", readonly=True,
                                    default=lambda self: self.env.user)
    learned_at = fields.Datetime(string="Asignado el", readonly=True,
                                 default=fields.Datetime.now)

    _value_unique = models.Constraint("unique(kind, value)",
                                      "Ese número ya está asignado a un cliente.")

    @staticmethod
    def _normalize(kind, value):
        value = digits(value)
        if kind == "phone":
            return value[-10:]
        return value

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            vals["value"] = self._normalize(vals.get("kind"), vals.get("value"))
            if vals.get("partner_id"):
                vals["partner_id"] = self.env["res.partner"].browse(
                    vals["partner_id"]).commercial_partner_id.id
        identities = super().create(vals_list)
        # also when typed by hand on the customer («Cobros» tab): the pending
        # cards of that CUIT or phone get the customer without a new report
        identities._repropose()
        return identities

    def write(self, vals):
        if "value" in vals and "kind" not in vals and len(self) == 1:
            vals = dict(vals, value=self._normalize(self.kind, vals["value"]))
        result = super().write(vals)
        if {"value", "kind", "partner_id"} & set(vals):
            self._repropose()
        return result

    @api.model
    def _learn(self, kind, value, partner, holder=""):
        """Remember a CUIT/phone for a customer. Never moves one already assigned."""
        value = self._normalize(kind, value)
        if len(value) < 8 or not partner:
            return self.browse()
        found = self.sudo().search([("kind", "=", kind), ("value", "=", value)], limit=1)
        if found:
            return found
        return self.sudo().create({"kind": kind, "value": value,
                                   "partner_id": partner.id, "holder": holder or False})

    def _repropose(self):
        """Assigned once: the other pending cards of that CUIT or phone get
        the customer right away."""
        Collection = self.env["distri.mp.collection"].sudo()
        Message = self.env["distri.whatsapp.message"].sudo()
        for identity in self:
            if identity.kind == "cuit":
                Collection.search([("state", "=", "draft"), ("kind", "=", "income"),
                                   ("payer_cuit", "=", identity.value),
                                   ("proposal", "!=", "manual")])._propose()
                continue
            for receipt in Message.search([("distri_is_receipt", "=", True),
                                           ("distri_partner_id", "=", False),
                                           ("distri_forwarded_by_id", "=", False),
                                           ("wa_id", "like", "%" + identity.value)]):
                receipt.write({"distri_partner_id": identity.partner_id.id,
                               "distri_partner_source": "Teléfono de WhatsApp del cliente"})
                if receipt.distri_collection_id.state == "draft":
                    receipt.distri_collection_id._propose()
                cards = Collection.search([("kind", "=", "receipt"), ("state", "=", "draft"),
                                           ("receipt_id", "=", receipt.id)])
                cards.write({"partner_id": identity.partner_id.id, "proposal": "photo"})


class ResPartnerCobros(models.Model):
    _inherit = "res.partner"

    distri_payer_ids = fields.One2many("distri.payer.identity", "partner_id",
                                       string="CUIT y teléfonos para cobros")

    @api.model
    def _distri_partners_by_cuit(self, cuit):
        """Customers of a payer's CUIT: learned ones, then the customer's own."""
        cuit = digits(cuit)
        if len(cuit) < 8:
            return self.browse()
        identities = self.env["distri.payer.identity"].sudo().search(
            [("kind", "=", "cuit"), ("value", "=", cuit)])
        if identities:
            return identities.partner_id.commercial_partner_id
        self.env.cr.execute("""
            SELECT id FROM res_partner
             WHERE active AND regexp_replace(coalesce(vat, ''), '\\D', '', 'g') = %s
        """, [cuit])
        partners = self.sudo().browse([r[0] for r in self.env.cr.fetchall()])
        return partners.commercial_partner_id.filtered(lambda p: not p.distri_generic_partner)

    @api.model
    def _distri_partners_by_phone(self, phone):
        """Customers of a WhatsApp number: learned phones, then the contact's
        WhatsApp or phone, compared on the last 10 digits (the wa_id carries
        the country code and the 9 of mobiles)."""
        number = digits(phone)[-10:]
        if len(number) < 8:
            return self.browse()
        identities = self.env["distri.payer.identity"].sudo().search(
            [("kind", "=", "phone"), ("value", "=", number)])
        if identities:
            return identities.partner_id.commercial_partner_id
        self.env.cr.execute("""
            SELECT id FROM res_partner
             WHERE active AND (
                   right(regexp_replace(coalesce(distri_whatsapp, ''), '\\D', '', 'g'), 10) = %s
                OR right(regexp_replace(coalesce(phone, ''), '\\D', '', 'g'), 10) = %s)
        """, [number, number])
        partners = self.sudo().browse([r[0] for r in self.env.cr.fetchall()])
        return partners.commercial_partner_id.filtered(lambda p: not p.distri_generic_partner)

    @api.model
    def _distri_partners_by_text(self, text):
        """Customers named in a seller's note: a CUIT, else a line that is
        (part of) exactly one customer's name."""
        text = text or ""
        for match in _CUIT_RE.finditer(text):
            partners = self._distri_partners_by_cuit("".join(match.groups()))
            if partners:
                return partners
        Partner = self.sudo()
        base = [("distri_generic_partner", "=", False), ("active", "=", True),
                ("parent_id", "=", False),
                ("id", "not in", self.env["res.company"].sudo().search([]).partner_id.ids)]
        for line in text.splitlines():
            line = re.sub(r"(?i)^\s*(cliente|titular|es de|de)\s*[:\-]?\s*", "", line)
            line = " ".join(re.sub(r"[^\w\s.&-]", " ", line).split())
            if len(line) < 4 or line.replace(" ", "").isdigit():
                continue
            partners = Partner.search(base + [("name", "ilike", line)], limit=2)
            if len(partners) == 1:
                return partners.commercial_partner_id
        return self.browse()

    def _distri_has_known_cuit(self):
        self.ensure_one()
        partner = self.commercial_partner_id
        return bool(len(digits(partner.vat)) >= 8 or self.env["distri.payer.identity"].sudo(
        ).search_count([("partner_id", "=", partner.id), ("kind", "=", "cuit")]))


# ====================================================================== journal


class AccountJournalCobros(models.Model):
    _inherit = "account.journal"

    @api.model
    def _distri_mp_journal(self, company=None):
        """«Mercado Pago» bank journal of the company, created the first time.

        The Mercado Pago account is a bank account of its own: the transfers
        confirmed on the board enter here, apart from «Banco» (BNK1).
        """
        company = company or self.env.company
        Journal = self.sudo()
        journal = Journal.search([("code", "=", MP_JOURNAL_CODE),
                                  ("company_id", "=", company.id)], limit=1)
        if not journal:
            journal = Journal.create({
                "name": MP_JOURNAL_NAME,
                "code": MP_JOURNAL_CODE,
                "type": "bank",
                "company_id": company.id,
            })
            # The report is the account statement and the administrator
            # confirms against it: the payment enters the Mercado Pago account
            # itself (state "paid"), not «Recibos pendientes».
            journal.inbound_payment_method_line_ids.write(
                {"payment_account_id": journal.default_account_id.id})
        return journal

    @api.model
    def _distri_ensure_mp_journal(self):
        """Called on module update, in companies with accounting set up."""
        for company in self.env["res.company"].sudo().search([]):
            if self.sudo().search_count([("company_id", "=", company.id),
                                         ("type", "=", "bank")]):
                self._distri_mp_journal(company)


# ====================================================================== report


class DistriMpReport(models.Model):
    _name = "distri.mp.report"
    _description = "Reporte de Mercado Pago"
    _order = "imported_at desc, id desc"

    name = fields.Char(string="Archivo", required=True, readonly=True)
    source = fields.Selection([("manual", "Subido a mano"), ("sftp", "Carpeta SFTP")],
                              string="Origen", required=True, default="manual", readonly=True)
    imported_at = fields.Datetime(string="Importado el", default=fields.Datetime.now,
                                  readonly=True)
    imported_by_id = fields.Many2one("res.users", string="Importó", readonly=True,
                                     default=lambda self: self.env.user)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    attachment_id = fields.Many2one("ir.attachment", string="Archivo original", readonly=True)
    period_start = fields.Datetime(string="Desde", readonly=True)
    period_end = fields.Datetime(string="Hasta", readonly=True)
    generated_at = fields.Datetime(
        string="Generado el", readonly=True,
        help="La hora en que Mercado Pago generó el reporte, según el nombre del archivo "
             "(…-2026-09-24-235946.csv).")
    covers_until = fields.Datetime(
        string="Cubre hasta", compute="_compute_covers_until", store=True,
        help="La última operación del reporte o la hora en que se generó, la que sea más tarde.")
    row_count = fields.Integer(string="Filas", readonly=True)
    new_count = fields.Integer(string="Transferencias nuevas", readonly=True)
    duplicate_count = fields.Integer(string="Ya cargadas", readonly=True)
    skipped_count = fields.Integer(
        string="Otras filas", readonly=True,
        help="Rendimientos, pagos propios y otras operaciones que no son cobros.")
    reversal_count = fields.Integer(string="Devoluciones y contracargos", readonly=True)
    incoming_total = fields.Monetary(string="Total de transferencias", readonly=True)
    no_money_count = fields.Integer(string="Fotos sin transferencia", readonly=True)
    # before 19.0.1.25.0 the report expired the promised transfers; kept
    # only so old reports keep their value (not shown)
    overdue_count = fields.Integer(string="Vencidas (anterior)", readonly=True)
    collection_ids = fields.One2many("distri.mp.collection", "report_id", string="Cobros")
    summary = fields.Text(string="Resumen", readonly=True)

    # ------------------------------------------------------------ import

    @api.depends("period_end", "generated_at")
    def _compute_covers_until(self):
        for report in self:
            ends = [d for d in (report.period_end, report.generated_at) if d]
            report.covers_until = max(ends) if ends else False

    def _import_file(self, filename, data, source="manual", date_from=None, date_to=None):
        """Loads one report. Returns the distri.mp.report record. With a date
        range, only the operations of those Argentine days."""
        rows = read_report(data)
        if date_from or date_to:
            def inside(row):
                day = local_day(parse_mp_datetime(row.get("TRANSACTION_DATE")))
                return bool(day) and (not date_from or day >= date_from) and \
                    (not date_to or day <= date_to)
            rows = [r for r in rows if inside(r)]
        company = self.env.company
        report = self.sudo().create({
            "name": filename or "reporte.csv",
            "source": source,
            "company_id": company.id,
            "generated_at": report_generated_at(filename),
        })
        report.attachment_id = self.env["ir.attachment"].sudo().create({
            "name": filename or "reporte.csv",
            "raw": data if isinstance(data, bytes) else data.encode(),
            "mimetype": "text/csv" if not (isinstance(data, bytes) and data[:2] == b"PK")
            else "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            "res_model": report._name,
            "res_id": report.id,
        })
        report._load_rows(rows)
        return report.sudo(False)

    def _load_rows(self, rows):
        self.ensure_one()
        Collection = self.env["distri.mp.collection"].sudo()
        Outgoing = self.env["distri.mp.outgoing"].sudo()
        dates = [d for d in (parse_mp_datetime(r.get("TRANSACTION_DATE")) for r in rows) if d]
        stats = {"new": 0, "dup": 0, "skip": 0, "rev": 0, "total": 0.0}
        new = Collection.browse()
        refilled = Collection.browse()
        reversals = []
        for row in rows:
            source = digits(row.get("SOURCE_ID"))
            kind = (row.get("TRANSACTION_TYPE") or "").upper()
            try:
                amount = float((row.get("TRANSACTION_AMOUNT") or "0").replace(",", "."))
            except ValueError:
                amount = 0.0
            method_type = row.get("PAYMENT_METHOD_TYPE") or ""
            if not source:
                stats["skip"] += 1
                continue
            if kind in REVERSAL_TYPES:
                stats["rev"] += 1
                # applied after the incomes: the refund may come before its
                # transfer in the same file
                reversals.append((source, kind, amount))
                continue
            if kind not in INCOMING_TYPES or amount <= 0 or not method_type:
                stats["skip"] += 1
                if amount < 0 and kind in INCOMING_TYPES:
                    # the company's own outgoing transfer: a receipt of it is
                    # a refund, never a customer's payment
                    Outgoing._record(source, self._row_values(row, source, amount))
                continue
            stats["total"] += amount
            values = self._row_values(row, source, amount)
            existing = Collection.search([("source_id", "=", source)], limit=1)
            if existing:
                stats["dup"] += 1
                if existing.state == "draft" and existing._refill(values):
                    refilled |= existing
                continue
            new |= Collection.create(dict(values, report_id=self.id,
                                          company_id=self.company_id.id))
            stats["new"] += 1
        for source, kind, amount in reversals:
            Collection.search([("source_id", "=", source)])._flag_reversal(kind, amount)
        # a delivery authorized by hand gives its customer to its income (and
        # a receipt paid by hand before 19.0.1.21.0 is never paid again)
        (new | refilled)._match_hand_verified()
        (new | refilled)._propose()
        # payers remembered as mayoristas: out of the board (mayoristas.py)
        (new | refilled)._distri_apply_wholesale_rules()
        # the period the report covers: from its first operation to its last
        # one or to the moment Mercado Pago generated it, whichever is later
        # (the same `covers_until` the daily close uses)
        start = min(dates) if dates else False
        until = max([d for d in (max(dates) if dates else False, self.generated_at) if d],
                    default=False)
        flagged = self._flag_hand_verified_missing(start, until)
        no_money = self._receipts_without_money(start, until)
        days = {d for d in (new | refilled).mapped("date_day") if d}
        self.env["distri.mp.day"].sudo()._ensure_days(days, self.company_id)
        self.write({
            "period_start": min(dates) if dates else False,
            "period_end": max(dates) if dates else False,
            "row_count": len(rows),
            "new_count": stats["new"],
            "duplicate_count": stats["dup"],
            "skipped_count": stats["skip"],
            "reversal_count": stats["rev"],
            "incoming_total": stats["total"],
            "no_money_count": len(no_money),
        })
        self.summary = self._summary_text(new, flagged)

    def _summary_text(self, new, flagged=None):
        columns = dict(COLUMNS)
        parts = ["%s filas: %s transferencias nuevas, %s ya cargadas, %s otras filas." % (
            self.row_count, self.new_count, self.duplicate_count, self.skipped_count)]
        for key, label in [c for c in COLUMNS if c[0] in INCOME_COLUMNS]:
            count = len(new.filtered(lambda c: c.column == key))
            if count:
                parts.append("%s: %s." % (label, count))
        if self.no_money_count:
            parts.append("%s: %s." % (columns["no_money"], self.no_money_count))
        authorized = (flagged or self.env["distri.mp.collection"]).filtered(
            lambda c: c.state == "authorized")
        if authorized:
            parts.append("%s: %s." % (AUTHORIZED_MISSING, len(authorized)))
        if self.reversal_count:
            parts.append("Devoluciones o contracargos informados: %s." % self.reversal_count)
        return " ".join(parts)

    @api.model
    def _row_values(self, row, source, amount):
        when = parse_mp_datetime(row.get("TRANSACTION_DATE"))
        return {
            "kind": "income",
            "source_id": source,
            "date": when,
            "amount": amount,
            "transaction_type": (row.get("TRANSACTION_TYPE") or "").upper()[:32],
            "method_type": (row.get("PAYMENT_METHOD_TYPE") or "")[:64],
            "method": (row.get("PAYMENT_METHOD") or "")[:64],
            "segment": (row.get("SEGMENT_DETAIL") or "")[:64],
            "sale_detail": (row.get("SALE_DETAIL") or "").strip('"')[:128],
            "external_reference": (row.get("EXTERNAL_REFERENCE") or "")[:128],
            "payer_name": (row.get("PAYER_NAME") or "")[:128],
            "payer_id_type": (row.get("PAYER_ID_TYPE") or "")[:16],
            "payer_cuit": digits(row.get("PAYER_ID_NUMBER"))[:16],
            "bank_transfer_id": digits(row.get("PAY_BANK_TRANSFER_ID"))[:32],
            "fee_amount": abs(float((row.get("FEE_AMOUNT") or "0").replace(",", ".") or 0)),
        }

    def _receipts_without_money(self, start, end):
        """«Sin transferencia»: receipts sent within the period the report
        covers (up to `covers_until`) that no transfer of it (or an earlier
        one) matched."""
        self.ensure_one()
        if not (start and end):
            return self.env["distri.mp.collection"]
        Message = self.env["distri.whatsapp.message"].sudo()
        receipts = Message.search([
            ("distri_is_receipt", "=", True),
            ("distri_receipt_state", "=", "pending"),
            ("timestamp", ">=", start - timedelta(hours=1)),
            ("timestamp", "<=", end),
        ])
        Collection = self.env["distri.mp.collection"].sudo()
        cards = Collection
        for receipt in receipts:
            receipt._distri_classify()
            receipt._distri_match_income()
            if receipt.distri_receipt_state != "pending":
                continue
            values = {
                "kind": "receipt",
                "report_id": self.id,
                "receipt_id": receipt.id,
                "partner_id": receipt.distri_partner_id.id,
                "date": receipt.timestamp,
                "amount": receipt.distri_amount,
                "proposal": "photo" if receipt.distri_partner_id else "none",
                "proposal_note": "Sin ingreso en el reporte",
            }
            # the card waiting in «Esperando reporte MP» moves to «Sin
            # transferencia»: one card per receipt
            waiting = Collection.search([("kind", "=", "verify"), ("state", "=", "draft"),
                                         ("receipt_id", "=", receipt.id)], limit=1)
            if waiting:
                waiting.write(values)
                cards |= waiting
            else:
                cards |= Collection.create(values)
            receipt.distri_receipt_state = "no_money"
        return cards

    def _flag_hand_verified_missing(self, start, end):
        """A delivery authorized by hand (or, before 19.0.1.21.0, a receipt
        verified and paid by hand) whose moment the report covers but whose
        operation it does not bring: «Sin transferencia», with an alert."""
        self.ensure_one()
        Collection = self.env["distri.mp.collection"].sudo()
        if not (start and end):
            return Collection
        cards = Collection.search([
            ("kind", "=", "verify"), ("state", "in", ("confirmed", "authorized")),
            ("income_id", "=", False), ("report_review", "=", False),
            ("date", ">=", start - timedelta(hours=1)), ("date", "<=", end),
        ])
        for card in cards:
            note = AUTHORIZED_MISSING if card.state == "authorized" else HAND_VERIFIED_MISSING
            card.write({"report_review": True, "report_id": self.id, "proposal_note": note})
            card.message_post(body="%s (%s). Revisá la operación %s en la app%s." % (
                note, self.name, card.operation or "",
                " y avisá al gerente: la mercadería ya se entregó"
                if card.state == "authorized" else ""))
        return cards

    # ------------------------------------------------------------ SFTP folder

    @api.model
    def _folder(self):
        return (self.env["ir.config_parameter"].sudo().get_param(PARAM_FOLDER)
                or DEFAULT_FOLDER).strip()

    @api.model
    def _cron_import_folder(self):
        """Every 15 minutes: reports left by Mercado Pago in the SFTP folder.

        Each file is imported once and moved to «procesados» (or «errores»).
        Only regular .csv files directly in the folder are read.
        """
        folder = self._folder()
        if not os.path.isdir(folder):
            _logger.debug("Mercado Pago: report folder %s does not exist", folder)
            return
        for name in sorted(os.listdir(folder)):
            path = os.path.join(folder, name)
            if not name.lower().endswith(".csv") or os.path.islink(path) \
                    or not os.path.isfile(path):
                continue
            target = DONE_FOLDER
            try:
                if self.sudo().search_count([("name", "=", name), ("source", "=", "sftp")]):
                    # imported and committed before, but the move failed: only move it
                    _logger.info("Mercado Pago: %s already imported", name)
                else:
                    if os.path.getsize(path) > MAX_REPORT_BYTES:
                        raise UserError("Archivo de más de 20 MB.")
                    with open(path, "rb") as handle:
                        data = handle.read()
                    with self.env.cr.savepoint():
                        report = self._import_file(name, data, source="sftp")
                    _logger.info("Mercado Pago: %s imported (%s new transfers)",
                                 name, report.new_count)
            except Exception as exc:  # the file stays aside, the next ones go on
                target = ERROR_FOLDER
                _logger.warning("Mercado Pago: %s not imported: %s", name, exc)
            # commit before moving: a file in «procesados» is always in the database
            if not odoo_module.current_test:
                self.env["ir.cron"]._commit_progress(1)
            destination = os.path.join(folder, target)
            try:
                os.makedirs(destination, exist_ok=True)
                shutil.move(path, os.path.join(destination, name))
            except OSError as exc:
                _logger.warning("Mercado Pago: %s could not be moved to %s: %s",
                                name, target, exc)
                return

    def action_open_collections(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Cobros del reporte",
            "res_model": "distri.mp.collection",
            "view_mode": "list,form",
            "domain": [("report_id", "=", self.id)],
        }


# ====================================================================== outgoing


class DistriMpOutgoing(models.Model):
    """An outgoing transfer of the company's own Mercado Pago account, as the
    report brings it (a negative amount). Only its number, moment and amount
    are kept: a WhatsApp receipt with that operation number is the company's
    refund or payment, never a customer's income."""

    _name = "distri.mp.outgoing"
    _description = "Egreso de Mercado Pago"
    _order = "date desc, id desc"
    _rec_name = "source_id"

    source_id = fields.Char(string="Número de operación", required=True, readonly=True,
                            index=True)
    date = fields.Datetime(string="Fecha", readonly=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    payer_name = fields.Char(string="Contraparte", readonly=True)
    payer_cuit = fields.Char(string="CUIT de la contraparte", readonly=True)

    _source_unique = models.Constraint("unique(source_id)", "Ese egreso ya está cargado.")

    @api.model
    def _record(self, source, values):
        if self.search_count([("source_id", "=", source)]):
            return self.browse()
        return self.create({
            "source_id": source, "date": values.get("date") or False,
            "amount": values.get("amount") or 0.0,
            "payer_name": values.get("payer_name") or False,
            "payer_cuit": values.get("payer_cuit") or False,
        })


# ====================================================================== collection


class DistriMpCollection(models.Model):
    _name = "distri.mp.collection"
    _description = "Cobro de Mercado Pago"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc, id desc"

    name = fields.Char(string="Cobro", compute="_compute_name", store=True)
    kind = fields.Selection([("income", "Ingreso de Mercado Pago"),
                             ("receipt", "Foto sin transferencia"),
                             ("verify", "Comprobante por verificar")],
                            string="Tipo", required=True, default="income", readonly=True)
    report_id = fields.Many2one("distri.mp.report", string="Reporte", readonly=True,
                                ondelete="set null", index=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    source_id = fields.Char(string="Número de operación", readonly=True, index=True)
    date = fields.Datetime(string="Fecha", readonly=True)
    date_day = fields.Date(string="Día", compute="_compute_date_day", store=True, index=True)
    hour = fields.Char(string="Hora", compute="_compute_texts")
    amount = fields.Monetary(string="Importe", readonly=True,
                             help="Sale de Mercado Pago y no se edita.")
    fee_amount = fields.Monetary(string="Comisión", readonly=True)
    transaction_type = fields.Char(string="Tipo de operación", readonly=True)
    method_type = fields.Char(string="Medio", readonly=True)
    method = fields.Char(string="Forma de pago", readonly=True)
    segment = fields.Char(string="Segmento", readonly=True)
    sale_detail = fields.Char(string="Detalle", readonly=True)
    external_reference = fields.Char(string="Referencia externa", readonly=True)
    payer_name = fields.Char(string="Titular", readonly=True)
    payer_id_type = fields.Char(string="Tipo de documento", readonly=True)
    payer_cuit = fields.Char(string="CUIT de quien pagó", readonly=True, index=True)
    bank_transfer_id = fields.Char(string="Id de transferencia bancaria", readonly=True)
    origin_label = fields.Char(string="Origen", compute="_compute_texts")
    detail = fields.Char(string="Detalle del ingreso", compute="_compute_texts")

    partner_id = fields.Many2one("res.partner", string="Cliente", tracking=True,
                                 domain="[('distri_generic_partner', '=', False)]")
    proposal = fields.Selection(PROPOSALS, string="Propuesto por", default="none",
                                readonly=True, tracking=True)
    proposal_note = fields.Char(string="Motivo", readonly=True)
    column = fields.Selection(COLUMNS, string="Columna", compute="_compute_column",
                              store=True, group_expand=True, index=True)
    # «authorized»: a «Esperando reporte MP» card seen in the Mercado Pago app to
    # deliver goods; no payment until the report brings the transfer
    state = fields.Selection([
        ("draft", "Pendiente"),
        ("authorized", "Autorizado para entregar"),
        ("confirmed", "Confirmado"),
        ("ignored", "Descartado"),
    ], string="Estado", default="draft", required=True, readonly=True, tracking=True,
        index=True)
    receipt_id = fields.Many2one("distri.whatsapp.message", string="Comprobante por WhatsApp",
                                 readonly=True, ondelete="set null")
    receipt_is_image = fields.Boolean(related="receipt_id.media_is_image")
    receipt_amount_review = fields.Boolean(related="receipt_id.distri_amount_review",
                                           string="Revisar importe de la foto")
    receipt_image = fields.Binary(related="receipt_id.media_data", string="Foto")
    receipt_name = fields.Char(related="receipt_id.media_name")
    receipt_forwarded_by_id = fields.Many2one(related="receipt_id.distri_forwarded_by_id",
                                              string="Foto reenviada por")
    promise_id = fields.Many2one("distri.promised.transfer", string="Comprobante pendiente",
                                 readonly=True, ondelete="set null")
    fulfilled_promise_ids = fields.Many2many(
        "distri.promised.transfer", "distri_mp_collection_promise_rel", "collection_id",
        "promise_id", string="Comprobantes pendientes resueltos", readonly=True)
    payment_id = fields.Many2one("account.payment", string="Pago registrado", readonly=True)
    confirmed_by_id = fields.Many2one("res.users", string="Confirmó", readonly=True)
    confirmed_at = fields.Datetime(string="Confirmado el", readonly=True)
    ignore_reason = fields.Selection(IGNORE_REASONS, string="Motivo del descarte", readonly=True)
    ignore_note = fields.Char(string="Aclaración", readonly=True)
    reversal = fields.Char(string="Aviso de Mercado Pago", readonly=True,
                           help="El reporte informó una devolución o un contracargo.")
    card_color = fields.Integer(compute="_compute_card_color")
    # board card: where the proposed customer came from and what to check
    proposal_badge = fields.Char(string="Pista", compute="_compute_card_flags")
    amount_mismatch = fields.Boolean(string="Importe distinto", compute="_compute_card_flags")
    date_mismatch = fields.Boolean(
        string="Fecha distinta", compute="_compute_card_flags",
        help="La fecha leída en el comprobante está a más de un día de la transferencia.")
    conflict_note = fields.Char(
        string="Comprobante en conflicto", readonly=True,
        help="El mismo comprobante llegó para dos clientes distintos: una persona decide.")
    receipt_destination = fields.Char(string="Destino del comprobante",
                                      compute="_compute_card_texts")
    receipt_other_account = fields.Boolean(
        string="A otra cuenta", compute="_compute_card_texts",
        help="El destino leído en el comprobante no es la cuenta propia.")
    hand_verified = fields.Boolean(string="Verificado a mano", compute="_compute_card_flags")
    review_reason = fields.Char(string="Por qué revisar", compute="_compute_card_flags")
    payer_label = fields.Char(string="Pagó", compute="_compute_card_texts")
    report_label = fields.Char(string="Reporte de origen", compute="_compute_card_texts")
    receipt_operation = fields.Char(related="receipt_id.distri_operation",
                                    string="Número de operación leído del comprobante")
    waiting_label = fields.Char(string="Esperando", compute="_compute_card_texts")
    receipt_date_label = fields.Char(string="Fecha del comprobante", compute="_compute_card_texts")
    received_label = fields.Char(string="Foto recibida", compute="_compute_card_texts")
    period_label = fields.Char(string="Período revisado", compute="_compute_card_texts")
    # «Esperando reporte MP»: a WhatsApp receipt checked by hand in the
    # Mercado Pago app before any report
    operation = fields.Char(
        string="Número de operación del comprobante", readonly=True, index=True,
        help="El número de operación de Mercado Pago del comprobante. Con él se reconoce la "
             "transferencia cuando llega el reporte.")
    income_id = fields.Many2one(
        "distri.mp.collection", string="Ingreso del reporte", readonly=True,
        ondelete="set null", index=True,
        help="La transferencia del reporte de Mercado Pago que corresponde a este comprobante "
             "verificado a mano.")
    report_review = fields.Boolean(
        string="Revisar contra el reporte", readonly=True,
        help="Autorizado (o verificado) a mano, pero el reporte de Mercado Pago que cubre ese "
             "momento no trae la operación.")
    authorized_by_id = fields.Many2one("res.users", string="Autorizó la entrega", readonly=True)
    authorized_at = fields.Datetime(string="Autorizado el", readonly=True)
    authorized_label = fields.Char(string="Autorización", compute="_compute_card_texts")
    authorized_conflict = fields.Char(
        string="Otro cliente por CUIT", readonly=True,
        help="La entrega se autorizó a mano para un cliente, pero el CUIT de quien pagó, según "
             "el reporte, ya es de otro cliente.")

    _source_unique = models.Constraint("unique(source_id)",
                                       "Esa operación de Mercado Pago ya está cargada.")

    # ------------------------------------------------------------ computes

    @api.depends("kind", "source_id", "receipt_id")
    def _compute_name(self):
        for rec in self:
            if rec.kind in ("receipt", "verify"):
                # no receipt (emptied, or not set yet): the card's own id
                rec.name = "Foto %s" % str(rec.receipt_id.wa_message_id or rec._origin.id or "")[-8:]
            else:
                rec.name = "MP %s" % (rec.source_id or "")

    @api.depends("date")
    def _compute_date_day(self):
        for rec in self:
            rec.date_day = local_day(rec.date)

    @api.depends("date", "method_type", "method", "payer_cuit", "payer_name", "segment",
                 "kind", "receipt_id", "operation")
    def _compute_texts(self):
        for rec in self:
            rec.hour = local_hour(rec.date)
            if rec.kind == "verify":
                rec.origin_label = "WhatsApp"
                parts = ["Foto del %s" % local_when(rec.date),
                         "Operación %s" % rec.operation if rec.operation else "sin número de operación",
                         "CUIT %s" % format_cuit(rec.payer_cuit) if rec.payer_cuit else "",
                         friendly_case(rec.payer_name) if rec.payer_name else ""]
                rec.detail = " · ".join(p for p in parts if p)
                continue
            if rec.kind == "receipt":
                rec.origin_label = "WhatsApp"
                rec.detail = "Foto recibida el %s. No aparece en el reporte." % local_when(rec.date)
                continue
            if rec.method_type == "bank_transfer":
                rec.origin_label = "Banco" if rec.method != "debin_transfer" else "DEBIN"
            else:
                rec.origin_label = "Mercado Pago"
            parts = [rec.origin_label, rec.hour]
            if rec.payer_cuit:
                parts.append("CUIT %s" % format_cuit(rec.payer_cuit))
            else:
                parts.append("sin CUIT")
            if rec.payer_name:
                parts.append(friendly_case(rec.payer_name))
            rec.detail = " · ".join(p for p in parts if p)

    @api.depends("kind", "state", "proposal", "partner_id", "receipt_id.distri_forwarded_by_id",
                 "report_review", "amount", "receipt_id.distri_amount", "authorized_conflict",
                 "reversal", "conflict_note")
    def _compute_column(self):
        for rec in self:
            if rec.kind == "income" and rec.reversal:
                # Mercado Pago reported a refund or a chargeback: never
                # confirmed on sight, nor by «Confirmar todos»
                rec.column = "review"
            elif rec.conflict_note and rec.state == "draft" and rec.kind in ("income", "verify"):
                # the same receipt came for two customers: a person decides
                rec.column = "review"
            elif rec.kind == "verify":
                # authorized (or, before 19.0.1.21.0, paid) by hand and the
                # report that covers its moment does not bring it
                rec.column = "no_money" if rec.report_review else "verify"
            elif rec.kind == "receipt":
                rec.column = "no_money"
            elif rec.proposal == "authorized":
                # the report brought a delivery authorized by hand: confirmed
                # like any other, unless Mercado Pago says another amount or
                # the payer's CUIT is already another customer's
                rec.column = "review" if (rec._authorized_other_amount()
                                          or rec.authorized_conflict) else "confirm"
            elif rec.proposal == "verified":
                # tied to a receipt verified by hand: never paid twice
                rec.column = "review"
            elif rec.receipt_id.distri_forwarded_by_id and rec.proposal != "manual":
                # a photo forwarded by a seller is always checked by a person
                rec.column = "review"
            elif rec.partner_id and rec.proposal in ("cuit", "link", "manual"):
                rec.column = "confirm"
            elif rec.proposal in ("operation", "photo", "promise", "several") or (
                    rec.proposal == "none" and rec.partner_id):
                rec.column = "review"
            else:
                rec.column = "unidentified"

    @api.depends("column")
    def _compute_card_color(self):
        colors = {"verify": 4, "confirm": 10, "review": 2, "unidentified": 1, "no_money": 9}
        for rec in self:
            rec.card_color = colors.get(rec.column, 0)

    @api.depends("kind", "state", "proposal", "partner_id", "amount", "receipt_id", "column",
                 "payer_cuit", "receipt_id.distri_amount", "receipt_id.distri_partner_source",
                 "receipt_id.distri_forwarded_by_id", "reversal", "conflict_note", "date",
                 "receipt_id.distri_receipt_date")
    def _compute_card_flags(self):
        for rec in self:
            receipt = rec.receipt_id
            rec.proposal_badge = rec._proposal_badge()
            rec.hand_verified = rec.proposal == "verified" or (
                rec.kind == "verify" and rec.state == "confirmed")
            rec.amount_mismatch = bool(
                rec.kind == "income" and receipt and receipt.distri_amount
                and abs(receipt.distri_amount - rec.amount) > AMOUNT_TOLERANCE)
            read = receipt.distri_receipt_date
            rec.date_mismatch = bool(
                rec.kind == "income" and read and rec.date
                and abs(read - rec.date) > RECEIPT_DATE_WINDOW)
            rec.review_reason = rec._review_reason() if rec.column == "review" else False

    def _authorized_other_amount(self):
        """An income tied to a delivery authorized by hand whose amount is
        not the one of the receipt."""
        self.ensure_one()
        receipt_amount = self.receipt_id.distri_amount
        return bool(receipt_amount and abs(receipt_amount - self.amount) > AMOUNT_TOLERANCE)

    def _review_reason(self):
        """Why the card is in «Revisar», in a few words (follows
        `_compute_column`)."""
        self.ensure_one()
        if self.kind == "income" and self.reversal:
            return "%s: revisá antes de confirmar" % self.reversal
        if self.conflict_note:
            return self.conflict_note
        if self.proposal == "authorized":
            if self.authorized_conflict:
                return "Entrega autorizada a mano, pero el CUIT es de otro cliente"
            return "Entrega autorizada a mano con otro importe"
        if self.proposal == "verified":
            return ("Verificado a mano con otro importe" if self.amount_mismatch
                    else "Verificado a mano: revisar antes de confirmar")
        reasons = []
        seller = self.receipt_id.distri_forwarded_by_id
        if seller and self.proposal != "manual":
            reasons.append("Foto reenviada por el vendedor")
        if self.proposal == "several":
            reasons.append("El CUIT es de varios clientes")
        elif self.proposal == "promise":
            reasons.append("Cliente sacado de un comprobante pendiente del reparto")
        elif self.proposal in ("photo", "operation") and not seller:
            source = self.receipt_id.distri_partner_source or ""
            reasons.append("Cliente sacado del texto del vendedor" if source.startswith("Nota de")
                           else "Cliente sacado solo de la foto")
        elif self.proposal == "none" and self.partner_id:
            reasons.append("Cliente sin confirmar")
        if self.kind == "income" and self.payer_cuit and self.proposal not in ("cuit", "several"):
            reasons.append("CUIT no conocido")
        if self.amount_mismatch:
            reasons.append("el importe no coincide con la foto")
        if self.date_mismatch:
            reasons.append("la fecha leída en la foto no es la de la transferencia")
        text = ", ".join(reasons) or "Revisar el cliente"
        return text[:1].upper() + text[1:]

    @api.depends("kind", "state", "date", "payer_name", "payer_cuit", "report_id",
                 "receipt_id.distri_receipt_date", "authorized_by_id", "authorized_at",
                 "income_id", "receipt_id.distri_dest_name", "receipt_id.distri_dest_cuit",
                 "receipt_id.distri_destination")
    def _compute_card_texts(self):
        now = fields.Datetime.now()
        own = own_account(self.env)
        for rec in self:
            receipt = rec.receipt_id
            rec.receipt_destination = receipt._distri_destination_label() if receipt else False
            rec.receipt_other_account = bool(receipt and receipt._distri_other_account(own))
            payer = [friendly_case(rec.payer_name) if rec.payer_name else "",
                     "CUIT %s" % format_cuit(rec.payer_cuit) if rec.payer_cuit else ""]
            rec.payer_label = " · ".join(p for p in payer if p) or False
            report = rec.report_id
            when = report.generated_at or report.covers_until or report.imported_at
            rec.report_label = ("Reporte N° %s del %s" % (report.id, local_when(when))
                                if report else False)
            rec.waiting_label = (
                "Esperando hace %s" % _elapsed(now - rec.date)
                if rec.kind == "verify" and rec.state in ("draft", "authorized") and rec.date
                and not rec.income_id else False)
            rec.authorized_label = (
                "%s el %s" % (rec.authorized_by_id.name or "", local_when(rec.authorized_at))
                if rec.authorized_at else False)
            read = rec.receipt_id.distri_receipt_date
            rec.receipt_date_label = local_when(read) if read else False
            rec.received_label = local_when(rec.date) or False
            rec.period_label = ("%s a %s" % (local_when(report.period_start),
                                             local_when(report.period_end))
                                if rec.kind == "receipt" and report.period_start else False)

    def action_view_report(self):
        """The Mercado Pago report the card came from, to check it by hand."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "distri.mp.report",
            "res_id": self.report_id.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }

    def _proposal_badge(self):
        """Short name of where the proposed customer came from."""
        self.ensure_one()
        if self.proposal == "several":
            return "Varios"
        if not self.partner_id:
            return False
        if self.proposal in ("cuit", "link", "promise", "manual", "authorized"):
            return {"cuit": "CUIT", "link": "Link", "promise": "Reparto",
                    "manual": "A mano", "authorized": "Entrega autorizada"}[self.proposal]
        if self.proposal not in ("photo", "operation", "none"):
            return False
        source = self.receipt_id.distri_partner_source or ""
        if source.startswith("Nota de") or self.receipt_id.distri_forwarded_by_id:
            return "Texto del vendedor"
        if source.startswith("Teléfono"):
            return "Teléfono"
        if source.startswith("CUIT"):
            return "CUIT"
        if source.startswith("Pedido"):
            return "Pedido"
        if source.startswith(("Asignado", "Elegido")):
            return "A mano"
        return "Foto" if self.receipt_id else False

    @api.model
    def distri_board_columns(self):
        """Columns of the board with their help text, for the «i» of each
        column header. One source: COLUMNS and COLUMN_HELP."""
        return [{"key": key, "label": label, "help": COLUMN_HELP.get(key, ""),
                 "priority": key in PRIORITY_COLUMNS}
                for key, label in COLUMNS]

    # ------------------------------------------------------------ alerts

    @api.model
    def _alert_hours(self):
        value = self.env["ir.config_parameter"].sudo().get_param(PARAM_ALERT_HOURS)
        try:
            hours = float(value) if value else DEFAULT_ALERT_HOURS
        except ValueError:
            hours = DEFAULT_ALERT_HOURS
        return hours if hours > 0 else DEFAULT_ALERT_HOURS

    def _alert_help(self):
        self.ensure_one()
        if self.kind == "verify" and self.state == "authorized" and not self.report_review:
            return "%s. %s" % (AUTHORIZED_WAITING, AUTHORIZED_WAITING_HELP)
        if self.kind == "verify" and self.state == "authorized":
            return "%s. %s" % (AUTHORIZED_MISSING, AUTHORIZED_MISSING_HELP)
        if self.kind == "verify" and self.state == "confirmed":
            return "%s. %s" % (HAND_VERIFIED_MISSING, HAND_VERIFIED_MISSING_HELP)
        return COLUMN_HELP[self.column]

    def _close_alerts(self, feedback):
        """The board alert of the card is done: it was resolved."""
        alerts = self.sudo().activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY)
        if alerts:
            alerts.action_feedback(feedback=feedback)

    @api.model
    def _cron_alerts(self):
        """One To-Do per card that waits in «Asignar cliente» or «Sin
        transferencia» longer than the parameter (24 h by default); «Sin
        transferencia» includes the deliveries authorized by hand (and the
        payments registered by hand before 19.0.1.21.0) whose transfer the
        report did not bring. A delivery authorized by hand that still waits
        in «Esperando reporte MP» (no report brought or missed its operation)
        is alerted too, counted from the authorization.
        Never a second one while the first is open. Activities only: nothing
        leaves the server (no email, no WhatsApp). Returns the cards alerted.
        The cron runs every hour, so the limit is kept within the hour."""
        limit = fields.Datetime.now() - timedelta(hours=self._alert_hours())
        cards = self.sudo().search([
            "|",
            "&", "&", ("column", "in", PRIORITY_COLUMNS), ("create_date", "<=", limit),
            "|", ("state", "=", "draft"), ("report_review", "=", True),
            "&", "&", "&", ("kind", "=", "verify"), ("state", "=", "authorized"),
            ("income_id", "=", False), ("authorized_at", "<=", limit)],
            order="create_date, id")
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        admins = self.env.ref(ADMIN_GROUP).sudo().user_ids.filtered(
            lambda u: u.active and not u.share and u.id != self.env.ref("base.user_root").id
        ).sorted("id")
        if not (cards and todo and admins):
            return self.browse()
        Activity = self.env["mail.activity"].sudo()
        # spread the cards over the administrators: the one with fewest open
        # board alerts gets the next card
        load = {user.id: Activity.search_count([
            ("res_model", "=", self._name), ("summary", "=", ALERT_SUMMARY),
            ("user_id", "=", user.id)]) for user in admins}
        alerted = self.browse()
        quiet = self.with_context(mail_activity_quick_update=True, mail_notrack=True).sudo()
        for card in cards:
            if card.activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY):
                continue
            user_id = min(load, key=lambda uid: (load[uid], uid))
            column = dict(COLUMNS)[card.column]
            quiet.browse(card.id).activity_schedule(
                activity_type_id=todo.id, summary=ALERT_SUMMARY, user_id=user_id,
                note="Hace más de %s h en «%s»: %s, %s. %s" % (
                    int(self._alert_hours()), column, _money(card.amount),
                    card.partner_id.name or "sin cliente", card._alert_help()))
            load[user_id] += 1
            alerted |= card
        if alerted:
            _logger.info("Tablero de cobros: %s alertas nuevas", len(alerted))
        return alerted

    def action_check_alerts(self):
        """«Revisar alertas»: the hourly check, now."""
        if not self.env.user.has_group(ADMIN_GROUP):
            raise UserError("Solo administración revisa las alertas del tablero.")
        alerted = self._cron_alerts()
        message = ("%s tarjetas esperan hace más de %s h: quedó una actividad para cada una."
                   % (len(alerted), int(self._alert_hours())) if alerted
                   else "No hay tarjetas nuevas esperando de más en «Asignar cliente», en "
                        "«Sin transferencia» ni entregas autorizadas esperando el reporte.")
        return {"type": "ir.actions.client", "tag": "display_notification",
                "params": {"title": "Alertas del tablero", "message": message,
                           "type": "warning" if alerted else "info", "sticky": False}}

    # ------------------------------------------------------------ proposal

    def _refill(self, values):
        """A later report brought data the first one lacked (e.g. the CUIT).
        Returns True when something was filled."""
        self.ensure_one()
        fill = {k: v for k, v in values.items()
                if k in ("payer_name", "payer_id_type", "payer_cuit", "bank_transfer_id",
                         "segment", "sale_detail", "external_reference", "method")
                and v and not self[k]}
        if fill:
            self.write(fill)
        return bool(fill) and self.proposal not in ("manual",)

    def _propose(self):
        """Proposed customer and column of each draft transfer."""
        Partner = self.env["res.partner"].sudo()
        todo = self.filtered(lambda c: c.kind == "income" and c.state == "draft"
                             and c.proposal not in ("manual", "verified", "authorized"))
        by_amount = todo._pair_receipts_by_amount()
        for rec in todo:
            partner = Partner.browse()
            proposal, note, several = "none", "", ""
            if rec.payer_cuit:
                partners = Partner._distri_partners_by_cuit(rec.payer_cuit)
                if len(partners) == 1:
                    partner, proposal, note = partners, "cuit", "CUIT conocido"
                elif len(partners) > 1:
                    several = "El CUIT es de varios clientes: %s" % ", ".join(
                        partners[:3].mapped("name"))
            if not partner:
                link = self.env["distri.mp.payment"].sudo().search(
                    [("payment_id", "=", rec.source_id)], limit=1)
                if link.order_id:
                    partner = link.order_id.partner_id.commercial_partner_id
                    proposal = "link"
                    note = "Pagó con el link del pedido %s" % link.order_id.name
            receipt = rec.receipt_id or rec._find_receipt()
            if not receipt and rec.id in by_amount and \
                    by_amount[rec.id].distri_receipt_state in ("pending", "no_money"):
                receipt = by_amount[rec.id]
            if receipt:
                rec._link_receipt(receipt)
                photo_partner = receipt.distri_partner_id.commercial_partner_id
                by_operation = rec._receipt_by_operation()
                if not partner and photo_partner:
                    partner = photo_partner
                    proposal = "operation" if by_operation else "photo"
                    note = ("Mismo número de operación que la foto de las %s" if by_operation
                            else "Coincide con la foto de las %s") % local_hour(receipt.timestamp)
                elif partner and photo_partner and photo_partner != partner:
                    proposal = "photo"
                    note = "La foto de las %s es de otro cliente (%s)" % (
                        local_hour(receipt.timestamp), photo_partner.name)
                elif not partner:
                    note = note or "Hay una foto de las %s, de un número sin cliente" % (
                        local_hour(receipt.timestamp))
            if not partner:
                promise = rec._find_promise()
                if promise:
                    partner = promise.partner_id.commercial_partner_id
                    proposal = "promise"
                    rec.promise_id = promise
                    note = "Transferencia de %s cobrada en el reparto el %s (%s)" % (
                        _money(promise.amount), local_when(promise.captured_at),
                        promise.driver_id.name or "")
            if not partner and several:
                proposal, note = "several", several
            seller = rec.receipt_id.distri_forwarded_by_id
            if seller:
                note = "Foto reenviada por %s%s" % (seller.name, (". " + note) if note else "")
            rec.write({"partner_id": partner.id, "proposal": proposal,
                       "proposal_note": note or False,
                       "conflict_note": rec.receipt_id.distri_conflict or False})

    def _find_receipt(self):
        """The WhatsApp receipt of this transfer with the same operation number
        (or one OCR slip away from it, with the same amount). Receipts of the
        same amount only are paired by `_pair_receipts_by_amount`."""
        self.ensure_one()
        Message = self.env["distri.whatsapp.message"].sudo()
        numbers = [n for n in (self.source_id, self.bank_transfer_id) if n]
        if not numbers:
            return Message
        base = [("distri_is_receipt", "=", True),
                ("distri_receipt_state", "in", ("pending", "no_money"))]
        # the first one received: a copy sent later is «Ya recibido» or, for
        # another customer, in conflict with it
        found = Message.search(base + [("distri_operation", "in", numbers)],
                               order="timestamp, id", limit=1)
        if found:
            return found
        near = Message.search(base + [
            ("distri_operation", "!=", False),
            ("distri_amount", ">=", self.amount - AMOUNT_TOLERANCE),
            ("distri_amount", "<=", self.amount + AMOUNT_TOLERANCE)], order="timestamp, id")
        near = near.filtered(lambda m: any(operation_close(m.distri_operation, n) for n in numbers))
        # never a receipt whose number is exactly another transfer's
        taken = self._transfer_numbers(near.mapped("distri_operation"))
        return near.filtered(lambda m: m.distri_operation not in taken)[:1]

    def _receipt_by_operation(self):
        """The receipt tied to this transfer has its operation number (or one
        OCR slip away from it)."""
        self.ensure_one()
        receipt = self.receipt_id
        return bool(receipt and receipt.distri_operation and any(
            operation_close(receipt.distri_operation, n)
            for n in (self.source_id, self.bank_transfer_id) if n))

    def _receipt_distance(self, receipt):
        """Seconds between the transfer and the receipt: its own date and time
        when the photo carries one, else the moment it arrived."""
        self.ensure_one()
        moment = receipt.distri_receipt_date or receipt.timestamp
        return abs((moment - self.date).total_seconds())

    def _pair_receipts_by_amount(self):
        """{income id: receipt} for the incomes without a receipt of their
        operation: same amount, at the receipt's own date and time when it
        carries one (an old receipt sent late still finds its transfer), else
        sent a little after (or just before) the transfer. The closest
        transfer/receipt pair goes first, whatever the order of the report,
        so a receipt goes to the transfer nearest in time and never to an
        earlier one of the same amount."""
        pairs = []
        for rec in self:
            if rec.receipt_id or rec._find_receipt():
                continue
            for receipt in rec._amount_candidates():
                pairs.append((rec._receipt_distance(receipt), rec.id, receipt.id, receipt))
        found, taken = {}, set()
        for _distance, rec_id, receipt_id, receipt in sorted(pairs, key=lambda p: p[:3]):
            if rec_id in found or receipt_id in taken:
                continue
            found[rec_id] = receipt
            taken.add(receipt_id)
        return found

    def _amount_candidates(self):
        """Waiting receipts of the same amount around the transfer: stamped
        (read from the photo) within a day of it, or sent a little after (or
        just before) it; except the ones whose operation number is another
        transfer's."""
        self.ensure_one()
        Message = self.env["distri.whatsapp.message"].sudo()
        if not self.date:
            return Message
        base = [("distri_is_receipt", "=", True),
                ("distri_receipt_state", "in", ("pending", "no_money"))]
        candidates = Message.search(base + [
            ("distri_amount", ">=", self.amount - AMOUNT_TOLERANCE),
            ("distri_amount", "<=", self.amount + AMOUNT_TOLERANCE),
            "|",
            "&", ("distri_receipt_date", ">=", self.date - RECEIPT_DATE_WINDOW),
            ("distri_receipt_date", "<=", self.date + RECEIPT_DATE_WINDOW),
            "&", ("timestamp", ">=", self.date - RECEIPT_BEFORE),
            ("timestamp", "<=", self.date + RECEIPT_AFTER),
        ])
        # a receipt whose operation number is another transfer's belongs to
        # that transfer only, whatever its amount and time
        taken = self._transfer_numbers(candidates.mapped("distri_operation"))
        return candidates.filtered(lambda m: m.distri_operation not in taken)

    @api.model
    def _transfer_numbers(self, numbers):
        """The operation numbers, among `numbers`, of a Mercado Pago transfer
        already loaded (stored, or created by the import running now)."""
        numbers = [n for n in set(numbers) if n]
        if not numbers:
            return set()
        transfers = self.sudo().search([("kind", "=", "income"), "|",
                                        ("source_id", "in", numbers),
                                        ("bank_transfer_id", "in", numbers)])
        return set(numbers) & set(transfers.mapped("source_id") +
                                  transfers.mapped("bank_transfer_id"))

    def _find_promise(self):
        """A receipt claimed at a delivery for the same amount around the
        transfer, only when it is the only one."""
        self.ensure_one()
        if not self.date:
            return self.env["distri.promised.transfer"]
        promises = self.env["distri.promised.transfer"].sudo().search([
            # a receipt that already arrived still waits for its money
            "|", ("state", "in", ("open", "overdue")),
            "&", ("state", "=", "fulfilled"), ("fulfilled_by_id", "=", False),
            ("amount", ">=", self.amount - AMOUNT_TOLERANCE),
            ("amount", "<=", self.amount + AMOUNT_TOLERANCE),
            ("captured_at", ">=", self.date - PROMISE_AFTER),
            ("captured_at", "<=", self.date + PROMISE_BEFORE),
        ])
        return promises if len(promises.partner_id.commercial_partner_id) == 1 and \
            len(promises) == 1 else self.env["distri.promised.transfer"]

    def _link_receipt(self, receipt):
        """Ties a receipt to this transfer and closes its «Sin transferencia» card."""
        self.ensure_one()
        self.receipt_id = receipt
        receipt.write({"distri_receipt_state": "matched", "distri_collection_id": self.id})
        cards = self.search([("kind", "in", ("receipt", "verify")),
                             ("receipt_id", "=", receipt.id), ("state", "=", "draft")])
        if cards:
            cards.write({"state": "ignored", "ignore_reason": "receipt_late",
                         "ignore_note": "La plata llegó: %s" % self.name})

    def _match_hand_verified(self):
        """Incomes of a report whose operation number is a delivery authorized
        by hand: proposed with that customer, confirmed like any other. Before
        19.0.1.21.0 a receipt verified by hand was already paid: same amount
        -> confirmed with that payment; another amount -> «Revisar». Returns
        the incomes tied."""
        tied = self.browse()
        for rec in self.filtered(lambda c: c.kind == "income" and c.state == "draft"
                                 and c.proposal not in ("verified", "authorized")):
            numbers = [n for n in (rec.source_id, rec.bank_transfer_id) if n]
            if not numbers:
                continue
            card = self.search([("kind", "=", "verify"),
                                ("state", "in", ("authorized", "confirmed")),
                                ("income_id", "=", False), ("operation", "in", numbers)],
                               order="id", limit=1)
            if card.state == "authorized":
                rec._tie_authorized(card)
                tied |= rec
            elif card:
                rec._tie_hand_verified(card)
                tied |= rec
        return tied

    def _tie_authorized(self, card):
        """The report brought a delivery authorized by hand: the income gets
        the card's customer and waits in «Confirmado contra reporte MP»."""
        self.ensure_one()
        note = "Autorizado a mano por %s el %s" % (card.authorized_by_id.name or "",
                                                   local_when(card.authorized_at))
        card.write({"income_id": self.id, "report_review": False,
                    "proposal_note": "Llegó en el reporte: %s" % self.name})
        # the payer's CUIT already known for another customer: a person decides
        partner = card.partner_id.commercial_partner_id
        by_cuit = self.env["res.partner"].sudo()._distri_partners_by_cuit(self.payer_cuit)
        other = by_cuit - partner if by_cuit and partner not in by_cuit else by_cuit.browse()
        conflict = ", ".join(other[:3].mapped("name")) if other else False
        self.write({"partner_id": card.partner_id.id, "proposal": "authorized",
                    "receipt_id": card.receipt_id.id, "proposal_note": note,
                    "authorized_conflict": conflict})
        if conflict:
            self.proposal_note = "%s para %s, pero el CUIT %s del reporte es de %s: revisá " \
                                 "antes de confirmar." % (note, partner.name,
                                                          format_cuit(self.payer_cuit), conflict)
        elif self._authorized_other_amount():
            self.proposal_note = "%s con %s, pero Mercado Pago informa %s: revisá antes de " \
                                 "confirmar." % (note, _money(card.amount), _money(self.amount))
        self.message_post(body="%s. El pago se registra al confirmar este ingreso." % (
            self.proposal_note.rstrip(".")))
        card.message_post(body="La operación llegó en el reporte: %s de %s." % (
            self.name, _money(self.amount)))
        card._close_alerts("La transferencia llegó en el reporte: %s." % self.name)

    def _tie_hand_verified(self, card):
        self.ensure_one()
        when = "%s por %s" % (local_when(card.confirmed_at), card.confirmed_by_id.name or "")
        same = self.currency_id.compare_amounts(self.amount, card.amount) == 0
        card.write({"income_id": self.id, "report_review": False,
                    "proposal_note": "Llegó en el reporte: %s" % self.name})
        values = {"partner_id": card.partner_id.id, "proposal": "verified",
                  "receipt_id": card.receipt_id.id}
        if same:
            values.update({
                "state": "confirmed",
                "payment_id": card.payment_id.id,
                "confirmed_by_id": card.confirmed_by_id.id,
                "confirmed_at": card.confirmed_at,
                "proposal_note": "Verificado a mano el %s" % when,
            })
            text = "Ya estaba verificado a mano el %s (pago %s): no se registra otro pago." % (
                when, card.payment_id.name or "")
        else:
            values["proposal_note"] = (
                "Verificado a mano el %s por %s, pero Mercado Pago informa %s. Ya hay un pago "
                "registrado: revisá antes de confirmar." % (when, _money(card.amount),
                                                           _money(self.amount)))
            text = values["proposal_note"]
        self.write(values)
        self.message_post(body=text)
        card.message_post(body="La operación llegó en el reporte: %s de %s." % (
            self.name, _money(self.amount)))

    def _fill_from_receipt(self, receipt):
        """A «Esperando reporte MP» card shows what its receipt says now."""
        self.ensure_one()
        partner = receipt.distri_partner_id.commercial_partner_id
        values = {
            "date": receipt.timestamp,
            "amount": receipt.distri_amount,
            "operation": digits(receipt.distri_operation) or False,
            "payer_name": receipt.distri_payer_name or False,
            "payer_cuit": digits(receipt.distri_payer_cuit) or False,
            "conflict_note": receipt.distri_conflict or False,
        }
        if not (self.proposal == "manual" and self.partner_id == partner):
            if partner:
                values.update(partner_id=partner.id, proposal="photo",
                              proposal_note=receipt.distri_partner_source
                              or "Cliente del comprobante")
            else:
                values.update(partner_id=False, proposal="none",
                              proposal_note="Sin cliente propuesto: elegilo mirando la foto")
        values = {k: v for k, v in values.items() if (
            self[k].id != v if isinstance(self._fields[k], fields.Many2one) else self[k] != v)}
        if values:
            self.write(values)

    def _hand_verified_card(self):
        """The confirmed «Esperando reporte MP» card tied to this income."""
        self.ensure_one()
        return self.sudo().search([("kind", "=", "verify"), ("state", "=", "confirmed"),
                                   ("income_id", "=", self.id)], limit=1)

    def _flag_reversal(self, kind, amount):
        """A refund or chargeback of a transfer: a pending one goes to
        «Revisar» (`_compute_column`), a confirmed one gets an activity for
        Administración, whose payment is already in the customer's account."""
        for rec in self:
            text = "%s de %s informado por Mercado Pago" % (
                {"REFUND": "Devolución", "CHARGEBACK": "Contracargo",
                 "DISPUTE": "Reclamo"}.get(kind, kind), _money(abs(amount)))
            if rec.reversal != text:
                rec.reversal = text
                rec.message_post(body=text + ". Revisá el cobro.")
                if rec.kind == "income" and rec.state == "confirmed":
                    rec._alert_reversal()

    def _alert_reversal(self):
        """To-Do for Administración: a confirmed income was refunded or
        charged back. Never a second one while the first is open."""
        self.ensure_one()
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        if not todo or self.activity_ids.filtered(lambda a: a.summary == REVERSAL_SUMMARY):
            return
        admins = self.env.ref(ADMIN_GROUP).sudo().user_ids.filtered(
            lambda u: u.active and not u.share and u.id != self.env.ref("base.user_root").id
        ).sorted("id")
        if not admins:
            return
        self.with_context(mail_activity_quick_update=True, mail_notrack=True).sudo(
        ).activity_schedule(
            activity_type_id=todo.id, summary=REVERSAL_SUMMARY, user_id=admins[0].id,
            note="%s sobre %s, ya confirmado (pago %s de %s a %s). Revisá la cuenta del cliente: "
                 "el pago sigue registrado." % (
                     self.reversal, self.name, self.payment_id.name or "", _money(self.amount),
                     self.partner_id.name or ""))

    # ------------------------------------------------------------ actions

    def action_confirm(self):
        """Registers the payment in «Mercado Pago», oldest invoice first."""
        _check_admin(self.env)
        todo = self.filtered(lambda c: c.state == "draft" and c.kind == "income")
        if not todo:
            raise UserError("No hay cobros pendientes para confirmar.")
        missing = todo.filtered(lambda c: not c.partner_id)
        if missing:
            raise UserError("Elegí el cliente antes de confirmar: %s." % ", ".join(
                missing.mapped("name")))
        for rec in todo:
            card = rec._hand_verified_card()
            if card:
                raise UserError(
                    "%s ya tiene un pago: el comprobante %s se verificó a mano (pago %s). Si el "
                    "importe verificado estaba mal, deshacé primero esa verificación." % (
                        rec.name, card.name, card.payment_id.name or ""))
        for rec in todo:
            rec._confirm_one()
        return True

    def _payment_memo(self):
        """Memo of the payment: the Mercado Pago operation number first, so a
        later report can be matched against it."""
        self.ensure_one()
        payer = (" · " + friendly_case(self.payer_name)) if self.payer_name else ""
        if self.kind == "verify":
            return "Mercado Pago %s · verificado a mano%s" % (self.operation or "", payer)
        return "Mercado Pago %s%s" % (self.source_id, payer)

    def _receipt_by_amount_only(self):
        """The receipt was tied to this transfer by amount and time only: its
        operation number is not the transfer's."""
        self.ensure_one()
        return bool(self.receipt_id) and not self._receipt_by_operation()

    def _confirm_one(self):
        """Registers the payment of one income of the report. Only a pending
        income of a report gets a payment here: a WhatsApp receipt never does."""
        self.ensure_one()
        record = self.sudo()
        if record.kind != "income" or record.state != "draft" or not record.source_id:
            raise UserError("%s no es un ingreso pendiente del reporte de Mercado Pago: no se "
                            "registra ningún pago." % record.name)
        partner = record.partner_id.commercial_partner_id
        journal = self.env["account.journal"]._distri_mp_journal(record.company_id)
        payment = self.env["account.payment"].sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": record.amount,
            "journal_id": journal.id,
            "date": record.date_day or fields.Date.context_today(self),
            "memo": record._payment_memo(),
            "company_id": record.company_id.id,
        })
        payment.action_post()
        self.env["distri.route"].sudo()._apply_oldest_first(payment, partner)
        # Identities are learned only from a sure link: a CUIT read from a
        # photo (OCR) never, only Mercado Pago's; nothing when the customer
        # came from a photo or a promise tied by amount and time only, unless
        # the administrator chose the customer («A mano»).
        chosen = record.proposal == "manual"
        amount_only = record._receipt_by_amount_only()
        if record.payer_cuit and (chosen or record.proposal in (
                "cuit", "link", "operation", "authorized")):
            self.env["distri.payer.identity"]._learn(
                "cuit", record.payer_cuit, partner, friendly_case(record.payer_name))
        if record.receipt_id and record.receipt_id.wa_id and \
                not record.receipt_id.distri_forwarded_by_id and (chosen or not amount_only):
            self.env["distri.payer.identity"]._learn(
                "phone", record.receipt_id.wa_id, partner, record.receipt_id.profile_name)
            if not record.receipt_id.distri_partner_id:
                record.receipt_id.distri_partner_id = partner
        promises = self.env["distri.promised.transfer"].sudo()._distri_fulfil_for(
            partner, record.date, record.amount, record)
        record.write({
            "state": "confirmed",
            "partner_id": partner.id,
            "payment_id": payment.id,
            "confirmed_by_id": self.env.user.id,
            "confirmed_at": fields.Datetime.now(),
            "fulfilled_promise_ids": [(6, 0, promises.ids)],
        })
        record.message_post(body="Confirmado por %s: pago %s de %s a %s." % (
            self.env.user.name, payment.name, _money(record.amount), partner.name))
        for card in record._authorized_card():
            card.message_post(body="El pago se registró al confirmar %s: pago %s." % (
                record.name, payment.name))

    def _authorized_card(self):
        """The delivery authorized by hand tied to this income."""
        self.ensure_one()
        if self.kind != "income":
            return self.browse()
        return self.sudo().search([("kind", "=", "verify"), ("state", "=", "authorized"),
                                   ("income_id", "=", self.id)], limit=1)

    def action_verify(self):
        """«Visto en MP: autorizar entrega»: the administrator saw the transfer
        in the Mercado Pago app and authorizes delivering the goods. Nothing
        is registered: no payment, the customer's balance stays. The report
        that brings the operation proposes its income with this customer
        (`_tie_authorized`); one that covers the moment without it sends the
        card to «Sin transferencia», with an alert
        (`DistriMpReport._flag_hand_verified_missing`)."""
        _check_admin(self.env)
        for rec in self:
            if rec.kind != "verify" or rec.state != "draft":
                raise UserError("%s no es un comprobante pendiente de verificar." % rec.name)
            receipt = rec.sudo().receipt_id
            if not receipt:
                raise UserError("%s no tiene el comprobante de WhatsApp." % rec.name)
            rec.sudo()._fill_from_receipt(receipt)
            if not rec.partner_id:
                raise UserError("Elegí el cliente antes de autorizar la entrega (%s)." % rec.name)
            if rec.amount <= 0:
                raise UserError("Falta el importe de %s: escribilo en el comprobante (botón «Ver "
                                "foto») tal como figura en Mercado Pago." % rec.name)
            if not rec.operation:
                raise UserError(
                    "Falta el número de operación de %s: copialo de la app de Mercado Pago en el "
                    "comprobante (botón «Ver foto»). Sin él, el reporte no puede reconocer la "
                    "transferencia de esta entrega." % rec.name)
            income = self.sudo().search([
                ("kind", "=", "income"), "|", ("source_id", "=", rec.operation),
                ("bank_transfer_id", "=", rec.operation)], limit=1)
            if income:
                raise UserError(
                    "La operación %s ya está en el reporte de Mercado Pago (%s, %s): trabajala "
                    "desde esa tarjeta." % (rec.operation, income.name,
                                            dict(income._fields["state"].selection)[income.state]))
            twin = self.sudo().search([("kind", "=", "verify"),
                                       ("state", "in", ("authorized", "confirmed")),
                                       ("operation", "=", rec.operation), ("id", "!=", rec.id)],
                                      limit=1)
            if twin:
                raise UserError(
                    "La operación %s ya se %s con otro comprobante (%s): es un comprobante "
                    "repetido, descartalo." % (
                        rec.operation, "autorizó a mano" if twin.state == "authorized"
                        else "verificó a mano, pago %s," % (twin.payment_id.name or ""),
                        twin.name))
            rec.sudo().write({"state": "authorized", "authorized_by_id": self.env.user.id,
                              "authorized_at": fields.Datetime.now()})
            # the receipt waits no more: no other transfer takes it by amount
            receipt.with_context(distri_receipt_matching=True).write({
                "distri_receipt_state": "matched", "distri_collection_id": rec.id})
            rec.sudo().message_post(body=(
                "Entrega autorizada por %s al ver la operación %s en Mercado Pago: %s a %s. "
                "No se registró ningún pago: se registra cuando el reporte traiga la "
                "transferencia.") % (self.env.user.name, rec.operation, _money(rec.amount),
                                     rec.partner_id.name))
        return True

    def action_clear_review(self):
        """«Revisado»: the hand-verified receipt (paid before 19.0.1.21.0) was
        checked again although the report does not bring it (e.g. it came in
        another report). A delivery authorized by hand is never cleared this
        way: a later report ties it, or the authorization is undone."""
        _check_admin(self.env)
        for rec in self.sudo().filtered(lambda c: c.report_review and c.state != "authorized"):
            rec.write({"report_review": False,
                       "proposal_note": "Revisado por %s: %s" % (
                           self.env.user.name, HAND_VERIFIED_MISSING.lower())})
            rec.message_post(body="Revisado por %s aunque no está en el reporte." % (
                self.env.user.name))
        return True

    def _bulk_problem(self):
        """Why «Confirmar todos» leaves this card for a person, or ""."""
        self.ensure_one()
        if self.reversal:
            return self.reversal
        if self._hand_verified_card():
            return "ya tiene un pago verificado a mano"
        return ""

    def action_confirm_ready(self):
        """«Confirmar todos»: every draft card of «Confirmado contra reporte MP».
        A card with a problem (a refund reported, a payment verified by hand)
        is left for a person, never stops the others."""
        _check_admin(self.env)
        ready = self.search([("state", "=", "draft"), ("column", "=", "confirm"),
                             ("kind", "=", "income"), ("partner_id", "!=", False)])
        skipped = ready.filtered(lambda c: c._bulk_problem())
        ready -= skipped
        if not ready:
            raise UserError("No hay cobros en «Confirmado contra reporte MP»%s." % (
                " que se puedan confirmar juntos (%s)" % ", ".join(
                    "%s: %s" % (c.name, c._bulk_problem()) for c in skipped[:3])
                if skipped else ""))
        ready.action_confirm()
        message = "Se confirmaron %s cobros por %s." % (
            len(ready), _money(sum(ready.mapped("amount"))))
        if skipped:
            message += " Quedaron %s para revisar uno por uno: %s." % (
                len(skipped), ", ".join(skipped[:5].mapped("name")))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning" if skipped else "success",
                "message": message,
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def action_undo(self):
        """Wrong customer: cancels the payment and sends the card back. A
        delivery authorized by hand goes back to «Esperando reporte MP»."""
        _check_admin(self.env)
        for rec in self.sudo():
            if rec.state == "authorized":
                rec._undo_authorization()
                continue
            if rec.state == "ignored":
                rec.write({"state": "draft", "ignore_reason": False, "ignore_note": False})
                if rec.kind == "receipt" and rec.receipt_id:
                    rec.receipt_id.distri_receipt_state = "no_money"
                elif rec.kind == "verify" and rec.receipt_id:
                    rec.receipt_id.distri_receipt_state = "pending"
                continue
            if rec.state != "confirmed":
                continue
            if rec.kind == "income" and rec._hand_verified_card():
                raise UserError(
                    "%s se confirmó con la verificación a mano de %s: deshacé esa tarjeta." % (
                        rec.name, rec._hand_verified_card().name))
            payment = rec.payment_id
            if payment:
                payment.action_draft()
                payment.action_cancel()
            rec.fulfilled_promise_ids._distri_undo_mp(rec)
            rec.write({"state": "draft", "payment_id": False, "confirmed_by_id": False,
                       "confirmed_at": False, "fulfilled_promise_ids": [(5, 0, 0)]})
            rec.message_post(body="Confirmación deshecha por %s; pago %s anulado." % (
                self.env.user.name, payment.name or ""))
            if rec.kind == "verify":
                rec._undo_hand_verification()
        return True

    def _undo_authorization(self):
        """Possible while nothing is registered: the card waits for the report
        again; an income of the report already tied to it (not confirmed)
        becomes an ordinary pending one and takes the receipt by its number."""
        self.ensure_one()
        income = self.income_id
        if income.state == "confirmed":
            raise UserError(
                "La entrega de %s ya se registró al confirmar %s (pago %s): deshacé primero "
                "esa confirmación." % (self.name, income.name, income.payment_id.name or ""))
        self._close_alerts("Autorización deshecha por %s." % self.env.user.name)
        self.write({"state": "draft", "authorized_by_id": False, "authorized_at": False,
                    "income_id": False, "report_review": False,
                    "proposal_note": "Autorización deshecha por %s" % self.env.user.name})
        self.message_post(body="Autorización de entrega deshecha por %s." % self.env.user.name)
        if income:
            income.write({"proposal": "none", "proposal_note": False, "receipt_id": False,
                          "authorized_conflict": False})
            income.message_post(body="Se deshizo la entrega autorizada a mano de %s." % self.name)
        if self.receipt_id:
            # back to waiting: the card follows the receipt again
            self.receipt_id.write({"distri_receipt_state": "pending"})
        if income:
            # with the receipt pending again, the income finds it by its number
            income._propose()

    def _undo_hand_verification(self):
        """The hand-verified card goes back to «Esperando reporte MP»; the
        income of the report tied to it becomes an ordinary pending one."""
        self.ensure_one()
        income = self.income_id
        self.write({"income_id": False, "report_review": False})
        if income:
            if income.state == "confirmed":
                income.write({"state": "draft", "payment_id": False, "confirmed_by_id": False,
                              "confirmed_at": False})
            if income.state == "draft":
                income.write({"proposal": "none", "proposal_note": False, "receipt_id": False})
                income.message_post(body="Se deshizo la verificación a mano de %s." % self.name)
        if self.receipt_id:
            self.receipt_id.write({"distri_receipt_state": "pending"})
        if income.state == "draft":
            # with the receipt pending again, the income finds it by its number
            income._propose()

    def action_assign(self):
        """«Elegir cliente»."""
        self.ensure_one()
        _check_admin(self.env)
        return {
            "type": "ir.actions.act_window",
            "name": "Elegir cliente",
            "res_model": "distri.mp.assign",
            "view_mode": "form",
            "target": "new",
            "context": {"default_collection_id": self.id,
                        "default_partner_id": self.partner_id.id},
        }

    def action_ignore(self):
        """«Descartar»: not a customer payment, or a receipt without money."""
        self.ensure_one()
        _check_admin(self.env)
        return {
            "type": "ir.actions.act_window",
            "name": "Descartar",
            "res_model": "distri.mp.ignore",
            "view_mode": "form",
            "target": "new",
            "context": {"default_collection_id": self.id},
        }

    def action_view_receipt(self):
        self.ensure_one()
        if not self.receipt_id:
            raise UserError("Este cobro no tiene foto.")
        return {
            "type": "ir.actions.act_window",
            "name": "Comprobante",
            "res_model": "distri.whatsapp.message",
            "res_id": self.receipt_id.id,
            "view_mode": "form",
            "views": [(self.env.ref("distri_pedido.view_receipt_form").id, "form")],
            "target": "new",
        }

    def action_view_payment(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "res_model": "account.payment",
            "res_id": self.payment_id.id,
            "view_mode": "form",
        }

    def action_import_report(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Importar reporte de Mercado Pago",
            "res_model": "distri.mp.import",
            "view_mode": "form",
            "target": "new",
        }

    def action_open_days(self):
        return self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_mp_day")


class DistriMpAssign(models.TransientModel):
    _name = "distri.mp.assign"
    _description = "Elegir el cliente de un cobro"

    collection_id = fields.Many2one("distri.mp.collection", required=True, ondelete="cascade")
    currency_id = fields.Many2one(related="collection_id.currency_id")
    amount = fields.Monetary(related="collection_id.amount")
    detail = fields.Char(related="collection_id.detail")
    payer_cuit = fields.Char(related="collection_id.payer_cuit")
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True,
                                 domain="[('distri_generic_partner', '=', False)]")
    learn = fields.Boolean(
        string="Guardar el CUIT en el cliente", default=True,
        help="La próxima transferencia desde esta cuenta se propone sola.")

    def _apply(self):
        self.ensure_one()
        _check_admin(self.env)
        rec = self.collection_id.sudo()
        if rec.state != "draft":
            raise UserError("El cobro ya no está pendiente.")
        partner = self.partner_id.commercial_partner_id
        # an income tied to a card handled by hand keeps its gate: the other
        # customer is chosen after undoing that card, never over it
        if rec.kind == "income" and rec.proposal == "verified":
            card = rec._hand_verified_card()
            raise UserError(
                "%s ya tiene un pago: el comprobante %s se verificó a mano (pago %s). Si el "
                "cliente o el importe estaban mal, deshacé primero esa verificación." % (
                    rec.name, card.name or "", card.payment_id.name or ""))
        if rec.kind == "income" and rec.proposal == "authorized" and \
                partner != rec.partner_id.commercial_partner_id:
            card = self.env["distri.mp.collection"].sudo().search(
                [("kind", "=", "verify"), ("income_id", "=", rec.id)], limit=1)
            raise UserError(
                "La entrega de %s se autorizó a mano para %s (%s). Si el cliente es otro, "
                "deshacé primero esa autorización." % (
                    rec.name, rec.partner_id.name or "", card.name or ""))
        if rec.kind == "income" and rec.proposal == "authorized":
            # the same customer: nothing to change, the gate stays as it is
            return rec
        rec.write({"partner_id": partner.id, "proposal": "manual",
                   "proposal_note": "Asignado por %s" % self.env.user.name})
        if self.learn and rec.payer_cuit:
            self.env["distri.payer.identity"]._learn(
                "cuit", rec.payer_cuit, partner, friendly_case(rec.payer_name))
        if rec.kind in ("receipt", "verify") and rec.receipt_id:
            rec.receipt_id.write({"distri_partner_id": partner.id,
                                  "distri_partner_source": "Asignado por %s" % self.env.user.name})
        return rec

    def action_assign(self):
        self._apply()
        return {"type": "ir.actions.act_window_close"}

    def action_assign_confirm(self):
        rec = self._apply()
        if rec.kind == "verify":
            raise UserError("Un comprobante no registra ningún pago: se registra el ingreso cuando "
                            "llegue el reporte. Para entregar antes, tocá «Visto en MP: autorizar "
                            "entrega» después de verlo en la app de Mercado Pago.")
        if rec.kind != "income":
            raise UserError("Una foto sin transferencia no se confirma: no hay ingreso de Mercado Pago.")
        rec.sudo(False).action_confirm()
        return {"type": "ir.actions.act_window_close"}


class DistriMpIgnore(models.TransientModel):
    _name = "distri.mp.ignore"
    _description = "Descartar un cobro"

    collection_id = fields.Many2one("distri.mp.collection", required=True, ondelete="cascade")
    kind = fields.Selection(related="collection_id.kind")
    reason = fields.Selection(IGNORE_REASONS, string="Motivo", required=True)
    note = fields.Char(string="Aclaración")

    def action_ignore(self):
        self.ensure_one()
        _check_admin(self.env)
        rec = self.collection_id.sudo()
        if rec.state != "draft":
            raise UserError("El cobro ya no está pendiente.")
        if self.reason == "other" and not (self.note or "").strip():
            raise UserError("Escribí la aclaración.")
        rec.write({"state": "ignored", "ignore_reason": self.reason,
                   "ignore_note": self.note or False})
        if rec.kind in ("receipt", "verify") and rec.receipt_id:
            rec.receipt_id.distri_receipt_state = "dismissed"
        rec.message_post(body="Descartado por %s: %s%s" % (
            self.env.user.name, dict(IGNORE_REASONS)[self.reason],
            (". " + self.note) if self.note else ""))
        return {"type": "ir.actions.act_window_close"}


class DistriMpImport(models.TransientModel):
    _name = "distri.mp.import"
    _description = "Importar reporte de Mercado Pago"

    file = fields.Binary(string="Reporte (.csv o .xlsx)", required=True)
    filename = fields.Char(string="Nombre del archivo")
    date_from = fields.Date(
        string="Solo desde", help="Opcional: se cargan solo las operaciones desde ese día "
                                  "(por ejemplo, para la puesta al día).")
    date_to = fields.Date(string="Solo hasta", help="Opcional: hasta ese día inclusive.")
    report_id = fields.Many2one("distri.mp.report", readonly=True)
    summary = fields.Text(related="report_id.summary")

    def action_import(self):
        self.ensure_one()
        _check_admin(self.env)
        if self.filename and not self.filename.lower().endswith((".csv", ".xlsx")):
            raise UserError("Subí el reporte en formato .csv o .xlsx.")
        data = base64.b64decode(self.file or b"")
        if len(data) > MAX_REPORT_BYTES:
            raise UserError("El archivo pasa los 20 MB.")
        self.report_id = self.env["distri.mp.report"]._import_file(
            self.filename or "reporte.csv", data, source="manual",
            date_from=self.date_from, date_to=self.date_to)
        return {
            "type": "ir.actions.act_window",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "name": "Reporte importado",
        }

    def action_open_board(self):
        """Back to the board (or wherever the upload started), reloaded."""
        return {"type": "ir.actions.act_window_close"}


# ====================================================================== daily close


class DistriMpDay(models.Model):
    """«Cierre diario»: the report's transfers of a day against the journal."""

    _name = "distri.mp.day"
    _description = "Cierre diario de Mercado Pago"
    _inherit = ["mail.thread", "mail.activity.mixin"]
    _order = "date desc"
    _rec_name = "date"

    date = fields.Date(string="Día", required=True, readonly=True)
    company_id = fields.Many2one("res.company", required=True, readonly=True,
                                 default=lambda self: self.env.company)
    currency_id = fields.Many2one(related="company_id.currency_id")
    report_total = fields.Monetary(string="Transferencias del reporte", compute="_compute_totals")
    confirmed_total = fields.Monetary(
        string="Confirmado en el diario", compute="_compute_totals",
        help="Cobros del día en el diario Mercado Pago, confirmados en el tablero o cargados "
             "a mano.")
    ignored_total = fields.Monetary(string="Descartado", compute="_compute_totals")
    difference = fields.Monetary(string="Diferencia", compute="_compute_totals")
    pending_count = fields.Integer(string="Pendientes", compute="_compute_totals")
    transfer_count = fields.Integer(string="Transferencias", compute="_compute_totals")
    covered = fields.Boolean(string="Reporte completo", compute="_compute_totals",
                             help="Hay un reporte que llega hasta las 23:59 del día.")
    report_end = fields.Datetime(
        string="Último reporte hasta", compute="_compute_totals",
        help="Hasta qué hora del día llega el reporte más completo que se importó.")
    incomplete_reason = fields.Text(
        string="Motivo para cerrar con el reporte incompleto",
        help="Solo si hay que cerrar el día antes de tener un reporte hasta las 23:59. Una "
             "transferencia de las últimas horas puede llegar en el reporte siguiente.")
    closed_incomplete = fields.Boolean(string="Cerrado con el reporte incompleto", readonly=True)
    state = fields.Selection([("open", "Abierto"), ("closed", "Cerrado"),
                              ("alert", "Cerrado con diferencia")],
                             string="Estado", default="open", required=True, readonly=True,
                             tracking=True)
    closed_difference = fields.Monetary(string="Diferencia al cerrar", readonly=True)
    closed_by_id = fields.Many2one("res.users", string="Cerró", readonly=True)
    closed_at = fields.Datetime(string="Cerrado el", readonly=True)
    note = fields.Text(string="Explicación de la diferencia")

    _day_unique = models.Constraint("unique(company_id, date)", "El día ya tiene su cierre.")

    @api.depends("date")
    def _compute_display_name(self):
        for day in self:
            day.display_name = "Cierre del %s" % day.date.strftime("%d/%m/%Y") if day.date else ""

    @api.model
    def _ensure_days(self, days, company):
        existing = set(self.search([("company_id", "=", company.id),
                                    ("date", "in", list(days))]).mapped("date"))
        self.create([{"date": day, "company_id": company.id}
                     for day in sorted(days) if day not in existing])

    def _day_bounds(self):
        start = datetime.combine(self.date, datetime.min.time(), AR_TZ).astimezone(
            timezone.utc).replace(tzinfo=None)
        return start, start + timedelta(days=1)

    @api.depends("date")
    def _compute_totals(self):
        Collection = self.env["distri.mp.collection"].sudo()
        Report = self.env["distri.mp.report"].sudo()
        for day in self:
            incomes = Collection.search([("kind", "=", "income"), ("date_day", "=", day.date),
                                         ("company_id", "=", day.company_id.id)])
            journal = self.env["account.journal"].sudo().search(
                [("code", "=", MP_JOURNAL_CODE), ("company_id", "=", day.company_id.id)],
                limit=1)
            payments = self.env["account.payment"].sudo().search([
                ("journal_id", "=", journal.id), ("date", "=", day.date),
                ("payment_type", "=", "inbound"), ("state", "not in", ("draft", "canceled")),
            ]) if journal else self.env["account.payment"]
            day.report_total = sum(incomes.mapped("amount"))
            day.confirmed_total = sum(payments.mapped("amount"))
            day.ignored_total = sum(incomes.filtered(lambda c: c.state == "ignored")
                                    .mapped("amount"))
            day.difference = day.report_total - day.confirmed_total - day.ignored_total
            day.pending_count = len(incomes.filtered(lambda c: c.state == "draft"))
            day.transfer_count = len(incomes)
            start, end = day._day_bounds()
            # a report that ends at 23:59 of the day covers it (the reports
            # of Mercado Pago end at 23:59:59, not at 00:00 of the next day)
            last = Report.search([("covers_until", ">=", start), ("period_start", "<", end),
                                  ("company_id", "=", day.company_id.id)],
                                 order="covers_until desc", limit=1)
            day.report_end = last.covers_until if last else False
            day.covered = bool(last) and last.covers_until >= end - REPORT_DAY_END_MARGIN

    def action_close(self):
        """Closes the day. A difference needs an explanation and alerts management."""
        _check_admin(self.env)
        for day in self:
            if day.state != "open":
                raise UserError("El día ya se cerró.")
            if day.pending_count:
                raise UserError("Quedan %s cobros pendientes del %s en el tablero." % (
                    day.pending_count, day.date.strftime("%d/%m")))
            incomplete = not day.covered
            if incomplete and not (day.incomplete_reason or "").strip():
                raise UserError(
                    "Reporte incompleto: %s. Importá el reporte que llega hasta las 23:59 del "
                    "%s, o escribí el motivo para cerrar igual." % (
                        "el último reporte llega hasta las %s" % _ar_time(day.report_end)
                        if day.report_end and day.report_end < day._day_bounds()[1]
                        else "no hay un reporte de ese día",
                        day.date.strftime("%d/%m")))
            differs = not day.currency_id.is_zero(day.difference)
            if differs and not (day.note or "").strip():
                raise UserError("Hay una diferencia de %s: escribí la explicación antes de "
                                "cerrar." % _money(day.difference))
            if incomplete:
                day.message_post(body="Cerrado con el reporte incompleto (%s). Motivo: %s" % (
                    "hasta las %s" % _ar_time(day.report_end) if day.report_end
                    else "sin reporte", day.incomplete_reason.strip()))
            day.write({
                "state": "alert" if differs else "closed",
                "closed_incomplete": incomplete,
                "closed_difference": day.difference,
                "closed_by_id": self.env.user.id,
                "closed_at": fields.Datetime.now(),
            })
            if differs:
                day._alert()
        return True

    def action_reopen(self):
        _check_admin(self.env)
        self.write({"state": "open", "closed_by_id": False, "closed_at": False})

    def _alert(self):
        """Activity for management (the system warns, the manager decides)."""
        self.ensure_one()
        text = ("Cierre de Mercado Pago del %s con diferencia de %s: reporte %s, confirmado "
                "%s, descartado %s. Explicación: %s" % (
                    self.date.strftime("%d/%m/%Y"), _money(self.difference),
                    _money(self.report_total), _money(self.confirmed_total),
                    _money(self.ignored_total), self.note))
        self.message_post(body=text)
        group = self.env.ref("distri_pedido.grupo_gerencia", raise_if_not_found=False)
        users = group and self.env["res.users"].sudo().search(
            [("all_group_ids", "in", group.ids), ("share", "=", False)], limit=5)
        for user in users or []:
            self.activity_schedule("mail.mail_activity_data_warning",
                                   summary="Diferencia en el cierre de Mercado Pago",
                                   note=text, user_id=user.id)

    def action_open_collections(self):
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Cobros del %s" % self.date.strftime("%d/%m"),
            "res_model": "distri.mp.collection",
            "view_mode": "list,form",
            "domain": [("date_day", "=", self.date), ("kind", "=", "income")],
        }


# ====================================================================== receipts


class WhatsappMessageComprobante(models.Model):
    """A photo or PDF received by WhatsApp, read as a payment receipt.

    Never creates a payment (decision 10): it proposes who paid and stays as
    backup. The customer comes from the order number written next to it, the
    phone or the payer's CUIT read on the receipt; the amount, operation
    number, date, payer and destination from the photo (local OCR) or the
    PDF text (models/ocr_comprobante.py), else from the caption. The
    administrator can always type the amount: a typed amount is «Cargado a
    mano» and nothing read later replaces it.
    """

    _inherit = "distri.whatsapp.message"

    distri_is_receipt = fields.Boolean(string="Es comprobante", index=True)
    distri_partner_id = fields.Many2one("res.partner", string="Cliente",
                                        domain="[('distri_generic_partner', '=', False)]")
    distri_amount = fields.Float(string="Importe del comprobante", digits=(16, 2))
    distri_operation = fields.Char(string="Número de operación", index=True)
    distri_receipt_state = fields.Selection([
        ("pending", "Esperando el reporte"),
        ("matched", "Con ingreso"),
        ("no_money", "Sin transferencia"),
        ("dismissed", "Descartado"),
        ("duplicate", "Ya recibido"),
        ("registered", "Ya registrado"),
        ("outgoing", "Devolución o pago propio"),
    ], string="Estado del comprobante", index=True)
    distri_receipt_note = fields.Char(
        string="Aclaración", readonly=True,
        help="Por qué el comprobante no espera el reporte: ya se había recibido, su "
             "transferencia ya está registrada o es un pago de la propia cuenta.")
    distri_conflict = fields.Char(
        string="Mismo comprobante, dos clientes", readonly=True,
        help="El mismo número de operación llegó para dos clientes distintos.")
    distri_payer_own = fields.Boolean(
        string="Pagó la cuenta propia", readonly=True,
        help="Quien paga en el comprobante es la cuenta de la empresa: una devolución o un "
             "pago propio, nunca un cobro.")
    distri_other_account = fields.Boolean(
        string="A otra cuenta", compute="_compute_distri_other_account",
        help="El destino leído en el comprobante no es la cuenta propia.")
    distri_collection_id = fields.Many2one("distri.mp.collection", string="Cobro",
                                           readonly=True, ondelete="set null")
    distri_order_ref = fields.Char(string="Pedido mencionado", readonly=True)
    distri_file_text = fields.Text(string="Texto leído del comprobante", readonly=True)
    distri_read_amount = fields.Float(string="Importe leído", digits=(16, 2), readonly=True)
    distri_amount_confidence = fields.Selection([
        (CONFIDENCE_HIGH, "Leído"),
        (CONFIDENCE_LOW, "Revisar importe"),
        ("none", "Sin importe leído"),
        ("manual", "Cargado a mano"),
    ], string="Importe", readonly=True, copy=False)
    distri_amount_review = fields.Boolean(
        string="Revisar importe", compute="_compute_distri_amount_review", store=True,
        help="El importe no se pudo leer con seguridad: confirmalo mirando la foto.")
    distri_receipt_date = fields.Datetime(string="Fecha del comprobante", readonly=True)
    distri_payer_name = fields.Char(string="Quién pagó", readonly=True)
    distri_payer_cuit = fields.Char(string="CUIT de quien pagó", readonly=True)
    distri_dest_name = fields.Char(string="Destinatario", readonly=True)
    distri_dest_cuit = fields.Char(string="CUIT del destinatario", readonly=True)
    distri_destination = fields.Char(string="Destino (alias, CVU o CBU)", readonly=True)
    distri_bank = fields.Char(string="Banco o billetera", readonly=True)
    distri_partner_source = fields.Char(
        string="Cliente propuesto por", readonly=True,
        help="De dónde salió el cliente: el pedido escrito junto a la foto, el teléfono, la "
             "nota del vendedor, el CUIT de quien pagó o el administrador.")

    @api.depends("distri_amount_confidence", "distri_is_receipt", "distri_receipt_state")
    def _compute_distri_amount_review(self):
        for msg in self:
            msg.distri_amount_review = bool(
                msg.distri_is_receipt
                and msg.distri_receipt_state in ("pending", "no_money")
                and msg.distri_amount_confidence in (CONFIDENCE_LOW, "none"))

    @api.depends("distri_dest_cuit", "distri_destination", "distri_dest_name")
    def _compute_distri_other_account(self):
        own = own_account(self.env)
        for msg in self:
            msg.distri_other_account = msg._distri_other_account(own)

    def _distri_other_account(self, own):
        """The destination read on the receipt is not the company's own
        account (CUIT, CVU/CBU, alias or holder)."""
        self.ensure_one()
        values = [v for v in (self.distri_dest_cuit, self.distri_destination) if v]
        if not values:
            return False
        for value in values:
            if digits(value) in own or value.lower() in own:
                return False
        name = " ".join((self.distri_dest_name or "").lower().split())
        return name not in own

    def _distri_destination_label(self):
        """«Alias juan.perez.mp · Juan Pérez» for the card."""
        self.ensure_one()
        account = self.distri_destination or ""
        if account:
            account = ("CVU/CBU %s" % account if account.isdigit() else "Alias %s" % account)
        parts = [account, "CUIT %s" % format_cuit(self.distri_dest_cuit)
                 if self.distri_dest_cuit else "", self.distri_dest_name or ""]
        return " · ".join(p for p in parts if p) or False

    def _distri_local_reference(self):
        """The moment the message arrived, as a naive Argentine datetime, for
        the dates on the receipt that lack a year or a day."""
        self.ensure_one()
        if not self.timestamp:
            return None
        return self.timestamp.replace(tzinfo=timezone.utc).astimezone(AR_TZ).replace(tzinfo=None)

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        records.filtered(lambda m: m.direction == "in")._distri_read_receipt()
        records.filtered(lambda m: m.direction == "in"
                         and m.message_type == "text")._distri_apply_late_note()
        return records

    def write(self, vals):
        matching = self.env.context.get("distri_receipt_matching")
        if "distri_amount" in vals and not matching:
            # typed by the administrator: it wins over anything read
            vals = dict(vals, distri_amount_confidence="manual")
        if vals.get("distri_partner_id") and not matching and "distri_partner_source" not in vals:
            vals = dict(vals, distri_partner_source="Elegido a mano en el comprobante")
        result = super().write(vals)
        if vals.get("media_state") == "done":
            self.filtered(lambda m: m.direction == "in")._distri_read_receipt(read_file=True)
        if not matching and (
                {"distri_amount", "distri_operation", "distri_partner_id"} & set(vals)):
            for receipt in self.filtered(lambda m: m.distri_receipt_state in ("pending", "no_money")):
                receipt = receipt.with_context(distri_receipt_matching=True)
                receipt._distri_classify()
                receipt._distri_match_income()
        if not matching and (VERIFY_CARD_FIELDS & set(vals)):
            self._distri_sync_verify_card()
        return result

    def _distri_nearby_text(self):
        """Caption plus the text messages sent by the same number around it."""
        self.ensure_one()
        texts = [self.body or ""]
        # a seller forwards receipts of many customers in a row: only the
        # text written right next to the photo belongs to it
        window = SELLER_TEXT_WINDOW if (self.distri_forwarded_by_id
                                        or self.distri_chat_import_id) else RECEIPT_TEXT_WINDOW
        if self.timestamp and self.wa_id:
            texts += self.sudo().search([
                ("wa_id", "=", self.wa_id), ("direction", "=", "in"), ("id", "!=", self.id),
                ("message_type", "=", "text"),
                ("timestamp", ">=", self.timestamp - window),
                ("timestamp", "<=", self.timestamp + window),
            ]).mapped("body")
        return "\n".join(t for t in texts if t)

    def _distri_apply_late_note(self):
        """A seller's note written after the photo was already read: it names
        the customer of the last receipt the seller forwarded just before,
        while that receipt is still open and nothing proposed a customer."""
        Partner = self.env["res.partner"].sudo()
        for note in self.sudo():
            if not (note.wa_id and note.timestamp and (note.body or "").strip()):
                continue
            receipt = note.search([
                ("wa_id", "=", note.wa_id), ("direction", "=", "in"), ("id", "!=", note.id),
                ("message_type", "in", ("image", "document")),
                ("distri_forwarded_by_id", "!=", False), ("distri_is_receipt", "=", True),
                ("timestamp", ">=", note.timestamp - SELLER_NOTE_WINDOW),
                ("timestamp", "<=", note.timestamp),
            ], order="timestamp desc, id desc", limit=1)
            # a customer from the order, the payer's CUIT or the administrator stays
            if not receipt or receipt.distri_partner_id or not receipt._distri_is_open():
                continue
            partners = Partner._distri_partners_by_text(note.body)
            if len(partners) != 1:
                continue
            receipt.write({
                "distri_partner_id": partners.id,
                "distri_partner_source": "Nota de %s junto a la foto" % (
                    receipt.distri_forwarded_by_id.name),
            })
            income = receipt.distri_collection_id
            if income.kind == "income" and income.state == "draft":
                income._propose()

    def _distri_is_open(self):
        """Still waiting for the report, or tied to a transfer not confirmed yet."""
        self.ensure_one()
        if self.distri_receipt_state in ("pending", "no_money"):
            return True
        return self.distri_receipt_state == "matched" and \
            self.distri_collection_id.state == "draft"

    def _distri_pdf_text(self):
        self.ensure_one()
        attachment = self.sudo().attachment_id
        if not attachment or attachment.mimetype != "application/pdf":
            return ""
        try:
            reader = PdfReader(io.BytesIO(attachment.raw))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:3])
        except Exception as exc:  # a broken PDF is still a receipt to look at
            _logger.info("Receipt %s: PDF text not readable (%s)", self.id, type(exc).__name__)
            return ""

    def _distri_file_reading(self):
        """(text, parsed fields) of the attached photo or PDF."""
        self.ensure_one()
        attachment = self.sudo().attachment_id
        if not attachment:
            return "", {}
        context = {"reference": self._distri_local_reference(), "own": own_account(self.env)}
        if attachment.mimetype == "application/pdf":
            text = self._distri_pdf_text()
            reading = parse_receipt(text, **context) if text.strip() else {}
            if not reading.get("amount"):
                # a scanned PDF: its text is inside a picture
                for png in pdf_images(attachment.raw):
                    scanned_text, _confidence, scanned = read_image(png, **context)
                    if scanned.get("amount"):
                        return scanned_text, scanned
            return text, reading
        if (attachment.mimetype or "").startswith("image/"):
            text, _confidence, reading = read_image(attachment.raw, **context)
            return text, reading
        return "", {}

    def _distri_file_values(self, reading, caption_amount):
        """Receipt fields from what the file says. The amount follows the
        confidence rules of models/ocr_comprobante.py."""
        self.ensure_one()
        vals = {}
        for field, key in (("distri_payer_name", "payer_name"),
                           ("distri_payer_cuit", "payer_cuit"),
                           ("distri_dest_name", "dest_name"),
                           ("distri_dest_cuit", "dest_cuit"),
                           ("distri_destination", "destination"),
                           ("distri_bank", "bank")):
            if reading.get(key) and not self[field]:
                vals[field] = reading[key]
        if reading.get("operation") and not self.distri_operation:
            vals["distri_operation"] = reading["operation"]
        if reading.get("date") and not self.distri_receipt_date:
            vals["distri_receipt_date"] = local_to_utc(reading["date"], AR_TZ)
        if reading.get("payer_own") and not self.distri_payer_own:
            vals["distri_payer_own"] = True

        read_amount = reading.get("amount") or 0.0
        confidence = reading.get("amount_confidence")
        vals["distri_read_amount"] = read_amount
        if self.distri_amount_confidence == "manual":
            return vals
        current = self.distri_amount or caption_amount
        agrees = bool(read_amount and current and abs(read_amount - current) <= AMOUNT_TOLERANCE)
        if read_amount and confidence == CONFIDENCE_HIGH and (not current or agrees):
            vals.update(distri_amount=read_amount, distri_amount_confidence=CONFIDENCE_HIGH)
        elif agrees:
            # a doubtful reading that matches what the customer wrote
            vals["distri_amount_confidence"] = CONFIDENCE_HIGH
        elif read_amount or current:
            # different amounts, or only one of them and not a sure one: the
            # administrator decides; a doubtful reading never fills the amount
            vals["distri_amount_confidence"] = CONFIDENCE_LOW
        else:
            vals["distri_amount_confidence"] = "none"
        return vals

    def _distri_read_receipt(self, read_file=False):
        """Classifies an incoming photo/PDF and reads what it can."""
        Partner = self.env["res.partner"].sudo()
        for msg in self.sudo():
            if msg.message_type not in ("image", "document"):
                continue
            text = msg._distri_nearby_text()
            vals = {}
            reading = {}
            if read_file:
                file_text, reading = msg._distri_file_reading()
                if file_text:
                    vals["distri_file_text"] = file_text[:20000]
                    text = "\n".join(t for t in (text, file_text) if t)
            order_ref = _ORDER_RE.search(text or "")
            if order_ref and not msg.distri_order_ref:
                vals["distri_order_ref"] = order_ref.group(0)
            if not msg.distri_partner_id:
                partner = Partner.browse()
                source = ""
                if order_ref:
                    order = self.env["sale.order"].sudo().search(
                        [("name", "=", order_ref.group(0))], limit=1)
                    if order and not order.partner_id.distri_generic_partner:
                        partner = order.partner_id.commercial_partner_id
                        source = "Pedido %s escrito junto a la foto" % order.name
                if not partner and (msg.distri_forwarded_by_id or msg.distri_chat_import_id):
                    # the seller wrote the customer (name or CUIT) next to it;
                    # the seller's own number says nothing about the customer.
                    # A receipt of the staff's transfers group (chat imported,
                    # puesta_al_dia.py) works the same way.
                    partners = Partner._distri_partners_by_text(msg._distri_nearby_text())
                    partner = partners if len(partners) == 1 else Partner.browse()
                    source = "Nota de %s junto a la foto" % (
                        msg.distri_forwarded_by_id.name or msg.distri_chat_sender or "")
                elif not partner:
                    partners = Partner._distri_partners_by_phone(msg.wa_id)
                    partner = partners if len(partners) == 1 else Partner.browse()
                    source = "Teléfono de WhatsApp del cliente"
                if not partner and reading.get("payer_cuit"):
                    partners = Partner._distri_partners_by_cuit(reading["payer_cuit"])
                    partner = partners if len(partners) == 1 else Partner.browse()
                    source = "CUIT de quien pagó (%s), leído de la foto" % format_cuit(
                        reading["payer_cuit"])
                if partner:
                    vals["distri_partner_id"] = partner.id
                    vals["distri_partner_source"] = source
            caption_amount = parse_amount_ar(msg._distri_nearby_text())
            if read_file:
                vals.update(msg._distri_file_values(reading, caption_amount))
            elif not msg.distri_amount and caption_amount:
                vals["distri_amount"] = caption_amount
            if not msg.distri_operation and not vals.get("distri_operation"):
                operation = parse_operation(text)
                if operation:
                    vals["distri_operation"] = operation
            partner = Partner.browse(vals.get("distri_partner_id")) or msg.distri_partner_id
            is_receipt = bool(
                msg.distri_forwarded_by_id
                or msg.media_mime_type == "application/pdf" or vals.get("distri_amount")
                or vals.get("distri_read_amount") or reading.get("operation")
                or msg.distri_amount or order_ref or _RECEIPT_WORDS.search(text or "")
                or (partner and self.env["distri.promised.transfer"].sudo().search_count([
                    ("partner_id", "child_of", partner.id),
                    ("state", "in", ("open", "overdue"))])))
            if is_receipt and not msg.distri_is_receipt:
                vals["distri_is_receipt"] = True
            if (is_receipt or msg.distri_is_receipt) and not msg.distri_receipt_state:
                vals["distri_receipt_state"] = "pending"
            if vals:
                msg.with_context(distri_receipt_matching=True).write(vals)
            if msg.distri_receipt_state in ("pending", "no_money"):
                msg._distri_classify()
                msg._distri_match_income()
            msg._distri_sync_verify_card()

    def action_distri_use_read_amount(self):
        """«Usar importe leído»: the administrator checked the photo."""
        for msg in self:
            if not msg.distri_read_amount:
                raise UserError("No hay un importe leído en el comprobante.")
            msg.write({"distri_amount": msg.distri_read_amount})
        return True

    def action_distri_read_again(self):
        """«Leer de nuevo»: runs the OCR / PDF reading again."""
        self.filtered(lambda m: m.attachment_id)._distri_read_receipt(read_file=True)
        return True

    def _distri_close(self, state, note):
        """The receipt stops waiting for the report: already received,
        already registered or the company's own payment."""
        self.ensure_one()
        self.with_context(distri_receipt_matching=True).write({
            "distri_receipt_state": state, "distri_receipt_note": note})
        self._distri_sync_verify_card()

    def _distri_classify(self):
        """Before looking for the transfer: a receipt of the company's own
        payment (a refund), one already received (same operation number, or
        the same amount, date and time for a bank receipt without one) or
        the same operation sent for two different customers."""
        self.ensure_one()
        receipt = self.sudo()
        if receipt.distri_receipt_state not in ("pending", "no_money"):
            return
        operation = receipt.distri_operation
        # the operation number of an income of the report: a customer's
        # payment, whatever the photo says about who paid
        income = operation and self.env["distri.mp.collection"].sudo().search_count([
            ("kind", "=", "income"), "|", ("source_id", "=", operation),
            ("bank_transfer_id", "=", operation)])
        if receipt.distri_payer_own and not income:
            receipt._distri_close(
                "outgoing", "Quien paga es la cuenta propia: devolución o pago propio, no un cobro.")
            return
        if operation and not income:
            outgoing = self.env["distri.mp.outgoing"].sudo().search(
                [("source_id", "=", digits(operation))], limit=1)
            if outgoing:
                receipt._distri_close("outgoing", "La operación %s es un egreso de la cuenta de "
                                      "Mercado Pago del %s (%s): no es un cobro." % (
                                          operation, local_when(outgoing.date),
                                          _money(abs(outgoing.amount))))
                return
        twins = receipt._distri_twins()
        if not twins:
            return
        partners = (twins | receipt).mapped("distri_partner_id.commercial_partner_id")
        if len(partners) > 1:
            # the same receipt for two customers: every card to «Revisar»
            note = TWO_CUSTOMERS % (partners[0].name, partners[1].name)
            for msg in (twins | receipt).filtered(lambda m: m.distri_conflict != note):
                msg.with_context(distri_receipt_matching=True).write({"distri_conflict": note})
                msg._distri_sync_verify_card()
                income = msg.distri_collection_id
                if income.kind == "income" and income.state == "draft":
                    income.write({"conflict_note": note})
            return
        first = twins.sorted(lambda m: (m.timestamp or datetime.min, m.id))[0]
        if (first.timestamp or datetime.min, first.id) < (receipt.timestamp or datetime.min,
                                                          receipt.id):
            if partners and not first.distri_partner_id and first._distri_is_open():
                # the copy names the customer the first one lacked
                first.write({"distri_partner_id": partners.id,
                             "distri_partner_source": "Cliente del mismo comprobante recibido "
                                                      "de nuevo el %s" % local_when(receipt.timestamp)})
            receipt._distri_close("duplicate", "Ya recibido el %s (%s)." % (
                local_when(first.timestamp),
                first.distri_forwarded_by_id.name or first.profile_name or first.wa_id or ""))

    def _distri_twins(self):
        """Other receipts of the same transfer: the same operation number, or
        (a bank receipt without one) the same amount, date and time."""
        self.ensure_one()
        base = [("id", "!=", self.id), ("distri_is_receipt", "=", True),
                ("distri_receipt_state", "not in", ("dismissed", "duplicate"))]
        if self.distri_operation:
            return self.sudo().search(base + [("distri_operation", "=", self.distri_operation)])
        if self.distri_amount and self.distri_receipt_date:
            return self.sudo().search(base + [
                ("distri_operation", "=", False),
                ("distri_receipt_date", "=", self.distri_receipt_date),
                ("distri_amount", ">=", self.distri_amount - AMOUNT_TOLERANCE),
                ("distri_amount", "<=", self.distri_amount + AMOUNT_TOLERANCE)])
        return self.browse()

    def _distri_match_income(self):
        """A receipt that arrives (or gets its amount) after the report: look
        for its transfer among the ones loaded. By operation number first
        (allowing one OCR slip when the amount is the same), else by amount
        around the date and time read from the photo (or, without one, the
        moment it arrived). A transfer already confirmed marks the receipt
        «Ya registrado»: never a second payment."""
        self.ensure_one()
        receipt = self.sudo()
        if receipt.distri_receipt_state not in ("pending", "no_money"):
            return False
        Collection = self.env["distri.mp.collection"].sudo()
        base = [("kind", "=", "income"), ("state", "=", "draft"), ("receipt_id", "=", False)]
        found = Collection.browse()
        operation, amount = receipt.distri_operation, receipt.distri_amount
        if operation:
            owner = Collection.search([("kind", "=", "income"), "|",
                                       ("source_id", "=", operation),
                                       ("bank_transfer_id", "=", operation)])
            if not owner and amount and digits(operation):
                owner = Collection.search([
                    ("kind", "=", "income"),
                    ("amount", ">=", amount - AMOUNT_TOLERANCE),
                    ("amount", "<=", amount + AMOUNT_TOLERANCE),
                ]).filtered(lambda c: operation_close(operation, c.source_id)
                            or operation_close(operation, c.bank_transfer_id))
            if owner:
                # its transfer is loaded: that one or none, never by amount
                found = Collection.search(base + [("id", "in", owner.ids)], limit=1)
                if not found:
                    receipt._distri_already(owner)
                    return False
        if not found and amount:
            found = receipt._distri_by_amount(base)
        if not found:
            return False
        found._link_receipt(receipt)
        found._propose()
        return found

    def _distri_already(self, owner):
        """The transfer of the receipt is loaded but not pending: confirmed
        (the payment exists: «Ya registrado») or with another receipt."""
        self.ensure_one()
        confirmed = owner.filtered(lambda c: c.state == "confirmed")[:1]
        if confirmed:
            self._distri_close("registered", "Ya registrado: %s confirmado el %s (pago %s)." % (
                confirmed.name, local_when(confirmed.confirmed_at),
                confirmed.payment_id.name or ""))

    def _distri_by_amount(self, base):
        """The pending transfer of the same amount: within 2 minutes of the
        date and time read from the photo when it has one, else the closest
        one around that date or around the moment the photo arrived. A
        confirmed transfer within those 2 minutes marks the receipt «Ya
        registrado»."""
        self.ensure_one()
        receipt = self
        Collection = self.env["distri.mp.collection"].sudo()
        read, arrived = receipt.distri_receipt_date, receipt.timestamp
        amount = [("amount", ">=", receipt.distri_amount - AMOUNT_TOLERANCE),
                  ("amount", "<=", receipt.distri_amount + AMOUNT_TOLERANCE)]
        windows = []
        if read:
            windows.append([("date", ">=", read - RECEIPT_DATE_WINDOW),
                            ("date", "<=", read + RECEIPT_DATE_WINDOW)])
        if arrived:
            windows.append([("date", ">=", arrived - RECEIPT_AFTER),
                            ("date", "<=", arrived + RECEIPT_BEFORE)])
        if not windows:
            return Collection
        domain = ["|"] * (len(windows) - 1)
        for window in windows:
            domain += ["&"] + window
        if read:
            registered = Collection.search([
                ("kind", "=", "income"), ("state", "=", "confirmed"),
                ("date", ">=", read - RECEIPT_TIME_CLOSE),
                ("date", "<=", read + RECEIPT_TIME_CLOSE)] + amount)
            if len(registered) == 1:
                receipt._distri_already(registered)
                return Collection
        candidates = Collection.search(base + amount + domain)
        # a transfer whose own receipt (same operation number) is waiting
        # is not taken by another receipt of the same amount
        numbers = [n for n in candidates.mapped("source_id")
                   + candidates.mapped("bank_transfer_id") if n]
        waiting = set(self.sudo().search([
            ("id", "!=", receipt.id), ("distri_is_receipt", "=", True),
            ("distri_receipt_state", "in", ("pending", "no_money")),
            ("distri_operation", "in", numbers),
        ]).mapped("distri_operation")) if numbers else set()
        candidates = candidates.filtered(
            lambda c: not ({c.source_id, c.bank_transfer_id} & waiting))
        if read:
            close = candidates.filtered(lambda c: abs(c.date - read) <= RECEIPT_TIME_CLOSE)
            if close:
                candidates = close
        return candidates.sorted(lambda c: c._receipt_distance(receipt))[:1]

    def action_distri_mark_receipt(self):
        # a receipt authorized for delivery (or paid by hand before
        # 19.0.1.21.0) never waits for the report again behind its card's back
        held = self.sudo().filtered(lambda m: m.distri_collection_id.kind == "verify" and
                                    m.distri_collection_id.state in ("authorized", "confirmed"))
        if held:
            card = held[:1].distri_collection_id
            raise UserError(
                "El comprobante ya tiene %s (%s): deshacé primero eso en el tablero de cobros." % (
                    "la entrega autorizada" if card.state == "authorized"
                    else "el pago registrado a mano", card.name))
        self.with_context(distri_receipt_matching=True).write(
            {"distri_is_receipt": True, "distri_receipt_state": "pending",
             "distri_receipt_note": False})
        for msg in self:
            msg._distri_match_income()
        self._distri_sync_verify_card()

    def action_distri_dismiss_receipt(self):
        self.write({"distri_receipt_state": "dismissed"})

    # ------------------------------------------------------------ «Esperando reporte MP»

    def _distri_sync_verify_card(self):
        """Every receipt waiting for the report («Esperando el reporte») has
        one card in «Esperando reporte MP», kept in step with the receipt.
        Never two cards for the same receipt."""
        Collection = self.env["distri.mp.collection"].sudo()
        for msg in self.sudo():
            if not msg.id or not msg.distri_is_receipt:
                continue
            cards = Collection.search([("receipt_id", "=", msg.id),
                                       ("kind", "in", ("verify", "receipt"))])
            verify = cards.filtered(lambda c: c.kind == "verify")
            if msg.distri_receipt_state in RECEIPT_CLOSED_STATES:
                reason, note = {
                    "dismissed": ("not_receipt", "Descartado en «Comprobantes por WhatsApp»"),
                    "duplicate": ("receipt_error", msg.distri_receipt_note),
                    "registered": ("receipt_late", msg.distri_receipt_note),
                    "outgoing": ("own", msg.distri_receipt_note),
                }[msg.distri_receipt_state]
                verify.filtered(lambda c: c.state == "draft").write({
                    "state": "ignored", "ignore_reason": reason, "ignore_note": note or False})
                continue
            if msg.distri_receipt_state != "pending":
                continue
            if cards.filtered(lambda c: c.kind == "receipt" and c.state == "draft") or \
                    verify.filtered(lambda c: c.state in ("authorized", "confirmed")):
                continue
            card = verify.filtered(lambda c: c.state == "draft")[:1]
            if not card and verify:
                # dismissed before and marked as a receipt again: the same card
                card = verify[:1]
                card.write({"state": "draft", "ignore_reason": False, "ignore_note": False})
            if not card:
                card = Collection.create({"kind": "verify", "receipt_id": msg.id})
                msg.with_context(distri_receipt_matching=True).distri_collection_id = card
            card._fill_from_receipt(msg)

    @api.model
    def _distri_backfill_verify_cards(self):
        """On module update: receipts already waiting get their card."""
        receipts = self.sudo().search([("distri_is_receipt", "=", True),
                                       ("distri_receipt_state", "=", "pending")])
        receipts._distri_sync_verify_card()
        _logger.info("Esperando reporte MP: %s receipts waiting", len(receipts))
        return len(receipts)
