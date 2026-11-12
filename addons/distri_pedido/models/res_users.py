from odoo import api, fields, models

# Every user works in Argentina. Odoo takes the user's time zone for the
# dates it sets by itself (invoice and credit note dates, "today" in
# filters) and for the times it prints (reports, chatter, web pages). The
# web client shows times in the browser's zone and warns when it differs
# from the user's.
DEFAULT_TZ = "America/Argentina/Buenos_Aires"
# Settings > Technical > System Parameters: "1" locks sellers to /cuentas
PARAM_SELLER_ONLY_ACCOUNTS = "distri_pedido.vendedor_solo_cuentas"


class ResUsers(models.Model):
    _inherit = "res.users"

    etiqueta_cartera_id = fields.Many2one(
        "res.partner.category",
        string="Cartera de clientes",
        help="El vendedor ve unicamente los clientes que tengan esta etiqueta.",
    )

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + ["etiqueta_cartera_id"]

    @api.model_create_multi
    def create(self, vals_list):
        """New users (internal and portal) start in Argentina's time zone."""
        Partner = self.env["res.partner"].sudo()
        for vals in vals_list:
            if vals.get("tz"):
                continue
            partner = Partner.browse(vals["partner_id"]) if vals.get("partner_id") else Partner
            if not partner.tz:
                vals["tz"] = DEFAULT_TZ
        return super().create(vals_list)

    def _distri_landing(self):
        """The screen of a user who only packs or delivers: /reparto for
        the driver, /armado for the packer. Administración, gerencia and
        system administrators keep the backend (empty string)."""
        self.ensure_one()
        user = self.sudo()
        if user.share or user.has_group("distri_pedido.grupo_administracion") \
                or user.has_group("base.group_system"):
            return ""
        if user.has_group("distri_pedido.grupo_reparto"):
            return "/reparto"
        if user.has_group("distri_pedido.grupo_armador"):
            return "/armado"
        if user._distri_seller_only_accounts():
            return "/cuentas"
        return ""

    def _distri_seller_only_accounts(self):
        """12/11/2026: while PARAM_SELLER_ONLY_ACCOUNTS is "1" a seller works
        only in the accounts app (/cuentas): every backend page sends him
        there (controllers/tienda.py web_client). Administración, gerencia
        and system administrators are never locked. /pedido keeps its own
        switch (distri_pedido.pedido_pausado)."""
        self.ensure_one()
        user = self.sudo()
        if user.share or user.has_group("distri_pedido.grupo_administracion") \
                or user.has_group("base.group_system"):
            return False
        if not user.has_group("distri_pedido.grupo_vendedor"):
            return False
        return self.env["ir.config_parameter"].sudo().get_param(PARAM_SELLER_ONLY_ACCOUNTS) == "1"

    @api.model
    def _distri_set_argentina_tz(self):
        """Users without a time zone (all of them before 19.0.1.8.0), the
        public user included, get Argentina's. Called on every module update;
        a zone set on purpose is kept."""
        users = self.sudo().with_context(active_test=False).search([("tz", "=", False)])
        users.write({"tz": DEFAULT_TZ})
        companies = self.env["res.company"].sudo().search([])
        companies.partner_id.filtered(lambda p: not p.tz).write({"tz": DEFAULT_TZ})
        return len(users)
