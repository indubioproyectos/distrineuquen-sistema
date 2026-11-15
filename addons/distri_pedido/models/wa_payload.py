"""Pure helpers for the WhatsApp Cloud API webhook payload (19.0.1.88.0).

No models here: models/whatsapp.py, models/pedido_whatsapp.py and
models/cobros.py import these names, and model modules must not import each
other (overrides are built in import order).

Roles of the receiving numbers (`distri.whatsapp.numero`):
- ROLE_BOTH: orders and receipts, the behaviour before several numbers.
- ROLE_RECEIPTS: drivers and salespeople send payment receipts. Never
  confirms an order by code, never learns or verifies a customer's phone,
  every image or PDF is a receipt candidate.
- ROLE_ORDERS: orders only; photos are stored and downloaded but not read as
  receipts unless someone marks them by hand.
A message to a phone_number_id that is not configured is stored with
ROLE_BOTH and flagged, so nothing is lost.

Senders without a phone (WhatsApp usernames, 2026): Meta identifies a user
by a business-scoped user id (BSUID, e.g. "US.13491208655302741918") in
`contacts[].user_id` and `messages[].from_user_id`, and may omit (or null)
`contacts[].wa_id` and `messages[].from`. `sender_identity` reads both shapes
defensively and never raises.
"""
import re

ROLE_BOTH = "both"
ROLE_RECEIPTS = "receipts"
ROLE_ORDERS = "orders"
ROLES = [
    (ROLE_BOTH, "Pedidos y comprobantes"),
    (ROLE_RECEIPTS, "Solo comprobantes"),
    (ROLE_ORDERS, "Solo pedidos"),
]
DEFAULT_ROLE = ROLE_BOTH

# Media ids received by webhook expire after about 7 days (Meta); each
# download URL obtained from the media id lives about 5 minutes.
MEDIA_EXPIRY_DAYS = 7
# Alerts of media not downloaded (models/whatsapp.py, hourly cron)
MEDIA_ALERT_LATE_HOURS = 2
MEDIA_ALERT_EXPIRING_DAYS = 5

_PHONE_RE = re.compile(r"^\+?\d{6,20}$")
USER_ID_MAX = 128


def _clean(value, limit):
    if value is None or isinstance(value, (dict, list, tuple, set, bool)):
        return ""
    return str(value).strip()[:limit]


def phone_or_empty(value):
    """Digits of a phone sent by Meta («5492995551234»), or "" when the
    value is missing, null or not a phone (a BSUID in `from`)."""
    text = _clean(value, 32)
    return text.lstrip("+") if _PHONE_RE.match(text) else ""


def _contact_for(contacts, phone, user_id):
    """The contact of the sender: by phone, else by BSUID, else the only one."""
    valid = [c for c in (contacts or []) if isinstance(c, dict)]
    for contact in valid:
        if phone and phone_or_empty(contact.get("wa_id")) == phone:
            return contact
    for contact in valid:
        if user_id and _clean(contact.get("user_id"), USER_ID_MAX) == user_id:
            return contact
    return valid[0] if len(valid) == 1 else {}


def sender_identity(message, contacts):
    """{"phone", "user_id", "username", "name"} of an incoming message.

    `phone` is digits or "" (never None). `from` may be missing, null, or
    carry a BSUID instead of a phone; `from_user_id` and the contact's
    `user_id` carry the BSUID."""
    message = message if isinstance(message, dict) else {}
    raw_from = _clean(message.get("from"), USER_ID_MAX)
    phone = phone_or_empty(raw_from)
    user_id = _clean(message.get("from_user_id"), USER_ID_MAX)
    if not user_id and raw_from and not phone:
        # some providers put the BSUID in `from` for username-only users
        user_id = raw_from
    contact = _contact_for(contacts, phone, user_id)
    profile = contact.get("profile") if isinstance(contact.get("profile"), dict) else {}
    if not phone:
        phone = phone_or_empty(contact.get("wa_id"))
    if not user_id:
        user_id = _clean(contact.get("user_id"), USER_ID_MAX)
    username = (_clean(message.get("username"), 64)
                or _clean(profile.get("username"), 64))
    return {
        "phone": phone,
        "user_id": user_id,
        "username": username,
        "name": _clean(profile.get("name"), 256),
    }


def status_recipient(status):
    """(phone, user_id) of the recipient of a status update."""
    status = status if isinstance(status, dict) else {}
    raw = _clean(status.get("recipient_id"), USER_ID_MAX)
    phone = phone_or_empty(raw)
    user_id = _clean(status.get("recipient_user_id"), USER_ID_MAX)
    if not user_id and raw and not phone:
        user_id = raw
    return phone, user_id
