"""Staff upload with the customer and the credit in one step (19.0.1.126.1).

Found testing 19.0.1.126.0 in practice: a receipt could be uploaded at
/comprobantes without a customer, and uploading and crediting were two
separate steps («Ver comprobante» › «Acreditar»), so receipts with a customer
and an amount stayed uncredited and nobody was told. Owner decision:

- The customer is mandatory on the staff upload (the screen and «Compartir
  con Distri», which opens the same form). If the customer is not in the
  system the uploader checks «No está en el sistema» and types who sent it
  (`distri_sender_note`): the receipt waits «Pendiente de asignación», no
  customer is created, and administración, gerencia or the admin assign it
  («Elegir cliente») and credit it (models/acreditar.py,
  `_distri_unknown_sender`).
- With a customer, «Subir y acreditar» uploads and credits each new file at
  once with `_distri_credit` (same defenses, same warnings). A warning leaves
  the receipt uploaded and pending; «Acreditar igual» credits it without
  uploading it again (/comprobantes/<id>/acreditar). Who does not credit
  only uploads.
- The system SUGGESTS customers from what it reads (payer CUIT or a learned
  identity, an order or invoice number in the text, the payer name with less
  confidence; `_distri_partner_suggestions`), before the upload
  (/comprobantes/sugerir reads the chosen file without storing it) and on the
  receipt afterwards. A suggestion never becomes the customer by itself: the
  reading of a staff upload without customer only fills
  `distri_suggested_partner_id` (models/cobros.py
  `_distri_partner_suggest_only`).
- The uploader manages his own upload, the same day and while the MP report
  has not verified it; administración, gerencia and the admin always
  (`_distri_upload_manage_problem`):
  · «Cancelar» (soft): the receipt is «Descartado» with «Cancelado por X:
    motivo», image kept, its key freed so the same file can be uploaded
    again (CANCEL_KEY; the duplicate check skips it). A credited receipt is
    first undone (payment cancelled by the card's action_undo);
  · «Cambiar foto»: a new file for the same receipt, read again (duplicate
    bytes, operation twins); a credit is undone first;
  · «Girar» ↺ ↻: the stored photo is turned and read again.

New column distri_sender_note (19.0.1.128.0 removed the rollout guard).
"""
import hashlib
import io
import logging
import re
from datetime import timezone

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools.pdf import PdfReader

from .acreditar import CreditWarning, can_approve, can_credit
from .cobros import _ORDER_RE, own_account
from .comprobante_subir import (
    _PIL_FORMAT, MAX_FILE_BYTES, UPLOAD_KEY, file_key, receipt_kind, rotation,
    upright_image,
)
from .ocr_comprobante import (
    CONFIDENCE_HIGH, OCR_GAVE_UP, ocr_deadline, ocr_gave_up, ocr_seconds, parse_receipt,
    pdf_images, read_image,
)
from .paquete_v127 import invoice_suggestions
from .reparto import _money
from .reparto_cliente import AR_TZ, today_ar
from .sugerencia_cliente import note_tokens

_logger = logging.getLogger(__name__)

SENDER_NOTE_MAX = 200
CANCEL_REASON_MAX = 200
SUGGESTIONS_MAX = 3
# «0003-00001234»: point of sale and number of an invoice
_INVOICE_RE = re.compile(r"\b(\d{4,5})-(\d{8})\b")

CUSTOMER_MISSING = ("Elegí el cliente. Si no está en el sistema, marcá «No está en el sistema» y "
                    "escribí quién lo mandó.")
NOTE_MISSING = ("Escribí quién mandó el comprobante (nombre, negocio o teléfono): sin eso "
                "administración no puede asignarlo.")
PENDING_ASSIGNMENT = "Pendiente de asignación: no se acreditó (falta el cliente en el sistema)."
NO_CREDIT_RIGHT = ("Subido, pendiente: no se acreditó (tu usuario no acredita comprobantes; lo "
                   "acredita administración).")
UPLOADED = "Subido. Lo revisa administración."
CREDITED = "Acreditado: %s a %s."
NOT_CREDITED = "Subido, pendiente: no se acreditó. %s"
# what a new reading recomputes (replace or turn the photo)
READING_RESET = {
    "distri_operation": False, "distri_receipt_date": False, "distri_read_amount": 0.0,
    "distri_file_text": False, "distri_payer_name": False, "distri_payer_cuit": False,
    "distri_dest_name": False, "distri_dest_cuit": False, "distri_destination": False,
    "distri_bank": False, "distri_payer_own": False, "distri_conflict": False,
    "distri_receipt_note": False, "media_state": False, "media_error": False,
}


class WhatsappMessageStaffUpload(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_sender_note = fields.Char(
        string="Lo mandó (no está en el sistema)", readonly=True, copy=False,
        help="Quien lo subió marcó «No está en el sistema» y escribió quién lo mandó. Asignalo "
             "con «Elegir cliente» y después acreditalo.")

    def _distri_sender_note_text(self):
        self.ensure_one()
        return self.sudo().distri_sender_note or ""

    # ------------------------------------------------------------ suggestions

    @api.model
    def _distri_partner_suggestions(self, payer_cuit="", text="", payer_name=""):
        """Up to SUGGESTIONS_MAX customers, best first: [{id, name, reason}].
        Only customers the user may pick (no generic, wholesale only for who
        sees them)."""
        Partner = self.env["res.partner"].sudo()
        Cash = self.env["distri.cash.collection"]
        out, seen = [], set()

        def add(partners, reason):
            for partner in partners.commercial_partner_id:
                if len(out) >= SUGGESTIONS_MAX or partner.id in seen \
                        or partner.distri_generic_partner or not Cash._cash_visible(partner):
                    continue
                seen.add(partner.id)
                out.append({"id": partner.id, "name": partner.name or "", "reason": reason})

        if payer_cuit:
            add(Partner._distri_partners_by_cuit(payer_cuit), "por CUIT")
        for ref in dict.fromkeys(_ORDER_RE.findall(text or "")):
            order = self.env["sale.order"].sudo().search([("name", "=", ref)], limit=1)
            if order:
                add(order.partner_id, "por pedido %s" % order.name)
        for point, number in dict.fromkeys(_INVOICE_RE.findall(text or "")):
            move = self.env["account.move"].sudo().search([
                ("move_type", "=", "out_invoice"), ("state", "=", "posted"),
                ("name", "ilike", "%s-%s" % (point, number))], limit=1)
            if move:
                add(move.partner_id, "por factura %s" % move.name)
        # 19.0.1.127.0: «0001/2026/07963» and a bare 5-digit tail, lab and Online
        for partners, reason in invoice_suggestions(self.env, text):
            add(partners, reason)
        words = note_tokens(payer_name or "")
        if words and len(out) < SUGGESTIONS_MAX:
            ids = Partner._distri_name_index().match(words)
            if 0 < len(ids) <= SUGGESTIONS_MAX:
                add(Partner.browse(sorted(ids)), "por nombre")
        return out

    def _distri_stored_suggestions(self):
        """The suggestions of an uploaded receipt still without customer."""
        self.ensure_one()
        msg = self.sudo()
        if msg.distri_partner_id:
            return []
        found = self._distri_partner_suggestions(
            msg.distri_payer_cuit, "\n".join(t for t in (msg.distri_file_text, msg.distri_order_ref)
                                             if t), msg.distri_payer_name)
        if msg.distri_suggested_partner_id and msg.distri_suggested_partner_id.id not in {
                s["id"] for s in found}:
            found.insert(0, {"id": msg.distri_suggested_partner_id.id,
                             "name": msg.distri_suggested_partner_id.name,
                             "reason": (msg.distri_suggestion_note or "").lower() or "sugerido"})
        return found[:SUGGESTIONS_MAX]

    @api.model
    def _distri_read_raw(self, raw, degrees=0):
        """(text, reading) of a file not stored yet: what the upload screen
        reads before «Subir» to suggest the customer and the amount."""
        kind = receipt_kind(raw)
        if not kind:
            return "", {}
        now = fields.Datetime.now().replace(tzinfo=timezone.utc).astimezone(AR_TZ)
        context = {"reference": now.replace(tzinfo=None), "own": own_account(self.env)}
        if kind[0] == "application/pdf":
            try:
                reader = PdfReader(io.BytesIO(raw))
                text = "\n".join((page.extract_text() or "") for page in reader.pages[:3])
            except Exception:
                text = ""
            reading = parse_receipt(text, **context) if text.strip() else {}
            if not reading.get("amount"):
                for png in pdf_images(raw):
                    scanned_text, _confidence, scanned = read_image(png, **context)
                    if scanned.get("amount"):
                        return scanned_text, scanned
            return text, reading
        text, _confidence, reading = read_image(upright_image(raw, kind[0], degrees), **context)
        return text, reading

    @api.model
    def _distri_upload_preview(self, raw, degrees=0):
        """{amount, amount_text, amount_sure, suggestions, gave_up, notice} of a
        chosen file. 19.0.1.128.0: the OCR gives up after `ocr_seconds`
        («gave_up», the person types the amount)."""
        if not raw or len(raw) > MAX_FILE_BYTES or not receipt_kind(raw):
            return {"amount": 0.0, "amount_text": "", "amount_sure": False, "suggestions": [],
                    "gave_up": False, "notice": "", "date": "", "dest_cuit": "", "dest_name": ""}
        with ocr_deadline(ocr_seconds(self.env)):
            try:
                text, reading = self._distri_read_raw(raw, rotation(degrees))
            except Exception:
                _logger.warning("Receipt preview could not be read", exc_info=True)
                text, reading = "", {}
        gave_up = ocr_gave_up()
        amount = reading.get("amount") or 0.0
        return {
            "gave_up": gave_up,
            "notice": OCR_GAVE_UP if gave_up and not amount else "",
            "amount": amount,
            "amount_text": ("%.2f" % amount).replace(".", ",").replace(",00", "") if amount else "",
            "amount_sure": bool(amount and reading.get("amount_confidence") == CONFIDENCE_HIGH),
            "suggestions": self._distri_partner_suggestions(
                reading.get("payer_cuit") or "", text, reading.get("payer_name") or ""),
            # 19.0.1.129.14: the prefill of «Es un pago a proveedor» (the date
            # read and the destination; models/pago_proveedor_subir.py keeps
            # the destination only for grupo_pago_proveedor)
            "date": reading["date"].date().isoformat() if reading.get("date") else "",
            "dest_cuit": reading.get("dest_cuit") or "",
            "dest_name": reading.get("dest_name") or "",
        }

    # ------------------------------------------------------------ upload and credit

    @api.model
    def _distri_staff_upload(self, files, partner=None, unknown=False, sender_note="",
                             credit=False, via="pantalla", amounts=None, rotations=None):
        """The staff upload of 19.0.1.126.1 (see the module docstring). Each
        result of `_distri_upload_receipts` gains «credit»: «acreditado»,
        «aviso» (warnings to confirm), «pendiente», «asignacion» or ""."""
        partner = partner.commercial_partner_id if partner else self.env["res.partner"]
        sender_note = " ".join(str(sender_note or "").split())[:SENDER_NOTE_MAX]
        # what was refused before keeps its message (no file, a wrong amount)
        if not files:
            raise UserError("Elegí al menos una foto o un PDF del comprobante.")
        amounts = list(amounts or [])
        for n, (name, _raw) in enumerate(files):
            self._distri_typed_amount(amounts[n] if n < len(amounts) else "",
                                      (name or "comprobante")[:120])
        if unknown:
            if not sender_note:
                raise UserError(NOTE_MISSING)
            partner = self.env["res.partner"]
        elif not partner:
            raise UserError(CUSTOMER_MISSING)
        else:
            sender_note = ""
        results = self._distri_upload_receipts(files, partner=partner, via=via, amounts=amounts,
                                               rotations=rotations,
                                               sender_note=sender_note or None,
                                               complete_own=True)
        may_credit = can_credit(self.env.user)
        for result in results:
            result.setdefault("credit", "")
            if result["status"] != "nuevo":
                continue
            msg = self.browse(result["id"])
            if unknown:
                result.update(credit="asignacion", message=PENDING_ASSIGNMENT)
            elif not credit:
                result["message"] = UPLOADED
            elif not may_credit:
                result.update(credit="pendiente", message=NO_CREDIT_RIGHT)
            else:
                result.update(msg._distri_upload_credit_now(partner))
        return results

    def _distri_upload_credit_now(self, partner, amount=None, confirmed=False):
        """Credits this just uploaded receipt to `partner`: the amount given,
        else the typed one, else the one read. Never raises: returns
        {credit, message, warnings?, amount_text?, partner_id?}."""
        self.ensure_one()
        user = self.env.user
        msg = self.sudo()
        msg._distri_read_now()
        card = msg._distri_credit_card(user)
        if not card:
            msg.invalidate_recordset()
            state = dict(msg._fields["distri_receipt_state"].selection).get(
                msg.distri_receipt_state, "")
            reason = "El comprobante quedó «%s»%s." % (
                state, (": %s" % msg.distri_receipt_note) if msg.distri_receipt_note else "") \
                if msg.distri_receipt_state != "pending" else \
                "No se puede acreditar desde acá: lo revisa administración."
            return {"credit": "pendiente", "message": NOT_CREDITED % reason}
        if not amount:
            read = msg.distri_read_amount if msg.distri_amount_confidence == CONFIDENCE_HIGH else 0
            amount = msg.distri_amount_typed or read or msg.distri_amount
        if not amount:
            return {"credit": "pendiente", "message": NOT_CREDITED % (
                "Falta el importe: abrilo, escribilo y acreditalo.")}
        Collection = self.env["distri.mp.collection"]
        try:
            with self.env.cr.savepoint():
                Collection.browse(card.id)._distri_credit(partner, amount, confirmed=confirmed)
        except CreditWarning as exc:
            return {"credit": "aviso", "warnings": exc.warnings,
                    "message": "Subido, falta confirmar antes de acreditar %s a %s." % (
                        _money(amount), partner.name),
                    "amount_text": ("%.2f" % amount).replace(".", ","),
                    "partner_id": partner.id, "partner_name": partner.name}
        except UserError as exc:
            return {"credit": "pendiente",
                    "message": NOT_CREDITED % (exc.args[0] if exc.args else str(exc))}
        return {"credit": "acreditado", "message": CREDITED % (_money(amount), partner.name)}

    # ------------------------------------------------------------ manage an upload

    def _distri_upload_credit_state(self):
        """(card, credited, verified) of the receipt."""
        self.ensure_one()
        card = self.sudo()._distri_verify_card()
        credited = bool(card and card.state == "confirmed"
                        and card.credited_at)
        verified = bool(credited and (card.income_id or card.audit_state == "verificado"))
        return card, credited, verified

    def _distri_upload_manage_problem(self, user):
        """Why `user` may not cancel, replace or turn this upload, or ""."""
        self.ensure_one()
        msg = self.sudo()
        if not msg.distri_upload or not msg.distri_forwarded_by_id:
            return "Solo se cambian los comprobantes subidos por el personal."
        if msg._distri_upload_cancelled():
            return "Este comprobante ya está cancelado."
        _card, credited, verified = msg._distri_upload_credit_state()
        if msg.distri_receipt_state not in ("pending", "no_money") and not credited:
            state = dict(msg._fields["distri_receipt_state"].selection).get(
                msg.distri_receipt_state, msg.distri_receipt_state or "")
            return "El comprobante está «%s»: lo resuelve administración en Cobros." % state
        if can_approve(user):
            return ""
        if msg.distri_forwarded_by_id != user:
            return ("Solo quien lo subió, o administración, gerencia o el admin, pueden "
                    "cambiarlo.")
        if verified:
            return ("Ya está verificado con Mercado Pago (✓ Pago confirmado): lo cancelan o lo "
                    "cambian administración, gerencia o el admin.")
        created = msg.create_date.replace(tzinfo=timezone.utc).astimezone(AR_TZ).date() \
            if msg.create_date else None
        if created != today_ar():
            return ("Solo el mismo día en que lo subiste; después lo hacen administración, "
                    "gerencia o el admin.")
        return ""

    def _distri_upload_check_manage(self):
        problem = self._distri_upload_manage_problem(self.env.user)
        if problem:
            raise UserError(problem)

    def _distri_upload_undo_credit(self):
        """Undoes the credit of this receipt (its card's action_undo cancels
        the payment). Returns the sentence for the user, or ""."""
        self.ensure_one()
        card, credited, _verified = self._distri_upload_credit_state()
        if not credited:
            return ""
        amount, partner = card.amount, card.partner_id.commercial_partner_id
        # the permission was checked by `_distri_upload_check_manage`
        card.sudo().action_undo()
        if card.state == "confirmed":
            raise UserError("No se pudo anular la acreditación.")
        return "Se anuló la acreditación de %s a %s." % (_money(amount), partner.name or "")

    def _distri_upload_cancel(self, reason):
        """«Cancelar»: soft, the image stays (see the module docstring)."""
        self.ensure_one()
        reason = " ".join(str(reason or "").split())[:CANCEL_REASON_MAX]
        if not reason:
            raise UserError("Escribí por qué lo cancelás.")
        self._distri_upload_check_manage()
        user = self.env.user
        msg = self.sudo()
        undone = msg._distri_upload_undo_credit()
        note = "Cancelado por %s: %s" % (user.name, reason)
        # 19.0.1.126.2: the same helper frees a key left behind (lazy path)
        msg._distri_upload_free_key()
        msg._distri_close("dismissed", note)
        card = msg._distri_verify_card()
        if card:
            card.message_post(body=note)
        _logger.info("Uploaded receipt %s cancelled by %s", msg.id, user.login)
        return " ".join(p for p in (undone, "Comprobante cancelado.") if p)

    def _distri_upload_reread(self):
        """Reads the stored file again now, as if it had just arrived."""
        msg = self.sudo()
        vals = dict(READING_RESET)
        if msg.distri_amount_confidence != "manual":
            vals.update(distri_amount=0.0, distri_amount_confidence=False)
        if not msg.distri_partner_id:
            vals.update(distri_suggested_partner_id=False, distri_suggestion_note=False)
        if msg.distri_receipt_state != "pending":
            vals["distri_receipt_state"] = "pending"
        msg.with_context(distri_receipt_matching=True).write(vals)
        msg.with_context(distri_reread=True)._distri_read_now()
        if not msg.media_state:
            # no file to read: still the checks of an arrival
            msg._distri_read_receipt()

    def _distri_upload_replace(self, name, raw, degrees=0):
        """«Cambiar foto»: a new file for the same receipt, read again."""
        self.ensure_one()
        self._distri_upload_check_manage()
        msg = self.sudo()
        kind = receipt_kind(raw)
        if len(raw or b"") > MAX_FILE_BYTES:
            raise UserError("La foto es demasiado grande (más de 10 MB).")
        if not kind:
            raise UserError("No es una foto (JPG, PNG o WEBP) ni un PDF.")
        existing = self._distri_upload_duplicate(raw)
        if existing == msg:
            raise UserError("Es la misma foto que ya tiene.")
        if existing:
            raise UserError("Esa foto ya está cargada (%s): no se usa para reemplazar." % (
                existing._distri_upload_label()))
        undone = msg._distri_upload_undo_credit()
        mime, message_type, ext = kind
        name = (name or "comprobante")[:120]
        filename = name if name.lower().endswith("." + ext) else "%s.%s" % (name, ext)
        stored = upright_image(raw, mime, rotation(degrees))
        old = msg.attachment_id
        if old:
            # kept (never deleted), no longer the receipt's file
            old.write({"name": "reemplazada-%s" % (old.name or "comprobante")})
        attachment = self.env["ir.attachment"].sudo().create({
            "name": filename, "raw": stored, "mimetype": mime,
            "res_model": msg._name, "res_id": msg.id})
        msg.with_context(distri_receipt_matching=True).write({
            "wa_message_id": UPLOAD_KEY % file_key(raw),
            "message_type": message_type, "media_mime_type": mime, "media_filename": filename,
            "distri_original_checksum": hashlib.sha1(raw).hexdigest() if stored != raw else False,
            "attachment_id": attachment.id,
        })
        msg._distri_upload_reread()
        _logger.info("Uploaded receipt %s: photo replaced by %s", msg.id, self.env.user.login)
        return " ".join(p for p in (undone, "Foto cambiada: se leyó de nuevo.") if p)

    def _distri_upload_rotate(self, degrees):
        """«Girar» ↺ ↻ on «Ver comprobante»: the stored photo, read again."""
        self.ensure_one()
        self._distri_upload_check_manage()
        msg = self.sudo()
        if msg._distri_upload_credit_state()[1]:
            raise UserError("Ya está acreditado: para corregir la foto usá «Cambiar foto» o "
                            "cancelalo.")
        attachment = msg.attachment_id
        if not attachment or (attachment.mimetype or "") not in _PIL_FORMAT:
            raise UserError("Solo se pueden girar las fotos (JPG, PNG o WEBP).")
        turned = upright_image(attachment.raw, attachment.mimetype, rotation(degrees))
        if turned == attachment.raw:
            raise UserError("No se pudo girar la foto.")
        original = msg.distri_original_checksum or attachment.checksum \
            or hashlib.sha1(attachment.raw).hexdigest()
        attachment.write({"raw": turned})
        msg.with_context(distri_receipt_matching=True).write(
            {"distri_original_checksum": original})
        msg._distri_upload_reread()
        return "Foto girada: se leyó de nuevo."

    # ------------------------------------------------------------ rows and detail

    def _distri_upload_status_label(self):
        """What «Subidos hoy por vos» says of the receipt (19.0.1.126.1)."""
        self.ensure_one()
        msg = self.sudo()
        if msg._distri_upload_cancelled():
            return "Cancelado"
        if msg._distri_upload_credit_state()[1]:
            return "Acreditado"
        if not msg.distri_partner_id and msg._distri_sender_note_text() \
                and msg.distri_receipt_state == "pending":
            return "Pendiente de asignación"
        return ""

    def _distri_upload_row(self):
        row = super()._distri_upload_row()
        label = self._distri_upload_status_label()
        if label:
            row["state_label"] = label
        row["sender_note"] = self._distri_sender_note_text()
        row["can_manage"] = not self._distri_upload_manage_problem(self.env.user)
        return row

    def _distri_upload_detail(self):
        detail = super()._distri_upload_detail()
        msg = self.sudo()
        user = self.env.user
        problem = msg._distri_upload_manage_problem(user)
        detail.update({
            "sender_note": msg._distri_sender_note_text(),
            "suggestions": msg._distri_stored_suggestions() if can_approve(user) else [],
            "manage": not problem,
            "manage_problem": problem,
            "credited": msg._distri_upload_credit_state()[1],
            "cancel_note": msg.distri_receipt_note if msg._distri_upload_cancelled() else "",
        })
        return detail


class DistriMpCollectionSenderNote(models.Model):
    """The board card shows who sent a receipt uploaded with «No está en el
    sistema» (computed and guarded: no column of its own)."""
    _inherit = "distri.mp.collection"

    receipt_sender_note = fields.Char(string="Lo mandó (no está en el sistema)",
                                      compute="_compute_receipt_sender_note")

    @api.depends("receipt_id")
    def _compute_receipt_sender_note(self):
        for card in self:
            card.receipt_sender_note = (card.sudo().receipt_id.distri_sender_note or False) \
                if card.receipt_id else False
