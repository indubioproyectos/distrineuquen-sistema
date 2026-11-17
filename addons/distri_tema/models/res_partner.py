import re
from urllib.parse import quote

from odoo import _, models
from odoo.exceptions import UserError


def whatsapp_digits(phone):
    """Digits for https://wa.me/<digits> from an Argentine phone as typed.

    WhatsApp needs the international mobile form 54 9 <area> <number>:
    «+54 299 327-2591» -> 5492993272591, «0299 15 327-2591» -> 5492993272591.
    Returns "" when nothing usable is left.
    """
    digits = re.sub(r"\D", "", phone or "")
    if not digits:
        return ""
    if digits.startswith("54"):
        rest = digits[2:]
    else:
        rest = digits.lstrip("0")
    if rest.startswith("9") and len(rest) == 11:
        rest = rest[1:]
    # old local mobile prefix «15» after a 2-4 digit area code (0299 15 ...)
    if len(rest) == 12:
        for area in (2, 3, 4):
            if rest[area:area + 2] == "15":
                rest = rest[:area] + rest[area + 2:]
                break
    if len(rest) != 10:
        # not an Argentine number we understand: hand it to WhatsApp as is
        return digits
    return "549" + rest


class ResPartner(models.Model):
    _inherit = "res.partner"

    def _distri_open_url(self, url, target):
        return {"type": "ir.actions.act_url", "url": url, "target": target}

    def action_distri_call(self):
        self.ensure_one()
        if not self.phone:
            raise UserError(_("Este contacto no tiene teléfono cargado."))
        return self._distri_open_url("tel:%s" % re.sub(r"[^\d+]", "", self.phone), "self")

    def action_distri_whatsapp(self):
        self.ensure_one()
        digits = whatsapp_digits(self.phone)
        if not digits:
            raise UserError(_("Este contacto no tiene teléfono cargado."))
        return self._distri_open_url("https://wa.me/%s" % digits, "new")

    def action_distri_directions(self):
        self.ensure_one()
        parts = [self.street, self.street2, self.city, self.state_id.name, self.country_id.name]
        address = ", ".join(p for p in parts if p)
        if not (self.street or self.city):
            raise UserError(_("Este contacto no tiene dirección cargada."))
        return self._distri_open_url(
            "https://www.google.com/maps/dir/?api=1&destination=%s" % quote(address), "new")
