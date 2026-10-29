#!/usr/bin/env python3
"""Control 2026-10-28: rebuild the electronic-payment migration from the raw
evidence, outside Odoo, and compare it with what the lab holds.

Inputs (CSV exported from the copy distri_lab_control, see README):
  DATA/messages.csv, cards.csv, online_payments_after_sync.csv, partners.csv,
  identities.csv, lab_payments.csv, wholesale_rules.csv

The rule (user, 28/10/2026): only transfers informed WITH A RECEIPT in the
WhatsApp group count. Receipt + note (customer) -> Mercado Pago movement
(operation, date, amount) -> if Online already has the payment, link it;
otherwise it is a confirmed electronic payment of the lab. Movements without
receipt are not assigned nor chased.

Outputs (OUT):
  receipts.csv    one row per receipt: note, customer, movement, Online, lab
                  state, verdict
  decisions.csv   the changes the copy needs versus the live lab, with evidence
  summary.json    counters
"""
import csv
import json
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
from datetime import datetime, timedelta

DATA = os.environ.get("CTL_DATA", "/tmp/claude-1000/-srv/297057ae-d6c4-4f4f-bef1-a067ba52a7c3/scratchpad/data")
OUT = os.environ.get("CTL_OUT", "/srv/docs/distrineuquen-modulo/insumos/control-2026-10-28/work")
MIGRATION_FROM = datetime(2026, 9, 15, 3, 0)   # distri_pedido.migracion_desde, 00:00 AR as UTC
NOTE_WINDOW = timedelta(minutes=5)             # note after the photo
NOTE_BEFORE = timedelta(minutes=3)             # note before a heavier photo
MOVE_BEFORE = timedelta(hours=72)              # movement before the photo was sent
MOVE_AFTER = timedelta(hours=6)                # photo sent before the report's timestamp
ONLINE_BEFORE, ONLINE_AFTER = 10, 3            # the lab's own rule (days)
LAST_REPORT = datetime(2026, 10, 27, 14, 12)   # covers_until of the last MP report
FROZEN_PARTNERS = {315}                         # MELI - BARDA DEL MEDIO
WHOLESALE_NAMES = {"JORGE USTIAS - MAYORISTA", "ANDRES MAYORISTA", "CRISTIAN CORREA - MAYORISTA"}

# ------------------------------------------------------------------ text helpers (same as the lab)
_ZONE_PREFIX_RE = re.compile(r"^\s*[a-z]\s*-\s*")
_PARENS_RE = re.compile(r"\(([^)]*)\)")
_EXPORT_MARK_RE = re.compile(r"<[^>]*>")
_WORD_RE = re.compile(r"[a-z0-9]+")
NAME_STOPWORDS = {"de", "del", "la", "las", "el", "los", "y", "e", "a", "al", "en", "con", "por", "para"}
NOTE_NOISE = NAME_STOPWORDS | {
    "dia", "ok", "llego", "pago", "pagos", "pagado", "pagaron", "transferencia", "transf",
    "comprobante", "gracias", "cliente", "es", "se", "hola", "buenas", "buen", "dias",
    "tardes", "noches", "listo", "edito", "este", "mensaje", "mp", "transfirio", "hoy", "ayer",
    "parte", "resto", "total", "saldo", "factura", "fact", "pesos", "mil", "recibi", "efectivo",
    "recibo", "ambas", "ambos", "dos"}
BOTH_RE = re.compile(r"\b(ambas|ambos|las dos|los dos|las 2|los 2|x\s?2)\b")
CASH_RE = re.compile(r"\befectivo\b")
NOTE_MAX_WORDS = 6
PREFIX_MIN_LEN = 4


def fold(text):
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def name_tokens(name):
    text = _ZONE_PREFIX_RE.sub("", fold(name))
    place = set()
    for inside in _PARENS_RE.findall(text):
        place |= set(_WORD_RE.findall(inside))
    core = set(_WORD_RE.findall(_PARENS_RE.sub(" ", text))) - NAME_STOPWORDS
    return core, place - core


def note_tokens(line):
    text = _ZONE_PREFIX_RE.sub("", fold(_EXPORT_MARK_RE.sub(" ", line or "")))
    words = {w for w in _WORD_RE.findall(text) if w not in NOTE_NOISE and (len(w) >= 2 or w.isdigit())}
    if not words or len(words) > NOTE_MAX_WORDS:
        return set()
    if not any(len(w) >= 3 and not w.isdigit() for w in words):
        return set()
    return words


def _edits(a, b):
    """Levenshtein distance, small strings only."""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > 2:
        return 3
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _word_hits(word, words, fuzzy=False):
    if word in words or (len(word) >= PREFIX_MIN_LEN and any(w.startswith(word) for w in words)):
        return True
    if fuzzy and len(word) >= 5:
        tol = 2 if len(word) >= 9 else 1
        return any(len(w) >= 5 and _edits(word, w) <= tol for w in words)
    return False


class NameIndex:
    def __init__(self, rows):
        self.core, self.place, self.by_word = {}, {}, {}
        for pid, name in rows:
            core, place = name_tokens(name)
            if not core:
                continue
            self.core[pid], self.place[pid] = core, place
            for w in core | place:
                self.by_word.setdefault(w, set()).add(pid)
            for w in core | place:
                for k in range(PREFIX_MIN_LEN, len(w)):
                    self.by_word.setdefault(w[:k], set()).add(pid)

    def match(self, words, fuzzy=False):
        if not fuzzy:
            cands = None
            for w in words:
                ids = self.by_word.get(w, set())
                cands = ids if cands is None else cands & ids
                if not cands:
                    return set()
        else:
            cands = set(self.core)
        out = set()
        for pid in cands or ():
            allw = self.core[pid] | self.place[pid]
            if all(_word_hits(w, allw, fuzzy) for w in words) and any(_word_hits(w, self.core[pid], fuzzy) for w in words):
                out.add(pid)
        return out

    def agrees(self, text, pid):
        """The customer `pid` is consistent with the note: every line that
        looks like a name has at least one significant word in that name."""
        if pid not in self.core:
            return False
        allw = self.core[pid] | self.place[pid]
        lines = [note_tokens(l) for l in (text or "").splitlines()]
        lines = [w for w in lines if w]
        if not lines:
            return False
        return any(any(_word_hits(w, allw, True) and len(w) >= 4 for w in words) for words in lines)

    def suggest(self, text, fuzzy=False):
        found = {}
        for line in (text or "").splitlines():
            words = note_tokens(line)
            ids = self.match(words, fuzzy) if words else set()
            if len(ids) == 1:
                found.setdefault(ids.pop(), line.strip())
            elif len(ids) > 1:
                return None, "ambiguous:%d" % len(ids), ids
        if len(found) != 1:
            return None, "none" if not found else "multi", set(found)
        return next(iter(found.items())) + (set(),)


# ------------------------------------------------------------------ load
def read(name):
    with open(os.path.join(DATA, name), newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def ts(value):
    if not value:
        return None
    return datetime.fromisoformat(value[:19])


def num(value):
    try:
        return float(value) if value not in (None, "") else None
    except ValueError:
        return None


def digits(value):
    return re.sub(r"\D", "", value or "")


partners = {int(p["id"]): p for p in read("partners.csv")}
pname = lambda pid: partners.get(pid, {}).get("name", "?") if pid else ""
for p in partners.values():
    p["frozen"] = p["name"].startswith("Z -") or int(p["id"]) in FROZEN_PARTNERS
    p["wholesale"] = p["distri_es_mayorista"] == "t" or p["name"] in WHOLESALE_NAMES
index = NameIndex([(int(p["id"]), p["name"]) for p in partners.values()
                   if p["active"] == "t" and not p["parent_id"] and p["distri_generic_partner"] != "t"])

identities = defaultdict(set)
for row in read("identities.csv"):
    if row["kind"] == "cuit":
        identities[digits(row["value"])].add(int(row["partner_id"]))
wholesale_cuits = {digits(r["cuit"]) for r in read("wholesale_rules.csv") if r.get("active", "t") == "t"}

messages = read("messages.csv")
for m in messages:
    m["id"] = int(m["id"])
    m["t"] = ts(m["timestamp"])
    m["sender"] = m["distri_chat_sender"] or m["profile_name"] or m["wa_id"]
    m["is_receipt"] = m["distri_is_receipt"] == "t" and m["message_type"] in ("image", "document")
    # the amount read on the photo/PDF beats a caption («+ efectivo $100.000»)
    m["amount"] = num(m["distri_read_amount"]) or num(m["distri_amount"])
    m["op"] = (m["distri_operation"] or "").strip()
by_msg = {m["id"]: m for m in messages}
# true order inside a minute, from the raw exports (sequence.py)
SEQ = {}
try:
    for r in read("sequence.csv"):
        SEQ[int(r["message_id"])] = int(r["seq"])
except OSError:
    pass
for m in messages:
    m["seq"] = SEQ.get(m["id"], 10 ** 9 + m["id"])
messages.sort(key=lambda m: (m["t"] or datetime.min, m["seq"]))

cards = [c for c in read("cards.csv")]
for c in cards:
    c["id"] = int(c["id"])
    c["t"] = ts(c["date"])
    c["amount"] = num(c["amount"]) or 0.0
    c["partner"] = int(c["partner_id"]) if c["partner_id"] else None
    c["receipt"] = int(c["receipt_id"]) if c["receipt_id"] else None
    c["online"] = int(c["online_payment_id"]) if c["online_payment_id"] else None
    c["payment"] = int(c["payment_id"]) if c["payment_id"] else None
incomes = [c for c in cards if c["kind"] == "income"]
by_card = {c["id"]: c for c in cards}
by_source = defaultdict(list)
for c in incomes:
    if c["source_id"]:
        by_source[digits(c["source_id"])].append(c)
    if c["bank_transfer_id"]:
        by_source[digits(c["bank_transfer_id"])].append(c)
by_amount = defaultdict(list)
for c in incomes:
    by_amount[round(c["amount"])].append(c)

online = read("online_payments_after_sync.csv")
for o in online:
    o["id"] = int(o["id"])
    o["d"] = ts(o["date"]).date() if o["date"] else None
    o["amount"] = num(o["amount"]) or 0.0
    o["partner"] = int(o["partner_id"]) if o["partner_id"] else None
    o["card"] = int(o["collection_id"]) if o["collection_id"] else None
    o["bank"] = o["journal_name"] == "Bank"
    o["ok"] = o["state"] in ("paid", "in_process", "posted", "draft")
by_online = {o["id"]: o for o in online}
online_by_amount = defaultdict(list)
for o in online:
    if o["ok"] and o["amount"] > 0:
        online_by_amount[round(o["amount"])].append(o)
online_create = {}
try:
    for r in json.load(open(os.path.join(DATA, "online_payments_uid.json"))):
        online_create[r["id"]] = r["create_date"]
except OSError:
    pass

lab_payments = {int(p["id"]): p for p in read("lab_payments.csv")}

SELLER_BY_SENDER = {
    "Distri Neuquén Neuquén": "GONZA", "Distri NEUQUÉN": "GONZA", "Italo Javi Dios Es Fiel 😎": "ITALO",
    "Italo": "ITALO", "Simon Nvo": "SIMON", "Simón Fernández Kiosco Fonocomahue": "SIMON",
    "Simon Distrineuquen - Business": "SIMON", "Jorgito jelvez": "JORGE", "Jorge GELVEZ": "JORGE",
    "Denis Barros": "DENIS", "Ale Castañeda": "ALE"}


def seller_of_memo(memo):
    m = fold(memo or "")
    for key in ("gonza", "italo", "simon", "jorge", "denis"):
        if key in m:
            return key.upper()
    return ""


# ------------------------------------------------------------------ 1. receipt <-> note
def pair_notes():
    """Returns {receipt id: (note message id, how)}. how: caption | after |
    before | both. Rules (user, 28/10): the note usually follows the photo;
    a heavier photo may arrive after its note; two photos then two notes
    pair in order; «ambas/ambos» names both pending photos."""
    out = {}
    by_sender = defaultdict(list)
    for m in messages:
        if not m["t"]:
            continue
        if m["is_receipt"]:
            by_sender[m["sender"]].append(m)
        elif m["message_type"] == "text" and note_tokens(m["body"] or ""):
            by_sender[m["sender"]].append(m)
    for sender, items in by_sender.items():
        items.sort(key=lambda m: (m["t"], m["seq"]))
        pending = []          # receipts without a firm note, oldest first
        tentative = {}        # receipt id -> note id (a note just before it)
        last_note = None      # the last unpaired note (for «before»)
        recent = []           # receipts of the last minutes (duplicate photos)
        used = set()
        for m in items:
            # drop what fell out of the window
            pending = [r for r in pending if m["t"] - r["t"] <= NOTE_WINDOW]
            if m["is_receipt"]:
                if (m["body"] or "").strip() and note_tokens(m["body"]):
                    out[m["id"]] = (m["id"], "caption")
                    continue
                # the same operation number as a photo just sent: a second photo
                # of the same receipt, it takes no note
                op = digits(m["op"])
                if op and len(op) >= 8 and any(digits(r["op"]) == op for r in recent if r["id"] != m["id"]):
                    out[m["id"]] = (None, "dup")
                    continue
                recent.append(m)
                recent = [r for r in recent if m["t"] - r["t"] <= NOTE_WINDOW]
                pending.append(m)
                if last_note and last_note["id"] not in used and timedelta(0) <= m["t"] - last_note["t"] <= NOTE_BEFORE \
                        and not any(r["id"] != m["id"] and r["id"] not in tentative for r in pending):
                    tentative[m["id"]] = last_note["id"]
                    last_note = None
                continue
            # a text: pair it with the oldest pending receipt without a tentative note
            free = [r for r in pending if r["id"] not in tentative]
            if BOTH_RE.search(fold(m["body"])) and len(pending) >= 2:
                for r in pending[-2:] if not free or len(free) < 2 else free[:2]:
                    out[r["id"]] = (m["id"], "both")
                    tentative.pop(r["id"], None)
                used.add(m["id"])
                pending = [r for r in pending if r["id"] not in out]
                last_note = None
                continue
            target = free[0] if free else (pending[0] if pending else None)
            if target:
                if target["id"] in tentative:
                    # «after» beats a tentative «before»: that earlier note is released
                    tentative.pop(target["id"])
                out[target["id"]] = (m["id"], "after")
                used.add(m["id"])
                pending = [r for r in pending if r["id"] != target["id"]]
            last_note = m
            # receipts that keep a tentative note and are no longer pending get it
            for r in list(pending):
                pass
        # tentative notes that were never displaced
        for rid, nid in tentative.items():
            if rid not in out and nid not in used:
                out[rid] = (nid, "before")
                used.add(nid)
        # a note just before a receipt that still has none
        notes = [m for m in items if not m["is_receipt"] and m["id"] not in used]
        for r in items:
            if not r["is_receipt"] or r["id"] in out:
                continue
            best = None
            for n in notes:
                if n["id"] in used:
                    continue
                if timedelta(0) <= (r["t"] - n["t"]) <= NOTE_BEFORE:
                    if best is None or n["t"] > best["t"]:
                        best = n
            if best:
                out[r["id"]] = (best["id"], "before")
                used.add(best["id"])
    return out


# ------------------------------------------------------------------ 2. customer of a receipt
def customer_of(m, note_text, lab_pid=None, online_pid=None):
    """(partner id or None, source, detail). Sources, strongest first: note
    (exact, one customer), note~ (one customer, a letter off), note_lab (the
    note is ambiguous or odd but the customer the lab/Online chose fits it),
    alias (learned from Online-confirmed receipts), photo_cuit."""
    if note_text:
        pid, line, _ = index.suggest(note_text)
        if pid:
            return pid, "note", line
        pid2, line2, cands = index.suggest(note_text, fuzzy=True)
        if pid2:
            return pid2, "note~", line2
        for cand, tag in ((online_pid, "note_online"), (lab_pid, "note_lab")):
            if cand and (cand in cands or index.agrees(note_text, cand)):
                return cand, tag, note_text
        alias = ALIASES.get(fold(note_text).strip())
        if alias:
            return alias, "alias", note_text
        return None, "note_" + (line2 if line2.startswith("ambiguous") else line), note_text
    cuit = digits(m["distri_payer_cuit"])
    if cuit and len(identities.get(cuit, ())) == 1:
        return next(iter(identities[cuit])), "photo_cuit", cuit
    return None, "none", ""


# alias dictionary: note text -> Online partner, from receipts whose card is
# tied to an Online payment of the same partner (majority vote, >= 2 cases)
def build_aliases(notes):
    votes = defaultdict(Counter)
    for c in incomes:
        if not (c["receipt"] and c["online"]):
            continue
        o = by_online.get(c["online"])
        if not o or not o["partner"]:
            continue
        nid = notes.get(c["receipt"])
        if not nid:
            continue
        text = fold(by_msg[nid[0]]["body"] or "").strip()
        if text and note_tokens(text):
            votes[text][o["partner"]] += 1
    out = {}
    for text, counter in votes.items():
        pid, n = counter.most_common(1)[0]
        if n >= 2 and n / sum(counter.values()) >= 0.8:
            out[text] = pid
    return out


# ------------------------------------------------------------------ 3. receipt -> movement
def near(a, b, tol=1):
    return a is not None and b is not None and abs(a - b) <= tol


def movement_of(m, lab_card):
    """(card or None, how). how: op | op~ | amount_time | lab | none | ambiguous:N"""
    op = digits(m["op"])
    if op and len(op) >= 8:
        cands = [c for c in by_source.get(op, []) if near(c["amount"], m["amount"]) or m["amount"] is None]
        if len(cands) == 1:
            return cands[0], "op"
        if len(cands) > 1:
            cands.sort(key=lambda c: abs((c["t"] - m["t"]).total_seconds()))
            return cands[0], "op_multi"
        # one OCR digit wrong
        for c in by_amount.get(round(m["amount"] or -1), []):
            for key in (digits(c["source_id"]), digits(c["bank_transfer_id"])):
                if key and len(key) == len(op) and sum(a != b for a, b in zip(key, op)) == 1:
                    return c, "op~"
    if m["amount"]:
        cands = [c for c in by_amount.get(round(m["amount"]), [])
                 if c["t"] and -MOVE_AFTER <= (m["t"] - c["t"]) <= MOVE_BEFORE]
        if len(cands) == 1:
            return cands[0], "amount_time"
        if len(cands) > 1:
            # the lab's own link breaks the tie when it is one of them
            if lab_card and any(c["id"] == lab_card for c in cands):
                return by_card[lab_card], "amount_time_lab"
            # the closest movement before the photo
            before = [c for c in cands if c["t"] <= m["t"]]
            pool = before or cands
            pool.sort(key=lambda c: abs((m["t"] - c["t"]).total_seconds()))
            if len(pool) >= 2 and abs((pool[0]["t"] - pool[1]["t"]).total_seconds()) < 3600 * 6:
                return pool[0], "ambiguous:%d" % len(cands)
            return pool[0], "amount_time_closest"
    if lab_card:
        return by_card[lab_card], "lab"
    return None, "none"


# ------------------------------------------------------------------ 4. Online payment of a receipt
def online_candidates(card, partner, amount):
    day = card["t"].date() if card and card["t"] else None
    out = []
    for o in online_by_amount.get(round(amount), []):
        if not o["bank"] or not near(o["amount"], amount, 1.0):
            continue
        if day and o["d"] and not (o["d"] - timedelta(days=ONLINE_BEFORE) <= day <= o["d"] + timedelta(days=ONLINE_AFTER)):
            continue
        out.append(o)
    same = [o for o in out if partner and o["partner"] == partner]
    return same, out


# ------------------------------------------------------------------ main
def main():
    global ALIASES
    os.makedirs(OUT, exist_ok=True)
    notes = pair_notes()
    ALIASES = {}
    ALIASES = build_aliases(notes)
    receipts = [m for m in messages if m["is_receipt"]]
    # which card the lab tied to each receipt
    lab_card_of = {}
    for c in incomes:
        if c["receipt"]:
            lab_card_of.setdefault(c["receipt"], []).append(c)
    consumed_online = {}      # online id -> receipt id (by this control)
    consumed_card = {}        # card id -> receipt id
    rows = []
    for m in sorted(receipts, key=lambda x: (x["t"], x["seq"])):
        note = notes.get(m["id"])
        if note and note[0] is None:
            note = None
        note_text = by_msg[note[0]]["body"] if note else ""
        lab_cards = lab_card_of.get(m["id"], [])
        _lab_pid = lab_cards[0]["partner"] if lab_cards else (int(m["distri_partner_id"]) if m["distri_partner_id"] else None)
        _online_pid = None
        if lab_cards and lab_cards[0]["online"] and by_online.get(lab_cards[0]["online"]):
            _online_pid = by_online[lab_cards[0]["online"]]["partner"]
        pid, src, detail = customer_of(m, note_text, _lab_pid, _online_pid)
        if (pid is None or src == "photo_cuit") and note_text and CASH_RE.search(fold(note_text)):
            src = "cash_notice"
        lab_card = lab_cards[0]["id"] if lab_cards else (int(m["distri_collection_id"]) if m["distri_collection_id"] and int(m["distri_collection_id"]) in by_card and by_card[int(m["distri_collection_id"])]["kind"] == "income" else None)
        card, how = movement_of(m, lab_card)
        row = {
            "receipt_id": m["id"], "sent_at": m["timestamp"], "sender": m["sender"],
            "seller": SELLER_BY_SENDER.get(m["sender"], ""), "note_id": note[0] if note else "",
            "note_how": note[1] if note else "", "note": (note_text or "").replace("\n", " / ")[:120],
            "customer_id": pid or "", "customer": pname(pid), "customer_source": src,
            "lab_customer_id": m["distri_partner_id"], "lab_customer": pname(int(m["distri_partner_id"])) if m["distri_partner_id"] else "",
            "ocr_amount": m["amount"] or "", "ocr_op": m["op"], "ocr_bank": m["distri_bank"],
            "ocr_payer": m["distri_payer_name"], "ocr_payer_cuit": m["distri_payer_cuit"],
            "card_id": card["id"] if card else "", "card_how": how,
            "customer_id_final": "",
            "card_date": card["date"] if card else "", "card_amount": card["amount"] if card else "",
            "card_payer": card["payer_name"] if card else "", "card_cuit": card["payer_cuit"] if card else "",
            "lab_card_id": lab_card or "", "lab_card_state": by_card[lab_card]["state"] if lab_card else "",
            "lab_card_customer": pname(by_card[lab_card]["partner"]) if lab_card else "",
            "lab_card_online": by_online[by_card[lab_card]["online"]]["name"] if lab_card and by_card[lab_card]["online"] in by_online else "",
            "lab_card_payment": lab_payments.get(by_card[lab_card]["payment"], {}).get("name", "") if lab_card else "",
            "online_match": "", "online_match_partner": "", "online_other": "", "verdict": "", "flag": "",
        }
        in_period = m["t"] >= MIGRATION_FROM
        row["in_period"] = "yes" if in_period else "no"
        row["customer_id"], row["customer"], row["customer_source"] = pid or "", pname(pid), src
        frozen = pid and partners[pid]["frozen"]
        if pid and partners[pid]["wholesale"]:
            row["flag"] = (row["flag"] + " wholesale_customer").strip()
        dup_of = None
        if card:
            if card["id"] in consumed_card and consumed_card[card["id"]] != m["id"]:
                first = consumed_card[card["id"]]
                first_row = next(x for x in rows if x["receipt_id"] == first)
                if how.startswith(("amount_time_closest", "ambiguous")):
                    # a weak match on a movement another receipt already explains
                    row["flag"] = "weak_match_movement_taken_by_%s" % first
                    card, how = None, "none"
                else:
                    dup_of = first
                    row["flag"] = "same_movement_as_receipt_%s" % first
                    if not pid or str(first_row["customer_id"]) == str(pid):
                        pid = pid or (int(first_row["customer_id"]) if first_row["customer_id"] else None)
            else:
                consumed_card[card["id"]] = m["id"]
        row["card_id"], row["card_how"] = (card["id"] if card else ""), how
        if dup_of:
            row["verdict"] = "duplicate_photo" if str(next(x for x in rows if x["receipt_id"] == dup_of)["customer_id"]) == str(pid or "") else "conflict_same_movement"
            row["compare"] = "first_receipt:%s" % dup_of
            rows.append(row)
            continue
        if card:
            same, anyp = online_candidates(card, pid, card["amount"])
            same = [o for o in same if consumed_online.get(o["id"], m["id"]) == m["id"]]
            if same:
                # several: Online loading order follows the receipts' order
                same.sort(key=lambda o: (online_create.get(o["id"], ""), o["name"]))
                o = same[0]
                consumed_online[o["id"]] = m["id"]
                row["online_match"] = o["name"]
                row["online_match_partner"] = pname(o["partner"])
                row["online_match_date"] = o["date"]
                row["online_match_memo"] = o["memo"]
                row["online_match_card"] = o["card"] or ""
            else:
                others = [o for o in anyp if consumed_online.get(o["id"], m["id"]) == m["id"]]
                row["online_other"] = "; ".join("%s %s %s %s" % (o["name"], o["date"], pname(o["partner"]), o["memo"]) for o in others[:4])
            # verdict
            if card["state"] == "apartado":
                row["verdict"] = "wholesale_apart"
            elif frozen:
                row["verdict"] = "frozen"
            elif card["reversal"]:
                row["verdict"] = "reversal"
            elif not pid:
                row["verdict"] = "no_customer"
            elif row["online_match"]:
                row["verdict"] = "in_online"
            else:
                row["verdict"] = "confirm_lab"
            # compare with the lab
            lab_state = card["state"]
            lab_partner = card["partner"]
            lab_online = by_online.get(card["online"]) if card["online"] else None
            cmp = []
            if row["verdict"] == "in_online":
                if lab_state == "confirmed" and card["payment"]:
                    cmp.append("DOUBLE:lab_pmp_%s_and_online_%s" % (lab_payments.get(card["payment"], {}).get("name"), row["online_match"]))
                elif lab_online and lab_online["name"] != row["online_match"]:
                    cmp.append("OTHER_ONLINE:lab_%s_vs_%s" % (lab_online["name"], row["online_match"]))
                elif not lab_online and lab_state in ("draft",):
                    cmp.append("MISSING_LINK:online_%s_is_on_card_%s" % (row["online_match"], row.get("online_match_card")))
                elif not lab_online and lab_state == "ignored":
                    cmp.append("IGNORED_NO_LINK:%s" % card["ignore_reason"])
                if lab_partner and pid and lab_partner != pid:
                    cmp.append("CUSTOMER:lab_%s_vs_%s" % (pname(lab_partner), pname(pid)))
            elif row["verdict"] == "confirm_lab":
                if lab_state == "confirmed" and lab_partner == pid:
                    cmp.append("OK")
                elif lab_state == "confirmed":
                    cmp.append("WRONG_CUSTOMER:lab_%s_vs_%s" % (pname(lab_partner), pname(pid)))
                elif lab_state == "draft":
                    cmp.append("NOT_CONFIRMED:%s" % card["migration_box"])
                elif lab_state == "ignored" and card["ignore_reason"] == "online" and lab_online:
                    cmp.append("LINKED_BY_AMOUNT_TO:%s_%s_%s" % (lab_online["name"], pname(lab_online["partner"]), lab_online["link_kind"]))
                elif lab_state == "ignored":
                    cmp.append("IGNORED:%s:%s" % (card["ignore_reason"], (card["ignore_note"] or "")[:60]))
            elif row["verdict"] == "no_customer":
                cmp.append("lab:%s:%s" % (lab_state, pname(lab_partner)))
            row["compare"] = " ".join(cmp)
        else:
            if m["t"] > LAST_REPORT - timedelta(hours=6):
                row["verdict"] = "waiting_report"
            elif m["distri_receipt_state"] in ("outgoing",):
                row["verdict"] = "own_payment"
            elif m["distri_receipt_state"] in ("duplicate",):
                row["verdict"] = "duplicate_photo"
            else:
                row["verdict"] = "no_movement"
            row["compare"] = "lab_state:%s %s" % (m["distri_receipt_state"], (m["distri_receipt_note"] or "")[:60])
        rows.append(row)

    fields = list(rows[0].keys())
    for r in rows:
        for k in r:
            if k not in fields:
                fields.append(k)
    with open(os.path.join(OUT, "receipts.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(rows)

    # ---- pattern 1: Online linked by amount to a card without receipt while a
    # card with receipt of the same customer/amount is loose
    p1 = []
    for o in online:
        if not (o["ok"] and o["bank"] and o["card"] and o["link_kind"] == "amount"):
            continue
        c = by_card.get(o["card"])
        if not c or c["receipt"]:
            continue
        taker = consumed_online.get(o["id"])
        if taker:
            r = next(x for x in rows if x["receipt_id"] == taker)
            p1.append({"online": o["name"], "online_date": o["date"], "amount": o["amount"], "online_partner": pname(o["partner"]),
                       "memo": o["memo"], "linked_card": c["id"], "linked_card_date": c["date"], "linked_card_payer": c["payer_name"],
                       "receipt_card": r["card_id"], "receipt_id": taker, "receipt_customer": r["customer"],
                       "receipt_card_state": r["lab_card_state"], "receipt_card_payment": r["lab_card_payment"]})
    with open(os.path.join(OUT, "pattern1_amount_links.csv"), "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(p1[0].keys()) if p1 else ["none"])
        w.writeheader()
        w.writerows(p1)

    summary = {
        "receipts": len(rows),
        "with_note": sum(1 for r in rows if r["note_id"]),
        "note_how": dict(Counter(r["note_how"] for r in rows)),
        "customer_source": dict(Counter(r["customer_source"] for r in rows)),
        "card_how": dict(Counter(r["card_how"] for r in rows)),
        "verdict": dict(Counter(r["verdict"] for r in rows)),
        "verdict_in_period": dict(Counter(r["verdict"] for r in rows if r["in_period"] == "yes")),
        "compare_in_period": dict(Counter(r["compare"].split(":")[0] for r in rows if r["in_period"] == "yes" and r["compare"])),
        "compare_all": dict(Counter(r["compare"].split(":")[0] for r in rows if r["compare"])),
        "customer_vs_lab": dict(Counter(
            "same" if str(r["customer_id"]) == str(r["lab_customer_id"]) else ("lab_only" if not r["customer_id"] else ("mine_only" if not r["lab_customer_id"] else "differ"))
            for r in rows)),
        "pattern1": len(p1),
        "aliases": len(ALIASES),
    }
    with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as fh:
        json.dump(summary, fh, ensure_ascii=False, indent=1)
    print(json.dumps(summary, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    main()
