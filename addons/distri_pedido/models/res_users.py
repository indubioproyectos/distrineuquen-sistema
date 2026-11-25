from odoo import api, fields, models
from odoo.exceptions import AccessError
from odoo.fields import Domain

# Every user works in Argentina. Odoo takes the user's time zone for the
# dates it sets by itself (invoice and credit note dates, "today" in
# filters) and for the times it prints (reports, chatter, web pages). The
# web client shows times in the browser's zone and warns when it differs
# from the user's.
DEFAULT_TZ = "America/Argentina/Buenos_Aires"
# Settings > Technical > System Parameters: "1" locks sellers to /cuentas
PARAM_SELLER_ONLY_ACCOUNTS = "distri_pedido.vendedor_solo_cuentas"
# 19.0.1.129.20: internal sellers (company staff) keep their portfolio and
# /cuentas as before; an external (independent) seller only reaches his
# customers (_distri_chosen_customers: they chose him, he took their orders
# or he created them)
TIPOS_VENDEDOR = [("interno", "Interno"), ("externo", "Externo")]
TIPO_VENDEDOR_EDITORS = ("distri_pedido.grupo_administracion", "base.group_system")
TIPO_VENDEDOR_DENIED = "Solo administración, gerencia o sistemas cambian el tipo de vendedor."
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

    distri_tipo_vendedor = fields.Selection(
        TIPOS_VENDEDOR, string="Tipo de vendedor", default="interno", required=True,
        help="Interno: personal de la empresa, ve los clientes como hasta ahora. Externo: "
             "vendedor independiente, ve solo sus clientes: los que lo eligieron («Mi vendedor "
             "es» o el vendedor del pedido), aquellos a los que les cargó un pedido y los que "
             "dio de alta.")

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

    # 19.0.1.129.33 (#3, ui_v2 only): the user's own order of the Inicio tiles,
    # a comma list of tile keys (controllers/tablero.py apply_user_order),
    # set on /tablero/ordenar. Empty = the default order. A plain new column.
    distri_inicio_orden = fields.Char(string="Orden del Inicio", copy=False)

    @property
    def SELF_READABLE_FIELDS(self):
        return super().SELF_READABLE_FIELDS + [
            "etiqueta_cartera_id", "distri_tipo_vendedor", "distri_armado_vista",
            "distri_armado_orden"]

    @property
    def SELF_WRITEABLE_FIELDS(self):
        # distri_tipo_vendedor is never self-writeable (see write)
        return super().SELF_WRITEABLE_FIELDS + ["distri_armado_vista", "distri_armado_orden"]

    def write(self, vals):
        """19.0.1.129.20: only administración (gerencia implies it) and
        system administrators change the seller type; it changes the partner
        record rule (security/reglas.xml), whose domain Odoo caches per user,
        so the caches are cleared."""
        if "distri_tipo_vendedor" in vals:
            if not self.env.su and not any(
                    self.env.user.has_group(g) for g in TIPO_VENDEDOR_EDITORS):
                raise AccessError(TIPO_VENDEDOR_DENIED)
        res = super().write(vals)
        if "distri_tipo_vendedor" in vals:
            self.env.registry.clear_cache()
        return res

    def _login(self, credential, user_agent_env):
        """19.0.1.129.24: the login is typed by hand on a phone: «DENIS»,
        «Mariana» or «mariana » (trailing space) failed. Spaces around it are
        dropped and, unless a user has it exactly as typed, it is taken in
        lowercase (every login is lowercase). Only the login is touched: the
        password, the cooldown and every check stay in Odoo. Changed in
        place, so the session keeps the clean login too."""
        login = credential.get("login")
        if isinstance(login, str):
            clean = login.strip()
            if clean != clean.lower() and not self.sudo().search_count(
                    self._get_login_domain(clean), limit=1):
                clean = clean.lower()
            credential["login"] = clean
        return super()._login(credential, user_agent_env)

    def _distri_is_external_seller(self):
        """An external seller: the seller group, type «Externo», and neither
        administración (nor gerencia, which implies it) nor system
        administrator. Only such a user is limited to his customers
        (_distri_chosen_customers); everybody else is unchanged."""
        self.ensure_one()
        user = self.sudo()
        if user.share or user.distri_tipo_vendedor != "externo":
            return False
        if user.has_group("distri_pedido.grupo_administracion") \
                or user.has_group("base.group_system"):
            return False
        return user.has_group("distri_pedido.grupo_vendedor")

    def _distri_chosen_customers(self):
        """The customers of these external sellers: the commercial partners
        (sudo, archived ones too) the seller reaches in /cuentas and through
        the partner record rule (res.partner distri_vendedor_elegido_ids).
        The one definition, the union of:
        (a) the customers of the sale orders mis_ventas.de_vendedor counts
            for him: «Mi vendedor es» picked him, or no web seller and he is
            the salesperson;
        (b) the customers of the sale orders he created: an order he takes
            on /pedido from his session is created with sudo, which keeps
            his uid, so create_uid is him (controllers/main.py pedido_enviar;
            its salesperson is him too, _valores_del_personal), also when
            administración assigns the customer later;
        (c) the customers he created himself (create_uid, e.g. «Cliente
            nuevo» of /cobro, also with sudo), only top-level ones: a
            contact he adds to somebody else's company does not give him
            that company.
        Any order state (a draft or cancelled order still says who the
        customer works with). The generic partners are never his."""
        from .mis_ventas import de_vendedor
        Partner = self.env["res.partner"].sudo().with_context(active_test=False)
        if not self.ids:
            return Partner
        domain = Domain.OR(de_vendedor(uid) for uid in self.ids) \
            | Domain("create_uid", "in", self.ids)
        groups = self.env["sale.order"].sudo().with_context(active_test=False)._read_group(
            domain, ["partner_id"])
        partners = Partner.browse(partner.id for partner, in groups).commercial_partner_id
        partners |= Partner.search([("create_uid", "in", self.ids), ("parent_id", "=", False)])
        return partners.filtered(lambda p: not p.distri_generic_partner)

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
        from .mis_ventas import puede_ver_mis_ventas
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
            # 19.0.1.119.0: /proyecto from every screen
            "capacitacion": not user.share,
            # 19.0.1.129.18: «Mis ventas» from every screen
            "mis_ventas": puede_ver_mis_ventas(user),
            # 19.0.1.120.0: the way back to Inicio from every staff screen
            "inicio": "/tablero" if user._distri_has_inicio() else "",
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
        """The screen a user lands on when it is not Inicio (/tablero), or ""
        (Inicio, or the backend for whoever keeps it).

        19.0.1.120.0 (owner, day 1): every packer and driver lands on Inicio,
        whatever his other roles, with a tile for each screen he may open
        and a way back to it from every staff screen (the «Inicio» button of
        dn_ajustes, kit.staff_nav). Before, a user with one role went
        straight to /reparto or /armado and could not see his other screens.

        19.0.1.129.20 (owner): a seller locked to the accounts app
        (_distri_seller_only_accounts) lands on Inicio too, internal or
        external, with the tiles his groups allow (Cuentas, Mis ventas,
        Tomar un pedido, Cobrar, Comprobantes...) and no backend tile; until
        then he landed on /cuentas, without «Inicio», and could not reach
        /mis-ventas. The backend stays locked for him (controllers/tienda.py
        web_client sends him to Inicio). So nobody has a screen of his own
        today: always "". Kept as the one place to add one again; every
        staff screen asks it for its «Inicio» link."""
        self.ensure_one()
        return ""

    def _distri_has_inicio(self):
        """19.0.1.120.0: whether the user's home is Inicio (/tablero): every
        internal user without a screen of his own (_distri_landing)."""
        self.ensure_one()
        return not self.share and not self._is_public() and not self._distri_landing()

    def _distri_seller_only_accounts(self):
        """12/11/2026: while PARAM_SELLER_ONLY_ACCOUNTS is "1" a seller does
        not use the backend: every backend page sends him to Inicio
        (controllers/tienda.py web_client; until 19.0.1.129.20 to /cuentas)
        and Inicio shows him no backend tile nor «Todas las aplicaciones». Administración, gerencia
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

    def _distri_customer_ids_limit(self):
        """None when this user reaches every customer his screens allow
        (anybody but an external seller); for an external seller, the ids
        of his customers (_distri_chosen_customers). The one check of the
        staff screens that read customers with sudo: /cuentas, /pedido,
        /cobro and /comprobantes."""
        self.ensure_one()
        if not self._distri_is_external_seller():
            return None
        return set(self._distri_chosen_customers().ids)

    def _distri_may_reach_customer(self, partner):
        """Whether this user may pick or read `partner` (a customer or one
        of its contacts) on a staff screen (see _distri_customer_ids_limit)."""
        self.ensure_one()
        allowed = self._distri_customer_ids_limit()
        return allowed is None or partner.commercial_partner_id.id in allowed


class ResPartnerChosenSeller(models.Model):
    _inherit = "res.partner"

    # 19.0.1.129.20: what the partner record rule of an external seller
    # searches on (security/reglas.xml regla_partner_vendedor). Not stored:
    # the search method reads the sale orders at search time, so a new order
    # counts at once (Odoo caches the rule's domain, not this search).
    distri_vendedor_elegido_ids = fields.Many2many(
        "res.users", string="Vendedores externos del cliente",
        compute="_compute_distri_vendedor_elegido_ids",
        search="_search_distri_vendedor_elegido_ids",
        help="Los vendedores externos que ven a este cliente: lo eligió («Mi vendedor es» o "
             "el vendedor del pedido), le cargaron un pedido o lo dieron de alta.")

    def _compute_distri_vendedor_elegido_ids(self):
        Users = self.env["res.users"].sudo()
        externals = Users.search([("distri_tipo_vendedor", "=", "externo"), ("share", "=", False)])
        externals = externals.filtered(lambda u: u._distri_is_external_seller())
        customers = {user: user._distri_chosen_customers() for user in externals}
        for partner in self:
            commercial = partner.commercial_partner_id
            partner.distri_vendedor_elegido_ids = Users.browse(
                user.id for user, chosen in customers.items() if commercial in chosen)

    def _search_distri_vendedor_elegido_ids(self, operator, value):
        """The partners (the commercial partner and its contacts) of the
        external sellers in `value` (Users._distri_chosen_customers)."""
        if operator != "in":
            return NotImplemented
        user_ids = [v for v in value if isinstance(v, int) and v]
        if not user_ids:
            return Domain.FALSE
        chosen = self.env["res.users"].sudo().browse(user_ids)._distri_chosen_customers()
        return Domain("commercial_partner_id", "in", chosen.ids)
