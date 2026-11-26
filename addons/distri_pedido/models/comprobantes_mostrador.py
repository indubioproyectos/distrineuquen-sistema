"""Receipts of a counter sale and of a depot hand-over (19.0.1.129.37, owner,
25/11/2026). Behind distri_pedido.nueva_venta (models/nueva_venta.py): with
it off nothing below runs and the depot answers exactly as 129.36.

«Entrega en depósito» (/deposito/entrega/<id>, also right after «Venta en
mostrador» and «Nueva venta › Mostrador») paid by transfer asks for the
receipt with two answers:

1. «Subir comprobante» (first, big): one or several files (photo or PDF)
   attached on the spot. Each file goes through the staff upload of
   «Subir comprobante» (models/comprobante_subir.py
   `_distri_upload_receipts`): the same checks (type, size, «Ya estaba
   cargado»), a `distri.whatsapp.message` pending review, the reading
   (OCR / PDF text) in the background and the same matching, «Para
   acreditar» and auto-credit rules (models/verificacion.py; a staff upload
   never credits by itself). The receipt names the order
   (`distri_order_ref`), so the visit's pending receipt is resolved by the
   existing rule of comprobante_pendiente.py (`_distri_resolve_from_receipt`:
   «its order's receipt»). The visit is recorded with the answer «photo».
2. «Lo manda después»: the answer «not_yet» of the driver's app. The
   pending receipt (`distri.promised.transfer`) is already at the name of
   whoever delivered (the depot "route" is the user's own, its driver_id is
   the user). With the flag on it is marked `distri_user_charge` and follows
   the driver's rules: «Vencida» from the day after the next one (the hourly
   cron, as a driver's receipt without a settlement) with a charge
   (distri.route.case «Comprobante de transferencia no rendido») on that
   user's depot box; it is resolved, and its charge closed, by the same
   rules as a driver's (the receipt arrives, the Mercado Pago report brings
   the transfer: comprobante_pendiente.py, reparto_v2.py). The user sees it
   in «Mis comprobantes a reclamar» (/deposito/reclamar, a row in Inicio).

19.0.1.129.39 (review of 129.37): the mark is put on EVERY depot pending
receipt of a transfer, whatever the answer («photo», the backend wizard's
«forwarded», «not_yet»). Before, «Subir comprobante» with any file left the
item unmarked: a file that is not a receipt kept it open forever, and one
administración rejected reopened it unmarked, so it never expired nor
charged anybody. A real receipt still resolves the item before its
deadline (no charge), and the report or a verified payment closes it.

Owner's rule: nothing here creates, edits or deletes an entry, a payment or
a reconciliation. Only receipts (messages), pending receipts and charges
(cases) are written; a receipt credits only by the existing, unchanged paths.
"""
from datetime import timedelta
from urllib.parse import quote

from odoo import api, fields, models
from odoo.exceptions import UserError

from .comprobante_pendiente import OPEN_STATES, day_label, relative_when
from .comprobante_subir import MAX_FILE_BYTES, MAX_FILES, receipt_kind
from .nueva_venta import nueva_venta_on
from .reparto import _money
from .reparto_cliente import today_ar

# the context of the depot wizard when the receipt was uploaded on the spot
CTX_UPLOAD = "distri_depot_receipt_upload"
DEPOT_VIA = "deposito"
UPLOAD_ANSWER = "photo"
LATER_ANSWER = "not_yet"
# the two answers of the page (controllers/deposito.py)
ANSWER_UPLOAD = "subir"
ANSWER_LATER = "despues"
FILE_TOO_BIG = "Es demasiado grande (más de 10 MB)."
FILE_WRONG_KIND = "No es una foto (JPG, PNG o WEBP) ni un PDF."
NO_FILE = "Elegí la foto o el PDF del comprobante, o tocá «Lo manda después»."
TOO_MANY = "Subí hasta %d archivos por vez." % MAX_FILES
USER_CASE_TEXT = (
    "%s: el comprobante de la transferencia de %s del %s no llegó a la empresa. Queda a cargo "
    "de %s, que entregó sin el comprobante, salvo que administración verifique el pago en "
    "Mercado Pago.")


def check_receipt_files(files):
    """[(name, message)] of the files that cannot be a receipt (type, size);
    raises when there are none or too many. `files`: [(name, raw)]."""
    if not files:
        raise UserError(NO_FILE)
    if len(files) > MAX_FILES:
        raise UserError(TOO_MANY)
    bad = []
    for name, raw in files:
        if len(raw or b"") > MAX_FILE_BYTES:
            bad.append((name, FILE_TOO_BIG))
        elif not receipt_kind(raw):
            bad.append((name, FILE_WRONG_KIND))
    return bad


def bad_files_text(bad):
    return "Revisá estos archivos: %s" % " · ".join("%s: %s" % (n, m) for n, m in bad)


def upload_summary(results):
    """«2 comprobantes subidos · 1 ya estaba cargado» for the page."""
    new = [r for r in results if r["status"] == "nuevo"]
    seen = [r for r in results if r["status"] == "repetido"]
    parts = []
    if new:
        parts.append("1 comprobante subido" if len(new) == 1 else
                     "%d comprobantes subidos" % len(new))
    if seen:
        parts.append("1 ya estaba cargado" if len(seen) == 1 else
                     "%d ya estaban cargados" % len(seen))
    return " · ".join(parts)


class WhatsappMessageDepotUpload(models.Model):
    _inherit = "distri.whatsapp.message"

    @api.model
    def _distri_depot_upload(self, order, files, amount=0.0, user=None):
        """The receipts of `order` uploaded at the depot: one per new file,
        through the staff upload (checks, background reading, matching),
        naming the order. `amount`: the transfer of the visit, kept as the
        typed amount of a single file (several files split it unknown).
        Returns the results of `_distri_upload_receipts`."""
        order = order.sudo()
        partner = order.partner_id.commercial_partner_id
        body = "Comprobante del pedido %s · subido por %s en la entrega en depósito" % (
            order.name, (user or self.env.user).name)
        amounts = []
        if amount and len(files) == 1:
            amounts = [("%.2f" % amount).replace(".", ",")]
        return self._distri_upload_receipts(
            files, partner=partner, via=DEPOT_VIA, amounts=amounts,
            extra={"distri_order_ref": order.name, "body": body})


class DistriDepotDeliveryUpload(models.TransientModel):
    _inherit = "distri.depot.delivery"

    def _visit_payload(self, stop):
        """The receipt was uploaded on the spot: the visit says «photo», as
        a driver's photo in the app (the files follow, controllers/
        deposito.py)."""
        payload = super()._visit_payload(stop)
        if self.env.context.get(CTX_UPLOAD) and self.transfer == "yes":
            payload["receipt_answer"] = UPLOAD_ANSWER
        return payload

    def _summary(self, stop):
        """The order's note says what happened to the receipt."""
        text = super()._summary(stop)
        if stop.transfer != "yes" or not nueva_venta_on(self.env):
            return text
        pending = "comprobante pendiente hasta que llegue."
        if self.env.context.get(CTX_UPLOAD):
            return text.replace(pending, "comprobante subido en la entrega.")
        if stop.receipt_answer == LATER_ANSWER:
            return text.replace(pending, "lo manda después, queda a cargo de %s." % (
                self.env.user.name))
        return text


class DistriPromisedTransferDepot(models.Model):
    _inherit = "distri.promised.transfer"

    distri_user_charge = fields.Boolean(
        string="A cargo de quien entregó", readonly=True, index=True, copy=False,
        help="Entrega en depósito o venta en mostrador por transferencia: si el comprobante no "
             "llega o no sirve, queda a cargo del usuario que entregó, con las reglas del "
             "repartidor.")

    # ------------------------------------------------------------ the visit

    @api.model
    def _distri_sync_from_stop(self, stop, captured):
        result = super()._distri_sync_from_stop(stop, captured)
        if nueva_venta_on(self.env) and stop.route_id.kind == "depot":
            promise = self.sudo().search([("stop_id", "=", stop.id), ("kind", "=", "done")],
                                         limit=1)
            # 19.0.1.129.39: every transfer, whatever the receipt answer
            # («photo», «forwarded» or «not_yet»): a file that is not a
            # receipt, or one administración rejects (the item reopens),
            # still expires into the charge of who delivered; a real receipt
            # or the report resolves it first, as before
            charged = stop.transfer == "yes"
            if promise and promise.state in OPEN_STATES and \
                    promise.distri_user_charge != charged:
                promise.distri_user_charge = charged
        return result

    def _distri_reopen(self):
        """19.0.1.129.39: a depot item of a transfer reopened (its receipt
        rejected or found to be a copy) is the user's charge too, also when
        it was resolved before the visit marked it."""
        result = super()._distri_reopen()
        if nueva_venta_on(self.env):
            for promise in self.sudo():
                if promise.route_kind == "depot" and (promise.kind or "done") == "done" \
                        and not promise.distri_user_charge and promise.state in OPEN_STATES \
                        and promise.stop_id.transfer == "yes":
                    promise.distri_user_charge = True
        return result

    # ------------------------------------------------------------ expiry

    @api.model
    def _distri_cron_expire(self):
        """Also the receipts at the name of who delivered at the depot: the
        same deadline as a driver's without a settlement."""
        count = super()._distri_cron_expire()
        if not nueva_venta_on(self.env):
            return count
        expired = self.sudo().search([
            ("distri_user_charge", "=", True), ("route_kind", "=", "depot"),
            ("state", "=", "open"), ("delivered_day", "<", today_ar() - timedelta(days=1)),
        ])
        expired._distri_expire()
        return count + len(expired)

    def _distri_expire(self):
        """The charge text names who delivered, not «el repartidor»."""
        fresh = self.sudo().filtered(lambda p: p.distri_user_charge and not p.charge_case_id)
        result = super()._distri_expire()
        for promise in fresh:
            case = promise.charge_case_id
            if case:
                case.candidates = USER_CASE_TEXT % (
                    "%s · %s" % (promise.partner_id._distri_app_name(), promise.order_id.name),
                    _money(promise.amount), day_label(promise.delivered_day),
                    promise.driver_id.name or "quien entregó")
        return result

    # ------------------------------------------------------------ «Mis comprobantes a reclamar»

    @api.model
    def _distri_user_items(self, user):
        """The user's own pending receipts of the depot (open or expired)
        and the ones whose charge is still open."""
        return self.sudo().search([
            ("driver_id", "=", user.id), ("route_kind", "=", "depot"),
            ("distri_user_charge", "=", True),
            "|", ("state", "in", OPEN_STATES),
            "&", ("charge_case_id.state", "=", "open"), ("charge_case_id.replenished", "=", False),
        ], order="captured_at, id")

    @api.model
    def _distri_user_summary(self, user):
        """(count, total) for the row in Inicio; (0, 0) with the flag off."""
        if not nueva_venta_on(self.env):
            return 0, 0.0
        items = self._distri_user_items(user)
        return len(items), sum(items.mapped("amount"))

    def _distri_user_row(self, now=None):
        self.ensure_one()
        promise = self.sudo()
        case = promise.charge_case_id
        charged = bool(case and case.state == "open")
        # 19.0.1.129.48 (owner): claiming is a request to help, so the list
        # no longer says «cargo a tu nombre»
        if promise.state == "overdue":
            state = "Vencido desde el %s" % day_label(promise.overdue_since)
        elif promise.state == "fulfilled" and charged:
            state = "Llegó el comprobante · falta que administración lo verifique"
        else:
            state = "Pendiente · vence el %s" % day_label(
                (promise.delivered_day or today_ar()) + timedelta(days=2))
        digits = promise._whatsapp_digits() if promise.state in OPEN_STATES else ""
        claim = self._distri_customer_claim(promise, now=now, voice="company") if digits else ""
        return {
            "id": promise.id,
            "cliente": promise.partner_id._distri_app_name() or promise.partner_id.name or "",
            "pedido": promise.order_id.name or "",
            "importe": promise.amount,
            "cuando": relative_when(promise.captured_at, now),
            "estado": state,
            "vencido": promise.state == "overdue" or charged,
            "abierto": promise.state in OPEN_STATES,
            "wa_url": "https://wa.me/%s?text=%s" % (digits, quote(claim)) if digits else "",
        }

    def _distri_check_user_item(self, user):
        """The user's own open item, or a UserError."""
        self.ensure_one()
        promise = self.sudo()
        if not (promise.distri_user_charge and promise.route_kind == "depot"
                and promise.driver_id == user):
            raise UserError("Ese comprobante no está a tu nombre.")
        if promise.state not in OPEN_STATES:
            raise UserError("Ese comprobante ya llegó.")
        return promise
