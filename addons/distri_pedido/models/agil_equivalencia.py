"""Equivalencias de productos AgilPedido: Odoo aprende los nombres.

Transicion (27/09/2026): AgilPedido no tiene listas de precios ni unidades de
venta, y eso no se puede configurar. Vende, por ejemplo, «Bulto DURACELL AAA
X 120U.» a $ 130.000; en Odoo el mismo producto es «Duracell AAA», que se
vende solo en la unidad «Bulto x 120» (migracion del catalogo, 22/09/2026)
con el precio por unidad suelta.

Los nombres si se pueden editar en AgilPedido, y el dueño los va a ir
cambiando a la convencion del catalogo exportado (exportar_agilpedido.py):
el nombre exacto de Odoo, y « · Bulto x 120 u.» si va por envase. Esos entran
solos (importar_pedido.identificar). Esta tabla es la red de seguridad para
los nombres que todavia no se corrigieron:

- Se aprende cuando el personal corrige un renglon marcado de un pedido de
  AgilPedido: eligiendo el producto, con «Está bien» sobre el propuesto, o con
  «Corregir producto» (el asistente de abajo), que pregunta tambien como
  viene en AgilPedido (la unidad de venta del producto).
- Se usa al cargar el mensaje (agilpedido_wa.py y el asistente de pegar),
  antes de buscar el producto: el renglon entra con el producto y la unidad
  aprendidos, la cantidad de AgilPedido y el precio de Odoo, sin marcar.
- El factor (cuantas de esa unidad es 1 de AgilPedido) queda para cuando la
  unidad no existe en Odoo; con la unidad correcta es 1.

Nunca se aprende de un renglon que entro solo y nadie corrigio, ni el
producto «A CORREGIR». El precio nunca sale del mensaje.
"""
from odoo import api, fields, models
from odoo.exceptions import UserError, ValidationError

from .agilpedido_wa import PRODUCTO_A_CORREGIR, cantidad_texto, equivale_texto
from .importar_pedido import leer_envase, leer_renglon, normalizar, unidad_de_venta, unidades_de_venta


class DistriAgilEquivalencia(models.Model):
    _name = "distri.agil.equivalencia"
    _description = "Equivalencia de producto AgilPedido"
    _inherit = ["mail.thread"]
    _order = "nombre_agil, id"
    _rec_name = "nombre_agil"

    nombre_agil = fields.Char(
        string="Nombre en AgilPedido", required=True, tracking=True,
        help="Como se ve en el mensaje de AgilPedido, sin el «May» de adelante y sin la "
             "cantidad ni el importe.")
    clave = fields.Char(
        string="Clave de búsqueda", compute="_compute_clave", store=True, readonly=True,
        index=True,
        help="El nombre sin mayúsculas, acentos ni puntuación: así se busca al cargar el "
             "mensaje.")
    product_id = fields.Many2one(
        "product.product", string="Producto de Odoo", required=True, tracking=True,
        ondelete="cascade", domain=[("sale_ok", "=", True)])
    product_uom_id = fields.Many2one(
        "uom.uom", string="Cómo viene en AgilPedido", required=True, tracking=True,
        ondelete="restrict", domain="[('id', 'in', unidades_ids)]",
        help="La unidad de venta del producto que es 1 de AgilPedido: «Bulto x 120» si "
             "AgilPedido vende el bulto.")
    unidades_ids = fields.Many2many("uom.uom", compute="_compute_unidades_ids")
    factor = fields.Float(
        string="Cantidad por 1 de AgilPedido", default=1.0, required=True, tracking=True,
        digits="Product Unit",
        help="Cuántas de esa unidad es 1 de AgilPedido. Con la unidad correcta es 1; otro "
             "número solo si la unidad no existe en Odoo.")
    equivale = fields.Char(string="Equivale a", compute="_compute_equivale")
    pedido_id = fields.Many2one(
        "sale.order", string="Aprendida en el pedido", readonly=True, ondelete="set null",
        copy=False)
    ultimo_uso = fields.Datetime(string="Último uso", readonly=True, copy=False)
    usos = fields.Integer(string="Usos", readonly=True, copy=False,
                          help="Cuántos renglones se cargaron solos con esta equivalencia.")
    active = fields.Boolean(string="Activa", default=True, tracking=True)

    _distri_clave_unica = models.Constraint(
        "UNIQUE(clave)", "Ya hay una equivalencia para ese nombre de AgilPedido (puede estar "
        "archivada).")
    _distri_factor_positivo = models.Constraint(
        "CHECK(factor > 0)", "La cantidad por 1 de AgilPedido tiene que ser más que cero.")

    @api.depends("nombre_agil")
    def _compute_clave(self):
        for equivalencia in self:
            equivalencia.clave = normalizar(equivalencia.nombre_agil)

    @api.depends("product_id")
    def _compute_unidades_ids(self):
        for equivalencia in self:
            equivalencia.unidades_ids = unidades_de_venta(equivalencia.product_id.product_tmpl_id) \
                if equivalencia.product_id else self.env["uom.uom"]

    @api.depends("factor", "product_uom_id", "product_id")
    def _compute_equivale(self):
        for equivalencia in self:
            equivalencia.equivale = equivalencia.product_id and equivale_texto(
                equivalencia.factor, equivalencia.product_uom_id, equivalencia.product_id)

    @api.onchange("product_id")
    def _onchange_product_id_unidad(self):
        for equivalencia in self:
            plantilla = equivalencia.product_id.product_tmpl_id
            if plantilla and equivalencia.product_uom_id not in unidades_de_venta(plantilla):
                equivalencia.product_uom_id = unidad_de_venta(plantilla)[0]

    @api.constrains("clave", "product_id", "product_uom_id")
    def _check_clave_y_producto(self):
        a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR,
                                  raise_if_not_found=False)
        for equivalencia in self:
            if not equivalencia.clave:
                raise ValidationError("El nombre de AgilPedido tiene que tener letras o números.")
            if a_corregir and equivalencia.product_id == a_corregir:
                raise ValidationError("El producto «A CORREGIR» no puede ser una equivalencia.")
            if equivalencia.product_uom_id not in unidades_de_venta(
                    equivalencia.product_id.product_tmpl_id):
                raise ValidationError(
                    "%s no se vende en %s. Unidades de venta: %s." % (
                        equivalencia.product_id.display_name, equivalencia.product_uom_id.name,
                        ", ".join(unidades_de_venta(
                            equivalencia.product_id.product_tmpl_id).mapped("name"))))

    # ------------------------------------------------------------ uso

    def _distri_vendible(self):
        """El producto aprendido se sigue vendiendo, y en esa unidad."""
        self.ensure_one()
        plantilla = self.product_id.product_tmpl_id
        return bool(self.product_id.active and plantilla.active and plantilla.sale_ok
                    and self.product_uom_id in unidades_de_venta(plantilla))

    @api.model
    def _distri_buscar(self, nombre):
        """La equivalencia activa para un nombre de AgilPedido (sin «May»).
        Puede apuntar a un producto que ya no se vende: lo decide quien la usa."""
        clave = normalizar(nombre)
        if not clave:
            return self.browse()
        return self.sudo().search([("clave", "=", clave)], limit=1)

    def _distri_registrar_uso(self):
        ahora = fields.Datetime.now()
        for equivalencia in self.sudo():
            equivalencia.write({"usos": equivalencia.usos + 1, "ultimo_uso": ahora})

    def _distri_texto(self):
        """«1 = Bulto x 120 (120 u.)»"""
        self.ensure_one()
        return equivale_texto(self.factor, self.product_uom_id, self.product_id)

    # ------------------------------------------------------------ aprendizaje

    @api.model
    def _distri_aprender(self, nombre, producto, unidad, factor=None, pedido=False):
        """Crea o actualiza la equivalencia de un nombre de AgilPedido.

        Sin `factor` (se eligió el producto o se tocó «Está bien»): si ya
        estaba aprendido el mismo producto en la misma unidad se respeta su
        factor; si no, 1. Una equivalencia archivada se vuelve a activar: la
        corrección nueva manda. Devuelve la equivalencia, o vacío si no hay
        nada que aprender."""
        clave = normalizar(nombre)
        a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR,
                                  raise_if_not_found=False)
        if not clave or not producto or (a_corregir and producto == a_corregir):
            return self.browse()
        plantilla = producto.product_tmpl_id
        if unidad not in unidades_de_venta(plantilla):
            unidad = unidad_de_venta(plantilla)[0]
        Equivalencia = self.sudo().with_context(active_test=False)
        equivalencia = Equivalencia.search([("clave", "=", clave)], limit=1)
        if not equivalencia:
            return Equivalencia.create({
                "nombre_agil": " ".join((nombre or "").split()),
                "product_id": producto.id,
                "product_uom_id": unidad.id,
                "factor": factor or 1.0,
                "pedido_id": pedido and pedido.id,
            })
        if factor is None:
            misma = equivalencia.product_id == producto and equivalencia.product_uom_id == unidad
            factor = equivalencia.factor if misma else 1.0
        valores = {"active": True, "pedido_id": pedido and pedido.id}
        if equivalencia.product_id != producto:
            valores["product_id"] = producto.id
        if equivalencia.product_uom_id != unidad:
            valores["product_uom_id"] = unidad.id
        if equivalencia.factor != factor:
            valores["factor"] = factor
        equivalencia.write(valores)
        return equivalencia


class DistriAgilCorregirRenglon(models.TransientModel):
    """«Corregir producto»: el producto de Odoo de un renglón de AgilPedido y
    cómo viene en AgilPedido (su unidad de venta). Queda aprendido."""
    _name = "distri.agil.corregir.renglon"
    _description = "Corregir el producto de un renglón de AgilPedido"

    linea_id = fields.Many2one("sale.order.line", string="Renglón", required=True,
                               readonly=True, ondelete="cascade")
    currency_id = fields.Many2one(related="linea_id.currency_id")
    texto_agil = fields.Char(related="linea_id.distri_agil_texto", string="Como vino en AgilPedido")
    motivo = fields.Char(related="linea_id.distri_agil_motivo", string="Qué revisar")
    importe_agil = fields.Monetary(related="linea_id.distri_agil_importe",
                                   string="Subtotal en AgilPedido", currency_field="currency_id")
    cantidad_agil = fields.Float(string="Cantidad en AgilPedido",
                                 compute="_compute_renglon_agil", digits="Product Unit")
    es_mayorista = fields.Boolean(string="Vino con «May»", compute="_compute_renglon_agil")
    product_id = fields.Many2one(
        "product.product", string="Producto de Odoo", required=True,
        domain=[("sale_ok", "=", True)])
    unidades_ids = fields.Many2many("uom.uom", compute="_compute_unidades_ids")
    product_uom_id = fields.Many2one(
        "uom.uom", string="¿Cómo viene en AgilPedido?", required=True,
        domain="[('id', 'in', unidades_ids)]",
        help="1 de AgilPedido es 1 de esta unidad. Si el producto se vende solo por bulto, "
             "solo aparece el bulto.")
    otra_cantidad = fields.Boolean(
        string="La unidad no está en Odoo",
        help="Solo si ninguna unidad sirve: AgilPedido vende un envase que en Odoo no existe.")
    factor = fields.Float(
        string="¿Cuántas unidades de Odoo es 1 de AgilPedido?", default=1.0, required=True,
        digits="Product Unit")
    cantidad_odoo = fields.Float(string="Cantidad en Odoo", compute="_compute_cantidad_odoo",
                                 digits="Product Unit")
    resumen = fields.Char(string="Queda en el pedido", compute="_compute_cantidad_odoo")

    @api.model
    def default_get(self, fields_list):
        valores = super().default_get(fields_list)
        linea = self.env["sale.order.line"].browse(
            valores.get("linea_id") or self.env.context.get("default_linea_id"))
        if not linea.exists():
            return valores
        a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR,
                                  raise_if_not_found=False)
        renglon = leer_renglon(linea.distri_agil_texto or "")
        equivalencia = self.env["distri.agil.equivalencia"]
        if renglon:
            equivalencia = equivalencia._distri_buscar(renglon["nombre_limpio"])
        producto = unidad = False
        if equivalencia and equivalencia._distri_vendible():
            producto, unidad = equivalencia.product_id, equivalencia.product_uom_id
            if "factor" in fields_list:
                valores["factor"] = equivalencia.factor
                valores["otra_cantidad"] = equivalencia.factor != 1
        elif linea.product_id and linea.product_id != a_corregir:
            producto = linea.product_id
            if linea.product_uom_id in unidades_de_venta(producto.product_tmpl_id):
                unidad = linea.product_uom_id
        if producto and "product_id" in fields_list and not valores.get("product_id"):
            valores["product_id"] = producto.id
            valores["product_uom_id"] = (unidad or self._unidad_propuesta(producto, renglon)).id
        return valores

    @api.model
    def _unidad_propuesta(self, producto, renglon):
        """La unidad que dice el envase del nombre, si el producto la tiene;
        si no, la primera unidad de venta (el bulto, si solo va por bulto)."""
        envase = renglon and leer_envase(renglon["nombre_limpio"])
        return unidad_de_venta(producto.product_tmpl_id, envase and envase["contiene"])[0]

    @api.depends("linea_id.distri_agil_texto")
    def _compute_renglon_agil(self):
        for asistente in self:
            renglon = leer_renglon(asistente.linea_id.distri_agil_texto or "")
            asistente.cantidad_agil = renglon["cantidad"] if renglon else 0.0
            asistente.es_mayorista = bool(renglon and renglon["es_mayorista"])

    @api.depends("product_id")
    def _compute_unidades_ids(self):
        for asistente in self:
            asistente.unidades_ids = unidades_de_venta(asistente.product_id.product_tmpl_id) \
                if asistente.product_id else self.env["uom.uom"]

    @api.onchange("product_id")
    def _onchange_product_id_unidad(self):
        for asistente in self:
            if asistente.product_id and asistente.product_uom_id not in unidades_de_venta(
                    asistente.product_id.product_tmpl_id):
                asistente.product_uom_id = self._unidad_propuesta(
                    asistente.product_id, leer_renglon(asistente.texto_agil or ""))

    @api.onchange("otra_cantidad")
    def _onchange_otra_cantidad(self):
        for asistente in self:
            if not asistente.otra_cantidad:
                asistente.factor = 1.0

    @api.depends("cantidad_agil", "factor", "otra_cantidad", "product_id", "product_uom_id")
    def _compute_cantidad_odoo(self):
        for asistente in self:
            factor = asistente._factor()
            asistente.cantidad_odoo = asistente.cantidad_agil * factor
            if asistente.product_id and asistente.product_uom_id and factor > 0:
                asistente.resumen = "%s %s de %s · %s" % (
                    cantidad_texto(asistente.cantidad_odoo), asistente.product_uom_id.name,
                    asistente.product_id.name,
                    equivale_texto(factor, asistente.product_uom_id, asistente.product_id))
            else:
                asistente.resumen = False

    def _factor(self):
        self.ensure_one()
        return self.factor if self.otra_cantidad else 1.0

    def action_corregir(self):
        self.ensure_one()
        linea = self.linea_id
        a_corregir = self.env.ref("distri_pedido.%s" % PRODUCTO_A_CORREGIR,
                                  raise_if_not_found=False)
        if linea.order_id.state not in ("draft", "sent"):
            raise UserError("El pedido ya está aprobado.")
        if not self.product_id or (a_corregir and self.product_id == a_corregir):
            raise UserError("Elegí el producto de Odoo.")
        if self.product_uom_id not in unidades_de_venta(self.product_id.product_tmpl_id):
            raise UserError("%s no se vende en %s." % (
                self.product_id.display_name, self.product_uom_id.name))
        if self._factor() <= 0:
            raise UserError("La cantidad por 1 de AgilPedido tiene que ser más que cero.")
        renglon = leer_renglon(linea.distri_agil_texto or "")
        if not renglon:
            raise UserError("Este renglón no vino de AgilPedido: corregilo en el pedido.")
        linea._distri_agil_corregir_con_factor(
            self.product_id, self.product_uom_id, self._factor(), renglon)
        return {"type": "ir.actions.act_window_close"}
