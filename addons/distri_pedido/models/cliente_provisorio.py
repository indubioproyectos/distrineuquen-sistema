"""«Cliente nuevo» from «Cobro en efectivo» (19.0.1.103.0).

The person collecting cash (/cobro, controllers/cobro.py) does not find the
customer: new, not in the base yet, or written in a way the search does not
catch. Instead of keeping the cash aside, he creates the customer right
there with a minimal form (name; the phone, checked with
ar_phone.check_ar_phone; address, locality and a note optional) and the
cash is registered on it «a cuenta», exactly as for any customer
(distri.cash.collection.cash_register).

- Before creating, the likely existing customers are shown
  (`cash_new_matches`: the same normalized phone, a name sharing a word):
  he picks one and collects on it, or says it is another one.
- A new customer owes nothing, so the over-debt question of 19.0.1.99.0
  always comes up, with its own words (NEW_OVER_DEBT).
- The customer is PROVISIONAL: `distri_cliente_provisorio`, who and when,
  and the collection it came with. It is found by the search as usual,
  marked «Cliente nuevo · a confirmar».
- Administración (and gerencia) settles it: «Confirmar» (it stays, the flag
  goes), «Unir con un cliente existente» (`_distri_provisorio_unir_con`:
  every reference to it, the payments and their journal items included,
  moves to the chosen customer with Odoo's own partner-merge SQL, the open
  invoices of that customer get the cash oldest first, and the provisional
  one is archived with a note on both) or «Editar». The list is on /caja
  (receiving and closing, never blocking the closing), on the backend cash
  closing and route forms, and in Cobros › Clientes nuevos a confirmar.
"""
from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import AccessError, UserError

from .ar_phone import check_ar_phone, format_ar_national
from .asignar_cliente import _palabras
from .cobro_efectivo import DuplicateCashError, OverDebtWarning
from .reparto import _money

PROVISIONAL_LABEL = "Cliente nuevo · a confirmar"
NEW_OVER_DEBT = ("Cliente nuevo: el importe queda a favor del cliente (%s), porque todavía no "
                 "tiene deuda. ¿Registrar igual?")
CONFIRM_GROUPS = ("distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia",
                  "base.group_system")
MATCHES_LIMIT = 6
NAME_MAX = 120
DUPLICATE_MINUTES = 5


def can_confirm(env):
    return env.su or any(env.user.has_group(g) for g in CONFIRM_GROUPS)


class NewCustomerMatches(UserError):
    """There are customers that look like the new one: the page shows them
    first (`matches`) and asks."""

    def __init__(self, message, matches):
        super().__init__(message)
        self.matches = matches


class ResPartnerProvisorio(models.Model):
    _inherit = "res.partner"

    distri_cliente_provisorio = fields.Boolean(
        string="Cliente nuevo a confirmar", index=True, copy=False, readonly=True,
        help="Lo creó quien cobró en «Cobro en efectivo» porque no lo encontró. Administración "
             "lo confirma o lo une con el cliente que ya existía.")
    distri_provisorio_user_id = fields.Many2one("res.users", string="Lo creó", copy=False,
                                                readonly=True)
    distri_provisorio_fecha = fields.Datetime(string="Creado el", copy=False, readonly=True)
    distri_provisorio_cobro_id = fields.Many2one(
        "distri.cash.collection", string="Cobro con el que se creó", copy=False, readonly=True,
        ondelete="set null")
    distri_provisorio_nota = fields.Char(string="Nota de quien lo creó", copy=False, readonly=True)
    distri_provisorio_unido_id = fields.Many2one(
        "res.partner", string="Unido con", copy=False, readonly=True, ondelete="set null")
    distri_provisorio_cobrado = fields.Float(
        string="Cobrado", digits=(16, 2), compute="_compute_distri_provisorio_cobrado")
    distri_provisorio_etiqueta = fields.Char(compute="_compute_distri_provisorio_cobrado")

    def _compute_distri_provisorio_cobrado(self):
        Cash = self.env["distri.cash.collection"].sudo()
        totals = {}
        if self.ids:
            for partner, amount in Cash._read_group([("partner_id", "in", self.ids)],
                                                    ["partner_id"], ["amount:sum"]):
                totals[partner.id] = amount
        for partner in self:
            partner.distri_provisorio_cobrado = totals.get(partner.id, 0.0)
            partner.distri_provisorio_etiqueta = (PROVISIONAL_LABEL
                                                  if partner.distri_cliente_provisorio else False)

    # ------------------------------------------------------------ lists

    @api.model
    def _distri_provisorios_pendientes(self, route=None, limit=50):
        """The provisional customers still to confirm, oldest first; only the
        ones of `route`'s collections when given."""
        domain = [("distri_cliente_provisorio", "=", True)]
        if route:
            domain.append(("distri_provisorio_cobro_id.route_id", "in", route.ids))
        return self.sudo().search(domain, order="distri_provisorio_fecha, id", limit=limit)

    def _distri_provisorio_row(self):
        """What /caja shows of one provisional customer."""
        self.ensure_one()
        partner = self.sudo()
        when = partner.distri_provisorio_fecha
        return {
            "id": partner.id,
            "name": partner.name or "",
            "detail": " · ".join(x for x in (partner.phone, partner.street, partner.city) if x),
            "who": partner.distri_provisorio_user_id.name or "",
            "when": fields.Datetime.context_timestamp(partner, when).strftime("%d/%m %H:%M")
            if when else "",
            "amount": partner.distri_provisorio_cobrado,
            "url": "/odoo/action-distri_pedido.action_clientes_provisorios/%d" % partner.id,
        }

    # ------------------------------------------------------------ actions

    def _distri_check_confirm(self):
        if not can_confirm(self.env):
            raise AccessError("Solo administración y gerencia confirman los clientes nuevos.")

    def action_distri_provisorio_confirmar(self):
        """«Confirmar»: the customer stays as it is (completed by hand when
        needed) and leaves the list."""
        self._distri_check_confirm()
        for partner in self.sudo().filtered("distri_cliente_provisorio"):
            partner.distri_cliente_provisorio = False
            partner.message_post(body="Cliente nuevo confirmado por %s." % self.env.user.name)
        return True

    def action_distri_provisorio_unir(self):
        """«Unir con un cliente existente»: the wizard."""
        self.ensure_one()
        self._distri_check_confirm()
        if not self.distri_cliente_provisorio:
            raise UserError("Este cliente ya está confirmado.")
        wizard = self.env["distri.provisorio.unir"].create({"partner_id": self.id})
        return {
            "type": "ir.actions.act_window",
            "name": "Unir con un cliente existente",
            "res_model": wizard._name,
            "res_id": wizard.id,
            "view_mode": "form",
            "target": "new",
        }

    def action_distri_provisorio_editar(self):
        """«Editar»: the customer's form."""
        self.ensure_one()
        self._distri_check_confirm()
        return {
            "type": "ir.actions.act_window",
            "res_model": "res.partner",
            "res_id": self.id,
            "view_mode": "form",
            "target": "current",
        }

    def _distri_provisorio_unir_con(self, target):
        """Moves everything of this provisional customer to `target` and
        archives it. The journal items keep their amounts: the sum of both
        balances does not change."""
        self.ensure_one()
        self._distri_check_confirm()
        src = self.sudo()
        target = target.sudo().commercial_partner_id
        if not src.distri_cliente_provisorio:
            raise UserError("Este cliente ya está confirmado.")
        if not target or target == src or target.distri_generic_partner \
                or target.distri_cliente_provisorio or not target.active:
            raise UserError("Elegí el cliente que ya existía (no otro cliente nuevo ni el genérico).")
        if src.child_ids or src.with_context(active_test=False).user_ids:
            raise UserError("Este cliente tiene contactos o usuarios: unilo desde Contactos.")
        collections = self.env["distri.cash.collection"].sudo().search(
            [("partner_id", "=", src.id)])
        amount = sum(collections.mapped("amount"))
        self.env.flush_all()
        Merge = self.env["base.partner.merge.automatic.wizard"].sudo()
        Merge._update_foreign_keys(src, target)
        # its own commercial partner was moved too: it stays itself
        self.env.cr.execute("UPDATE res_partner SET commercial_partner_id = id WHERE id = %s",
                            [src.id])
        self.env.invalidate_all()
        for collection in collections:
            payment = collection.payment_id
            if payment and not collection.invoice_id:
                # «a cuenta»: the chosen customer's oldest invoices first
                collection.route_id._apply_oldest_first(payment, target)
        user = self.env.user
        src.write({"active": False, "distri_cliente_provisorio": False,
                   "distri_provisorio_unido_id": target.id})
        src.message_post(body=Markup("Unido con <b>%s</b> por %s: los cobros y los movimientos "
                                     "pasaron a ese cliente.") % (target.display_name, user.name))
        target.message_post(body=Markup(
            "Se unió el cliente nuevo <b>%s</b> (lo creó %s en «Cobro en efectivo»), por %s: "
            "pasaron %s cobros por %s.") % (
            src.name, src.distri_provisorio_user_id.name or "-", user.name, len(collections),
            _money(amount)))
        return target


class DistriProvisorioUnir(models.TransientModel):
    _name = "distri.provisorio.unir"
    _description = "Unir un cliente nuevo con un cliente existente"

    partner_id = fields.Many2one("res.partner", string="Cliente nuevo", required=True,
                                 readonly=True, ondelete="cascade")
    target_id = fields.Many2one(
        "res.partner", string="Es este cliente",
        domain="[('distri_cliente_provisorio', '=', False), ('distri_generic_partner', '=', False),"
               " ('parent_id', '=', False), ('id', '!=', partner_id)]")
    suggestion_ids = fields.Many2many("res.partner", string="Parecidos",
                                      compute="_compute_suggestion_ids")

    @api.depends("partner_id")
    def _compute_suggestion_ids(self):
        Cash = self.env["distri.cash.collection"]
        for wizard in self:
            partner = wizard.partner_id.sudo()
            ids = [m["id"] for m in Cash._cash_matches(partner.name, partner.phone,
                                                       exclude=partner)] if partner else []
            wizard.suggestion_ids = [(6, 0, ids)]

    def action_unir(self):
        self.ensure_one()
        if not self.target_id:
            raise UserError("Elegí con qué cliente se une.")
        target = self.partner_id._distri_provisorio_unir_con(self.target_id)
        return {"type": "ir.actions.act_window", "res_model": "res.partner", "res_id": target.id,
                "view_mode": "form", "target": "current"}


class DistriCashCollectionNew(models.Model):
    _inherit = "distri.cash.collection"

    @api.model
    def _cash_matches(self, name, phone, exclude=None):
        """Customers that may be the «new» one: the same normalized phone
        first, then a name sharing a word (4 letters or more)."""
        Partner = self.env["res.partner"].sudo()
        domain = self._cash_partner_domain()
        if exclude:
            domain = domain + [("id", "not in", exclude.ids)]
        found, why = Partner.browse(), {}
        national = check_ar_phone(phone)[0] if phone else ""
        if national:
            by_phone = Partner._distri_clientes_por_telefono(national, limit=MATCHES_LIMIT)
            by_phone = Partner.search(domain + [("id", "in", by_phone.commercial_partner_id.ids)])
            for partner in by_phone:
                why[partner.id] = "Mismo teléfono"
            found |= by_phone
        words = sorted({w for w in _palabras(name or "") if len(w) >= 4}, key=len, reverse=True)
        for word in words[:3]:
            if len(found) >= MATCHES_LIMIT:
                break
            for partner in Partner.search(domain + [("name", "ilike", word)],
                                          order="customer_rank desc, name", limit=MATCHES_LIMIT):
                why.setdefault(partner.id, "Nombre parecido")
                found |= partner
        return [{"id": p.id, "name": p.name or "", "why": why[p.id],
                 "detail": " · ".join(x for x in (p.ref, p.city, p.distri_whatsapp or p.phone) if x),
                 "provisional": p.distri_cliente_provisorio}
                for p in found[:MATCHES_LIMIT]]

    @api.model
    def cash_new_customer_values(self, name, phone="", street="", city=""):
        """The checked values of the minimal form; UserError otherwise."""
        name = " ".join((name or "").split())[:NAME_MAX]
        if len(name) < 3:
            raise UserError("Escribí el nombre del cliente (del comercio o de la persona).")
        phone = (phone or "").strip()
        national = ""
        if phone:
            national, error = check_ar_phone(phone)
            if error:
                raise UserError(error)
        return {
            "name": name,
            "phone": format_ar_national(national) if national else False,
            "street": " ".join((street or "").split())[:NAME_MAX] or False,
            "city": " ".join((city or "").split())[:60] or False,
        }

    @api.model
    def cash_register_new(self, name, amount, phone="", street="", city="", note="",
                          new_ok=False, over_ok=False, confirmed=False):
        """«Cliente nuevo»: creates the provisional customer and registers
        the cash «a cuenta» on it. Raises NewCustomerMatches while
        `new_ok` is not set and there are likely customers,
        OverDebtWarning while `over_ok` is not set (he owes nothing) and
        DuplicateCashError for the same new customer and amount a moment
        ago."""
        user = self._cash_check_user()
        values = self.cash_new_customer_values(name, phone, street, city)
        typed_amount = amount
        amount = self._cash_amount(amount)
        note = " ".join((note or "").split())[:200]
        if not new_ok:
            matches = self._cash_matches(values["name"], phone)
            if matches:
                raise NewCustomerMatches(
                    "Puede que ya esté cargado. Si es uno de estos, cobrale a ese.", matches)
        if not over_ok:
            raise OverDebtWarning(NEW_OVER_DEBT % _money(amount))
        previous = self.sudo().search([
            ("user_id", "=", user.id), ("amount", "=", amount),
            ("partner_id.distri_cliente_provisorio", "=", True),
            ("partner_id.name", "=ilike", values["name"]),
            ("create_date", ">=", fields.Datetime.subtract(fields.Datetime.now(),
                                                           minutes=DUPLICATE_MINUTES))], limit=1)
        if previous and not confirmed:
            raise DuplicateCashError(
                "Ya creaste a %s con un cobro de %s hace un momento. Si es otro cobro, "
                "confirmalo." % (values["name"], _money(amount)))
        partner = self.env["res.partner"].sudo().create(dict(
            values, company_type="company", customer_rank=1,
            distri_cliente_provisorio=True, distri_provisorio_user_id=user.id,
            distri_provisorio_fecha=fields.Datetime.now(),
            distri_provisorio_nota=note or False))
        collection = self.cash_register(partner.id, typed_amount, note=note, confirmed=True,
                                        route_ok=True, over_ok=True)
        partner.distri_provisorio_cobro_id = collection
        partner.message_post(body=Markup(
            "Cliente nuevo creado por %s en «Cobro en efectivo» con un cobro de <b>%s</b>: falta "
            "que administración lo confirme o lo una con el cliente que ya existía.") % (
            user.name, _money(amount)))
        return collection

    @api.model
    def cash_search(self, query):
        rows = super().cash_search(query)
        if rows:
            flagged = set(self.env["res.partner"].sudo().search([
                ("id", "in", [r["id"] for r in rows]),
                ("distri_cliente_provisorio", "=", True)]).ids)
            for row in rows:
                row["provisional"] = row["id"] in flagged
        return rows


class DistriRouteProvisorios(models.Model):
    """The backend cash closing and route forms list the new customers of
    their collections."""
    _inherit = "distri.route"

    distri_provisorio_ids = fields.Many2many(
        "res.partner", string="Clientes nuevos a confirmar",
        compute="_compute_distri_provisorio_ids")

    def _compute_distri_provisorio_ids(self):
        Partner = self.env["res.partner"]
        for route in self:
            route.distri_provisorio_ids = [(6, 0, Partner._distri_provisorios_pendientes(
                route=route).ids)] if route.id else [(5,)]
