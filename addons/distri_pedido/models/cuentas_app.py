"""«Cuentas (celular)»: the phone app to review customer accounts
(19.0.1.74.0, controllers/cuentas.py, static/src/cuentas/).

Read-only: it shows debtors, statements, the Mercado Pago card and receipt
of a payment and the memo and cash sheet of a cash payment (19.0.1.75.0,
models/cash_sheet.py; a seller sees only his customers' rows of a sheet). It authorizes nothing: no
payment, card or adjustment action is reachable from it. The only write is
a comment on an account or on one statement row (distri.account.comment).

Who opens it (app_scope):
- administración (gerencia implies it) and Settings: every customer;
- vendedor: only the customers of his portfolio. The scope is the set of
  partners the user can read under the record rules (security/reglas.xml,
  «Vendedor: solo los clientes de su cartera»), searched as the user, never
  with sudo. Only after a partner id is verified to be in that set are its
  balance and statement computed with sudo;
- anyone else: refused.

The debtor list and the statement reuse the logic of «Deudores» and of the
account statement (models/deudores.py debtor_entries, models/estado_cuenta.py
_distri_statement), computed live.
"""
from datetime import timedelta, timezone

from dateutil.relativedelta import relativedelta

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .deudores import ADMIN_GROUP, SELLERS, debtor_entries, direct_customers, seller_of, \
    seller_tags
from .estado_cuenta import PROVISIONAL_NOTE, SOURCE_LABELS
from .reparto_cliente import AR_TZ, today_ar

SELLER_GROUP = "distri_pedido.grupo_vendedor"
SYSTEM_GROUP = "base.group_system"
APP_DENIED = "Esta pantalla es para administración, gerencia y vendedores."
PARTNER_DENIED = "Ese cliente no está en tu cartera."
# a comment can be deleted by its author during this time
COMMENT_DELETE_WINDOW = timedelta(hours=1)
COMMENT_MAX_LENGTH = 2000
PERIODS = {"1m": 1, "3m": 3, "all": None}
DEFAULT_PERIOD = "3m"
PAGE_SIZE = 50
SELLER_FILTER_VALUES = ("all", "italo", "distrineuquen")
CARD_STATES = {"draft": "A revisar", "confirmed": "Confirmado", "ignored": "Descartado"}


def app_scope(env):
    """'all' (administración, gerencia, Settings), 'portfolio' (vendedor) or
    None (no access)."""
    user = env.user
    if user.share:
        return None
    if user.has_group(ADMIN_GROUP) or user.has_group(SYSTEM_GROUP):
        return "all"
    if user.has_group(SELLER_GROUP):
        return "portfolio"
    return None


def _iso(day):
    return day.isoformat() if day else ""


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
    def _app_customers(self):
        """The direct customers the user may review: all of them for
        administración, those the record rules let him read for a seller."""
        scope = self._app_check()
        customers = direct_customers(self.env)
        if scope == "all":
            return customers
        visible = self.env["res.partner"].with_context(active_test=False).search(
            [("id", "in", customers.ids)])
        allowed = set(visible.ids)
        return customers.filtered(lambda p: p.id in allowed)

    @api.model
    def _app_partner(self, partner_id):
        """The commercial partner, with sudo, only when the user can read it
        under the record rules; AccessError otherwise."""
        self._app_check()
        try:
            wanted = int(partner_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        Partner = self.env["res.partner"].with_context(active_test=False)
        partner = Partner.search([("id", "=", wanted)]) if wanted > 0 else Partner
        if not partner:
            raise AccessError(PARTNER_DENIED)
        commercial = partner.sudo().commercial_partner_id
        if commercial != partner.sudo() and not Partner.search([("id", "=", commercial.id)]):
            raise AccessError(PARTNER_DENIED)
        if commercial.distri_generic_partner:
            raise AccessError(PARTNER_DENIED)
        return commercial.sudo()

    def _app_comment_counts(self, partners):
        Comment = self.env["distri.account.comment"].sudo()
        data = Comment._read_group([("partner_id", "in", partners.ids)], ["partner_id"],
                                   ["__count"])
        return {partner.id: count for partner, count in data}

    # ------------------------------------------------------------ debtors

    @api.model
    def app_debtors(self, seller="all", settled=False):
        """The debtors screen: every customer of the scope with its balance,
        highest debt first, the uncollectible ones and, when `settled` is
        asked (the «saldos a favor» toggle), the settled ones (zero or in
        credit) apart: they are most of the customers and most of the bytes."""
        scope = self._app_check()
        customers = self._app_customers()
        if scope != "all" or seller not in SELLER_FILTER_VALUES:
            seller = "all"
        entries = debtor_entries(self.env, customers, include_settled=True)
        if seller != "all":
            entries = [e for e in entries if e["seller"] == seller]
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
            }

        groups = {"debtor": [], "uncollectible": [], "settled": []}
        for entry in entries:
            bucket = groups[entry["kind"]]
            bucket.append(payload(entry, len(bucket) + 1))
        debtors = groups["debtor"]
        return {
            "user": self.env.user.name,
            "scope": scope,
            "can_filter_seller": scope == "all",
            "seller": seller,
            "debtors": debtors,
            "uncollectible": groups["uncollectible"],
            "settled": groups["settled"] if settled else [],
            "settled_loaded": bool(settled),
            "total_debt": round(sum(d["balance"] for d in debtors), 2),
            "debtor_count": len(debtors),
            "uncollectible_total": round(sum(d["balance"] for d in groups["uncollectible"]), 2),
            "last_load": self._app_last_load(),
        }

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
            "owed_note": row.get("owed_note") or "",
            "res_model": row["res_model"],
            "res_id": row["res_id"],
            "card_id": row.get("card_id") or False,
            "comment_count": counts.get(row["line_key"], 0),
        }

    @api.model
    def app_account(self, partner_id, period=DEFAULT_PERIOD, offset=0, limit=PAGE_SIZE):
        """The account screen: header, balances, account comments and one
        page of the statement rows of the period, newest first."""
        partner = self._app_partner(partner_id)
        if period not in PERIODS:
            period = DEFAULT_PERIOD
        date_from = self._app_period_start(period)
        data = partner._distri_statement(date_from, False)
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
            "offset": offset,
            "row_count": len(rows),
            "more": offset + limit < len(rows),
            "rows": [self._app_row(row, counts) for row in page],
            "opening": data["opening"],
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
                "uncollectible": partner.distri_uncollectible,
                "uncollectible_note": partner.distri_uncollectible_note or "",
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
            "owed_count": len([r for r in data["rows"] if r.get("owed")]),
            "comments": comments.filtered(lambda c: not c.line_key)._app_payload(),
            "line_comment_count": len(comments.filtered("line_key")),
            "line_comments": comments.filtered("line_key")._app_payload(),
        })
        return result

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
        """The ids of `partners` the user may see: all of them for
        administración, those the record rules let him read for a seller
        (searched as the user, never with sudo)."""
        if self._app_check() == "all":
            return set(partners.ids)
        return set(self.env["res.partner"].with_context(active_test=False).search(
            [("id", "in", partners.ids)]).ids)

    @api.model
    def _app_sheet_files(self, sheet, visible):
        """The originals of a sheet, only for a user who sees every row of it
        (a photo or an xlsx shows the whole sheet)."""
        lines = sheet.line_ids
        if not lines or any(not l.partner_id or l.partner_id.id not in visible for l in lines):
            if self._app_check() != "all":
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
        marked, the total and its originals. A seller sees only the rows of
        the customers of his portfolio; the others are only counted, with no
        name or amount, and the originals are offered only when he sees every
        row. None when the payment is on no sheet."""
        line = self.env["distri.cash.sheet.line"].sudo().search(
            [("payment_id.move_id", "=", move.id)], limit=1)
        if not line:
            return None
        sheet = line.sheet_id
        full = self._app_check() == "all"
        visible = self._app_visible_partner_ids(sheet.line_ids.partner_id)
        rows, hidden = [], 0
        for item in sheet.line_ids:
            if not full and (not item.partner_id or item.partner_id.id not in visible):
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
            "total_label": "Total cobrado" if full else "Total de tus clientes",
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
        """The only write of the app. Created as the user, so the access
        rights and the record rules of distri.account.comment apply."""
        partner = self._app_partner(partner_id)
        label = ""
        if line_key:
            label = self._app_line_label(self._app_find_row(partner, line_key))
        comment = self.env["distri.account.comment"].create({
            "partner_id": partner.id,
            "line_key": line_key or False,
            "line_label": label,
            "body": body or "",
        })
        return comment._app_payload()[0]

    @api.model
    def app_delete_comment(self, comment_id):
        self._app_check()
        comment = self.env["distri.account.comment"].search(
            [("id", "=", int(comment_id or 0))])
        if not comment:
            raise AccessError("No se encontró el comentario.")
        comment.unlink()
        return True
