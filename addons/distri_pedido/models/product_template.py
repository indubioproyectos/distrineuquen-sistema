from odoo import fields, models


class ProductTemplate(models.Model):
    _inherit = "product.template"

    # Los destacados salen arriba de todo, sueltos y sin categoria. Se usan
    # para ofertas relampago: mercaderia con fecha de vencimiento comprometida
    # que hay que sacar rapido. Es el mismo lugar donde AgilPedido muestra la
    # tarjeta de "SUPER OFERTA".
    destacado = fields.Boolean(
        string="Destacar arriba de todo",
        help="Lo muestra suelto arriba del catalogo, antes de las categorias.",
    )
    destacado_vence = fields.Date(
        string="Vence el",
        help="Opcional. Si se carga, la tarjeta avisa hasta cuando dura.",
    )
