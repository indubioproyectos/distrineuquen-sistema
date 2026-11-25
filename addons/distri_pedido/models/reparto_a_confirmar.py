"""«Ya estaba pagado · a confirmar» (19.0.1.129.25, owner's decision A of
24/11/2026).

The customer already uploaded a transfer receipt that nobody credited yet
and it covers what the order still owes (`reparto_pagos.py`,
`_app_cover_card`). The driver may then save the visit as «Ya estaba
pagado · a confirmar»:

- the stop is `already_paid` (delivered, no cash, no transfer): no charge to
  the driver, not counted as «No pagó» and no pending item at his name
  (`comprobante_pendiente.py` only creates one for «No pagó» or a transfer);
- the stop keeps the receipt card it relied on and stays flagged «A
  confirmar» (`paid_confirm_state`);
- the hourly cron of the pending receipts (`_distri_cron_expire`) checks
  each flagged stop: when the card is credited, brought by the Mercado Pago
  report or the order has nothing left to pay, the flag turns «Confirmado»;
  when 48 h pass without that, it goes to administración: one To-Do per
  administración user on the card of the collections board (the same
  activity mechanism as the board alerts, `cobros.py` `_cron_alerts`) and a
  note in the route's chatter. The flag turns «En administración».

Owner's rule (until the accounts audit closes): nothing here credits,
creates, cancels or reconciles a payment, an invoice or a journal entry.
Only the stop's own fields, activities and chatter notes are written.
"""
import logging
from datetime import timedelta

from markupsafe import Markup

from odoo import api, fields, models

from .cobros import local_when
from .reparto import _money

_logger = logging.getLogger(__name__)

ADMIN_GROUP = "distri_pedido.grupo_administracion"
CONFIRM_STATES = [
    ("to_confirm", "A confirmar"),
    ("confirmed", "Confirmado"),
    ("escalated", "En administración"),
]
OPEN_CONFIRM = ("to_confirm", "escalated")
CONFIRM_WINDOW = timedelta(hours=48)
CONFIRM_SUMMARY = "Pago a confirmar del reparto"
CONFIRM_LABEL = "Ya estaba pagado · a confirmar"


class DistriRouteStopToConfirm(models.Model):
    _inherit = "distri.route.stop"

    paid_confirm_state = fields.Selection(
        CONFIRM_STATES, string="Pago a confirmar", readonly=True, copy=False, index=True,
        help="Entregado como «Ya estaba pagado · a confirmar»: el cliente había cargado un "
             "comprobante sin acreditar que cubre lo que debía. No se carga al repartidor. Si "
             "en 48 h el reporte de Mercado Pago no lo trae, pasa a administración.")
    paid_confirm_card_id = fields.Many2one(
        "distri.mp.collection", string="Comprobante a confirmar", readonly=True, copy=False,
        ondelete="set null", index=True)
    paid_confirm_since = fields.Datetime(string="A confirmar desde", readonly=True, copy=False)
    paid_confirm_done_at = fields.Datetime(string="Pago a confirmar resuelto el", readonly=True,
                                           copy=False)

    def _app_visit(self):
        data = super()._app_visit()
        if isinstance(data, dict) and "already_paid" in data:
            # 19.0.1.129.25: the phone shows «a confirmar» on the restored visit
            data["to_confirm"] = self.state == "already_paid" \
                and self.paid_confirm_state in OPEN_CONFIRM
        return data

    # ------------------------------------------------------------ the check

    def _distri_paid_confirmed(self):
        """The receipt was credited, the Mercado Pago report brought its
        transfer, or the order has nothing left to pay."""
        self.ensure_one()
        card = self.sudo().paid_confirm_card_id
        if card and (card.state == "confirmed" or card.income_id):
            return True
        return bool(self.sudo()._app_payments()["paid"])

    def _distri_confirm_feedback(self, text):
        """Closes the To-Dos of this stop's card, if any."""
        card = self.sudo().paid_confirm_card_id
        if not card:
            return
        activities = card.activity_ids.filtered(
            lambda a: a.summary == CONFIRM_SUMMARY and self.order_id.name in (a.note or ""))
        if activities:
            activities.action_feedback(feedback=text)

    @api.model
    def _distri_cron_paid_to_confirm(self, now=None):
        """Hourly: the stops «a confirmar» confirmed, or sent to
        administración after 48 h. Returns (confirmed, escalated)."""
        now = now or fields.Datetime.now()
        stops = self.sudo().search([("paid_confirm_state", "in", OPEN_CONFIRM),
                                    ("state", "=", "already_paid")], order="id")
        confirmed = escalated = self.browse()
        for stop in stops:
            if stop._distri_paid_confirmed():
                stop._distri_mark_paid_confirmed(now)
                confirmed |= stop
            elif stop.paid_confirm_state == "to_confirm" and stop.paid_confirm_since \
                    and stop.paid_confirm_since <= now - CONFIRM_WINDOW:
                stop._distri_escalate_paid_to_confirm(now)
                escalated |= stop
        if confirmed or escalated:
            _logger.info("Pagos a confirmar del reparto: %s confirmados, %s a administración",
                         len(confirmed), len(escalated))
        return confirmed, escalated

    def _distri_mark_paid_confirmed(self, now):
        self.ensure_one()
        card = self.paid_confirm_card_id
        text = "Pago confirmado%s: %s · %s ya no está a confirmar." % (
            " (%s)" % card.name if card else "", self.partner_id._distri_app_name(),
            self.order_id.name)
        self._distri_confirm_feedback(text)
        self.write({"paid_confirm_state": "confirmed", "paid_confirm_done_at": now})
        self.route_id.message_post(body=text)

    def _distri_admin_users(self):
        group = self.env.ref(ADMIN_GROUP, raise_if_not_found=False)
        if not group:
            return self.env["res.users"]
        root = self.env.ref("base.user_root", raise_if_not_found=False)
        return group.sudo().all_user_ids.filtered(
            lambda u: u.active and not u.share and u != root).sorted("id")

    def _distri_escalate_paid_to_confirm(self, now):
        """48 h without the transfer: administración decides. A To-Do per
        administración user on the board card (never a second one while the
        first is open) and a note on the route."""
        self.ensure_one()
        card = self.paid_confirm_card_id
        text = ("%s · %s: el repartidor %s lo entregó el %s como «%s» con un comprobante de %s "
                "sin acreditar (%s). Pasaron 48 h y el reporte de Mercado Pago no lo trae. Revisá "
                "el cobro. No se cargó nada al repartidor ni cambió el saldo del cliente." % (
                    self.partner_id._distri_app_name(), self.order_id.name,
                    self.route_id.driver_id.name or "", local_when(self.paid_confirm_since),
                    CONFIRM_LABEL, _money(card.amount if card else 0.0),
                    card.name if card else "el comprobante ya no está en el tablero"))
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        if card and todo:
            open_users = card.activity_ids.filtered(
                lambda a: a.summary == CONFIRM_SUMMARY
                and self.order_id.name in (a.note or "")).user_id
            quiet = card.with_context(mail_activity_quick_update=True, mail_notrack=True).sudo()
            for user in self._distri_admin_users() - open_users:
                quiet.activity_schedule(activity_type_id=todo.id, summary=CONFIRM_SUMMARY,
                                        user_id=user.id, note=text)
        self.write({"paid_confirm_state": "escalated"})
        self.route_id.message_post(body=Markup("<b>%s</b>: %s") % (CONFIRM_SUMMARY, text))


class DistriSyncEventToConfirm(models.Model):
    _inherit = "distri.sync.event"

    def _visit_values(self, stop, payload):
        """19.0.1.129.25: «Ya estaba pagado» on an order not paid yet whose
        uncredited receipt covers the amount due is «a confirmar» (state
        already_paid, no charge), instead of «No pagó»."""
        vals = super()._visit_values(stop, payload)
        was_open = stop.paid_confirm_state in OPEN_CONFIRM
        if payload.get("already_paid") and vals.get("state") == "not_paid":
            info = stop.sudo()._app_payments()
            card_id = info.get("to_confirm_card_id")
            if card_id:
                keep = was_open and stop.paid_confirm_card_id.id == card_id
                vals.update({
                    "state": "already_paid",
                    "paid_confirm_state": stop.paid_confirm_state if keep else "to_confirm",
                    "paid_confirm_card_id": card_id,
                    "paid_confirm_since": stop.paid_confirm_since if keep
                    else fields.Datetime.now(),
                    "paid_confirm_done_at": False,
                })
                return vals
        if was_open:
            stop._distri_confirm_feedback(
                "La visita se corrigió: %s ya no está a confirmar." % stop.order_id.name)
        if stop.paid_confirm_state:
            vals.update({"paid_confirm_state": False, "paid_confirm_card_id": False,
                         "paid_confirm_since": False, "paid_confirm_done_at": False})
        return vals


class DistriPromisedTransferToConfirm(models.Model):
    _inherit = "distri.promised.transfer"

    @api.model
    def _distri_cron_expire(self):
        """19.0.1.129.25: the same hourly cron also follows the visits «Ya
        estaba pagado · a confirmar». A failure there never stops the
        expiry of the pending receipts."""
        result = super()._distri_cron_expire()
        try:
            with self.env.cr.savepoint():
                self.env["distri.route.stop"]._distri_cron_paid_to_confirm()
        except Exception:  # noqa: BLE001 - logged, the expiry already ran
            _logger.exception("Pagos a confirmar del reparto: falló la revisión")
        return result
