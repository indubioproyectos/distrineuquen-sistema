"""Cash sheets («planillas de rendición de efectivo», 19.0.1.75.0).

A collector (Simón, Jorge, Ítalo…) hands in a paper or spreadsheet sheet
with the cash he collected on a day: one row per customer, the invoice,
its amount and what was paid. `distri.cash.sheet` is one such sheet (one
tab of an xlsx of the 2026 archive, or the rows typed from a photo or a
Drive sheet and loaded with the cash loader) with its originals attached;
`distri.cash.sheet.line` is one row, linked to the lab `account.payment`
it became when the pairing is known.

Read-only for money: nothing here creates, changes, posts or cancels a
payment or a move. A line only points at a payment (unique: one payment is
one row). Writes come from the import script (scripts/planillas-lab/) and
from the cash loader (scripts/planillas-2026-10-28/cargar_efectivo_body.py,
`_link_loader_row`), both as superuser; in Odoo only Settings can edit,
administración and gerencia read (security/ir.model.access.csv).

The phone app (models/cuentas_app.py `_app_cash_sheet`) shows the sheet of
a cash payment; a seller only ever sees the rows of his own customers.
"""
import re
import unicodedata
from datetime import date as date_cls, timedelta

from odoo import api, fields, models

ROW_KINDS = [
    ("cliente", "Cliente"),
    ("gasto_cobrador", "Gasto del cobrador"),
    ("faltante_sobrante", "Faltante o sobrante"),
    ("flete_zapala", "Flete Zapala"),
    ("resumen", "Total o resumen"),
    ("dudoso", "Dudoso"),
]
ORIGINS = [
    ("xlsx", "Planilla Excel"),
    ("photo", "Foto"),
    ("transcription", "Transcripción"),
]
LINK_KINDS = [
    ("pareo", "Cruce de planillas"),
    ("clave", "Carga de efectivo"),
    ("memo", "Memo del pago"),
    ("importe", "Mismo cliente e importe"),
]
COLLECTOR_LABELS = {
    "SIMON": "Simón", "JORGE": "Jorge", "ITALO": "Ítalo", "GONZALO": "Gonzalo",
    "ALE": "Ale", "DEPOSITO": "Depósito", "DENIS": "Denis", "CASTANEDA": "Castañeda",
}
# «s0511-2-01» = Simón 05/11, sheet 2, row 01; «s0511-01» = sheet 1, row 01
FILA_RE = re.compile(r"^[a-z](\d{4})-(?:(\d+)-)?(\d+)$")
# a row of the 2026 archive (_procesado/v2/movimientos.csv mov_id)
MOV_RE = re.compile(r"^M\d{6}$")
# memo of a cash payment of the loader (cargar_efectivo_body.py)
MEMO_RE = re.compile(r"^Efectivo · cobró (\S+) · planilla (\d\d)/(\d\d) · ([^·]+?)\s*(?:·|$)")
# same customer, same day: amounts within this are the same row
AMOUNT_SLACK = 1.0


def collector_code(text):
    """«Simón», «SIMON », «simon» -> «SIMON»."""
    text = unicodedata.normalize("NFD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"\s+", " ", text).strip().upper()


def collector_label(code):
    return COLLECTOR_LABELS.get(code or "", (code or "").title())


def name_tokens(text):
    """The words of a name that tell customers apart (4+ letters, no accents)."""
    return {t for t in re.split(r"[^A-Z0-9]+", collector_code(text)) if len(t) >= 4}


def same_row(lines, partner, amount):
    """The unlinked lines of `partner` for `amount`; when none, the unlinked
    lines with no customer yet for `amount` whose written name shares a word
    with the customer's name («DAIANA ARONOF» for DAIANA ARONOFF)."""
    free = lines.filtered(lambda l: not l.payment_id and abs(l.amount_paid - amount) <= AMOUNT_SLACK)
    found = free.filtered(lambda l: l.partner_id == partner)
    if not found and partner:
        words = name_tokens(partner.name)
        found = free.filtered(lambda l: not l.partner_id and name_tokens(l.client_raw) & words)
    return found


def fila_position(fila):
    """(sheet index, row number, explicit index) of a loader row id."""
    match = FILA_RE.match(fila or "")
    if match:
        return int(match.group(2) or 1), int(match.group(3)), bool(match.group(2))
    digits = re.findall(r"\d+", fila or "")
    return 1, int(digits[-1]) if digits else 0, False


class DistriCashSheet(models.Model):
    _name = "distri.cash.sheet"
    _description = "Planilla de rendición de efectivo"
    _order = "date desc, collector, sheet_index, id desc"

    name = fields.Char(string="Planilla", required=True)
    collector = fields.Char(string="Cobrador", required=True, index=True,
                            help="Código del cobrador en mayúsculas sin acentos (SIMON, JORGE, ITALO…).")
    collector_label = fields.Char(string="Cobró", compute="_compute_collector_label")
    date = fields.Date(string="Fecha", required=True, index=True)
    sheet_index = fields.Integer(string="Nº de planilla del día", default=1)
    origin = fields.Selection(ORIGINS, string="Origen", required=True, default="xlsx")
    source_path = fields.Char(string="Archivo de origen")
    source_tab = fields.Char(string="Hoja")
    sha256 = fields.Char(string="SHA-256 del archivo", index=True)
    source_key = fields.Char(string="Clave de origen", required=True, index=True, readonly=True,
                             help="Evita cargar dos veces la misma planilla.")
    attachment_ids = fields.Many2many("ir.attachment", "distri_cash_sheet_attachment_rel",
                                      "sheet_id", "attachment_id", string="Originales")
    line_ids = fields.One2many("distri.cash.sheet.line", "sheet_id", string="Filas")
    line_count = fields.Integer(string="Cantidad de filas", compute="_compute_totals", store=True)
    linked_count = fields.Integer(string="Filas con pago", compute="_compute_totals", store=True)
    total_paid = fields.Float(string="Cobrado (clientes)", compute="_compute_totals", store=True,
                              help="Suma de lo pagado en las filas de clientes, arriba del total.")
    has_photo = fields.Boolean(string="Con foto", compute="_compute_has_photo", store=True)
    note = fields.Text(string="Nota")

    _source_key_unique = models.Constraint("unique(source_key)", "Esa planilla ya está cargada.")

    @api.depends("collector")
    def _compute_collector_label(self):
        for sheet in self:
            sheet.collector_label = collector_label(sheet.collector)

    @api.depends("line_ids.amount_paid", "line_ids.row_kind", "line_ids.after_total",
                 "line_ids.payment_id")
    def _compute_totals(self):
        for sheet in self:
            lines = sheet.line_ids
            sheet.line_count = len(lines)
            sheet.linked_count = len(lines.filtered("payment_id"))
            sheet.total_paid = round(sum(lines.filtered(
                lambda l: l.row_kind == "cliente" and not l.after_total).mapped("amount_paid")), 2)

    @api.depends("attachment_ids.mimetype")
    def _compute_has_photo(self):
        for sheet in self:
            sheet.has_photo = any((a.mimetype or "").startswith("image/") for a in sheet.attachment_ids)

    # ------------------------------------------------------------ attach

    def _attach(self, name, raw, mimetype):
        """Attach one original once (same name and content = already there).
        Odoo shrinks a big photo when storing it (base.image_autoresize_max_px),
        so the checksum of what was given is kept in the description."""
        self.ensure_one()
        Attachment = self.env["ir.attachment"].sudo()
        marker = "Original sha1 %s" % Attachment._compute_checksum(raw)
        if self.attachment_ids.filtered(lambda a: a.name == name and a.description == marker):
            return False
        attachment = Attachment.create({
            "name": name, "raw": raw, "mimetype": mimetype, "description": marker,
            "res_model": self._name, "res_id": self.id,
        })
        self.sudo().attachment_ids = [(4, attachment.id)]
        return attachment

    # ------------------------------------------------------------ loader

    @api.model
    def _loader_sheet(self, collector, day, index, explicit):
        """The sheet of the loaded rows of a collector on a day (found or
        created): source_key «carga:<COBRADOR>:<fecha>:<n>»."""
        key = "carga:%s:%s:%d" % (collector, day.isoformat(), index)
        Sheet = self.sudo()
        sheet = Sheet.search([("source_key", "=", key)], limit=1)
        if sheet:
            return sheet
        name = "%s · %s" % (collector_label(collector), day.strftime("%d/%m/%Y"))
        if explicit or index > 1:
            name += " · planilla %d" % index
        return Sheet.create({
            "name": name, "collector": collector, "date": day, "sheet_index": index,
            "origin": "transcription", "source_key": key,
        })

    @api.model
    def _link_loader_row(self, item, payment=None):
        """The sheet row of one row of the cash loader (cargar_efectivo_body.py
        JSONL: fila, clave, fecha, repartidor, factura, efectivo,
        cliente_planilla, lab_partner…), linked to `payment` when given.

        In order: the row already linked to the payment; the row already
        loaded under the same key (clave) or, for a row of the 2026 archive
        (fila = mov id), that row; a single unlinked row of an Excel sheet of
        the same collector and day with the same customer and amount;
        otherwise a new row of the transcription sheet (collector + day +
        sheet index of the fila). Without a payment, a row of a day the
        Excel archive already covers is not added (the Excel has it).
        Returns the line or an empty recordset. Never touches the payment."""
        Line = self.env["distri.cash.sheet.line"].sudo()
        payment = payment.sudo() if payment else self.env["account.payment"].sudo()
        if payment:
            linked = Line.search([("payment_id", "=", payment.id)], limit=1)
            if linked:
                return linked
        collector = collector_code(item.get("repartidor"))
        day = item["fecha"] if isinstance(item["fecha"], date_cls) else \
            date_cls.fromisoformat(item["fecha"])
        fila = item.get("fila") or ""
        key = item.get("clave") or ""
        partner = payment.partner_id.commercial_partner_id if payment else \
            self.env["res.partner"].sudo().browse(item.get("lab_partner") or 0).exists() \
            .commercial_partner_id

        line = Line.search([("source_key", "=", key)], limit=1) if key else Line
        if not line and MOV_RE.match(fila):
            line = Line.search([("source_key", "=", fila)], limit=1)
        if line:
            if payment and (not line.payment_id or line.payment_id.state == "canceled"):
                line.write({"payment_id": payment.id, "link_kind": "clave",
                            "partner_id": partner.id or line.partner_id.id})
            return line

        amount = float(item.get("efectivo") or 0.0)
        covered = Line.search([("sheet_id.collector", "=", collector), ("sheet_id.date", "=", day),
                               ("sheet_id.origin", "=", "xlsx")])
        if payment and partner:
            candidates = same_row(covered, partner, amount)
            if len(candidates) == 1:
                candidates.write({"payment_id": payment.id, "link_kind": "importe",
                                  "partner_id": partner.id})
                return candidates
        if not payment and covered:
            return Line

        index, row, explicit = fila_position(fila)
        sheet = self._loader_sheet(collector, day, index, explicit)
        return Line.create({
            "sheet_id": sheet.id,
            "sequence": row,
            "source_cell": fila,
            "source_key": key or False,
            "client_raw": item.get("cliente_planilla") or partner.name or "",
            "partner_id": partner.id or False,
            "invoice_ref": item.get("factura") or False,
            "amount_invoiced": float(item.get("importe_factura") or 0.0),
            "amount_paid": amount,
            "method": "efectivo",
            "notes": item.get("nota") or False,
            "row_kind": "cliente",
            "row_date": day,
            "payment_id": payment.id or False,
            "link_kind": "clave" if payment else False,
        })

    @api.model
    def _memo_parts(self, payment):
        """(COLLECTOR, day, invoice or '') of a loader memo
        «Efectivo · cobró X · planilla dd/mm · <factura | sin factura>»."""
        match = MEMO_RE.match(payment.memo or "")
        if not match or not payment.date:
            return None
        who, dd, mm, doc = match.groups()
        try:
            day = date_cls(payment.date.year, int(mm), int(dd))
        except ValueError:
            return None
        if day > payment.date + timedelta(days=31):
            day = day.replace(year=day.year - 1)
        doc = doc.strip()
        return collector_code(who), day, "" if doc.lower().startswith("sin factura") else doc

    @api.model
    def _link_by_memo(self, payment):
        """Link a cash payment whose memo names its sheet to the one unlinked
        row of that collector and day with the memo's invoice (or, without an
        invoice row, the same customer and amount, `same_row`). Empty when
        ambiguous."""
        Line = self.env["distri.cash.sheet.line"].sudo()
        payment = payment.sudo()
        if Line.search_count([("payment_id", "=", payment.id)]):
            return Line
        parts = self._memo_parts(payment)
        if not parts:
            return Line
        collector, day, doc = parts
        pool = Line.search([("sheet_id.collector", "=", collector), ("payment_id", "=", False),
                            "|", ("row_date", "=", day), ("sheet_id.date", "=", day)])
        partner = payment.partner_id.commercial_partner_id
        kind = "memo"
        found = pool.filtered(lambda l: doc and l.invoice_ref == doc)
        if not found:
            kind = "importe"
            found = same_row(pool, partner, payment.amount)
        if len(found) != 1:
            return Line
        found.write({"payment_id": payment.id, "link_kind": kind, "partner_id": partner.id})
        return found


class DistriCashSheetLine(models.Model):
    _name = "distri.cash.sheet.line"
    _description = "Fila de planilla de efectivo"
    _order = "sheet_id, sequence, id"
    _rec_name = "client_raw"

    sheet_id = fields.Many2one("distri.cash.sheet", string="Planilla", required=True, index=True,
                               ondelete="cascade")
    collector = fields.Char(related="sheet_id.collector", store=True, string="Cobrador")
    sheet_date = fields.Date(related="sheet_id.date", store=True, string="Fecha de la planilla")
    sequence = fields.Integer(string="Orden")
    source_cell = fields.Char(string="Celda / fila de origen")
    source_key = fields.Char(string="Clave de origen", index=True,
                             help="mov_id del archivo 2026 o clave del pago de la carga de efectivo.")
    client_raw = fields.Char(string="Cliente (como figura)")
    partner_id = fields.Many2one("res.partner", string="Cliente", index=True, ondelete="set null")
    invoice_ref = fields.Char(string="Factura", index=True)
    amount_invoiced = fields.Float(string="Importe factura")
    amount_paid = fields.Float(string="Pagado")
    method = fields.Char(string="Medio")
    notes = fields.Text(string="Notas")
    row_kind = fields.Selection(ROW_KINDS, string="Tipo de fila", default="cliente")
    after_total = fields.Boolean(string="Debajo del total")
    dup_status = fields.Char(string="Duplicado")
    row_date = fields.Date(string="Fecha de la fila")
    payment_id = fields.Many2one("account.payment", string="Pago en el lab", index=True,
                                 ondelete="set null")
    payment_state = fields.Selection(related="payment_id.state", string="Estado del pago")
    link_kind = fields.Selection(LINK_KINDS, string="Vinculado por")
    match_kind = fields.Char(string="Tipo de cruce")

    _payment_unique = models.Constraint("unique(payment_id)", "Ese pago ya está en otra fila.")
    _source_key_unique = models.Constraint("unique(source_key)", "Esa fila ya está cargada.")
