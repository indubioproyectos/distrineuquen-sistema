import math
import unicodedata

from psycopg2 import errors as pg_errors

from odoo import api, fields, models
from odoo.exceptions import UserError

from .reparto_cliente import town_from_name


# El texto que ve el cliente cuando quiere guardar un cambio y el armador ya
# tomo el pedido. Lo usan el controlador y el backend.
YA_SE_ESTA_ARMANDO = (
    "Tu pedido ya se está armando. Si necesitás agregar algo, "
    "escribinos por WhatsApp."
)

# Mismo orden que date.weekday(): lunes es 0
DIAS = [
    ("dia_lunes", "lunes"),
    ("dia_martes", "martes"),
    ("dia_miercoles", "miércoles"),
    ("dia_jueves", "jueves"),
    ("dia_viernes", "viernes"),
    ("dia_sabado", "sábado"),
]

ESTADOS_ARMADO = [
    ("pendiente", "Pendiente"),
    ("en_armado", "En armado"),
    ("armado", "Armado"),
]

# Tope de bultos: mas que esto es un error de tipeo
MAX_BULTOS = 999

# Los campos del renglon que cambian lo que se entrega o se cobra. Los demas
# (cantidad entregada, facturada) los escribe Odoo solo y no se bloquean.
CAMPOS_DEL_PEDIDO = ("product_id", "product_uom_qty", "product_uom_id",
                     "price_unit", "discount")


def normalizar_ciudad(texto):
    """"NEUQUÉN ", "neuquen" y "Neuquén" son la misma ciudad.

    En la base hay de todo: la ciudad del cliente se cargo a mano durante
    anios, en mayusculas, con y sin tilde.
    """
    texto = unicodedata.normalize("NFKD", texto or "")
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.lower().split())


def _por_unidades(env, unidad):
    """True when the unit of measure is «Units» or a pack of units."""
    unidades = env.ref("uom.product_uom_unit", raise_if_not_found=False)
    actual, vistas = unidad, set()
    while actual and actual.id not in vistas:
        if actual == unidades:
            return True
        vistas.add(actual.id)
        actual = actual.relative_uom_id
    return False


class DistriZona(models.Model):
    _name = "distri.zona"
    _description = "Zona de reparto"
    _order = "viaje, sequence, name"

    name = fields.Char(string="Ciudad", required=True)
    sequence = fields.Integer(string="Orden", default=10)
    viaje = fields.Selection(
        [("1", "Viaje 1"), ("2", "Viaje 2")],
        string="Viaje", required=True, default="1",
        help="El armador ve primero las zonas del viaje 1.",
    )
    dia_lunes = fields.Boolean(string="Lunes")
    dia_martes = fields.Boolean(string="Martes")
    dia_miercoles = fields.Boolean(string="Miércoles")
    dia_jueves = fields.Boolean(string="Jueves")
    dia_viernes = fields.Boolean(string="Viernes")
    dia_sabado = fields.Boolean(string="Sábado")
    dias_texto = fields.Char(string="Días de reparto", compute="_compute_dias_texto")
    active = fields.Boolean(default=True)

    _nombre_unico = models.Constraint(
        "unique(name)", "Ya hay una zona con esa ciudad.",
    )

    @api.depends(*[campo for campo, _ in DIAS])
    def _compute_dias_texto(self):
        for zona in self:
            dias = [nombre for campo, nombre in DIAS if zona[campo]]
            if len(dias) == 5 and not zona.dia_sabado:
                zona.dias_texto = "lunes a viernes"
            else:
                zona.dias_texto = ", ".join(dias) or "sin días"

    def reparte_el(self, dia):
        """dia: date.weekday(). Domingo no hay reparto."""
        self.ensure_one()
        if dia >= len(DIAS):
            return False
        return bool(self[DIAS[dia][0]])

    @api.model
    def zona_en_texto(self, texto):
        """La zona cuya ciudad aparece en un domicilio escrito a mano.

        Es lo que tiene un pedido sin cliente asignado: "Av. Argentina 120,
        Plottier". Si aparecen dos ciudades gana la ultima, que suele ser la
        localidad ("Calle Neuquén 40, Plottier" es Plottier).
        """
        clave = " %s " % " ".join(
            "".join(c if c.isalnum() else " " for c in normalizar_ciudad(texto)).split()
        )
        if not clave.strip():
            return self.browse()
        mejor, donde = self.browse(), -1
        for zona in self.search([]):
            nombre = normalizar_ciudad(zona.name)
            if not nombre:
                continue
            posicion = clave.rfind(" %s " % nombre)
            if posicion > donde:
                mejor, donde = zona, posicion
        return mejor

    @api.model
    def zona_de_ciudad(self, ciudad):
        clave = normalizar_ciudad(ciudad)
        if not clave:
            return self.browse()
        for zona in self.search([]):
            if normalizar_ciudad(zona.name) == clave:
                return zona
        return self.browse()


class SaleOrder(models.Model):
    _inherit = "sale.order"

    distri_armado = fields.Selection(
        ESTADOS_ARMADO, string="Armado", default="pendiente", copy=False,
        readonly=True, index=True,
    )
    distri_armador_id = fields.Many2one(
        "res.users", string="Armador", copy=False, readonly=True,
    )
    distri_armado_inicio = fields.Datetime(string="Tomado", copy=False, readonly=True)
    distri_armado_fin = fields.Datetime(string="Armado terminado", copy=False, readonly=True)
    # Para el repartidor y el control de carga en la camioneta. Lo anota el
    # armador al terminar; las etiquetas quedan para despues.
    distri_bultos = fields.Integer(string="Bultos", copy=False, readonly=True)
    distri_zona_id = fields.Many2one(
        "distri.zona", string="Zona de reparto", compute="_compute_distri_zona",
    )

    # Para que el administrador encuentre rapido lo que el armador ya tomo y
    # todavia no tiene cliente: sin cliente no se puede facturar.
    distri_falta_cliente = fields.Selection(
        [("falta", "Falta asignar cliente")], string="Cliente",
        compute="_compute_distri_falta_cliente", store=True,
    )

    @api.depends("partner_shipping_id.city", "partner_id.city", "partner_id.distri_generic_partner",
                 "partner_id.name", "distri_domicilio")
    def _compute_distri_zona(self):
        Zona = self.env["distri.zona"].sudo()
        for pedido in self:
            zona = Zona.browse()
            if not pedido._distri_sin_cliente():
                ciudad = pedido.partner_shipping_id.city or pedido.partner_id.city
                zona = Zona.zona_de_ciudad(ciudad)
            # sin cliente asignado, o un cliente sin ciudad cargada: la que
            # escribio el cliente en el domicilio del pedido
            zona = zona or Zona.zona_en_texto(pedido.distri_domicilio)
            if not zona and not pedido._distri_sin_cliente():
                # la letra del nombre del contacto («Z - ...» es Zapala; sin
                # letra, Neuquén capital), la misma regla que usa la ruta
                zona = Zona.zona_de_ciudad(town_from_name(pedido.partner_id.name))
            pedido.distri_zona_id = zona

    @api.depends("partner_id.distri_generic_partner", "distri_armado")
    def _compute_distri_falta_cliente(self):
        for pedido in self:
            pedido.distri_falta_cliente = (
                "falta" if pedido.distri_armado != "pendiente" and pedido._distri_sin_cliente()
                else False
            )

    # ------------------------------------------------------------ reglas

    def _distri_aprobado(self):
        """Aprobado = el administrador le asigno el cliente y lo confirmo.

        Es el ultimo paso del circuito: cliente + lista + aprobar, y aprobar en
        Odoo es "Confirmar", que convierte la cotizacion en pedido de venta.
        """
        self.ensure_one()
        return self.state == "sale" and not self._distri_sin_cliente()

    def _distri_sin_cliente(self):
        """Sigue a nombre de un socio generico "Pedidos web"."""
        self.ensure_one()
        return bool(self.partner_id.sudo().distri_generic_partner)

    def action_distri_abrir_armado(self):
        """Boton "Armar este pedido": abre la pantalla del armador en el pedido."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/armado/pedido/%s" % self.id,
            "target": "self",
        }

    def _distri_armable(self):
        """Lo que puede ver y armar el armador.

        Los aprobados, y tambien los pedidos de la pagina que el
        administrador todavia no aprobo (decision 7): un administrador que
        llega tarde no demora el armado. La factura espera a que tengan
        cliente. Una cotizacion cargada a mano en el backend no entra.
        19.0.1.96.0 (audit F1): the anonymous ones arrive held
        (models/revisar.py, distri_retenido_sin_cliente) and enter only
        once the customer is assigned and the order released.
        """
        self.ensure_one()
        if self.state == "sale":
            return True
        return self.state in ("draft", "sent") and (
            self._distri_sin_cliente() or bool(self.distri_original)
        )

    def _distri_nombre_cliente(self):
        """El cliente para el armador: el asignado o el que escribio en la pagina."""
        self.ensure_one()
        if not self._distri_sin_cliente():
            return self.partner_id.name
        return self.distri_nombre or "Sin nombre"

    def _distri_sin_entregar(self):
        """Ni entregado ni facturado: lo de antes del armado se puede tocar."""
        self.ensure_one()
        pedido = self.sudo()
        if pedido.invoice_ids.filtered(lambda f: f.state != "cancel"):
            return False
        return not pedido.picking_ids.filtered(lambda p: p.state == "done")

    def _distri_editable(self):
        """El cliente y el vendedor lo tocan hasta que el armador lo toma.

        Reemplaza el corte fijo de las 08:00 (decision 1 del diseño de
        armado). Un pedido aprobado sigue siendo editable y no se vuelve a
        aprobar (decision 6).
        """
        self.ensure_one()
        return (
            self.state in ("draft", "sent", "sale")
            and self.distri_armado == "pendiente"
            and not self.locked
            and self._distri_sin_entregar()
        )

    def _distri_para_armar(self):
        self.ensure_one()
        if not self._distri_armable():
            return False
        if self.distri_armado == "pendiente":
            return self._distri_sin_entregar()
        return True

    def _distri_bloquear(self, esperar=True):
        """Bloquea la fila del pedido hasta el fin de la transaccion.

        Tomar el pedido y guardar un cambio del cliente pasan los dos por aca,
        asi que nunca corren a la vez. Despues del bloqueo se descarta lo que
        el ORM tenia en memoria y se vuelve a leer el estado.

        Con esperar=False, si otro lo tiene bloqueado devuelve False en vez
        de esperar. Si el otro ya termino y cambio la fila, Postgres corta con
        un error de serializacion y Odoo reintenta el pedido entero: en el
        reintento se ve el estado nuevo.
        """
        self.ensure_one()
        consulta = "SELECT id FROM sale_order WHERE id = %s FOR UPDATE"
        if not esperar:
            consulta += " NOWAIT"
            try:
                with self.env.cr.savepoint(flush=False):
                    self.env.cr.execute(consulta, (self.id,))
            except pg_errors.LockNotAvailable:
                return False
        else:
            self.env.cr.execute(consulta, (self.id,))
        self.invalidate_recordset()
        self.order_line.invalidate_recordset()
        return True

    # --------------------------------------------- cambios del cliente

    def _distri_reemplazar_renglones(self, valores):
        """Deja el pedido con los renglones que mando el cliente.

        En borrador se borran y se vuelven a crear, como siempre. Un pedido
        confirmado no deja borrar renglones (Odoo necesita seguir lo que se
        entrega y se factura): se corrige la cantidad del renglon que ya
        existe, los nuevos se agregan y los que el cliente saco quedan en
        cero. La entrega del deposito se ajusta sola.
        """
        self.ensure_one()
        if self.state != "sale":
            self.order_line.unlink()
            self.write({"order_line": [(0, 0, v) for v in valores]})
            return

        existentes = {}
        for linea in self.order_line.filtered(lambda l: not l.display_type).sorted("id"):
            existentes.setdefault(linea.product_id.id, []).append(linea)
        nuevos = []
        for v in valores:
            cola = existentes.get(v["product_id"])
            linea = cola.pop(0) if cola else None
            if linea and linea.product_uom_id.id == v["product_uom_id"]:
                cambios = {}
                if linea.product_uom_qty != v["product_uom_qty"]:
                    cambios["product_uom_qty"] = v["product_uom_qty"]
                if "price_unit" in v and linea.price_unit != v["price_unit"]:
                    cambios["price_unit"] = v["price_unit"]
                if cambios:
                    linea.write(cambios)
            else:
                if linea:
                    linea.product_uom_qty = 0
                nuevos.append((0, 0, v))
        for cola in existentes.values():
            for linea in cola:
                if linea.product_uom_qty:
                    linea.product_uom_qty = 0
        if nuevos:
            self.write({"order_line": nuevos})

    # -------------------------------------------------------- el armador

    def distri_tomar(self):
        """El armador toma el pedido. Devuelve un texto de error o ""."""
        self.ensure_one()
        if not self._distri_bloquear(esperar=False):
            return "El pedido se está guardando en este momento. Probá de nuevo."
        if self.distri_armado == "en_armado" and self.distri_armador_id == self.env.user:
            # doble toque en "Tomar pedido": ya es suyo
            return ""
        if self.distri_armado != "pendiente":
            quien = self.distri_armador_id.name or "otro armador"
            return "Este pedido ya lo tomó %s." % quien
        if not self._distri_para_armar():
            return "Este pedido no se puede armar."
        self.write({
            "distri_armado": "en_armado",
            "distri_armador_id": self.env.user.id,
            "distri_armado_inicio": fields.Datetime.now(),
        })
        # desde aca la pantalla trabaja con sudo(): lo que siga lo firma el armador
        self.message_post(body="%s tomó el pedido para armarlo." % self.env.user.name)
        return ""

    def distri_dejar_para_mas_tarde(self):
        """El armador lo suelta: vuelve a pendiente. Texto de error o "".

        Lo puede soltar el que lo tomo o administracion (un pedido que quedo
        trabado). Queda libre para cualquier armador y el cliente lo puede
        volver a cambiar (_distri_editable). Los faltantes que se marcaron se
        deshacen: el que lo tome despues vuelve a mirar el deposito. Lo
        agregado por telefono queda, porque lo pidio el cliente.
        """
        self.ensure_one()
        self._distri_bloquear()
        if self.state == "cancel":
            return "Este pedido se canceló."
        if self.distri_armado == "pendiente":
            # doble toque en "Dejar para más tarde": el primero ya lo solto
            return ""
        if self.distri_armado != "en_armado":
            return "Este pedido ya está armado."
        if not (self.distri_armador_id == self.env.user
                or self.env.user.has_group("distri_pedido.grupo_administracion")):
            return ("Este pedido lo está armando %s: solo esa persona o administración "
                    "lo pueden dejar para más tarde." % (self.distri_armador_id.name or "otro armador"))
        con_faltante = self.order_line.filtered(
            lambda l: not l.display_type and l.distri_faltante > 0
        )
        # 19.0.1.94.0: the «Listo» ticks go too, for the same reason
        self.order_line.filtered("distri_listo").write({"distri_listo": False})
        for linea in con_faltante:
            # lo mismo que "No falta nada" en distri_marcar
            linea.with_context(distri_armado=True).write({
                "product_uom_qty": linea.distri_pedida,
                "distri_faltante": 0.0,
            })
        self.write({
            "distri_armado": "pendiente",
            "distri_armador_id": False,
            "distri_armado_inicio": False,
        })
        nota = "%s dejó el pedido para más tarde; vuelve a pendiente." % self.env.user.name
        if con_faltante:
            nota += (" Se deshizo 1 faltante." if len(con_faltante) == 1
                     else " Se deshicieron %s faltantes." % len(con_faltante))
        self.message_post(body=nota)
        return ""

    def action_distri_dejar_para_mas_tarde(self):
        """Boton del backend: administracion suelta un pedido trabado."""
        self.ensure_one()
        error = self.distri_dejar_para_mas_tarde()
        if error:
            raise UserError(error)
        return True

    def _distri_exigir_en_armado(self):
        if self.state == "cancel":
            raise UserError("Este pedido se canceló.")
        if self.distri_armado != "en_armado":
            raise UserError("El pedido no está en armado.")

    def distri_marcar(self, linea, faltante):
        """Falta N, o deshacer con faltante 0. Devuelve la linea.

        El armador solo marca lo que falta (decision 2): el renglon que no
        toca se entrega completo. Como hace hoy el deposito, el faltante se
        descuenta de la cantidad del renglon: se factura lo entregado y el
        cliente ve el faltante al comparar con su pedido original. El
        faltante queda anotado aparte para la planilla del dia.
        """
        self.ensure_one()
        self._distri_bloquear()
        self._distri_exigir_en_armado()
        if linea.order_id != self or linea.display_type:
            raise UserError("Ese renglón no es de este pedido.")
        pedida = linea.distri_pedida
        faltante = float(faltante or 0)
        if not math.isfinite(faltante):
            raise UserError("Poné cuántas unidades faltan.")
        faltante = min(max(faltante, 0.0), pedida)
        cambios = {}
        if abs(linea.distri_faltante - faltante) > 1e-6:
            cambios["distri_faltante"] = faltante
        if abs(linea.product_uom_qty - (pedida - faltante)) > 1e-6:
            cambios["product_uom_qty"] = pedida - faltante
        # 19.0.1.94.0: «No falta nada» on a line that had a shortage means the
        # packer checked it whole: it counts as «Listo»
        if not faltante and linea.distri_faltante and not linea.distri_listo:
            cambios["distri_listo"] = True
        # sin cambios no se escribe: tocar la cantidad recalcula la entrega
        if cambios:
            linea.with_context(distri_armado=True).write(cambios)
        return linea

    def distri_marcar_listo(self, linea, listo=True):
        """«Listo, N» (19.0.1.94.0): the packer put the whole line in the box.

        Only a tick for the screen (progress, next item): it changes no
        quantity, and a line left without a tick is still delivered whole
        (decision 2). Undo with listo=False. Returns the line.
        """
        self.ensure_one()
        self._distri_bloquear()
        self._distri_exigir_en_armado()
        if linea.order_id != self or linea.display_type:
            raise UserError("Ese renglón no es de este pedido.")
        listo = bool(listo)
        if linea.distri_listo != listo:
            linea.with_context(distri_armado=True).distri_listo = listo
        return linea

    def distri_agregar(self, producto, cantidad, unidad, clave=""):
        """Un producto que el cliente pidio por telefono.

        Sin precio en los valores: Odoo lo calcula con la lista del pedido,
        que es la del cliente. El armador no lo puede cambiar. Devuelve el
        renglon nuevo.

        La clave la manda la pantalla con cada "Agregar": si el celular
        pierde la señal despues de guardar y el armador vuelve a tocar, llega
        la misma clave y se devuelve el renglon ya creado en vez de otro.
        """
        self.ensure_one()
        self._distri_bloquear()
        self._distri_exigir_en_armado()
        if not math.isfinite(cantidad) or cantidad <= 0:
            raise UserError("La cantidad tiene que ser mayor que cero.")
        # 19.0.1.93.0 (E2E-C C10): a product sold by units takes whole units
        # (like /pedido); a weight or a length may still be fractional
        if _por_unidades(self.env, unidad) and not float(cantidad).is_integer():
            raise UserError("Este producto se vende por unidad: poné una cantidad entera.")
        if clave:
            ya = self.order_line.filtered(lambda l: l.distri_clave == clave)
            if ya:
                return ya[:1]
        antes = self.order_line
        self.with_context(distri_armado=True).write({"order_line": [(0, 0, {
            "product_id": producto.id,
            "product_uom_qty": cantidad,
            "product_uom_id": unidad.id,
            "distri_por_telefono": True,
            "distri_clave": clave or False,
        })]})
        return self.order_line - antes

    def _distri_renglones_a_armar(self):
        """Los renglones que el armador tiene que mirar.

        Los que el cliente saco de un pedido confirmado quedan en cero (Odoo no
        deja borrarlos) y no se muestran.
        """
        return self.order_line.filtered(
            lambda l: not l.display_type and (l.product_uom_qty or l.distri_faltante)
        )

    def distri_terminar(self, bultos):
        """Devuelve un texto de error o "". Exige los bultos (1 o más).

        Lo que el armador no marco como faltante se entrega completo.
        """
        self.ensure_one()
        self._distri_bloquear()
        if (self.distri_armado == "armado" and self.state != "cancel"
                and self.distri_armador_id == self.env.user):
            # doble toque en "Terminar armado": el primero ya lo termino
            return ""
        self._distri_exigir_en_armado()
        if not bultos or bultos < 1 or bultos > MAX_BULTOS:
            return "Poné en cuántos bultos quedó el pedido."
        self.write({
            "distri_armado": "armado",
            "distri_armado_fin": fields.Datetime.now(),
            "distri_bultos": bultos,
        })
        self.message_post(body="%s terminó de armar el pedido en %s %s." % (
            self.env.user.name, bultos, "bulto" if bultos == 1 else "bultos"))
        # receipt code for the delivery app (models/reparto_cliente.py)
        self._distri_generate_receipt_code()
        return ""

    def distri_corregir_bultos(self, bultos):
        """Corrige los bultos de un pedido ya armado. Texto de error o ""."""
        self.ensure_one()
        self._distri_bloquear()
        if self.distri_armado != "armado":
            return "El pedido no está armado."
        if not bultos or bultos < 1 or bultos > MAX_BULTOS:
            return "Poné en cuántos bultos quedó el pedido."
        if bultos != self.distri_bultos:
            anterior = self.distri_bultos
            self.distri_bultos = bultos
            self.message_post(body="%s corrigió los bultos: %s → %s." % (
                self.env.user.name, anterior, bultos))
        return ""


class SaleOrderLine(models.Model):
    _inherit = "sale.order.line"

    distri_faltante = fields.Float(
        string="Faltante", copy=False, digits="Product Unit",
        help="Lo que el armador anotó como faltante. Ya está descontado de la cantidad.",
    )
    distri_pedida = fields.Float(
        string="Pedida", compute="_compute_distri_pedida", digits="Product Unit",
        help="Cantidad antes del armado: la entregada más el faltante.",
    )
    distri_por_telefono = fields.Boolean(string="Agregado por teléfono", copy=False)
    # la clave del "Agregar" que creo el renglon: un reintento no lo duplica
    distri_clave = fields.Char(copy=False, readonly=True)
    # 19.0.1.94.0: «Listo, N» in /armado (sale.order.distri_marcar_listo)
    distri_listo = fields.Boolean(string="Revisado en el armado", copy=False, readonly=True)

    @api.depends("product_uom_qty", "distri_faltante")
    def _compute_distri_pedida(self):
        for linea in self:
            linea.distri_pedida = linea.product_uom_qty + linea.distri_faltante

    # Un vendedor que edita en el backend sigue la misma regla que el
    # cliente: hasta que el armador toma el pedido. Administracion y gerencia
    # pueden corregir siempre (descuentan faltantes, corrigen la factura).

    def _distri_revisar_backend(self, pedidos):
        if self.env.su or self.env.context.get("distri_armado"):
            return
        if self.env.user.has_group("distri_pedido.grupo_administracion"):
            return
        # un pedido sin aprobar tambien se arma (decision 7): desde que lo
        # toman queda bloqueado igual que uno aprobado
        for pedido in pedidos.filtered(
            lambda p: p.state == "sale" or p.distri_armado != "pendiente"
        ):
            pedido._distri_bloquear()
            if pedido.distri_armado != "pendiente":
                raise UserError(YA_SE_ESTA_ARMANDO)
            if pedido.state == "sale" and not pedido.distri_modificado:
                pedido.sudo().distri_modificado = True

    @api.model_create_multi
    def create(self, vals_list):
        pedidos = self.env["sale.order"].browse(
            {v["order_id"] for v in vals_list if v.get("order_id")}
        )
        self._distri_revisar_backend(pedidos)
        return super().create(vals_list)

    def write(self, vals):
        if any(campo in vals for campo in CAMPOS_DEL_PEDIDO):
            self._distri_revisar_backend(self.order_id)
        return super().write(vals)

    def unlink(self):
        self._distri_revisar_backend(self.order_id)
        return super().unlink()
