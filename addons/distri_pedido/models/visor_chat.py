"""Chat viewer of the transfers group (19.0.1.46.0; two columns 19.0.1.49.0).

Staff open a receipt in its chat context: was the customer's name («leyenda»)
sent before or after the photo, who sent it, what else came that minute.
The viewer is an OWL client action (static/src/js/chat_viewer.js) that reads
pages of messages through `distri.chat.viewer`. Only administración and
gerencia (the same people as the collections board).

Where the messages come from. Every message of the group is a
`distri.whatsapp.message` created by an import of a WhatsApp export
(`distri_chat_import_id`); the live Meta feed never writes group messages.
Each import says whose phone exported it (`distri_export_owner`):

- «manager» (gerente): chat_gerente.zip, 08/04/2025 to 24/10/2026. Its
  texts were created first and its photos after, so the ids (texts ~1869-
  7770, files ~7771-14240) say nothing about the order in the chat.
- «own» (ale): the user's own export, from 23/10/2026.
- «admin» (Denis): his export of 24/10/2026 was never imported as messages.
  Only the receipts the 24/10 cross attached to cards came from it; each one
  has the card note «export del administrador, llegó el <fecha>, lo mandó
  <nombre>» (scripts/cruce-2026-10-24/cargar_mp_body.py). The viewer shows
  those files as messages of Denis when no imported message has the same
  file (same checksum): they are the receipts the manager's export lacks
  (it shows «<Multimedia omitido>» there).

Which source is shown (`select_visible`):

- before OWN_SINCE (24/10/2026, Argentine day): the texts of the manager's
  export only; from that day on, the texts of the user's own export only.
  The same text in two exports is the same message: showing one source per
  day is what avoids the duplicates (the names of the senders differ from
  phone to phone, so they cannot be matched by sender).
- photos and PDF: one message per file content already (the importer skips
  a file it has seen, whatever the export), so every one is shown.
- Denis's files on cards: only before OWN_SINCE, and only when no imported
  message has the same file.

The receipt's own date (19.0.1.49.0, `receipt_moment`), shown on each photo
or PDF next to the time it was sent. In this order:

- «read»: `distri_receipt_date`, the date the module read from the file
  (models/cobros.py `_distri_file_values`) and matches cards with;
- «text»: the text read from the file (`distri_file_text`) parsed again with
  the same parser (models/ocr_comprobante.py `_date`), for the files read
  before that field existed. A bare «a las HH:MM» is not a date: skipped;
- «card»: the transfer date of the linked Mercado Pago card (an income).

A receipt more than a day away from its message is marked (`receipt_far`).

Search (19.0.1.49.0): every visible match is counted; the viewer gets them a
page at a time, newest first, with the snippet around the match.

«Pendientes de aprobar» (19.0.1.92.0, `distri_chat_pending`): the left
column lists the receipts of this chat (an imported message with a photo or
PDF) whose card is still open («Pendiente» or «Autorizado para entregar»),
sent in the last PENDING_DAYS days, newest first. Older open cards are the
leftovers of the manager's history (2025) and stay on the collections board.
Approving or discarding stays on the card itself (grupo_cobros).
"""
import html
import re
from datetime import date, datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .ocr_comprobante import _date as date_in_receipt_text
from .puesta_al_dia import _Source, chat_file_key, chat_text_key, minute_positions, parse_chat
from .reparto_cliente import AR_TZ

ADMIN_GROUP = "distri_pedido.grupo_administracion"
OWN_SINCE_PARAM = "distri_pedido.chat_propio_desde"
OWN_SINCE_DEFAULT = "2026-10-24"
SOURCE_TAGS = {"manager": "gerente", "admin": "Denis", "own": "ale"}
SOURCE_HELP = {"manager": "Export del chat del gerente",
               "admin": "Export del chat de Denis (administrador): falta en el del gerente",
               "own": "Export del chat propio (ale)"}
ADMIN_NOTE_RE = re.compile(
    r"export del administrador, llegó el (\d{4}-\d{2}-\d{2} \d{2}:\d{2})"
    r"(?:, lo mandó (.+?)(?=, vínculo |, leyenda |\.\s*$|$))?")
DATE_RE = re.compile(r"(\d{1,2})/(\d{1,2})(?:/(\d{2}|\d{4}))?(?:\s+(\d{1,2})[:.](\d{2}))?")
AMOUNT_RE = re.compile(r"\$?\s*\d[\d.,]*")
WEEKDAYS = ["lunes", "martes", "miércoles", "jueves", "viernes", "sábado", "domingo"]
PAGE = 40
SEARCH_PAGE = 30
SNIPPET = 140
# «Pendientes de aprobar»: open cards whose receipt came in this window
PENDING_DAYS = 60
PENDING_STATES = ("draft", "authorized")
PENDING_STATE_LABELS = {"draft": "Pendiente", "authorized": "Autorizado para entregar"}
RECEIPT_DATE_HELP = {
    "read": "Fecha leída del comprobante (la que usa el cruce con Mercado Pago)",
    "text": "Fecha leída en el texto del comprobante",
    "card": "Fecha de la transferencia en la tarjeta de Mercado Pago vinculada"}


def _check_chat_access(env):
    user = env.user
    if not (env.su or user.has_group(ADMIN_GROUP) or user.has_group("base.group_system")):
        raise AccessError("El chat de comprobantes es solo para administración y gerencia.")


def to_local(utc_naive):
    return utc_naive.replace(tzinfo=timezone.utc).astimezone(AR_TZ).replace(tzinfo=None)


def to_utc(local_naive):
    return local_naive.replace(tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)


def floor_minute(value):
    return value.replace(second=0, microsecond=0)


def primary_owner(local_day, own_since):
    """The export whose texts are shown on that Argentine day."""
    return "own" if local_day >= own_since else "manager"


def is_visible(entry, own_since):
    """The source rule for one entry (see the module docstring).
    `entry`: {"owner", "kind" ("text" | "image" | "pdf"), "local" (datetime),
    "from_card" (Denis's file kept on a card)}."""
    primary = primary_owner(entry["local"].date(), own_since)
    if entry.get("from_card"):
        return primary == "manager"
    if entry["kind"] == "text":
        return entry["owner"] == primary
    return True


def _dedup_key(entry):
    if entry["kind"] != "text":
        return "f:" + (entry.get("checksum") or entry["key"])
    return "t:%s|%s" % (floor_minute(entry["local"]).isoformat(),
                        " ".join((entry.get("body") or "").lower().split()))


def sort_key(entry):
    """Chronological order. Inside a minute the order of the lines in the
    export (`pos`, models/puesta_al_dia.py minute_positions) is the best
    signal there is: WhatsApp lists them in its own order but prints only the
    minute. An entry without it (Denis's files on cards, messages imported
    before 19.0.1.46.0 and not reordered yet) goes at the end of its minute,
    and the viewer says so. The id only keeps the order stable."""
    pos = entry.get("pos") or 0
    return (floor_minute(entry["ts"]), pos == 0, pos, entry["ts"], entry["key"])


def select_visible(entries, own_since):
    """The entries to show, without duplicates, in chronological order.
    Applies `is_visible`; then one entry per file content and per text of
    the same minute, preferring the primary export of the day, then an
    imported message over a file kept on a card."""
    best = {}
    for entry in entries:
        if not is_visible(entry, own_since):
            continue
        key = _dedup_key(entry)
        rank = (entry["owner"] != primary_owner(entry["local"].date(), own_since),
                bool(entry.get("from_card")), entry["key"])
        if key not in best or rank < best[key][0]:
            best[key] = (rank, entry)
    return sorted((e for _rank, e in best.values()), key=sort_key)


def parse_amount(text):
    """«$ 100.000», «100000», «1.500,50» -> float, or None."""
    if not AMOUNT_RE.fullmatch(text or ""):
        return None
    raw = text.replace("$", "").replace(" ", "")
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    else:
        raw = raw.replace(".", "")
    try:
        return float(raw)
    except ValueError:
        return None


def receipt_moment(read_utc=None, file_text="", sent_local=None, card_kind="",
                   card_date_utc=None):
    """The receipt's own local date: (datetime, has_time, source) or
    (None, False, ""). See the module docstring for the order."""
    if read_utc:
        moment, source = to_local(read_utc), "read"
    else:
        moment = None
        # without the message as reference only a real date is found: a
        # bare «a las HH:MM» would borrow the message's day
        if file_text and date_in_receipt_text(file_text):
            moment = date_in_receipt_text(file_text, sent_local)
        source = "text"
        if not moment and card_kind == "income" and card_date_utc:
            moment, source = to_local(card_date_utc), "card"
    if not moment:
        return None, False, ""
    return moment, moment.time() != datetime.min.time(), source


def receipt_is_far(moment, has_time, sent_local):
    """More than a day between the receipt and the message that sent it."""
    if not (moment and sent_local):
        return False
    if has_time:
        return abs(moment - sent_local) > timedelta(days=1)
    return abs((moment.date() - sent_local.date()).days) > 1


def snippet(text, needle, width=SNIPPET):
    """`width` characters of `text` around the first `needle` (any case)."""
    text = " ".join((text or "").split())
    at = text.lower().find((needle or "").lower()) if needle else -1
    start = max(0, at - width // 3) if at > width // 2 else 0
    out = text[start:start + width]
    return ("…" if start else "") + out + ("…" if start + width < len(text) else "")


class DistriChatViewer(models.AbstractModel):
    _name = "distri.chat.viewer"
    _description = "Visor del chat de comprobantes"

    # ------------------------------------------------------------ settings

    @api.model
    def _own_since(self):
        raw = self.env["ir.config_parameter"].sudo().get_param(OWN_SINCE_PARAM)
        try:
            return date.fromisoformat((raw or OWN_SINCE_DEFAULT).strip())
        except ValueError:
            return date.fromisoformat(OWN_SINCE_DEFAULT)

    # ------------------------------------------------------------ entries

    def _message_entries(self, messages):
        out = []
        for msg in messages:
            if not msg.timestamp:
                continue
            is_file = bool(msg.attachment_id)
            mime = msg.attachment_id.mimetype or msg.media_mime_type or ""
            out.append({
                "key": "m%d" % msg.id, "message_id": msg.id, "ts": msg.timestamp,
                "local": to_local(msg.timestamp),
                "owner": msg.distri_chat_import_id.distri_export_owner or "",
                "kind": ("pdf" if "pdf" in mime else "image") if is_file else "text",
                "sender": msg.distri_chat_sender or msg.profile_name or "",
                "body": msg.body or "", "pos": msg.distri_chat_minute_pos,
                "attachment_id": msg.attachment_id.id or False,
                "checksum": msg.attachment_id.checksum or "",
                "file_name": msg.media_filename or msg.attachment_id.name or "",
                "amount": msg.distri_amount or 0.0,
                "collection_id": msg.distri_collection_id.id or False,
                "receipt_read": msg.distri_receipt_date if is_file else False,
                "file_text": msg.distri_file_text or "" if is_file else "",
                "payer": msg.distri_payer_name or "",
                "partner": msg.distri_partner_id.name or "",
                "from_card": False})
        return out

    def _card_file_entries(self, lo=None, hi=None):
        """Denis's receipts kept on cards (see the module docstring) whose
        file no imported message has, between `lo` and `hi` (UTC)."""
        self.env.cr.execute("""
            SELECT m.res_id, m.body, a.id, a.name, a.mimetype, a.checksum
              FROM mail_message m
              JOIN message_attachment_rel r ON r.message_id = m.id
              JOIN ir_attachment a ON a.id = r.attachment_id
             WHERE m.model = 'distri.mp.collection'
               AND m.body ILIKE '%%export del administrador%%'
               AND NOT EXISTS (SELECT 1 FROM ir_attachment w
                                WHERE w.res_model = 'distri.whatsapp.message'
                                  AND w.checksum = a.checksum)
             ORDER BY a.id""")
        out = []
        for card_id, body, att_id, name, mimetype, checksum in self.env.cr.fetchall():
            text = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", body or "")).split())
            found = ADMIN_NOTE_RE.search(text)
            if not found:
                continue
            local = datetime.strptime(found.group(1), "%Y-%m-%d %H:%M")
            ts = to_utc(local)
            if (lo and ts < lo) or (hi and ts >= hi):
                continue
            out.append({
                "key": "d%d" % att_id, "message_id": False, "ts": ts, "local": local,
                "owner": "admin", "kind": "pdf" if "pdf" in (mimetype or "") else "image",
                "sender": (found.group(2) or "").strip(), "body": "", "pos": 0,
                "attachment_id": att_id, "checksum": checksum or "", "file_name": name or "",
                "amount": 0.0, "collection_id": card_id, "receipt_read": False,
                "file_text": "", "payer": "", "partner": "", "from_card": True})
        return out

    def _base_domain(self):
        return [("distri_chat_import_id", "!=", False), ("timestamp", "!=", False)]

    def _entries_between(self, lo, hi):
        """Visible entries with lo <= time < hi (UTC; None: open end)."""
        domain = self._base_domain()
        if lo:
            domain.append(("timestamp", ">=", lo))
        if hi:
            domain.append(("timestamp", "<", hi))
        messages = self.env["distri.whatsapp.message"].sudo().search(domain)
        entries = self._message_entries(messages) + self._card_file_entries(lo, hi)
        return select_visible(entries, self._own_since())

    def _page(self, cursor, direction, limit=PAGE):
        """Whole minutes before (`ts < cursor`) or from (`ts >= cursor`) the
        cursor: about `limit` messages. Returns (entries, new cursor, more)."""
        Message = self.env["distri.whatsapp.message"].sudo()
        for _attempt in range(6):
            if direction == "before":
                domain = self._base_domain() + ([("timestamp", "<", cursor)] if cursor else [])
                found = Message.search(domain, order="timestamp desc, id desc", limit=limit)
                more = len(found) == limit
                lo = floor_minute(found[-1].timestamp) if more else None
                entries = self._entries_between(lo, cursor)
                cursor, done = lo, not more
            else:
                domain = self._base_domain() + ([("timestamp", ">=", cursor)] if cursor else [])
                found = Message.search(domain, order="timestamp asc, id asc", limit=limit)
                more = len(found) == limit
                hi = floor_minute(found[-1].timestamp) + timedelta(minutes=1) if more else None
                entries = self._entries_between(cursor, hi)
                cursor, done = hi, not more
            # a page of hidden duplicates only: go on, the person sees nothing new
            if entries or done:
                return entries, cursor, more
        return entries, cursor, more

    # ------------------------------------------------------------ output

    def _add_card_info(self, entries):
        """Linked card (customer, amount) and the receipt's own date of each
        photo or PDF, with one read of the cards for all of them. A file
        whose message does not point at its card finds it by `receipt_id`."""
        files = [e for e in entries if e["kind"] != "text"]
        Card = self.env["distri.mp.collection"].sudo()
        loose = [e["message_id"] for e in files if e["message_id"] and not e["collection_id"]]
        by_receipt = {}
        if loose:
            for card in Card.search_fetch([("receipt_id", "in", loose)], ["receipt_id"],
                                          order="id"):
                by_receipt.setdefault(card.receipt_id.id, card.id)
        for entry in files:
            if not entry["collection_id"] and entry["message_id"] in by_receipt:
                entry["collection_id"] = by_receipt[entry["message_id"]]
        # 19.0.1.57.0: a receipt sent twice has no card of its own; it shows
        # the card of the copy received first
        repeated = self.env["distri.whatsapp.message"].sudo().browse(
            [e["message_id"] for e in files if e["message_id"] and not e["collection_id"]]
        ).filtered(lambda m: m.distri_receipt_state == "duplicate")
        twin_card = {}
        for msg in repeated:
            twins = msg._distri_twins().filtered("distri_collection_id")
            if twins:
                twin_card[msg.id] = twins.sorted(lambda m: (m.timestamp, m.id))[0] \
                    .distri_collection_id.id
        for entry in files:
            entry["repeated"] = entry["message_id"] in {m.id for m in repeated}
            if not entry["collection_id"] and entry["message_id"] in twin_card:
                entry["collection_id"] = twin_card[entry["message_id"]]
        cards = Card.browse({e["collection_id"] for e in files if e["collection_id"]})
        info = {card.id: card for card in cards.exists()}
        for entry in entries:
            card = info.get(entry["collection_id"]) if entry["kind"] != "text" else None
            entry["card_partner"] = card.partner_id.name or "" if card else ""
            entry["card_amount"] = card.amount or 0.0 if card else 0.0
            entry["card_state"] = card.state if card else ""
            if entry["kind"] == "text":
                entry["receipt"] = (None, False, "")
                continue
            entry["receipt"] = receipt_moment(
                entry.get("receipt_read"), entry.get("file_text"), entry["local"],
                card.kind if card else "", card.date if card else None)
        return entries

    def _entries_json(self, entries):
        return [self._entry_json(e) for e in self._add_card_info(entries)]

    def _entry_json(self, entry):
        local = entry["local"]
        owner = entry["owner"]
        moment, has_time, source = entry.get("receipt") or (None, False, "")
        return {
            "key": entry["key"], "message_id": entry["message_id"],
            "ts": entry["ts"].strftime("%Y-%m-%d %H:%M:%S"),
            "day": local.strftime("%Y-%m-%d"),
            "day_label": "%s %s" % (WEEKDAYS[local.weekday()], local.strftime("%d/%m/%Y")),
            "minute": local.strftime("%Y-%m-%d %H:%M"), "time": local.strftime("%H:%M"),
            "sender": entry["sender"], "body": entry["body"], "kind": entry["kind"],
            "source": owner, "source_tag": SOURCE_TAGS.get(owner, "chat"),
            "source_help": SOURCE_HELP.get(owner, "Chat importado sin indicar de quién es"),
            "pos_known": bool(entry["pos"]), "attachment_id": entry["attachment_id"],
            "file_name": entry["file_name"], "amount": entry["amount"],
            "collection_id": entry["collection_id"],
            "card_partner": entry.get("card_partner", ""),
            "card_amount": entry.get("card_amount", 0.0),
            # 19.0.1.57.0: approved cards look done in the chat
            "card_state": entry.get("card_state", ""),
            "repeated": entry.get("repeated", False),
            "receipt_date": moment.strftime("%Y-%m-%d %H:%M") if moment else False,
            "receipt_date_label": (moment.strftime("%d/%m/%Y %H:%M" if has_time else "%d/%m/%Y")
                                   if moment else ""),
            "receipt_date_source": source,
            "receipt_date_help": RECEIPT_DATE_HELP.get(source, ""),
            "receipt_far": receipt_is_far(moment, has_time, local)}

    def _anchor(self, message_id=None, collection_id=None, at=None):
        """(UTC time, entry key or None, note for the person)."""
        if message_id:
            msg = self.env["distri.whatsapp.message"].sudo().browse(int(message_id)).exists()
            if not msg or not msg.timestamp:
                raise UserError("Ese mensaje no existe o no tiene fecha.")
            return msg.timestamp, "m%d" % msg.id, ""
        if collection_id:
            card = self.env["distri.mp.collection"].sudo().browse(int(collection_id)).exists()
            if not card:
                raise UserError("Esa tarjeta no existe.")
            if card.receipt_id.timestamp:
                return card.receipt_id.timestamp, "m%d" % card.receipt_id.id, ""
            files = [e for e in self._card_file_entries() if e["collection_id"] == card.id]
            if files:
                return files[0]["ts"], files[0]["key"], ""
            return card.date, None, ("La tarjeta no tiene comprobante en el chat: se muestra "
                                     "el chat a la hora de la transferencia.")
        if at:
            return datetime.strptime(at[:19], "%Y-%m-%d %H:%M:%S"), None, ""
        return None, None, ""

    @api.model
    def distri_chat_open(self, message_id=None, collection_id=None, at=None):
        """The first page: centered on the anchor (or the latest messages)."""
        _check_chat_access(self.env)
        ts, key, note = self._anchor(message_id, collection_id, at)
        if ts:
            minute = floor_minute(ts)
            before, first, more_before = self._page(minute, "before", PAGE // 2)
            after, last, more_after = self._page(minute, "after", PAGE)
        else:
            before, first, more_before = self._page(None, "before", PAGE * 2)
            after, last, more_after = [], None, False
        entries = before + after
        if key and key not in {e["key"] for e in entries}:
            note = note or ("Ese mensaje no se muestra: es el mismo que otro export ya "
                            "muestra. Se abre el chat a esa hora.")
            key = None
        return {"entries": self._entries_json(entries), "anchor": key,
                "note": note, "first": self._cursor(first), "last": self._cursor(last),
                "more_before": more_before, "more_after": more_after,
                "own_since": self._own_since().strftime("%d/%m/%Y")}

    def _cursor(self, value):
        return value.strftime("%Y-%m-%d %H:%M:%S") if value else False

    @api.model
    def distri_chat_more(self, direction, cursor):
        _check_chat_access(self.env)
        if direction not in ("before", "after"):
            raise UserError("Dirección desconocida.")
        value = datetime.strptime(cursor[:19], "%Y-%m-%d %H:%M:%S") if cursor else None
        entries, new_cursor, more = self._page(value, direction)
        return {"entries": self._entries_json(entries),
                "cursor": self._cursor(new_cursor), "more": more}

    @api.model
    def distri_chat_search(self, query, offset=0, limit=SEARCH_PAGE):
        """A date («27/10/2025», «27/10 08:12») jumps there; an amount or a
        name finds every visible matching message, newest first: `total`
        counts them all, `hits` is the page from `offset`."""
        _check_chat_access(self.env)
        text = " ".join((query or "").split())
        if len(text) < 2:
            raise UserError("Escribí al menos dos letras o números.")
        found = DATE_RE.fullmatch(text)
        if found:
            day, month, year, hour, minute = found.groups()
            if not year:
                year = datetime.now(AR_TZ).year
            else:
                year = int(year) + (2000 if len(year) == 2 else 0)
            try:
                local = datetime(year, int(month), int(day), int(hour or 0), int(minute or 0))
            except ValueError as exc:
                raise UserError("Esa fecha no existe.") from exc
            return {"jump": to_utc(local).strftime("%Y-%m-%d %H:%M:%S"), "hits": [],
                    "total": 0, "offset": 0, "more": False}
        offset, limit = max(0, int(offset or 0)), max(1, min(int(limit or SEARCH_PAGE), 100))
        amount = parse_amount(text)
        domain = self._base_domain()
        if amount is not None:
            domain += ["|", "&", ("distri_amount", ">=", amount - 1),
                       ("distri_amount", "<=", amount + 1), ("body", "ilike", text)]
        else:
            domain += ["|", "|", "|", "|", ("body", "ilike", text),
                       ("profile_name", "ilike", text), ("distri_payer_name", "ilike", text),
                       ("distri_partner_id.name", "ilike", text),
                       ("media_filename", "ilike", text)]
        own_since = self._own_since()
        Message = self.env["distri.whatsapp.message"].sudo()
        # every match, with only what the source rule needs; the page alone
        # is read in full below
        matches = []
        for msg in Message.search_fetch(domain, ["timestamp", "attachment_id",
                                                 "distri_chat_import_id"],
                                        order="timestamp desc, id desc"):
            light = {"owner": msg.distri_chat_import_id.distri_export_owner or "",
                     "kind": "file" if msg.attachment_id else "text",
                     "local": to_local(msg.timestamp)}
            if is_visible(light, own_since):
                matches.append((msg.timestamp, msg.id, None))
        if amount is None:
            needle = text.lower()
            matches += [(e["ts"], 0, e) for e in self._card_file_entries()
                        if is_visible(e, own_since) and (
                            needle in e["sender"].lower() or needle in e["file_name"].lower())]
        matches.sort(key=lambda m: (m[0], m[1]), reverse=True)
        page = matches[offset:offset + limit]
        ids = [msg_id for _ts, msg_id, entry in page if entry is None]
        # a fresh browse: the page's prefetch, not the whole result's
        by_id = {e["message_id"]: e for e in self._message_entries(Message.browse(ids))}
        entries = [entry or by_id[msg_id] for _ts, msg_id, entry in page
                   if entry or msg_id in by_id]
        hits = []
        for entry, item in zip(entries, self._entries_json(entries)):
            item["snippet"] = snippet(entry["body"] or entry["file_name"], text)
            item["payer"] = entry.get("payer", "")
            item["partner"] = entry.get("partner", "")
            hits.append(item)
        return {"jump": False, "hits": hits, "total": len(matches), "offset": offset,
                "more": offset + limit < len(matches)}

    @api.model
    def distri_chat_pending(self):
        """The left column: receipts of the chat whose card is not resolved
        yet (see the module docstring), newest first."""
        _check_chat_access(self.env)
        since = fields.Datetime.now() - timedelta(days=PENDING_DAYS)
        cards = self.env["distri.mp.collection"].sudo().search([
            ("state", "in", PENDING_STATES),
            ("receipt_id.distri_chat_import_id", "!=", False),
            ("receipt_id.timestamp", ">=", since)])
        items = []
        for card in cards.sorted(lambda c: (c.receipt_id.timestamp, c.receipt_id.id), reverse=True):
            msg = card.receipt_id
            local = to_local(msg.timestamp)
            items.append({
                "key": "m%d" % msg.id, "message_id": msg.id, "collection_id": card.id,
                "ts": msg.timestamp.strftime("%Y-%m-%d %H:%M:%S"),
                "day_label": "%s %s" % (WEEKDAYS[local.weekday()], local.strftime("%d/%m/%Y")),
                "time": local.strftime("%H:%M"),
                "sender": msg.distri_chat_sender or msg.profile_name or "",
                "amount": card.amount or msg.distri_amount or 0.0,
                "partner": card.partner_id.name or msg.distri_partner_id.name or "",
                "state": card.state, "state_label": PENDING_STATE_LABELS.get(card.state, ""),
                "attachment_id": msg.attachment_id.id or False,
                "kind": "pdf" if "pdf" in (msg.attachment_id.mimetype or "") else "image",
            })
        return {"items": items, "count": len(items), "days": PENDING_DAYS}

    @api.model
    def distri_chat_card_states(self, collection_ids):
        """{card id: state} of the cards shown in the chat, read again after
        the person acted on one (19.0.1.92.0)."""
        _check_chat_access(self.env)
        ids = [int(i) for i in (collection_ids or []) if i]
        cards = self.env["distri.mp.collection"].sudo().browse(ids).exists()
        return {card.id: card.state for card in cards}

    @api.model
    def distri_chat_create_card(self, message_id):
        """«Crear tarjeta» on a photo or PDF the module did not take as a
        receipt: the same as «Es comprobante» in the receipts list. Returns
        the card id, or False when the photo turned out to be a copy of a
        receipt already received."""
        _check_chat_access(self.env)
        if not self.env.user.has_group("distri_pedido.grupo_administracion"):
            raise AccessError("Solo administración crea tarjetas de cobro.")
        msg = self.env["distri.whatsapp.message"].browse(int(message_id)).exists()
        if not msg or not msg.attachment_id:
            raise UserError("Ese mensaje no tiene foto ni PDF.")
        msg.action_distri_mark_receipt()
        card = msg.distri_collection_id or self.env["distri.mp.collection"].sudo().search(
            [("receipt_id", "=", msg.id)], order="id desc", limit=1)
        return card.id or False

    @api.model
    def distri_chat_action(self, message_id=None, collection_id=None):
        _check_chat_access(self.env)
        params = {}
        if message_id:
            params["message_id"] = message_id
        if collection_id:
            params["collection_id"] = collection_id
        return {"type": "ir.actions.client", "tag": "distri_chat_viewer",
                "name": "Chat de comprobantes", "params": params}


class DistriMpCollectionChat(models.Model):
    _inherit = "distri.mp.collection"

    def action_open_chat(self):
        self.ensure_one()
        return self.env["distri.chat.viewer"].distri_chat_action(collection_id=self.id)

    def action_tab_chat(self):
        """19.0.1.50.0: the chat centered on this card's receipt, in a new
        browser tab (the router hands `collection_id` to the client action)."""
        self.ensure_one()
        _check_chat_access(self.env)
        return {"type": "ir.actions.act_url", "target": "new",
                "url": "/odoo/action-distri_chat_viewer?collection_id=%d" % self.id}


class WhatsappMessageChat(models.Model):
    _inherit = "distri.whatsapp.message"

    def action_open_chat(self):
        self.ensure_one()
        return self.env["distri.chat.viewer"].distri_chat_action(message_id=self.id)

    def action_tab_chat(self):
        """19.0.1.51.0: «Ver en el chat» of the receipt, in a new browser tab."""
        self.ensure_one()
        _check_chat_access(self.env)
        return {"type": "ir.actions.act_url", "target": "new",
                "url": "/odoo/action-distri_chat_viewer?message_id=%d" % self.id}


class WhatsappChatImportOrder(models.Model):
    _inherit = "distri.whatsapp.chat.import"

    def _distri_backfill_minute_pos(self, path):
        """Order inside the minute for the messages imported before
        19.0.1.46.0: reads this import's export again (the same file) and
        sets `distri_chat_minute_pos` on its messages, and on the files it
        shares with another import that have none yet. Writes nothing else.
        Returns {"lines", "updated"}."""
        self.ensure_one()
        _check_chat_access(self.env)
        source = _Source(path=path)
        rows = parse_chat(source.chat_text())
        positions = {}
        repeats = {}
        for (when, sender, body, att), pos in zip(rows, minute_positions(rows)):
            if att:
                raw = source.read(att)
                key = chat_file_key(raw) if raw is not None else None
            elif body and not (body.startswith("<") and body.endswith(">")):
                repeats[(when, sender, body)] = nth = repeats.get((when, sender, body), 0) + 1
                key = chat_text_key(when, sender, body, nth)
            else:
                key = None
            if key:
                positions.setdefault(key, pos)
        Message = self.env["distri.whatsapp.message"].sudo()
        by_pos = {}
        for msg in Message.search([("wa_message_id", "in", list(positions))]):
            if msg.distri_chat_import_id != self and msg.distri_chat_minute_pos:
                continue
            pos = positions[msg.wa_message_id]
            if msg.distri_chat_minute_pos != pos:
                by_pos.setdefault(pos, []).append(msg.id)
        for pos, ids in by_pos.items():
            Message.browse(ids).write({"distri_chat_minute_pos": pos})
        return {"lines": len(rows), "updated": sum(len(ids) for ids in by_pos.values())}

