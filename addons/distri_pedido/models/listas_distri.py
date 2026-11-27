"""19.0.1.129.52 (owner, 26/11/2026): price lists and web access of the
customers with a hidden list, managed from «Listas y accesos» (/listas,
controllers/listas.py) by the group «Listas y accesos de distribuidores»
(security/listas.xml: Gonzalo and Ale only).

Price lists, chained so a «May» or General price is loaded once:

- General (distri_clave «general»): public; cigarettes «a presupuestar».
- May farmacia y varios («may», it was «Distribuidoras», same record): the
  hidden «May» prices, with a global rule based on «Cigarrillos a
  presupuestar» («presupuestar»), which has a 0 rule per product with
  distri_precio_por_lista («Precio por lista obligatorio», the cigarette
  packs) and a global rule based on General. So anything without a «May»
  price takes the General price, except those products: 0 = «a presupuestar».
  Papers, filters and rolling tobacco (same AgilPedido category as the
  cigarettes, owner 26/11/2026) take General unless a list prices them.
- Lista 2 + May farmacia / Lista 3 / Lista 4 («lista2/3/4»): one fixed rule
  per product the gerente prices, and a global rule based on «May farmacia y
  varios». The «Revendedor NN» lists are already based on the May list, so
  the same fallback holds for them.

Customer categories (one per customer, derived from its price list and
res.partner.distri_lista_oculta, never stored apart): 1 General (not hidden),
2 General + May farmacia, 3 Lista 2, 4 Lista 3, 5 Lista 4, 6 another hidden
list. 2–6 are hidden: the prices show only to the customer logged in.

Every public method checks the group (check_listas), so nobody else can call
it through RPC; private helpers (_*) cannot be called through RPC at all.
Nothing here writes an invoice, a payment or an entry: only price lists, the
partner's list and flag, open (not packed, not invoiced) orders the user opts
in to reprice, and portal users.
"""
import csv
import difflib
import io
import re
import unicodedata

from markupsafe import Markup

from odoo import SUPERUSER_ID, api, fields, models
from odoo.exceptions import AccessError, UserError

from ..controllers.main import (
    CATEGORIAS_CIGARRILLOS_LISTAS, LISTA_GENERAL, LISTA_OCULTA, LISTA_OCULTA_ANTERIOR,
)

GRUPO_LISTAS = "distri_pedido.grupo_listas_accesos"
# assigned by login in migrations/19.0.1.129.52 (never by id)
LOGINS_GRUPO_LISTAS = ("gonzalo", "admin")
MENSAJE_SIN_PERMISO = ("Las listas y los accesos de distribuidores los manejan solo Gonzalo y "
                       "Ale (Sistemas).")

LISTA_2 = "Lista 2 + May farmacia"
CLAVES_LISTA = [
    ("general", "General"),
    ("may", "May farmacia y varios"),
    ("lista2", "Lista 2 + May farmacia"),
    ("lista3", "Lista 3 + May farmacia"),
    ("lista4", "Lista 4 + May farmacia"),
    # between May and General: the 0 («a presupuestar») of the products with
    # distri_precio_por_lista, one rule per product
    ("presupuestar", "Cigarrillos a presupuestar"),
]
# names used to find (and create) each list on a database without the key
NOMBRES_POR_CLAVE = {
    "general": (LISTA_GENERAL,),
    "may": (LISTA_OCULTA, LISTA_OCULTA_ANTERIOR),
    "lista2": (LISTA_2,),
    "lista3": ("Lista 3",),
    "lista4": ("Lista 4",),
    "presupuestar": ("Cigarrillos a presupuestar",),
}
LISTAS_CIGARRILLOS = ("lista2", "lista3", "lista4")
# 19.0.1.129.55 (owner): the key of a plain «May …» product (wholesale
# pharmacy and misc price, no distri_lista_personal) in the «one product per
# cigarette and list» rule of the links. Informational only: no price list
# reads it.
CLAVE_MAY_SUELTO = "may"
# the «May X - lista N» products (models/lista_personal.py) of each list
PERSONAL_POR_CLAVE = {"lista3": "lista_3", "lista4": "lista_4"}

CATEGORIAS = [
    ("1", "General"),
    ("2", "General + May farmacia"),
    ("3", "Lista 2 + May farmacia"),
    ("4", "Lista 3 + May farmacia"),
    ("5", "Lista 4 + May farmacia"),
    ("6", "Otra lista"),
]
NOMBRE_CATEGORIA = dict(CATEGORIAS)
CLAVE_POR_CATEGORIA = {"1": "general", "2": "may", "3": "lista2", "4": "lista3", "5": "lista4"}
CATEGORIA_POR_CLAVE = {v: k for k, v in CLAVE_POR_CATEGORIA.items()}
# list and flag that match no category (e.g. General with the hidden flag,
# the way the hidden «May» section was given before 129.52)
A_REVISAR = "0"
NOMBRE_CATEGORIA[A_REVISAR] = "A revisar"

CSV_MAX_BYTES = 2 * 1024 * 1024
CSV_MAX_FILAS = 5000


def puede_listas(user):
    """Gonzalo and Ale: internal users of the group «Listas y accesos de
    distribuidores». Gerencia or administración alone is not enough."""
    return bool(user) and not user._is_public() and not user.share and user.has_group(GRUPO_LISTAS)


def check_listas(env):
    """Raises AccessError unless the user is in the group. Code running as
    the superuser (migrations, tests' setup) passes; nothing reachable
    through RPC or a route runs as the superuser."""
    if env.su and env.uid == SUPERUSER_ID:
        return
    if not puede_listas(env.user):
        raise AccessError(MENSAJE_SIN_PERMISO)


def plata(monto):
    return "$ " + "{:,.0f}".format(monto or 0).replace(",", ".")


def cuit_digitos(vat):
    """The CUIT of a partner as 11 digits, or "" when the VAT is not one."""
    digitos = re.sub(r"\D", "", vat or "")
    return digitos if len(digitos) == 11 else ""


# Words that mean the same cigarette within a brand (owner, 26/11/2026:
# «Milenio Mint» is the GREEN of «May Milenio RED / GREEN 20»)
ALIAS_POR_MARCA = {"milenio": {"mint": "green"}}


def _normalizar(nombre):
    texto = unicodedata.normalize("NFKD", (nombre or "").lower())
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    texto = re.sub(r"-\s*lista\s*\d\s*$", " ", texto)
    texto = re.sub(r"^\s*may\s+", " ", texto)
    palabras = [p for p in re.split(r"[^a-z0-9]+", texto)
                if p and p not in ("x", "cigarrillo", "cigarrillos", "de", "u", "un")]
    for marca, alias in ALIAS_POR_MARCA.items():
        if marca in palabras:
            palabras = [alias.get(p, p) for p in palabras]
    return " ".join(palabras)


# 19.0.1.129.52 (owner): papers, filters and rolling tobacco share the
# AgilPedido category with the cigarettes but are not cigarette packs
NO_ES_CIGARRILLO = re.compile(
    r"\b(ocb|filtros?|papel(es|illos?)?|celulosas?|tips|sedas?|tabacos?|boquillas?|"
    r"encendedor(es)?|picadoras?|armadoras?|tubos?)\b", re.IGNORECASE)
# the categories that were «a presupuestar» before the AgilPedido sync moved
# the cigarettes (CIGARRILLOS, Tabaco VAN KIFF)
CATEGORIAS_ANTERIORES = ("CIGARRILLOS", "Tabaco VAN KIFF")


def precio_por_lista_sugerido(nombre, categoria, vinculado=False):
    """Whether a product needs a list price (True: «a presupuestar» without
    one) or takes the General price (False). Papers, filters and rolling
    tobacco by name («OCB», «Papel», «Filtro», «Celulosa», «Tabaco»...), no,
    whatever the category; the old cigarette categories (CIGARRILLOS, Tabaco
    VAN KIFF), a product linked from a «May X - lista N» one or anything else
    in the AgilPedido cigarette category, yes."""
    if NO_ES_CIGARRILLO.search(nombre or ""):
        return False
    return bool(categoria in CATEGORIAS_ANTERIORES or vinculado
                or categoria in CATEGORIAS_CIGARRILLOS_LISTAS)


class ProductPricelistClave(models.Model):
    _inherit = "product.pricelist"

    distri_clave = fields.Selection(
        CLAVES_LISTA, string="Lista del sistema", copy=False, readonly=True, index=True,
        help="Referencia fija de las listas que usa el sistema (General, May farmacia y "
             "varios, Lista 2/3/4): se pueden renombrar sin romper nada.")

    @api.constrains("distri_clave")
    def _check_distri_clave_unica(self):
        for lista in self.filtered("distri_clave"):
            otras = self.with_context(active_test=False).search_count(
                [("distri_clave", "=", lista.distri_clave), ("id", "!=", lista.id)])
            if otras:
                raise UserError("Ya hay otra lista «%s»." % dict(CLAVES_LISTA)[lista.distri_clave])


class ProductTemplateCigarrilloMay(models.Model):
    _inherit = "product.template"

    distri_precio_por_lista = fields.Boolean(
        string="Precio por lista obligatorio", copy=False, index=True,
        help="Con la marca, en las listas ocultas el producto sin precio propio en la lista "
             "del cliente queda «a presupuestar» (los cigarrillos). Sin la marca toma el precio "
             "de la lista General (papel, filtros, tabaco para armar...).")
    # set by hand (grid) or by the migration: the migration never changes it again
    distri_precio_por_lista_fijado = fields.Boolean(copy=False, readonly=True)

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if "distri_precio_por_lista" not in vals and vals.get("categ_id"):
                categoria = self.env["product.category"].sudo().browse(vals["categ_id"])
                vals["distri_precio_por_lista"] = precio_por_lista_sugerido(
                    vals.get("name"), categoria.name)
        plantillas = super().create(vals_list)
        self.env["distri.listas"]._sincronizar_presupuestar(
            plantillas.filtered("distri_precio_por_lista"))
        return plantillas

    def write(self, vals):
        resultado = super().write(vals)
        if "distri_precio_por_lista" in vals:
            self.env["distri.listas"]._sincronizar_presupuestar(self)
        return resultado

    distri_cigarrillo_ids = fields.Many2many(
        "product.template", "distri_cigarrillo_may_rel", "producto_may_id", "cigarrillo_id",
        string="Cigarrillos de la lista", copy=False,
        help="En un producto «May X - lista 3/4»: los cigarrillos cuyo precio de lista 3/4 es "
             "este (uno, o varios si el producto vale para todos, como «Milenio RED / GREEN»). "
             "Lo usa «Listas y accesos» para «usar precio del producto May». En un producto "
             "«May …» común (sin lista 3/4) solo se muestra en la columna May de la grilla: "
             "no cambia ningún precio.")


class DistriListas(models.AbstractModel):
    _name = "distri.listas"
    _description = "Listas y accesos de distribuidores"

    # ------------------------------------------------------------ lists

    @api.model
    def _lista(self, clave, activas=True):
        Pl = self.env["product.pricelist"].sudo().with_context(active_test=activas)
        return Pl.search([("distri_clave", "=", clave)], limit=1)

    @api.model
    def _buscar_para_clave(self, clave):
        Pl = self.env["product.pricelist"].sudo().with_context(active_test=False)
        return (Pl.search([("distri_clave", "=", clave)], limit=1)
                or Pl.search([("distri_clave", "=", False),
                              ("name", "in", list(NOMBRES_POR_CLAVE[clave]))],
                             order="active desc, id", limit=1))

    @api.model
    def _asegurar_regla_base(self, lista, base):
        """One global rule of `lista` based on `base` (same price). An
        existing global rule based on another list is pointed to `base`."""
        Item = self.env["product.pricelist.item"].sudo()
        globales = lista.item_ids.filtered(lambda i: i.applied_on == "3_global")
        if globales.filtered(lambda i: i.compute_price == "formula" and i.base == "pricelist"
                             and i.base_pricelist_id == base):
            return False
        valores = {"applied_on": "3_global", "compute_price": "formula", "base": "pricelist",
                   "base_pricelist_id": base.id, "price_discount": 0.0, "price_surcharge": 0.0,
                   "price_round": 0.0, "min_quantity": 0.0}
        if globales:
            globales[:1].write(valores)
        else:
            Item.create(dict(valores, pricelist_id=lista.id))
        return True

    @api.model
    def _asegurar_estructura(self):
        """Idempotent: the keyed lists and their rules as described in the
        module docstring. Returns {clave: pricelist} and a list of what it
        changed (for the migration log)."""
        env = self.env(su=True)
        Pl = env["product.pricelist"].with_context(active_test=False)
        Item = env["product.pricelist.item"]
        moneda = env.company.currency_id
        hecho = []
        listas = {}
        for clave in ("general", "may", "lista2", "lista3", "lista4", "presupuestar"):
            lista = self.sudo()._buscar_para_clave(clave)
            valores = {}
            if not lista:
                lista = Pl.create({"name": NOMBRES_POR_CLAVE[clave][0], "currency_id": moneda.id,
                                   "selectable": clave == "general", "sequence": 16})
                hecho.append("creada la lista «%s» (id %s)" % (lista.name, lista.id))
            if lista.distri_clave != clave:
                valores["distri_clave"] = clave
            if not lista.active:
                valores["active"] = True
                hecho.append("reactivada la lista «%s» (id %s)" % (lista.name, lista.id))
            if clave == "may" and lista.name == LISTA_OCULTA_ANTERIOR:
                valores["name"] = LISTA_OCULTA
                hecho.append("«%s» (id %s) renombrada «%s»" % (LISTA_OCULTA_ANTERIOR, lista.id,
                                                              LISTA_OCULTA))
            if clave != "general" and not lista.distri_mayorista:
                valores["distri_mayorista"] = True
            if valores:
                lista.write(valores)
            listas[clave] = lista
        general, may, presupuestar = listas["general"], listas["may"], listas["presupuestar"]
        # May -> «Cigarrillos a presupuestar» -> General
        if self._asegurar_regla_base(presupuestar, general):
            hecho.append("«%s» basada en «%s»" % (presupuestar.name, general.name))
        if self._asegurar_regla_base(may, presupuestar):
            hecho.append("«%s»: lo que no tiene precio May toma el de General (los cigarrillos "
                         "sin precio, a presupuestar)" % may.name)
        # the per-category 0 rules of the first 129.52 builds give way to the
        # per-product ones
        viejas = may.item_ids.filtered(lambda i: i.applied_on == "2_product_category"
                                       and i.compute_price == "fixed" and not i.fixed_price)
        if viejas:
            hecho.append("«%s»: reglas por categoría quitadas (%s)" % (
                may.name, ", ".join(viejas.mapped("categ_id.name"))))
            viejas.unlink()
        n = self._sincronizar_presupuestar(
            env["product.template"].with_context(active_test=False).search(
                [("distri_precio_por_lista", "=", True)]))
        if n:
            hecho.append("«%s»: %s producto(s) a presupuestar" % (presupuestar.name, n))
        for clave in LISTAS_CIGARRILLOS:
            if self._asegurar_regla_base(listas[clave], may):
                hecho.append("«%s» basada en «%s»" % (listas[clave].name, may.name))
        return listas, hecho

    @api.model
    def _sincronizar_presupuestar(self, plantillas):
        """One 0 rule in «Cigarrillos a presupuestar» per product with
        distri_precio_por_lista, none for the rest of `plantillas`. Returns
        how many rules it created or removed."""
        lista = self._lista("presupuestar")
        if not lista or not plantillas:
            return 0
        Item = self.env["product.pricelist.item"].sudo()
        plantillas = plantillas.sudo().with_context(active_test=False)
        actuales = Item.search([("pricelist_id", "=", lista.id), ("applied_on", "=", "1_product"),
                                ("product_tmpl_id", "in", plantillas.ids)])
        con_regla = set(actuales.mapped("product_tmpl_id").ids)
        crear = [p for p in plantillas if p.distri_precio_por_lista and p.id not in con_regla]
        sobran = actuales.filtered(lambda i: not i.product_tmpl_id.distri_precio_por_lista)
        if crear:
            Item.create([{"pricelist_id": lista.id, "applied_on": "1_product",
                          "product_tmpl_id": p.id, "compute_price": "fixed",
                          "fixed_price": 0.0, "min_quantity": 0.0} for p in crear])
        sobran.unlink()
        return len(crear) + len(sobran)

    @api.model
    def _marcar_precio_por_lista(self):
        """For the migration: sets distri_precio_por_lista on the products of
        the cigarette categories (and those linked from a «May X - lista N»
        product) that nobody fixed yet (precio_por_lista_sugerido), and marks
        them fixed. Idempotent; never touches a value set by hand. Returns
        {True: templates, False: templates} changed."""
        Tmpl = self.env["product.template"].sudo().with_context(active_test=False)
        vinculados = Tmpl.search([("distri_lista_personal", "!=", False)]).mapped(
            "distri_cigarrillo_ids")
        candidatos = Tmpl.search([
            ("distri_lista_personal", "=", False), ("distri_precio_por_lista_fijado", "=", False),
            "|", ("categ_id.name", "in", list(CATEGORIAS_CIGARRILLOS_LISTAS)),
            ("id", "in", vinculados.ids)])
        cambios = {True: Tmpl.browse(), False: Tmpl.browse()}
        for plantilla in candidatos:
            valor = precio_por_lista_sugerido(plantilla.name, plantilla.categ_id.name,
                                              plantilla in vinculados)
            cambios[valor] |= plantilla
        for valor, plantillas in cambios.items():
            if plantillas:
                plantillas.write({"distri_precio_por_lista": valor,
                                  "distri_precio_por_lista_fijado": True})
        return cambios

    @api.model
    def _guardar_obligatorio(self, cambios):
        """The grid's toggle: {template id: bool}. Fixed by hand from then on."""
        check_listas(self.env)
        validos = set(self._cigarrillos().ids)
        Tmpl = self.env["product.template"].sudo()
        hechos = []
        for tmpl_id, valor in cambios.items():
            if tmpl_id not in validos:
                raise UserError("Ese producto no es de la grilla de cigarrillos.")
            plantilla = Tmpl.browse(tmpl_id)
            if plantilla.distri_precio_por_lista != bool(valor):
                hechos.append("%s: sin precio de lista → %s" % (
                    plantilla.name, "a presupuestar" if valor else "precio General"))
            plantilla.write({"distri_precio_por_lista": bool(valor),
                             "distri_precio_por_lista_fijado": True})
        lista = self._lista("presupuestar")
        if hechos and lista:
            lista.message_post(body=Markup("Cambiado por %s:<br/>%s") % (
                self.env.user.name, Markup("<br/>").join(hechos)))
        return len(hechos)

    @api.model
    def _listas_otra(self):
        """The hidden lists a gerente may pick for category 6: active, hidden
        (Revendedor NN and the like), not one of the keyed ones."""
        Pl = self.env["product.pricelist"].sudo()
        return Pl.search([("distri_clave", "=", False)]).filtered(
            lambda l: l._distri_es_oculta()).sorted(lambda l: (l.name or "", l.id))

    # ------------------------------------------------------- categories

    @api.model
    def _socio(self, socio):
        """The customer's company in this env (the caller's user), sudo."""
        return self.env["res.partner"].sudo().browse(socio.id).commercial_partner_id

    @api.model
    def _categoria(self, socio):
        """(category key, price list) of a customer, from its list and flag."""
        socio = self._socio(socio)
        lista = socio.property_product_pricelist
        general = self._lista("general")
        if not socio.distri_lista_oculta:
            es_general = not lista or lista == general or (
                not general and lista.name == LISTA_GENERAL)
            return ("1" if es_general else A_REVISAR), lista
        clave = lista.distri_clave
        if clave and clave != "general":
            return CATEGORIA_POR_CLAVE[clave], lista
        if lista and lista._distri_es_oculta():
            return "6", lista
        return A_REVISAR, lista

    @api.model
    def _lista_destino(self, categoria, lista_id=None):
        categoria = str(categoria or "")
        if categoria not in CLAVE_POR_CATEGORIA and categoria != "6":
            raise UserError("Elegí una categoría del 1 al 6.")
        if categoria != "6":
            lista = self._lista(CLAVE_POR_CATEGORIA[categoria])
            if not lista:
                raise UserError("Falta la lista «%s»: hay que actualizar el sistema."
                                % NOMBRE_CATEGORIA[categoria])
            return lista
        try:
            lista = self._listas_otra().filtered(lambda l: l.id == int(lista_id or 0))
        except (TypeError, ValueError):
            lista = None
        if not lista:
            raise UserError("Para «Otra lista» elegí una de las listas ocultas.")
        return lista

    @api.model
    def _pedidos_abiertos(self, socio):
        """The customer's orders whose prices may still change: not
        cancelled, not locked, not taken by the packer, no invoice."""
        socio = self._socio(socio)
        pedidos = self.env["sale.order"].sudo().search([
            ("partner_id", "child_of", socio.id), ("state", "in", ("draft", "sent", "sale")),
            ("distri_armado", "=", "pendiente"), ("locked", "=", False)], order="date_order, id")
        return pedidos.filtered(lambda p: not p.invoice_ids.filtered(
            lambda m: m.state != "cancel"))

    @api.model
    def _total_con_lista(self, pedido, lista):
        """About what the order would cost (untaxed) with `lista`."""
        total = 0.0
        for linea in pedido.order_line.filtered(lambda l: not l.display_type and l.product_id):
            plantilla = linea.product_id.product_tmpl_id
            if plantilla.distri_lista_personal:
                total += linea.price_subtotal
                continue
            precio = lista._get_product_price(linea.product_id, linea.product_uom_qty or 1.0,
                                              uom=linea.product_uom_id)
            total += precio * linea.product_uom_qty
        return total

    @api.model
    def _vista_previa(self, socio, categoria, lista_id=None):
        check_listas(self.env)
        socio = self._socio(socio)
        destino = self._lista_destino(categoria, lista_id)
        cat_antes, lista_antes = self._categoria(socio)
        pedidos = []
        for pedido in self._pedidos_abiertos(socio):
            pedidos.append({
                "id": pedido.id, "nombre": pedido.name,
                "fecha": pedido.date_order and pedido.date_order.strftime("%d/%m/%Y") or "",
                "lista": pedido.pricelist_id.name or "",
                "total": plata(pedido.amount_untaxed),
                "total_nuevo": plata(self._total_con_lista(pedido, destino)),
                "estado": "Confirmado" if pedido.state == "sale" else "Presupuesto",
            })
        oculta = str(categoria) != "1"
        return {
            "categoria_antes": cat_antes, "categoria_antes_nombre": NOMBRE_CATEGORIA[cat_antes],
            "lista_antes": lista_antes.name or "General",
            "categoria": str(categoria), "categoria_nombre": NOMBRE_CATEGORIA[str(categoria)],
            "lista": destino.name, "lista_id": destino.id,
            "oculta_antes": bool(socio.distri_lista_oculta), "oculta": oculta,
            "cambia": lista_antes != destino or bool(socio.distri_lista_oculta) != oculta,
            "pedidos": pedidos,
        }

    @api.model
    def _aplicar_categoria(self, socio, categoria, lista_id=None, pedido_ids=(), origen=""):
        """Sets the customer's list and hidden flag for the category and
        reprices the open orders in `pedido_ids` (opt-in). Posts who, when
        (the message's date) and old → new in the customer's chatter.
        Returns {"cambio": bool, "pedidos": [names]}."""
        check_listas(self.env)
        usuario = self.env.user
        socio = self._socio(socio)
        destino = self._lista_destino(categoria, lista_id)
        cat_antes, lista_antes = self._categoria(socio)
        oculta_antes = bool(socio.distri_lista_oculta)
        oculta = str(categoria) != "1"
        cambios = {}
        if lista_antes != destino:
            cambios["property_product_pricelist"] = destino.id
        if oculta_antes != oculta:
            cambios["distri_lista_oculta"] = oculta
        if cambios:
            socio.write(cambios)
        abiertos = self._pedidos_abiertos(socio)
        pedido_ids = {int(i) for i in pedido_ids or () if str(i).isdigit()}
        actualizados = []
        for pedido in abiertos.filtered(lambda p: p.id in pedido_ids):
            antes = pedido.amount_total
            lista_pedido = pedido.pricelist_id
            pedido = pedido.with_context(distri_armado=True)
            if lista_pedido != destino:
                pedido.write({"pricelist_id": destino.id})
            pedido._recompute_prices()
            pedido.message_post(body=Markup(
                "Precios actualizados con la lista <b>%s</b> (antes %s) por %s desde «Listas y "
                "accesos»: %s → %s.") % (destino.name, lista_pedido.name or "-", usuario.name,
                                         plata(antes), plata(pedido.amount_total)))
            actualizados.append("%s (%s → %s)" % (pedido.name, plata(antes),
                                                  plata(pedido.amount_total)))
        if cambios or actualizados:
            renglones = []
            if cambios:
                renglones.append(Markup("Categoría: %s → <b>%s</b>") % (
                    "%s · %s" % (cat_antes, NOMBRE_CATEGORIA[cat_antes]) if cat_antes != A_REVISAR
                    else NOMBRE_CATEGORIA[A_REVISAR],
                    "%s · %s" % (categoria, NOMBRE_CATEGORIA[str(categoria)])))
                renglones.append(Markup("Lista de precios: %s → <b>%s</b>") % (
                    lista_antes.name or "General", destino.name))
                renglones.append(Markup("Lista oculta: %s → <b>%s</b>") % (
                    "sí" if oculta_antes else "no", "sí" if oculta else "no"))
            if actualizados:
                renglones.append(Markup("Pedidos con precios actualizados: %s")
                                 % ", ".join(actualizados))
            socio.message_post(body=Markup(
                "<b>Lista cambiada</b> por %s%s.<br/>%s") % (
                usuario.name, (" (%s)" % origen) if origen else "",
                Markup("<br/>").join(renglones)))
        return {"cambio": bool(cambios), "pedidos": actualizados}

    @api.model
    def _clientes(self, texto="", categoria=""):
        """Customers for the list of the screen: companies (no contacts of a
        company), not the generic ones, with the category of each."""
        Partner = self.env["res.partner"].sudo()
        dominio = [("parent_id", "=", False), ("distri_generic_partner", "=", False),
                   "|", "|", ("customer_rank", ">", 0), ("distri_lista_oculta", "=", True),
                   ("distri_usuario_id", "!=", False)]
        texto = (texto or "").strip()
        if texto:
            dominio += ["|", "|", ("name", "ilike", texto), ("ref", "ilike", texto),
                        ("vat", "ilike", texto)]
        filas = []
        for socio in Partner.search(dominio, order="name, id"):
            cat, lista = self._categoria(socio)
            if categoria and cat != categoria:
                continue
            filas.append({"socio": socio, "categoria": cat,
                          "categoria_nombre": NOMBRE_CATEGORIA[cat],
                          "lista": lista.name or "General",
                          "acceso": bool(socio.distri_usuario_id)})
        return filas

    # ------------------------------------------------- cigarette prices

    @api.model
    def _cigarrillos(self):
        """The cigarettes of the grid: active, for sale, not a «May X - lista
        N» product. The temporary AgilPedido copies count: while that catalog
        is on (production, 26/11/2026) they are the only cigarettes for sale."""
        return self.env["product.template"].sudo().search([
            ("categ_id.name", "in", list(CATEGORIAS_CIGARRILLOS_LISTAS)), ("sale_ok", "=", True),
            ("distri_lista_personal", "=", False)], order="name, id")

    @api.model
    def _regla_cigarrillo(self, lista, plantilla_ids):
        """{template id: its own fixed rule} in `lista` (any quantity, no dates)."""
        reglas = {}
        for item in lista.sudo().item_ids:
            if (item.applied_on == "1_product" and item.product_tmpl_id.id in plantilla_ids
                    and item.compute_price == "fixed" and (item.min_quantity or 0) <= 1
                    and not item.date_start and not item.date_end):
                reglas.setdefault(item.product_tmpl_id.id, item)
        return reglas

    @api.model
    def _productos_may(self):
        """{(lista_3|lista_4, cigarette id): «May X - lista N» product}. A
        product may stand for several cigarettes (19.0.1.129.52, owner:
        «Milenio RED / GREEN 20» is RED and GREEN)."""
        productos = self.env["product.template"].sudo().search(
            [("distri_lista_personal", "in", ("lista_3", "lista_4")),
             ("distri_cigarrillo_ids", "!=", False)], order="id")
        mapa = {}
        for producto in productos:
            for cig in producto.distri_cigarrillo_ids:
                mapa.setdefault((producto.distri_lista_personal, cig.id), producto)
        return mapa

    @api.model
    def _dominio_may_suelto(self):
        """19.0.1.129.55: a plain «May …» product: active, name starting with
        «May » (any case), not a «May X - lista N» one. Its own sale price is
        the real «May farmacia y varios» price (that list only has a global
        rule)."""
        return [("name", "=ilike", "may %"), ("distri_lista_personal", "=", False)]

    @api.model
    def _es_may_suelto(self, producto):
        producto = producto.sudo()
        return bool(producto.active and not producto.distri_lista_personal
                    and (producto.name or "").lower().startswith("may "))

    @api.model
    def _clave_vinculo(self, producto):
        """The key of the «one product per cigarette» rule: lista_3 / lista_4
        or CLAVE_MAY_SUELTO."""
        return producto.distri_lista_personal or CLAVE_MAY_SUELTO

    @api.model
    def _productos_may_sueltos(self):
        """{cigarette id: plain «May …» product linked to it}. Only shown in
        the «May» column of the grid; no price comes from it."""
        productos = self.env["product.template"].sudo().search(
            self._dominio_may_suelto() + [("distri_cigarrillo_ids", "!=", False)], order="id")
        mapa = {}
        for producto in productos:
            for cig in producto.distri_cigarrillo_ids:
                mapa.setdefault(cig.id, producto)
        return mapa

    @api.model
    def _may_sueltos_para_vincular(self, cigarrillos=None):
        """The plain «May …» products the link screen offers: the linked ones
        plus those whose name (without «May») starts with the first word of a
        cigarette of the grid («May Milenio Red» -> «Milenio …», «May Ocb
        premium» -> «OCB …»). Keeps the screen short: there are many «May …»
        pharmacy products that are no cigarette at all."""
        if cigarrillos is None:
            cigarrillos = self._cigarrillos()
        def marca(nombre):
            palabras = _normalizar(nombre).split()
            return palabras[0] if palabras and not palabras[0].isdigit() else None

        marcas = {marca(c.name) for c in cigarrillos} - {None}
        productos = self.env["product.template"].sudo().search(
            self._dominio_may_suelto(), order="name, id")
        return productos.filtered(lambda p: p.distri_cigarrillo_ids or marca(p.name) in marcas)

    @api.model
    def _grilla(self):
        check_listas(self.env)
        cigarrillos = self._cigarrillos()
        ids = set(cigarrillos.ids)
        listas = {clave: self._lista(clave) for clave in LISTAS_CIGARRILLOS}
        reglas = {clave: self._regla_cigarrillo(lista, ids) if lista else {}
                  for clave, lista in listas.items()}
        may = self._productos_may()
        may_sueltos = self._productos_may_sueltos()
        filas = []
        for cig in cigarrillos:
            suelto = may_sueltos.get(cig.id)
            fila = {"id": cig.id, "nombre": cig.name, "precio_lista": cig.list_price,
                    "obligatorio": cig.distri_precio_por_lista,
                    "may": ({"id": suelto.id, "nombre": suelto.name, "precio": suelto.list_price}
                            if suelto else None)}
            for clave in LISTAS_CIGARRILLOS:
                regla = reglas[clave].get(cig.id)
                fila[clave] = regla.fixed_price if regla else None
                if clave in PERSONAL_POR_CLAVE:
                    producto = may.get((PERSONAL_POR_CLAVE[clave], cig.id))
                    fila["may_" + clave] = ({"id": producto.id, "nombre": producto.name,
                                             "precio": producto.list_price}
                                            if producto else None)
            filas.append(fila)
        return {"filas": filas, "listas": listas}

    @api.model
    def _guardar_precios(self, cambios):
        """cambios: {(clave, template id): price or None}. None (or 0) takes
        the rule away: the product goes back to its fallback («a presupuestar»
        with «Precio por lista obligatorio», else the General price). Returns
        how many rules changed. Logged in each list's chatter."""
        check_listas(self.env)
        validos = set(self._cigarrillos().ids)
        Item = self.env["product.pricelist.item"].sudo()
        Tmpl = self.env["product.template"].sudo()
        hechos = {}
        for (clave, tmpl_id), precio in cambios.items():
            if clave not in LISTAS_CIGARRILLOS or tmpl_id not in validos:
                raise UserError("Ese producto no es un cigarrillo de la grilla.")
            if precio is not None and precio < 0:
                raise UserError("El precio no puede ser negativo.")
            lista = self._lista(clave)
            if not lista:
                raise UserError("Falta la lista «%s»: hay que actualizar el sistema."
                                % dict(CLAVES_LISTA)[clave])
            regla = self._regla_cigarrillo(lista, {tmpl_id}).get(tmpl_id)
            antes = regla.fixed_price if regla else None
            nuevo = precio if precio else None
            if antes == nuevo or (antes is not None and nuevo is not None
                                  and lista.currency_id.compare_amounts(antes, nuevo) == 0):
                continue
            if nuevo is None:
                regla.unlink()
            elif regla:
                regla.fixed_price = nuevo
            else:
                Item.create({"pricelist_id": lista.id, "applied_on": "1_product",
                             "product_tmpl_id": tmpl_id, "compute_price": "fixed",
                             "fixed_price": nuevo, "min_quantity": 0.0})
            hechos.setdefault(lista, []).append("%s: %s → %s" % (
                Tmpl.browse(tmpl_id).name, plata(antes) if antes is not None else "a presupuestar",
                plata(nuevo) if nuevo is not None else "a presupuestar"))
        for lista, renglones in hechos.items():
            lista.message_post(body=Markup("Precios de cigarrillos cambiados por %s:<br/>%s") % (
                self.env.user.name, Markup("<br/>").join(renglones)))
        return sum(len(r) for r in hechos.values())

    @api.model
    def _usar_precio_may(self, clave, tmpl_ids=None):
        """«Usar precio del producto May» for one cigarette or (tmpl_ids None)
        all that have a linked «May X - lista N» product."""
        check_listas(self.env)
        if clave not in PERSONAL_POR_CLAVE:
            raise UserError("Solo la lista 3 y la lista 4 tienen productos May.")
        may = self._productos_may()
        cambios = {}
        for cig in self._cigarrillos():
            if tmpl_ids is not None and cig.id not in tmpl_ids:
                continue
            producto = may.get((PERSONAL_POR_CLAVE[clave], cig.id))
            if producto and producto.list_price > 0:
                cambios[(clave, cig.id)] = producto.list_price
        return self._guardar_precios(cambios)

    @api.model
    def _vincular(self, producto_may, cigarrillos):
        """Sets the cigarettes a «May X - lista N» product prices (empty =
        unlinked). One product per cigarette and list: a cigarette taken
        here is unlinked from any other product of the same list.
        19.0.1.129.55: a plain «May …» product (no list 3/4) links too, as
        its own list (CLAVE_MAY_SUELTO); that link only feeds the «May»
        column of the grid and changes no price."""
        check_listas(self.env)
        producto_may = producto_may.sudo()
        cigarrillos = cigarrillos.sudo()
        suelto = self._es_may_suelto(producto_may)
        if not producto_may.distri_lista_personal and not suelto:
            raise UserError("Ese producto no es de la lista 3 ni de la lista 4 ni un "
                            "producto «May …».")
        if cigarrillos - self._cigarrillos():
            raise UserError("Ese producto no es un cigarrillo de la grilla.")
        if producto_may.distri_cigarrillo_ids == cigarrillos:
            return False
        if cigarrillos:
            dominio = (self._dominio_may_suelto() if suelto else
                       [("distri_lista_personal", "=", producto_may.distri_lista_personal)])
            otros = self.env["product.template"].sudo().search(dominio + [
                ("distri_cigarrillo_ids", "in", cigarrillos.ids),
                ("id", "!=", producto_may.id)])
            for otro in otros:
                otro.distri_cigarrillo_ids = [(3, c.id) for c in cigarrillos]
        producto_may.distri_cigarrillo_ids = [(6, 0, cigarrillos.ids)]
        return True

    @api.model
    def _variantes_nombre(self, nombre):
        """The cigarettes a «May X - lista N» name stands for: «May Milenio
        RED / GREEN 20 - lista 3» -> ["milenio red 20", "milenio green 20"];
        a name without «/» -> [itself], normalized."""
        texto = re.sub(r"-\s*lista\s*\d\s*$", " ", nombre or "", flags=re.IGNORECASE)
        partido = re.search(r"(\S+)\s*/\s*(\S+)", texto)
        if not partido:
            return [_normalizar(texto)]
        antes, despues = texto[:partido.start()], texto[partido.end():]
        return [_normalizar("%s %s %s" % (antes, alternativa, despues))
                for alternativa in (partido.group(1), partido.group(2))]

    @api.model
    def _sugerir_vinculos(self):
        """For the migration: links each unlinked «May X - lista N» product
        to its cigarette(s) when the match is clear. A name with «A / B»
        («Milenio RED / GREEN 20») stands for one cigarette per alternative.
        For each one: first the cigarettes whose current Lista 3/4 rule has
        exactly the product's price, the closest name among them; else the
        closest name if it is clearly the best. Idempotent: linked products
        are skipped. Returns [(May product, cigarette)]."""
        Tmpl = self.env["product.template"].sudo().with_context(active_test=False)
        cigarrillos = self._cigarrillos()
        nombres = {c.id: _normalizar(c.name) for c in cigarrillos}
        hechos = []
        for valor, clave in (("lista_3", "lista3"), ("lista_4", "lista4")):
            lista = self._lista(clave, activas=False)
            reglas = self._regla_cigarrillo(lista, set(cigarrillos.ids)) if lista else {}
            usados = set(Tmpl.search([("distri_lista_personal", "=", valor),
                                      ("distri_cigarrillo_ids", "!=", False)]
                                     ).mapped("distri_cigarrillo_ids").ids)
            for producto in Tmpl.search([("distri_lista_personal", "=", valor),
                                         ("distri_cigarrillo_ids", "=", False)], order="id"):
                variantes = self._variantes_nombre(producto.name)
                elegidos = Tmpl.browse()
                for propio in variantes:

                    def parecido(cig, propio=propio):
                        return difflib.SequenceMatcher(None, propio, nombres[cig.id]).ratio()

                    def de_otra_variante(cig, propio=propio):
                        # «RED / GREEN»: the GREEN cigarette is not a rival of RED
                        mio = parecido(cig)
                        return any(difflib.SequenceMatcher(None, otra, nombres[cig.id]).ratio()
                                   > mio for otra in variantes if otra != propio)

                    libres = cigarrillos.filtered(
                        lambda c: c.id not in usados and c not in elegidos
                        and not de_otra_variante(c))
                    por_precio = libres.filtered(
                        lambda c: c.id in reglas and lista.currency_id.compare_amounts(
                            reglas[c.id].fixed_price, producto.list_price) == 0)
                    elegido = None
                    if por_precio:
                        mejor = max(por_precio, key=parecido)
                        if parecido(mejor) >= 0.4:
                            elegido = mejor
                    if not elegido and libres:
                        orden = sorted(libres, key=parecido, reverse=True)
                        primero = parecido(orden[0])
                        segundo = parecido(orden[1]) if len(orden) > 1 else 0.0
                        if primero >= 0.75 and primero - segundo >= 0.1:
                            elegido = orden[0]
                    if elegido:
                        elegidos |= elegido
                if elegidos:
                    producto.distri_cigarrillo_ids = [(6, 0, elegidos.ids)]
                    usados.update(elegidos.ids)
                    hechos += [(producto, cig) for cig in elegidos]
        return hechos

    # --------------------------------------------------------- web access

    @api.model
    def _accesos(self):
        """Customers with the hidden list or a web user: login, last login,
        active, must change the password."""
        check_listas(self.env)
        socios = self.env["res.partner"].sudo().with_context(active_test=False).search(
            [("parent_id", "=", False), "|", ("distri_lista_oculta", "=", True),
             ("distri_usuario_id", "!=", False)], order="name, id")
        filas = []
        for socio in socios.filtered("active"):
            usuario = socio.distri_usuario_id.sudo().with_context(active_test=False)
            cat, lista = self._categoria(socio)
            filas.append({
                "socio": socio, "categoria": cat, "categoria_nombre": NOMBRE_CATEGORIA[cat],
                "lista": lista.name or "General", "cuit": cuit_digitos(socio.vat),
                "login": usuario.login or "", "activo": bool(usuario) and usuario.active,
                "ultimo": usuario.login_date and usuario.login_date.strftime("%d/%m/%Y %H:%M")
                or "",
                "debe_cambiar": bool(usuario) and usuario.distri_debe_cambiar_clave,
                "tiene_usuario": bool(usuario),
            })
        return filas

    # ----------------------------------------------------- initial load

    @api.model
    def _leer_csv(self, contenido):
        """The rows of an initial-load CSV (partner id, category 1-6, list
        name for 6), checked. Nothing is written."""
        check_listas(self.env)
        if isinstance(contenido, str):
            contenido = contenido.encode("utf-8")
        if not contenido:
            raise UserError("El archivo está vacío.")
        if len(contenido) > CSV_MAX_BYTES:
            raise UserError("El archivo es muy grande (máximo 2 MB).")
        try:
            texto = contenido.decode("utf-8-sig")
        except UnicodeDecodeError:
            texto = contenido.decode("latin-1")
        primera = texto.splitlines()[0] if texto.splitlines() else ""
        separador = max((";", ",", "\t"), key=primera.count)
        lectores = list(csv.reader(io.StringIO(texto), delimiter=separador))
        Partner = self.env["res.partner"].sudo().with_context(active_test=False)
        otras = {l.name.strip().lower(): l for l in self._listas_otra()}
        vistos = set()
        filas = []
        for n, columnas in enumerate(lectores, start=1):
            columnas = [c.strip() for c in columnas]
            if not any(columnas):
                continue
            if n == 1 and columnas and not columnas[0].isdigit():
                continue  # header
            if len(filas) >= CSV_MAX_FILAS:
                raise UserError("Demasiadas filas (máximo %s)." % CSV_MAX_FILAS)
            fila = {"linea": n, "id": columnas[0] if columnas else "",
                    "categoria": columnas[1] if len(columnas) > 1 else "",
                    "lista_nombre": columnas[2] if len(columnas) > 2 else "",
                    "estado": "error", "mensaje": "", "avisos": [], "socio": None,
                    "antes": "", "despues": "", "lista_id": False, "pedidos": 0}
            filas.append(fila)
            if not fila["id"].isdigit():
                fila["mensaje"] = "El id del cliente no es un número."
                continue
            socio = Partner.browse(int(fila["id"])).exists()
            if not socio or socio.distri_generic_partner:
                fila["mensaje"] = "Cliente desconocido."
                continue
            if socio.parent_id:
                fila["mensaje"] = ("Es un contacto de %s: usá el id de la empresa (%s)."
                                   % (socio.parent_id.name, socio.commercial_partner_id.id))
                continue
            if not socio.active:
                fila["mensaje"] = "El cliente está archivado."
                continue
            fila["socio"] = socio
            if socio.id in vistos:
                fila["mensaje"] = "El cliente está repetido en el archivo."
                continue
            vistos.add(socio.id)
            categoria = fila["categoria"]
            if categoria not in NOMBRE_CATEGORIA or categoria == A_REVISAR:
                fila["mensaje"] = "Categoría inválida «%s»: tiene que ser del 1 al 6." % categoria
                continue
            if categoria == "6":
                lista = otras.get(fila["lista_nombre"].lower())
                if not lista:
                    fila["mensaje"] = ("Lista desconocida «%s»: para la categoría 6 escribí el "
                                       "nombre de una lista oculta (por ejemplo Revendedor 01)."
                                       % fila["lista_nombre"])
                    continue
                fila["lista_id"] = lista.id
            elif fila["lista_nombre"]:
                fila["avisos"].append("La lista «%s» se ignora: solo cuenta para la categoría 6."
                                      % fila["lista_nombre"])
            destino = self._lista_destino(categoria, fila["lista_id"])
            cat_antes, lista_antes = self._categoria(socio)
            fila["antes"] = "%s · %s" % (NOMBRE_CATEGORIA[cat_antes], lista_antes.name or "General")
            fila["despues"] = "%s · %s" % (NOMBRE_CATEGORIA[categoria], destino.name)
            oculta = categoria != "1"
            if lista_antes == destino and bool(socio.distri_lista_oculta) == oculta:
                fila["estado"] = "igual"
                fila["mensaje"] = "Sin cambios."
            else:
                fila["estado"] = "ok"
            cuit = cuit_digitos(socio.vat)
            if oculta and not socio.distri_usuario_id and cuit:
                conflicto = socio._distri_conflicto_cuit(cuit)
                if conflicto:
                    fila["avisos"].append(conflicto + " No se le va a poder crear el acceso web "
                                          "con el CUIT.")
            pedidos = len(self._pedidos_abiertos(socio))
            fila["pedidos"] = pedidos
            if pedidos and fila["estado"] == "ok":
                fila["avisos"].append(
                    "%s pedido(s) abierto(s): la carga no les cambia el precio; para "
                    "actualizarlos, cambiá la categoría desde «Clientes»." % pedidos)
        return filas

    @api.model
    def _aplicar_csv(self, contenido):
        """Applies the valid rows that change something. Idempotent: a
        second run finds every row «Sin cambios». Returns (applied, rows)."""
        check_listas(self.env)
        filas = self._leer_csv(contenido)
        aplicadas = 0
        for fila in filas:
            if fila["estado"] != "ok":
                continue
            self._aplicar_categoria(fila["socio"], fila["categoria"], fila["lista_id"],
                                    origen="carga inicial")
            aplicadas += 1
        return aplicadas, filas

