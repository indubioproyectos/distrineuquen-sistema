"""«Cuentas (celular)»: the phone app to review customer accounts
(19.0.1.74.0, controllers/cuentas.py, static/src/cuentas/).

Read-only: it shows debtors, statements, the Mercado Pago card and receipt
of a payment and the memo and cash sheet of a cash payment (19.0.1.75.0,
models/cash_sheet.py; a seller sees only his customers' rows of a sheet). It authorizes nothing: no
payment, card or adjustment action is reachable from it. The only write is
a comment on an account or on one statement row (distri.account.comment).

Who opens it (app_scope):
- administración (gerencia implies it) and Settings: every customer («all»);
- vendedor and reparto (19.0.1.92.0, «staff»): every customer too, they
  chase old missing receipts; but a seller without Administración never
  reaches a distributor's account (distri_lista_oculta, the same policy as
  models/lista_oculta.py), and the «incobrable» state stays hidden from
  them. This is checked here, in the app layer: the partner record rules
  (security/reglas.xml) are unchanged, and the app reads with sudo only
  after `_app_partner` has accepted the id;
- 19.0.1.129.20: an external seller (res.users distri_tipo_vendedor
  «externo», seller group, without Administración nor Settings,
  res.users._distri_is_external_seller) is «staff» too, but reaches only
  his customers (res.users._distri_chosen_customers, the same set as the
  partner record rule: the customers of the sale orders that
  mis_ventas.de_vendedor counts for him or that he created, any state, and
  the customers he created). The one check is `_app_allowed`: the debtor list
  (`_app_customers`), every account endpoint through `_app_partner`
  (statement, row detail, card receipt, PDF, WhatsApp share, comments),
  comment deletion and the rows and originals of a cash sheet
  (`_app_visible_partner_ids`). «Transferencias» stays for the audit groups;
- anyone else: refused.

The debtor list and the statement reuse the logic of «Deudores» and of the
account statement (models/deudores.py debtor_entries, models/estado_cuenta.py
_distri_statement), computed live.

19.0.1.112.0 (owner-approved mockups «05 Estado de cuenta» and
«pc-cuentas-deudores»): the statement takes a free date range besides the
quick periods; «Compartir PDF» prints the existing report
(report_estado_cuenta) through the statement wizard, so its rules apply as
they are: vendedor, administración or gerencia, and a seller only for the
customers of his portfolio (audit F9). «Por WhatsApp» sends the public link
of models/estado_cuenta.py share_token under the same rule and only while
the account is shown to the customer (models/cuenta_visible.py). The
account also lists the customer's open «Comprobantes pendientes»
(models/comprobante_pendiente.py). The debtors take the filters of the PC
screen (zone, minimum balance, age of the oldest red invoice). Still
read-only: nothing here approves, registers or adjusts money.
"""
import math
from datetime import date, timedelta, timezone
from urllib.parse import quote

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .armado import normalizar_ciudad
from .deudores import ADMIN_GROUP, DEBT_EPSILON, SELLERS, debtor_balances, debtor_entries, \
    direct_customers, seller_of, seller_tags
from .ar_phone import wa_digits
from .comprobante_pendiente import OPEN_STATES
from .estado_cuenta import PROVISIONAL_NOTE, SOURCE_LABELS, share_token
from .lista_oculta import ve_mayoristas
from .reparto_cliente import AR_TZ, today_ar, town_from_name

SELLER_GROUP = "distri_pedido.grupo_vendedor"
DRIVER_GROUP = "distri_pedido.grupo_reparto"
SYSTEM_GROUP = "base.group_system"
APP_DENIED = "Esta pantalla es para administración, gerencia, vendedores y repartidores."
PARTNER_DENIED = "No podés ver la cuenta de ese cliente."
# a comment can be deleted by its author during this time
COMMENT_DELETE_WINDOW = timedelta(hours=1)
COMMENT_MAX_LENGTH = 2000
PERIODS = {"1m": 1, "3m": 3, "all": None}
DEFAULT_PERIOD = "3m"
# 19.0.1.112.0: «Elegir fechas», a free range (date_from / date_to)
CUSTOM_PERIOD = "custom"
SHARE_HIDDEN = ("La cuenta está oculta para el cliente (administración la muestra desde el "
                "contacto). Mandale el PDF con «Compartir PDF».")
SHARE_NO_WHATSAPP = "El cliente no tiene WhatsApp cargado. Mandale el PDF con «Compartir PDF»."
PAGE_SIZE = 50
SELLER_FILTER_VALUES = ("all", "italo", "distrineuquen")
# 19.0.1.129.43: the one list of every customer (app_list), by pages
LIST_PAGE = 50
LIST_MAX = 200
LIST_STATES = ("all", "debt", "zero", "credit", "uncollectible")
LIST_SORTS = ("debt", "oldest", "name")
CARD_STATES = {"draft": "A revisar", "confirmed": "Confirmado", "ignored": "Descartado"}


def app_scope(env):
    """'all' (administración, gerencia, Settings), 'staff' (vendedor or
    reparto, 19.0.1.92.0) or None (no access)."""
    user = env.user
    if user.share:
        return None
    if user.has_group(ADMIN_GROUP) or user.has_group(SYSTEM_GROUP):
        return "all"
    if user.has_group(SELLER_GROUP) or user.has_group(DRIVER_GROUP):
        return "staff"
    return None


def _iso(day):
    return day.isoformat() if day else ""


def _day_text(day):
    return day.strftime("%d/%m/%Y") if day else ""


def _parse_day(value):
    """«2026-11-01» -> date; empty -> False; anything else -> UserError."""
    if not value:
        return False
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value).strip())
    except ValueError:
        raise UserError("La fecha «%s» no es válida." % value) from None


def _number(value):
    """A non-negative finite number; 0 for anything else (19.0.1.129.45:
    «inf» and «nan» from a hand-made URL included)."""
    try:
        number = float(value or 0)
    except (TypeError, ValueError):
        return 0.0
    return max(number, 0.0) if math.isfinite(number) else 0.0


def filter_debtors(entries, zone="", min_balance=0.0, min_age=0, today=None):
    """The PC filters over debtor_entries (19.0.1.112.0): the zone, the
    minimum balance and the age in days of the oldest red invoice
    (oldest_owed, FIFO). A filter left empty does not filter; with an age,
    a customer without red invoices is left out."""
    today = today or today_ar()
    zone = (zone or "").strip()
    min_balance = _number(min_balance)
    min_age = int(_number(min_age))
    out = []
    for entry in entries:
        if zone and (entry["zone"] or "") != zone:
            continue
        if min_balance and entry["balance"] < min_balance:
            continue
        if min_age and not (entry.get("oldest_owed")
                            and (today - entry["oldest_owed"]).days >= min_age):
            continue
        out.append(entry)
    return out


def _fold(text):
    """Lower case without accents, for the list search («Peñón» ~ «penon»)."""
    import unicodedata
    text = unicodedata.normalize("NFD", str(text or "").lower())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def _digits(text):
    return "".join(ch for ch in str(text or "") if ch.isdigit())


def balance_state(balance):
    """«debt», «zero» or «credit» of a balance, with the debtors' one-cent
    rule (deudores.DEBT_EPSILON)."""
    if balance > DEBT_EPSILON:
        return "debt"
    if balance < -DEBT_EPSILON:
        return "credit"
    return "zero"


def _int(value, default=0, low=0, high=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    value = max(value, low)
    return min(value, high) if high is not None else value


def _local_text(moment):
    """Naive UTC datetime -> «dd/mm/aaaa HH:MM» in Argentina."""
    if not moment:
        return ""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime("%d/%m/%Y %H:%M")


class DistriAccountComment(models.Model):
    """A note about a customer's account or about one row of its statement.

    `line_key` is the row's `line_key` (models/estado_cuenta.py
    statement_line_key): the move id for this Odoo, the Odoo Online id for
    the imported history, the return id for a provisional return. It does
    not depend on the date, the order or the local id of an imported row,
    so it survives recomputing the statement and new imports of the Online
    history. `line_label` keeps how the row read when the comment was made,
    so the comment still makes sense if the row stops showing (an Online
    row replaced by the same document in this Odoo)."""
    _name = "distri.account.comment"
    _description = "Comentario de cuenta"
    _order = "create_date desc, id desc"
    _rec_name = "body"

    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, index=True,
                                 ondelete="cascade", domain=[("parent_id", "=", False)])
    line_key = fields.Char(string="Movimiento (clave)", index=True,
                           help="Vacío: comentario sobre la cuenta. Si no, el movimiento del "
                                "estado de cuenta al que se refiere.")
    line_label = fields.Char(string="Movimiento")
    body = fields.Text(string="Comentario", required=True)
    resolved = fields.Boolean(string="Resuelto")
    author_id = fields.Many2one("res.users", string="Autor", related="create_uid", store=True)

    @api.model_create_multi
    def create(self, vals_list):
        Partner = self.env["res.partner"].with_context(active_test=False)
        for vals in vals_list:
            body = (vals.get("body") or "").strip()
            if not body:
                raise UserError("Escribí el comentario.")
            vals["body"] = body[:COMMENT_MAX_LENGTH]
            if vals.get("partner_id"):
                # always on the account (commercial partner), never a contact
                vals["partner_id"] = Partner.browse(vals["partner_id"]).commercial_partner_id.id
        return super().create(vals_list)

    def write(self, vals):
        if not self.env.su and set(vals) - {"resolved"}:
            raise AccessError("Un comentario no se edita: se puede marcar como resuelto.")
        return super().write(vals)

    def unlink(self):
        if not self.env.su:
            limit = fields.Datetime.now() - COMMENT_DELETE_WINDOW
            for comment in self:
                if comment.create_uid != self.env.user or comment.create_date < limit:
                    raise AccessError("Solo el autor borra su comentario, durante la primera hora.")
        return super().unlink()

    def _can_delete(self):
        self.ensure_one()
        limit = fields.Datetime.now() - COMMENT_DELETE_WINDOW
        return self.create_uid == self.env.user and self.create_date >= limit

    def _app_payload(self):
        return [{
            "id": c.id,
            "line_key": c.line_key or "",
            "line_label": c.line_label or "",
            "body": c.body,
            "author": c.create_uid.name or "",
            "date": _local_text(c.create_date),
            "resolved": c.resolved,
            "can_delete": c._can_delete(),
        } for c in self]


class ResPartnerAccountComment(models.Model):
    _inherit = "res.partner"

    distri_comment_count = fields.Integer(
        string="Comentarios de la cuenta", compute="_compute_distri_comment_count",
        groups="distri_pedido.grupo_vendedor,base.group_system")

    @api.depends_context("uid")
    def _compute_distri_comment_count(self):
        data = self.env["distri.account.comment"]._read_group(
            [("partner_id", "in", self.commercial_partner_id.ids)], ["partner_id"], ["__count"])
        counts = {partner.id: count for partner, count in data}
        for partner in self:
            partner.distri_comment_count = counts.get(partner.commercial_partner_id.id, 0)

    def action_distri_account_comments(self):
        self.ensure_one()
        partner = self.commercial_partner_id
        return {
            "type": "ir.actions.act_window",
            "name": "Comentarios de %s" % partner.name,
            "res_model": "distri.account.comment",
            "view_mode": "list,form",
            "domain": [("partner_id", "=", partner.id)],
            "context": {"default_partner_id": partner.id},
        }


class DistriAccountApp(models.AbstractModel):
    """What the phone app reads. Every method runs as the logged-in user and
    checks the scope first (see the module docstring)."""
    _name = "distri.account.app"
    _description = "Cuentas (celular)"

    # ------------------------------------------------------------ scope

    @api.model
    def _app_check(self):
        scope = app_scope(self.env)
        if not scope:
            raise AccessError(APP_DENIED)
        return scope

    @api.model
    def _app_hides_distributors(self):
        """Staff without Administración (sellers and drivers) never reach a
        wholesale customer. 19.0.1.115.0 (owner): the drivers too, and
        «wholesale» is the whole rule of lista_oculta._distri_es_distribuidora
        (lista oculta, «Es mayorista», a wholesale price list)."""
        return self._app_check() != "all" and not ve_mayoristas(self.env.user)

    @api.model
    def _app_chosen_ids(self):
        """19.0.1.129.20: None for everybody but an external seller; for him,
        the ids of his customers (see the module docstring), the only
        accounts he reaches."""
        if self._app_check() == "all":
            return None
        return self.env.user._distri_customer_ids_limit()

    @api.model
    def _app_allowed(self, partner, chosen_ids=False):
        """Whether the user may see this commercial partner's account.
        `chosen_ids` is `_app_chosen_ids()` when the caller already has it
        (one query for many partners); False computes it here."""
        if partner.distri_generic_partner or (
                self._app_hides_distributors() and partner._distri_es_distribuidora()):
            return False
        if chosen_ids is False:
            chosen_ids = self._app_chosen_ids()
        return chosen_ids is None or partner.commercial_partner_id.id in chosen_ids

    @api.model
    def _app_customers(self):
        """The direct customers the user may review (see the module
        docstring)."""
        self._app_check()
        # 19.0.1.120.0 (owner): administración and gerencia see the wholesale
        # customers and their balance here too; until then the debtors list
        # dropped them for everybody (deudores.direct_customers)
        hides = self._app_hides_distributors()
        customers = direct_customers(self.env, wholesale=ve_mayoristas(self.env.user))
        if hides:
            customers = customers.filtered(lambda p: not p._distri_es_distribuidora())
        chosen_ids = self._app_chosen_ids()
        if chosen_ids is not None:
            customers = customers.filtered(lambda p: p.id in chosen_ids)
        return customers

    @api.model
    def _app_partner(self, partner_id):
        """The commercial partner, with sudo, when the user may see it
        (`_app_allowed`); AccessError otherwise."""
        self._app_check()
        try:
            wanted = int(partner_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        Partner = self.env["res.partner"].sudo().with_context(active_test=False)
        partner = Partner.browse(wanted).exists() if wanted > 0 else Partner
        if not partner:
            raise AccessError(PARTNER_DENIED)
        commercial = partner.commercial_partner_id
        if not self._app_allowed(commercial):
            raise AccessError(PARTNER_DENIED)
        return commercial

    def _app_comment_counts(self, partners):
        Comment = self.env["distri.account.comment"].sudo()
        data = Comment._read_group([("partner_id", "in", partners.ids)], ["partner_id"],
                                   ["__count"])
        return {partner.id: count for partner, count in data}

    # ------------------------------------------------------------ debtors

    @api.model
    def app_debtors(self, seller="all", settled=False, zone="", min_balance=0, min_age=0):
        """The debtors screen: every customer of the scope with its balance,
        highest debt first, the uncollectible ones and, when `settled` is
        asked (the «saldos a favor» toggle), the settled ones (zero or in
        credit) apart: they are most of the customers and most of the bytes.
        19.0.1.112.0: `zone`, `min_balance` and `min_age` (filter_debtors)."""
        scope = self._app_check()
        customers = self._app_customers()
        if scope != "all" or seller not in SELLER_FILTER_VALUES:
            seller = "all"
        entries = debtor_entries(self.env, customers, include_settled=True)
        if seller != "all":
            entries = [e for e in entries if e["seller"] == seller]
        zones = sorted({e["zone"] for e in entries if e["zone"]})
        today = today_ar()
        entries = filter_debtors(entries, zone, min_balance, min_age, today)
        counts = self._app_comment_counts(customers)
        sellers = dict(SELLERS)

        def payload(entry, rank):
            partner = entry["partner"]
            return {
                "id": partner.id,
                "rank": rank,
                "name": partner.name or "",
                "ref": partner.ref or "",
                "phone": entry["phone"],
                "zone": entry["zone"] or "",
                "seller": entry["seller"],
                "seller_label": sellers.get(entry["seller"], ""),
                "balance": entry["balance"],
                "last_payment_date": _iso(entry["last_payment"] and entry["last_payment"][0]),
                "last_payment_amount": entry["last_payment"] and entry["last_payment"][1] or 0.0,
                "last_invoice_date": _iso(entry["last_invoice"] and entry["last_invoice"][0]),
                "last_invoice_amount": entry["last_invoice"] and entry["last_invoice"][1] or 0.0,
                "candidate_count": entry["candidate_count"],
                "candidate_amount": entry["candidate_amount"],
                "comment_count": counts.get(partner.id, 0),
                # 19.0.1.112.0: «Deuda más vieja» (the oldest red invoice, FIFO)
                "oldest_owed_date": _iso(entry.get("oldest_owed")),
                "oldest_owed_days": (today - entry["oldest_owed"]).days
                if entry.get("oldest_owed") else None,
            }

        # 12/11/2026: a seller must not learn that the «incobrable» state
        # exists (so he never pushes his debts there): his uncollectible
        # customers are listed as ordinary debtors, in the same balance order
        hide_uncollectible = scope != "all"
        groups = {"debtor": [], "uncollectible": [], "settled": []}
        for entry in entries:
            kind = entry["kind"]
            if hide_uncollectible and kind == "uncollectible":
                kind = "debtor"
            bucket = groups[kind]
            bucket.append(payload(entry, len(bucket) + 1))
        debtors = groups["debtor"]
        return {
            "user": self.env.user.name,
            "scope": scope,
            "can_filter_seller": scope == "all",
            "can_see_uncollectible": not hide_uncollectible,
            # 19.0.1.129.8: «Transferencias» (models/cuentas_transferencias.py);
            # the API checks the same groups again
            "can_see_transfers": self._app_can_see_transfers(),
            "seller": seller,
            "zone": (zone or "").strip(),
            "zones": zones,
            "min_balance": _number(min_balance),
            "min_age": int(_number(min_age)),
            "total_candidates": round(sum(d["candidate_amount"] for d in debtors), 2),
            # «Deudores» of the backend: PDF and Excel (administración)
            "report_url": "/odoo/action-distri_pedido.action_debtor_report_open"
            if scope == "all" else "",
            "debtors": debtors,
            "uncollectible": groups["uncollectible"],
            "settled": groups["settled"] if settled else [],
            "settled_loaded": bool(settled),
            "total_debt": round(sum(d["balance"] for d in debtors), 2),
            "debtor_count": len(debtors),
            "uncollectible_total": round(sum(d["balance"] for d in groups["uncollectible"]), 2),
            "last_load": self._app_last_load(),
        }

    # ------------------------------------------------------------ one list (129.43)

    @api.model
    def _app_candidates(self, ids):
        """{commercial partner id: [count, amount]} of the Mercado Pago
        receipt candidates still to confirm: debtor_balances' rule, no cut."""
        wanted = set(ids)
        out = {}
        if not wanted:
            return out
        cards = self.env["distri.mp.collection"].sudo().search([
            ("partner_id", "child_of", sorted(wanted)), ("kind", "=", "income"),
            ("state", "=", "draft"), ("receipt_candidate", "=", True)])
        for card in cards:
            pid = card.partner_id.commercial_partner_id.id
            if pid in wanted:
                count, amount = out.get(pid, (0, 0.0))
                out[pid] = (count + 1, amount + card.amount)
        return out

    @api.model
    def app_list(self, q="", seller="all", zone="", min_balance=0, min_age=0, state="all",
                 sort="debt", offset=0, limit=LIST_PAGE, with_zones=False):
        """19.0.1.129.43 (owner, 25/11): the ONE list of every customer of the
        scope, highest debt first, then the zero balances, then the credit
        balances, by pages. Same customers, scope and balances as
        app_debtors, but:

        - the balances of the whole scope come from one batched read
          (res.partner._distri_account_balances, the statement's rule, the
          same numbers as debtor_balances) and only the page gets the
          details (last payment and invoice, oldest red row, candidates,
          comments) through debtor_balances;
        - the totals are over every customer of the filters (seller, zone,
          minimum balance, age), never over the page nor the search: the
          total is app_debtors' total_debt;
        - `q` searches on the server (name, ref., CUIT, WhatsApp or phone);
          `state` keeps one kind (debt, zero, credit, uncollectible) and
          `sort` orders by debt (default), oldest red row or name. None of
          them is on by default;
        - `limit` 0 answers only the counts (the filter sheet's «Ver N»).

        Only `min_age` and `sort=oldest` read the details of every debtor
        (the oldest red row is not in the batched balance)."""
        scope = self._app_check()
        hide_uncollectible = scope != "all"
        if scope != "all" or seller not in SELLER_FILTER_VALUES:
            seller = "all"
        state = state if state in LIST_STATES else "all"
        if hide_uncollectible and state == "uncollectible":
            state = "all"
        sort = sort if sort in LIST_SORTS else "debt"
        offset = _int(offset)
        limit = _int(limit, LIST_PAGE, 0, LIST_MAX)
        zone = (zone or "").strip()
        min_balance = _number(min_balance)
        min_age = int(_number(min_age))
        today = today_ar()

        customers = self._app_customers()
        balances = self.env["res.partner"].sudo()._distri_account_balances(customers.ids)
        tags = seller_tags(self.env)
        zone_names = {normalizar_ciudad(z.name): z.name
                      for z in self.env["distri.zona"].sudo().search([])}

        def zone_of(partner):
            # debtor_entries' rule: the city, else the town in the name
            town = partner.city or town_from_name(partner.name) or ""
            return zone_names.get(normalizar_ciudad(town), town)

        items, zones = [], set()
        for partner in customers:
            raw = balances.get(partner.id, 0.0)
            balance = round(raw, 2)
            if seller != "all" and seller_of(partner, tags) != seller:
                continue
            item = {"partner": partner, "name": partner.name or "", "balance": balance}
            if zone or with_zones:
                item["zone"] = zone_of(partner)
                if item["zone"]:
                    zones.add(item["zone"])
                if zone and item["zone"] != zone:
                    continue
            if min_balance and balance < min_balance:
                continue
            # app_debtors' kinds: an uncollectible customer apart for
            # administración; a seller never learns the state exists
            if partner.distri_uncollectible:
                item["kind"] = "debtor" if hide_uncollectible else "uncollectible"
            else:
                item["kind"] = "debtor" if raw > DEBT_EPSILON else "settled"
            item["state"] = ("uncollectible" if item["kind"] == "uncollectible"
                             else balance_state(raw))
            items.append(item)

        oldest = {}
        if min_age or sort == "oldest":
            # only a positive balance has red rows (mark_owed_fifo)
            owing = customers.browse([i["partner"].id for i in items if i["balance"] > 0])
            oldest = {pid: data["oldest_owed"]
                      for pid, data in debtor_balances(self.env, owing).items()}
            if min_age:
                items = [i for i in items if oldest.get(i["partner"].id)
                         and (today - oldest[i["partner"].id]).days >= min_age]

        debtors = [i for i in items if i["kind"] == "debtor"]
        uncollectible = [i for i in items if i["kind"] == "uncollectible"]
        credit = [i for i in items if i["state"] == "credit"]
        count_only = limit == 0

        if sort == "name":
            items.sort(key=lambda i: (_fold(i["name"]), i["partner"].id))
        elif sort == "oldest":
            items.sort(key=lambda i: (oldest.get(i["partner"].id) is None,
                                      oldest.get(i["partner"].id) or date.max,
                                      -i["balance"], i["name"]))
        else:
            # debtor_entries' order: highest balance first, then the name
            items.sort(key=lambda i: (-i["balance"], i["name"]))
        for rank, item in enumerate(items, 1):
            item["rank"] = rank
        filtered_count = len(items)

        words = _fold(q).split()
        if words:
            digits = _digits(q)

            def found(partner):
                text = _fold(" ".join(filter(None, (partner.name, partner.ref, partner.vat))))
                if all(word in text for word in words):
                    return True
                return len(digits) >= 3 and any(
                    digits in _digits(value)
                    for value in (partner.vat, partner.distri_whatsapp, partner.phone))

            items = [i for i in items if found(i["partner"])]
        counts = dict.fromkeys(LIST_STATES, 0)
        counts["all"] = len(items)
        for item in items:
            counts[item["state"]] += 1
        shown = items if state == "all" else [i for i in items if i["state"] == state]
        page = [] if count_only else shown[offset:offset + limit]

        result = {
            "scope": scope,
            "q": " ".join(words),
            "seller": seller,
            "zone": zone,
            "min_balance": min_balance,
            "min_age": min_age,
            "state": state,
            "sort": sort,
            "counts": counts,
            "total_rows": len(shown),
            "offset": offset,
            "customer_count": filtered_count,
            "total_debt": round(sum(i["balance"] for i in debtors), 2),
            "debtor_count": len(debtors),
            "uncollectible_total": round(sum(i["balance"] for i in uncollectible), 2),
            "uncollectible_count": len(uncollectible),
            "credit_total": round(sum(i["balance"] for i in credit), 2),
            "credit_count": len(credit),
        }
        if count_only:
            return result
        candidates = self._app_candidates([i["partner"].id for i in debtors])
        result.update({
            "user": self.env.user.name,
            "can_filter_seller": scope == "all",
            "can_see_uncollectible": not hide_uncollectible,
            "can_see_transfers": self._app_can_see_transfers(),
            # «Descargar PDF / Excel»: the «Deudores» report (administración)
            "can_export": self.env.user.has_group(ADMIN_GROUP),
            "zones": sorted(zones) if with_zones else None,
            "total_candidates": round(sum(candidates.get(i["partner"].id, (0, 0.0))[1]
                                          for i in debtors), 2),
            "rows": self._app_list_rows(page, zone_of, tags, today),
            "more": offset + len(page) < len(shown),
            "updated_at": fields.Datetime.now().replace(tzinfo=timezone.utc).astimezone(
                AR_TZ).strftime("%H:%M"),
            "last_load": self._app_last_load() if offset == 0 else None,
        })
        return result

    @api.model
    def _app_list_rows(self, items, zone_of, tags, today):
        """The payload of one page of app_list: app_debtors' row (same keys)
        plus `state`, with the details read for these customers only."""
        if not items:
            return []
        partners = items[0]["partner"].browse([i["partner"].id for i in items])
        details = debtor_balances(self.env, partners)
        counts = self._app_comment_counts(partners)
        sellers = dict(SELLERS)
        rows = []
        for item in items:
            partner = item["partner"]
            data = details[partner.id]
            seller = seller_of(partner, tags)
            last_payment, last_invoice = data["last_payment"], data["last_invoice"]
            rows.append({
                "id": partner.id,
                "rank": item["rank"],
                "name": partner.name or "",
                "ref": partner.ref or "",
                "phone": partner.distri_whatsapp or partner.phone or "",
                "zone": item.get("zone", zone_of(partner)) or "",
                "seller": seller,
                "seller_label": sellers.get(seller, ""),
                "balance": item["balance"],
                "state": item["state"],
                "last_payment_date": _iso(last_payment and last_payment[0]),
                "last_payment_amount": last_payment and last_payment[1] or 0.0,
                "last_invoice_date": _iso(last_invoice and last_invoice[0]),
                "last_invoice_amount": last_invoice and last_invoice[1] or 0.0,
                "candidate_count": data["candidate_count"],
                "candidate_amount": data["candidate_amount"],
                "comment_count": counts.get(partner.id, 0),
                "oldest_owed_date": _iso(data["oldest_owed"]),
                "oldest_owed_days": (today - data["oldest_owed"]).days
                if data["oldest_owed"] else None,
            })
        return rows

    @api.model
    def _app_last_load(self):
        """Up to when the WhatsApp receipts are loaded: the newest receipt that
        came in an imported chat zip, and that import (read only, no amounts)."""
        Message = self.env["distri.whatsapp.message"].sudo()
        last = Message.search([("distri_chat_import_id", "!=", False), ("distri_is_receipt", "=", True)],
                              order="timestamp desc, id desc", limit=1)
        if not last:
            return False
        chat = self.env["distri.whatsapp.chat.import"].sudo().search([], limit=1)  # _order id desc
        return {
            "receipts_until": _local_text(last.timestamp),
            "import_file": chat.name or "",
            "imported_at": _local_text(chat.imported_at),
            "imported_by": chat.imported_by_id.name or "",
        }

    # ------------------------------------------------------------ account

    @api.model
    def _app_period_start(self, period):
        months = PERIODS.get(period, PERIODS[DEFAULT_PERIOD])
        return (today_ar() - relativedelta(months=months)) if months else False

    @api.model
    def _app_range(self, period, date_from=None, date_to=None):
        """(period, date_from, date_to): a quick period runs up to today;
        «custom» (19.0.1.112.0) is the range typed, either end open."""
        if period == CUSTOM_PERIOD:
            start, end = _parse_day(date_from), _parse_day(date_to)
            if start and end and start > end:
                raise UserError("La fecha «Desde» es posterior a «Hasta».")
            if not start and not end:
                return "all", False, False
            return CUSTOM_PERIOD, start, end
        if period not in PERIODS:
            period = DEFAULT_PERIOD
        return period, self._app_period_start(period), False

    @api.model
    def _app_row(self, row, counts):
        return {
            "line_key": row["line_key"],
            "date": _iso(row["date"]),
            "source": row["source"],
            "source_label": SOURCE_LABELS.get(row["source"], ""),
            "doc_type": row["doc_type"],
            "number": row["number"],
            "remito": row["remito"],
            "concept": row["concept"],
            "debit": row["debit"] or 0.0,
            "credit": row["credit"] or 0.0,
            "balance": row["balance"],
            "provisional": row["provisional"],
            "owed": row.get("owed") or 0.0,
            "review_note": row.get("review_note") or "",
            "res_model": row["res_model"],
            "res_id": row["res_id"],
            "card_id": row.get("card_id") or False,
            # 19.0.1.126.0: a credited receipt tied to the Mercado Pago report
            "confirmed": bool(row.get("confirmed")),
            "comment_count": counts.get(row["line_key"], 0),
        }

    @api.model
    def app_account(self, partner_id, period=DEFAULT_PERIOD, offset=0, limit=PAGE_SIZE,
                    date_from=None, date_to=None):
        """The account screen: header, balances, account comments and one
        page of the statement rows of the period, newest first.
        19.0.1.112.0: a free range (`period` «custom»), the period's totals,
        what may be shared and the open «Comprobantes pendientes»."""
        partner = self._app_partner(partner_id)
        full = self._app_check() == "all"
        period, date_from, date_to = self._app_range(period, date_from, date_to)
        data = partner._distri_statement(date_from, date_to)
        comments = self.env["distri.account.comment"].sudo().search(
            [("partner_id", "=", partner.id)])
        counts = {}
        for comment in comments.filtered("line_key"):
            counts[comment.line_key] = counts.get(comment.line_key, 0) + 1
        rows = list(reversed(data["rows"]))
        offset = max(int(offset or 0), 0)
        limit = min(max(int(limit or PAGE_SIZE), 1), 500)
        page = rows[offset:offset + limit]
        result = {
            "period": period,
            "date_from": _iso(date_from),
            "date_to": _iso(date_to),
            "offset": offset,
            "row_count": len(rows),
            "more": offset + limit < len(rows),
            "rows": [self._app_row(row, counts) for row in page],
            "opening": data["opening"],
            # the period's totals (the PDF's «Totales del período»)
            "debit": round(data["debit"], 2),
            "credit": round(data["credit"], 2),
            "closing": data["closing"],
        }
        if offset:
            return result
        street = ", ".join(x for x in (partner.street, partner.street2) if x)
        town = ", ".join(x for x in (partner.city, partner.state_id.name) if x)
        entry = next(iter(debtor_entries(self.env, partner, include_settled=True)), None)
        phone = partner.distri_whatsapp or partner.phone or ""
        result.update({
            "partner": {
                "id": partner.id,
                "name": partner.name or "",
                "ref": partner.ref or "",
                "address": " · ".join(x for x in (street, town) if x),
                "zone": (entry and entry["zone"]) or "",
                "seller": dict(SELLERS).get(seller_of(partner, seller_tags(self.env)), ""),
                "phone": phone,
                "whatsapp": "".join(ch for ch in (partner.distri_whatsapp or partner.phone or "")
                                    if ch.isdigit()),
                # a seller never sees the «incobrable» state (see app_debtors)
                "uncollectible": full and partner.distri_uncollectible,
                "uncollectible_note": (full and partner.distri_uncollectible_note) or "",
            },
            "balance": data["balance"],
            "provisional": data["provisional"],
            "provisional_note": PROVISIONAL_NOTE,
            "candidates": [{
                "date": _iso(c["date"]), "name": c["name"], "amount": c["amount"],
                "note": c["note"], "has_receipt": c["has_receipt"], "card_id": c["res_id"],
            } for c in data["candidates"]],
            "candidate_amount": data["candidate_amount"],
            "whatif_balance": data["whatif_balance"],
            # over the whole statement, not only the period (19.0.1.99.0, mark_owed)
            "owed_count": data["owed_count"],
            "owed_total": data["owed_total"],
            "comments": comments.filtered(lambda c: not c.line_key)._app_payload(),
            "line_comment_count": len(comments.filtered("line_key")),
            "line_comments": comments.filtered("line_key")._app_payload(),
            "share": self._app_share(partner, date_from, date_to),
            "pending_receipts": self._app_pending_receipts(partner),
        })
        return result

    # ------------------------------------------------------------ share

    @api.model
    def _app_print_denied(self, partner):
        """Why the user may not print this customer's statement, or "": the
        statement wizard's own checks (vendedor, administración, gerencia;
        a seller only for his portfolio, audit F9)."""
        Wizard = self.env["distri.estado.cuenta"]
        try:
            Wizard._check_access_saldos()
            Wizard.new({"partner_id": partner.id})._check_partner_visible()
        except (AccessError, UserError) as exc:
            return str(exc.args[0] if exc.args else exc)
        return ""

    @api.model
    def _app_period_text(self, date_from, date_to):
        if date_from and date_to:
            return "del %s al %s" % (_day_text(date_from), _day_text(date_to))
        if date_from:
            return "desde el %s" % _day_text(date_from)
        if date_to:
            return "hasta el %s" % _day_text(date_to)
        return ""

    @api.model
    def _app_share(self, partner, date_from, date_to):
        """«Compartir PDF» and «Por WhatsApp» of the account: allowed or
        why not, and the WhatsApp link with the public statement link."""
        denied = self._app_print_denied(partner)
        share = {"pdf": not denied, "pdf_reason": denied, "whatsapp_url": "",
                 "whatsapp_reason": denied, "link": "", "expires": ""}
        if denied:
            return share
        digits = wa_digits(partner.distri_whatsapp or partner.phone or "")
        if not partner._distri_account_visible():
            share["whatsapp_reason"] = SHARE_HIDDEN
        elif not digits:
            share["whatsapp_reason"] = SHARE_NO_WHATSAPP
        else:
            from ..controllers.main import FICHA
            token, expires = share_token(self.env, partner, date_from, date_to)
            link = "%s/estado-de-cuenta/%s" % (partner.get_base_url(), token)
            period = self._app_period_text(date_from, date_to)
            text = ("Hola! Te paso el estado de cuenta de %s en %s%s: %s\n"
                    "El link es personal y vence el %s.") % (
                partner.name or "", FICHA["nombre"], (" " + period) if period else "", link,
                _day_text(expires.date()))
            share.update({
                "whatsapp_url": "https://wa.me/%s?text=%s" % (digits, quote(text)),
                "whatsapp_reason": "",
                "link": link,
                "expires": _day_text(expires.date()),
            })
        return share

    @api.model
    def _app_pending_receipts(self, partner):
        """The customer's open «Comprobantes pendientes» (transfers without
        their receipt, models/comprobante_pendiente.py), oldest first, and
        «Reclamar por WhatsApp» with the company's claim of all of them."""
        Promise = self.env["distri.promised.transfer"].sudo()
        items = Promise.search([("partner_id", "child_of", partner.id),
                                ("state", "in", OPEN_STATES)], order="captured_at, id")
        kinds = dict(Promise._fields["kind"].selection)
        digits = wa_digits(partner.distri_whatsapp or partner.phone or "")
        claim = Promise._distri_customer_claim(items, voice="company") if items else ""
        return {
            "items": [{
                "id": item.id,
                "order": item.order_id.name or "",
                "kind": kinds.get(item.kind or "done", ""),
                "amount": item.amount,
                "delivered": _day_text(item.delivered_day),
                "status": item.status_label or "",
                "overdue": item.state == "overdue",
            } for item in items],
            "total": round(sum(items.mapped("amount")), 2),
            "claim_url": "https://wa.me/%s?text=%s" % (digits, quote(claim))
            if items and digits and claim else "",
        }

    @api.model
    def app_statement_pdf(self, partner_id, period=DEFAULT_PERIOD, date_from=None,
                          date_to=None):
        """(pdf bytes, file name) of the existing statement report for the
        range, printed through the statement wizard as the user (its checks
        decide, see _app_print_denied)."""
        partner = self._app_partner(partner_id)
        denied = self._app_print_denied(partner)
        if denied:
            raise AccessError(denied)
        _period, start, end = self._app_range(period, date_from, date_to)
        wizard = self.env["distri.estado.cuenta"].create(
            {"partner_id": partner.id, "date_from": start, "date_to": end})
        pdf, _kind = self.env["ir.actions.report"]._render_qweb_pdf(
            "distri_pedido.report_estado_cuenta", wizard.ids)
        name = "Estado de cuenta - %s - %s.pdf" % (
            (partner.name or "cliente").replace("/", "-"), today_ar().strftime("%Y-%m-%d"))
        return pdf, name

    # ------------------------------------------------------------ one row

    @api.model
    def _app_find_row(self, partner, line_key):
        for row in partner._distri_statement_rows():
            if row["line_key"] == line_key:
                return row
        raise UserError("Ese movimiento ya no figura en el estado de cuenta.")

    @api.model
    def app_line(self, partner_id, line_key):
        """Every field of one statement row plus what it links to: the card
        and receipt of a Mercado Pago payment, the memo of a cash payment
        and its cash sheet, the Online history record."""
        partner = self._app_partner(partner_id)
        row = self._app_find_row(partner, line_key or "")
        comments = self.env["distri.account.comment"].sudo().search(
            [("partner_id", "=", partner.id), ("line_key", "=", row["line_key"])])
        detail = self._app_row(row, {row["line_key"]: len(comments)})
        detail["comments"] = comments._app_payload()
        detail["label"] = self._app_line_label(row)
        if row["res_model"] == "account.move":
            move = self.env["account.move"].sudo().browse(row["res_id"])
            detail["journal"] = move.journal_id.name or ""
            detail["journal_type"] = move.journal_id.type or ""
            detail["ref"] = move.ref or ""
            if row.get("card_id"):
                detail["card"] = self._app_card(partner, row["card_id"])
            elif row["credit"] and move.journal_id.type == "cash":
                sheet = self._app_cash_sheet(move)
                detail["cash"] = {"memo": move.ref or row["concept"], "sheet": sheet}
        elif row["res_model"] == "distri.cuenta.movimiento":
            mov = self.env["distri.cuenta.movimiento"].sudo().browse(row["res_id"])
            detail["online"] = {
                "comprobante": mov.comprobante or "",
                "concepto": mov.concepto or "",
                "tipo": dict(mov._fields["tipo"].selection).get(mov.tipo, ""),
                "origen_id": mov.origen_id or False,
            }
        elif row["res_model"] == "distri.route.return":
            ret = self.env["distri.route.return"].sudo().browse(row["res_id"])
            detail["route_return"] = {
                "order": ret.order_id.name or "",
                "route": ret.route_id.name or "",
                "product": ret.product_id.display_name or "",
                "quantity": ret.quantity,
                "reason": ret.reason or "",
            }
        return detail

    @api.model
    def _app_line_label(self, row):
        day = row["date"].strftime("%d/%m/%Y") if row["date"] else ""
        return " · ".join(x for x in (day, row["doc_type"], row["number"]) if x)

    @api.model
    def _app_visible_partner_ids(self, partners):
        """The ids of `partners` the user may see (`_app_allowed`)."""
        if self._app_check() == "all":
            return set(partners.ids)
        chosen_ids = self._app_chosen_ids()
        return {p.id for p in partners.sudo()
                if self._app_allowed(p.commercial_partner_id, chosen_ids)}

    @api.model
    def _app_sheet_files(self, sheet, visible):
        """The originals of a sheet, only for a user who sees every row of it
        (a photo or an xlsx shows the whole sheet)."""
        # 19.0.1.92.0: only a row of a customer he may not see (a distributor
        # for a plain seller) hides the originals
        if any(l.partner_id and l.partner_id.id not in visible for l in sheet.line_ids):
            return []
        files = []
        for attachment in sheet.attachment_ids.sorted(lambda a: (a.mimetype or "", a.id)):
            mimetype = attachment.mimetype or ""
            kind = "image" if mimetype.startswith("image/") and mimetype != "image/svg+xml" else \
                "xlsx" if (attachment.name or "").lower().endswith(".xlsx") else "file"
            files.append({
                "id": attachment.id,
                "name": attachment.name or "",
                "kind": kind,
                "url": "/cuentas/api/sheet_file?sheet=%d&file=%d" % (sheet.id, attachment.id),
            })
        return files

    @api.model
    def _app_cash_sheet(self, move):
        """The cash sheet («planilla de rendición») a cash payment comes from
        (models/cash_sheet.py): collector, date, its rows with the paired one
        marked, the total and its originals. 19.0.1.92.0: sellers and drivers
        see the whole sheet, except the rows of customers they may not see
        (`_app_allowed`), which are only counted, with no name or amount; the
        originals are then not offered. None when the payment is on no sheet."""
        line = self.env["distri.cash.sheet.line"].sudo().search(
            [("payment_id.move_id", "=", move.id)], limit=1)
        if not line:
            return None
        sheet = line.sheet_id
        visible = self._app_visible_partner_ids(sheet.line_ids.partner_id)
        rows, hidden = [], 0
        for item in sheet.line_ids:
            if item.partner_id and item.partner_id.id not in visible:
                hidden += 1
                continue
            rows.append({
                "id": item.id,
                "client": item.client_raw or item.partner_id.name or "",
                "invoice": item.invoice_ref or "",
                "invoiced": item.amount_invoiced or 0.0,
                "paid": item.amount_paid or 0.0,
                "method": item.method or "",
                "kind": item.row_kind or "cliente",
                "kind_label": dict(item._fields["row_kind"].selection).get(item.row_kind, "")
                if item.row_kind != "cliente" else "",
                "after_total": item.after_total,
                "note": item.notes or "",
                "linked": bool(item.payment_id),
                "hit": item == line,
            })
        counted = [r for r in rows if r["kind"] == "cliente" and not r["after_total"]]
        return {
            "id": sheet.id,
            "name": sheet.name or "",
            "collector": sheet.collector_label or "",
            "date": _iso(sheet.date),
            "origin": sheet.origin,
            "origin_label": dict(sheet._fields["origin"].selection).get(sheet.origin, ""),
            "rows": rows,
            "hidden_count": hidden,
            "total": round(sum(r["paid"] for r in counted), 2),
            "total_label": "Total cobrado" if not hidden else "Total de los clientes que ves",
            "files": self._app_sheet_files(sheet, visible),
            "note": sheet.note or "",
        }

    @api.model
    def app_sheet_file(self, sheet_id, file_id):
        """(bytes, mimetype, filename) of one original of a cash sheet, under
        the same rule as `_app_sheet_files`; AccessError otherwise."""
        self._app_check()
        try:
            sheet = self.env["distri.cash.sheet"].sudo().browse(int(sheet_id or 0)).exists()
            wanted = int(file_id or 0)
        except (TypeError, ValueError):
            sheet, wanted = self.env["distri.cash.sheet"].sudo(), 0
        if not sheet:
            raise AccessError("No se encontró la planilla.")
        visible = self._app_visible_partner_ids(sheet.line_ids.partner_id)
        allowed = {f["id"] for f in self._app_sheet_files(sheet, visible)}
        attachment = sheet.attachment_ids.filtered(lambda a: a.id == wanted and a.id in allowed)
        if not attachment:
            raise AccessError("No podés ver ese archivo de la planilla.")
        attachment = attachment.sudo()
        return attachment.raw, attachment.mimetype or "application/octet-stream", \
            attachment.name or "planilla"

    @api.model
    def _app_card_for(self, partner, card_id):
        """The Mercado Pago card, only when it is one of `partner`'s own:
        the card of one of its payments or one of its pending candidates."""
        card = self.env["distri.mp.collection"].sudo().browse(int(card_id or 0)).exists()
        if not card:
            return card
        owners = card.partner_id.commercial_partner_id | \
            card.payment_id.partner_id.commercial_partner_id
        return card if partner in owners else card.browse()

    @api.model
    def _app_card(self, partner, card_id):
        card = self._app_card_for(partner, card_id)
        if not card:
            return False
        return {
            "id": card.id,
            "name": card.name or "",
            "operation": card.source_id or "",
            "date": _local_text(card.date),
            "amount": card.amount,
            "payer_name": card.payer_name or "",
            "state": CARD_STATES.get(card.state, card.state or ""),
            "partner": card.partner_id.name or "",
            "has_receipt": bool(card.receipt_id and card.receipt_id.attachment_id),
            "receipt_is_image": bool(card.receipt_is_image),
        }

    @api.model
    def app_receipt(self, partner_id, card_id):
        """(bytes, mimetype, filename) of the receipt of one of the partner's
        cards; AccessError when the card is not his."""
        partner = self._app_partner(partner_id)
        card = self._app_card_for(partner, card_id)
        attachment = card.receipt_id.attachment_id.sudo() if card else False
        if not attachment:
            raise AccessError("No hay comprobante para ese cobro.")
        return attachment.raw, attachment.mimetype or "application/octet-stream", \
            attachment.name or "comprobante"

    # ------------------------------------------------------------ comments

    @api.model
    def app_add_comment(self, partner_id, body, line_key=""):
        """The only write of the app. 19.0.1.92.0: with sudo once
        `_app_partner` accepted the customer (sellers and drivers reach every
        account, beyond the comment record rules); the author is still the
        user (sudo keeps the uid)."""
        partner = self._app_partner(partner_id)
        label = ""
        if line_key:
            label = self._app_line_label(self._app_find_row(partner, line_key))
        comment = self.env["distri.account.comment"].sudo().create({
            "partner_id": partner.id,
            "line_key": line_key or False,
            "line_label": label,
            "body": body or "",
        })
        return comment._app_payload()[0]

    @api.model
    def app_delete_comment(self, comment_id):
        self._app_check()
        comment = self.env["distri.account.comment"].sudo().browse(int(comment_id or 0)).exists()
        if not comment or not self._app_allowed(comment.partner_id):
            raise AccessError("No se encontró el comentario.")
        # only its author, during the first hour (same rule as unlink)
        if not comment._can_delete():
            raise AccessError("Solo el autor borra su comentario, durante la primera hora.")
        comment.unlink()
        return True
