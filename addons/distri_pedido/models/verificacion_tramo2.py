"""«Tablero de verificación», second slice (19.0.1.129.38, plan unificado
Etapa 5). Behind distri_pedido.verificacion_v2 like the first slice
(models/verificacion.py): with the flag off nothing here changes what the
module does.

1. Decision 2 of 24/11/2026: a receipt loaded and not credited is valid
   for the delivery («Ya estaba pagado · a confirmar», 19.0.1.129.25,
   models/reparto_a_confirmar.py) and the order stays «a confirmar». After
   48 h without the report it goes to Ale and Denis: queue 9 «Entregados a
   confirmar» of the board and one To-Do each (`_distri_admin_users`), not
   one per administración user. Since 19.0.1.129.39 by login («admin» and
   «denis», parameter distri_pedido.a_confirmar_logins). No accounting.

2. Queue 7 «En el reporte sin comprobante»:
   - «Reclamar por WhatsApp»: the customer's chat (the driver app's
     helpers: `_distri_whatsapp_shown`, `wa_digits`) with the claim already
     written; a note on the card says who claimed it;
   - «Comprobante de otro cliente»: a pending receipt (not tied yet) of
     ANOTHER customer that may be this transfer: the same operation number,
     or the same amount with the same payer's CUIT or within 2 minutes of
     the transfer (`_distri_v2_other_customer_receipt`). Its own flag on
     the row and its own filter;
   - «Sin cliente» (#48): the incomes of the report nobody could assign
     (column «Asignar cliente»), not historic, as a filter of the queue.
     Informs only: they are not added to the Inicio count.

3. «Comprobantes subidos» (#10 and #66): every receipt of the last days by
   who or where it came from (each staff user, the customer's page, the
   driver's app, WhatsApp), with its state (`_distri_v2_uploads`).

4. The round-amount rule also for the report's ties of the receipts
   already credited (`_match_credited`, and the audit's
   `_untied_state`): a round amount is tied by itself only with the exact
   operation number; any other level goes to «Revisar» for a person. Those
   ties create no payment either way.

5. The old board alerts (`_distri_v2_old_alerts`, run by
   scripts/correcciones/v129_38_cerrar_alertas.sh): the open «Cobro sin
   resolver en el tablero» To-Dos the board no longer raises are marked
   done with a note. Only the activities: never a card, payment or entry.
"""
import logging
from datetime import timedelta

from odoo import api, fields, models

from .ar_phone import wa_digits
from .cobros import ALERT_SUMMARY, AMOUNT_TOLERANCE, PRIORITY_COLUMNS, RECEIPT_TIME_CLOSE, \
    digits, local_when
from .comprobante_pendiente import relative_when
from .reparto import _money
from .verificacion import historic_before, is_round_amount, no_receipt_days, round_step, \
    verificacion_on

_logger = logging.getLogger(__name__)

CLAIM_TEXT = ("Hola, en Mercado Pago nos llegó una transferencia tuya de %s %s y todavía no "
              "tenemos el comprobante. ¿Nos lo podés mandar por acá?\n%s")
CLAIM_NOTE = "Reclamado por WhatsApp por %s: falta el comprobante de esta transferencia."
OTHER_CUSTOMER_FLAG = "Comprobante de otro cliente: el de %s del %s (%s) coincide por %s."
OTHER_WHY = {
    "operation": "el número de operación",
    "cuit": "el importe y el CUIT de quien pagó",
    "time": "el importe y la hora",
}
ROUND_HOLD_NOTE = ("Importe redondo: «%s» no alcanza para vincularlo solo. Puede ser el "
                   "comprobante acreditado %s (%s): vinculalo o, si es otro pago, confirmalo "
                   "como otro pago.")
OLD_ALERT_NOTE = ("Cerrada por la limpieza de la v129.38 (%s): el tablero ya no avisa por esta "
                  "tarjeta. No se tocó la tarjeta, ningún pago ni ningún saldo.")
OLD_ALERT_REASONS = {
    "income": "ingreso del reporte",
    "resolved": "la tarjeta ya no está en «Sin transferencia»",
    "old": "tarjeta anterior al corte",
}
# filters of queue 7 (controllers/verificacion.py ?filtro=)
FILTER_OTHER = "otro_cliente"
FILTER_UNIDENTIFIED = "sin_cliente"
# how many queue-7 incomes the «otro cliente» filter looks at
OTHER_SCAN = 500
UPLOAD_DAYS = 7
UPLOAD_DAYS_MAX = 60
UPLOAD_LIMIT = 300


# 19.0.1.129.39 (review of 129.38): the deliveries «a confirmar» after 48 h
# go to these logins only (Ale = «admin», Denis), not to whoever joins
# administración later. A parameter so a change of person needs no deploy.
PARAM_CONFIRM_LOGINS = "distri_pedido.a_confirmar_logins"
DEFAULT_CONFIRM_LOGINS = "admin,denis"


def confirm_logins(env):
    value = env["ir.config_parameter"].sudo().get_param(PARAM_CONFIRM_LOGINS) \
        or DEFAULT_CONFIRM_LOGINS
    return [login.strip() for login in value.split(",") if login.strip()]


def _the_ale_and_denis(env):
    """Ale (the user «admin») and Denis: who gets the deliveries «a
    confirmar» after 48 h (decision 2 of 24/11/2026), by login
    (PARAM_CONFIRM_LOGINS), active internal users only. Empty when none of
    them exists (the caller then keeps the 129.37 rule)."""
    root = env.ref("base.user_root", raise_if_not_found=False)
    users = env["res.users"].sudo().search([("login", "in", confirm_logins(env))])
    return users.filtered(lambda u: u.active and not u.share and u != root).sorted("id")


class DistriRouteStopVerificacion(models.Model):
    _inherit = "distri.route.stop"

    def _distri_admin_users(self):
        """1. With the flag the delivery «a confirmar» goes to Ale and Denis."""
        if verificacion_on(self.env):
            users = _the_ale_and_denis(self.env)
            if users:
                return users
        return super()._distri_admin_users()


class DistriMpCollectionTramo2(models.Model):
    _inherit = "distri.mp.collection"

    # ------------------------------------------------------------ queue 7

    def _distri_v2_other_customer_receipt(self):
        """(receipt, why) of a pending receipt of ANOTHER customer that may
        be this income's transfer, or (empty, ""). Reads only."""
        self.ensure_one()
        return self._distri_v2_other_customer_map().get(
            self.id, (self.env["distri.whatsapp.message"].sudo().browse(), ""))

    def _distri_v2_other_customer_map(self):
        """{income id: (receipt, why)} for these incomes, with two searches
        for all of them (19.0.1.129.39; before, two per row). The rule per
        income is unchanged: among the receipts of another customer with
        one of its operation numbers (at most 5) or its amount (the 40
        newest), the oldest that has its operation number, or no operation
        number and the same payer's CUIT, or the amount within 2 minutes."""
        Message = self.env["distri.whatsapp.message"].sudo()
        incomes = self.sudo().filtered(
            lambda i: i.kind == "income" and i.partner_id and i.amount)
        if not incomes:
            return {}
        base = [("distri_is_receipt", "=", True),
                ("distri_receipt_state", "in", ("pending", "no_money")),
                ("distri_partner_id", "!=", False)]
        raw = {n for income in incomes for n in (income.source_id, income.bank_transfer_id) if n}
        by_operation = Message.search(base + [("distri_operation", "in", list(raw))]) \
            if raw else Message.browse()
        amounts = incomes.mapped("amount")
        by_amount = Message.search(base + [
            ("distri_amount", ">=", min(amounts) - AMOUNT_TOLERANCE),
            ("distri_amount", "<=", max(amounts) + AMOUNT_TOLERANCE)], order="id desc")
        # index the receipts by operation and by whole peso (the tolerance
        # is $ 1, so an amount is looked up in its peso and the two next)
        op_index, peso_index = {}, {}
        for msg in by_operation:
            op_index.setdefault(msg.distri_operation, []).append(msg)
        for msg in by_amount:  # newest first
            peso_index.setdefault(round(msg.distri_amount or 0.0), []).append(msg)
        result = {}
        for income in incomes:
            customer = income.partner_id.commercial_partner_id
            raw_numbers = {n for n in (income.source_id, income.bank_transfer_id) if n}
            numbers = {digits(n) for n in raw_numbers}

            def others(msg, customer=customer):
                return msg.distri_partner_id.commercial_partner_id != customer
            same_op = [m for n in raw_numbers for m in op_index.get(n, ()) if others(m)]
            peso = round(income.amount)
            same_amount = sorted(
                (m for p in (peso - 1, peso, peso + 1) for m in peso_index.get(p, ())
                 if abs((m.distri_amount or 0.0) - income.amount) <= AMOUNT_TOLERANCE
                 and others(m)), key=lambda m: -m.id)
            found = Message.browse([m.id for m in same_op[:5]] + [m.id for m in same_amount[:40]])
            payer = digits(income.payer_cuit)
            for msg in found.sorted("id"):
                operation = digits(msg.distri_operation)
                if operation and operation in numbers:
                    result[income.id] = (msg, "operation")
                    break
                if operation or abs((msg.distri_amount or 0.0) - income.amount) > AMOUNT_TOLERANCE:
                    # another transfer, or another amount
                    continue
                cuit = digits(msg.distri_payer_cuit)
                if payer and cuit and cuit == payer:
                    result[income.id] = (msg, "cuit")
                    break
                read_at = msg.distri_receipt_date
                if read_at and income.date and abs(read_at - income.date) <= RECEIPT_TIME_CLOSE:
                    result[income.id] = (msg, "time")
                    break
        return result

    def _distri_v2_other_customer_text(self, found=None):
        """The row's flag; `found`: (receipt, why) already computed."""
        msg, why = found if found is not None else self._distri_v2_other_customer_receipt()
        if not msg:
            return ""
        return OTHER_CUSTOMER_FLAG % (
            msg.distri_partner_id.commercial_partner_id.name or "",
            local_when(msg.distri_receipt_date or msg.timestamp or msg.create_date),
            _money(msg.distri_amount), OTHER_WHY[why])

    def _distri_v2_claim_digits(self):
        self.ensure_one()
        partner = self.sudo().partner_id.commercial_partner_id
        return wa_digits(partner._distri_whatsapp_shown()) if partner else ""

    def _distri_v2_claim_text(self):
        self.ensure_one()
        income = self.sudo()
        return CLAIM_TEXT % (_money(income.amount), relative_when(income.date),
                             self.env.company.name or "")

    def _distri_v2_in_no_receipt_queue(self):
        """The income is in queue 7 right now."""
        self.ensure_one()
        income = self.sudo()
        return bool(income.kind == "income" and income.state == "draft" and not income.receipt_id
                    and income.partner_id and not income.historic and income.date
                    and income.date < fields.Datetime.now()
                    - timedelta(days=no_receipt_days(self.env)))

    def _distri_v2_claim(self):
        """«Reclamar por WhatsApp»: the wa.me link with the claim written. A
        note on the card says who claimed it. No other write."""
        self.ensure_one()
        number = self._distri_v2_claim_digits()
        if not number:
            return ""
        from urllib.parse import quote
        self.sudo().message_post(body=CLAIM_NOTE % self.env.user.name)
        return "https://wa.me/%s?text=%s" % (number, quote(self._distri_v2_claim_text()))

    # ------------------------------------------------------------ round rule on credited ties

    def _match_credited(self):
        """4. The report's ties of the credited receipts: with the flag a
        round amount needs the exact operation number (`_tie_credited`)."""
        if verificacion_on(self.env):
            return super(DistriMpCollectionTramo2,
                         self.with_context(distri_v2_round_hold=True))._match_credited()
        return super()._match_credited()

    def _distri_v2_round_hold(self, level):
        """True when this income must not be tied by itself at `level`."""
        self.ensure_one()
        return bool(verificacion_on(self.env) and level != "operation"
                    and is_round_amount(self.amount, round_step(self.env)))

    def _tie_credited(self, card, level, by_hand=False):
        if not by_hand and self.env.context.get("distri_v2_round_hold") \
                and self._distri_v2_round_hold(level):
            self._distri_v2_send_round_review(card, level)
            return False
        return super()._tie_credited(card, level, by_hand=by_hand)

    def _distri_v2_send_round_review(self, card, level):
        """«Revisar» with the reason: a round amount (no payment, no tie)."""
        from .acreditar import LEVEL_LABELS
        self.ensure_one()
        self._send_credited_review(card)
        note = ROUND_HOLD_NOTE % (LEVEL_LABELS.get(level, level), card.sudo().name,
                                  _money(self.amount))
        self.sudo().write({"proposal_note": note})
        _logger.info("Income %s: round amount %s tied by %s to the credited card %s waits for "
                     "a person", self.id, self.amount, level, card.id)

    # ------------------------------------------------------------ old alerts

    @api.model
    def _distri_v2_old_alerts(self, before=None, include_blank=False):
        """5. The open board alerts the board no longer raises:
        {reason: activities}. «Cobro sin resolver en el tablero» To-Dos of
        this model whose card is an income of the report (never alerted
        since 19.0.1.129.16), is out of «Sin transferencia» / the waiting
        authorized delivery (resolved), or was created before `before`
        (default: the historic cut, 15/09/2026). `include_blank`: also the
        To-Dos with an empty summary AND note (a sanitized copy, practice).
        Reads only."""
        todo = self.env.ref("mail.mail_activity_data_todo", raise_if_not_found=False)
        if not todo:
            return {}
        before = before or historic_before(self.env)
        cut = fields.Datetime.to_datetime(before)
        Activity = self.env["mail.activity"].sudo()
        summary = [("summary", "=", ALERT_SUMMARY)]
        if include_blank:
            summary = ["|", ("summary", "=", ALERT_SUMMARY), "&", ("summary", "=", False),
                       ("note", "=", False)]
        activities = Activity.search([("res_model", "=", self._name),
                                      ("activity_type_id", "=", todo.id)] + summary,
                                     order="id")
        cards = self.sudo().browse(activities.mapped("res_id")).exists()
        by_id = {card.id: card for card in cards}
        found = {key: Activity.browse() for key in OLD_ALERT_REASONS}
        for activity in activities:
            card = by_id.get(activity.res_id)
            if not card:
                continue
            if card.kind == "income":
                found["income"] |= activity
            elif not self._distri_v2_alert_live(card):
                found["resolved"] |= activity
            elif card.create_date and card.create_date < cut:
                found["old"] |= activity
        return found

    @staticmethod
    def _distri_v2_alert_live(card):
        """The card would still be alerted by `_cron_alerts` (its domain,
        without the age)."""
        if card.kind != "income" and card.column in PRIORITY_COLUMNS and (
                card.state == "draft" or card.report_review):
            return True
        return card.kind == "verify" and card.state == "authorized" and not card.income_id

    @api.model
    def _distri_v2_close_old_alerts(self, apply=False, before=None, include_blank=False):
        """Marks the old alerts done with a note (`action_feedback`, the way
        `_close_alerts` closes one). Dry run unless `apply`. Returns
        {reason: count}. Writes only the activities and their notes."""
        found = self._distri_v2_old_alerts(before=before, include_blank=include_blank)
        counts = {key: len(activities) for key, activities in found.items()}
        if apply:
            for key, activities in found.items():
                if activities:
                    activities.with_context(mail_activity_quick_update=True).action_feedback(
                        feedback=OLD_ALERT_NOTE % OLD_ALERT_REASONS[key])
        return counts


class DistriVerificacionTramo2(models.AbstractModel):
    _inherit = "distri.verificacion"

    # ------------------------------------------------------------ rows

    def _card_row(self, card, key, user):
        row = super()._card_row(card, key, user)
        if key == "sin_comprobante" and card.kind == "income" and card.partner_id:
            row["claim"] = bool(card._distri_v2_claim_digits())
            # «otro cliente»: filled for all the rows at once in `_rows`
            row["other"] = ""
        return row

    def _stop_row(self, stop, user):
        """Queue 9: a delivery «a confirmar» escalated after 48 h."""
        stop = stop.sudo()
        card = stop.paid_confirm_card_id
        order = stop.order_id
        partner = stop.partner_id.commercial_partner_id
        url = ("/odoo/distri.mp.collection/%s" % card.id if card
               else "/odoo/sale.order/%s" % order.id if order else "")
        return {
            "model": "distri.route.stop", "id": stop.id,
            "name": "%s · entregado el %s" % (order.name or "", local_when(stop.paid_confirm_since)),
            "partner": partner.name or "Sin cliente",
            "amount": _money(card.amount if card else 0.0),
            "reason": "Entregado como «Ya estaba pagado · a confirmar» con el comprobante %s sin "
                      "acreditar. El reporte de Mercado Pago no lo trae: acreditalo, vinculalo "
                      "o avisá al repartidor." % (card.name or "que ya no está en el tablero"),
            "url": url, "check": False, "twin": False,
            "who": stop.route_id.driver_id.name or "",
        }

    @api.model
    def _rows(self, key, user=None, limit=None, filtro=""):
        """Queue 7 also lists by filter: «otro cliente» and «sin cliente»."""
        from .verificacion import LIST_LIMIT
        limit = limit or LIST_LIMIT
        user = user or self.env.user
        if key == "sin_comprobante" and filtro == FILTER_UNIDENTIFIED:
            return self._distri_v2_unidentified_rows(user, limit)
        if key == "sin_comprobante":
            rows = super()._rows(key, user, limit=OTHER_SCAN if filtro == FILTER_OTHER else limit)
            self._distri_v2_fill_other(rows)
            if filtro == FILTER_OTHER:
                rows = [r for r in rows if r.get("other")]
        else:
            rows = super()._rows(key, user, limit=limit)
        for row in rows:
            row.setdefault("claim", False)
            row.setdefault("other", "")
        return rows[:limit]

    def _distri_v2_fill_other(self, rows):
        """«Comprobante de otro cliente» of the queue-7 rows, two searches for
        all of them (19.0.1.129.39)."""
        ids = [r["id"] for r in rows if r.get("model") == "distri.mp.collection"]
        if not ids:
            return
        cards = self.env["distri.mp.collection"].sudo().browse(ids)
        found = cards._distri_v2_other_customer_map()
        by_id = {card.id: card for card in cards}
        for row in rows:
            hit = found.get(row.get("id")) if row.get("model") == "distri.mp.collection" else None
            if hit:
                row["other"] = by_id[row["id"]]._distri_v2_other_customer_text(hit)

    # ------------------------------------------------------------ «sin cliente» (#48)

    @api.model
    def _distri_v2_unidentified_domain(self):
        return [("kind", "=", "income"), ("state", "=", "draft"),
                ("column", "=", "unidentified"), ("historic", "=", False)]

    @api.model
    def _distri_v2_unidentified_count(self):
        return self.env["distri.mp.collection"].sudo().search_count(
            self._distri_v2_unidentified_domain())

    def _distri_v2_unidentified_rows(self, user, limit):
        cards = self.env["distri.mp.collection"].sudo().search(
            self._distri_v2_unidentified_domain(), order="date asc, id asc", limit=limit)
        now = fields.Datetime.now()
        from .verificacion import age_text
        rows = []
        for card in cards:
            row = self._card_row(card, "sin_cliente", user)
            payer = card.payer_label or card.payer_name or ""
            row.update(since=card.date, age=age_text(card.date, now), overdue=False,
                       claim=False, other="",
                       reason="Sin cliente: %s" % payer if payer else "Sin cliente")
            if not card.with_user(user).board_visible:
                # only Sistemas opens the report's incomes (19.0.1.129.11)
                row["url"] = ""
            rows.append(row)
        return rows

    # ------------------------------------------------------------ «Comprobantes subidos» (#10, #66)

    @api.model
    def _distri_v2_upload_who(self, msg):
        """Who or where a receipt came from, for the board."""
        msg = msg.sudo()
        if msg.distri_customer_order_id:
            return "Cliente · página del pedido"
        if msg.distri_upload:
            return msg.distri_forwarded_by_id.name or "Personal"
        if msg.distri_from_app or msg.distri_app_stop_id:
            driver = msg.distri_app_stop_id.route_id.driver_id
            return "App del repartidor%s" % (" · %s" % driver.name if driver else "")
        if msg.distri_chat_import_id:
            return "Exportación del chat de WhatsApp"
        if msg.distri_forwarded_by_id:
            return "%s · por WhatsApp" % msg.distri_forwarded_by_id.name
        return "Cliente · por WhatsApp"

    @api.model
    def _distri_v2_uploads(self, days=UPLOAD_DAYS, who="", bucket=""):
        """{"groups": [{who, total, buckets{key: n}}], "rows": [...], ...}:
        the receipts of the last `days` days by who sent them and state."""
        from .comprobante_subir import BUCKETS, _since
        days = max(1, min(int(days or UPLOAD_DAYS), UPLOAD_DAYS_MAX))
        msgs = self.env["distri.whatsapp.message"].sudo().search([
            ("distri_is_receipt", "=", True), ("direction", "=", "in"),
            ("create_date", ">=", _since(days))], order="id desc")
        groups = {}
        rows = []
        for msg in msgs:
            name = self._distri_v2_upload_who(msg)
            state = msg._distri_upload_bucket()
            group = groups.setdefault(name, {"who": name, "total": 0,
                                             "buckets": dict.fromkeys(BUCKETS, 0)})
            group["total"] += 1
            group["buckets"][state] += 1
            if (who and who != name) or (bucket and bucket != state) or len(rows) >= UPLOAD_LIMIT:
                continue
            rows.append({
                "id": msg.id, "who": name, "when": local_when(msg.create_date),
                "amount": _money(msg.distri_amount or msg.distri_amount_typed or 0.0),
                "partner": msg.distri_partner_id.commercial_partner_id.name or "Sin cliente",
                "state": dict(msg._fields["distri_receipt_state"].selection).get(
                    msg.distri_receipt_state, "") or "Leyendo",
                "bucket": state, "bucket_label": BUCKETS[state],
                "url": "/odoo/distri.whatsapp.message/%s" % msg.id,
            })
        from urllib.parse import urlencode
        for group in groups.values():
            group["url"] = "/verificacion/subidos?" + urlencode({"dias": days, "quien": group["who"]})
            group["urls"] = {key: "/verificacion/subidos?" + urlencode(
                {"dias": days, "quien": group["who"], "estado": key}) for key in BUCKETS}
        ordered = sorted(groups.values(), key=lambda g: (-g["total"], g["who"]))
        return {"days": days, "groups": ordered, "rows": rows, "total": len(msgs),
                "buckets": BUCKETS, "who": who, "bucket": bucket if bucket in BUCKETS else ""}


class DistriMpAuditTramo2(models.Model):
    """4. The audit ties a credited receipt to its transfer too: a round
    amount needs the exact operation number there as well."""
    _inherit = "distri.mp.audit"

    def _untied_state(self, card, claimed, coverage, found, values, stats):
        if verificacion_on(self.env):
            level, incomes = card._distri_audit_candidates(claimed)
            if len(incomes) == 1 and incomes.state == "draft" and level \
                    and level != "near" and incomes._distri_v2_round_hold(level):
                income = incomes
                found["revisar:%s" % card.id] = dict(
                    values, kind="revisar", income_id=income.id,
                    detail="Importe redondo: puede ser %s (%s), lo vincula una persona." % (
                        income.name, OTHER_LEVEL_TEXT.get(level, level)))
                if income.proposal != "credited":
                    income._distri_v2_send_round_review(card, level)
                stats["revisar"] += 1
                return "pendiente"
        return super()._untied_state(card, claimed, coverage, found, values, stats)


OTHER_LEVEL_TEXT = {
    "operation_close": "número de operación con un dígito distinto y mismo importe",
    "time": "mismo importe y hora",
    "customer": "mismo importe, cliente y día",
}
