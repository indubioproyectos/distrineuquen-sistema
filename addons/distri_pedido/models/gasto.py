"""Driver expenses paid with the day's cash («Cargar gasto»).

The driver loads each expense in the app with a mandatory photo of the
receipt: fuel, spare parts, parking, parcel shipping or other. The paper
receipt is handed in at the settlement. Expenses reduce the cash to settle:

    cash collected − expenses = cash to settle (route.cash_expected)

- The photo is read by the local OCR (models/ocr_comprobante.py) in a cron
  right after it arrives; an amount that differs from the typed one, or no
  amount read, is flagged «Revisar importe».
- Settlement: administración ticks «Comprobante en papel recibido» for each
  expense or rejects it. When the settlement closes, each expense with its
  paper is approved and posted from the route's cash journal («Efectivo»)
  to its expense account; an expense without paper or rejected opens a case
  against the route for its amount (the driver owes it).
- Parcel shipping paid by Distrineuquén is created by the parcel dispatch
  (models/encomienda.py) with the guide photo as its receipt.

Accounts (Argentine chart, ar_base). Each kind can point elsewhere with the
parameter distri_pedido.cuenta_gasto_<kind> (an account code).
"""
import base64
import binascii

from odoo import api, fields, models
from odoo.exceptions import UserError

from .ocr_comprobante import ocr_image, parse_receipt
from .reparto_cliente import today_ar

EXPENSE_KINDS = [
    ("fuel", "Nafta"),
    ("parts", "Repuestos"),
    ("parking", "Estacionamiento"),
    ("parcel", "Encomienda"),
    ("other", "Otro"),
]

EXPENSE_ACCOUNTS = {
    "fuel": "5.2.1.01.050",     # Movilidad y viáticos
    "parts": "5.1.2.01.090",    # Mantenimiento y Reparaciones
    "parking": "5.2.1.01.050",  # Movilidad y viáticos
    "parcel": "5.3.1.01.100",   # Cadetería y franqueo
    "other": "5.2.1.01.030",    # Gastos Varios Comercial
}
ACCOUNT_PARAM = "distri_pedido.cuenta_gasto_%s"

AMOUNT_TOLERANCE = 1.0
MAX_PHOTO_BYTES = 4 * 1024 * 1024
_IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG", b"RIFF")


def decode_photo(value):
    """A photo sent by the phone (data URL or plain base64) -> base64 bytes
    for an Image field. Raises UserError when it is not a photo."""
    if not isinstance(value, str) or not value:
        raise UserError("Falta la foto del comprobante.")
    if value.startswith("data:"):
        value = value.split(",", 1)[-1]
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        raise UserError("La foto no se pudo leer. Sacala de nuevo.")
    if len(raw) > MAX_PHOTO_BYTES:
        raise UserError("La foto es demasiado grande.")
    if not raw.startswith(_IMAGE_MAGIC):
        raise UserError("El archivo no es una foto.")
    return base64.b64encode(raw)


class DistriRouteExpense(models.Model):
    _name = "distri.route.expense"
    _description = "Gasto del repartidor"
    _order = "captured_at, id"

    uuid = fields.Char(required=True, readonly=True, copy=False, index=True)
    route_id = fields.Many2one("distri.route", string="Ruta", required=True, ondelete="cascade",
                               index=True, readonly=True)
    driver_id = fields.Many2one(related="route_id.driver_id", store=True, string="Repartidor")
    date = fields.Date(related="route_id.date", store=True, string="Fecha")
    company_id = fields.Many2one(related="route_id.company_id", store=True)
    currency_id = fields.Many2one(related="route_id.currency_id")
    kind = fields.Selection(EXPENSE_KINDS, string="Tipo", required=True, readonly=True)
    amount = fields.Monetary(string="Importe", readonly=True)
    comment = fields.Char(string="Comentario", readonly=True)
    photo = fields.Image(string="Foto del comprobante", max_width=2000, max_height=2000,
                         attachment=True, readonly=True)
    captured_at = fields.Datetime(string="Cargado en el celular", readonly=True)
    received_at = fields.Datetime(string="Recibido", readonly=True, default=fields.Datetime.now)
    stop_id = fields.Many2one("distri.route.stop", string="Encomienda", readonly=True,
                              ondelete="set null")

    ocr_state = fields.Selection([
        ("pending", "Leyendo la foto"),
        ("ok", "Coincide con la foto"),
        ("review", "Revisar importe"),
        ("none", "Sin importe leído"),
    ], string="Lectura", default="pending", readonly=True)
    ocr_amount = fields.Monetary(string="Importe leído", readonly=True)
    amount_review = fields.Boolean(string="Revisar importe", compute="_compute_amount_review",
                                   store=True)

    paper_received = fields.Boolean(string="Comprobante en papel recibido")
    state = fields.Selection([
        ("reported", "Informado"),
        ("approved", "Aprobado"),
        ("rejected", "Rechazado"),
        ("cancelled", "Anulado"),
    ], string="Estado", default="reported", required=True, readonly=True)
    reject_reason = fields.Char(string="Motivo del rechazo")
    account_id = fields.Many2one("account.account", string="Cuenta", compute="_compute_account")
    move_id = fields.Many2one("account.move", string="Asiento", readonly=True, copy=False)
    case_id = fields.Many2one("distri.route.case", string="Caso", readonly=True, copy=False)

    _uuid_unique = models.Constraint("unique(uuid)", "Ese gasto ya está registrado.")

    @api.depends("ocr_state")
    def _compute_amount_review(self):
        for expense in self:
            expense.amount_review = expense.ocr_state == "review"

    @api.depends("kind", "company_id")
    def _compute_account(self):
        for expense in self:
            expense.account_id = expense._expense_account(raise_if_missing=False)

    # ------------------------------------------------------------ accounts

    def _expense_account(self, raise_if_missing=True):
        """The account of the expense's kind: parameter, chart code, or the
        company's default expense account."""
        self.ensure_one()
        Account = self.env["account.account"].sudo().with_company(self.company_id)
        code = (self.env["ir.config_parameter"].sudo().get_param(ACCOUNT_PARAM % self.kind)
                or EXPENSE_ACCOUNTS.get(self.kind))
        account = Account.search([("code", "=", code), ("active", "=", True)], limit=1) \
            if code else Account
        if not account:
            account = Account.search([("account_type", "=", "expense"), ("active", "=", True)],
                                     order="code", limit=1)
        if not account and raise_if_missing:
            raise UserError("No hay una cuenta de gastos para «%s»." % dict(EXPENSE_KINDS)[self.kind])
        return account

    # ------------------------------------------------------------ settlement

    def _check_open(self):
        if any(e.route_id.state == "settled" for e in self):
            raise UserError("La rendición ya se cerró.")

    def write(self, vals):
        if {"paper_received", "reject_reason"} & set(vals) and not self.env.su:
            self._check_open()
        return super().write(vals)

    def action_reject(self):
        """«Rechazar»: the expense is not accepted; a case against the route."""
        self._check_open()
        for expense in self:
            if expense.state == "cancelled":
                raise UserError("El repartidor anuló ese gasto.")
            expense.state = "rejected"
            expense._open_case("Gasto rechazado")

    def action_undo_reject(self):
        self._check_open()
        for expense in self.filtered(lambda e: e.state == "rejected"):
            if expense.case_id.state == "open":
                expense.case_id.unlink()
            expense.write({"state": "reported", "reject_reason": False})

    def _open_case(self, why):
        self.ensure_one()
        if self.case_id:
            return self.case_id
        case = self.env["distri.route.case"].create({
            "route_id": self.route_id.id,
            "kind": "expense",
            "amount": -self.amount,
            "expense_id": self.id,
            "candidates": "%s: %s %s%s%s" % (
                why, dict(EXPENSE_KINDS)[self.kind], _money(self.amount),
                " · %s" % self.comment if self.comment else "",
                " · motivo: %s" % self.reject_reason if self.reject_reason else ""),
        })
        self.case_id = case
        return case

    def _settle(self, journal):
        """At the settlement: paper received -> approved and posted;
        rejected or without paper -> a case against the route."""
        for expense in self.filtered(lambda e: e.state in ("reported", "rejected")):
            if expense.state == "rejected":
                expense._open_case("Gasto rechazado")
            elif not expense.paper_received:
                expense._open_case("Gasto sin comprobante en papel")
                expense.write({"state": "rejected",
                               "reject_reason": expense.reject_reason or "Sin comprobante en papel"})
            else:
                # 19.0.1.97.0: a settlement received in the cashier's daily
                # cash (models/caja.py) posts the expense at the closing, as
                # a line of the cash statement, instead of here
                if not self.env.context.get("distri_caja_defer_post"):
                    expense._post(journal)
                expense.state = "approved"

    def _post(self, journal):
        """Expense account against the cash journal, no taxes."""
        self.ensure_one()
        if self.move_id:
            return self.move_id
        cash_account = journal.default_account_id
        if not cash_account:
            raise UserError("El diario %s no tiene cuenta." % journal.name)
        label = "%s · %s%s" % (dict(EXPENSE_KINDS)[self.kind], self.route_id.name,
                               " · %s" % self.comment if self.comment else "")
        move = self.env["account.move"].sudo().create({
            "move_type": "entry",
            "journal_id": journal.id,
            "date": today_ar(),
            "ref": "Gasto del repartidor · %s" % self.route_id.name,
            "line_ids": [
                (0, 0, {"name": label, "account_id": self._expense_account().id,
                        "debit": self.amount, "credit": 0.0}),
                (0, 0, {"name": label, "account_id": cash_account.id,
                        "debit": 0.0, "credit": self.amount}),
            ],
        })
        move.action_post()
        if self.photo:
            self.env["ir.attachment"].sudo().create({
                "name": "Comprobante %s.jpg" % label, "datas": self.photo,
                "res_model": "account.move", "res_id": move.id, "mimetype": "image/jpeg"})
        self.move_id = move
        return move

    # ------------------------------------------------------------ OCR

    def _distri_read_amount(self):
        """Reads the amount of the receipt photo and flags a difference."""
        for expense in self.sudo().filtered(lambda e: e.ocr_state == "pending"):
            reading = {}
            if expense.photo:
                text, confidence = ocr_image(base64.b64decode(expense.photo))
                reading = parse_receipt(text, confidence) if text else {}
            read = reading.get("amount") or 0.0
            if not read:
                state = "none"
            elif abs(read - expense.amount) <= AMOUNT_TOLERANCE:
                state = "ok"
            else:
                state = "review"
            expense.write({"ocr_state": state, "ocr_amount": read})

    @api.model
    def _cron_read_photos(self, limit=30):
        """Expense receipts and parcel guides that arrived from the phones."""
        self.search([("ocr_state", "=", "pending")], limit=limit)._distri_read_amount()
        self.env["distri.parcel"].search([("ocr_state", "=", "pending")],
                                         limit=limit)._distri_read_guide()

    @api.model
    def _trigger_read(self):
        cron = self.env.ref("distri_pedido.cron_read_route_photos", raise_if_not_found=False)
        if cron:
            cron.sudo()._trigger()

    # ------------------------------------------------------------ from the phone

    @api.model
    def _distri_apply(self, route, payload, captured):
        """An expense event of the phone: new, or «Anular»."""
        expense_uuid = str(payload.get("expense_uuid") or "")
        if not (8 <= len(expense_uuid) <= 64):
            raise UserError("Gasto sin identificador.")
        existing = self.sudo().search([("uuid", "=", expense_uuid)], limit=1)
        if existing and existing.route_id != route:
            raise UserError("Ese gasto es de otra ruta.")
        if payload.get("deleted"):
            if not existing:
                return "anulado antes de llegar"
            if existing.state not in ("reported", "cancelled"):
                raise UserError("El gasto ya se controló en la rendición.")
            existing.state = "cancelled"
            return "anulado"
        if existing:
            return "ya estaba"
        kind = payload.get("kind")
        if kind not in dict(EXPENSE_KINDS):
            raise UserError("Tipo de gasto inválido.")
        from .reparto_sync import _number, _text
        amount = _number(payload.get("amount"))
        if amount <= 0:
            raise UserError("Poné el importe del gasto.")
        photo = decode_photo(payload.get("photo"))
        self.sudo().create({
            "uuid": expense_uuid,
            "route_id": route.id,
            "kind": kind,
            "amount": amount,
            "comment": _text(payload.get("comment"), 250) or False,
            "photo": photo,
            "captured_at": captured,
        })
        self._trigger_read()
        return False


def _money(amount):
    from .reparto import _money as money
    return money(amount)
