"""«Transferencias» inside /cuentas (19.0.1.129.8).

Gerencia reviews from the phone the transfers the staff loaded or credited:
the same rows as the backend «Transferencias cargadas» list
(models/transferencias_cargadas.py, the read-only SQL model
`distri.loaded.transfer`), read here with `search_read` / `read_group` as the
logged-in user, so its access rights (Administración, Gerencia, Sistemas)
and its company rule apply. The audit check (`_check_audit`, AUDIT_GROUPS)
runs first: a seller or a driver opens /cuentas but never this section.

Read-only: nothing here writes. The receipt file is served only for a row
the user can read.
"""
from datetime import timedelta, timezone

from odoo import api, models
from odoo.exceptions import AccessError, UserError

from .acreditar import AUDIT_STATES
from .auditoria import ANOMALY_KINDS, _check_audit, audit_user
from .cuentas_app import _parse_day
from .reparto_cliente import AR_TZ, today_ar
from .transferencias_cargadas import LOAD_STATES

TRANSFER_PAGE_SIZE = 50
TRANSFER_PERIODS = ("today", "yesterday", "week", "month", "custom")
TRANSFER_STATES = ("all", "not_matched", "anomaly", "no_receipt")
TRANSFER_FIELDS = [
    "loaded_at", "loaded_by_id", "uploaded_by_id", "load_state", "partner_id", "amount",
    "operation", "destination", "audit_state", "without_receipt", "matched", "income_date",
    "confirmed", "anomaly_id", "with_anomaly", "receipt_id",
]


def _local(moment):
    """Naive UTC datetime -> aware Argentine datetime (or None)."""
    return moment.replace(tzinfo=timezone.utc).astimezone(AR_TZ) if moment else None


class DistriAccountAppTransfers(models.AbstractModel):
    _inherit = "distri.account.app"

    @api.model
    def _app_can_see_transfers(self):
        return audit_user(self.env.user)

    @api.model
    def _app_transfers_check(self):
        """The /cuentas scope and then the audit groups (both on the server)."""
        self._app_check()
        _check_audit(self.env)

    @api.model
    def _app_transfer_range(self, period, date_from=None, date_to=None):
        """(period, first day, last day) in Argentina; either end may be open
        for «custom»."""
        today = today_ar()
        if period not in TRANSFER_PERIODS:
            period = "today"
        if period == "today":
            return period, today, today
        if period == "yesterday":
            yesterday = today - timedelta(days=1)
            return period, yesterday, yesterday
        if period == "week":  # since Monday, like the backend «Esta semana»
            return period, today - timedelta(days=today.weekday()), today
        if period == "month":
            return period, today.replace(day=1), today
        start, end = _parse_day(date_from), _parse_day(date_to)
        if start and end and start > end:
            raise UserError("La fecha «Desde» es posterior a «Hasta».")
        return period, start or False, end or False

    @api.model
    def _app_transfer_domain(self, start, end, user_id=0, state="all"):
        domain = []
        if start:
            domain.append(("loaded_day", ">=", start))
        if end:
            domain.append(("loaded_day", "<=", end))
        if user_id:
            domain.append(("loaded_by_id", "=", user_id))
        if state == "not_matched":
            domain.append(("matched", "=", False))
        elif state == "anomaly":
            domain.append(("with_anomaly", "=", True))
        elif state == "no_receipt":
            domain.append(("without_receipt", "=", True))
        return domain

    @api.model
    def app_transfers(self, period="today", date_from=None, date_to=None, user_id=0,
                      state="all", offset=0, limit=TRANSFER_PAGE_SIZE):
        """One page of the loaded transfers of the filter, newest first, with
        the count and amount of the whole filter, the subtotal per person
        and (first page) the people who ever loaded one."""
        self._app_transfers_check()
        period, start, end = self._app_transfer_range(period, date_from, date_to)
        try:
            user_id = max(int(user_id or 0), 0)
        except (TypeError, ValueError):
            user_id = 0
        if state not in TRANSFER_STATES:
            state = "all"
        offset = max(int(offset or 0), 0)
        limit = min(max(int(limit or TRANSFER_PAGE_SIZE), 1), 200)
        Loaded = self.env["distri.loaded.transfer"]
        domain = self._app_transfer_domain(start, end, user_id, state)
        (count, amount), = Loaded._read_group(domain, [], ["__count", "amount:sum"])
        rows = Loaded.search_read(domain, TRANSFER_FIELDS, offset=offset, limit=limit)
        by_person = [{
            "id": user.id or 0,
            "name": user.name or "Sin usuario",
            "count": n,
            "amount": round(total or 0.0, 2),
        } for user, n, total in Loaded._read_group(
            domain, ["loaded_by_id"], ["__count", "amount:sum"], order="amount:sum desc")]
        result = {
            "period": period,
            "date_from": start.isoformat() if start else "",
            "date_to": end.isoformat() if end else "",
            "user_id": user_id,
            "state": state,
            "offset": offset,
            "count": count,
            "amount": round(amount or 0.0, 2),
            "more": offset + len(rows) < count,
            "rows": self._app_transfer_rows(rows),
            "by_person": by_person,
        }
        if not offset:
            people = Loaded._read_group([("loaded_by_id", "!=", False)], ["loaded_by_id"])
            result["people"] = sorted(
                ({"id": user.id, "name": user.name or ""} for user, in people),
                key=lambda p: p["name"].lower())
        return result

    @api.model
    def _app_transfer_rows(self, rows):
        """search_read rows -> the app payload; the receipt files and the
        anomaly kinds in one batch each (no read per row)."""
        Message = self.env["distri.whatsapp.message"].sudo()
        messages = Message.browse([r["receipt_id"][0] for r in rows if r["receipt_id"]])
        mimes = {m.id: m.attachment_id.mimetype or "" for m in messages if m.attachment_id}
        anomalies = self.env["distri.mp.anomaly"].sudo().browse(
            [r["anomaly_id"][0] for r in rows if r["anomaly_id"]])
        kinds = dict(ANOMALY_KINDS)
        anomaly_kind = {a.id: kinds.get(a.kind, "") for a in anomalies}
        audit_labels = dict(AUDIT_STATES)
        load_labels = dict(LOAD_STATES)
        out = []
        for r in rows:
            moment = _local(r["loaded_at"])
            income = _local(r["income_date"])
            message_id = r["receipt_id"] and r["receipt_id"][0]
            mime = mimes.get(message_id, "")
            out.append({
                "id": r["id"],
                "date": moment.date().isoformat() if moment else "",
                "time": moment.strftime("%H:%M") if moment else "",
                "loaded_by": r["loaded_by_id"] and r["loaded_by_id"][1] or "",
                "uploaded_by": r["uploaded_by_id"] and r["uploaded_by_id"][1] or "",
                "load_state": r["load_state"] or "",
                "load_label": load_labels.get(r["load_state"], ""),
                "partner_id": r["partner_id"] and r["partner_id"][0] or 0,
                "partner": r["partner_id"] and r["partner_id"][1] or "",
                "amount": r["amount"] or 0.0,
                "operation": r["operation"] or "",
                "destination": r["destination"] or "",
                "audit_state": r["audit_state"] or "",
                "audit_label": audit_labels.get(r["audit_state"], ""),
                "matched": bool(r["matched"]),
                "income_date": income.strftime("%d/%m/%Y %H:%M") if income else "",
                "confirmed": bool(r["confirmed"]),
                "with_anomaly": bool(r["with_anomaly"]),
                "anomaly": anomaly_kind.get(r["anomaly_id"] and r["anomaly_id"][0], ""),
                "without_receipt": bool(r["without_receipt"]),
                "has_file": bool(mime),
                "file_kind": "image" if mime.startswith("image/") and mime != "image/svg+xml"
                else "pdf" if mime == "application/pdf" else "other" if mime else "",
            })
        return out

    @api.model
    def app_transfer_receipt(self, transfer_id):
        """(bytes, mimetype, filename) of the receipt of one loaded transfer
        the user can read; AccessError otherwise."""
        self._app_transfers_check()
        try:
            wanted = int(transfer_id or 0)
        except (TypeError, ValueError):
            wanted = 0
        rows = self.env["distri.loaded.transfer"].search_read(
            [("id", "=", wanted)], ["receipt_id"], limit=1) if wanted > 0 else []
        message_id = rows and rows[0]["receipt_id"] and rows[0]["receipt_id"][0]
        attachment = self.env["distri.whatsapp.message"].sudo().browse(
            message_id).attachment_id if message_id else False
        if not attachment:
            raise AccessError("No hay comprobante para esa transferencia.")
        return attachment.raw, attachment.mimetype or "application/octet-stream", \
            attachment.name or "comprobante"
