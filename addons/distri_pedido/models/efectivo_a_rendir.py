"""Driver cash credited at visit close, verified at settlement (19.0.1.129.2).

Owner's design (22/11/2026). Before, the cash a driver collected at a stop
became a customer payment only when a cashier received the route («Caja del
día», distri.caja.receive_route -> distri.route.action_settle): until then
the customer still appeared to owe.

- At the visit (`distri.sync.event._record_visit`, a phone event of a
  delivery route): a stop with cash gets its customer payment at once,
  applied oldest invoice first like the settlement does. It goes to the
  driver's own «Efectivo a rendir · <driver>» cash journal (one per driver
  and company, created the first time, like «Caja depósito»), is linked to
  the stop and the route and is marked «a rendir»
  (`distri_driver_cash_state`): the statement shows it as «Efectivo cobrado
  por el repartidor, a rendir». Like every cash payment of the module it
  waits in «Recibos pendientes» (the journal's method lines have no account
  of their own), so no money is booked in any cash account yet.
- Corrections before the settlement (another amount, «No pagó», not
  delivered, a resend of the app): the stop keeps exactly one live payment
  (`_distri_sync_driver_cash`). A payment whose amount no longer matches is
  cancelled (unreconciled first, so the invoices get their debt back) and a
  new one is made; a resend with the same amount changes nothing. A unique
  partial index on the live payment of a stop is the last guard.
- A failure while making the payment never rejects the visit (the driver
  keeps working): it is logged and noted on the route, and the settlement
  makes the payment as before.
- At the settlement (`distri.route._register_cash_payments`): the stops are
  synced once more (strictly: a problem stops the settlement) and each
  stop's «a rendir» payment is reused, never duplicated: it joins the
  route's «Cobros registrados» and becomes «verificado» (✓ «Pago confirmado»
  in the statement). The cash reaches the caja exactly as before: the
  «Rendición» movement counts what the cashier counted, and the closing's
  cash statement reconciles the route's payments from «Recibos pendientes»
  into the caja's cash account. So there is no extra internal transfer and
  the caja totals and differences do not change. A shortfall stays the
  route's cash case charged to the driver; the customer payment stands.
- Routes on the road or closed but not settled when 19.0.1.129.2 was
  installed keep the old path (`distri_cash_at_settlement`, set by
  migrations/19.0.1.129.2): their payments are made at the settlement in the
  route's journal, as before. The depot box (kind «depot») never uses this:
  it already registers each payment at the delivery.
- The collections audit (models/auditoria.py) flags an «a rendir» payment
  older than PARAM_DRIVER_CASH_DAYS days (default 1) and alerts Gerencia.
  Being cash, these payments are never «sin comprobante» and never enter the
  Mercado Pago matching.
"""
import logging

from markupsafe import Markup
from psycopg2 import errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import UserError

from .reparto import _money
from .reparto_cliente import today_ar

_logger = logging.getLogger(__name__)

DRIVER_CASH_STATES = [
    ("a_rendir", "A rendir"),
    ("verificado", "Rendido · pago confirmado"),
]
DRIVER_CASH_LABEL = "Efectivo cobrado por el repartidor, a rendir"
DRIVER_CASH_DONE_LABEL = "Efectivo cobrado por el repartidor, rendido"
DRIVER_CASH_JOURNAL_NAME = "Efectivo a rendir · %s"
# after CSH1 and CDEP in any journal list (and never the route's default)
DRIVER_CASH_JOURNAL_SEQUENCE = 90
LIVE_STATES = ("in_process", "paid")
CONCURRENCY_ERRORS = (pg_errors.SerializationFailure, pg_errors.LockNotAvailable,
                      pg_errors.DeadlockDetected)


class AccountJournalDriverCash(models.Model):
    _inherit = "account.journal"

    distri_driver_cash_user_id = fields.Many2one(
        "res.users", string="Repartidor (efectivo a rendir)", readonly=True, index=True,
        copy=False, ondelete="restrict",
        help="Caja del repartidor: el efectivo que cobró en las visitas y todavía no rindió.")

    @api.model
    def _distri_driver_cash_journal(self, user, company):
        """«Efectivo a rendir · <driver>» of the company, created the first time."""
        Journal = self.sudo().with_context(active_test=False)
        journal = Journal.search([("distri_driver_cash_user_id", "=", user.id),
                                  ("company_id", "=", company.id)], limit=1)
        if journal:
            return journal
        taken = set(Journal.search([("company_id", "=", company.id)]).mapped("code"))
        # a journal code has at most 5 characters
        code = next(c for c in ["R%d" % user.id] + ["R%d" % n for n in range(1, 10000)]
                    if len(c) <= 5 and c not in taken)
        journal = Journal.create({
            "name": DRIVER_CASH_JOURNAL_NAME % (user.name or user.login),
            "code": code,
            "type": "cash",
            "company_id": company.id,
            "sequence": DRIVER_CASH_JOURNAL_SEQUENCE,
            "distri_driver_cash_user_id": user.id,
        })
        # like CSH1 and CDEP in production: the payments wait in «Recibos
        # pendientes» until a caja's closing statement reconciles them
        (journal.inbound_payment_method_line_ids
         | journal.outbound_payment_method_line_ids).write({"payment_account_id": False})
        return journal


class AccountPaymentDriverCash(models.Model):
    _inherit = "account.payment"

    distri_driver_stop_id = fields.Many2one(
        "distri.route.stop", string="Visita del repartidor", readonly=True, index=True,
        copy=False, ondelete="set null")
    distri_driver_route_id = fields.Many2one(
        "distri.route", string="Ruta del repartidor", readonly=True, index=True, copy=False,
        ondelete="set null")
    distri_driver_cash_state = fields.Selection(
        DRIVER_CASH_STATES, string="Efectivo del repartidor", readonly=True, index=True,
        copy=False,
        help="A rendir: el repartidor lo cobró en la visita y el cliente ya lo tiene acreditado; "
             "falta que rinda la ruta en una caja. Rendido: un cajero recibió la ruta.")
    distri_driver_cash_label = fields.Char(string="Efectivo del repartidor",
                                           compute="_compute_distri_driver_cash_label")
    distri_driver_cash_verified_at = fields.Datetime(string="Rendido el", readonly=True,
                                                     copy=False)
    distri_driver_cash_verified_by_id = fields.Many2one("res.users", string="Lo recibió",
                                                        readonly=True, copy=False)

    _driver_stop_live = models.UniqueIndex(
        "(distri_driver_stop_id) WHERE distri_driver_stop_id IS NOT NULL "
        "AND state IN ('in_process', 'paid')",
        "Esa visita ya tiene su cobro en efectivo a rendir.")

    @api.depends("distri_driver_cash_state")
    def _compute_distri_driver_cash_label(self):
        labels = {"a_rendir": DRIVER_CASH_LABEL, "verificado": DRIVER_CASH_DONE_LABEL}
        for payment in self:
            payment.distri_driver_cash_label = labels.get(payment.distri_driver_cash_state, False)


class DistriRouteDriverCash(models.Model):
    _inherit = "distri.route"

    distri_cash_at_settlement = fields.Boolean(
        string="Cobros al rendir (ruta anterior a 19.0.1.129.2)", readonly=True, copy=False,
        help="La ruta ya estaba en reparto o cerrada al instalar 19.0.1.129.2: su efectivo se "
             "registra como pago al rendirla, como antes.")
    driver_cash_payment_ids = fields.One2many(
        "account.payment", "distri_driver_route_id", string="Efectivo a rendir")

    def _distri_driver_cash_enabled(self):
        self.ensure_one()
        return (self.kind == "route" and not self.distri_cash_at_settlement
                and self.state != "settled")

    def _distri_create_driver_cash(self, stop):
        """The stop's cash as an «a rendir» customer payment in the driver's
        journal, applied oldest invoice first."""
        self.ensure_one()
        partner = stop.partner_id.commercial_partner_id
        journal = self.env["account.journal"]._distri_driver_cash_journal(
            self.driver_id, self.company_id)
        # as the driver (who collected it), sudo: customer payments are
        # otherwise reserved to grupo_cobros (models/permisos_cobros.py)
        payment = self.env["account.payment"].with_user(self.driver_id).sudo().create({
            "payment_type": "inbound",
            "partner_type": "customer",
            "partner_id": partner.id,
            "amount": stop.cash_amount,
            "journal_id": journal.id,
            "date": today_ar(),
            "memo": "Reparto %s · %s" % (self.name, stop.order_id.name),
            "company_id": self.company_id.id,
            "distri_driver_stop_id": stop.id,
            "distri_driver_route_id": self.id,
            "distri_driver_cash_state": "a_rendir",
        })
        # never a caja's own cash: the settlement brings it
        payment.with_context(distri_caja_no_instant=True).action_post()
        self._apply_oldest_first(payment, partner)
        payment.message_post(body=Markup("%s: %s cobró %s en la visita de %s (%s).") % (
            DRIVER_CASH_LABEL, self.driver_id.name, _money(stop.cash_amount),
            partner.name, self.name))
        return payment

    def _distri_verify_driver_cash(self, payment):
        """Settlement: the «a rendir» payment joins the route's payments and
        is verified (the cash reached a caja)."""
        self.ensure_one()
        payment = payment.sudo()
        if payment.distri_driver_cash_state != "verificado":
            payment.write({
                "distri_driver_cash_state": "verificado",
                "distri_driver_cash_verified_at": fields.Datetime.now(),
                "distri_driver_cash_verified_by_id": self.env.user.id,
            })
            payment.message_post(body=Markup("Rendido: %s recibió %s y el pago quedó confirmado.")
                                 % (self.env.user.name, self.name))
        self.payment_ids = [(4, payment.id)]
        return payment


class DistriRouteStopDriverCash(models.Model):
    _inherit = "distri.route.stop"

    def _distri_driver_cash_payment(self):
        """The stop's live «a rendir» (or verified) payments: at most one."""
        self.ensure_one()
        if not self.id:
            return self.env["account.payment"]
        return self.env["account.payment"].sudo().search([
            ("distri_driver_stop_id", "=", self.id), ("state", "in", LIVE_STATES)])

    def _distri_void_driver_cash(self, payments, reason):
        """Cancel «a rendir» payments: off the invoices first, so they owe again."""
        self.ensure_one()
        for payment in payments.sudo():
            payment.move_id.line_ids.remove_move_reconcile()
            payment.action_draft()
            payment.action_cancel()
            text = Markup("Se anuló el cobro a rendir %s de %s (%s): %s.") % (
                payment.name or "", _money(payment.amount), self.route_id.name, reason)
            payment.message_post(body=text)
            self.route_id.message_post(body=text)

    def _distri_driver_cash_reason(self, wanted):
        if wanted > 0:
            return "el repartidor corrigió la visita, ahora cobró %s" % _money(wanted)
        if self.state == "not_paid":
            return "el repartidor corrigió la visita: «No pagó»"
        if self.state in ("not_delivered", "dispatched"):
            return "el repartidor corrigió la visita: no se entregó"
        return "la visita ya no tiene efectivo"

    def _distri_void_stale_driver_cash(self, wanted):
        """Before a visit is rewritten: the live payment whose amount differs
        from `wanted` goes first, so the new visit sees the real debt."""
        for stop in self:
            if not stop.route_id._distri_driver_cash_enabled():
                continue
            live = stop._distri_driver_cash_payment()
            stale = live.filtered(lambda p: p.distri_driver_cash_state != "a_rendir"
                                  or p.currency_id.compare_amounts(p.amount, wanted) != 0)
            if stale:
                stop._distri_void_driver_cash(stale, stop._distri_driver_cash_reason(wanted))

    def _distri_sync_driver_cash(self):
        """One live «a rendir» payment per stop with cash, of its amount;
        none for a stop without cash. Idempotent: run it as often as wanted."""
        for stop in self:
            route = stop.route_id
            if not route._distri_driver_cash_enabled():
                continue
            # two syncs of the same stop at once: the second waits here
            self.env.cr.execute("SELECT id FROM distri_route_stop WHERE id = %s FOR UPDATE",
                                [stop.id])
            wanted = stop.cash_amount if stop.cash_amount > 0 else 0.0
            partner = stop.partner_id.commercial_partner_id
            live = stop._distri_driver_cash_payment()
            keep = live.filtered(
                lambda p: wanted and p.distri_driver_cash_state == "a_rendir"
                and p.partner_id == partner
                and p.currency_id.compare_amounts(p.amount, wanted) == 0)[:1]
            if live - keep:
                stop._distri_void_driver_cash(live - keep, stop._distri_driver_cash_reason(wanted))
            if wanted and not keep:
                route._distri_create_driver_cash(stop)

    def _distri_sync_driver_cash_safe(self):
        """The sync at the visit: a problem never rejects the visit (the
        settlement makes the payment as before)."""
        for stop in self:
            try:
                with self.env.cr.savepoint():
                    stop._distri_sync_driver_cash()
            except CONCURRENCY_ERRORS:
                raise
            except Exception as error:  # noqa: BLE001 - logged, settled later
                _logger.exception("Driver cash: could not sync the payment of stop %s", stop.id)
                stop.route_id.sudo().message_post(body=Markup(
                    "No se pudo registrar al momento el cobro en efectivo de %s (%s): %s. Se "
                    "registra al rendir la ruta.") % (
                    stop.partner_id.name or "", stop.order_id.name or "",
                    error.args[0] if isinstance(error, UserError) and error.args else
                    "error del servidor"))


class DistriSyncEventDriverCash(models.Model):
    _inherit = "distri.sync.event"

    @staticmethod
    def _distri_visit_cash(payload):
        """The cash a visit payload will leave on the stop, or None when it
        cannot be read (the visit itself then refuses it)."""
        if payload.get("outcome") != "delivered" or payload.get("no_payment"):
            return 0.0
        try:
            cash = float(payload.get("cash") or 0)
        except (TypeError, ValueError):
            return None
        return cash if cash > 0 else 0.0

    def _record_visit(self, stop, visit_uuid, payload, captured):
        if stop.route_id._distri_driver_cash_enabled():
            wanted = self._distri_visit_cash(payload)
            if wanted is not None:
                try:
                    with self.env.cr.savepoint():
                        stop._distri_void_stale_driver_cash(wanted)
                except CONCURRENCY_ERRORS:
                    raise
                except Exception:  # noqa: BLE001 - the sync after the visit retries
                    _logger.exception("Driver cash: could not void the old payment of stop %s",
                                      stop.id)
        result = super()._record_visit(stop, visit_uuid, payload, captured)
        stop._distri_sync_driver_cash_safe()
        return result

    def _apply_parcel(self, user, route, payload, captured):
        # a parcel stop carries no cash: a payment left from before goes
        result = super()._apply_parcel(user, route, payload, captured)
        self._stop_for(route, payload)._distri_sync_driver_cash_safe()
        return result


class ResPartnerDriverCash(models.Model):
    _inherit = "res.partner"

    def _distri_statement_rows(self):
        """«A rendir» payments say so; a verified one gets ✓ «Pago confirmado»."""
        rows = super()._distri_statement_rows()
        move_ids = [r["res_id"] for r in rows if r["res_model"] == "account.move" and r["credit"]]
        if not move_ids:
            return rows
        payments = self.env["account.payment"].sudo().search([
            ("move_id", "in", move_ids), ("distri_driver_cash_state", "!=", False)])
        by_move = {p.move_id.id: p for p in payments}
        for row in rows:
            payment = by_move.get(row["res_id"]) if row["res_model"] == "account.move" else None
            if not payment:
                continue
            if payment.distri_driver_cash_state == "a_rendir":
                row["driver_cash_pending"] = True
                row["concept"] = " · ".join(x for x in (DRIVER_CASH_LABEL, row["concept"]) if x)
            else:
                row["confirmed"] = True
        return rows
