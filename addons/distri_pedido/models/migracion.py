"""Migration board (19.0.1.41.0).

Design: documentacion/modulos/distri_pedido/cobros/propuesta-tablero-migracion-2026-10-27.md
(items 1 to 4 of its §7).

The board creates nothing: every row is an existing card of the collections
board (`distri.mp.collection`), with the decisions already taken (the step 14
cards, the links with Online, the confirmations). It adds an evidence grade
and a box to each card, computed and stored so the kanban groups and counts
them:

- money grade: how strongly the WhatsApp photo is tied to the Mercado Pago
  movement (A operation number, A1 operation with one OCR slip, B same amount
  and stamp within 2 min, C same amount within the module's window, D other).
  The operation number proves which transfer the photo is, never whose.
- customer sources: the independent evidences of the customer (the payer's
  CUIT learned from the report, the seller's note, the CUIT read on the photo
  when it is another CUIT, the phone, the Online payment, a choice by hand)
  and whether they agree.
- Online grade: how the card was tied to a payment of Online.
- duplicates: two receipt cards with the same operation number, a photo whose
  operation is another movement, or two receipts on the same movement. Both
  sides are blocked until a person keeps one.
- box: the board lists only the cards that come from a WhatsApp receipt; the
  Mercado Pago report only verifies it. «Listo para confirmar» needs money
  grade A, B or N (payer name on the receipt = MP payer), two agreeing
  customer sources, not in Online, no duplicate. A movement without receipt
  is stored as «Falta el comprobante», is not on the board and never reduces
  a debt. A receipt without movement («Sin movimiento en MP») is never a
  payment. Links with Online by amount alone are listed apart: they can be
  wrong (another customer's CUIT, a wholesaler's CUIT, far in days).

Approving goes through `distri.migration.approve`: a preview with every
guard (receipt, customer, duplicate, Online, reseller, credit balance, the
Online mirror synced less than 24 h ago, the cut-off date and the top box for
a batch, at most 50 per batch), then the board's own `action_confirm`.

The «Última sincronización con Online» is the ir.config_parameter
distri_pedido.online_sync_at, written by every load of the Online mirror
(models/puesta_al_dia.py); before 19.0.1.41.0 it falls back to the last
`synced_at` of the mirror.
"""
import re
import unicodedata
from datetime import datetime, time, timedelta, timezone

from markupsafe import Markup, escape

from odoo import api, fields, models
from odoo.exceptions import UserError

from .cobros import (
    AMOUNT_TOLERANCE, RECEIPT_AFTER, RECEIPT_BEFORE, RECEIPT_CLOSED_STATES, RECEIPT_DATE_WINDOW,
    RECEIPT_TEXT_WINDOW, RECEIPT_TIME_CLOSE, _check_admin, digits, local_when, operation_close,
)
from .puesta_al_dia import PARAM_ONLINE_SYNC, _check_admin_or_raise
from .reparto import _money
from .reparto_cliente import AR_TZ
from .revision_cliente import is_reseller

PARAM_CUTOFF = "distri_pedido.migracion_desde"
DEFAULT_CUTOFF = "2026-09-15"
SYNC_MAX_AGE = timedelta(hours=24)
BATCH_LIMIT = 50
# the Online payment more than this from the transfer: «lejano»
ONLINE_FAR_DAYS = 3

MONEY_GRADES = [
    ("A", "A · Número de operación"),
    ("A1", "A1 · Operación con un dígito distinto"),
    ("B", "B · Importe y hora (≤ 2 min)"),
    ("N", "N · Nombre del pagador coincide"),
    ("C", "C · Importe dentro de la ventana"),
    ("D", "D · Otro (visión o a mano)"),
    ("none", "Sin foto"),
]
AGREEMENTS = [
    ("agree", "Dos fuentes coinciden"),
    ("single", "Una sola fuente"),
    ("disagree", "Fuentes en desacuerdo"),
    ("none", "Ninguna fuente"),
]
ONLINE_GRADES = [
    ("memo", "Por memo (exportado del lab)"),
    ("close", "Fecha cercana, mismo cliente"),
    ("far", "Más de 3 días entre transferencia y pago"),
    ("other_customer", "Otro cliente"),
    ("conflict", "Posible doble carga"),
    ("none", "No está en Online"),
]
# the board shows only the cards that come from a WhatsApp receipt: an MP
# movement without receipt is stored as «missing_receipt» and never listed
BOARD_BOXES = [
    ("to_approve", "Listo para confirmar"),
    ("review", "En revisión"),
    ("no_customer", "Sin cliente"),
    ("duplicate", "Duplicado"),
    ("waiting_money", "Sin movimiento en MP (esperando)"),
    ("online", "Ya en Online"),
    ("confirmed", "Confirmado en el lab"),
    ("closed", "Cerrados"),
]
BOXES = BOARD_BOXES[:4] + [("missing_receipt", "Falta el comprobante")] + BOARD_BOXES[4:]
BOX_HELP = {
    "to_approve": "Comprobante de WhatsApp verificado con el reporte de Mercado Pago (número de "
                  "operación, importe y hora a 2 minutos, o el nombre del pagador), dos fuentes "
                  "del cliente que coinciden, no está en Online y no está repetido.",
    "review": "Tiene comprobante y movimiento, pero la verificación es más débil, el cliente tiene "
              "una sola fuente o las fuentes no coinciden: lo decide una persona.",
    "duplicate": "Otro comprobante tiene el mismo número de operación, la foto es de otro "
                 "movimiento o dos comprobantes apuntan al mismo: no se confirma ninguno hasta "
                 "descartar el que sobra.",
    "no_customer": "Tiene comprobante y movimiento, pero ninguna fuente dice de qué cliente es.",
    "missing_receipt": "Movimiento de Mercado Pago sin comprobante de WhatsApp. Nunca baja la deuda "
                       "de un cliente: pedí el comprobante o marcalo como de mayorista o revendedor.",
    "waiting_money": "Comprobante de WhatsApp sin movimiento de Mercado Pago. Nunca es un pago: se "
                     "resuelve con el próximo reporte, o se cierra como copia, devolución u otra "
                     "cuenta.",
    "online": "Ya cargado en Odoo Online (o un cobro del lab que Online ya tiene: posible doble "
              "carga).",
    "confirmed": "Confirmado en el lab: curación, se revisa por la fuerza de su evidencia.",
    "closed": "Copias, devoluciones, descartados y mayoristas apartados.",
}
SOURCE_LABELS = {
    "cuit_identity": "CUIT del reporte",
    "seller_note": "Nota del vendedor",
    "photo_cuit": "CUIT leído en la foto",
    "phone": "Teléfono",
    "order": "Pedido",
    "online": "Pago de Online",
    "manual": "Elegido a mano",
    "photo": "Foto",
}
MIGRATION_FIELDS = ("money_grade", "receipt_minutes", "customer_sources", "customer_agreement",
                    "online_grade", "migration_duplicate", "migration_box", "migration_in_period",
                    "evidence_note")
APPROVABLE_GRADES = ("A", "B", "N")


# ====================================================================== helpers


def migration_cutoff(env):
    """First day of the migration period (parameter, 2026-09-15 by default)."""
    raw = env["ir.config_parameter"].sudo().get_param(PARAM_CUTOFF) or DEFAULT_CUTOFF
    try:
        return fields.Date.to_date(raw.strip())
    except (ValueError, AttributeError):
        return fields.Date.to_date(DEFAULT_CUTOFF)


def cutoff_moment(env):
    """00:00 Argentine time of the cut-off day, as naive UTC."""
    day = migration_cutoff(env)
    return datetime.combine(day, time.min).replace(tzinfo=AR_TZ).astimezone(
        timezone.utc).replace(tzinfo=None)


def online_sync_at(env):
    """When the Online mirror was last applied (naive UTC), or None."""
    raw = env["ir.config_parameter"].sudo().get_param(PARAM_ONLINE_SYNC)
    if raw:
        try:
            return fields.Datetime.to_datetime(raw)
        except ValueError:
            pass
    env["distri.online.payment"].flush_model(["synced_at"])
    env.cr.execute("SELECT max(synced_at) FROM distri_online_payment")
    return env.cr.fetchone()[0]


def online_sync_state(env):
    """{at, label, stale, hours}: the KPI «Última sincronización con Online»."""
    at = online_sync_at(env)
    if not at:
        return {"at": False, "stale": True, "hours": None,
                "label": "Nunca se sincronizó con Online: no se aprueba nada."}
    age = fields.Datetime.now() - at
    hours = age.total_seconds() / 3600
    stamp = at.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")
    stale = age > SYNC_MAX_AGE
    ago = "%d min" % (age.total_seconds() // 60) if hours < 1 else "%d h" % hours
    label = "Última sincronización con Online: %s (hace %s)." % (stamp, ago)
    if stale:
        label += (" Tiene más de 24 h: Online puede tener pagos nuevos. Sincronizá antes de "
                  "aprobar (scripts/online/sincronizar-online.sh).")
    return {"at": at, "stale": stale, "hours": hours, "label": label}


def tab_action(url):
    """Opens a record in a new browser tab."""
    return {"type": "ir.actions.act_url", "url": url, "target": "new"}


def record_url(record):
    return "/odoo/%s/%s" % (record._name, record.id)


def account_url(partner):
    """«Cuenta corriente» of the customer (server action on res.partner)."""
    return "/odoo/%s/action-distri_pedido.action_partner_account_tab" % (
        partner.commercial_partner_id.id)


def receipt_file_url(receipt):
    attachment = receipt.sudo().attachment_id
    return "/web/content/%s" % attachment.id if attachment else False


def _name_words(text):
    text = unicodedata.normalize("NFKD", (text or "").lower())
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    return {w for w in re.split(r"[^a-z0-9]+", text) if len(w) >= 3}


def names_match(receipt_name, payer_name):
    """The payer read on the receipt is Mercado Pago's payer: at least two
    words in common (or every word of a one-word name)."""
    a, b = _name_words(receipt_name), _name_words(payer_name)
    if not a or not b:
        return False
    return len(a & b) >= min(2, len(a), len(b))


def _source_key(text):
    text = text or ""
    if text.startswith("Nota de"):
        return "seller_note"
    if text.startswith("CUIT"):
        return "photo_cuit"
    if text.startswith("Teléfono"):
        return "phone"
    if text.startswith("Pedido"):
        return "order"
    if text.startswith(("Asignado", "Elegido")):
        return "manual"
    return "photo"


# ====================================================================== cards


class DistriMpCollectionMigration(models.Model):
    _inherit = "distri.mp.collection"

    money_grade = fields.Selection(MONEY_GRADES, string="Fuerza del vínculo",
                                   compute="_compute_migration", store=True, index=True)
    receipt_minutes = fields.Integer(
        string="Minutos entre foto y transferencia", compute="_compute_migration", store=True,
        help="Distancia entre la fecha leída en el comprobante y la transferencia.")
    customer_sources = fields.Char(string="Fuentes del cliente", compute="_compute_migration",
                                   store=True)
    customer_agreement = fields.Selection(AGREEMENTS, string="Acuerdo de las fuentes",
                                          compute="_compute_migration", store=True, index=True)
    online_grade = fields.Selection(ONLINE_GRADES, string="Vínculo con Online",
                                    compute="_compute_migration", store=True, index=True)
    migration_duplicate = fields.Char(
        string="Posible duplicado", compute="_compute_migration", store=True,
        help="Otro comprobante tiene el mismo número de operación, la foto es de otro "
             "movimiento o dos comprobantes apuntan a este movimiento. No se aprueba ninguno "
             "hasta descartar el que sobra.")
    migration_box = fields.Selection(BOXES, string="Caja", compute="_compute_migration",
                                     store=True, index=True,
                                     group_expand="_migration_expand_boxes")
    migration_in_period = fields.Boolean(
        string="Desde la fecha de corte", compute="_compute_migration", store=True, index=True)
    evidence_note = fields.Char(string="Evidencia", compute="_compute_migration", store=True)
    receipt_context_html = fields.Html(string="Mensaje de WhatsApp", sanitize=False,
                                       compute="_compute_receipt_context")
    receipt_file_url = fields.Char(compute="_compute_receipt_context")
    receipt_request_text = fields.Text(string="Pedido del comprobante",
                                       compute="_compute_receipt_request")
    # 19.0.1.56.0: typed on the card when the photo was not read; it is the
    # receipt's own amount (manual), and the receipt looks for its transfer
    receipt_amount = fields.Float(related="receipt_id.distri_amount", readonly=False,
                                  string="Importe del comprobante")
    receipt_read_amount = fields.Float(related="receipt_id.distri_read_amount",
                                       string="Importe leído en la foto")
    receipt_amount_confidence = fields.Selection(related="receipt_id.distri_amount_confidence",
                                                 string="Confianza del importe")
    receipt_payer_name = fields.Char(related="receipt_id.distri_payer_name",
                                     string="Pagador leído")
    receipt_payer_cuit = fields.Char(related="receipt_id.distri_payer_cuit",
                                     string="CUIT leído")
    receipt_bank = fields.Char(related="receipt_id.distri_bank", string="Banco o billetera")
    receipt_partner_source = fields.Char(related="receipt_id.distri_partner_source",
                                         string="Cliente de la foto propuesto por")

    def action_receipt_use_read_amount(self):
        """«Usar importe leído» on the card: the administrator checked the photo."""
        self.ensure_one()
        self.receipt_id.action_distri_use_read_amount()
        return True

    def _migration_expand_boxes(self, values, domain):
        """The kanban shows the boards' boxes (an MP movement without receipt
        is never on the migration board)."""
        return [key for key, _label in BOARD_BOXES]

    # ------------------------------------------------------------ evidence

    @api.depends("kind", "state", "amount", "date", "source_id", "bank_transfer_id",
                 "payer_cuit", "partner_id", "proposal", "receipt_id", "reversal",
                 "conflict_note", "ignore_reason", "online_conflict", "online_status",
                 "online_payment_id.partner_id", "online_payment_id.link_kind",
                 "online_payment_id.date", "receipt_id.distri_operation",
                 "receipt_id.distri_amount", "receipt_id.distri_receipt_date",
                 "receipt_id.timestamp", "receipt_id.distri_partner_id",
                 "receipt_id.distri_partner_source", "receipt_id.distri_payer_cuit")
    def _compute_migration(self):
        cuits = {c.payer_cuit for c in self if c.payer_cuit}
        owners = {}
        if cuits:
            for identity in self.env["distri.payer.identity"].sudo().search(
                    [("kind", "=", "cuit"), ("value", "in", list(cuits))]):
                owners.setdefault(identity.value, set()).add(
                    identity.partner_id.commercial_partner_id.id)
        duplicates = self._migration_duplicates()
        wholesale = set(self.env["distri.mp.wholesale.rule"].sudo().search(
            [("cuit", "in", list(cuits)), ("active", "=", True)]).mapped("cuit")) if cuits else set()
        since = cutoff_moment(self.env)
        for rec in self:
            grade, minutes = rec._migration_money_grade()
            sources = rec._migration_sources(owners)
            agreement = rec._migration_agreement(sources)
            online = rec._migration_online_grade(sources, wholesale)
            duplicate = duplicates.get(rec._origin.id or rec.id, "")
            rec.money_grade = grade
            rec.receipt_minutes = minutes
            rec.customer_sources = " + ".join(SOURCE_LABELS[k] for k in sorted(sources)) or False
            rec.customer_agreement = agreement
            rec.online_grade = online
            rec.migration_duplicate = duplicate or False
            rec.migration_in_period = bool(rec.date and rec.date >= since)
            rec.migration_box = rec._migration_box(grade, agreement, duplicate)
            labels = dict(MONEY_GRADES)
            parts = ["Plata: %s" % labels[grade] + (
                " (%s min)" % minutes if grade in ("B", "C") and rec.receipt_id.distri_receipt_date
                else "")]
            parts.append("Cliente: %s (%s)" % (rec.customer_sources or "sin fuente",
                                               dict(AGREEMENTS)[agreement].lower()))
            if online != "none":
                parts.append("Online: %s" % dict(ONLINE_GRADES)[online].lower())
            if duplicate:
                parts.append("Duplicado: %s" % duplicate)
            rec.evidence_note = " · ".join(parts)

    def _migration_money_grade(self):
        """(grade, minutes between the stamp read on the photo and the transfer)."""
        self.ensure_one()
        receipt = self.receipt_id
        read = receipt.distri_receipt_date
        minutes = int(abs((read - self.date).total_seconds()) // 60) if (
            receipt and read and self.date) else 0
        if not receipt or self.kind != "income":
            return "none", minutes
        operation = digits(receipt.distri_operation)
        own = {digits(self.source_id), digits(self.bank_transfer_id)} - {""}
        if operation and operation in own:
            return "A", minutes
        if operation and any(operation_close(operation, number) for number in own):
            return "A1", minutes
        same_amount = bool(receipt.distri_amount and abs(
            receipt.distri_amount - self.amount) <= AMOUNT_TOLERANCE)
        if same_amount and read and self.date and abs(read - self.date) <= RECEIPT_TIME_CLOSE:
            return "B", minutes
        if names_match(receipt.distri_payer_name, self.payer_name):
            return "N", minutes
        arrived = receipt.timestamp
        if same_amount and self.date and (
                (read and abs(read - self.date) <= RECEIPT_DATE_WINDOW)
                or (arrived and self.date - RECEIPT_BEFORE <= arrived
                    <= self.date + RECEIPT_AFTER)):
            return "C", minutes
        return "D", minutes

    def _migration_sources(self, owners):
        """{source key: set of commercial partner ids}."""
        self.ensure_one()
        sources = {}
        if self.kind == "income" and self.payer_cuit in owners:
            sources["cuit_identity"] = set(owners[self.payer_cuit])
        receipt = self.receipt_id
        if receipt.distri_partner_id:
            text = receipt.distri_partner_source or ""
            key = _source_key(text)
            # confirming a card writes its customer on the receipt («Elegido a
            # mano en el comprobante»): that is the same decision, not a source
            derived = text.startswith("Elegido a mano") and self.state != "draft"
            # the CUIT read on the photo is the report's own CUIT: one evidence
            same_cuit = key == "photo_cuit" and digits(receipt.distri_payer_cuit) and \
                digits(receipt.distri_payer_cuit) == digits(self.payer_cuit)
            if not derived and not same_cuit:
                sources.setdefault(key, set()).add(
                    receipt.distri_partner_id.commercial_partner_id.id)
        if self.proposal == "manual" and self.partner_id:
            sources.setdefault("manual", set()).add(self.partner_id.commercial_partner_id.id)
        online_partner = self.online_payment_id.partner_id
        if online_partner:
            sources["online"] = {online_partner.commercial_partner_id.id}
        return sources

    def _migration_agreement(self, sources):
        self.ensure_one()
        if not sources:
            return "none"
        named = set().union(*sources.values())
        card = self.partner_id.commercial_partner_id.id
        if len(named) > 1 or (card and card not in named):
            return "disagree"
        return "agree" if len(sources) >= 2 else "single"

    def _migration_online_grade(self, sources=None, wholesale=()):
        self.ensure_one()
        pay = self.online_payment_id
        if self.online_conflict:
            return "conflict"
        if not pay:
            return "none"
        if pay.link_kind == "memo":
            return "memo"
        online = pay.partner_id.commercial_partner_id.id
        card = self.partner_id.commercial_partner_id.id
        photo = self.receipt_id.distri_partner_id.commercial_partner_id.id
        owners = (sources or {}).get("cuit_identity", set())
        if online and ((card and card != online) or (photo and photo != online)
                       or (owners and online not in owners)
                       or (pay.link_kind == "amount" and self.payer_cuit in wholesale)):
            # the payer's CUIT is another customer's, or a wholesaler's
            return "other_customer"
        if pay.date and self.date_day and abs((self.date_day - pay.date).days) > ONLINE_FAR_DAYS:
            return "far"
        return "close"

    def _migration_box(self, grade, agreement, duplicate):
        self.ensure_one()
        in_online = self.online_conflict or self.online_status == "already" or (
            self.state == "ignored" and self.ignore_reason == "online")
        if in_online:
            return "online"
        if self.state in ("ignored", "apartado"):
            return "closed"
        if self.state == "confirmed":
            return "confirmed"
        if self.kind != "income":
            # a receipt without movement: never a payment
            return "waiting_money"
        if not self.receipt_id:
            return "missing_receipt"
        if duplicate:
            return "duplicate"
        if not self.partner_id:
            return "no_customer"
        if (grade in APPROVABLE_GRADES and agreement == "agree" and not duplicate
                and not self.reversal and not self.conflict_note):
            return "to_approve"
        return "review"

    def _migration_duplicates(self):
        """{card id: why} for the income cards of self whose receipt is
        repeated: the same operation number on another receipt card, a photo
        whose operation is another movement (both sides), or two open
        receipts on the same movement. Read in SQL over all the cards."""
        ids = [rec._origin.id or rec.id for rec in self]
        ids = [i for i in ids if isinstance(i, int)]
        if not ids:
            return {}
        Collection = self.env["distri.mp.collection"]
        Collection.flush_model(["receipt_id", "state", "kind", "source_id", "bank_transfer_id",
                                "name"])
        self.env["distri.whatsapp.message"].flush_model(
            ["distri_operation", "distri_collection_id", "distri_is_receipt",
             "distri_receipt_state"])
        cr = self.env.cr
        cr.execute("""
            SELECT c.id, c.name, m.id, m.distri_operation
              FROM distri_mp_collection c JOIN distri_whatsapp_message m ON m.id = c.receipt_id
             WHERE c.kind = 'income' AND c.state != 'ignored'
               AND coalesce(m.distri_operation, '') != ''""")
        by_operation, receipt_op, names = {}, {}, {}
        for card_id, name, _msg_id, operation in cr.fetchall():
            number = digits(operation)
            names[card_id] = name
            if len(number) < 6:
                continue
            by_operation.setdefault(number, []).append(card_id)
            receipt_op[card_id] = number
        cr.execute("""
            SELECT id, name, source_id, bank_transfer_id FROM distri_mp_collection
             WHERE kind = 'income' AND (source_id IS NOT NULL OR bank_transfer_id IS NOT NULL)""")
        by_source = {}
        for card_id, name, source, transfer in cr.fetchall():
            names.setdefault(card_id, name)
            for number in {digits(source), digits(transfer)} - {""}:
                by_source[number] = card_id
        pointer, pointed = {}, {}
        for card_id, number in receipt_op.items():
            target = by_source.get(number)
            if target and target != card_id:
                pointer[card_id] = target
                pointed.setdefault(target, []).append(card_id)
        cr.execute("""
            SELECT distri_collection_id, count(*) FROM distri_whatsapp_message
             WHERE distri_collection_id = ANY(%s) AND distri_is_receipt
               AND coalesce(distri_receipt_state, '') NOT IN %s
             GROUP BY 1 HAVING count(*) > 1""", [ids, tuple(RECEIPT_CLOSED_STATES)])
        crowded = dict(cr.fetchall())
        result = {}
        for card_id in ids:
            why = []
            number = receipt_op.get(card_id)
            twins = [c for c in by_operation.get(number, []) if c != card_id] if number else []
            if twins:
                why.append("mismo número de operación que %s" % ", ".join(
                    names.get(c, "") for c in twins[:3]))
            if card_id in pointer:
                why.append("la foto es de la operación de %s" % names.get(pointer[card_id], ""))
            if card_id in pointed and card_id in receipt_op:
                why.append("la foto de %s es de este movimiento" % ", ".join(
                    names.get(c, "") for c in pointed[card_id][:3]))
            elif card_id in pointed:
                # no number read on this card's receipt (or no receipt): still
                # another photo claims this movement
                why.append("la foto de %s es de este movimiento" % ", ".join(
                    names.get(c, "") for c in pointed[card_id][:3]))
            if crowded.get(card_id):
                why.append("%s comprobantes apuntan a este movimiento" % crowded[card_id])
            if why:
                text = "; ".join(why)
                result[card_id] = text[:1].upper() + text[1:]
        return result

    def _migration_recompute(self):
        """Recomputes the evidence of these cards now (the customer sources
        and the duplicates depend on other records)."""
        cards = self.sudo()
        if not cards:
            return
        for fname in MIGRATION_FIELDS:
            self.env.add_to_compute(cards._fields[fname], cards)
        cards.flush_recordset(list(MIGRATION_FIELDS))

    # ------------------------------------------------------------ viewer

    @api.depends("receipt_id")
    def _compute_receipt_context(self):
        Message = self.env["distri.whatsapp.message"].sudo()
        for rec in self:
            msg = rec.receipt_id.sudo()
            rec.receipt_file_url = receipt_file_url(msg) if msg else False
            if not msg:
                rec.receipt_context_html = False
                continue
            sender = msg.distri_chat_sender or msg.profile_name or msg.wa_id or "—"
            when = msg.timestamp.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime(
                "%d/%m/%Y %H:%M") if msg.timestamp else "—"
            html = Markup("<div class='o_distri_wa_message'>")
            html += Markup("<p class='mb-1'><b>Enviado por</b> %s · <b>%s</b></p>") % (sender, when)
            # without a caption, the text the sender wrote right after the photo
            # (models/sugerencia_cliente.py)
            text, following = msg._distri_receipt_text()
            label = "Texto del comprobante (mensaje siguiente):" if following \
                else "Texto junto a la foto:"
            html += Markup("<p class='mb-2'><b>%s</b> %s</p>") % (
                label, Markup("<br/>").join(text.splitlines()) if text
                else Markup("<i>sin texto</i>"))
            if msg.distri_suggested_partner_id and not rec.partner_id:
                html += Markup("<p class='mb-2'><b>Cliente sugerido:</b> %s · %s</p>") % (
                    msg.distri_suggested_partner_id.name, msg.distri_suggestion_note or "")
            nearby = Message.browse()
            if msg.timestamp:
                domain = [("id", "!=", msg.id), ("direction", "=", "in"),
                          ("message_type", "=", "text"),
                          ("timestamp", ">=", msg.timestamp - RECEIPT_TEXT_WINDOW),
                          ("timestamp", "<=", msg.timestamp + RECEIPT_TEXT_WINDOW)]
                if msg.distri_chat_import_id:
                    domain.append(("distri_chat_import_id", "=", msg.distri_chat_import_id.id))
                elif msg.wa_id:
                    domain.append(("wa_id", "=", msg.wa_id))
                else:
                    domain = None
                if domain:
                    nearby = Message.search(domain, order="timestamp, id", limit=40)
            if nearby:
                html += Markup("<p class='mb-1'><b>Mensajes cercanos (±30 min):</b></p><ul class='mb-2'>")
                for other in nearby:
                    who = other.distri_chat_sender or other.profile_name or other.wa_id or ""
                    stamp = other.timestamp.replace(tzinfo=timezone.utc).astimezone(
                        AR_TZ).strftime("%H:%M")
                    line = Markup("%s · %s: %s") % (stamp, who, (other.body or "")[:500])
                    if who == sender:
                        line = Markup("<b>%s</b>") % line
                    html += Markup("<li>%s</li>") % line
                html += Markup("</ul>")
            else:
                html += Markup("<p class='mb-2 text-muted'>No hay otros mensajes guardados "
                               "±30 min.</p>")
            if rec.receipt_file_url:
                html += Markup("<p class='mb-0'><a href='%s' target='_blank' rel='noopener'>"
                               "<i class='fa fa-external-link'/> Abrir la foto en tamaño "
                               "completo (pestaña nueva)</a></p>") % rec.receipt_file_url
            html += Markup("</div>")
            rec.receipt_context_html = html

    @api.depends("date", "amount", "payer_name", "payer_cuit")
    def _compute_receipt_request(self):
        for rec in self:
            payer = " de %s" % rec.payer_name if rec.payer_name else ""
            rec.receipt_request_text = (
                "Hola, ¿nos pasan el comprobante de la transferencia del %s por %s%s "
                "(operación %s)? Gracias." % (local_when(rec.date) or "", _money(rec.amount),
                                              payer, rec.source_id or "")) \
                if rec.kind == "income" else False

    # ------------------------------------------------------------ actions

    def action_tab_card(self):
        self.ensure_one()
        return tab_action(record_url(self))

    def action_tab_payment(self):
        self.ensure_one()
        if not self.payment_id:
            raise UserError("Este cobro no tiene pago registrado.")
        return tab_action(record_url(self.payment_id))

    def action_tab_receipt(self):
        self.ensure_one()
        url = receipt_file_url(self.receipt_id) if self.receipt_id else False
        if not url:
            raise UserError("Este cobro no tiene foto.")
        return tab_action(url)

    def action_tab_account(self):
        self.ensure_one()
        if not self.partner_id:
            raise UserError("Este cobro no tiene cliente.")
        return tab_action(account_url(self.partner_id))

    def action_receipt_viewer(self):
        """The photo large, with the WhatsApp message under it (dialog)."""
        self.ensure_one()
        if not self.receipt_id:
            raise UserError("Este cobro no tiene foto.")
        return {
            "type": "ir.actions.act_window",
            "name": "Comprobante",
            "res_model": "distri.mp.collection",
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("distri_pedido.view_mp_collection_receipt_viewer").id, "form")],
            "target": "new",
        }

    def action_request_receipt(self):
        """«Pedir comprobante»: a text to copy into the group, nothing is sent."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_window",
            "name": "Pedir comprobante",
            "res_model": "distri.mp.collection",
            "res_id": self.id,
            "view_mode": "form",
            "views": [(self.env.ref("distri_pedido.view_mp_collection_receipt_request").id,
                       "form")],
            "target": "new",
        }

    def action_migration_approve(self):
        """«Aprobar» one card: the same preview and guards as the batch."""
        _check_admin(self.env)
        return self.env["distri.migration.approve"]._open(self, mode="single")

    def _migration_problem(self, mode, sync, cutoff, duplicates=None):
        """Why this card cannot be approved now, or "". Every guard, read
        live (not from the stored box)."""
        self.ensure_one()
        card = self.sudo()
        if card.kind != "income" or card.state != "draft":
            return "ya no está pendiente"
        if not card.receipt_id:
            return "sin comprobante de WhatsApp: un movimiento sin comprobante no baja la deuda"
        if not card.partner_id:
            return "sin cliente"
        if sync["stale"]:
            return "la sincronización con Online tiene más de 24 h"
        duplicate = (duplicates if duplicates is not None
                     else card._migration_duplicates()).get(card.id)
        if duplicate:
            return "duplicado: %s" % duplicate
        if card.online_payment_id or card.online_status != "none":
            return "ya está en Online"
        if card.online_conflict:
            return "posible doble carga en Online"
        if card.reversal:
            return card.reversal
        if card.conflict_note:
            return card.conflict_note
        problem = card._bulk_problem()
        if problem:
            return problem
        if is_reseller(card.partner_id):
            return "el cliente es revendedor o genérico"
        if mode == "batch":
            if card.migration_box != "to_approve":
                return "no está en «Listo para confirmar»"
            if not card.date or card.date < cutoff:
                return "anterior a la fecha de corte"
        return ""


class DistriPayerIdentityMigration(models.Model):
    _inherit = "distri.payer.identity"

    def _repropose(self):
        result = super()._repropose()
        values = [i.value for i in self if i.kind == "cuit" and i.value]
        if values:
            self.env["distri.mp.collection"].sudo().search(
                [("kind", "=", "income"), ("payer_cuit", "in", values)])._migration_recompute()
        return result


# ====================================================================== summary


class DistriMigrationSummary(models.TransientModel):
    """«Tablero de migración»: the KPIs on top and one button per box."""
    _name = "distri.migration.summary"
    _description = "Tablero de migración"

    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    cutoff = fields.Date(string="Desde (fecha de corte)", compute="_compute_summary")
    sync_label = fields.Char(string="Online", compute="_compute_summary")
    sync_stale = fields.Boolean(compute="_compute_summary")
    links_count = fields.Integer(string="Vínculos nuevos por importe",
                                 compute="_compute_summary")
    links_amount = fields.Monetary(compute="_compute_summary")
    conflict_count = fields.Integer(string="Posibles dobles cargas", compute="_compute_summary")
    conflict_amount = fields.Monetary(compute="_compute_summary")
    duplicate_count = fields.Integer(string="Duplicados", compute="_compute_summary")
    weak_online_count = fields.Integer(compute="_compute_summary")
    box_html = fields.Html(sanitize=False, compute="_compute_summary")
    count_to_approve = fields.Integer(string="Para aprobar", compute="_compute_summary")
    amount_to_approve = fields.Monetary(string="Importe · Para aprobar", compute="_compute_summary")
    count_review = fields.Integer(string="En revisión", compute="_compute_summary")
    amount_review = fields.Monetary(string="Importe · En revisión", compute="_compute_summary")
    count_no_customer = fields.Integer(string="Sin cliente", compute="_compute_summary")
    amount_no_customer = fields.Monetary(string="Importe · Sin cliente", compute="_compute_summary")
    count_duplicate = fields.Integer(string="Duplicado", compute="_compute_summary")
    amount_duplicate = fields.Monetary(string="Importe · Duplicado", compute="_compute_summary")
    count_waiting_money = fields.Integer(string="Esperando el movimiento", compute="_compute_summary")
    amount_waiting_money = fields.Monetary(string="Importe · Esperando el movimiento", compute="_compute_summary")
    count_online = fields.Integer(string="Ya en Online", compute="_compute_summary")
    amount_online = fields.Monetary(string="Importe · Ya en Online", compute="_compute_summary")
    count_confirmed = fields.Integer(string="Confirmado en el lab", compute="_compute_summary")
    amount_confirmed = fields.Monetary(string="Importe · Confirmado en el lab", compute="_compute_summary")
    count_closed = fields.Integer(string="Cerrados", compute="_compute_summary")
    amount_closed = fields.Monetary(string="Importe · Cerrados", compute="_compute_summary")

    def _links_domain(self):
        sync = online_sync_at(self.env)
        if not sync:
            return [("id", "=", 0)]
        return [("online_payment_id.linked_at", ">=", sync),
                ("online_payment_id.link_kind", "=", "amount")]

    def _period_domain(self):
        # only the cards that come from a WhatsApp receipt
        return [("migration_in_period", "=", True), ("receipt_id", "!=", False)]

    def _compute_summary(self):
        Collection = self.env["distri.mp.collection"].sudo()
        sync = online_sync_state(self.env)
        groups = dict(((box,), (n, total)) for box, n, total in Collection._read_group(
            self._period_domain(), ["migration_box"], ["__count", "amount:sum"]))
        links = Collection._read_group(self._links_domain(), [], ["__count", "amount:sum"])[0]
        conflicts = Collection._read_group([("online_conflict", "!=", False)], [],
                                           ["__count", "amount:sum"])[0]
        duplicates = Collection.search_count(self._period_domain() + [
            ("migration_duplicate", "!=", False), ("state", "=", "draft")])
        weak_online = Collection.search_count(self._weak_online_domain())
        for wiz in self:
            wiz.cutoff = migration_cutoff(self.env)
            wiz.sync_label = sync["label"]
            wiz.sync_stale = sync["stale"]
            wiz.links_count, wiz.links_amount = links[0], links[1] or 0.0
            wiz.conflict_count, wiz.conflict_amount = conflicts[0], conflicts[1] or 0.0
            wiz.duplicate_count = duplicates
            rows = Markup("")
            wiz.weak_online_count = weak_online
            for key, label in BOARD_BOXES:
                n, total = groups.get((key,), (0, 0.0))
                wiz["count_%s" % key] = n
                wiz["amount_%s" % key] = total or 0.0
                rows += Markup("<tr><td><b>%s</b></td><td class='text-end'>%s</td>"
                               "<td class='text-end'>%s</td><td class='text-muted small'>%s</td>"
                               "</tr>") % (label, n, _money(total or 0.0), BOX_HELP[key])
            wiz.box_html = Markup(
                "<table class='table table-sm mb-0'><thead><tr><th>Caja</th>"
                "<th class='text-end'>Tarjetas</th><th class='text-end'>Importe</th><th/></tr>"
                "</thead><tbody>%s</tbody></table>") % rows

    @api.model
    def action_open(self):
        """Menu «Tablero de migración»: refreshes the evidence of the period
        and the pending cards, then the summary."""
        _check_admin_or_raise(self.env)
        Collection = self.env["distri.mp.collection"].sudo()
        Collection.search(["|", ("date", ">=", cutoff_moment(self.env)),
                           ("state", "=", "draft")])._migration_recompute()
        summary = self.create({})
        return {
            "type": "ir.actions.act_window",
            "name": "Tablero de migración",
            "res_model": self._name,
            "res_id": summary.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "current",
        }

    def action_refresh_all(self):
        _check_admin_or_raise(self.env)
        self.env["distri.mp.collection"].sudo().search([])._migration_recompute()
        return self.action_open()

    def _board(self, name, domain, group_by="migration_box", view="kanban"):
        action = self.env["ir.actions.act_window"]._for_xml_id("distri_pedido.action_migration_board")
        action.update(name=name, domain=domain,
                      context={"group_by": group_by} if group_by else {})
        if view == "list":
            action["views"] = [(self.env.ref("distri_pedido.view_migration_list").id, "list"),
                               (False, "form")]
            action["view_mode"] = "list,form"
        return action

    def action_open_board(self):
        # every card with its WhatsApp receipt (19.0.1.47.0): «Desde la fecha
        # de corte» stays as a filter of the board; the boxes keep the period
        return self._board("Tablero de migración", [("receipt_id", "!=", False)])

    def action_open_box(self):
        box = self.env.context.get("box")
        label = dict(BOXES).get(box, "")
        return self._board(label, self._period_domain() + [("migration_box", "=", box)],
                           group_by=False, view="list")

    def action_open_links(self):
        return self._board("Vínculos nuevos con Online (última sincronización)",
                           self._links_domain(), group_by=False, view="list")

    def action_open_conflicts(self):
        return self._board("Posibles dobles cargas en Online",
                           [("online_conflict", "!=", False)], group_by=False, view="list")

    def action_open_duplicates(self):
        return self._board("Posibles duplicados", self._period_domain() + [
            ("migration_duplicate", "!=", False), ("state", "=", "draft")], group_by=False,
            view="list")

    def _weak_online_domain(self):
        # links by amount alone can be wrong: with or without receipt
        return [("migration_in_period", "=", True), ("online_payment_id.link_kind", "=", "amount"),
                ("online_grade", "in", ("far", "other_customer", "conflict"))]

    def action_open_weak_online(self):
        return self._board("Ya en Online por importe, para volver a mirar",
                           self._weak_online_domain(), group_by=False, view="list")

    def action_batch_approve(self):
        _check_admin(self.env)
        cards = self.env["distri.mp.collection"].search(self._period_domain() + [
            ("migration_box", "=", "to_approve"), ("state", "=", "draft")],
            order="date desc, id desc", limit=BATCH_LIMIT)
        if not cards:
            raise UserError("No hay tarjetas en «Listo para confirmar» desde la fecha de corte.")
        return self.env["distri.migration.approve"]._open(cards, mode="batch")


# ====================================================================== approval


class DistriMigrationApprove(models.TransientModel):
    """«Aprobar» / «Aprobar lote»: the preview, then the board's own
    confirmation for the cards that pass every guard."""
    _name = "distri.migration.approve"
    _description = "Confirmar desde el tablero de migración"

    mode = fields.Selection([("single", "Una tarjeta"), ("batch", "Lote")], required=True,
                            default="single", readonly=True)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    sync_label = fields.Char(string="Online", readonly=True)
    sync_stale = fields.Boolean(readonly=True)
    line_ids = fields.One2many("distri.migration.approve.line", "wizard_id", string="Tarjetas")
    ok_count = fields.Integer(string="Se aprueban", compute="_compute_totals")
    ok_amount = fields.Monetary(string="Importe que se aprueba", compute="_compute_totals")
    blocked_count = fields.Integer(string="Quedan para revisar", compute="_compute_totals")

    @api.depends("line_ids.problem", "line_ids.amount")
    def _compute_totals(self):
        for wiz in self:
            ok = wiz.line_ids.filtered(lambda l: not l.problem)
            wiz.ok_count = len(ok)
            wiz.ok_amount = sum(ok.mapped("amount"))
            wiz.blocked_count = len(wiz.line_ids) - len(ok)

    @api.model
    def _preview(self, cards, mode):
        """[(card, problem, balance_after, warning)] with every guard."""
        sync = online_sync_state(self.env)
        cutoff = cutoff_moment(self.env)
        duplicates = cards._migration_duplicates()
        Review = self.env["distri.balance.review"].sudo()
        partners = cards.mapped("partner_id")
        if partners:
            Review._distri_refresh(partners)
        lines = self.env["distri.balance.review.line"].sudo().search(
            [("collection_id", "in", cards.ids)])
        by_card = {line.collection_id.id: line for line in lines}
        rows = []
        for card in cards:
            problem = card._migration_problem(mode, sync, cutoff, duplicates)
            line = by_card.get(card.id)
            warning = ""
            if not problem and line and line.red_flag:
                warning = "deja al cliente con saldo a favor (posible venta sin registrar)"
                if mode == "batch":
                    problem, warning = warning, ""
            if not problem and not line:
                problem = "el cliente no está en «Revisión por cliente»"
            rows.append((card, problem, line.balance_after if line else 0.0, warning))
        return sync, rows

    @api.model
    def _open(self, cards, mode):
        _check_admin(self.env)
        if mode == "batch" and len(cards) > BATCH_LIMIT:
            cards = cards[:BATCH_LIMIT]
        cards._migration_recompute()
        sync, rows = self._preview(cards, mode)
        wizard = self.create({
            "mode": mode,
            "sync_label": sync["label"],
            "sync_stale": sync["stale"],
            "line_ids": [(0, 0, {
                "collection_id": card.id,
                "partner_id": card.partner_id.id,
                "amount": card.amount,
                "money_grade": card.money_grade,
                "customer_sources": card.customer_sources,
                "balance_after": balance,
                "problem": problem or False,
                "warning": warning or False,
            }) for card, problem, balance, warning in rows],
        })
        return {
            "type": "ir.actions.act_window",
            "name": "Confirmar lote" if mode == "batch" else "Confirmar",
            "res_model": self._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "views": [(False, "form")],
            "target": "new",
        }

    def action_approve(self):
        """Checks every guard again, then the board's own confirmation, one
        card at a time, each with its evidence in the chatter."""
        _check_admin(self.env)
        self.ensure_one()
        cards = self.line_ids.mapped("collection_id")
        _sync, rows = self._preview(cards, self.mode)
        approved = self.env["distri.mp.collection"]
        skipped = []
        for card, problem, _balance, _warning in rows:
            if problem:
                skipped.append("%s: %s" % (card.name, problem))
                continue
            note = card.evidence_note or ""
            card.action_confirm()
            card.sudo().message_post(body="Aprobado desde el tablero de migración (%s) por %s. "
                                          "%s" % ("lote" if self.mode == "batch" else "una tarjeta",
                                                  self.env.user.name, note))
            approved |= card
        if approved:
            self.env["distri.balance.review"].sudo()._distri_refresh(
                approved.mapped("partner_id"))
        message = "Se aprobaron %s cobros por %s." % (len(approved),
                                                      _money(sum(approved.mapped("amount"))))
        if skipped:
            message += " Quedaron %s sin aprobar: %s." % (len(skipped), "; ".join(skipped[:5]))
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "type": "warning" if skipped or not approved else "success",
                "message": message,
                "sticky": bool(skipped),
                "next": {"type": "ir.actions.act_window_close"},
            },
        }


class DistriMigrationApproveLine(models.TransientModel):
    _name = "distri.migration.approve.line"
    _description = "Tarjeta de la aprobación del tablero de migración"
    _order = "id"

    wizard_id = fields.Many2one("distri.migration.approve", required=True, ondelete="cascade")
    currency_id = fields.Many2one(related="wizard_id.currency_id")
    collection_id = fields.Many2one("distri.mp.collection", string="Tarjeta", readonly=True)
    date = fields.Datetime(related="collection_id.date", string="Fecha")
    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    money_grade = fields.Selection(MONEY_GRADES, string="Plata", readonly=True)
    customer_sources = fields.Char(string="Fuentes del cliente", readonly=True)
    balance_after = fields.Monetary(string="Saldo después", readonly=True)
    problem = fields.Char(string="No se aprueba porque", readonly=True)
    warning = fields.Char(string="Atención", readonly=True)
