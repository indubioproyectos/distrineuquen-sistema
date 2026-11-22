"""19.0.1.129.4: no user-visible «Odoo»; the system is «DistriNeuquén».

What a staff member or a customer can read (page text, tab title, tooltips,
alt texts, emails) never says Odoo. URLs (/odoo, /web), asset paths, script
code and technical attributes are not visible and are ignored.
"""
import re
from unittest.mock import patch

from lxml import etree, html as lxml_html

from odoo.tests import HttpCase, TransactionCase, new_test_user, tagged
from odoo.tools import config, file_open

from odoo.addons.distri_pedido.models.estado_cuenta import SOURCE_LABELS
from odoo.addons.distri_pedido.models.marca import debrand_body

BRAND = "DistriNeuquén"
ODOO = re.compile(r"\bodoo\b", re.I)
URLS = re.compile(r"https?://\S+|\S*/odoo\S*|\S+\.odoo\.com\S*")
VISIBLE_ATTRS = ("title", "alt", "placeholder", "aria-label")
HIDDEN_TAGS = ("script", "style", "noscript", "template")


def visible_text(page):
    """Text a person can read on `page`: text nodes (no script, style or
    comments), the tooltip/alt/placeholder attributes, button values and the
    tab title. URLs are dropped."""
    doc = lxml_html.fromstring(page)
    etree.strip_elements(doc, etree.Comment, *HIDDEN_TAGS, with_tail=False)
    chunks = [doc.text_content()]
    for element in doc.iter():
        if not isinstance(element.tag, str):
            continue
        chunks.extend(element.get(attr) or "" for attr in VISIBLE_ATTRS)
        if element.tag == "input" and element.get("type") in ("button", "submit"):
            chunks.append(element.get("value") or "")
    return URLS.sub(" ", "\n".join(chunks))


def odoo_mentions(page):
    text = visible_text(page)
    return [text[max(0, m.start() - 40):m.end() + 40] for m in ODOO.finditer(text)]


def page_title(page):
    return (lxml_html.fromstring(page).findtext(".//title") or "").strip()


def label_texts(arch):
    """Text nodes and label attributes of a view arch (no comments/scripts)."""
    root = etree.fromstring(arch.encode(), etree.XMLParser(remove_comments=True, recover=True))
    if root is None:
        return []
    etree.strip_elements(root, "script", "style", with_tail=False)
    texts = []
    for element in root.iter():
        if not isinstance(element.tag, str):
            continue
        texts += [element.text or "", element.tail or ""]
        texts += [element.get(attr) or "" for attr in ("string", "help", "title", "placeholder",
                                                        "alt", "confirm", "aria-label")]
    return texts


@tagged("post_install", "-at_install", "distri_v129_4")
class TestMarcaPaginas(HttpCase):

    def test_login_page_has_no_odoo(self):
        response = self.url_open("/web/login")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(odoo_mentions(response.text), [])
        self.assertNotIn("Powered by", response.text)
        self.assertNotIn("odoo.com", response.text)
        self.assertNotIn("Odoo", page_title(response.text))

    def test_backend_title_and_page_have_no_odoo(self):
        new_test_user(self.env, login="v1294_staff", password="v1294_staff_pw1",
                      groups="base.group_user,sales_team.group_sale_salesman")
        self.authenticate("v1294_staff", "v1294_staff_pw1")
        # the web client page itself (bare /odoo goes to /tablero, checked below)
        response = self.url_open("/odoo/discuss")
        self.assertEqual(response.status_code, 200)
        self.assertIn("o_web_client", response.text)
        self.assertEqual(page_title(response.text), BRAND)
        self.assertEqual(odoo_mentions(response.text), [])
        self.assertIn('rel="shortcut icon" href="/distri_pedido/static/src/img/iconos/', response.text)
        home = self.url_open("/odoo")
        self.assertEqual(home.status_code, 200)
        self.assertEqual(odoo_mentions(home.text), [])

    def test_database_pages_answer_404_without_list_db(self):
        with patch.dict(config.options, {"list_db": False}):
            for url in ("/web/database/selector", "/web/database/manager"):
                response = self.url_open(url)
                self.assertEqual(response.status_code, 404, url)
                self.assertEqual(odoo_mentions(response.text), [], url)

    def test_offline_page_has_no_odoo(self):
        response = self.url_open("/odoo/offline")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(odoo_mentions(response.text), [])
        self.assertIn("Sin conexión", response.text)
        self.assertEqual(page_title(response.text), "Sin conexión · DistriNeuquén")


@tagged("post_install", "-at_install", "distri_v129_4")
class TestMarcaServidor(TransactionCase):

    def test_powered_by_templates_are_clean(self):
        promotion = self.env["ir.qweb"]._render("web.brand_promotion_message",
                                                {"_message": "", "_utm_medium": "portal"})
        self.assertEqual(str(promotion).strip(), "")
        for xmlid in ("web.brand_promotion_message", "web.login_layout",
                      "portal.portal_record_sidebar", "mail.mail_notification_layout",
                      "mail.mail_notification_light", "auth_signup.reset_password_email",
                      "digest.digest_mail_main"):
            arch = etree.tostring(self.env.ref(xmlid)._get_combined_arch(), encoding="unicode")
            self.assertNotIn("odoo.com", arch, xmlid)
            self.assertNotIn("Powered by", arch, xmlid)
            self.assertNotIn("Odoo Mobile", arch, xmlid)

    def test_email_layouts_without_powered_by(self):
        partner = self.env["res.partner"].create({"name": "Cliente v1294", "email": "c1294@example.com"})
        for layout in ("mail.mail_notification_layout", "mail.mail_notification_light"):
            body = self.env["mail.render.mixin"]._render_encapsulate(
                layout, "<p>Tu pedido está listo</p>", context_record=partner,
                add_context={"email_notification_force_footer": True, "show_unfollow": True,
                             "company": self.env.company})
            self.assertIn("Tu pedido está listo", body, layout)
            self.assertNotIn("Powered by", body, layout)
            self.assertNotIn("odoo.com", body, layout)
            self.assertEqual(odoo_mentions(body), [], layout)
            self.assertIn("mail_unfollow", body, layout)

    def test_mail_templates_cleaned(self):
        self.env["mail.template"]._distri_debrand_templates()
        for xmlid in ("auth_signup.set_password_email", "auth_signup.reset_password_email",
                      "auth_signup.mail_template_user_signup_account_created",
                      "auth_signup.portal_set_password_email"):
            template = self.env.ref(xmlid, raise_if_not_found=False)
            if not template or template._name != "mail.template":
                continue
            self.env.cr.execute("SELECT body_html, subject FROM mail_template WHERE id = %s",
                                [template.id])
            body, subject = self.env.cr.fetchone()
            for lang, value in (body or {}).items():
                self.assertNotIn("Powered by", value, (xmlid, lang))
                self.assertNotIn("Con la tecnología de", value, (xmlid, lang))
                self.assertEqual(odoo_mentions(value), [], (xmlid, lang))
            for lang, value in (subject or {}).items():
                self.assertNotIn("Odoo", value, (xmlid, lang))
        # idempotent
        self.assertEqual(self.env["mail.template"]._distri_debrand_templates(), [])

    def test_debrand_body_keeps_the_template(self):
        source = ('<div>Hola <t t-out="object.name">Marc</t>, te invita <t t-out="x">OdooBot</t> a Odoo.'
                  '<a href="/web/login?db=odoo">Entrar</a><br/><br/>\n'
                  'Never heard of Odoo? It is great.\n<br/>'
                  '<img alt="Odoo" src="/web/static/img/logo.png"/>'
                  '<p>Powered by <a href="https://www.odoo.com?utm_source=db">Odoo</a></p></div>')
        result = debrand_body(source)
        self.assertIn('<t t-out="object.name">Marc</t>', result)
        self.assertIn("OdooBot", result)  # a sample value, replaced when sending
        self.assertIn("a %s." % BRAND, result)
        self.assertIn('href="/web/login?db=odoo"', result)
        self.assertNotIn("Never heard", result)
        self.assertNotIn("Powered by", result)
        self.assertNotIn('alt="Odoo"', result)

    def test_system_partner_and_bot(self):
        self.assertEqual(self.env.ref("base.partner_root").name, BRAND)
        user = new_test_user(self.env, login="v1294_bot", groups="base.group_user")
        user.odoobot_state = "not_initialized"
        user._init_odoobot()
        self.assertEqual(user.odoobot_state, "disabled")
        chat = self.env["discuss.channel"].search([
            ("channel_type", "=", "chat"),
            ("channel_member_ids.partner_id", "=", user.partner_id.id),
            ("channel_member_ids.partner_id", "=", self.env.ref("base.partner_root").id)])
        self.assertFalse(chat.message_ids, "no onboarding message about Odoo")

    def test_user_menu_items_removed(self):
        bundle = self.env["ir.qweb"]._get_asset_bundle("web.assets_backend")
        urls = [f.get("url") or f.get("filename") or "" for f in bundle.files]
        self.assertTrue(any(u.endswith("distri_pedido/static/src/js/marca.js") for u in urls))
        with file_open("distri_pedido/static/src/js/marca.js") as f:
            ours = f.read()
        removed = re.search(r"REMOVED_USER_MENU_ITEMS = \[([^\]]*)\]", ours).group(1)
        removed = set(re.findall(r'"([^"]+)"', removed))
        self.assertEqual(removed, {"support", "shortcuts", "separator", "odoo_account"})
        # the keys are core's: a rename in core would leave the item in place
        with file_open("web/static/src/webclient/user_menu/user_menu_items.js") as f:
            core = f.read()
        core_keys = set(re.findall(r'\.add\("([a-z_]+)"', core))
        self.assertLessEqual(removed, core_keys)
        self.assertIn("preferences", core_keys - removed)
        self.assertIn("log_out", core_keys - removed)
        # the tab title fallback is the brand, the service replaces core's
        self.assertIn('Object.values(titleParts).join(" - ") || BRAND', ours)
        self.assertIn('registry.category("services").add("title", distriTitleService, { force: true })', ours)

    def test_module_strings_renamed(self):
        self.assertEqual(self.env["account.payment"]._fields["distri_history_invoice_id"].string,
                         "Factura del sistema anterior")
        self.assertEqual(self.env["distri.agil.equivalencia"]._fields["product_id"].string,
                         "Producto del sistema")
        self.assertEqual(self.env["res.partner"]._fields["saldo_odoo"].string,
                         "Saldo en el sistema anterior")
        self.assertEqual(SOURCE_LABELS["online"], "Sistema anterior")
        self.assertEqual(SOURCE_LABELS["odoo"], BRAND)
        self.assertEqual(self.env.ref("distri_pedido.menu_puesta_export").name,
                         "Exportar para el sistema anterior")
        self.assertEqual(self.env.ref("distri_pedido.menu_puesta_online_payments").name,
                         "Pagos del sistema anterior (lectura)")

    def test_no_odoo_in_module_labels(self):
        data = self.env["ir.model.data"].sudo()
        word = re.compile(r"\bOdoo\b")

        def ids(model):
            return data.search([("module", "=", "distri_pedido"), ("model", "=", model)]).mapped("res_id")

        for view in self.env["ir.ui.view"].browse(ids("ir.ui.view")):
            found = [t.strip() for t in label_texts(view.arch_db or "<t/>") if word.search(t)]
            self.assertEqual(found, [], view.xml_id)
        for menu in self.env["ir.ui.menu"].browse(ids("ir.ui.menu")):
            self.assertNotRegex(menu.name, word)
        for field in self.env["ir.model.fields"].browse(ids("ir.model.fields")):
            self.assertNotRegex(field.field_description or "", word, field.name)
            self.assertNotRegex(field.help or "", word, field.name)
        for model in self.env["ir.model"].browse(ids("ir.model")):
            self.assertNotRegex(model.name or "", word, model.model)
        for project in self.env["distri.project.module"].browse(ids("distri.project.module")):
            for fname in ("name", "description", "next_step"):
                self.assertNotRegex(project[fname] or "", word, (project.code, fname))
