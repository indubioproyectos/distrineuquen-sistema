"""Customer suggested by the text sent after a receipt (19.0.1.43.0).

The manager's chat export often brings the receipt photo without a caption
and the customer's name as a separate text message right after it
(«José Namuncurá» for «JOSE RUBALDO CARO NAMUNCURA»). The exact matching of
`_distri_partners_by_text` (models/cobros.py) misses those names: other word
order, accents, the «Y - » prefix of the zone, the locality in parentheses.

- `_distri_following_text`: the text messages of the same sender after the
  photo, until that sender's next photo or document, within the seller window.
  The receipt viewer (models/migracion.py) shows it when there is no caption.
- `name_tokens` / `note_tokens`: normalized words, compared as sets. Every
  meaningful word of the note must be a word of the customer's name (a word of
  4 letters or more may be the start of one: «Nico» for «NICOLAS»), and one of
  them outside the parentheses.
- Only when exactly one active customer matches, the receipt gets a
  suggestion (`distri_suggested_partner_id`), never the customer: the card
  shows it and «Usar este cliente» assigns it through «Elegir cliente»
  (`distri.mp.assign`, «Asignado por ...»).
- `_distri_refresh_suggestions`: fills the suggestions of the receipts already
  imported (the 19.0.1.43.0 migration runs it); it assigns nothing.
"""
import bisect
import logging
import re
import unicodedata

from odoo import api, fields, models
from odoo.exceptions import UserError

from .cobros import RECEIPT_TEXT_WINDOW, SELLER_NOTE_WINDOW, SELLER_TEXT_WINDOW, _check_cobros

_logger = logging.getLogger(__name__)

# «Y - », «X - »: the zone letter in front of the customer's name
_ZONE_PREFIX_RE = re.compile(r"^\s*[a-z]\s*-\s*")
_PARENS_RE = re.compile(r"\(([^)]*)\)")
# «<Se editó este mensaje.>» and the like, added by the chat export
_EXPORT_MARK_RE = re.compile(r"<[^>]*>")
_WORD_RE = re.compile(r"[a-z0-9]+")
NAME_STOPWORDS = {"de", "del", "la", "las", "el", "los", "y", "e", "a", "al", "en", "con",
                  "por", "para"}
# words of a note that never name the customer («MERCADO CHRISTIAN AL DIA»)
NOTE_NOISE = NAME_STOPWORDS | {
    "dia", "ok", "llego", "pago", "pagos", "pagado", "pagaron", "transferencia", "transf",
    "comprobante", "gracias", "cliente", "es", "se", "hola", "buenas", "buen", "dias",
    "tardes", "noches", "listo", "edito", "este", "mensaje"}
# a note longer than this is a sentence, not a name
NOTE_MAX_WORDS = 6
# a word of the note may be the start of a word of the name from this length
PREFIX_MIN_LEN = 4


def fold(text):
    """Lower case, without accents."""
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def name_tokens(name):
    """(core, place): the words of a customer's name, without the zone
    prefix; `place` the words in parentheses only (the locality)."""
    text = _ZONE_PREFIX_RE.sub("", fold(name))
    place = set()
    for inside in _PARENS_RE.findall(text):
        place |= set(_WORD_RE.findall(inside))
    core = set(_WORD_RE.findall(_PARENS_RE.sub(" ", text))) - NAME_STOPWORDS
    return core, place - core


def note_tokens(line):
    """The meaningful words of one line of a note, or an empty set when the
    line clearly is not a name (a sentence, only numbers or filler words)."""
    text = _ZONE_PREFIX_RE.sub("", fold(_EXPORT_MARK_RE.sub(" ", line or "")))
    words = {w for w in _WORD_RE.findall(text)
             if w not in NOTE_NOISE and (len(w) >= 2 or w.isdigit())}
    if not words or len(words) > NOTE_MAX_WORDS:
        return set()
    if not any(len(w) >= 3 and not w.isdigit() for w in words):
        return set()
    return words


def _word_hits(word, words):
    return word in words or (len(word) >= PREFIX_MIN_LEN
                             and any(w.startswith(word) for w in words))


class CustomerNameIndex:
    """The normalized names of the customers, built once per batch."""

    def __init__(self, rows):
        self.core = {}
        self.by_word = {}
        for partner_id, name in rows:
            core, place = name_tokens(name)
            if not core:
                continue
            self.core[partner_id] = core
            for word in core | place:
                self.by_word.setdefault(word, set()).add(partner_id)
        self.words = sorted(self.by_word)

    def _with_word(self, word):
        found = set(self.by_word.get(word, ()))
        if len(word) >= PREFIX_MIN_LEN:
            start = bisect.bisect_left(self.words, word)
            for other in self.words[start:]:
                if not other.startswith(word):
                    break
                found |= self.by_word[other]
        return found

    def match(self, words):
        """Ids of the customers whose name has every word, one of them
        outside the parentheses."""
        ids = None
        for word in words:
            ids = self._with_word(word) if ids is None else ids & self._with_word(word)
            if not ids:
                return set()
        return {i for i in ids or () if any(_word_hits(w, self.core[i]) for w in words)}

    def suggest(self, text):
        """(partner id, line) when the lines of the text that name exactly one
        customer all name the same one, else (False, "")."""
        found = {}
        for line in (text or "").splitlines():
            words = note_tokens(line)
            ids = self.match(words) if words else set()
            if len(ids) == 1:
                found.setdefault(ids.pop(), " ".join(_EXPORT_MARK_RE.sub(" ", line).split()))
        if len(found) != 1:
            return False, ""
        return next(iter(found.items()))


class ResPartnerSugerencia(models.Model):
    _inherit = "res.partner"

    @api.model
    def _distri_name_index(self):
        """The same customers `_distri_partners_by_text` looks in."""
        rows = self.sudo().search_read(
            [("distri_generic_partner", "=", False), ("active", "=", True),
             ("parent_id", "=", False),
             ("id", "not in", self.env["res.company"].sudo().search([]).partner_id.ids)],
            ["name"])
        return CustomerNameIndex([(r["id"], r["name"]) for r in rows])


class WhatsappMessageSugerencia(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_suggested_partner_id = fields.Many2one(
        "res.partner", string="Cliente sugerido", readonly=True, copy=False,
        help="El texto que mandaron después de la foto coincide con un solo cliente. No se "
             "asigna solo: lo confirma el administrador con «Usar este cliente».")
    distri_suggestion_note = fields.Char(string="Por qué se sugiere", readonly=True, copy=False)

    def write(self, vals):
        if vals.get("distri_partner_id"):
            # the receipt has its customer: the suggestion is over
            vals = dict(vals, distri_suggested_partner_id=False, distri_suggestion_note=False)
        return super().write(vals)

    def _distri_following_text(self):
        """The text messages the same sender wrote right after this photo or
        document, before the sender's next one, within the seller window
        (`_distri_nearby_text`). A chat export keeps only the minute: when
        the sender sent several photos close together, the notes pair with
        them by their order in the export (`_distri_chat_paired_notes`,
        models/cobros.py), and nobody's when that order says nothing."""
        self.ensure_one()
        if not (self.timestamp and self.wa_id):
            return ""
        paired = self._distri_chat_paired_notes()
        if paired is not None:
            return "\n".join(paired)
        window = SELLER_TEXT_WINDOW if (self.distri_forwarded_by_id
                                        or self.distri_chat_import_id) else RECEIPT_TEXT_WINDOW
        domain = [("wa_id", "=", self.wa_id), ("direction", "=", "in"), ("id", "!=", self.id),
                  ("message_type", "in", ("text", "image", "document")),
                  ("timestamp", ">=", self.timestamp),
                  ("timestamp", "<=", self.timestamp + window)]
        if self.distri_chat_import_id:
            domain.append(("distri_chat_import_id", "=", self.distri_chat_import_id.id))
        later = self.sudo().search(domain, order="timestamp, id")
        media = later.filtered(lambda m: m.message_type != "text")
        same_minute = media.filtered(lambda m: m.timestamp == self.timestamp)
        if same_minute.filtered(lambda m: m.id > self.id):
            return ""
        stop = min((m.timestamp for m in media if m.timestamp > self.timestamp), default=None)
        texts = [(m.body or "").strip() for m in later if m.message_type == "text"
                 and (not same_minute or m.timestamp > self.timestamp)
                 and (stop is None or m.timestamp < stop)]
        return "\n".join(t for t in texts if t)

    def _distri_receipt_text(self):
        """(text, following): the caption, else the text sent right after."""
        self.ensure_one()
        caption = (self.body or "").strip()
        if caption:
            return caption, False
        following = self._distri_following_text()
        return following, bool(following)

    def _distri_suggest_partner(self, index=None):
        """Suggests the customer named by the receipt's text when exactly one
        matches; never assigns it. Returns the receipts with a suggestion."""
        index = index or self.env["res.partner"]._distri_name_index()
        suggested = self.browse()
        for msg in self.sudo():
            partner_id, line = False, ""
            # a receipt in conflict (two customers, or the note against the
            # payer's CUIT) waits for a person: no suggestion either
            if msg.distri_is_receipt and not msg.distri_partner_id and not msg.distri_conflict \
                    and (msg.distri_forwarded_by_id or msg.distri_chat_import_id):
                partner_id, line = index.suggest(msg._distri_receipt_text()[0])
            if not partner_id and msg._distri_partner_suggest_only() \
                    and msg.distri_suggested_partner_id:
                # 19.0.1.126.1: the suggestion the reading of a staff upload
                # left (CUIT, order) stays
                continue
            vals = {"distri_suggested_partner_id": partner_id,
                    "distri_suggestion_note": "Por el texto «%s»" % line if partner_id else False}
            if msg.distri_suggested_partner_id.id != partner_id or \
                    (msg.distri_suggestion_note or False) != vals["distri_suggestion_note"]:
                msg.with_context(distri_receipt_matching=True).write(vals)
            if partner_id:
                suggested |= msg
        return suggested

    def _distri_read_receipt(self, read_file=False):
        super()._distri_read_receipt(read_file=read_file)
        todo = self.sudo().filtered(lambda m: m.message_type in ("image", "document")
                                    and m.distri_is_receipt and not m.distri_partner_id
                                    and (m.distri_forwarded_by_id or m.distri_chat_import_id))
        if todo:
            todo._distri_suggest_partner()

    def _distri_apply_late_note(self):
        """A seller's note that names no customer exactly may still suggest one
        for the receipt it follows."""
        super()._distri_apply_late_note()
        receipts = self.browse()
        for note in self.sudo():
            if not (note.wa_id and note.timestamp and (note.body or "").strip()):
                continue
            receipts |= note.search([
                ("wa_id", "=", note.wa_id), ("direction", "=", "in"), ("id", "!=", note.id),
                ("message_type", "in", ("image", "document")),
                ("distri_forwarded_by_id", "!=", False), ("distri_is_receipt", "=", True),
                ("distri_partner_id", "=", False),
                ("timestamp", ">=", note.timestamp - SELLER_NOTE_WINDOW),
                ("timestamp", "<=", note.timestamp),
            ], order="timestamp desc, id desc", limit=1)
        receipts = receipts.filtered(lambda r: r._distri_is_open())
        if receipts:
            receipts._distri_suggest_partner()

    @api.model
    def _distri_refresh_suggestions(self, domain=None):
        """Fills the suggestions of the receipts already imported (chat
        exports and forwarded by a seller) without a customer. It assigns
        nothing. From odoo shell:
            env["distri.whatsapp.message"]._distri_refresh_suggestions(); env.cr.commit()
        """
        receipts = self.sudo().search((domain or []) + [
            ("distri_is_receipt", "=", True), ("distri_partner_id", "=", False),
            "|", ("distri_chat_import_id", "!=", False), ("distri_forwarded_by_id", "!=", False)])
        # the receipts that got a customer meanwhile drop theirs (write())
        index = self.env["res.partner"]._distri_name_index()
        suggested = receipts._distri_suggest_partner(index)
        _logger.info("Clientes sugeridos: %s de %s comprobantes sin cliente",
                     len(suggested), len(receipts))
        return {"receipts": len(receipts), "suggested": len(suggested)}


class DistriMpCollectionSugerencia(models.Model):
    _inherit = "distri.mp.collection"

    suggested_partner_id = fields.Many2one(related="receipt_id.distri_suggested_partner_id",
                                           string="Cliente sugerido")
    suggestion_note = fields.Char(related="receipt_id.distri_suggestion_note",
                                  string="Por qué se sugiere")

    def action_use_suggestion(self):
        """«Usar este cliente»: the suggested customer, assigned by hand as
        «Elegir cliente» does (the payer's CUIT is not learned)."""
        self.ensure_one()
        _check_cobros(self.env)
        receipt = self.sudo().receipt_id
        partner = receipt.distri_suggested_partner_id
        if not partner:
            raise UserError("Este cobro no tiene un cliente sugerido.")
        if self.partner_id:
            raise UserError("El cobro ya tiene cliente: usá «Corregir cliente».")
        self.env["distri.mp.assign"].create({
            "collection_id": self.id, "partner_id": partner.id, "learn": False})._apply()
        receipt.with_context(distri_receipt_matching=True).write(
            {"distri_suggested_partner_id": False, "distri_suggestion_note": False})
        return True
