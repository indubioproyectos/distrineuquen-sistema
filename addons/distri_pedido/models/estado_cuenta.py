"""Account statement of a customer: the whole history, one list.

One statement merges three sources, sorted by date with a running balance,
from the first movement to today:

- the history imported from Odoo Online (distri.cuenta.movimiento);
- the documents in this Odoo: posted receivable lines (invoices, credit
  notes, payments);
- the returns the driver reported (route visit or depot delivery) that are
  not settled yet: «Devolución informada», marked «a controlar». They lower
  the balance right away; at settlement the posted credit note replaces
  them (distri.route.return._distri_is_provisional).

A document present both in the history and in Odoo counts once and the
live one from Odoo wins (models/cuenta.py, clave_documento: number plus
amount). res.partner._distri_account_balance applies the same rule and
always equals the closing balance of the full statement.

The cards «Candidato · falta el comprobante» of the customer (19.0.1.45.0)
show apart for staff only, as «Pago a confirmar · falta el comprobante»:
never subtracted from the balance, with the balance if they were approved.

Red rows («debe $ X», 19.0.1.99.0, priority passes since 19.0.1.123.0):
every credit of the customer (payments, credit notes, refunds, reported
returns) is applied to the debit rows in passes, each over what is still
unallocated (mark_owed): first the reconciliations of this Odoo, then the
Online history invoice a /cobro payment is linked to by id (19.0.1.125.0),
then the invoice a memo names, then an exact amount nearest in date, then
2 or 3 payments adding up exactly to an invoice within 7 days after it
(«pago dividido», 19.0.1.126.3), and last oldest debit first. What stays
uncovered is owed, so the red total always equals the balance and a
customer up to date or in credit has nothing red. A paid invoice no longer
stays red because an older one took its payment. The review marks
(distri_owed*) no longer decide the color; their note stays visible as
`review_note`. Before every pass (pass 0, 19.0.1.129.9), the customer's
reviewed allocations («Imputación revisada», distri.owed.allocation, by
line_key) are applied; staff see their reason as «Revisado: …» in
`review_note`.

Staff see the invoice number and the remito. The customer sees only the
remito (models/remito.py): `customer_type` and `customer_ref` never carry
an invoice number.

Public link («Por WhatsApp» in /cuentas, 19.0.1.112.0): share_token signs
(customer, expiry, date range) with the database secret; nothing is stored,
so it needs no new column. /estado-de-cuenta/<token> (controllers/main.py)
shows that customer's account the way he sees it in «Mi cuenta» (remitos,
never an invoice number), read-only, until the expiry
(PARAM_LINK_DAYS, 7 days by default) and only while his account is shown
to him (models/cuenta_visible.py).
"""
import base64
import binascii
import re
from datetime import date, datetime, time, timedelta, timezone
from itertools import combinations

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools import consteq, html_escape
from odoo.tools.misc import hmac as odoo_hmac

from .ajuste_cuenta import AJC_JOURNAL_CODE
from .cuenta import clave_documento
from .imputacion_revisada import REVIEWED_PREFIX
from .remito import es_numero_de_factura
from .reparto_cliente import AR_TZ, today_ar

VE_SALDOS = (
    "distri_pedido.grupo_vendedor,"
    "distri_pedido.grupo_administracion,"
    "distri_pedido.grupo_gerencia"
)

# Sort order inside a day: the Online history first, then this Odoo, then
# the provisional returns (the newest information).
SOURCE_ONLINE = "online"
SOURCE_ODOO = "odoo"
SOURCE_RETURN = "return"
_SOURCE_ORDER = {SOURCE_ONLINE: 0, SOURCE_ODOO: 1, SOURCE_RETURN: 2}
SOURCE_LABELS = {
    SOURCE_ONLINE: "Sistema anterior",
    SOURCE_ODOO: "DistriNeuquén",
    SOURCE_RETURN: "Reparto",
}

PROVISIONAL_LABEL = "Devolución informada"
PROVISIONAL_NOTE = "a controlar"
CANDIDATE_ROW_LABEL = "Pago a confirmar · falta el comprobante"
WHATIF_LABEL = "Saldo si se aprueban los candidatos"


OWED_TOLERANCE = 0.005

# the public statement link (see the module docstring)
PARAM_LINK_DAYS = "distri_pedido.estado_link_dias"
DEFAULT_LINK_DAYS = 7
LINK_SCOPE = "distri_pedido.estado_link"
LINK_VERSION = "e1"
LINK_SIGNATURE_BYTES = 24


def mark_owed_fifo(rows):
    """Set row["owed"] on statement rows, FIFO only (the statement uses
    mark_owed; «Deudores» still dates its «Deuda más vieja» with this one).

    The credit is the sum of the rows whose net (debit - credit) is negative,
    whatever their date: a payment made before an invoice still covers it.
    It is applied to the rows with a positive net in the given order (oldest
    first); whatever a row keeps uncovered is its «owed». Every other row gets
    0. Invariant: sum(owed) == max(balance, 0), within cents.

    Returns (count, total) of the owed rows.
    """
    credit = 0.0
    for row in rows:
        net = (row.get("debit") or 0.0) - (row.get("credit") or 0.0)
        if net < 0:
            credit -= net
    count, total = 0, 0.0
    for row in rows:
        net = round((row.get("debit") or 0.0) - (row.get("credit") or 0.0), 2)
        if net <= 0:
            row["owed"] = 0.0
            continue
        covered = min(credit, net)
        credit -= covered
        owed = round(net - covered, 2)
        if owed > OWED_TOLERANCE:
            row["owed"] = owed
            count += 1
            total += owed
        else:
            row["owed"] = 0.0
    return count, round(total, 2)


# Invoice numbers a credit's memo may name (mark_owed, pass 2): the full
# number, or a bare 5-digit tail that is not part of a longer token (S00241,
# an amount like 12345,50, a date or a full number).
_MEMO_FULL_NUMBER = re.compile(r"(?<![\w/])((?:\d{4}|VSR|AJC)/\d{4}/\d{5})(?![\d/])")
_MEMO_TAIL = re.compile(r"(?<![\w/.,$])(\d{5})(?![\w/]|[.,]\d)")
_INVOICE_NUMBER = re.compile(r"^\d{4}/\d{4}/(\d{5})$")


def _memo_refs(text):
    """The invoice references in a memo, in order: ("full", number) or
    ("tail", 5 digits); a tail already named by a full number is skipped."""
    refs = [("full", m.group(1)) for m in _MEMO_FULL_NUMBER.finditer(text)]
    seen = {ref[-5:] for _kind, ref in refs}
    for m in _MEMO_TAIL.finditer(text):
        if m.group(1) not in seen:
            seen.add(m.group(1))
            refs.append(("tail", m.group(1)))
    return refs


def _nearest_debit(candidates, day):
    """The candidate dated on or before `day`, nearest in date; if none, the
    nearest after. Ties: the first in row order (the oldest)."""
    def when(item):
        return item["row"].get("date") or date.min
    before = [d for d in candidates if when(d) <= day]
    if before:
        best = max(when(d) for d in before)
        return next(d for d in before if when(d) == best)
    best = min(when(d) for d in candidates)
    return next(d for d in candidates if when(d) == best)


# mark_owed, pass 3b (19.0.1.126.3): how far after an invoice its split
# payment may be dated, how many credits it may take and how many of the
# nearest credits are tried per invoice (C(12,2) + C(12,3) = 286 sums).
SPLIT_MAX_DAYS = 7
SPLIT_SIZES = (2, 3)
SPLIT_MAX_CANDIDATES = 12


def _cents(amount):
    return int(round(amount * 100))


def _split_payments(debits, credits, allocate, stats=None):
    """Pass 3b of mark_owed: a debit still open is paid by 2 or 3 open
    credits whose remaining amounts add up to its remaining amount exactly
    (to the cent), every one dated from the debit's day to SPLIT_MAX_DAYS
    after it.

    Debits go by date (row order on a tie). Per debit only the
    SPLIT_MAX_CANDIDATES credits nearest in date are tried. The best
    combination has the smallest maximum date gap, then fewer credits, then
    the earliest credit dates; two different combinations equal on all three
    are a tie: the debit is skipped (never a guess) and recorded as
    ambiguous. `credits` is the open credits in row order; each match
    consumes them entirely, so a later debit only sees what is left.

    `stats` (optional dict) collects "split": [(debit row, [credit rows])]
    and "ambiguous": [debit row].
    """
    dated = [(index, credit) for index, credit in enumerate(credits)
             if credit["row"].get("date")]
    if len(dated) < min(SPLIT_SIZES):
        return
    order = sorted(
        (d for d in debits if d["row"].get("date")),
        key=lambda d: d["row"]["date"],
    )
    for debit in order:
        if debit["rem"] <= OWED_TOLERANCE:
            continue
        day = debit["row"]["date"]
        target = _cents(debit["rem"])
        pool = []
        for index, credit in dated:
            gap = (credit["row"]["date"] - day).days
            if credit["rem"] > OWED_TOLERANCE and 0 <= gap <= SPLIT_MAX_DAYS \
                    and _cents(credit["rem"]) < target:
                pool.append((gap, index, credit))
        if len(pool) < min(SPLIT_SIZES):
            continue
        pool.sort(key=lambda item: (item[0], item[1]))
        pool = sorted(pool[:SPLIT_MAX_CANDIDATES], key=lambda item: item[1])
        best_key, best, tied = None, None, False
        for size in SPLIT_SIZES:
            for combo in combinations(pool, size):
                if sum(_cents(item[2]["rem"]) for item in combo) != target:
                    continue
                key = (max(item[0] for item in combo), size,
                       tuple(sorted(item[0] for item in combo)))
                if best_key is None or key < best_key:
                    best_key, best, tied = key, combo, False
                elif key == best_key:
                    tied = True
        if best is None:
            continue
        if tied:
            if stats is not None:
                stats.setdefault("ambiguous", []).append(debit["row"])
            continue
        for _gap, _index, credit in best:
            allocate(credit, debit, credit["rem"])
        if stats is not None:
            stats.setdefault("split", []).append(
                (debit["row"], [item[2]["row"] for item in best]))


def _reviewed_allocations(rows, debits, credits, allocations, allocate, stats=None):
    """Pass 0 of mark_owed: the reviewed allocations (see mark_owed)."""
    by_debit = {d["row"].get("line_key"): d for d in debits if d["row"].get("line_key")}
    by_credit = {c["row"].get("line_key"): c for c in credits if c["row"].get("line_key")}
    present = {row.get("line_key") for row in rows}

    def skip(alloc, why):
        if stats is not None:
            stats.setdefault("allocation_skipped", []).append((alloc, why))

    for alloc in allocations:
        credit_key, debit_key = alloc.get("credit_key"), alloc.get("debit_key")
        if credit_key not in present or debit_key not in present:
            skip(alloc, "missing")
            continue
        credit, debit = by_credit.get(credit_key), by_debit.get(debit_key)
        if not credit:
            skip(alloc, "not_credit")
            continue
        if not debit:
            skip(alloc, "not_debit")
            continue
        before = credit["rem"]
        allocate(credit, debit, alloc.get("amount") or 0.0)
        applied = round(before - credit["rem"], 2)
        if applied <= OWED_TOLERANCE:
            skip(alloc, "spent")
            continue
        reason = (alloc.get("reason") or "").strip()
        reasons = debit["row"]["reviewed_reasons"]
        if reason and reason not in reasons:
            reasons.append(reason)
        if stats is not None:
            stats.setdefault("allocated", []).append((alloc, applied))


def mark_owed(rows, stats=None, allocations=None):
    """Set row["owed"] on statement rows (oldest first), by priority passes.

    Debits are the rows with a positive net (debit - credit), credits the
    ones with a negative net; net-zero rows are ignored. Every pass applies
    only the amounts still unallocated:

    0. reviewed allocations (19.0.1.129.9, distri.owed.allocation): each
       dict of `allocations` (credit_key, debit_key, amount, reason) covers
       the debit row whose row["line_key"] is debit_key with the credit row
       whose row["line_key"] is credit_key, in the given order, up to the
       amount and to what both still have. One whose keys are not in the
       rows, or name a row on the wrong side, is skipped. A debit it touches
       gets its reason in row["reviewed_reasons"];
    1. reconciliation: row["reconciled"] of a credit lists (debit line id,
       amount) pairs of account.partial.reconcile; each covers the debit row
       whose row["line_id"] matches, for that amount;
    1b. history link (19.0.1.125.0): a credit whose row["history_target_id"]
       is set (a /cobro payment of an Odoo Online invoice) covers the debit
       row whose row["history_id"] is that id, up to what it still owes.
       Such a credit skips pass 2: an Online number may also be the number
       of another document of this Odoo (L4), so the id decides, not the
       memo. If that row is not in the statement, pass 2 applies as usual;
    2. memo: a credit whose row["memo"] names an invoice number (full, or
       its 5-digit tail) covers that debit, up to what it still owes;
    3. same amount: a credit whose remaining amount equals a debit's
       remaining amount pays it, preferring the debit dated on or before the
       credit, nearest in date, else the nearest after;
    3b. split payment (19.0.1.126.3): 2 or 3 credits whose remaining
       amounts add up exactly to a debit's remaining amount, all dated from
       the debit's day to 7 days after, pay it (_split_payments; a tie is
       skipped, never guessed);
    4. FIFO: what is left goes to the debits oldest first.

    A debit's «owed» is what stays uncovered; every other row gets 0.
    Invariant: sum(owed) == max(balance, 0), within cents.

    `stats` (optional dict) receives what pass 3b matched and skipped, and
    for pass 0 "allocated": [(allocation, amount applied)] and
    "allocation_skipped": [(allocation, why)], why being "missing" (a key
    not in the rows), "not_credit" / "not_debit" (wrong side) or "spent"
    (nothing left to apply).
    Returns (count, total) of the owed rows.
    """
    debits, credits = [], []
    for row in rows:
        row["owed"] = 0.0
        row["reviewed_reasons"] = []
        net = round((row.get("debit") or 0.0) - (row.get("credit") or 0.0), 2)
        if net > OWED_TOLERANCE:
            debits.append({"row": row, "rem": net})
        elif net < -OWED_TOLERANCE:
            credits.append({"row": row, "rem": -net})

    def allocate(credit, debit, amount):
        amount = round(min(amount, credit["rem"], debit["rem"]), 2)
        if amount > OWED_TOLERANCE:
            credit["rem"] = round(credit["rem"] - amount, 2)
            debit["rem"] = round(debit["rem"] - amount, 2)

    def open_(items):
        return [item for item in items if item["rem"] > OWED_TOLERANCE]

    # 0. reviewed allocations, by stable line_key
    if allocations:
        _reviewed_allocations(rows, debits, credits, allocations, allocate, stats)

    # 1. reconciliation (min(amount, what is left): never over pass 0)
    by_line = {d["row"]["line_id"]: d for d in debits if d["row"].get("line_id")}
    for credit in credits:
        for line_id, amount in credit["row"].get("reconciled") or ():
            if line_id in by_line:
                allocate(credit, by_line[line_id], amount)

    # 1b. history link: by id, never by number
    by_history = {d["row"]["history_id"]: d for d in debits if d["row"].get("history_id")}
    linked = set()
    for index, credit in enumerate(credits):
        target = by_history.get(credit["row"].get("history_target_id"))
        if target:
            linked.add(index)
            allocate(credit, target, credit["rem"])

    # 2. memo / ref
    by_number, by_tail = {}, {}
    for debit in debits:
        number = debit["row"].get("number") or ""
        if number:
            by_number.setdefault(number, []).append(debit)
            match = _INVOICE_NUMBER.match(number)
            if match:
                by_tail.setdefault(match.group(1), []).append(debit)
    for index, credit in enumerate(credits):
        if index in linked:
            continue
        text = credit["row"].get("memo") or ""
        own = credit["row"].get("number") or ""
        if own:
            text = text.replace(own, " ")
        for kind, ref in _memo_refs(text):
            if credit["rem"] <= OWED_TOLERANCE:
                break
            candidates = open_((by_number if kind == "full" else by_tail).get(ref, []))
            if candidates:
                day = credit["row"].get("date") or date.min
                allocate(credit, _nearest_debit(candidates, day), credit["rem"])

    # 3. same amount (credits are in date order already)
    for credit in open_(credits):
        candidates = [d for d in open_(debits)
                      if abs(d["rem"] - credit["rem"]) < OWED_TOLERANCE]
        if candidates:
            day = credit["row"].get("date") or date.min
            allocate(credit, _nearest_debit(candidates, day), credit["rem"])

    # 3b. split payment: 2 or 3 credits summing a debit to the cent
    _split_payments(debits, open_(credits), allocate, stats)

    # 4. FIFO
    for credit in credits:
        for debit in debits:
            if credit["rem"] <= OWED_TOLERANCE:
                break
            if debit["rem"] > OWED_TOLERANCE:
                allocate(credit, debit, credit["rem"])

    count, total = 0, 0.0
    for debit in debits:
        if debit["rem"] > OWED_TOLERANCE:
            debit["row"]["owed"] = debit["rem"]
            count += 1
            total += debit["rem"]
    return count, round(total, 2)


def link_days(env):
    """How many days a public statement link lasts (>= 1)."""
    value = env["ir.config_parameter"].sudo().get_param(PARAM_LINK_DAYS)
    if not value:
        return DEFAULT_LINK_DAYS
    try:
        return max(int(value), 1)
    except (TypeError, ValueError):
        return DEFAULT_LINK_DAYS


def _b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _link_signature(env, payload):
    digest = odoo_hmac(env(su=True), LINK_SCOPE, payload)
    return _b64(binascii.unhexlify(digest)[:LINK_SIGNATURE_BYTES])


def share_token(env, partner, date_from=False, date_to=False, now=None):
    """(token, expiry) of the public link to `partner`'s statement between
    the two dates. The expiry is the end of the last day, in Argentina."""
    now = now or datetime.now(AR_TZ)
    last_day = now.astimezone(AR_TZ).date() + timedelta(days=link_days(env))
    expires = datetime.combine(last_day, time(23, 59, 59), tzinfo=AR_TZ)
    payload = "%s:%d:%d:%s:%s" % (
        LINK_VERSION, partner.commercial_partner_id.id, int(expires.timestamp()),
        date_from.isoformat() if date_from else "", date_to.isoformat() if date_to else "")
    return "%s.%s" % (_b64(payload.encode()), _link_signature(env, payload)), expires


def read_share_token(env, token, now=None):
    """{"partner", "date_from", "date_to", "expires"} of a valid, unexpired
    link; None for anything else (forged, altered, expired, unknown or
    generic customer)."""
    try:
        encoded, signature = (token or "").split(".")
        payload = _unb64(encoded).decode()
        version, partner_id, expires, date_from, date_to = payload.split(":")
        if version != LINK_VERSION or not consteq(signature, _link_signature(env, payload)):
            return None
        expires = datetime.fromtimestamp(int(expires), AR_TZ)
        date_from = date.fromisoformat(date_from) if date_from else False
        date_to = date.fromisoformat(date_to) if date_to else False
        partner_id = int(partner_id)
    except (ValueError, TypeError, UnicodeDecodeError, binascii.Error):
        return None
    if expires < (now or datetime.now(AR_TZ)):
        return None
    partner = env["res.partner"].sudo().with_context(active_test=False).browse(partner_id).exists()
    if not partner or partner.parent_id or partner.distri_generic_partner:
        return None
    return {"partner": partner, "date_from": date_from, "date_to": date_to, "expires": expires}


def local_date(moment):
    """Naive UTC datetime -> date in Argentina."""
    if not moment:
        return False
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).date()


def imported_customer_view(mov):
    """How the customer sees an imported movement: (type, reference).

    The history keeps the Odoo Online invoice or credit note number
    (0001/AAAA/NNNNN), which the customer never sees: it shows as «Remito»
    or «Nota de crédito» without a number. Payments keep their receipt
    number.
    """
    number = mov.comprobante or ""
    if es_numero_de_factura(number):
        kind = "Remito" if (mov.debe or 0.0) >= (mov.haber or 0.0) else "Nota de crédito"
        return kind, ""
    if mov.tipo in ("factura", "pago"):
        # bank and cash statements (BNK1/..., CSH1/...) and receipts
        return ("Pago" if mov.haber else "Ajuste"), number
    if mov.tipo == "nota":
        return "Nota de crédito", ""
    return "Movimiento", number


def _imported_staff_type(mov):
    number = mov.comprobante or ""
    if es_numero_de_factura(number):
        return "Factura" if (mov.debe or 0.0) >= (mov.haber or 0.0) else "Nota de crédito"
    if mov.tipo == "nota":
        return "Nota de crédito"
    if mov.haber and not mov.debe:
        return "Pago"
    return "Ajuste" if mov.tipo != "otro" else "Movimiento"


def statement_line_key(kind, record_id, nth=1):
    """Stable identity of a statement row, kept by the comments of the phone
    app (distri.account.comment.line_key). The sort key (`key`) is not
    enough: it carries the date and, for this Odoo, the receivable line id,
    which a reset to draft can recreate. Instead:

    - this Odoo: «account.move:<move id>» (a move keeps its id; a second
      receivable line of the same move adds «:2», «:3»…);
    - Odoo Online history: «online:<origen_id>», the id in Odoo Online, so a
      new import of the history keeps it; «distri.cuenta.movimiento:<id>»
      for a row without it;
    - provisional returns: «distri.route.return:<id>».
    """
    key = "%s:%d" % (kind, record_id)
    return key if nth <= 1 else "%s:%d" % (key, nth)


# 19.0.1.126.0: a credited receipt the Mercado Pago report already tied
# (row["confirmed"]): a green check on screens, plain text where an icon may
# not render (PDF, text). Nothing for a payment not verified yet.
CONFIRMED_LABEL = "Pago confirmado"
CONFIRMED_TEXT = "✓ Confirmado"
CONFIRMED_MARK_HTML = (
    " <span class='o_distri_confirmado' style='color:#198754;font-weight:bold' "
    "title='%s' aria-label='%s' role='img'>✓</span>" % (CONFIRMED_LABEL, CONFIRMED_LABEL))


def _statement_number(row):
    """The row's number; a payment from a Mercado Pago card links to the card."""
    number = html_escape(row["number"])
    if not row.get("card_id"):
        return number
    mark = (" <span class='badge text-bg-light o_distri_estado_verificar' title='Comprobante "
            "acreditado: falta verlo en el reporte de Mercado Pago'>a verificar con MP</span>"
            if row.get("audit_pending") else "")
    return ("%s <a href='/odoo/distri.mp.collection/%d' target='_blank' "
            "class='o_distri_estado_tarjeta'>Ver tarjeta</a>%s" % (number, row["card_id"], mark))


def _money(amount):
    """$ 62.300 / $ 62.300,50 / -$ 1.200"""
    sign = "-" if (amount or 0.0) < -0.004 else ""
    text = "{:,.2f}".format(abs(amount or 0.0)).replace(",", "X").replace(".", ",").replace("X", ".")
    if text.endswith(",00"):
        text = text[:-3]
    return "%s$ %s" % (sign, text)


class ResPartnerEstadoCuenta(models.Model):
    _inherit = "res.partner"

    distri_statement_html = fields.Html(
        string="Estado de cuenta", compute="_compute_distri_statement_html",
        sanitize=False, groups=VE_SALDOS)
    distri_statement_balance = fields.Monetary(
        string="Saldo de la cuenta", compute="_compute_distri_statement_html",
        currency_field="currency_id", groups=VE_SALDOS,
        help="Historia del sistema anterior más lo registrado en este sistema y las "
             "devoluciones informadas. Positivo: debe. Negativo: saldo a favor.")

    # ------------------------------------------------------------ rows

    def _distri_candidate_cards(self):
        """The pending Mercado Pago cards of the customer marked «Candidato ·
        falta el comprobante» (with or without the receipt arrived yet)."""
        self.ensure_one()
        return self.env["distri.mp.collection"].sudo().search([
            ("partner_id", "child_of", self.commercial_partner_id.id),
            ("kind", "=", "income"), ("state", "=", "draft"),
            ("receipt_candidate", "=", True)], order="date, id")

    def _distri_statement_rows(self):
        """Every movement of the customer, oldest first, with running balance.

        Each row: date, source, doc_type (staff), number (staff: invoice or
        payment number), remito, concept, customer_type, customer_ref,
        debit, credit, balance, provisional, owed (uncovered amount, see
        mark_owed), review_note (the account review's note, shown only), key
        (sort key), line_key (stable identity of the row, see
        statement_line_key), line_id (account.move.line of this Odoo, else
        False), reconciled ((debit line id, amount) pairs reconciled with
        this credit line), memo (label/ref text that may name an
        invoice), history_id (the distri.cuenta.movimiento of an Online
        history row, else False) and history_target_id (the history row a
        /cobro payment of an Online invoice pays, 19.0.1.125.0, else False);
        the last five feed mark_owed and are never shown.
        """
        self.ensure_one()
        partner = self.commercial_partner_id.sudo()
        rows = []
        from .caja_v2 import debt_account_ids

        lines = self.env["account.move.line"].sudo().search([
            ("partner_id", "=", partner.id),
            ("parent_state", "=", "posted"),
            ("account_id.account_type", "=", "asset_receivable"),
            # 19.0.1.129.27: «Deudores varios» (employees' debts) is no sale
            ("account_id", "not in", debt_account_ids(self.env)),
        ], order="date, id")
        seen = set()
        per_move = {}
        # 19.0.1.125.0: /cobro payments of an Online invoice, by their move
        targets = {p.move_id.id: p.distri_history_invoice_id.id
                   for p in self.env["account.payment"].sudo().search([
                       ("move_id", "in", lines.move_id.ids),
                       ("distri_history_invoice_id", "!=", False)])} if lines else {}
        for line in lines:
            move = line.move_id
            per_move[move.id] = per_move.get(move.id, 0) + 1
            seen.add(clave_documento(move.name, line.debit, line.credit))
            if move.journal_id.code == AJC_JOURNAL_CODE:
                # «Ajuste de cuentas» (19.0.1.44.0): neither a sale nor a return
                doc_type = customer_type = "Ajuste de cuenta"
                remito = customer_ref = ""
            elif move.move_type == "out_invoice":
                doc_type, customer_type = "Factura", "Remito"
                remito = customer_ref = move._distri_remito_texto()
            elif move.move_type == "out_refund":
                doc_type, customer_type = "Nota de crédito", "Devolución"
                remito = customer_ref = move._distri_remito_texto()
            elif line.credit:
                doc_type = customer_type = "Pago"
                remito, customer_ref = "", move.name or ""
            else:
                doc_type = customer_type = "Ajuste"
                remito = customer_ref = ""
            rows.append({
                "date": line.date,
                "source": SOURCE_ODOO,
                "doc_type": doc_type,
                "number": move.name or "",
                "remito": remito,
                "concept": move.ref or line.name or "",
                "customer_type": customer_type,
                "customer_ref": customer_ref,
                "debit": line.debit,
                "credit": line.credit,
                "provisional": False,
                "owed": 0.0,
                "review_note": "",
                "res_model": "account.move",
                "res_id": move.id,
                "key": (line.date, _SOURCE_ORDER[SOURCE_ODOO], line.id),
                "line_key": statement_line_key("account.move", move.id, per_move[move.id]),
                "line_id": line.id,
                "reconciled": [(p.debit_move_id.id, p.amount)
                               for p in line.matched_debit_ids.sorted("id")],
                "memo": " ".join(dict.fromkeys(x for x in (line.name, move.ref) if x)),
                "history_id": False,
                "history_target_id": targets.get(move.id, False) if line.credit else False,
            })
        seen.discard(None)

        history = self.env["distri.cuenta.movimiento"].sudo().with_context(active_test=True).search(
            [("partner_id", "=", partner.id)], order="fecha, id")
        for mov in history:
            if clave_documento(mov.comprobante, mov.debe, mov.haber) in seen:
                continue
            customer_type, customer_ref = imported_customer_view(mov)
            rows.append({
                "date": mov.fecha,
                "source": SOURCE_ONLINE,
                "doc_type": _imported_staff_type(mov),
                "number": mov.comprobante or "",
                "remito": "",
                "concept": mov.concepto or "",
                "customer_type": customer_type,
                "customer_ref": customer_ref,
                "debit": mov.debe or 0.0,
                "credit": mov.haber or 0.0,
                "provisional": False,
                # set by mark_owed; the review mark is only a note now
                "owed": 0.0,
                "review_note": mov.distri_owed_note or (
                    "Marcada adeudada en la revisión" if mov.distri_owed else ""),
                "res_model": "distri.cuenta.movimiento",
                "res_id": mov.id,
                "key": (mov.fecha, _SOURCE_ORDER[SOURCE_ONLINE], mov.id),
                "line_key": (statement_line_key("online", mov.origen_id) if mov.origen_id
                             else statement_line_key("distri.cuenta.movimiento", mov.id)),
                "line_id": False,
                "reconciled": [],
                "memo": mov.concepto or "",
                "history_id": mov.id,
                "history_target_id": False,
            })

        for ret in self.env["distri.route.return"]._distri_provisional_for(partner):
            when = local_date(ret.stop_id.visit_captured_at or ret.create_date) or today_ar()
            remitos = ret.order_id._distri_remitos_texto() or ret.order_id.name
            rows.append({
                "date": when,
                "source": SOURCE_RETURN,
                "doc_type": PROVISIONAL_LABEL,
                "number": "%s · %s" % (ret.order_id.name, ret.route_id.name or ""),
                "remito": remitos,
                "concept": "%g %s%s · %s" % (
                    ret.quantity, ret.product_id.name or "",
                    (" (%s)" % ret.reason) if ret.reason else "", PROVISIONAL_NOTE),
                "customer_type": PROVISIONAL_LABEL,
                "customer_ref": "%s · %s" % (remitos, PROVISIONAL_NOTE),
                "debit": 0.0,
                "credit": ret.amount,
                "provisional": True,
                "owed": 0.0,
                "review_note": "",
                "res_model": "distri.route.return",
                "res_id": ret.id,
                "key": (when, _SOURCE_ORDER[SOURCE_RETURN], ret.id),
                "line_key": statement_line_key("distri.route.return", ret.id),
                "line_id": False,
                "reconciled": [],
                "memo": "",
                "history_id": False,
                "history_target_id": False,
            })

        # a payment registered from a Mercado Pago card links back to it
        move_ids = [r["res_id"] for r in rows if r["res_model"] == "account.move" and r["credit"]]
        if move_ids:
            cards = self.env["distri.mp.collection"].sudo().search(
                [("payment_id.move_id", "in", move_ids)])
            by_move = {c.payment_id.move_id.id: c.id for c in cards}
            # 19.0.1.126.0: the receipt cards credited at once. «confirmed»
            # (shown to everybody as «Pago confirmado»): the report tied it;
            # «audit_pending» (staff only, «a verificar con MP»): not yet
            pending, confirmed = set(), set()
            for card in cards.filtered(lambda c: c.kind == "verify" and c.credited_at):
                move = card.payment_id.move_id.id
                if card.audit_state == "verificado":
                    confirmed.add(move)
                elif card.audit_state == "pendiente":
                    pending.add(move)
            for row in rows:
                if row["res_model"] == "account.move" and row["res_id"] in by_move:
                    row["card_id"] = by_move[row["res_id"]]
                    row["audit_pending"] = row["res_id"] in pending
                    row["confirmed"] = row["res_id"] in confirmed
        for row in rows:
            row.setdefault("confirmed", False)
        rows.sort(key=lambda r: (r["key"][0] or date.min,) + r["key"][1:])
        balance = 0.0
        for row in rows:
            balance += (row["debit"] or 0.0) - (row["credit"] or 0.0)
            row["balance"] = balance
        mark_owed(rows, allocations=self._distri_owed_allocations())
        # 19.0.1.129.9: staff see why a reviewed allocation decided a debit
        # (review_note: staff views only, never the customer's)
        for row in rows:
            if row.get("reviewed_reasons"):
                row["review_note"] = " · ".join(x for x in (
                    row["review_note"],
                    REVIEWED_PREFIX + " · ".join(row["reviewed_reasons"])) if x)
        return rows

    def _distri_statement(self, date_from=False, date_to=False):
        """Rows between two dates (inclusive) plus opening and closing balance."""
        self.ensure_one()
        rows = self._distri_statement_rows()
        opening = 0.0
        selected = []
        for row in rows:
            day = row["date"] or date.min
            if date_from and day < date_from:
                opening = row["balance"]
                continue
            if date_to and day > date_to:
                continue
            selected.append(row)
        closing = selected[-1]["balance"] if selected else opening
        balance = rows[-1]["balance"] if rows else 0.0
        # staff only (the customer page never reads them): shown, never applied
        candidates = [{
            "date": card.date_day,
            "name": card.name,
            "amount": card.amount,
            "note": card.candidate_note or "",
            "has_receipt": bool(card.receipt_id),
            "res_id": card.id,
        } for card in self._distri_candidate_cards()]
        candidate_amount = sum(c["amount"] for c in candidates)
        return {
            "candidates": candidates,
            "candidate_amount": candidate_amount,
            "whatif_balance": balance - candidate_amount,
            "rows": selected,
            "opening": opening,
            "closing": closing,
            "balance": balance,
            "provisional": sum(r["credit"] for r in rows if r["provisional"]),
            # over every row, not only the period's: the red follows the balance
            "owed_count": len([r for r in rows if r["owed"]]),
            "owed_total": round(sum(r["owed"] for r in rows), 2),
            "debit": sum(r["debit"] for r in selected),
            "credit": sum(r["credit"] for r in selected),
        }

    # ------------------------------------------------------------ backend

    @api.depends_context("uid")
    def _compute_distri_statement_html(self):
        for partner in self:
            if not partner.id or partner.distri_generic_partner:
                partner.distri_statement_html = False
                partner.distri_statement_balance = 0.0
                continue
            data = partner._distri_statement()
            partner.distri_statement_balance = data["balance"]
            partner.distri_statement_html = partner._distri_statement_table(
                data, newest_first=True)

    def _distri_statement_table(self, data, newest_first=False):
        """Staff table: invoice number and remito, running balance."""
        rows = list(reversed(data["rows"])) if newest_first else data["rows"]
        candidates = data.get("candidates") or []
        if not rows and not candidates:
            return "<p class='text-muted'>Sin movimientos.</p>"
        out = [
            "<div class='o_distri_estado'>",
            "<p><b>Saldo%s: %s</b>%s</p>" % (
                " actual" if candidates else "",
                html_escape(_money(data["balance"])),
                (" · incluye devoluciones informadas a controlar por %s"
                 % html_escape(_money(data["provisional"]))) if data["provisional"] else ""),
        ]
        if data.get("owed_count"):
            out.append("<p class='text-danger o_distri_estado_adeudadas'><b>Facturas adeudadas: "
                       "%d por %s</b> · en rojo en la tabla · cada pago cancela primero "
                       "la factura que concilia o nombra; el resto, de la más vieja a la más "
                       "nueva</p>" % (
                           data["owed_count"], html_escape(_money(data["owed_total"]))))
        if candidates:
            out.append("<p><b>%s: %s</b> · %d %s por %s, no restados del saldo</p>" % (
                WHATIF_LABEL, html_escape(_money(data["whatif_balance"])), len(candidates),
                "pago a confirmar" if len(candidates) == 1 else "pagos a confirmar",
                html_escape(_money(data["candidate_amount"]))))
        out += [
            "<table class='table table-sm table-striped o_distri_estado_tabla'>",
            "<thead><tr><th>Fecha</th><th>Tipo</th><th>Comprobante</th><th>Remito</th>"
            "<th>Concepto</th><th>Origen</th><th class='text-end'>Debe</th>"
            "<th class='text-end'>Haber</th><th class='text-end'>Saldo</th></tr></thead><tbody>",
        ]
        # the candidates first, muted: the «Haber» in parentheses is not applied
        for cand in candidates:
            out.append(
                "<tr class='table-secondary text-muted o_distri_estado_candidato'><td>%s</td>"
                "<td>%s <span class='badge text-bg-secondary'>%s</span></td><td>%s</td><td></td>"
                "<td>%s</td><td>Mercado Pago</td><td></td><td class='text-end'>(%s)</td>"
                "<td class='text-end'>no resta</td></tr>" % (
                    cand["date"].strftime("%d/%m/%Y") if cand["date"] else "",
                    html_escape(CANDIDATE_ROW_LABEL),
                    "llegó el comprobante" if cand["has_receipt"] else "sin comprobante",
                    html_escape(cand["name"]), html_escape(cand["note"]),
                    html_escape(_money(cand["amount"]))))
        for row in rows:
            note = row.get("review_note") or ""
            badges = ""
            if row["provisional"]:
                badges += " <span class='badge text-bg-warning'>a controlar</span>"
            elif row.get("owed"):
                badges += " <span class='badge text-bg-danger'>debe %s</span>" % html_escape(
                    _money(row["owed"]))
            if row.get("confirmed"):
                badges += CONFIRMED_MARK_HTML
            elif row.get("driver_cash_pending"):
                # 19.0.1.129.2 (models/efectivo_a_rendir.py)
                badges += (" <span class='badge text-bg-info o_distri_estado_a_rendir' "
                           "title='Efectivo cobrado por el repartidor, a rendir'>a rendir</span>")
            if note:
                badges += (" <span class='badge text-bg-secondary o_distri_estado_nota' "
                           "title='%s'>Revisión: %s</span>" % (
                               html_escape(note), html_escape(note[:60])))
            out.append(
                "<tr%s><td>%s</td><td>%s%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td>"
                "<td class='text-end'>%s</td><td class='text-end'>%s</td>"
                "<td class='text-end'>%s</td></tr>" % (
                    " class='table-warning'" if row["provisional"] else (
                        " class='table-danger o_distri_estado_adeudada'"
                        if row.get("owed") else ""),
                    row["date"].strftime("%d/%m/%Y") if row["date"] else "",
                    html_escape(row["doc_type"]),
                    badges,
                    _statement_number(row), html_escape(row["remito"]),
                    html_escape(row["concept"]), html_escape(SOURCE_LABELS[row["source"]]),
                    html_escape(_money(row["debit"])) if row["debit"] else "",
                    html_escape(_money(row["credit"])) if row["credit"] else "",
                    html_escape(_money(row["balance"]))))
        out.append("</tbody></table></div>")
        return "".join(out)

    def action_distri_estado_cuenta(self):
        """«Estado de cuenta»: the statement with date filters and printing."""
        self.ensure_one()
        wizard = self.env["distri.estado.cuenta"].create({
            "partner_id": self.commercial_partner_id.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Estado de cuenta de %s" % self.commercial_partner_id.name,
            "res_model": "distri.estado.cuenta",
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }


class DistriEstadoCuenta(models.TransientModel):
    _name = "distri.estado.cuenta"
    _description = "Estado de cuenta del cliente"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True,
                                 readonly=True)
    date_from = fields.Date(string="Desde")
    date_to = fields.Date(string="Hasta")
    currency_id = fields.Many2one("res.currency",
                                  default=lambda self: self.env.company.currency_id)
    opening = fields.Monetary(string="Saldo anterior", compute="_compute_statement")
    closing = fields.Monetary(string="Saldo al cierre", compute="_compute_statement")
    balance = fields.Monetary(string="Saldo actual", compute="_compute_statement")
    candidate_amount = fields.Monetary(string="Pagos a confirmar", compute="_compute_statement",
                                       help="Candidatos sin comprobante: no restan del saldo.")
    whatif_balance = fields.Monetary(string=WHATIF_LABEL, compute="_compute_statement")
    # «Candidato · falta el comprobante» (19.0.1.45.0): notes and «Aprobar»
    # from the statement, administration only (the board's access)
    candidate_ids = fields.Many2many(
        "distri.mp.collection", "distri_estado_cuenta_candidate_rel", "wizard_id",
        "collection_id", string=CANDIDATE_ROW_LABEL, compute="_compute_candidate_ids",
        store=True, readonly=False)
    statement_html = fields.Html(string="Movimientos", compute="_compute_statement",
                                 sanitize=False)

    def _check_access_saldos(self):
        user = self.env.user
        if not (self.env.su or any(user.has_group(g) for g in VE_SALDOS.split(","))):
            raise UserError("Solo vendedores, administración y gerencia ven el estado de cuenta.")

    def _check_partner_visible(self):
        """19.0.1.96.0 (audit F9): the statement is computed with sudo, so
        the wizard itself checks that the user may see the customer: the
        same record rules as the contacts (a seller, his portfolio). The
        customer is the commercial partner; seeing one of its contacts is
        enough (the tag may sit on the contact)."""
        if self.env.su:
            return
        Partner = self.env["res.partner"]
        for wizard in self:
            partner = wizard.partner_id
            if partner and not Partner.search_count([
                    "|", ("id", "=", partner.id),
                    ("commercial_partner_id", "=", partner.id)], limit=1):
                raise AccessError("Ese cliente no está en tu cartera: no podés ver su estado "
                                  "de cuenta.")

    @api.model_create_multi
    def create(self, vals_list):
        self._check_access_saldos()
        wizards = super().create(vals_list)
        wizards._check_partner_visible()
        return wizards

    @api.depends("partner_id", "date_from", "date_to")
    def _compute_statement(self):
        for wizard in self:
            if not wizard.partner_id:
                wizard.update({"opening": 0.0, "closing": 0.0, "balance": 0.0,
                               "candidate_amount": 0.0, "whatif_balance": 0.0,
                               "statement_html": False})
                continue
            data = wizard._statement()
            wizard.opening = data["opening"]
            wizard.closing = data["closing"]
            wizard.balance = data["balance"]
            wizard.candidate_amount = data["candidate_amount"]
            wizard.whatif_balance = data["whatif_balance"]
            wizard.statement_html = wizard.partner_id.sudo()._distri_statement_table(
                data, newest_first=False)

    @api.depends("partner_id")
    def _compute_candidate_ids(self):
        admin = self.env.su or self.env.user.has_group("distri_pedido.grupo_administracion")
        for wizard in self:
            wizard.candidate_ids = wizard.partner_id._distri_candidate_cards().with_env(
                self.env) if admin and wizard.partner_id else False

    def _statement(self):
        self.ensure_one()
        self._check_access_saldos()
        self._check_partner_visible()
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise UserError("La fecha «Desde» es posterior a «Hasta».")
        return self.partner_id.sudo()._distri_statement(self.date_from, self.date_to)

    def _report_values(self):
        """What the PDF prints."""
        self.ensure_one()
        data = self._statement()
        return {
            "partner": self.partner_id,
            "date_from": self.date_from,
            "date_to": self.date_to,
            "printed": datetime.now(AR_TZ).strftime("%d/%m/%Y %H:%M"),
            "data": data,
            "money": _money,
            "sources": SOURCE_LABELS,
        }

    def action_print(self):
        self.ensure_one()
        self._statement()
        return self.env.ref("distri_pedido.report_estado_cuenta").report_action(self)


class ReportEstadoCuenta(models.AbstractModel):
    _name = "report.distri_pedido.estado_cuenta_documento"
    _description = "Estado de cuenta (PDF)"

    @api.model
    def _get_report_values(self, docids, data=None):
        wizards = self.env["distri.estado.cuenta"].browse(docids)
        return {
            "doc_ids": docids,
            "doc_model": "distri.estado.cuenta",
            "docs": wizards,
            "values": {w.id: w._report_values() for w in wizards},
        }
