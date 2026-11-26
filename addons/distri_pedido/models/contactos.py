"""19.0.1.129.41: «Contactos» as a kit screen (controllers/contactos.py),
behind distri_pedido.ui_v2. The rules live here so the controller and the
tests share them:

- who opens the list and who edits what (the backend's ACLs and record
  rules decide: administración and gerencia write contacts in the backend,
  base.group_partner_manager; a seller reads his portfolio and only the
  three fields «Nueva venta» already lets him change, written with sudo as
  in 19.0.1.129.40);
- the search: one server query per page (never every contact), in the
  user's own environment, so record rules apply exactly as in the backend
  list;
- the balance of many customers at once: the same rule as
  res.partner._distri_account_balance (models/reparto_cliente.py) in three
  queries instead of three per customer;
- whether a «Cliente nuevo · a confirmar» may be merged from the kit: only
  when nothing accounting-related points at it (no journal item, no move,
  no payment, no cash collection, no imported history), so Odoo's partner
  merge never moves an entry.

Nothing here writes on account.move, account.move.line, account.payment or a
reconciliation.
"""
import re

from odoo import api, models
from odoo.exceptions import UserError
from odoo.tools import plaintext2html

from .cuenta import clave_documento

ADMIN_GROUP = "distri_pedido.grupo_administracion"
SYSTEM_GROUP = "base.group_system"
SELLER_GROUP = "distri_pedido.grupo_vendedor"
PAGE_SIZE = 30
MAX_QUERY = 80
DIGITS_MIN = 4
# what a seller may change (the 19.0.1.129.40 «Datos del cliente» fields)
SELLER_FIELDS = ("distri_whatsapp", "street", "city")
TIPOS = ("clientes", "proveedores", "otros", "todos")
FILTROS = ("mios", "deuda", "confirmar", "sin_wa", "sin_gps", "mayorista", "archivados")
# filters only administración / gerencia / Sistemas get
FILTROS_ADMIN = ("mayorista", "archivados")


def ve_todo(user):
    """Administración (gerencia implies it) and Sistemas: every contact,
    suppliers and archived ones included, as in the backend."""
    return bool(user) and not user.share and (
        user.has_group(ADMIN_GROUP) or user.has_group(SYSTEM_GROUP))


def puede_contactos(user):
    """The daily roles of the record screens (controllers/fichas.py may_use):
    vendedor, administración, gerencia and Sistemas. Packers and drivers do
    not read customers in the backend either (security/reglas.xml)."""
    from ..controllers.fichas import may_use
    return may_use(user)


def modo_edicion(env, partner):
    """What the user may change of this customer on the kit: "todo"
    (administración, gerencia, Sistemas, when the backend lets him write
    the contact), "vendedor" (a seller, on a customer he reads and reaches:
    the three fields of 19.0.1.129.40's «Datos del cliente», with its
    checks) or "" (read only)."""
    from .lista_oculta import solo_vendedor
    user = env.user
    if not puede_contactos(user):
        return ""
    if ve_todo(user):
        return "todo" if partner.has_access("write") else ""
    p = partner.sudo()
    if not user.has_group(SELLER_GROUP) or not p.active or p.distri_generic_partner:
        return ""
    if not user._distri_may_reach_customer(p) or not partner.has_access("read"):
        return ""
    if solo_vendedor(user) and p._distri_es_distribuidora():
        return ""
    return "vendedor"


def _clip(value, size):
    return " ".join((value or "").split())[:size]


# the value of «Sin vendedor» in the administración form (19.0.1.129.45)
SIN_VENDEDOR = "0"


def _choice(form, key, options, label):
    """The id picked in a <select> among `options` ((id, label) pairs);
    False when empty; UserError for anything else."""
    value = (form.get(key) or "").strip()
    if not value:
        return False
    try:
        value = int(value)
    except ValueError:
        value = 0
    if value not in {o[0] for o in options}:
        raise UserError("%s: elegí una opción de la lista." % label)
    return value


def _coordenada(text, low, high, label):
    text = (text or "").strip().replace(",", ".")
    if not text:
        return None
    try:
        value = float(text)
    except ValueError:
        raise UserError("%s: escribí un número (por ejemplo -38.951234)." % label) from None
    if not low <= value <= high or value == 0:
        raise UserError("%s fuera de rango." % label)
    return value


def valores_admin(env, partner, form, opciones):
    """The checked values of the administración form, as the backend would
    take them (the same normalisation helpers: ar_phone, the CUIT check
    digit, the delivery days and hours of the driver app). UserError with
    the first problem. `_entrega` is the delivery address (street, town)."""
    from .ar_phone import ar_mobile, ar_national, check_ar_phone, format_ar_national
    from .ocr_comprobante import cuit_is_valid
    from .reparto_cliente import normalize_days, normalize_hours
    Partner = env["res.partner"]
    current = partner.sudo()
    valores = {}
    nombre = _clip(form.get("nombre"), 120)
    if len(nombre) < 3:
        raise UserError("Escribí el nombre del cliente (del comercio o de la persona).")
    valores["name"] = nombre
    valores["ref"] = _clip(form.get("codigo"), 30) or False
    cuit = _clip(form.get("cuit"), 20)
    digits = solo_digitos(cuit)
    if cuit and cuit == (current.vat or ""):
        # untouched: kept as it is (an older format is not re-checked)
        pass
    elif cuit:
        if len(digits) == 11:
            if not cuit_is_valid(digits):
                raise UserError("El CUIT %s no es válido: revisá los 11 números." % cuit)
            kind = "l10n_ar.it_cuit"
        elif 7 <= len(digits) <= 8:
            kind = "l10n_ar.it_dni"
        else:
            raise UserError("CUIT: 11 números (o el DNI: 7 u 8 números).")
        valores["vat"] = digits
        if "l10n_latam_identification_type_id" in Partner._fields:
            ident = env.ref(kind, raise_if_not_found=False)
            if ident:
                valores["l10n_latam_identification_type_id"] = ident.id
    else:
        valores["vat"] = False
    email = _clip(form.get("email"), 120)
    if email and email != (current.email or "") \
            and not re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", email):
        raise UserError("El email «%s» no es válido." % email)
    valores["email"] = email or False
    area, numero = (form.get("wa_area") or "").strip(), (form.get("wa_numero") or "").strip()
    if area or numero:
        try:
            valores["distri_whatsapp"] = ar_mobile(area, numero)
        except ValueError as exc:
            raise UserError("WhatsApp del cliente: %s" % exc) from None
    elif not (current.distri_whatsapp and not ar_national(current.distri_whatsapp)):
        # emptied by hand; one the form could not show (an odd old format)
        # is kept as it is
        valores["distri_whatsapp"] = False
    telefono = _clip(form.get("telefono"), 40)
    if telefono and telefono == _clip(current.phone, 40):
        pass
    elif telefono:
        national, error = check_ar_phone(telefono)
        if error:
            raise UserError("Teléfono: %s" % error)
        valores["phone"] = format_ar_national(national)
    else:
        valores["phone"] = False
    valores["street"] = _clip(form.get("calle"), 120) or False
    valores["city"] = _clip(form.get("ciudad"), 60) or False
    lat = _coordenada(form.get("lat"), -90, 90, "Latitud")
    lng = _coordenada(form.get("lng"), -180, 180, "Longitud")
    if (lat is None) != (lng is None):
        raise UserError("Ubicación: hacen falta la latitud y la longitud (o ninguna).")
    valores["partner_latitude"] = lat or 0.0
    valores["partner_longitude"] = lng or 0.0
    valores["distri_delivery_days"] = normalize_days(",".join(form.getlist("dias"))) or False
    horarios = _clip(form.get("horarios"), 120)
    normal = normalize_hours(horarios)
    if horarios and not normal:
        raise UserError("Horarios: escribilos así: 08:00-12:30;17:00-20:30.")
    valores["distri_delivery_hours"] = normal or False
    nota = (form.get("nota_reparto") or "").strip()[:1000]
    valores["distri_driver_note"] = nota or False
    lista = _choice(form, "lista", opciones["listas"], "Lista de precios")
    if lista:
        valores["property_product_pricelist"] = lista
    # 19.0.1.129.45: «Sin vendedor» is an explicit choice ("0"); an empty or
    # missing value keeps the customer's salesperson as it is
    vendedor = (form.get("vendedor") or "").strip()
    if vendedor == SIN_VENDEDOR:
        valores["user_id"] = False
    elif vendedor:
        valores["user_id"] = _choice(form, "vendedor", opciones["vendedores"], "Vendedor")
    cartera = _choice(form, "cartera", opciones["carteras"], "Cartera")
    portfolio = {o[0] for o in opciones["carteras"]}
    tags = [t for t in partner.sudo().category_id.ids if t not in portfolio]
    valores["category_id"] = [(6, 0, sorted(tags + ([cartera] if cartera else [])))]
    valores["distri_vendedor_web_id"] = _choice(form, "vendedor_web", opciones["vendedores_web"],
                                                "Vendedor según el cliente")
    if opciones["iva"]:
        valores["l10n_ar_afip_responsibility_type_id"] = _choice(
            form, "iva", opciones["iva"], "Condición frente al IVA")
    if opciones["cuenta_corriente"]:
        valores["distri_cuenta_corriente"] = form.get("cuenta_corriente") == "1"
    notas = (form.get("notas") or "").strip()[:4000]
    valores["comment"] = plaintext2html(notas) if notas else False
    valores["_entrega"] = (_clip(form.get("entrega_calle"), 120) or False,
                           _clip(form.get("entrega_ciudad"), 60) or False)
    return valores


def palabras(texto):
    """The search words of what was typed (at most MAX_QUERY characters)."""
    return " ".join((texto or "").split())[:MAX_QUERY].split()


def solo_digitos(texto):
    return re.sub(r"\D", "", texto or "")


class ResPartnerContactos(models.Model):
    _inherit = "res.partner"

    # ------------------------------------------------------------ balances

    @api.model
    def _distri_account_balances(self, partner_ids):
        """{commercial partner id: balance} for many customers at once, with
        exactly the rule of _distri_account_balance: posted receivable items
        (not «Deudores varios») plus the imported history, a document in
        both counted once (clave_documento, the live one wins), minus the
        returns reported and not settled yet. Three queries for the whole
        page instead of three per customer."""
        ids = sorted({int(i) for i in partner_ids if i})
        if not ids:
            return {}
        from .caja_v2 import debt_account_ids
        env = self.env
        env["account.move.line"].flush_model()
        env["account.move"].flush_model(["name", "state"])
        env["distri.cuenta.movimiento"].flush_model()
        cr = env.cr
        cr.execute("""
            SELECT l.partner_id, m.name, l.debit, l.credit
              FROM account_move_line l
              JOIN account_move m ON m.id = l.move_id
              JOIN account_account a ON a.id = l.account_id
             WHERE l.partner_id = ANY(%s) AND l.parent_state = 'posted'
               AND a.account_type = 'asset_receivable' AND NOT (l.account_id = ANY(%s))""",
                   [ids, debt_account_ids(env) or [0]])
        balance = dict.fromkeys(ids, 0.0)
        debit, credit = dict.fromkeys(ids, 0.0), dict.fromkeys(ids, 0.0)
        seen = {i: set() for i in ids}
        for partner_id, name, line_debit, line_credit in cr.fetchall():
            debit[partner_id] += line_debit or 0.0
            credit[partner_id] += line_credit or 0.0
            seen[partner_id].add(clave_documento(name, line_debit, line_credit))
        for partner_id in ids:
            # summed apart, as sum(debit) - sum(credit) in the one-by-one rule
            balance[partner_id] = debit[partner_id] - credit[partner_id]
            seen[partner_id].discard(None)
        Mov = env["distri.cuenta.movimiento"]
        active = " AND active" if "active" in Mov._fields else ""
        cr.execute("SELECT partner_id, comprobante, debe, haber FROM distri_cuenta_movimiento"
                   " WHERE partner_id = ANY(%s)" + active, [ids])
        for partner_id, comprobante, debe, haber in cr.fetchall():
            if clave_documento(comprobante, debe, haber) not in seen[partner_id]:
                balance[partner_id] += (debe or 0.0) - (haber or 0.0)
        returns = env["distri.route.return"].sudo().search([
            ("order_id.partner_id.commercial_partner_id", "in", ids), ("quantity", ">", 0)])
        for ret in returns.filtered(lambda r: r._distri_is_provisional()):
            partner_id = ret.order_id.partner_id.commercial_partner_id.id
            if partner_id in balance:
                balance[partner_id] -= ret.amount
        return balance

    # ------------------------------------------------------------ search

    @api.model
    def _distri_contactos_domain(self, q="", tipo="clientes", filtros=(), localidad="",
                                 vendedor=0):
        """The domain of the list for the current user (the record rules are
        added by search() itself). `filtros` is a set of FILTROS keys; the
        ones only administración gets are ignored for anybody else."""
        from .lista_oculta import MAYORISTA_FUERA, solo_vendedor
        user = self.env.user
        todo = ve_todo(user)
        filtros = set(filtros or ())
        if not todo:
            filtros -= set(FILTROS_ADMIN)
            tipo = "clientes"
        domain = [("parent_id", "=", False), ("distri_generic_partner", "=", False)]
        if tipo == "clientes":
            domain.append(("customer_rank", ">", 0))
        elif tipo == "proveedores":
            domain.append(("supplier_rank", ">", 0))
        elif tipo == "otros":
            domain += [("customer_rank", "=", 0), ("supplier_rank", "=", 0)]
        if "archivados" in filtros:
            domain.append(("active", "=", False))
        # an external seller: only his customers (the kit's one check of
        # him, res.users._distri_customer_ids_limit), on top of the rule
        propios = user._distri_customer_ids_limit()
        if propios is not None:
            domain.append(("id", "in", sorted(propios)))
        if solo_vendedor(user):
            # wholesale customers are handled by administración (19.0.1.115.0);
            # their marks are not readable by a seller, so the ids come from sudo
            marks = [(f, "!=", v) for f, _op, v in MAYORISTA_FUERA]
            mayoristas = self.sudo().with_context(active_test=False).search(
                ["|"] * (len(marks) - 1) + marks).ids
            if mayoristas:
                domain.append(("id", "not in", mayoristas))
        if "mios" in filtros:
            domain += self._distri_cartera_domain(user)
        if todo and vendedor:
            seller = self.env["res.users"].sudo().browse(int(vendedor)).exists()
            if seller:
                domain += self._distri_cartera_domain(seller)
        if localidad:
            domain.append(("city", "=ilike", localidad.strip()))
        if "confirmar" in filtros:
            domain.append(("distri_cliente_provisorio", "=", True))
        if "sin_wa" in filtros:
            domain.append(("distri_whatsapp", "in", (False, "")))
        if "sin_gps" in filtros:
            domain += [("partner_latitude", "in", (False, 0.0)),
                       ("partner_longitude", "in", (False, 0.0))]
        if "mayorista" in filtros:
            domain += ["|", ("distri_lista_oculta", "=", True), ("distri_es_mayorista", "=", True)]
        words = palabras(q)
        if len(words) > 1 and re.fullmatch(r"[\d\s()+\-./]+", " ".join(words)) \
                and len(solo_digitos(q)) >= DIGITS_MIN:
            # a phone or CUIT typed with spaces («299 422 2444»): one number
            words = [solo_digitos(q)]
        for word in words:
            domain += self._distri_word_domain(word)
        return domain

    @api.model
    def _distri_cartera_domain(self, seller):
        """«Mis clientes»: his salesperson's customers and his portfolio tag
        (an external seller: the ones he was chosen for)."""
        seller = seller.sudo()
        parts = [[("user_id", "=", seller.id)]]
        if seller.etiqueta_cartera_id:
            parts.append([("category_id", "in", seller.etiqueta_cartera_id.ids)])
        if seller._distri_is_external_seller():
            parts.append([("distri_vendedor_elegido_ids", "in", [seller.id])])
        domain = parts[0]
        for part in parts[1:]:
            domain = ["|"] + domain + part
        return domain

    @api.model
    def _distri_word_domain(self, word):
        """One typed word: name, code, CUIT, street or town; a word with 4 or
        more digits also matches the phone, WhatsApp and CUIT written in any
        format (spaces, dashes, +54 9)."""
        domain = ["|", "|", "|", "|", ("name", "ilike", word), ("ref", "ilike", word),
                  ("vat", "ilike", word), ("street", "ilike", word), ("city", "ilike", word)]
        digits = solo_digitos(word)
        if len(digits) >= DIGITS_MIN and len(digits) * 2 >= len(word):
            ids = self._distri_ids_by_digits(digits)
            domain = ["|", ("id", "in", ids)] + domain
        return domain

    @api.model
    def _distri_ids_by_digits(self, digits):
        """Contacts whose phone, WhatsApp or CUIT holds these digits, ignoring
        the format. Only ids: search() then applies the record rules. A
        national number typed with its 0 or 15 is found too."""
        from .ar_phone import ar_national
        keys = {digits}
        national = ar_national(digits)
        if national:
            keys.add(national)
        self.env["res.partner"].flush_model(["phone", "distri_whatsapp", "vat"])
        clauses, params = [], []
        for key in keys:
            like = "%" + key + "%"
            clauses.append("regexp_replace(coalesce(phone, ''), '\\D', '', 'g') LIKE %s"
                           " OR regexp_replace(coalesce(distri_whatsapp, ''), '\\D', '', 'g') LIKE %s"
                           " OR regexp_replace(coalesce(vat, ''), '\\D', '', 'g') LIKE %s")
            params += [like, like, like]
        self.env.cr.execute("SELECT COALESCE(parent_id, id) FROM res_partner WHERE "
                            + " OR ".join("(%s)" % c for c in clauses) + " LIMIT 500", params)
        return sorted({row[0] for row in self.env.cr.fetchall()})

    @api.model
    def _distri_contactos_buscar(self, q="", tipo="clientes", filtros=(), localidad="",
                                 vendedor=0, page=0, limit=PAGE_SIZE):
        """One page of the list: (records in the user's environment, total,
        more pages?). With «Con deuda» the balance decides, computed in one
        batch for the matching customers only (never the page-less list)."""
        limit = max(1, min(int(limit or PAGE_SIZE), 100))
        offset = max(0, int(page or 0)) * limit
        domain = self._distri_contactos_domain(q, tipo, filtros, localidad, vendedor)
        Partner = self.with_context(active_test="archivados" not in set(filtros or ()))
        order = "name, id"
        if "deuda" in set(filtros or ()):
            ids = Partner.search(domain, order=order).ids
            balances = self.sudo()._distri_account_balances(ids)
            ids = [i for i in ids if balances.get(i, 0.0) > 0.005]
            return Partner.browse(ids[offset:offset + limit]), len(ids), \
                len(ids) > offset + limit
        total = Partner.search_count(domain)
        return Partner.search(domain, order=order, offset=offset, limit=limit), total, \
            total > offset + limit

    # ------------------------------------------------------------ merge

    def _distri_merge_refusal(self):
        """Why this «Cliente nuevo · a confirmar» may not be merged from the
        kit, or "". Odoo's partner merge (base.partner.merge.automatic.wizard,
        used by _distri_provisorio_unir_con) rewrites every reference to the
        contact, journal items included; the kit only does it when there is
        no accounting at all on it, so no entry is ever moved."""
        self.ensure_one()
        partner = self.sudo()
        env = self.env
        if not partner.distri_cliente_provisorio:
            return "Este cliente ya está confirmado."
        if partner.child_ids or partner.with_context(active_test=False).user_ids:
            return "Tiene contactos o usuarios: lo une Sistemas desde el sistema."
        checks = (
            ("account.move.line", [("partner_id", "=", partner.id)]),
            ("account.move", ["|", "|", ("partner_id", "=", partner.id),
                              ("commercial_partner_id", "=", partner.id),
                              ("partner_shipping_id", "=", partner.id)]),
            ("account.payment", [("partner_id", "=", partner.id)]),
            ("distri.cash.collection", [("partner_id", "=", partner.id)]),
            ("distri.cuenta.movimiento", [("partner_id", "=", partner.id)]),
            ("distri.mp.collection", [("partner_id", "=", partner.id)]),
        )
        for model, domain in checks:
            if model not in env:
                continue
            Model = env[model].sudo().with_context(active_test=False)
            if model == "account.move" and "partner_shipping_id" not in Model._fields:
                domain = ["|", ("partner_id", "=", partner.id),
                          ("commercial_partner_id", "=", partner.id)]
            if Model.search_count(domain, limit=1):
                return ("Tiene movimientos de cuenta (facturas, pagos o cobros): desde acá no se "
                        "une, para no tocar la contabilidad. Confirmalo, o pedile a Sistemas que "
                        "lo una desde el sistema.")
        return ""
