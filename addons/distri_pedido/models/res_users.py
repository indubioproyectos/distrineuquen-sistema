from odoo import api, fields, models

# Every user works in Argentina. Odoo takes the user's time zone for the
# dates it sets by itself (invoice and credit note dates, "today" in
# filters) and for the times it prints (reports, chatter, web pages). The
# web client shows times in the browser's zone and warns when it differs
# from the user's.
DEFAULT_TZ = "America/Argentina/Buenos_Aires"
# Settings > Technical > System Parameters: "1" locks sellers to /cuentas
PARAM_SELLER_ONLY_ACCOUNTS = "distri_pedido.vendedor_solo_cuentas"
ARMADO_VISTAS = [("tradicional", "Tradicional"), ("tarjetas", "Tarjetas")]
ARMADO_ORDENES = [("alfabetico", "Alfabético"), ("secciones", "Por secciones")]
# the roles named on the «Ajustes» sheet, in this order
ROLES = [
    ("distri_pedido.grupo_gerencia", "Gerencia"),
    ("distri_pedido.grupo_administracion", "Administración"),
    ("distri_pedido.grupo_cajero", "Cajero"),
    ("distri_pedido.grupo_vendedor", "Vendedor"),
    ("distri_pedido.grupo_armador", "Armador"),
    ("distri_pedido.grupo_reparto", "Reparto"),
]


class ResUsers(models.Model):
    _inherit = "res.users"

    etiqueta_cartera_id = fields.Many2one(
        "res.partner.category",
        string="Cartera de clientes",
        help="El vendedor ve unicamente los clientes que tengan esta etiqueta.",
    )

    # 19.0.1.98.0: how /armado shows an order, chosen by the packer (screen
    # toggle and the «Ajustes» sheet). Independent: any view with any order.
    distri_armado_vista = fields.Selection(
        ARMADO_VISTAS, string="Armado: vista", default="tradicional", required=True,
        help="Tradicional: el pedido entero como el mensaje de WhatsApp. Tarjetas: un "
             "producto por vez, con foto.")
    distri_armado_orden = fields.Selection(
        ARMADO_ORDENES, string="Armado: orden", default="alfabetico", required=True,
        help="Por secciones sigue el orden de recorrido del depósito (Inventario > "
             "Categorías de productos).")

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + [
            "etiqueta_cartera_id", "distri_armado_vista", "distri_armado_orden"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        return super().SELF_WRITEABLE_FIELDS + ["distri_armado_vista", "distri_armado_orden"]

    def _distri_rol_texto(self):
        """«Armador», «Administración · Cajero»... for the «Ajustes» sheet."""
        self.ensure_one()
        user = self.sudo()
        if user._is_public():
            return ""
        if user.share:
            return "Cliente"
        roles = [nombre for xmlid, nombre in ROLES if user.has_group(xmlid)]
        # gerencia and administración imply the other roles: name only the top
        # one (and the cash box, which is separate)
        for top in ("Gerencia", "Administración"):
            if top in roles:
                roles = [top] + (["Cajero"] if "Cajero" in roles else [])
                break
        if not roles and user.has_group("base.group_system"):
            roles = ["Administrador del sistema"]
        return " · ".join(roles) or "Equipo"

    def _distri_ajustes(self):
        """What the «Ajustes» sheet of every custom page shows for this user
        (views/kit.xml distri_pedido.dn_ajustes)."""
        self.ensure_one()
        user = self.sudo()
        if user._is_public():
            return {"publico": True}
        socio = user.partner_id
        return {
            "publico": False,
            "nombre": user.name,
            "rol": user._distri_rol_texto(),
            "armador": user.has_group("distri_pedido.grupo_armador"),
            "vista": user.distri_armado_vista,
            "orden": user.distri_armado_orden,
            "perfil": bool(user.share and socio.distri_alta_estado),
            "cuenta": bool(user.share),
            "clave": True,
            # 19.0.1.119.0: /proyecto from every screen (single-role users
            # never land on Inicio, where its tile is)
            "capacitacion": not user.share,
        }

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
        # 19.0.1.115.0: a user with more than one role (a seller who also
        # delivers) lands on Inicio (/tablero), with a tile for each screen
        roles = [g for g in ("distri_pedido.grupo_vendedor", "distri_pedido.grupo_reparto",
                             "distri_pedido.grupo_armador") if user.has_group(g)]
        if len(roles) > 1:
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
