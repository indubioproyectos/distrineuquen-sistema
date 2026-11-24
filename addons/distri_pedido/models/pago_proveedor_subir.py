"""«Es un pago a proveedor autorizado por Gerencia» in «Subir comprobante»
(19.0.1.129.14, owner).

A customer paid one of the company's suppliers straight, with Gerencia's
authorization (models/pago_proveedor.py). Its receipt was refused on
/comprobantes («transferencia a otra cuenta») and had to be loaded by hand
in the backend. Now, only for grupo_pago_proveedor (checked on the server):

- /comprobantes has the option «Es un pago a proveedor autorizado por
  Gerencia»: one file, the customer, the supplier (an existing partner by
  name or CUIT, or a new one from the CUIT and name read on the receipt), the
  amount and the transfer date (prefilled from the reading, editable) and
  «Autorizó» (a Gerencia or Sistemas user; the current user by default when
  he is Gerencia). It uploads the receipt and registers it in one
  transaction (`_distri_supplier_upload`): nothing is left behind when a
  check fails.
- A receipt refused for «transferencia a otra cuenta» offers «¿Es un pago a
  proveedor autorizado? Cargalo como pago a proveedor» (the upload result,
  its row and «Ver comprobante»): /comprobantes/<id>/pago-proveedor registers
  the receipt already uploaded (`_distri_supplier_register`).

Registering creates the `distri.supplier.payment` with the receipt's file
and posts it (the customer is credited in PPCC). The receipt ends «Ya
registrado» with the note «Registrado como pago a proveedor PPC-…», linked
(`distri_supplier_payment_id`); its «Esperando reporte MP» card is
discarded with the reason «Pago a proveedor autorizado por Gerencia». A
closed receipt is never offered for crediting again nor matched against the
Mercado Pago report (`_distri_match_income` and the credited pool only look
at pending receipts and credited cards).

Guards: the same receipt (operation or file checksum) is registered once, as
a supplier payment or as a normal credit (both ways: `_distri_credit_review`
refuses a receipt whose file or operation is in a supplier payment); the
amount > 0; never the generic customer.
"""
import base64
import logging
import re
from datetime import date as date_cls

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError, ValidationError

from .cobros import digits, format_cuit, local_day, own_account
from .comprobante_subir import BUCKETS
from .pago_proveedor import AUTHORIZER_GROUPS, SUPPLIER_DENIED, can_supplier_pay
from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

SUPPLIER_LABEL = "Pago a proveedor"
SUPPLIER_HINT = "¿Es un pago a proveedor autorizado? Cargalo como pago a proveedor"
SUPPLIER_URL = "/comprobantes/%d/pago-proveedor"
SUPPLIER_NOTE = "Registrado como pago a proveedor %s: %s a %s, proveedor %s, autorizó %s."
SUPPLIER_DONE = "Registrado como pago a proveedor %s: %s acreditados a %s (proveedor %s)."
SUPPLIER_SEARCH_MAX = 8
NOTE_MAX = 200
_CUIT_RE = re.compile(r"^\d{11}$")


def authorizer_users(env):
    """Who may authorize a supplier payment: Gerencia and Sistemas."""
    users = env["res.users"].sudo().search([("share", "=", False)])
    return users.filtered(lambda u: any(u.has_group(g) for g in AUTHORIZER_GROUPS)).sorted("name")


def default_authorizer(env):
    user = env.user
    return user if user.has_group("distri_pedido.grupo_gerencia") else env["res.users"]


def _partner_label(partner):
    cuit = digits(partner.vat)
    return "%s · CUIT %s" % (partner.name, format_cuit(cuit)) if cuit else (partner.name or "")


class ResPartnerSupplierSearch(models.Model):
    _inherit = "res.partner"

    @api.model
    def _distri_suppliers_by_cuit(self, cuit):
        """Partners whose own CUIT (vat) is `cuit`, suppliers first. Never
        the learned payer identities: those point at customers."""
        cuit = digits(cuit)
        if len(cuit) != 11:
            return self.browse()
        self.flush_model(["vat"])
        self.env.cr.execute("""
            SELECT id FROM res_partner
             WHERE active AND parent_id IS NULL
               AND regexp_replace(coalesce(vat, ''), '\\D', '', 'g') = %s
        """, [cuit])
        partners = self.sudo().browse([r[0] for r in self.env.cr.fetchall()]).filtered(
            lambda p: not p.distri_generic_partner)
        return partners.sorted(lambda p: (-(p.supplier_rank or 0), p.id))

    @api.model
    def _distri_supplier_search(self, query):
        """[{id, name, cuit, reason}] for «Proveedor» (name or CUIT)."""
        text = " ".join((query or "").split())
        if len(text) < 2:
            return []
        found = self.browse()
        number = digits(text)
        if len(number) == 11:
            found = self._distri_suppliers_by_cuit(number)
        if not found:
            domain = [("active", "=", True), ("parent_id", "=", False),
                      ("distri_generic_partner", "=", False)]
            if len(number) >= 6 and len(number) >= len(text.replace("-", "").replace(" ", "")):
                domain.append(("vat", "ilike", number))
            else:
                domain.append(("name", "ilike", text))
            found = self.sudo().search(domain, order="supplier_rank desc, name",
                                       limit=SUPPLIER_SEARCH_MAX)
        return [{"id": p.id, "name": p.name or "", "cuit": format_cuit(p.vat) if p.vat else "",
                 "supplier": bool(p.supplier_rank)} for p in found[:SUPPLIER_SEARCH_MAX]]

    @api.model
    def _distri_supplier_resolve(self, supplier_id=None, cuit="", name=""):
        """The supplier chosen («proveedor_id») or the one of the CUIT and
        name typed (found by CUIT, else created). Raises UserError."""
        try:
            wanted = int(supplier_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        Partner = self.sudo()
        if wanted:
            partner = Partner.browse(wanted).exists()
            if not partner or not partner.active or partner.distri_generic_partner:
                raise UserError("Elegí el proveedor de la lista.")
            return partner.commercial_partner_id
        cuit = digits(cuit)
        name = " ".join(str(name or "").split())[:120]
        if not cuit and not name:
            raise UserError("Elegí el proveedor o escribí su CUIT y su nombre.")
        if not _CUIT_RE.match(cuit):
            raise UserError("El CUIT del proveedor tiene 11 números.")
        existing = self._distri_suppliers_by_cuit(cuit)[:1]
        if existing:
            return existing
        if not name:
            raise UserError("Escribí el nombre del proveedor.")
        vals = {"name": name, "vat": cuit, "company_type": "company", "supplier_rank": 1}
        cuit_type = self.env.ref("l10n_ar.it_cuit", raise_if_not_found=False)
        if cuit_type:
            vals["l10n_latam_identification_type_id"] = cuit_type.id
        country = self.env.ref("base.ar", raise_if_not_found=False)
        if country:
            vals["country_id"] = country.id
        try:
            with self.env.cr.savepoint():
                partner = Partner.create(vals)
        except ValidationError as error:
            raise UserError("Revisá el CUIT del proveedor: %s" % error.args[0]) from None
        _logger.info("Supplier %s created from a receipt by %s", partner.id,
                     self.env.user.login)
        return partner


class DistriSupplierPaymentReceipt(models.Model):
    _inherit = "distri.supplier.payment"

    receipt_message_id = fields.Many2one(
        "distri.whatsapp.message", string="Comprobante subido", readonly=True, copy=False,
        index="btree_not_null", ondelete="set null",
        help="El comprobante subido en «Subir comprobante» que se registró como este pago.")
    receipt_checksum = fields.Char(string="Huella del archivo", readonly=True, copy=False,
                                   index="btree_not_null")
    operation = fields.Char(string="Operación", readonly=True, copy=False,
                            index="btree_not_null")

    @api.model_create_multi
    def create(self, vals_list):
        records = super().create(vals_list)
        for rec in records.sudo().filtered(lambda r: not r.receipt_checksum):
            attachment = self.env["ir.attachment"].sudo().search([
                ("res_model", "=", rec._name), ("res_id", "=", rec.id),
                ("res_field", "=", "receipt_file")], limit=1)
            if attachment.checksum:
                super(DistriSupplierPaymentReceipt, rec).write(
                    {"receipt_checksum": attachment.checksum})
        return records

    @api.model
    def _distri_same_receipt(self, checksums=(), operation=""):
        """The supplier payment (not cancelled) of the same file or the same
        operation, or an empty recordset."""
        checksums = [c for c in checksums if c]
        operation = digits(operation)
        if not checksums and not operation:
            return self.browse()
        domain = [("state", "!=", "cancelled")]
        if checksums and operation:
            domain += ["|", ("receipt_checksum", "in", checksums), ("operation", "=", operation)]
        elif checksums:
            domain.append(("receipt_checksum", "in", checksums))
        else:
            domain.append(("operation", "=", operation))
        return self.sudo().search(domain, limit=1)


class DistriMpCollectionSupplier(models.Model):
    _inherit = "distri.mp.collection"

    ignore_reason = fields.Selection(
        selection_add=[("supplier_payment", "Pago a proveedor autorizado por Gerencia")],
        ondelete={"supplier_payment": "set null"})

    def _distri_credit_review(self, partner, amount, driver_auto=False):
        """19.0.1.129.14: a receipt whose file or operation is already a
        supplier payment is never credited too."""
        errors, warnings = super()._distri_credit_review(partner, amount, driver_auto=driver_auto)
        receipt = self.sudo().receipt_id
        if receipt:
            twin = self.env["distri.supplier.payment"]._distri_same_receipt(
                receipt._distri_receipt_checksums(),
                self.sudo().operation or receipt.distri_operation)
            if twin:
                errors.append("Este comprobante ya está registrado como pago a proveedor (%s): "
                              "no se acredita." % twin.name)
        return errors, warnings


class DistriWhatsappMessageSupplier(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_supplier_payment_id = fields.Many2one(
        "distri.supplier.payment", string="Pago a proveedor", readonly=True, copy=False,
        index="btree_not_null", ondelete="set null",
        help="El comprobante se registró como pago a proveedor autorizado por Gerencia.")

    def _distri_receipt_checksums(self):
        self.ensure_one()
        msg = self.sudo()
        return [c for c in (msg.attachment_id.checksum, msg.distri_original_checksum) if c]

    # ------------------------------------------------------------ offer

    def _distri_supplier_problem(self):
        """Why this receipt can not be registered as a supplier payment, or
        "" (permissions apart)."""
        self.ensure_one()
        msg = self.sudo()
        if msg.distri_supplier_payment_id:
            return "Ya está registrado como pago a proveedor (%s)." % (
                msg.distri_supplier_payment_id.name)
        if not msg.attachment_id:
            return "El comprobante no tiene el archivo."
        if msg._distri_upload_cancelled():
            return "Este comprobante está cancelado."
        card = msg._distri_verify_card()
        if card.state in ("confirmed", "authorized"):
            return ("Ya está acreditado (%s): no se registra también como pago a proveedor."
                    % card.name)
        if msg.distri_receipt_state not in ("pending", "no_money", False):
            state = dict(msg._fields["distri_receipt_state"].selection).get(
                msg.distri_receipt_state, msg.distri_receipt_state)
            return "El comprobante está «%s»: no se registra como pago a proveedor." % state
        return ""

    def _distri_supplier_offer(self, user=None):
        """`user` may register this receipt as a supplier payment."""
        self.ensure_one()
        user = user or self.env.user
        return bool(user.has_group("distri_pedido.grupo_pago_proveedor")
                    and not self._distri_supplier_problem())

    def _distri_supplier_hint(self, user=None):
        """The link of «¿Es un pago a proveedor autorizado?»: a receipt to
        another account that `user` may register, else ""."""
        self.ensure_one()
        msg = self.sudo()
        if not msg._distri_other_account(own_account(self.env)):
            return ""
        return SUPPLIER_URL % msg.id if msg._distri_supplier_offer(user) else ""

    def _distri_supplier_defaults(self):
        """{amount, amount_text, date, dest_cuit, dest_name, supplier} read on
        the receipt (the form's prefill)."""
        self.ensure_one()
        msg = self.sudo()
        amount = msg.distri_read_amount or msg.distri_amount or msg.distri_amount_typed or 0.0
        supplier = self.env["res.partner"]._distri_suppliers_by_cuit(msg.distri_dest_cuit)[:1]
        return {
            "amount": amount,
            "amount_text": ("%.2f" % amount).replace(".", ",").replace(",00", "") if amount else "",
            "date": (local_day(msg.distri_receipt_date or msg.timestamp) or today_ar()).isoformat(),
            "dest_cuit": digits(msg.distri_dest_cuit),
            "dest_name": msg.distri_dest_name or "",
            "destination": msg._distri_destination_label() or "",
            "supplier": {"id": supplier.id, "name": _partner_label(supplier)} if supplier else None,
        }

    @api.model
    def _distri_supplier_fields(self, defaults=None, values=None):
        """What the supplier fields of both pages show (views/comprobantes.xml
        comprobante_pago_proveedor_campos): `defaults` read on the receipt,
        `values` typed by the user (kept after a refusal)."""
        defaults = defaults or {}
        values = values or {}
        authorizer = default_authorizer(self.env)
        supplier = defaults.get("supplier") or None
        chosen = values.get("proveedor_id")
        if chosen:
            try:
                partner = self.env["res.partner"].sudo().browse(int(chosen)).exists()
            except (TypeError, ValueError):
                partner = self.env["res.partner"]
            supplier = {"id": partner.id, "name": _partner_label(partner)} if partner else None
        elif values:
            supplier = None
        try:
            authorizer_id = int(values.get("autorizo_id") or 0) or authorizer.id
        except (TypeError, ValueError):
            authorizer_id = authorizer.id
        return {
            "supplier": supplier,
            "new_cuit": values.get("proveedor_cuit", defaults.get("dest_cuit", "")),
            "new_name": values.get("proveedor_nombre", defaults.get("dest_name", "")),
            "new_open": bool(not supplier and (values.get("proveedor_cuit")
                                               or defaults.get("dest_cuit"))),
            "destination": defaults.get("destination", ""),
            "amount_text": values.get("proveedor_importe", defaults.get("amount_text", "")),
            "date": values.get("proveedor_fecha", defaults.get("date", today_ar().isoformat())),
            "today": today_ar().isoformat(),
            "authorizers": [(u.id, u.name) for u in authorizer_users(self.env)],
            "authorizer_id": authorizer_id or False,
            "note": values.get("proveedor_nota", ""),
        }

    @api.model
    def _distri_supplier_parse(self, values):
        """(supplier, amount, day, authorizer, note) of the form's values;
        raises UserError."""
        Partner = self.env["res.partner"]
        supplier = Partner._distri_supplier_resolve(values.get("proveedor_id"),
                                                    values.get("proveedor_cuit"),
                                                    values.get("proveedor_nombre"))
        text = str(values.get("proveedor_importe") or "").strip()
        if not text:
            raise UserError("Escribí el importe.")
        amount = self.env["distri.cash.collection"]._cash_amount(text)
        day_text = str(values.get("proveedor_fecha") or "").strip()
        try:
            day = date_cls.fromisoformat(day_text) if day_text else None
        except ValueError:
            day = None
        if not day:
            raise UserError("Elegí la fecha de la transferencia.")
        try:
            authorizer_id = int(values.get("autorizo_id") or 0)
        except (TypeError, ValueError):
            authorizer_id = 0
        authorizer = self.env["res.users"].sudo().browse(authorizer_id).exists() \
            if authorizer_id else self.env["res.users"]
        if not authorizer:
            raise UserError("Elegí quién de Gerencia lo autorizó.")
        note = " ".join(str(values.get("proveedor_nota") or "").split())[:NOTE_MAX]
        return supplier, amount, day, authorizer, note

    # ------------------------------------------------------------ register

    def _distri_supplier_duplicate(self):
        """Why this receipt is already registered elsewhere, or ""."""
        self.ensure_one()
        msg = self.sudo()
        same_image = msg._distri_same_image()
        if same_image:
            return "La misma imagen ya está cargada en otro comprobante (%s): no se registra " \
                   "dos veces." % same_image._distri_upload_label()
        twins = msg._distri_twins().filtered(
            lambda m: m.distri_receipt_state in ("matched", "registered")
            or m.distri_collection_id.state in ("confirmed", "authorized"))
        if twins:
            return "Es el mismo comprobante que otro ya acreditado o registrado (%s): no se " \
                   "registra dos veces." % twins[:1]._distri_upload_label()
        operation = digits(msg.distri_operation)
        if operation and self.env["distri.mp.collection"].sudo().search_count([
                ("kind", "=", "verify"), ("state", "in", ("confirmed", "authorized")),
                ("operation", "=", operation), ("receipt_id", "!=", msg.id)]):
            return "La operación %s ya se acreditó con otro comprobante: no se registra dos " \
                   "veces." % operation
        twin = self.env["distri.supplier.payment"]._distri_same_receipt(
            msg._distri_receipt_checksums(), operation)
        if twin:
            return "Este comprobante ya está registrado como pago a proveedor (%s)." % twin.name
        return ""

    def _distri_supplier_register(self, partner, supplier, amount, day, authorizer, note=""):
        """Registers this uploaded receipt as a supplier payment and credits
        the customer (see the module docstring). Returns the record."""
        self.ensure_one()
        if not can_supplier_pay(self.env):
            raise AccessError(SUPPLIER_DENIED)
        msg = self.sudo()
        problem = msg._distri_supplier_problem() or msg._distri_supplier_duplicate()
        if problem:
            raise UserError(problem)
        partner = partner.commercial_partner_id if partner else partner
        if not partner:
            raise UserError("Elegí el cliente.")
        if partner.distri_generic_partner:
            raise UserError("Elegí un cliente con cuenta corriente: el genérico no lleva saldo.")
        if not amount or amount <= 0:
            raise UserError("Escribí el importe: tiene que ser mayor que cero.")
        if not supplier:
            raise UserError("Elegí el proveedor.")
        if supplier.commercial_partner_id == partner:
            raise UserError("El proveedor no puede ser el mismo cliente.")
        if day > today_ar():
            raise UserError("La fecha de la transferencia es posterior a hoy.")
        if not authorizer or not any(authorizer.has_group(g) for g in AUTHORIZER_GROUPS):
            raise UserError("Lo autoriza un usuario de Gerencia o Sistemas.")
        attachment = msg.attachment_id
        record = self.env["distri.supplier.payment"].create({
            "partner_id": partner.id,
            "supplier_id": supplier.id,
            "amount": amount,
            "date": day,
            "authorized_by_id": authorizer.id,
            "receipt_file": base64.b64encode(attachment.raw or b""),
            "receipt_filename": attachment.name or msg.media_filename or "comprobante",
            "receipt_message_id": msg.id,
            "receipt_checksum": attachment.checksum or False,
            "operation": digits(msg.distri_operation) or False,
            "note": note or False,
        })
        record.action_post()
        if supplier.supplier_rank < 1:
            supplier.sudo().write({"supplier_rank": 1})
        text = SUPPLIER_NOTE % (record.name, _money(amount), partner.name, supplier.name,
                                authorizer.name)
        vals = {"distri_supplier_payment_id": record.id}
        if msg.distri_partner_id.commercial_partner_id != partner:
            vals.update(distri_partner_id=partner.id,
                        distri_partner_source="Elegido por %s al registrarlo" % self.env.user.name)
        msg.with_context(distri_receipt_matching=True).write(vals)
        msg._distri_close("registered", text)
        Collection = self.env["distri.mp.collection"].sudo()
        # «Foto sin transferencia» of a receipt without money: discarded too
        Collection.search([("receipt_id", "=", msg.id), ("kind", "=", "receipt"),
                           ("state", "=", "draft")]).write({"state": "ignored"})
        for card in Collection.search([("receipt_id", "=", msg.id),
                                       ("kind", "in", ("verify", "receipt")),
                                       ("state", "=", "ignored")]):
            card.write({"ignore_reason": "supplier_payment", "ignore_note": text})
            card.message_post(body=text)
        _logger.info("Receipt %s registered as supplier payment %s by %s", msg.id, record.id,
                     self.env.user.login)
        return record

    @api.model
    def _distri_supplier_upload(self, name, raw, partner, values, via="pantalla", turn=0):
        """«Es un pago a proveedor autorizado por Gerencia» on /comprobantes:
        uploads the one file and registers it. The caller runs it in a
        savepoint: a refusal leaves nothing. Returns the upload result."""
        if not can_supplier_pay(self.env):
            raise AccessError(SUPPLIER_DENIED)
        partner = partner.commercial_partner_id if partner else self.env["res.partner"]
        if not partner:
            raise UserError("Elegí el cliente.")
        if partner.distri_generic_partner:
            raise UserError("Elegí un cliente con cuenta corriente: el genérico no lleva saldo.")
        supplier, amount, day, authorizer, note = self._distri_supplier_parse(values)
        if amount <= 0:
            raise UserError("Escribí el importe: tiene que ser mayor que cero.")
        [result] = self._distri_upload_receipts(
            [(name, raw)], partner=partner, via=via,
            amounts=[values.get("proveedor_importe") or ""], rotations=[turn])
        if result["status"] != "nuevo":
            raise UserError(result["message"])
        msg = self.browse(result["id"])
        # read now: the operation and the destination are part of the checks
        msg._distri_read_now()
        record = msg._distri_supplier_register(partner, supplier, amount, day, authorizer, note)
        result.update(credit="proveedor", supplier_payment=record.name,
                      message=SUPPLIER_DONE % (record.name, _money(amount), partner.name,
                                               supplier.name))
        return result

    # ------------------------------------------------------------ the upload screen

    def _distri_upload_credit_now(self, partner, amount=None, confirmed=False):
        result = super()._distri_upload_credit_now(partner, amount=amount, confirmed=confirmed)
        if result.get("credit") == "pendiente":
            self.invalidate_recordset()
            hint = self._distri_supplier_hint(self.env.user)
            if hint:
                result.update(supplier_url=hint, supplier_hint=SUPPLIER_HINT)
        return result

    @api.model
    def _distri_upload_preview(self, raw, degrees=0):
        preview = super()._distri_upload_preview(raw, degrees)
        if not can_supplier_pay(self.env):
            for key in ("dest_cuit", "dest_name"):
                preview.pop(key, None)
            return preview
        supplier = self.env["res.partner"]._distri_suppliers_by_cuit(
            preview.get("dest_cuit") or "")[:1]
        preview["supplier"] = {"id": supplier.id, "name": _partner_label(supplier)} \
            if supplier else None
        return preview

    def _distri_upload_bucket(self):
        if self.sudo().distri_supplier_payment_id:
            return "aprobado"
        return super()._distri_upload_bucket()

    def _distri_upload_status_label(self):
        if self.sudo().distri_supplier_payment_id:
            return SUPPLIER_LABEL
        return super()._distri_upload_status_label()

    def _distri_upload_row(self):
        row = super()._distri_upload_row()
        msg = self.sudo()
        if msg.distri_supplier_payment_id:
            row["bucket"] = "aprobado"
            row["bucket_label"] = BUCKETS["aprobado"]
            row["supplier_payment"] = msg.distri_supplier_payment_id.name
        hint = self._distri_supplier_hint(self.env.user)
        if hint:
            row["supplier_url"] = hint
        return row

    def _distri_upload_detail(self):
        detail = super()._distri_upload_detail()
        msg = self.sudo()
        record = msg.distri_supplier_payment_id
        detail.update({
            "supplier_url": self._distri_supplier_hint(self.env.user),
            "supplier_hint": SUPPLIER_HINT,
            "supplier_payment": {
                "name": record.name, "supplier": record.supplier_id.name or "",
                "amount": record.amount, "authorized_by": record.authorized_by_id.name or "",
                "state": dict(record._fields["state"].selection).get(record.state, ""),
            } if record else None,
        })
        return detail

    def _distri_credit_form(self, user):
        if self.sudo().distri_supplier_payment_id:
            return None
        return super()._distri_credit_form(user)


def may_offer_supplier(user):
    """The upload page shows the option (the server checks again)."""
    return bool(user.has_group("distri_pedido.grupo_pago_proveedor"))
