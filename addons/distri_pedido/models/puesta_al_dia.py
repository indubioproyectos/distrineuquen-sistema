"""Catch-up of the customer accounts in the lab first (19.0.1.38.0).

The plan: the payments land in the lab. Administración approves them on the
collections board (Tablero de cobros), each one against its customer. Only
when every receipt and every Mercado Pago movement was dealt with, the lab
exports a clean spreadsheet with the payments it confirmed, ready for
«Importar registros» in Odoo Online. Nothing here ever writes to Online.

Pieces:

- `distri.online.payment`: a read-only mirror of the customer payments of
  Online (scripts/online/leer_online.py downloads them with the read-only
  guard; scripts/online/aplicar_online_body.py loads them). It tells which
  Mercado Pago operations are already registered in Online:
    * by memo: a payment exported from here carries «MP <operation>»;
    * by amount and date (the 29/09 cruce rule): a payment of the Banco
      journal and an income of the same amount (±$1), the transfer between
      10 days before and 3 days after the payment, closest pair first, a
      payer CUIT already known for that customer first.
  An income already in Online leaves the board as «Ya cargado en Online»
  and is never exported. Administración can undo a wrong link.
- `distri.online.export`: «Exportar para Odoo Online». Only the payments
  confirmed in the lab, not in Online and not exported yet; the columns of
  the template proven on 22/10 (insumos/cruce/importar-online/). Each batch
  keeps its file and can be downloaded again.
- Closing the loop: the next read-only sync finds the exported payments in
  Online by their memo. Once posted there, its row of the history
  (distri.cuenta.movimiento) is archived so the lab never counts the same
  payment twice, and «Saldos lab vs Online» lists what still differs.
- `distri.whatsapp.chat.import`: a WhatsApp export (.zip with media, or the
  folder it unzips to) of the staff's transfers group, filtered by date. Each
  photo or PDF becomes a received message with its file, so the existing
  reading (OCR) and matching rules run on it. Idempotent by file hash. Big
  exports go through scripts/importar-chat-whatsapp.sh on the server.
"""
import base64
import hashlib
import io
import logging
import os
import re
import zipfile
from datetime import datetime, timedelta, timezone

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .cobros import AMOUNT_TOLERANCE, RECEIPT_TIME_CLOSE, _check_admin, digits, local_day
from .reparto import _money
from .reparto_cliente import AR_TZ

_logger = logging.getLogger(__name__)

ADMIN_GROUP = "distri_pedido.grupo_administracion"
# whose phone a WhatsApp export of the group came from (19.0.1.46.0)
EXPORT_OWNERS = [("manager", "Gerente"), ("admin", "Denis (administrador)"),
                 ("own", "Propio (ale)")]
# Online ids, read on 22/10/2026 (insumos/cruce/importar-online/): the
# Mercado Pago collections go to «Banco»; there is no MP journal in Online.
PARAM_ONLINE_BANK = "distri_pedido.online_diario_banco"
DEFAULT_ONLINE_BANK = 13
ONLINE_POSTED = ("in_process", "paid", "posted")
ONLINE_DONE = ONLINE_POSTED + ("draft",)
# the 29/09 cruce rule: the transfer between 10 days before and 3 after
ONLINE_BEFORE = timedelta(days=10)
ONLINE_AFTER = timedelta(days=3)
MEMO_RE = re.compile(r"(?:\bMP|Mercado Pago)\s*(\d{6,})")
EXPORT_HEADERS = ["id", "partner_id/.id", "date", "amount", "journal_id/.id", "payment_type",
                  "partner_type", "memo", "Control: cliente", "Control: diario",
                  "Control: operación MP"]
XMLID_PREFIX = "distri_cobro_mp_"
# when the Online mirror was last applied (19.0.1.41.0, models/migracion.py)
PARAM_ONLINE_SYNC = "distri_pedido.online_sync_at"

ONLINE_STATUS = [
    ("none", "Solo en el lab"),
    ("already", "Ya cargado en Online"),
    ("exported", "Exportado, falta importarlo en Online"),
    ("imported", "Importado en Online, en borrador"),
    ("loaded", "Cargado en Online"),
]


def _check_admin_or_raise(env):
    if not (env.su or env.user.has_group(ADMIN_GROUP)):
        raise AccessError("Solo administración y gerencia.")


def online_ref(partner):
    """The Online id of a lab customer (ref «ODOO-<id>» from the sync), or 0."""
    ref = (partner.commercial_partner_id.ref or "") if partner else ""
    return int(ref[5:]) if ref.startswith("ODOO-") and ref[5:].isdigit() else 0


def online_bank_journal(env):
    raw = env["ir.config_parameter"].sudo().get_param(PARAM_ONLINE_BANK)
    return int(raw) if raw and str(raw).isdigit() else DEFAULT_ONLINE_BANK


# ====================================================================== mirror


class DistriOnlinePayment(models.Model):
    _name = "distri.online.payment"
    _description = "Cobro registrado en Odoo Online (copia de lectura)"
    _order = "date desc, id desc"

    online_id = fields.Integer(string="Id en Online", required=True, index=True, readonly=True)
    name = fields.Char(string="Pago en Online", readonly=True)
    move_name = fields.Char(string="Asiento en Online", readonly=True, index=True)
    date = fields.Date(string="Fecha", readonly=True, index=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    online_partner_id = fields.Integer(string="Cliente en Online (id)", readonly=True, index=True)
    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True, index=True)
    journal_online_id = fields.Integer(string="Diario en Online (id)", readonly=True)
    journal_name = fields.Char(string="Diario", readonly=True)
    memo = fields.Char(string="Memo", readonly=True)
    state = fields.Char(string="Estado en Online", readonly=True)
    synced_at = fields.Datetime(string="Leído el", readonly=True)
    collection_id = fields.Many2one("distri.mp.collection", string="Ingreso de Mercado Pago",
                                    readonly=True, index=True, ondelete="set null")
    link_kind = fields.Selection([("memo", "Por el memo (exportado del lab)"),
                                  ("amount", "Por importe y fecha")],
                                 string="Cómo se vinculó", readonly=True)
    linked_at = fields.Datetime(string="Vinculado el", readonly=True, index=True)

    _online_unique = models.Constraint("unique(online_id)", "Ese pago de Online ya está.")

    @api.model
    def _distri_load(self, rows):
        """Upsert of the payments read from Online, then the links. Returns
        a summary dict. Only the read-only sync calls it."""
        _check_admin_or_raise(self.env)
        Partner = self.env["res.partner"].sudo().with_context(active_test=False)
        partners = {int(p.ref[5:]): p.id for p in Partner.search([("ref", "=like", "ODOO-%")])
                    if p.ref[5:].isdigit()}
        existing = {p.online_id: p for p in self.sudo().search([])}
        now = fields.Datetime.now()
        created = updated = 0
        new_vals = []
        for row in rows:
            vals = {
                "online_id": row["online_id"],
                "name": row.get("name") or "",
                "move_name": row.get("move_name") or "",
                "date": row.get("date") or False,
                "amount": row.get("amount") or 0.0,
                "online_partner_id": row.get("partner_id") or 0,
                "partner_id": partners.get(row.get("partner_id") or 0, False),
                "journal_online_id": row.get("journal_id") or 0,
                "journal_name": row.get("journal_name") or "",
                "memo": (row.get("memo") or "")[:250],
                "state": row.get("state") or "",
                "synced_at": now,
            }
            old = existing.get(row["online_id"])
            if old:
                before = (old.name or "", old.move_name or "", old.date, round(old.amount, 2),
                          old.online_partner_id, old.partner_id.id or False,
                          old.journal_online_id, old.memo or "", old.state or "")
                after = (vals["name"], vals["move_name"], fields.Date.to_date(vals["date"]),
                         round(vals["amount"], 2), vals["online_partner_id"],
                         vals["partner_id"], vals["journal_online_id"], vals["memo"],
                         vals["state"])
                if before != after:
                    old.write(vals)
                    updated += 1
            else:
                new_vals.append(vals)
        if new_vals:
            self.sudo().create(new_vals)
            created = len(new_vals)
        links = self.sudo().with_context(distri_link_now=now)._distri_link_all()
        # «Última sincronización con Online» (models/migracion.py); rolled
        # back with everything else when the sync does not commit
        self.env["ir.config_parameter"].sudo().set_param(
            PARAM_ONLINE_SYNC, fields.Datetime.to_string(now))
        return dict(links, created=created, updated=updated)

    def _distri_link_all(self):
        """Ties the Online payments to the Mercado Pago incomes of the lab.
        `linked_at`: the moment of the sync (context distri_link_now), so the
        migration board lists the links of the last sync."""
        now = self.env.context.get("distri_link_now") or fields.Datetime.now()
        Collection = self.env["distri.mp.collection"].sudo()
        stats = {"by_memo": 0, "by_amount": 0, "conflicts": 0, "archived_rows": 0}
        # 1. by memo: the ones exported from the lab come back
        for pay in self.sudo().search([("collection_id", "=", False), ("memo", "!=", False)]):
            found = MEMO_RE.search(pay.memo or "")
            if not found:
                continue
            card = Collection.search([("kind", "=", "income"),
                                      ("source_id", "=", found.group(1))], limit=1)
            if card and not card.online_payment_id:
                pay.write({"collection_id": card.id, "link_kind": "memo", "linked_at": now})
                card.write({"online_payment_id": pay.id})
                stats["by_memo"] += 1
        # 2. by amount and date: the cruce rule
        incomes = Collection.search([
            ("kind", "=", "income"), ("online_payment_id", "=", False),
            ("online_unlinked", "=", False), ("export_batch_id", "=", False),
            ("state", "in", ("draft", "confirmed"))])
        if incomes:
            stats.update(self._distri_link_by_amount(incomes, now))
        # 3. a lab payment already posted in Online: its history row is
        #    archived, the lab payment counts once
        stats["archived_rows"] = Collection.search([
            ("online_payment_id", "!=", False), ("payment_id", "!=", False)
        ])._distri_archive_history()
        return stats

    def _distri_link_by_amount(self, incomes, now=None):
        bank = online_bank_journal(self.env)
        start = min(incomes.mapped("date")) - ONLINE_AFTER
        end = max(incomes.mapped("date")) + ONLINE_BEFORE
        payments = self.sudo().search([
            ("collection_id", "=", False), ("journal_online_id", "=", bank),
            ("state", "in", ONLINE_DONE), ("amount", ">", 0),
            ("date", ">=", start.date()), ("date", "<=", end.date())])
        by_amount = {}
        for pay in payments:
            by_amount.setdefault(round(pay.amount), []).append(pay)
        known = {}
        identities = self.env["distri.payer.identity"].sudo().search(
            [("kind", "=", "cuit"), ("value", "in", [c for c in incomes.mapped("payer_cuit") if c])])
        for identity in identities:
            known.setdefault(identity.value, set()).add(
                identity.partner_id.commercial_partner_id.id)
        pairs = []
        for card in incomes:
            day = local_day(card.date)
            if not day:
                continue
            for key in (round(card.amount) - 1, round(card.amount), round(card.amount) + 1):
                for pay in by_amount.get(key, []):
                    if abs(pay.amount - card.amount) > AMOUNT_TOLERANCE:
                        continue
                    # the transfer between 10 days before and 3 after the payment
                    if not (pay.date - ONLINE_BEFORE <= day <= pay.date + ONLINE_AFTER):
                        continue
                    owners = known.get(card.payer_cuit or "", set())
                    if owners and pay.partner_id.commercial_partner_id.id not in owners:
                        # the payer's CUIT belongs to another customer: not this payment
                        continue
                    pairs.append((0 if owners else 1, abs((day - pay.date).days),
                                  card.id, pay.id, card, pay))
        stats = {"by_amount": 0, "conflicts": 0}
        cards, pays = set(), set()
        for _rank, _distance, card_id, pay_id, card, pay in sorted(pairs, key=lambda p: p[:4]):
            if card_id in cards or pay_id in pays:
                continue
            cards.add(card_id)
            pays.add(pay_id)
            pay.write({"collection_id": card.id, "link_kind": "amount",
                       "linked_at": now or fields.Datetime.now()})
            card._distri_mark_online(pay)
            stats["conflicts" if card.state == "confirmed" else "by_amount"] += 1
        return stats


# ====================================================================== cards


class DistriMpCollectionOnline(models.Model):
    _inherit = "distri.mp.collection"

    ignore_reason = fields.Selection(selection_add=[("online", "Ya cargado en Online")],
                                     ondelete={"online": "set null"})
    online_payment_id = fields.Many2one("distri.online.payment", string="Pago en Online",
                                        readonly=True, index=True, ondelete="set null")
    online_unlinked = fields.Boolean(
        string="No está en Online", readonly=True,
        help="Administración deshizo el vínculo con un pago de Online: no se vuelve a vincular "
             "por importe y fecha.")
    online_conflict = fields.Char(
        string="Posible doble carga", readonly=True,
        help="El cobro se confirmó en el lab y Online tiene un pago del mismo importe y fecha "
             "que no salió de este lab: no se exporta hasta revisarlo.")
    export_batch_id = fields.Many2one("distri.online.export", string="Lote exportado",
                                      readonly=True, index=True, ondelete="set null")
    online_status = fields.Selection(ONLINE_STATUS, string="En Online", default="none",
                                     compute="_compute_online_status", store=True, index=True)
    online_label = fields.Char(string="Detalle en Online", compute="_compute_online_status",
                               store=True)

    @api.depends("online_payment_id", "online_payment_id.state", "export_batch_id")
    def _compute_online_status(self):
        for rec in self:
            pay = rec.online_payment_id
            if rec.export_batch_id:
                status = "exported" if not pay else (
                    "imported" if pay.state == "draft" else "loaded")
            else:
                status = "already" if pay else "none"
            rec.online_status = status
            rec.online_label = ("%s del %s · %s%s" % (
                pay.name or pay.move_name or "Pago", pay.date.strftime("%d/%m/%Y") if pay.date
                else "", pay.partner_id.name or "cliente %s de Online" % pay.online_partner_id,
                " (borrador)" if pay.state == "draft" else "")) if pay else False

    def _distri_mark_online(self, pay):
        """An income found in Online by amount and date."""
        for card in self:
            if card.state == "draft":
                vals = {"online_payment_id": pay.id, "state": "ignored",
                        "ignore_reason": "online",
                        "ignore_note": "Ya cargado en Online: %s" % (pay.name or pay.move_name)}
                if pay.partner_id and not card.partner_id:
                    vals["partner_id"] = pay.partner_id.commercial_partner_id.id
                card.write(vals)
                receipt = card.receipt_id
                if receipt and receipt.distri_receipt_state in ("pending", "no_money", "matched"):
                    receipt._distri_close("registered", "Ya cargado en Online: %s del %s." % (
                        pay.name or pay.move_name, pay.date.strftime("%d/%m/%Y")
                        if pay.date else ""))
                card.message_post(body="Ya cargado en Online: %s del %s, %s." % (
                    pay.name or pay.move_name, pay.date, _money(pay.amount)))
            elif card.state == "confirmed":
                # confirmed in the lab, never exported, and Online has it: the
                # export would duplicate it
                card.write({"online_payment_id": pay.id,
                            "online_conflict": "Online ya tiene %s del %s por %s" % (
                                pay.name or pay.move_name, pay.date, _money(pay.amount))})

    def action_online_unlink(self):
        """«No está en Online»: the Online payment of the same amount was
        another one. Back to the board, never linked by amount again."""
        _check_admin(self.env)
        for card in self.filtered("online_payment_id"):
            if card.export_batch_id:
                raise UserError("%s se exportó desde el lab: el vínculo es por memo." % card.name)
            pay = card.online_payment_id
            vals = {"online_payment_id": False, "online_unlinked": True, "online_conflict": False}
            if card.state == "ignored" and card.ignore_reason == "online":
                vals.update(state="draft", ignore_reason=False, ignore_note=False)
            pay.write({"collection_id": False, "link_kind": False, "linked_at": False})
            card.write(vals)
            card.message_post(body="%s: no está en Online (era %s)." % (
                self.env.user.name, pay.name or pay.move_name))
            if card.state == "draft":
                card._propose()
        return True

    def _distri_archive_history(self):
        """The history row of a lab payment once it is posted in Online: the
        lab payment and that row are the same money."""
        Mov = self.env["distri.cuenta.movimiento"].sudo()
        archived = 0
        for card in self:
            pay = card.online_payment_id
            if not pay.move_name or pay.state not in ONLINE_POSTED or not card.partner_id:
                continue
            rows = Mov.search([("partner_id", "child_of", card.partner_id.commercial_partner_id.id),
                               ("comprobante", "=", pay.move_name),
                               ("lab_payment_id", "=", False)])
            rows = rows.filtered(lambda r: abs((r.haber or 0.0) - card.amount) <= AMOUNT_TOLERANCE)
            if rows:
                rows[:1].write({"active": False, "lab_payment_id": card.payment_id.id})
                archived += 1
        return archived

    def _export_problem(self):
        """Why a confirmed payment of the lab is not exported, or ""."""
        self.ensure_one()
        if self.online_conflict:
            return "posible doble carga: %s" % self.online_conflict
        if not online_ref(self.partner_id):
            return "el cliente %s no tiene id de Online" % (self.partner_id.name or "")
        if not self.source_id:
            return "sin número de operación"
        return ""

    @api.model
    def _distri_exportable_domain(self):
        # a possible double (online_conflict) is listed, and blocked
        return [("kind", "=", "income"), ("state", "=", "confirmed"),
                ("payment_id", "!=", False), ("export_batch_id", "=", False),
                "|", ("online_payment_id", "=", False), ("online_conflict", "!=", False)]

    @api.model
    def distri_board_counters(self):
        # see models/mayoristas.py: the two halves of the bar merge in any order
        parent = getattr(super(), "distri_board_counters", None)
        data = parent() if parent else {}

        def count(domain):
            [(n, total)] = self._read_group(domain, [], ["__count", "amount:sum"])
            return {"count": n, "total": total or 0.0}

        data["review"] = count([("state", "=", "draft"), ("kind", "in", ("income", "receipt"))])
        data["online"] = count([("online_status", "=", "already")])
        data["export"] = count(self._distri_exportable_domain())
        data["without_receipt"] = count([("confirmed_without_receipt", "=", True)])
        return data

    @api.model
    def action_open_online_already(self):
        _check_admin(self.env)
        return self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_mp_online_already")

    @api.model
    def action_open_export(self):
        _check_admin(self.env)
        return self.env["ir.actions.act_window"]._for_xml_id(
            "distri_pedido.action_online_export_wizard")


class DistriMpReportOnline(models.Model):
    _inherit = "distri.mp.report"

    online_count = fields.Integer(string="Ya cargados en Online", readonly=True)

    def _load_rows(self, rows):
        """After the import: the incomes already registered in Online leave
        the board («Ya cargado en Online») and are never exported."""
        super()._load_rows(rows)
        Payment = self.env["distri.online.payment"].sudo()
        if not Payment.search_count([]):
            return
        Payment._distri_link_all()
        online = self.env["distri.mp.collection"].sudo().search_count(
            [("report_id", "=", self.id), ("online_status", "=", "already")])
        self.online_count = online
        if online:
            pending = self.collection_ids.filtered(lambda c: c.state == "draft")
            self.summary = "%s Ya cargados en Online: %s." % (
                self._summary_text(pending), online)


class WhatsappMessageOnline(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_chat_import_id = fields.Many2one("distri.whatsapp.chat.import",
                                            string="Importado del chat", readonly=True,
                                            index=True, ondelete="set null")
    distri_chat_sender = fields.Char(string="Quién lo mandó al grupo", readonly=True)
    # 19.0.1.46.0: order inside the minute for the chat viewer (minute_positions)
    distri_chat_minute_pos = fields.Integer(
        string="Orden dentro del minuto", readonly=True,
        help="Lugar del mensaje entre los del mismo minuto en el export (1, 2, ...). 0: no se sabe.")

    @api.depends("distri_chat_import_id", "media_filename", "distri_chat_sender", "timestamp")
    def _compute_display_name(self):
        super()._compute_display_name()
        for msg in self.filtered("distri_chat_import_id"):
            when = msg.timestamp.replace(tzinfo=timezone.utc).astimezone(AR_TZ).strftime(
                "%d/%m %H:%M") if msg.timestamp else ""
            msg.display_name = "%s · %s · %s" % (msg.media_filename or "Mensaje del chat",
                                                 msg.distri_chat_sender or "", when)

    def _distri_match_income(self):
        found = super()._distri_match_income()
        if found or self.sudo().distri_receipt_state not in ("pending", "no_money"):
            return found
        # its transfer was already registered in Online (not on the board)
        receipt = self.sudo()
        if not receipt.distri_amount:
            return found
        Collection = self.env["distri.mp.collection"].sudo()
        base = [("kind", "=", "income"), ("online_status", "=", "already"),
                ("amount", ">=", receipt.distri_amount - AMOUNT_TOLERANCE),
                ("amount", "<=", receipt.distri_amount + AMOUNT_TOLERANCE)]
        owner = Collection.browse()
        if receipt.distri_operation:
            owner = Collection.search(base + ["|", ("source_id", "=", receipt.distri_operation),
                                              ("bank_transfer_id", "=",
                                               receipt.distri_operation)], limit=1)
        if not owner and receipt.distri_receipt_date:
            owner = Collection.search(base + [
                ("date", ">=", receipt.distri_receipt_date - RECEIPT_TIME_CLOSE),
                ("date", "<=", receipt.distri_receipt_date + RECEIPT_TIME_CLOSE)])
            owner = owner if len(owner) == 1 else Collection.browse()
        if owner:
            pay = owner.online_payment_id
            receipt._distri_close("registered", "Ya cargado en Online: %s del %s." % (
                pay.name or pay.move_name, pay.date.strftime("%d/%m/%Y") if pay.date else ""))
        return found


class CuentaMovimientoOnline(models.Model):
    _inherit = "distri.cuenta.movimiento"

    active = fields.Boolean(default=True, help="Archivado: es el mismo cobro que un pago "
                                               "registrado en el lab (ver «Pago del lab»).")
    lab_payment_id = fields.Many2one("account.payment", string="Pago del lab", readonly=True,
                                     index=True, ondelete="set null")


# ====================================================================== export


class DistriOnlineExport(models.Model):
    _name = "distri.online.export"
    _description = "Lote exportado para Odoo Online"
    _order = "id desc"

    name = fields.Char(string="Lote", required=True, readonly=True)
    exported_at = fields.Datetime(string="Exportado el", readonly=True,
                                  default=fields.Datetime.now)
    exported_by_id = fields.Many2one("res.users", string="Exportó", readonly=True,
                                     default=lambda s: s.env.user)
    collection_ids = fields.One2many("distri.mp.collection", "export_batch_id", string="Cobros")
    row_count = fields.Integer(string="Filas", readonly=True)
    total = fields.Monetary(string="Total", readonly=True)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    attachment_id = fields.Many2one("ir.attachment", string="Planilla", readonly=True)
    loaded_count = fields.Integer(string="Ya en Online", compute="_compute_loaded")

    def _compute_loaded(self):
        for batch in self:
            batch.loaded_count = len(batch.collection_ids.filtered(
                lambda c: c.online_status in ("imported", "loaded")))

    def action_download(self):
        self.ensure_one()
        _check_admin_or_raise(self.env)
        return {"type": "ir.actions.act_url", "target": "self",
                "url": "/web/content/%s?download=true" % self.attachment_id.id}

    @api.model
    def _rows(self, cards):
        rows = []
        for card in cards.sorted(lambda c: (c.date or datetime.min, c.id)):
            receipt = card.receipt_id
            memo = "MP %s" % card.source_id
            if receipt and receipt.timestamp:
                memo += " · comprobante WhatsApp %s" % local_day(
                    receipt.distri_receipt_date or receipt.timestamp).isoformat()
            invoice = card._chosen_invoice_data()[0]  # 19.0.1.63.0
            if invoice:
                memo += " · %s" % invoice
            rows.append([XMLID_PREFIX + digits(card.source_id), online_ref(card.partner_id),
                         card.payment_id.date or local_day(card.date), round(card.amount, 2),
                         online_bank_journal(self.env), "Recibir", "Cliente", memo,
                         card.partner_id.commercial_partner_id.name, "Banco", card.source_id])
        return rows

    @api.model
    def _xlsx(self, rows):
        """The proven template (insumos/cruce/importar-online/generar_importacion.py)."""
        from openpyxl import Workbook
        from openpyxl.styles import Font, PatternFill
        book = Workbook()
        sheet = book.active
        sheet.title = "Pagos"
        sheet.append(EXPORT_HEADERS)
        for cell in sheet[1]:
            cell.font = Font(bold=True)
            if cell.value.startswith("Control:"):
                cell.fill = PatternFill("solid", fgColor="EEEEEE")
        for row in rows:
            sheet.append(row)
        for cell in sheet["C"][1:]:
            cell.number_format = "dd/mm/yyyy"  # lowercase: Odoo reads it as a date
        for cell in sheet["D"][1:]:
            cell.number_format = "0.00"
        for col, width in zip("ABCDEFGHIJK", (30, 15, 12, 13, 15, 13, 13, 48, 34, 14, 22)):
            sheet.column_dimensions[col].width = width
        sheet.freeze_panes = "A2"
        helper = book.create_sheet("Ayuda")
        helper.append(["Columna", "Campo en Odoo (si no se asigna solo)", "Contenido"])
        for line in (
                ("id", "ID externo", "Clave fija por cobro; reimportar la misma fila actualiza"),
                ("partner_id/.id", "Cliente/Proveedor > ID de la base de datos",
                 "Número interno del cliente en Odoo Online"),
                ("date", "Fecha", "Fecha del cobro"),
                ("amount", "Importe", "Importe en pesos"),
                ("journal_id/.id", "Diario > ID de la base de datos", "Banco"),
                ("payment_type", "Tipo de pago", "Recibir"),
                ("partner_type", "Tipo de contacto", "Cliente"),
                ("memo", "Memo", "Operación MP y fecha del comprobante"),
                ("Control: …", "No importar", "Solo para revisar")):
            helper.append(list(line))
        out = io.BytesIO()
        book.save(out)
        return out.getvalue()


class DistriOnlineExportWizard(models.TransientModel):
    """«Exportar para Odoo Online»: the check first, then the batch."""
    _name = "distri.online.export.wizard"
    _description = "Exportar cobros para Odoo Online"

    date_from = fields.Date(string="Desde", help="Opcional: cobros de MP desde ese día.")
    date_to = fields.Date(string="Hasta", help="Opcional: hasta ese día inclusive.")
    allow_pending = fields.Boolean(
        string="Exportar aunque haya pendientes",
        help="Lo que todavía está sin revisar en el tablero queda para el próximo lote.")
    ready_count = fields.Integer(string="Listos para exportar", compute="_compute_check")
    ready_total = fields.Monetary(string="Total", compute="_compute_check")
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)
    pending_count = fields.Integer(string="Pendientes de revisar", compute="_compute_check")
    blocked_count = fields.Integer(string="No se exportan", compute="_compute_check")
    check_text = fields.Text(string="Revisión previa", compute="_compute_check")
    batch_id = fields.Many2one("distri.online.export", string="Lote", readonly=True)

    def _cards(self):
        domain = self.env["distri.mp.collection"]._distri_exportable_domain()
        return self.env["distri.mp.collection"].sudo().search(domain + self._period())

    def _period(self):
        domain = []
        if self.date_from:
            domain.append(("date_day", ">=", self.date_from))
        if self.date_to:
            domain.append(("date_day", "<=", self.date_to))
        return domain

    @api.depends("date_from", "date_to")
    def _compute_check(self):
        Collection = self.env["distri.mp.collection"].sudo()
        for wizard in self:
            cards = wizard._cards()
            blocked = cards.filtered(lambda c: c._export_problem())
            ready = cards - blocked
            pending = Collection.search([("state", "=", "draft"),
                                         ("kind", "in", ("income", "receipt"))]
                                        + wizard._period())
            lines = []
            if pending:
                lines.append("Pendientes de revisar en el tablero: %s (%s)." % (
                    len(pending), _money(sum(pending.mapped("amount")))))
            for card in blocked[:40]:
                lines.append("No se exporta %s, %s: %s." % (
                    card.name, _money(card.amount), card._export_problem()))
            if len(blocked) > 40:
                lines.append("… y %s más." % (len(blocked) - 40))
            if not lines:
                lines.append("Nada bloquea la exportación.")
            wizard.ready_count = len(ready)
            wizard.ready_total = sum(ready.mapped("amount"))
            wizard.pending_count = len(pending)
            wizard.blocked_count = len(blocked)
            wizard.check_text = "\n".join(lines)

    def action_export(self):
        self.ensure_one()
        _check_admin_or_raise(self.env)
        if self.pending_count and not self.allow_pending:
            raise UserError("Quedan %s pendientes de revisar en el tablero. Resolvelos o tildá "
                            "«Exportar aunque haya pendientes»." % self.pending_count)
        cards = self._cards().filtered(lambda c: not c._export_problem())
        if not cards:
            raise UserError("No hay cobros confirmados en el lab para exportar.")
        Export = self.env["distri.online.export"].sudo()
        number = Export.search_count([]) + 1
        name = "Lote %04d" % number
        rows = Export._rows(cards)
        data = Export._xlsx(rows)
        batch = Export.create({"name": name, "row_count": len(rows),
                               "total": sum(cards.mapped("amount")),
                               "exported_by_id": self.env.user.id})
        filename = "cobros-online-%s-%s.xlsx" % (
            fields.Date.context_today(self).isoformat(), "%04d" % number)
        batch.attachment_id = self.env["ir.attachment"].sudo().create({
            "name": filename, "raw": data, "res_model": batch._name, "res_id": batch.id,
            "mimetype": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"})
        cards.write({"export_batch_id": batch.id})
        for card in cards:
            card.message_post(body="Exportado para Odoo Online en el %s." % name)
        self.batch_id = batch
        return batch.action_download()


# ====================================================================== balances


class DistriOnlineBalance(models.TransientModel):
    """«Saldos lab vs Online»: per customer, after the sync."""
    _name = "distri.online.balance"
    _description = "Saldo del lab comparado con Odoo Online"
    _order = "abs_difference desc"

    partner_id = fields.Many2one("res.partner", string="Cliente", readonly=True)
    online_id = fields.Integer(string="Id en Online", readonly=True)
    online_balance = fields.Monetary(string="Saldo en Online", readonly=True)
    lab_balance = fields.Monetary(string="Saldo en el lab", readonly=True)
    difference = fields.Monetary(string="Diferencia", readonly=True)
    abs_difference = fields.Float(readonly=True)
    lab_only = fields.Monetary(string="Cobros del lab que Online no tiene", readonly=True)
    explained = fields.Boolean(string="Explicada por cobros del lab", readonly=True)
    currency_id = fields.Many2one("res.currency", default=lambda s: s.env.company.currency_id)

    @api.model
    def action_compare(self):
        _check_admin_or_raise(self.env)
        self.sudo().search([("create_uid", "=", self.env.uid)]).unlink()
        # Online's balance: every history row (archived ones too: they are
        # Online's), as the sync verified
        self.env.cr.execute("""
            SELECT p.commercial_partner_id, round(sum(m.debe - m.haber), 2)
              FROM distri_cuenta_movimiento m JOIN res_partner p ON p.id = m.partner_id
             GROUP BY p.commercial_partner_id""")
        online = dict(self.env.cr.fetchall())
        Collection = self.env["distri.mp.collection"].sudo()
        lab_only = {}
        for card in Collection.search([("state", "=", "confirmed"), ("kind", "=", "income"),
                                       ("online_status", "in", ("none", "exported", "imported"))]):
            key = card.partner_id.commercial_partner_id.id
            lab_only[key] = lab_only.get(key, 0.0) + card.amount
        Partner = self.env["res.partner"].sudo()
        vals = []
        for partner in Partner.browse(sorted(set(online) | set(lab_only))).exists():
            lab = round(partner._distri_account_balance(), 2)
            on = float(online.get(partner.id, 0.0))
            diff = round(lab - on, 2)
            if abs(diff) <= AMOUNT_TOLERANCE:
                continue
            only = round(lab_only.get(partner.id, 0.0), 2)
            vals.append({"partner_id": partner.id, "online_id": online_ref(partner),
                         "online_balance": on, "lab_balance": lab, "difference": diff,
                         "abs_difference": abs(diff), "lab_only": only,
                         "explained": abs(diff + only) <= AMOUNT_TOLERANCE})
        self.sudo().create(vals)
        return {"type": "ir.actions.act_window", "name": "Saldos lab vs Online",
                "res_model": self._name, "view_mode": "list",
                "domain": [("create_uid", "=", self.env.uid)]}


# ====================================================================== chat


LINE_RE = re.compile(
    r"^‎?\[?(\d{1,2})/(\d{1,2})/(\d{2,4}),?\s+(\d{1,2}):(\d{2})(?::(\d{2}))?"
    r"(?:\s*([ap])\.?\s?m\.?)?\]?\s*(?:-\s*)?([^:]{1,80}?):\s(.*)$", re.I)
ATTACHED_RE = re.compile(
    r"^‎?(?:<adjunto:\s*(?P<a>[^>]+)>|(?P<b>.+?\.\w{2,5})\s*\((?:archivo adjunto|file "
    r"attached)\))\s*(?P<rest>.*)$", re.I)
MEDIA_TYPES = {".jpg": ("image", "image/jpeg"), ".jpeg": ("image", "image/jpeg"),
               ".png": ("image", "image/png"), ".pdf": ("document", "application/pdf")}


def parse_chat(text):
    """[(local datetime, sender, text, attachment filename or None)] of a
    WhatsApp export (Android «d/m/aaaa, hh:mm - Nombre: texto» or iOS
    «[d/m/aa hh:mm:ss] Nombre: texto»). Continuation lines are joined."""
    out = []
    for raw in text.splitlines():
        line = raw.replace(" ", " ").rstrip()
        match = LINE_RE.match(line)
        if not match:
            if out and line:
                when, sender, body, att = out[-1]
                out[-1] = (when, sender, (body + "\n" + line.strip("‎")).strip(), att)
            continue
        day, month, year, hour, minute, second, ampm, sender, body = match.groups()
        year = int(year) + (2000 if len(year) == 2 else 0)
        hour = int(hour)
        if ampm:
            hour = hour % 12 + (12 if ampm.lower() == "p" else 0)
        try:
            when = datetime(year, int(month), int(day), hour, int(minute), int(second or 0))
        except ValueError:
            continue
        sender = sender.strip("‎ ").strip()
        body = body.strip("‎")
        att = None
        found = ATTACHED_RE.match(body)
        if found:
            att = (found.group("a") or found.group("b")).strip()
            body = (found.group("rest") or "").strip()
        out.append((when, sender, body, att))
    return out


def chat_text_key(when, sender, body, nth=1):
    """The «wa_message_id» of a text line of an export: same minute, sender
    and text are the same message (re-importing never duplicates it). `nth`
    counts the identical lines of that minute in one export (a seller writing
    the same customer under two photos, 19.0.1.60.0): the second one is a
    message of its own, and the first keeps the key it always had."""
    key = "chat:t:" + hashlib.sha256(
        ("%s|%s|%s" % (when.isoformat(), sender, body)).encode()).hexdigest()[:40]
    return key if nth == 1 else "%s:%d" % (key, nth)


def chat_file_key(raw):
    """The «wa_message_id» of a photo or PDF: its content, whatever export or
    file name it came with."""
    return "chat:" + hashlib.sha256(raw).hexdigest()[:48]


def minute_positions(rows):
    """Position of each parsed line among the lines of its same minute (1, 2,
    ...; 0 is «unknown» on the message), parallel to `rows`. The export lists the lines in WhatsApp's own
    order while it prints only the minute, so this is the best order signal
    inside a minute. Every line counts, also «<Multimedia omitido>» and the
    files of other kinds: two exports of the same group give the same
    position to the same message (19.0.1.46.0, models/visor_chat.py)."""
    out = []
    last, pos = None, 0
    for when, _sender, _body, _att in rows:
        minute = when.replace(second=0, microsecond=0)
        pos = pos + 1 if minute == last else 1
        last = minute
        out.append(pos)
    return out


def _sender_key(sender):
    """The «wa_id» of a sender of the group: their number when the export
    shows one, else letters only (never mistaken for a phone)."""
    number = digits(sender)
    if len(number) >= 8 and not re.search(r"[A-Za-z]", sender):
        return number
    return "grupo-" + "".join(chr(97 + int(c, 16)) for c in
                              hashlib.sha1(sender.encode()).hexdigest()[:12])


class _Source:
    """The files of an export: a .zip (path or bytes) or its folder."""

    def __init__(self, path=None, data=None):
        self.zip = None
        self.folder = None
        if data is not None:
            self.zip = zipfile.ZipFile(io.BytesIO(data))
        elif path and os.path.isdir(path):
            self.folder = path
        elif path:
            self.zip = zipfile.ZipFile(path)
        else:
            raise UserError("Falta el archivo del chat.")
        names = self.zip.namelist() if self.zip else os.listdir(self.folder)
        self.by_base = {os.path.basename(n): n for n in names if not n.endswith("/")}

    def chat_text(self):
        names = [b for b in self.by_base if b.lower().endswith(".txt")]
        if not names:
            raise UserError("El archivo no trae el chat (.txt): exportalo «Incluir archivos».")
        name = sorted(names, key=lambda n: ("chat" not in n.lower(), n))[0]
        raw = self.read(name)
        for encoding in ("utf-8-sig", "utf-16", "latin-1"):
            try:
                return raw.decode(encoding)
            except UnicodeDecodeError:
                continue
        return raw.decode("utf-8", "replace")

    def read(self, base):
        name = self.by_base.get(base)
        if not name:
            return None
        if self.zip:
            return self.zip.read(name)
        with open(os.path.join(self.folder, name), "rb") as fh:
            return fh.read()


class DistriWhatsappChatImport(models.Model):
    _name = "distri.whatsapp.chat.import"
    _description = "Chat de WhatsApp importado"
    _order = "id desc"

    name = fields.Char(string="Archivo", required=True, readonly=True)
    date_from = fields.Date(string="Desde", readonly=True)
    date_to = fields.Date(string="Hasta", readonly=True)
    imported_at = fields.Datetime(string="Importado el", default=fields.Datetime.now,
                                  readonly=True)
    imported_by_id = fields.Many2one("res.users", string="Importó", readonly=True,
                                     default=lambda s: s.env.user)
    message_count = fields.Integer(string="Mensajes en el período", readonly=True)
    file_count = fields.Integer(string="Fotos y PDF nuevos", readonly=True)
    duplicate_count = fields.Integer(string="Ya importados", readonly=True)
    skipped_count = fields.Integer(string="Otros archivos (audio, video, stickers)",
                                   readonly=True)
    missing_count = fields.Integer(string="Archivos que no vinieron en el zip", readonly=True)
    receipt_count = fields.Integer(string="Comprobantes leídos", compute="_compute_results")
    matched_count = fields.Integer(string="Unidos a un ingreso de MP",
                                   compute="_compute_results")
    message_ids = fields.One2many("distri.whatsapp.message", "distri_chat_import_id",
                                  string="Mensajes")
    summary = fields.Text(string="Resumen", readonly=True)
    # 19.0.1.46.0: whose phone exported the chat; the chat viewer picks the
    # source of each period with it (models/visor_chat.py)
    distri_export_owner = fields.Selection(EXPORT_OWNERS, string="Exportado por",
                                           readonly=True, index=True)

    def _compute_results(self):
        for rec in self:
            receipts = rec.message_ids.filtered("distri_is_receipt")
            rec.receipt_count = len(receipts)
            rec.matched_count = len(receipts.filtered(
                lambda m: m.distri_collection_id.kind == "income"
                or m.distri_receipt_state == "registered"))

    @api.model
    def _distri_import(self, name, date_from, date_to, path=None, data=None, commit_every=0,
                       owner=None):
        """Imports the messages of [date_from, date_to] (Argentine days).
        `commit_every`: the server script commits every N files, so a long
        import keeps its progress (it is idempotent). `owner`: whose export
        it is (EXPORT_OWNERS)."""
        _check_admin_or_raise(self.env)
        if not date_from or not date_to or date_to < date_from:
            raise UserError("Elegí el período: desde y hasta.")
        if owner and owner not in dict(EXPORT_OWNERS):
            raise UserError("Exportado por: valor desconocido.")
        source = _Source(path=path, data=data)
        rows = parse_chat(source.chat_text())
        positions = minute_positions(rows)
        messages = [m + (pos,) for m, pos in zip(rows, positions)
                    if date_from <= m[0].date() <= date_to]
        record = self.sudo().create({"name": name, "date_from": date_from, "date_to": date_to,
                                     "message_count": len(messages),
                                     "distri_export_owner": owner or False})
        Message = self.env["distri.whatsapp.message"].sudo()
        seen = set(Message.search([("wa_message_id", "=like", "chat:%")]).mapped(
            "wa_message_id"))

        def utc(local):
            return local.replace(tzinfo=AR_TZ).astimezone(timezone.utc).replace(tzinfo=None)

        # texts first: the notes next to each photo name the customer
        texts = []
        repeats = {}
        for when, sender, body, att, pos in messages:
            if att or not body or body.startswith("<") and body.endswith(">"):
                continue
            repeats[(when, sender, body)] = nth = repeats.get((when, sender, body), 0) + 1
            key = chat_text_key(when, sender, body, nth)
            if key in seen:
                continue
            seen.add(key)
            texts.append({"wa_message_id": key, "direction": "in", "message_type": "text",
                          "wa_id": _sender_key(sender), "profile_name": sender[:64],
                          "body": body[:4000], "timestamp": utc(when),
                          "distri_chat_import_id": record.id, "distri_chat_sender": sender[:64],
                          "distri_chat_minute_pos": pos})
        if texts:
            Message.create(texts)
        new = dup = skipped = missing = 0
        for when, sender, body, att, pos in messages:
            if not att:
                continue
            ext = os.path.splitext(att)[1].lower()
            kind = MEDIA_TYPES.get(ext)
            if not kind or att.upper().startswith("STK-"):
                skipped += 1
                continue
            raw = source.read(att)
            if raw is None:
                missing += 1
                continue
            key = chat_file_key(raw)
            if key in seen:
                dup += 1
                continue
            seen.add(key)
            message_type, mime = kind
            msg = Message.create({
                "wa_message_id": key, "direction": "in", "message_type": message_type,
                "wa_id": _sender_key(sender), "profile_name": sender[:64], "body": body[:4000],
                "timestamp": utc(when), "media_mime_type": mime, "media_filename": att[:128],
                "media_state": "pending", "distri_chat_import_id": record.id,
                "distri_chat_sender": sender[:64], "distri_chat_minute_pos": pos})
            attachment = self.env["ir.attachment"].sudo().create({
                "name": att, "raw": raw, "mimetype": mime,
                "res_model": msg._name, "res_id": msg.id})
            # «done» runs the reading of the file (OCR) and the matching
            msg.write({"attachment_id": attachment.id, "media_state": "done"})
            new += 1
            if commit_every and new % commit_every == 0:
                record.write({"file_count": new, "duplicate_count": dup})
                self.env.cr.commit()
                _logger.info("chat import %s: %s files", record.id, new)
        record.write({"file_count": new, "duplicate_count": dup, "skipped_count": skipped,
                      "missing_count": missing})
        # receipts of a period a loaded report already covers and that found
        # no transfer: «Sin transferencia», as when the report comes after them
        stamps = record.message_ids.filtered("timestamp").mapped("timestamp")
        if stamps:
            reports = self.env["distri.mp.report"].sudo().search([
                ("period_start", "<=", max(stamps)), ("covers_until", ">=", min(stamps))])
            for report in reports:
                report._receipts_without_money(report.period_start, report.covers_until)
        record.summary = record._summary_text()
        return record

    def _summary_text(self):
        self.ensure_one()
        receipts = self.message_ids.filtered("distri_is_receipt")
        states = {}
        for msg in receipts:
            states[msg.distri_receipt_state] = states.get(msg.distri_receipt_state, 0) + 1
        labels = dict(self.env["distri.whatsapp.message"]._fields[
            "distri_receipt_state"].selection)
        parts = ["%s mensajes del %s al %s: %s fotos y PDF nuevos, %s ya importados, %s otros "
                 "archivos%s." % (
                     self.message_count, self.date_from.strftime("%d/%m/%Y"),
                     self.date_to.strftime("%d/%m/%Y"), self.file_count, self.duplicate_count,
                     self.skipped_count, (", %s no venían en el zip" % self.missing_count)
                     if self.missing_count else "")]
        parts.append("Comprobantes: %s. %s" % (len(receipts), " · ".join(
            "%s: %s" % (labels.get(k, k), v) for k, v in sorted(states.items(), key=str))))
        return "\n".join(parts)


class DistriWhatsappChatImportWizard(models.TransientModel):
    _name = "distri.whatsapp.chat.import.wizard"
    _description = "Importar chat de WhatsApp"

    file = fields.Binary(string="Chat exportado (.zip con archivos)")
    filename = fields.Char(string="Nombre del archivo")
    date_from = fields.Date(string="Desde", required=True)
    date_to = fields.Date(string="Hasta", required=True,
                          default=lambda s: fields.Date.context_today(s))
    owner = fields.Selection(EXPORT_OWNERS, string="Exportado por", required=True,
                             default="own",
                             help="De qué teléfono salió el export. El visor del chat muestra "
                                  "el del gerente hasta el 23/10/2026 y el propio desde el "
                                  "24/10/2026.")
    import_id = fields.Many2one("distri.whatsapp.chat.import", readonly=True)
    summary = fields.Text(related="import_id.summary")

    MAX_UPLOAD = 100 * 1024 * 1024

    def action_import(self):
        self.ensure_one()
        _check_admin_or_raise(self.env)
        if not self.file:
            raise UserError("Subí el .zip del chat.")
        data = base64.b64decode(self.file)
        if len(data) > self.MAX_UPLOAD:
            raise UserError("El archivo pasa los 100 MB: importalo en el servidor con "
                            "scripts/importar-chat-whatsapp.sh (ver la guía).")
        self.import_id = self.env["distri.whatsapp.chat.import"]._distri_import(
            self.filename or "chat.zip", self.date_from, self.date_to, data=data,
            owner=self.owner)
        return {"type": "ir.actions.act_window", "res_model": self._name, "res_id": self.id,
                "view_mode": "form", "target": "new", "name": "Chat importado"}
