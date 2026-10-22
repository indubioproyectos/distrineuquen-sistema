from odoo import fields, models


class ProductCategory(models.Model):
    _inherit = "product.category"

    # La portada muestra una foto por seccion. Si no se carga ninguna, agarra
    # la del primer producto que tenga, que no siempre representa al rubro.
    # Casi todos los productos tienen foto, asi que en vez de subir archivos
    # alcanza con elegir cual representa al rubro: la marca principal.
    distri_producto_id = fields.Many2one(
        "product.template", string="Producto de la portada",
        domain="[('categ_id', '=', id), ('image_1920', '!=', False)]",
        help="El producto cuya foto representa esta sección en la portada. "
             "Conviene elegir la marca principal del rubro.",
    )
    distri_imagen = fields.Image(
        string="Foto para la portada",
        max_width=512, max_height=512,
        help="Solo si querés una imagen propia, distinta de la de un producto. Si la cargás, gana sobre el producto elegido.",
    )
