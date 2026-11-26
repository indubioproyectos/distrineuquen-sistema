"""«Tablero de verificación», first slice (19.0.1.129.31, plan unificado
Etapa 5; proposal «Tablero de verificación» of 23/11/2026, owner's answers
of 24/11/2026).

Everything is behind the parameter distri_pedido.verificacion_v2, OFF by
default: with it off nothing here runs, no screen, no tile, and every
receipt is matched, closed and credited exactly as before.

1. The customer's receipt credits itself only on a sure match with the
   Mercado Pago report, and a person always checks it afterwards
   (`_distri_v2_auto_credit`, decisions 1 and 3). Only when the report
   arrived first (the income was loaded before the receipt), only for a
   receipt the customer sent (WhatsApp or his order page: no staff, driver,
   chat import, typed sender, conflict or possible twin) and only when:

   - the receipt is tied to the pending income and names its customer;
   - the operation number is the report's exactly, OR the amount is the
     report's (±AMOUNT_TOLERANCE) and the time read on the receipt is
     within RECEIPT_TIME_CLOSE (2 minutes) of the transfer;
   - the amount is not round (`is_round_amount`): a multiple of
     PARAM_ROUND_STEP (default $ 10.000) always goes to a person;
   - the destination is the own account and the receipt is from today or
     yesterday;
   - every guard of a confirmation passes (`_confirm_todo`).

   The income is confirmed (one payment, as «Subir y acreditar» after the
   report does since 19.0.1.129.13) and the receipt's card is left
   credited and verified with MP, marked `auto_credited` and
   `human_check` «pendiente»: queue «Acreditados solos · a revisar» until
   administración, gerencia or Ale press «Revisado».
   Only receipts that arrived after the flag was turned on
   (PARAM_SINCE, written by scripts/correcciones/
   v129_31_activar_verificacion.sh): nothing already loaded is credited
   by turning it on.

2. Round amounts in the staff's one-step crediting (19.0.1.129.13
   `_distri_upload_sure_income`): a round amount matched only by amount and
   time waits for a person; the exact operation number still credits.

3. WhatsApp chat import (zip) receipts never credit themselves: they stay
   in «Para acreditar» (`_distri_v2_auto_credit` refuses
   `distri_chat_import_id`).

4. Twin guard: a receipt WITHOUT an operation number whose amount, date and
   time are another receipt's was closed as «Ya recibido» on sight
   (models/cobros.py `_distri_classify`). Two real payments of the same
   amount in the same minute became one. With the flag it is marked
   `distri_twin_review` (queue «Posibles duplicados»), is neither matched
   nor credited by itself, and a person decides: «Es el mismo pago» (the
   old closing) or «Son dos pagos» (`distri_twin_ok`, never flagged again).

5. «Histórico»: `historic` on the pending incomes of the report before the
   cut-off (PARAM_HISTORIC_BEFORE, default 15/09/2026), set only by
   scripts/correcciones/v129_31_marcar_historicos.sh (dry run by default,
   refused with the flag off). Nothing is deleted or confirmed: the board
   hides them with its default filter «Sin históricos» and they go to the
   Sistemas queue.

The queues (QUEUES) only read existing records and states: no model of
their own. Each has an owner group, a term and a count on Inicio; Ale
(user «admin» or Sistemas, grupo_ingresos_reporte) is a member of all.
"""
import logging
from datetime import datetime, timedelta

import psycopg2

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError
from odoo.tools.safe_eval import safe_eval

from .acreditar import can_approve
from .cobros import AMOUNT_TOLERANCE, RECEIPT_TIME_CLOSE, digits, local_day, local_when, own_account
from .ingresos_reporte import INCOME_GROUP
from .ocr_comprobante import CONFIDENCE_HIGH
from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

PARAM_VERIFICACION = "distri_pedido.verificacion_v2"
# the moment the flag was turned on: receipts before it never credit themselves
PARAM_SINCE = "distri_pedido.verificacion_v2_desde"
# a round amount: an exact multiple of this (default $ 10.000)
PARAM_ROUND_STEP = "distri_pedido.verificacion_importe_redondo"
DEFAULT_ROUND_STEP = 10000.0
# «En el reporte sin comprobante»: days without the receipt (owner: 2)
PARAM_NO_RECEIPT_DAYS = "distri_pedido.verificacion_sin_comprobante_dias"
DEFAULT_NO_RECEIPT_DAYS = 2
# «Histórico»: pending incomes before this day (owner: 15/09/2026)
PARAM_HISTORIC_BEFORE = "distri_pedido.verificacion_historico_antes"
DEFAULT_HISTORIC_BEFORE = "2026-09-15"
ON_VALUES = ("1", "true", "yes", "si", "sí", "on")
# Owner, 26/11/2026: until the accounts audit closes the board is Ale's only
# (Sistemas / admin). With it on, administración and gerencia lose the tile,
# the queues and the board's actions; the crediting rules above keep running.
PARAM_OWNER_ONLY = "distri_pedido.verificacion_solo_sistemas"

ADMIN = "distri_pedido.grupo_administracion"
GERENCIA = "distri_pedido.grupo_gerencia"

HUMAN_CHECK = [("pendiente", "Falta revisar"), ("revisado", "Revisado")]
AUTO_CREDITED_NOTE = ("Acreditado solo: el reporte de Mercado Pago ya traía la transferencia (%s, "
                      "%s). Falta que lo revise una persona.")
TWIN_NOTE = ("Posible duplicado: mismo importe, fecha y hora que el comprobante del %s (%s), sin "
             "número de operación. Pueden ser dos pagos: decidí en «Posibles duplicados».")
TWIN_CREDIT_WARNING = ("Puede ser el mismo pago que el comprobante del %s (mismo importe, fecha y "
                       "hora, sin número de operación). Acreditalo solo si son dos pagos.")
TWIN_CREDITED_ERROR = ("Puede ser el mismo pago que el comprobante del %s, que ya se acreditó "
                       "(mismo importe, fecha y hora, sin número de operación). Primero "
                       "administración tiene que marcar «Son dos pagos».")

# (key, number, title, what comes in, owner groups, who, term in hours)
# owner groups: who handles it (gerencia implies administración); () = only
# Ale. term None: it only informs.
QUEUES = (
    ("para_acreditar", "1", "Para acreditar",
     "Comprobantes sin acreditar: los del cliente, los del zip de WhatsApp y los ingresos del "
     "reporte que ya tienen su comprobante.",
     (ADMIN,), "Administración", 24),
    ("revision_humana", "2", "Acreditados solos · a revisar",
     "Comprobantes del cliente que se acreditaron solos con el reporte. Siempre los revisa una "
     "persona.",
     (ADMIN,), "Denis, Gonzalo o Ale", 24),
    ("esperando_reporte", "3", "Acreditado · esperando reporte",
     "Lo acreditado que todavía no cruzó con el reporte de Mercado Pago. Solo informa.",
     (ADMIN,), "Nadie: solo informa", None),
    ("sin_transferencia", "4", "Acreditado sin transferencia",
     "El reporte ya cubre ese momento y la plata no apareció.",
     (GERENCIA,), "Gerencia · «Revisado» o «Anular pago»", 24),
    ("diferencias", "5", "Diferencias",
     "Importes distintos, reversiones, varias candidatas, comprobante de otro cliente.",
     (ADMIN,), "Administración vincula, gerencia anula", 48),
    ("duplicados", "6", "Posibles duplicados",
     "Comprobantes iguales sin número de operación y pagos cargados a mano que pueden ser la "
     "misma transferencia.",
     (ADMIN,), "Administración revisa, gerencia anula", 48),
    ("sin_comprobante", "7", "En el reporte sin comprobante",
     "Ingresos con cliente probable que siguen sin comprobante. Se reclama por WhatsApp; "
     "aprobar sin comprobante: solo Sistemas.",
     (ADMIN,), "Administración y el vendedor reclaman", 48),
    ("historicos", "8", "Históricos y migración",
     "Ingresos marcados «histórico» y las tarjetas «Falta el comprobante» del tablero de "
     "migración.",
     (), "Solo Sistemas", None),
    # 19.0.1.129.38 (decision 2 of 24/11/2026): a delivery «Ya estaba pagado
    # · a confirmar» whose receipt the report did not bring in 48 h
    ("entregados_a_confirmar", "9", "Entregados a confirmar",
     "Pedidos entregados como «Ya estaba pagado · a confirmar» con un comprobante sin "
     "acreditar: pasaron 48 h y el reporte de Mercado Pago no trae la transferencia.",
     (ADMIN,), "Denis y Ale", 24),
)
QUEUE_KEYS = tuple(q[0] for q in QUEUES)
LIST_LIMIT = 50


def _partner_url(env, partner, user):
    """The customer an income hidden on the board opens: the Odoo form, or
    with distri_pedido.ui_v2 on (19.0.1.129.41) the kit's customer screen for
    whoever uses it (controllers/fichas.py)."""
    from ..controllers.fichas import may_use, record_link
    if may_use(user):
        return record_link(env, "cliente", partner.id)
    return "/odoo/res.partner/%s" % partner.id


def _param(env, key, default=None):
    return env["ir.config_parameter"].sudo().get_param(key) or default


def verificacion_on(env):
    """19.0.1.129.31: the verification board and its rules."""
    return str(_param(env, PARAM_VERIFICACION, "0")).strip().lower() in ON_VALUES


def round_step(env):
    try:
        step = float(_param(env, PARAM_ROUND_STEP, DEFAULT_ROUND_STEP))
    except (TypeError, ValueError):
        step = DEFAULT_ROUND_STEP
    return step if step > 0 else DEFAULT_ROUND_STEP


def is_round_amount(amount, step=DEFAULT_ROUND_STEP):
    """A round amount: a positive exact multiple of `step` (cents count:
    $ 20.000,50 is not round). $ 10.000, $ 50.000 and $ 120.000 are round
    with the default step; $ 15.000 and $ 9.990 are not."""
    if not amount or amount < step:
        return False
    cents = round(amount * 100)
    return cents % round(step * 100) == 0


def no_receipt_days(env):
    try:
        days = int(_param(env, PARAM_NO_RECEIPT_DAYS, DEFAULT_NO_RECEIPT_DAYS))
    except (TypeError, ValueError):
        days = DEFAULT_NO_RECEIPT_DAYS
    return max(days, 0)


def historic_before(env):
    value = _param(env, PARAM_HISTORIC_BEFORE, DEFAULT_HISTORIC_BEFORE)
    try:
        return fields.Date.to_date(value)
    except ValueError:
        return fields.Date.to_date(DEFAULT_HISTORIC_BEFORE)


def flag_since(env):
    """The moment the flag was turned on (UTC, naive), or None."""
    value = _param(env, PARAM_SINCE)
    try:
        return fields.Datetime.to_datetime(value) if value else None
    except ValueError:
        return None


def is_owner(user):
    """Ale: the user «admin» or Sistemas (grupo_ingresos_reporte), member of
    every queue (owner, 24/11/2026)."""
    return bool(user) and not user.share and (
        user._is_superuser() or user.login == "admin" or user.has_group(INCOME_GROUP))


def owner_only(env):
    return str(_param(env, PARAM_OWNER_ONLY, "0")).strip().lower() in ON_VALUES


def queue_member(user, key):
    """`user` handles (or may see) the queue `key`."""
    if is_owner(user):
        return True
    if user and owner_only(user.env):
        return False
    groups = dict((q[0], q[4]) for q in QUEUES).get(key, ())
    return bool(user) and not user.share and any(user.has_group(g) for g in groups)


def user_queues(user):
    return [q for q in QUEUES if queue_member(user, q[0])]


def may_check(user):
    """«Revisado» of an auto-credited receipt: Denis, Gonzalo or Ale
    (administración, gerencia, the admin)."""
    return is_owner(user) or can_approve(user)


def age_text(since, now=None):
    if not since:
        return ""
    delta = (now or fields.Datetime.now()) - since
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return "hace menos de 1 h"
    if hours < 48:
        return "hace %s h" % hours
    return "hace %s días" % (hours // 24)


class DistriMpCollectionVerificacion(models.Model):
    _inherit = "distri.mp.collection"

    auto_credited = fields.Boolean(
        string="Acreditado solo", readonly=True, copy=False, index=True,
        help="El comprobante del cliente se acreditó solo: el reporte de Mercado Pago ya traía "
             "la transferencia con una coincidencia segura.")
    human_check = fields.Selection(
        HUMAN_CHECK, string="Revisión humana", readonly=True, copy=False, index=True,
        help="Lo acreditado solo siempre lo revisa una persona: Denis, Gonzalo o Ale.")
    human_checked_by_id = fields.Many2one("res.users", string="Revisó", readonly=True,
                                          copy=False)
    human_checked_at = fields.Datetime(string="Revisado el", readonly=True, copy=False)
    historic = fields.Boolean(
        string="Histórico", readonly=True, copy=False, index=True,
        help="Ingreso pendiente anterior al 15/09/2026. No se borra ni se confirma: queda "
             "oculto por defecto y lo ve Sistemas.")

    # ------------------------------------------------------------ auto credit

    def _distri_v2_sure(self, receipt):
        """«operation» or «time» when `receipt` is surely this pending
        income's transfer, else False (decision 3)."""
        self.ensure_one()
        income = self.sudo()
        msg = receipt.sudo()
        operation = digits(msg.distri_operation)
        numbers = {digits(n) for n in (income.source_id, income.bank_transfer_id) if n}
        read = msg.distri_read_amount if msg.distri_amount_confidence == CONFIDENCE_HIGH else 0
        known = msg.distri_amount_typed or read or msg.distri_amount
        if known and abs(known - income.amount) > AMOUNT_TOLERANCE:
            return False
        if operation and operation in numbers:
            return "operation"
        if operation:
            # another number read: never by amount
            return False
        read_at = msg.distri_receipt_date
        if known and read_at and income.date and abs(income.date - read_at) <= RECEIPT_TIME_CLOSE:
            return "time"
        return False

    def _distri_v2_refusal(self, receipt):
        """Why this income is not credited by itself with `receipt` (a short
        text for the log), or "" when it may be."""
        self.ensure_one()
        income = self.sudo()
        msg = receipt.sudo()
        env = self.env
        if not verificacion_on(env):
            return "flag off"
        if income.kind != "income" or income.state != "draft" or income.receipt_id != msg:
            return "not a pending income tied to the receipt"
        if msg.distri_forwarded_by_id or msg.distri_from_app or msg.distri_app_stop_id:
            return "not sent by the customer"
        if msg.distri_chat_import_id:
            return "WhatsApp chat import: always «Para acreditar»"
        if msg.distri_sender_note or msg.distri_conflict or msg.distri_twin_review:
            return "a person decides who sent it or whether it is a copy"
        since = flag_since(env)
        if since and (msg.create_date or msg.timestamp) and (msg.create_date or msg.timestamp) < since:
            return "arrived before the flag"
        if income.create_date and msg.create_date and income.create_date > msg.create_date:
            return "the receipt came before the report"
        if not income._distri_receipt_tied():
            return "the receipt names another customer"
        if income.reversal or income.conflict_note or income.bank_twin_payment_id \
                or income.receipt_candidate or income.historic \
                or income.proposal in ("credited", "verified", "authorized"):
            return "the card needs a person"
        if is_round_amount(income.amount, round_step(env)):
            return "round amount"
        if msg._distri_other_account(own_account(env)):
            return "another account"
        day = local_day(msg.distri_receipt_date or msg.timestamp)
        if day and (today_ar() - day).days > 1:
            return "older than yesterday"
        if not income._distri_v2_sure(msg):
            return "no sure match"
        return ""

    def _distri_v2_auto_credit(self, receipt):
        """Credits the customer's `receipt` with this pending income of the
        report when the match is sure (see the module docstring). Never
        raises: returns the receipt's card, or False."""
        self.ensure_one()
        refusal = self._distri_v2_refusal(receipt)
        if refusal:
            if refusal != "flag off":
                _logger.info("Receipt %s, income %s: not credited by itself (%s)",
                             receipt.id, self.id, refusal)
            return False
        income = self.sudo()
        msg = receipt.sudo()
        level = income._distri_v2_sure(msg)
        try:
            with self.env.cr.savepoint():
                income._confirm_todo()
                income._confirm_one()
                card = msg._distri_upload_verified_card(income)
                note = AUTO_CREDITED_NOTE % (income.name, {
                    "operation": "mismo número de operación",
                    "time": "mismo importe dentro de los 2 minutos"}[level])
                card.write({"auto_credited": True, "human_check": "pendiente",
                            "proposal_note": note})
        except (UserError, AccessError, psycopg2.Error) as exc:
            _logger.info("Receipt %s, income %s: not credited by itself: %s",
                         msg.id, income.id, exc)
            return False
        card.message_post(body=note)
        partner = income.partner_id.commercial_partner_id
        partner.sudo().message_post(body="Comprobante del cliente acreditado solo: %s (%s), ya en "
                                         "el reporte de Mercado Pago (%s). Falta la revisión de "
                                         "una persona." % (_money(income.amount), card.name,
                                                           income.name))
        _logger.info("Receipt %s credited by itself with income %s (%s, payment %s)",
                     msg.id, income.id, level, income.payment_id.id)
        return card

    def action_distri_human_check(self):
        """«Revisado»: Denis, Gonzalo or Ale checked an auto-credited receipt."""
        if not may_check(self.env.user):
            raise AccessError("Lo acreditado solo lo revisan Denis, Gonzalo o Ale.")
        now = fields.Datetime.now()
        for card in self.sudo():
            if not (card.auto_credited and card.human_check == "pendiente"):
                raise UserError("%s no espera revisión." % card.name)
            card.write({"human_check": "revisado", "human_checked_by_id": self.env.user.id,
                        "human_checked_at": now})
            card.message_post(body="Revisado por %s." % self.env.user.name)
        return True

    # ------------------------------------------------------------ histórico

    @api.model
    def _distri_v2_historic_domain(self):
        """The pending incomes of the report before the cut-off (Argentine
        day), not marked yet."""
        return [("kind", "=", "income"), ("state", "=", "draft"), ("historic", "=", False),
                ("date_day", "<", historic_before(self.env))]

    @api.model
    def _distri_v2_mark_historic(self, apply=False):
        """Marks the old pending incomes «histórico». Dry run unless
        `apply`; refused with the flag off. Changes nothing else: no state,
        no payment, no balance. Returns (count, amount)."""
        if not verificacion_on(self.env):
            raise UserError("Prendé distri_pedido.verificacion_v2 antes de marcar los históricos.")
        cards = self.sudo().search(self._distri_v2_historic_domain())
        total = sum(cards.mapped("amount"))
        if apply and cards:
            # a plain column write: no tracking, no recompute of money
            cards.write({"historic": True})
        return len(cards), total


class WhatsappMessageVerificacion(models.Model):
    _inherit = "distri.whatsapp.message"

    distri_twin_review = fields.Boolean(
        string="Posible duplicado", readonly=True, copy=False, index=True,
        help="Mismo importe, fecha y hora que otro comprobante, sin número de operación: puede "
             "ser el mismo pago o dos pagos iguales. Lo decide una persona.")
    distri_twin_of_id = fields.Many2one(
        "distri.whatsapp.message", string="Igual al comprobante", readonly=True, copy=False,
        ondelete="set null")
    distri_twin_note = fields.Char(string="Posible duplicado: aclaración", readonly=True,
                                   copy=False)
    distri_twin_ok = fields.Boolean(
        string="Son dos pagos", readonly=True, copy=False,
        help="Una persona dijo que no es una copia: no se vuelve a marcar.")

    # ------------------------------------------------------------ twin guard

    def _distri_classify(self):
        if verificacion_on(self.env) and len(self) == 1 and not self.sudo().distri_operation:
            return super(WhatsappMessageVerificacion,
                         self.with_context(distri_v2_twin_guard=True))._distri_classify()
        return super()._distri_classify()

    def _distri_close(self, state, note):
        if state == "duplicate" and self.env.context.get("distri_v2_twin_guard"):
            self.ensure_one()
            msg = self.sudo()
            if not msg.distri_twin_ok and not msg.distri_twin_review:
                msg._distri_v2_mark_twin()
            return None
        return super()._distri_close(state, note)

    def _distri_v2_mark_twin(self):
        """Holds this receipt for a person instead of closing it as a copy."""
        self.ensure_one()
        msg = self.sudo()
        twins = msg._distri_twins()
        first = twins.sorted(lambda m: (m.timestamp or datetime.min, m.id))[:1]
        note = TWIN_NOTE % (local_when(first.timestamp),
                            first.distri_forwarded_by_id.name or first.profile_name
                            or first.wa_id or "")
        msg.with_context(distri_receipt_matching=True).write({
            "distri_twin_review": True, "distri_twin_of_id": first.id or False,
            "distri_twin_note": note})
        _logger.info("Receipt %s: possible twin of %s held for a person", msg.id, first.id)

    def _distri_match_income(self):
        if verificacion_on(self.env) and len(self) == 1:
            msg = self.sudo()
            if msg.distri_twin_review and not msg.distri_twin_ok:
                return False
            found = super()._distri_match_income()
            if found:
                found._distri_v2_auto_credit(msg)
            return found
        return super()._distri_match_income()

    def action_distri_twin_same(self):
        """«Es el mismo pago»: closed as «Ya recibido», as before the guard."""
        self._distri_v2_check_twin_user()
        for msg in self.sudo().filtered("distri_twin_review"):
            first = msg.distri_twin_of_id
            msg.with_context(distri_receipt_matching=True).write({
                "distri_twin_review": False,
                "distri_twin_note": "Es el mismo pago: lo decidió %s el %s." % (
                    self.env.user.name, local_when(fields.Datetime.now()))})
            msg.with_context(distri_v2_twin_guard=False)._distri_close(
                "duplicate", "Ya recibido el %s (%s). Lo decidió %s." % (
                    local_when(first.timestamp),
                    first.distri_forwarded_by_id.name or first.profile_name or first.wa_id or "",
                    self.env.user.name))
        return True

    def action_distri_twin_two(self):
        """«Son dos pagos»: the receipt goes on as any other and is never
        held again."""
        self._distri_v2_check_twin_user()
        for msg in self.sudo().filtered("distri_twin_review"):
            msg.with_context(distri_receipt_matching=True).write({
                "distri_twin_review": False, "distri_twin_ok": True,
                "distri_twin_note": "Son dos pagos distintos: lo decidió %s el %s." % (
                    self.env.user.name, local_when(fields.Datetime.now()))})
            if msg.distri_receipt_state in ("pending", "no_money"):
                msg.with_context(distri_receipt_matching=True)._distri_match_income()
        return True

    def _distri_v2_check_twin_user(self):
        if not queue_member(self.env.user, "duplicados"):
            raise AccessError("Los posibles duplicados los revisa administración.")


class DistriMpCollectionCreditVerificacion(models.Model):
    """A person crediting a held twin is warned first."""
    _inherit = "distri.mp.collection"

    def _distri_credit_review(self, partner, amount, driver_auto=False):
        errors, warnings = super()._distri_credit_review(partner, amount, driver_auto=driver_auto)
        receipt = self.sudo().receipt_id
        if verificacion_on(self.env) and receipt.distri_twin_review and not receipt.distri_twin_ok:
            twin = receipt.distri_twin_of_id
            # 19.0.1.129.32: its twin was already credited by a person
            # (its card confirmed or authorized, before the report tied it:
            # the «matched»/«registered» twin is already an error in
            # acreditar.py): only an approver may credit this one after the
            # warning; anyone else waits for «Son dos pagos» in the queue
            twin_paid = bool(twin) and bool(self.sudo().search_count([
                ("kind", "=", "verify"), ("receipt_id", "=", twin.id),
                ("state", "in", ("confirmed", "authorized"))], limit=1))
            if driver_auto:
                errors.append(TWIN_CREDIT_WARNING % local_when(receipt.distri_twin_of_id.timestamp))
            elif twin_paid and not can_approve(self.env.user):
                errors.append(TWIN_CREDITED_ERROR % local_when(twin.timestamp))
            else:
                warnings.append(TWIN_CREDIT_WARNING % local_when(
                    receipt.distri_twin_of_id.timestamp))
        return errors, warnings


class WhatsappMessageUploadRound(models.Model):
    """2. The staff's one-step crediting after the report (19.0.1.129.13):
    a round amount tied only by amount and time waits for a person."""
    _inherit = "distri.whatsapp.message"

    def _distri_upload_sure_income(self, partner, amount=None):
        income = super()._distri_upload_sure_income(partner, amount=amount)
        if income and verificacion_on(self.env) and \
                is_round_amount(income.amount, round_step(self.env)) and \
                income._distri_v2_sure(self) != "operation":
            _logger.info("Upload %s: round amount %s tied by time waits for a person",
                         self.id, income.amount)
            return income.browse()
        return income


class DistriVerificacion(models.AbstractModel):
    """The queues: counts and rows read from existing records."""
    _name = "distri.verificacion"
    _description = "Tablero de verificación"

    # ------------------------------------------------------------ parts

    def _parts(self, key, user):
        """[(model, domain, since field, entry offset)] of the queue `key`,
        as seen by `user` (the board's own visibility for the incomes)."""
        Collection = self.env["distri.mp.collection"]
        now = fields.Datetime.now()
        if key == "para_acreditar":
            tied = Collection._board_receipt_tied_ids()
            return [
                ("distri.mp.collection", [
                    ("kind", "=", "verify"), ("state", "=", "draft"),
                    ("report_review", "=", False),
                    ("receipt_id.distri_twin_review", "=", False)], "create_date", None),
                ("distri.mp.collection", [("id", "in", tied)], "create_date", None),
            ]
        if key == "revision_humana":
            return [("distri.mp.collection", [
                ("kind", "=", "verify"), ("auto_credited", "=", True),
                ("human_check", "=", "pendiente")], "credited_at", None)]
        if key == "esperando_reporte":
            return [("distri.mp.collection", [
                ("kind", "=", "verify"), ("state", "=", "confirmed"), ("income_id", "=", False),
                ("credited_at", "!=", False), ("payment_id", "!=", False),
                ("report_review", "=", False)], "credited_at", None)]
        if key == "sin_transferencia":
            return [("distri.mp.collection", [
                ("kind", "=", "verify"), ("state", "=", "confirmed"), ("income_id", "=", False),
                ("credited_at", "!=", False),
                "|", ("report_review", "=", True), ("audit_state", "=", "sin_transferencia")],
                "write_date", None)]
        if key == "diferencias":
            visible = Collection.with_user(user)._search_board_visible("=", True)
            return [
                ("distri.mp.collection", [
                    ("kind", "in", ("income", "verify")), ("state", "=", "draft"),
                    ("column", "=", "review"), ("bank_twin_payment_id", "=", False),
                    ("historic", "=", False)] + visible, "create_date", None),
                ("distri.mp.collection", [
                    ("kind", "=", "verify"), ("state", "=", "confirmed"),
                    ("audit_state", "in", ("diferencia", "revertido"))], "write_date", None),
            ]
        if key == "duplicados":
            return [
                ("distri.whatsapp.message", [
                    ("distri_is_receipt", "=", True), ("distri_twin_review", "=", True),
                    ("distri_receipt_state", "in", ("pending", "no_money"))],
                 "create_date", None),
                ("distri.mp.collection", [
                    ("kind", "=", "income"), ("state", "=", "draft"),
                    ("bank_twin_payment_id", "!=", False)], "create_date", None),
            ]
        if key == "sin_comprobante":
            days = timedelta(days=no_receipt_days(self.env))
            return [("distri.mp.collection", [
                ("kind", "=", "income"), ("state", "=", "draft"), ("receipt_id", "=", False),
                ("partner_id", "!=", False), ("historic", "=", False),
                ("date", "<", now - days)], "date", days)]
        if key == "entregados_a_confirmar":
            # escalated after 48 h (models/reparto_a_confirmar.py): the term
            # runs from the escalation
            return [("distri.route.stop", [
                ("state", "=", "already_paid"), ("paid_confirm_state", "=", "escalated")],
                "paid_confirm_since", timedelta(hours=48))]
        if key == "historicos":
            return [
                ("distri.mp.collection", [("kind", "=", "income"), ("state", "=", "draft"),
                                          ("historic", "=", True)], "date", None),
                ("distri.mp.collection", [("migration_box", "=", "missing_receipt"),
                                          ("state", "=", "draft")], "date", None),
            ]
        raise KeyError(key)

    @api.model
    def _queue(self, key):
        return next(q for q in QUEUES if q[0] == key)

    @api.model
    def _summary(self, key, user=None):
        """{key, number, title, what, who, term, count, overdue, oldest}."""
        user = user or self.env.user
        _key, number, title, what, _groups, who, term = self._queue(key)
        now = fields.Datetime.now()
        count = overdue = 0
        oldest = None
        for model, domain, since_field, offset in self._parts(key, user):
            Model = self.env[model].sudo()
            count += Model.search_count(domain)
            first = Model.search(domain + [(since_field, "!=", False)], order="%s asc" % since_field,
                                 limit=1)
            if first:
                since = first[since_field] + (offset or timedelta())
                oldest = since if oldest is None else min(oldest, since)
            if term is not None:
                limit = now - timedelta(hours=term) - (offset or timedelta())
                overdue += Model.search_count(domain + [(since_field, "<", limit)])
        return {"key": key, "number": number, "title": title, "what": what, "who": who,
                "term": term, "term_label": ("%s h" % term) if term else "",
                "count": count, "overdue": overdue,
                "oldest": age_text(oldest, now) if count and oldest else ""}

    @api.model
    def _summaries(self, user=None):
        user = user or self.env.user
        return [self._summary(q[0], user) for q in user_queues(user)]

    @api.model
    def _badge(self, user=None):
        """The Inicio count: what waits in the user's queues with a term."""
        user = user or self.env.user
        total = overdue = 0
        for summary in self._summaries(user):
            if summary["term"] is None:
                continue
            total += summary["count"]
            overdue += summary["overdue"]
        if not total:
            return None
        text = str(total) + (" · %s vencidos" % overdue if overdue > 1 else
                             " · 1 vencido" if overdue else "")
        return {"text": text, "kind": "err" if overdue else "warn", "n": total}

    # ------------------------------------------------------------ rows

    @api.model
    def _rows(self, key, user=None, limit=LIST_LIMIT):
        """The oldest `limit` items of the queue, as dicts for the screen."""
        user = user or self.env.user
        term = self._queue(key)[6]
        now = fields.Datetime.now()
        rows = []
        for model, domain, since_field, offset in self._parts(key, user):
            records = self.env[model].sudo().search(domain, order="%s asc, id asc" % since_field,
                                                    limit=limit)
            for record in records:
                since = record[since_field] and record[since_field] + (offset or timedelta())
                if model == "distri.whatsapp.message":
                    row = self._message_row(record)
                elif model == "distri.route.stop":
                    row = self._stop_row(record, user)
                else:
                    row = self._card_row(record, key, user)
                row.update(since=since, age=age_text(since, now), overdue=bool(
                    term is not None and since and now - since > timedelta(hours=term)))
                rows.append(row)
        rows.sort(key=lambda r: r["since"] or datetime.max)
        return rows[:limit]

    def _card_row(self, card, key, user):
        partner = card.partner_id.commercial_partner_id
        reason = card.review_reason or card.proposal_note or card.ignore_note or ""
        if key == "sin_comprobante":
            reason = "Sin comprobante desde el %s" % local_when(card.date)
        elif key == "historicos" and card.migration_box == "missing_receipt":
            reason = "Tablero de migración: falta el comprobante"
        # the incomes hidden on the board open the customer's account instead
        url = "/odoo/distri.mp.collection/%s" % card.id
        if card.kind == "income" and card.state == "draft" and not card.with_user(user).board_visible:
            url = _partner_url(self.env, partner, user) if partner else ""
        return {
            "model": "distri.mp.collection", "id": card.id, "name": card.name or "",
            "partner": partner.name or "Sin cliente", "amount": _money(card.amount),
            "reason": reason, "url": url,
            "check": key == "revision_humana" and may_check(user),
            "twin": False,
            "who": (card.credited_by_id.name or "") if key in (
                "revision_humana", "esperando_reporte", "sin_transferencia") else "",
        }

    def _message_row(self, msg):
        return {
            "model": "distri.whatsapp.message", "id": msg.id,
            "name": "Comprobante del %s" % local_when(msg.timestamp),
            "partner": msg.distri_partner_id.commercial_partner_id.name or "Sin cliente",
            "amount": _money(msg.distri_amount),
            "reason": msg.distri_twin_note or "",
            "url": "/odoo/distri.whatsapp.message/%s" % msg.id,
            "check": False, "twin": True, "who": "",
        }


class IrActionsActWindowVerificacion(models.Model):
    _inherit = "ir.actions.act_window"

    def _get_action_dict(self):
        """19.0.1.129.32: the payments board («Tablero») keeps 129.26's action
        with the flag off; with verificacion_v2 on, the historic incomes are
        hidden by default and its filters show (views/verificacion.xml)."""
        result = super()._get_action_dict()
        if result.get("xml_id") == "distri_pedido.action_mp_board" and verificacion_on(self.env):
            try:
                ctx = safe_eval(result.get("context") or "{}")
            except Exception:
                return result
            if isinstance(ctx, dict):
                ctx.update({"search_default_not_historic": 1, "distri_verificacion_v2": True})
                result["context"] = repr(ctx)
        return result
