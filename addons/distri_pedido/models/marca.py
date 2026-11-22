"""19.0.1.129.4: no user-visible «Odoo»; the system is «DistriNeuquén».

Server side of the branding (the templates are in views/marca.xml, the web
client in static/src/js/marca.js):

- `mail.template._distri_debrand_templates`: the email templates of the core
  modules are noupdate records with «Powered by Odoo», the Odoo logo and, in
  the invitation, a paragraph about Odoo. Cleaned in every language, straight
  in SQL (writing through the ORM would run the outgoing-email sanitizer over
  the QWeb source). Runs on every update (data/marca.xml), idempotent.
- `res.partner._distri_brand_partner_root`: the system partner (author of the
  automatic chatter messages) is «DistriNeuquén», not «OdooBot», unless
  someone renamed it already.
- mail_bot: no onboarding chat («El chat de Odoo ayuda...») on the first
  login and no bot answers; the bot keeps its technical name.
"""
import json
import logging
import re

from odoo import api, models

_logger = logging.getLogger(__name__)

BRAND = "DistriNeuquén"
ROOT_DEFAULT_NAME = "OdooBot"

# Applied in order to each language of body_html.
_BODY_RULES = [
    # «Powered by <a href="https://www.odoo.com...">Odoo</a>» (any language):
    # the text right before the link goes with it
    (re.compile(r"(?<=>)[^<>]*<a\b[^>]*odoo\.com[^>]*>\s*Odoo\s*</a>"), ""),
    # «Have a look at the <a href=".../page/tour">Odoo Tour</a> to discover...»
    (re.compile(r"(?<=>)[^<>]*<a\b[^>]*odoo\.com/page/tour[^>]*>[^<]*</a>[^<]*"), ""),
    # the Odoo logo of the invoice emails
    (re.compile(r"<img\b[^>]*\balt=\"Odoo\"[^>]*>"), ""),
    # «Never heard of Odoo? It's an all-in-one business software...»
    (re.compile(r"(?<=>)[^<>]*\bOdoo\?[^<>]*"), "\n"),
]
_TAG = re.compile(r"(<[^>]+>)")
_WORD = re.compile(r"\bOdoo\b(?!\.?\w)")


def debrand_body(html):
    """`html` without «Powered by Odoo», the Odoo logo and the Odoo
    paragraphs; every other visible «Odoo» (text, never tags, attributes or
    URLs) becomes «DistriNeuquén»."""
    if not html or "Odoo" not in html:
        return html
    for pattern, repl in _BODY_RULES:
        html = pattern.sub(repl, html)
    parts = _TAG.split(html)
    return "".join(part if i % 2 else _WORD.sub(BRAND, part) for i, part in enumerate(parts))


def debrand_subject(subject):
    return _WORD.sub(BRAND, subject) if subject and "Odoo" in subject else subject


class MailTemplate(models.Model):
    _inherit = "mail.template"

    @api.model
    def _distri_debrand_templates(self):
        cr = self.env.cr
        cr.execute("""SELECT id, body_html, subject FROM mail_template
                       WHERE body_html::text LIKE '%%Odoo%%' OR subject::text LIKE '%%Odoo%%'""")
        changed = []
        for template_id, body, subject in cr.fetchall():
            new_body = {lang: debrand_body(value) for lang, value in (body or {}).items()} or None
            new_subject = {lang: debrand_subject(value) for lang, value in (subject or {}).items()} or None
            if new_body == body and new_subject == subject:
                continue
            cr.execute("UPDATE mail_template SET body_html = %s, subject = %s WHERE id = %s",
                       (json.dumps(new_body) if new_body else None,
                        json.dumps(new_subject) if new_subject else None, template_id))
            changed.append(template_id)
        if changed:
            self.browse(changed).invalidate_recordset(["body_html", "subject"])
            _logger.info("branding: %s email templates without Odoo: %s", len(changed), changed)
        return changed


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model
    def _distri_brand_partner_root(self):
        root = self.env.ref("base.partner_root", raise_if_not_found=False)
        if root and root.name == ROOT_DEFAULT_NAME:
            root.sudo().name = BRAND


class ResUsers(models.Model):
    _inherit = "res.users"

    def _init_odoobot(self):
        """No onboarding chat with the bot: mark it done without a message."""
        self.ensure_one()
        self.sudo().odoobot_state = "disabled"


class MailBot(models.AbstractModel):
    _inherit = "mail.bot"

    def _apply_logic(self, channel, values, command=None):
        """The bot never answers (its texts are about Odoo)."""
        return None
