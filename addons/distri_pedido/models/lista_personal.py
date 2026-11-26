"""19.0.1.129.46 (owner, 26/11/2026): the «lista 3» and «lista 4» products.

They are products of their own («May BOLD 20 - lista 3», …) with their price
as the product's sales price (list_price). They are never published: only
Administración, Gerencia and the system administrators (ve_listas_personales)
see them, as two extra sections «Lista 3» and «Lista 4» of /pedido (staff),
«Nueva venta»'s picker and its embedded catalog (controllers/main.py
_secciones_listas_personales), and only they may post them
(_plantillas_del_catalogo). Customers, visitors, sellers, drivers, packers
and distributors never get them, even if someone publishes one.

They are identified by product.template.distri_lista_personal (editable on
the product form); migrations/19.0.1.129.46 sets it on the existing ones by
name (marcar_por_nombre) and a new product named «… - lista 3/4» gets it
when created.

Price: an order line of one of these products takes the product's own sales
price, never a price list's rule (sale.order.line._compute_pricelist_item_id
below), so the customer's list, «Actualizar precios» and the assignment of
the customer keep exactly that price.
"""
import re

from odoo import api, fields, models

LISTAS_PERSONALES = [("lista_3", "Lista 3"), ("lista_4", "Lista 4")]
# «… - lista 3» / «… - Lista 4» at the end of the name, any case
PATRON_NOMBRE = re.compile(r"-\s*lista\s*([34])\s*$", re.IGNORECASE)
GRUPOS_LISTAS_PERSONALES = ("distri_pedido.grupo_administracion",
                            "distri_pedido.grupo_gerencia", "base.group_system")


def ve_listas_personales(user):
    """Administración, Gerencia or Sistemas, internal and logged in."""
    if not user or user._is_public() or user.share:
        return False
    return any(user.has_group(g) for g in GRUPOS_LISTAS_PERSONALES)


def lista_por_nombre(nombre):
    """'lista_3' / 'lista_4' for a name ending in «- lista 3/4», else False."""
    encontrado = PATRON_NOMBRE.search(nombre or "")
    return "lista_%s" % encontrado.group(1) if encontrado else False


def marcar_por_nombre(env):
    """Sets distri_lista_personal on the products named «… - lista 3/4»
    (archived ones too) that have none yet. Returns {value: templates}."""
    Tmpl = env["product.template"].sudo().with_context(active_test=False)
    candidatos = Tmpl.search([("distri_lista_personal", "=", False), "|",
                              ("name", "ilike", "lista 3"), ("name", "ilike", "lista 4")])
    marcados = {}
    for plantilla in candidatos:
        valor = lista_por_nombre(plantilla.name)
        if valor:
            marcados.setdefault(valor, Tmpl.browse())
            marcados[valor] |= plantilla
    for valor, plantillas in marcados.items():
        plantillas.write({"distri_lista_personal": valor})
    return marcados


class ProductTemplateListaPersonal(models.Model):
    _inherit = "product.template"

    distri_lista_personal = fields.Selection(
        LISTAS_PERSONALES, string="Lista 3 / Lista 4", index=True, copy=False,
        help="Producto de la lista 3 o de la lista 4: sale en «Nueva venta» y en /pedido "
             "solo para Administración, Gerencia y Sistemas, a su propio precio de venta "
             "(no lo cambia la lista de precios del cliente). No hace falta publicarlo.")

    @api.model_create_multi
    def create(self, vals_list):
        """A new «… - lista 3/4» product is marked from its name, unless the
        value is given (they keep being loaded: 14 more on 26/11/2026).
        Renaming later changes nothing: the form decides then."""
        for vals in vals_list:
            if "distri_lista_personal" not in vals and vals.get("name"):
                valor = lista_por_nombre(vals["name"])
                if valor:
                    vals["distri_lista_personal"] = valor
        return super().create(vals_list)


class SaleOrderLineListaPersonal(models.Model):
    _inherit = "sale.order.line"

    @api.depends("product_id.product_tmpl_id.distri_lista_personal")
    def _compute_pricelist_item_id(self):
        """No price list rule for a lista 3/4 product: Odoo then prices the
        line with the product's sales price (product.pricelist.item.
        _compute_price on an empty rule) and gives no discount."""
        personales = self.filtered(lambda l: l.product_id.product_tmpl_id.distri_lista_personal)
        personales.pricelist_item_id = False
        super(SaleOrderLineListaPersonal, self - personales)._compute_pricelist_item_id()
