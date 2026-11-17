"""«Asignar cliente»: the web order gets its real customer.

Web orders without a customer sit under a generic contact (res_partner.py).
Assigning the customer is a write of partner_id on the order: sale_order.py
re-prices it with the customer's list, and factura_armado.py invoices it on
its own if the packing is already finished and the order is approved.
"""
import re

from odoo import api, fields, models
from odoo.exceptions import UserError

from .res_partner import GENERIC_WHOLESALE

MAX_CANDIDATOS = 8
# Argentine numbers are written with and without +54, 9 and the area code:
# the last 8 digits identify the line.
DIGITOS_TELEFONO = 8
PALABRAS_VACIAS = {"de", "del", "la", "las", "los", "el", "y", "dni", "kiosco", "despensa",
                   "almacen", "minimarket", "maxikiosco", "drugstore", "mayorista", "calle"}
# match marks shown in the candidate list: si = same, no = different,
# parcial = some words in common, falta = nothing to compare
MARCAS = [("si", "Coincide"), ("parcial", "Parecido"), ("no", "Distinto"), ("falta", "Sin dato")]
PUNTOS = {"telefono": 6, "nombre": 3, "domicilio": 3, "ciudad": 1}
SIMBOLOS = {"si": "✓ ", "parcial": "≈ ", "no": "✗ ", "falta": ""}


# armado.py and reparto_cliente.py load after this file (models/__init__.py):
# importing them here would change the order in which the models extend
# sale.order and res.partner, so their helpers are imported when called.
def normalizar_ciudad(texto):
    from .armado import normalizar_ciudad as normalizar
    return normalizar(texto)


def strip_town_prefix(nombre):
    from .reparto_cliente import strip_town_prefix as sin_prefijo
    return sin_prefijo(nombre)


def town_from_name(nombre):
    from .reparto_cliente import town_from_name as ciudad
    return ciudad(nombre)


def _palabras(texto):
    """Normalized words of a name or address, without filler words."""
    texto = normalizar_ciudad(texto)
    texto = "".join(c if c.isalnum() else " " for c in texto)
    return [p for p in texto.split() if p not in PALABRAS_VACIAS and (len(p) >= 3 or p.isdigit())]


def _telefono_clave(texto):
    digitos = re.sub(r"\D", "", texto or "")
    return digitos[-DIGITOS_TELEFONO:] if len(digitos) >= 7 else ""


def comparar_nombre(web, nombre):
    web_p, cand_p = set(_palabras(web)), set(_palabras(strip_town_prefix(nombre)))
    if not web_p or not cand_p:
        return "falta"
    comunes = web_p & cand_p
    # one shared word ("Carolina", "Facundo") is only a resemblance; the
    # same name needs two words, or the whole name when it is a single word
    if len(comunes) >= 2 or (comunes and len(web_p) == 1 and len(cand_p) == 1):
        return "si"
    return "parcial" if comunes else "no"


def comparar_telefono(web, telefonos):
    clave = _telefono_clave(web)
    claves = {_telefono_clave(t) for t in telefonos} - {""}
    if not clave or not claves:
        return "falta"
    return "si" if clave in claves else "no"


def comparar_domicilio(web, calle):
    web_p = set(_palabras(web))
    calle_p = _palabras(calle)
    if not web_p or not calle_p:
        return "falta"
    comunes = [p for p in calle_p if p in web_p]
    if len(comunes) * 2 >= len(calle_p):
        return "si"
    return "parcial" if comunes else "no"


def comparar_ciudad(ciudad_web, ciudad_cliente):
    if not ciudad_web or not ciudad_cliente:
        return "falta"
    return "si" if normalizar_ciudad(ciudad_web) == normalizar_ciudad(ciudad_cliente) else "no"


GRUPOS_ASIGNAN = ("distri_pedido.grupo_administracion", "sales_team.group_sale_manager")


def puede_asignar(env):
    return env.su or any(env.user.has_group(g) for g in GRUPOS_ASIGNAN)


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def action_distri_asignar_cliente(self):
        self.ensure_one()
        if not puede_asignar(self.env):
            raise UserError("Solo administración asigna el cliente al pedido.")
        if not self.distri_partner_generic:
            raise UserError("Este pedido ya tiene el cliente asignado.")
        # the wizard is saved before it opens: the «Elegir este» buttons of
        # the candidate rows only work on saved rows
        asistente = self.env["distri.asignar.cliente"].with_context(
            default_order_id=self.id).create({})
        accion = asistente._reabrir()
        accion["context"] = dict(accion["context"], default_order_id=self.id)
        return accion


class DistriAsignarCliente(models.TransientModel):
    _name = "distri.asignar.cliente"
    _description = "Asignar el cliente a un pedido web"

    order_id = fields.Many2one("sale.order", string="Pedido", required=True, readonly=True,
                               ondelete="cascade")
    order_state = fields.Selection(related="order_id.state")
    web_nombre = fields.Char(related="order_id.distri_nombre", string="Nombre en la web")
    web_telefono = fields.Char(related="order_id.distri_telefono", string="Teléfono en la web")
    web_domicilio = fields.Char(related="order_id.distri_domicilio", string="Domicilio en la web")
    mayorista = fields.Boolean(compute="_compute_mayorista")

    modo = fields.Selection(
        [("existente", "Cliente existente"), ("nuevo", "Crear cliente nuevo")],
        string="Asignar a", required=True, default="existente",
    )

    # existing customer
    busqueda = fields.Char(
        string="Buscar", help="Nombre, CUIT o teléfono. Arranca con lo que escribió el comprador en la web.",
    )
    candidato_ids = fields.Many2many(
        "res.partner", string="Coincidencias", compute="_compute_candidatos",
    )
    # the same candidates with their data side by side and the match marks,
    # best match first; each row can be picked with «Elegir este»
    linea_ids = fields.One2many(
        "distri.asignar.cliente.candidato", "wizard_id", string="Candidatos", readonly=True,
    )
    web_ciudad = fields.Char(string="Ciudad en la web", compute="_compute_web_ciudad")
    # the chosen customer's card, next to what the buyer typed
    sel_telefonos = fields.Char(string="Teléfono / WhatsApp", compute="_compute_sel")
    sel_domicilio = fields.Char(string="Domicilio del cliente", compute="_compute_sel")
    sel_ciudad = fields.Char(string="Ciudad del cliente", compute="_compute_sel")
    sel_cuit = fields.Char(string="CUIT del cliente", compute="_compute_sel")
    sel_lista = fields.Char(string="Lista del cliente", compute="_compute_sel")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    sel_saldo = fields.Monetary(string="Saldo", currency_field="currency_id", compute="_compute_sel")
    sel_ultima_compra = fields.Date(string="Última compra", compute="_compute_sel")
    sel_compras = fields.Integer(string="Compras", compute="_compute_sel")
    sel_m_nombre = fields.Selection(MARCAS, compute="_compute_sel")
    sel_m_telefono = fields.Selection(MARCAS, compute="_compute_sel")
    sel_m_domicilio = fields.Selection(MARCAS, compute="_compute_sel")
    sel_m_ciudad = fields.Selection(MARCAS, compute="_compute_sel")
    partner_id = fields.Many2one(
        "res.partner", string="Cliente",
        domain="[('distri_generic_partner', '=', False), ('type', '=', 'contact')]",
    )

    # new customer
    nuevo_nombre = fields.Char(string="Nombre del comercio")
    nuevo_telefono = fields.Char(string="Teléfono")
    nuevo_domicilio = fields.Char(string="Domicilio")
    nuevo_ciudad = fields.Char(string="Ciudad")
    nuevo_cuit = fields.Char(string="CUIT")
    nuevo_lista_id = fields.Many2one("product.pricelist", string="Lista de precios")

    @api.model
    def default_get(self, fields_list):
        valores = super().default_get(fields_list)
        pedido = self.env["sale.order"].browse(valores.get("order_id"))
        if pedido.exists():
            valores.setdefault("busqueda", pedido.distri_nombre or pedido.distri_telefono or "")
            valores.setdefault("nuevo_nombre", pedido.distri_nombre or "")
            valores.setdefault("nuevo_telefono", pedido.distri_telefono or "")
            valores.setdefault("nuevo_domicilio", pedido.distri_domicilio or "")
            # the town the buyer wrote in the address, else the order's zone
            valores.setdefault("nuevo_ciudad", self.env["distri.zona"].sudo().zona_en_texto(
                pedido.distri_domicilio).name or pedido.distri_zona_id.name or "")
            if "partner_id" in fields_list and not valores.get("partner_id"):
                candidatos = self.env["res.partner"]._distri_buscar_clientes(valores["busqueda"])
                if len(candidatos) == 1:
                    valores["partner_id"] = candidatos.id
            if pedido.partner_id.distri_generic_kind == GENERIC_WHOLESALE:
                # a wholesale web order keeps the wholesale list
                valores.setdefault("nuevo_lista_id", pedido.pricelist_id.id)
        return valores

    @api.depends("order_id")
    def _compute_mayorista(self):
        for asistente in self:
            asistente.mayorista = asistente.order_id.partner_id.distri_generic_kind == GENERIC_WHOLESALE

    @api.depends("order_id")
    def _compute_web_ciudad(self):
        Zona = self.env["distri.zona"].sudo()
        for asistente in self:
            asistente.web_ciudad = Zona.zona_en_texto(asistente.web_domicilio).name or False

    @api.depends("busqueda", "order_id")
    def _compute_candidatos(self):
        Partner = self.env["res.partner"]
        for asistente in self:
            asistente.candidato_ids = Partner._distri_candidatos_pedido(
                asistente.busqueda, asistente.web_nombre, asistente.web_telefono)

    @api.model_create_multi
    def create(self, vals_list):
        asistentes = super().create(vals_list)
        asistentes._cargar_candidatos()
        return asistentes

    def _cargar_candidatos(self):
        """The candidate rows, best match first."""
        for asistente in self:
            filas = [asistente._fila_candidato(socio) for socio in asistente.candidato_ids]
            filas.sort(key=lambda f: (-f["puntaje"], f["nombre"]))
            asistente.linea_ids = [fields.Command.clear()] + [
                fields.Command.create(dict(fila, sequence=orden)) for orden, fila in enumerate(filas)]

    def action_buscar(self):
        self.ensure_one()
        self._cargar_candidatos()
        return self._reabrir()

    @api.depends("partner_id", "order_id")
    def _compute_sel(self):
        vacio = {"telefonos": False, "domicilio": False, "ciudad": False, "cuit": False,
                 "lista": False, "saldo": 0.0, "ultima_compra": False, "compras": 0,
                 "m_nombre": False, "m_telefono": False, "m_domicilio": False, "m_ciudad": False}
        for asistente in self:
            socio = asistente.partner_id
            fila = asistente._fila_candidato(socio) if socio else vacio
            for campo in vacio:
                asistente["sel_" + campo] = fila[campo] or vacio[campo]

    def _fila_candidato(self, socio):
        """What the list shows for one candidate, and how well it matches."""
        telefonos = [t for t in (socio.phone, socio.distri_whatsapp) if t]
        ciudad = socio.city or town_from_name(socio.name)
        marcas = {
            "telefono": comparar_telefono(self.web_telefono, telefonos),
            "nombre": comparar_nombre(self.web_nombre, socio.name),
            "domicilio": comparar_domicilio(self.web_domicilio, socio.street),
            "ciudad": comparar_ciudad(self.web_ciudad, ciudad),
        }
        puntaje = sum(PUNTOS[campo] * (1 if marca == "si" else 0.5 if marca == "parcial" else 0)
                      for campo, marca in marcas.items())
        pedidos = self.env["sale.order"].sudo().search(
            [("partner_id", "child_of", socio.commercial_partner_id.id),
             ("state", "=", "sale")], order="date_order desc")
        historia = self.env["distri.cuenta.movimiento"].sudo().with_context(active_test=True).search(
            [("partner_id", "=", socio.commercial_partner_id.id), ("tipo", "=", "factura")],
            order="fecha desc")
        fechas = [f for f in (pedidos[:1].date_order and pedidos[:1].date_order.date(),
                              historia[:1].fecha) if f]

        def marcado(campo, texto):
            return (SIMBOLOS[marcas[campo]] + texto) if texto else ""

        return {
            "partner_id": socio.id,
            "puntaje": int(puntaje * 2),
            "nombre": marcado("nombre", socio.name),
            "telefonos": marcado("telefono", " / ".join(telefonos)),
            "domicilio": marcado("domicilio", socio.street or ""),
            "ciudad": marcado("ciudad", ciudad),
            "cuit": socio.vat or "",
            "lista": socio.property_product_pricelist.name or "",
            "saldo": socio._distri_account_balance(),
            "ultima_compra": max(fechas) if fechas else False,
            "compras": len(pedidos) + len(historia),
            **{"m_%s" % campo: marca for campo, marca in marcas.items()},
        }

    def _reabrir(self):
        return {
            "type": "ir.actions.act_window",
            "name": "Asignar cliente",
            "res_model": self._name,
            "res_id": self.id,
            "view_mode": "form",
            "target": "new",
            "context": {"dialog_size": "extra-large"},
        }

    @api.onchange("busqueda")
    def _onchange_busqueda(self):
        # a single clear match is pre-selected; the user can change it
        if not self.partner_id and len(self.candidato_ids) == 1:
            self.partner_id = self.candidato_ids

    # ------------------------------------------------------------ actions

    def action_asignar(self):
        return self._asignar(aprobar=False)

    def action_asignar_y_aprobar(self):
        return self._asignar(aprobar=True)

    def _asignar(self, aprobar):
        self.ensure_one()
        if not puede_asignar(self.env):
            raise UserError("Solo administración asigna el cliente al pedido.")
        pedido = self.order_id
        if not pedido.distri_partner_generic:
            raise UserError("Este pedido ya tiene el cliente asignado.")
        if pedido.state == "cancel":
            raise UserError("El pedido está cancelado.")
        cliente = self._cliente()
        pedido.with_context(distri_asignar_cliente=True).write({"partner_id": cliente.id})
        pedido.message_post(body="Cliente asignado: %s." % cliente.display_name)
        if aprobar and pedido.distri_retenido_sin_cliente and (
                self.env.su or self.env.user.has_group("distri_pedido.grupo_administracion")):
            # 19.0.1.96.0 (audit F1): approving is the release of the hold
            # that waited only for the customer
            pedido.action_distri_liberar()
        if aprobar and pedido.state in ("draft", "sent"):
            # interactive: the customer's WhatsApp is asked when missing
            resultado = pedido.with_context(distri_wa_ask=True).action_confirm()
            if isinstance(resultado, dict):
                return resultado
        return {"type": "ir.actions.act_window_close"}

    def _cliente(self):
        if self.modo == "existente":
            if not self.partner_id:
                raise UserError("Elegí el cliente, o tocá «Crear cliente nuevo».")
            if self.partner_id.distri_generic_partner:
                raise UserError("Ese es el contacto genérico de los pedidos web. Elegí el cliente real.")
            return self.partner_id
        nombre = (self.nuevo_nombre or "").strip()
        if not nombre:
            raise UserError("Escribí el nombre del cliente nuevo.")
        valores = {
            "name": nombre,
            "company_type": "company",
            "phone": (self.nuevo_telefono or "").strip() or False,
            "street": (self.nuevo_domicilio or "").strip() or False,
            "city": (self.nuevo_ciudad or "").strip() or False,
            "vat": (self.nuevo_cuit or "").strip() or False,
            "customer_rank": 1,
        }
        if self.nuevo_lista_id:
            valores["property_product_pricelist"] = self.nuevo_lista_id.id
        return self.env["res.partner"].create(valores)


class DistriAsignarClienteCandidato(models.TransientModel):
    _name = "distri.asignar.cliente.candidato"
    _description = "Candidato para asignar a un pedido web"
    _order = "sequence, id"

    wizard_id = fields.Many2one("distri.asignar.cliente", required=True, ondelete="cascade")
    sequence = fields.Integer()
    partner_id = fields.Many2one("res.partner", string="Cliente", required=True, ondelete="cascade")
    elegido = fields.Boolean(compute="_compute_elegido")
    puntaje = fields.Integer(string="Coincidencias")
    nombre = fields.Char(string="Nombre")
    telefonos = fields.Char(string="Teléfono / WhatsApp")
    domicilio = fields.Char(string="Domicilio")
    ciudad = fields.Char(string="Ciudad")
    cuit = fields.Char(string="CUIT")
    lista = fields.Char(string="Lista")
    currency_id = fields.Many2one("res.currency", default=lambda self: self.env.company.currency_id)
    saldo = fields.Monetary(string="Saldo", currency_field="currency_id")
    ultima_compra = fields.Date(string="Última compra")
    compras = fields.Integer(string="Cantidad de compras")
    compras_texto = fields.Char(string="Compras", compute="_compute_compras_texto")
    m_nombre = fields.Selection(MARCAS, string="Nombre ≈")
    m_telefono = fields.Selection(MARCAS, string="Teléfono ≈")
    m_domicilio = fields.Selection(MARCAS, string="Domicilio ≈")
    m_ciudad = fields.Selection(MARCAS, string="Ciudad ≈")

    @api.depends("compras", "ultima_compra")
    def _compute_compras_texto(self):
        for linea in self:
            if not linea.compras:
                linea.compras_texto = "Sin compras"
                continue
            linea.compras_texto = "%s · última %s" % (
                linea.compras,
                linea.ultima_compra.strftime("%d/%m/%y") if linea.ultima_compra else "-")

    @api.depends("wizard_id.partner_id", "partner_id")
    def _compute_elegido(self):
        for linea in self:
            linea.elegido = linea.partner_id == linea.wizard_id.partner_id

    def action_elegir(self):
        self.ensure_one()
        self.wizard_id.write({"modo": "existente", "partner_id": self.partner_id.id})
        return self.wizard_id._reabrir()

    def action_ver_ficha(self):
        """The full contact in a new tab: the wizard stays open."""
        self.ensure_one()
        return {
            "type": "ir.actions.act_url",
            "url": "/odoo/res.partner/%s" % self.partner_id.id,
            "target": "new",
        }


class ResPartner(models.Model):
    _inherit = "res.partner"

    @api.model
    def _distri_candidatos_pedido(self, busqueda, nombre_web, telefono_web):
        """Customers that may be the buyer of a web order.

        What the search box finds, plus the customers with the phone the
        buyer typed and the ones whose name shares a word with the name the
        buyer typed: a shop name spelled differently still shows up.
        """
        base = [("distri_generic_partner", "=", False), ("type", "=", "contact")]
        encontrados = self._distri_buscar_clientes(busqueda)
        clave = _telefono_clave(telefono_web)
        if clave:
            telefono = ("phone_mobile_search", "ilike", clave) \
                if "phone_mobile_search" in self._fields else ("phone_sanitized", "=like", "%" + clave)
            encontrados |= self.search(base + ["|", telefono, ("distri_whatsapp", "like", clave)],
                                       limit=MAX_CANDIDATOS)
        palabras = sorted(set(_palabras(strip_town_prefix(nombre_web))), key=len, reverse=True)[:3]
        for palabra in palabras:
            if len(palabra) >= 4 and len(encontrados) < MAX_CANDIDATOS * 2:
                encontrados |= self.search(base + [("name", "ilike", palabra)],
                                           limit=MAX_CANDIDATOS, order="customer_rank desc, name")
        return encontrados

    @api.model
    def _distri_buscar_clientes(self, texto, limit=8):
        """Customers matching a name, CUIT or phone typed by the buyer."""
        texto = (texto or "").strip()
        if len(texto) < 3:
            return self.browse()
        dominio = [("display_name", "ilike", texto)]
        digitos = re.sub(r"\D", "", texto)
        if len(digitos) >= 6:
            dominio = ["|", "|", ("vat", "ilike", digitos), ("phone_mobile_search", "ilike", digitos)] + dominio \
                if "phone_mobile_search" in self._fields else ["|", ("vat", "ilike", digitos)] + dominio
        return self.search(
            [("distri_generic_partner", "=", False), ("type", "=", "contact")] + dominio,
            limit=limit, order="customer_rank desc, name",
        )
