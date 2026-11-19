"""La factura sale sola al terminar el armado, por lo entregado.

Diseño: armado/README.md (decision 7 y "Marcar lo que falta") y
cobranza/README.md ("se factura al terminar el armado"). El armador ya
descontó los faltantes de la cantidad de cada renglón, así que lo que queda
en el pedido es lo que se entrega y lo que se factura.

- Con cliente asignado y el pedido confirmado, al tocar "Terminar armado":
  se valida la entrega del depósito con lo armado, se crea la factura y se
  emite.
- Sin cliente asignado, la factura espera: sale sola cuando el
  administrador le asigna el cliente y lo confirma (en cualquier orden).
- Nunca hay dos facturas: si el pedido ya tiene una (a mano o automática,
  en cualquier estado) no se factura de nuevo. El botón "Facturar lo armado"
  solo ignora las canceladas.
- Un error al facturar no frena al armador: el armado se termina igual, el
  pedido queda marcado con el motivo y una actividad para administración, y
  se reintenta desde el pedido.
"""
import logging

from psycopg2 import OperationalError

from odoo import fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

# "0" deja la factura automática en borrador para que administración la
# revise y la emita. Con la facturación electrónica de ARCA la emisión pide
# el CAE: ahí la factura también queda en borrador (ver _distri_puede_emitir).
PARAM_EMITIR = "distri_pedido.factura_auto_emitir"

RESUMEN_ACTIVIDAD = "Revisar la factura del armado"


class SaleOrder(models.Model):
    _inherit = "sale.order"

    distri_factura_auto_id = fields.Many2one(
        "account.move", string="Factura del armado", copy=False, readonly=True,
        help="La factura que se creó sola al terminar el armado.",
    )
    distri_factura_error = fields.Text(
        string="Error al facturar", copy=False, readonly=True,
        help="Por qué no salió la factura del armado. Se reintenta con "
             "«Facturar lo armado».",
    )

    # ------------------------------------------------------------ reglas

    def _distri_listo_para_facturar(self):
        """Armado terminado, con cliente real y confirmado."""
        self.ensure_one()
        return (
            self.state == "sale"
            and self.distri_armado == "armado"
            and not self._distri_sin_cliente()
        )

    def _distri_tiene_factura(self, contar_canceladas=True):
        self.ensure_one()
        facturas = self.sudo().invoice_ids
        if not contar_canceladas:
            facturas = facturas.filtered(lambda f: f.state != "cancel")
        return bool(facturas)

    def _distri_puede_emitir(self, factura):
        """Emitir la factura solo si no hace falta un servicio fiscal externo.

        En el laboratorio no hay localización argentina ni factura
        electrónica: emitir es un asiento interno. Si el diario pide el CAE a
        ARCA (l10n_ar_edi), la factura queda en borrador y la emite
        administración.
        """
        valor = self.env["ir.config_parameter"].sudo().get_param(PARAM_EMITIR, "1")
        if valor.strip() in ("0", "false", "False", ""):
            return False
        diario = factura.journal_id
        if "l10n_ar_afip_ws" in diario._fields and diario.l10n_ar_afip_ws:
            return False
        return True

    # ---------------------------------------------------------- disparos

    def distri_terminar(self, bultos):
        error = super().distri_terminar(bultos)
        if not error:
            self._distri_facturar_armado()
        return error

    def action_confirm(self):
        resultado = super().action_confirm()
        # un pedido armado sin cliente que el administrador aprueba
        self._distri_facturar_armado()
        return resultado

    def write(self, vals):
        resultado = super().write(vals)
        if "partner_id" in vals:
            # confirmado a nombre de "Pedidos web" y el cliente asignado despues
            self._distri_facturar_armado()
        return resultado

    # ----------------------------------------------------------- factura

    def _distri_facturar_armado(self):
        """Factura sola lo armado. No lanza errores: los deja en el pedido.

        Los errores de concurrencia de Postgres sí se propagan, para que Odoo
        reintente la operación entera (el reintento vuelve a pasar por acá).
        """
        for pedido in self.sudo():
            if not pedido._distri_listo_para_facturar() or pedido._distri_tiene_factura():
                continue
            try:
                with self.env.cr.savepoint():
                    pedido._distri_facturar_ahora()
            except OperationalError:
                raise
            except Exception as exc:
                _logger.warning("Factura del armado de %s: %s", pedido.name, exc,
                                exc_info=not isinstance(exc, UserError))
                pedido._distri_avisar_error_factura(exc)

    def _distri_facturar_ahora(self, contar_canceladas=True):
        """Entrega, factura y emisión. Devuelve la factura o None. Lanza errores."""
        self.ensure_one()
        pedido = self.sudo()
        pedido._distri_bloquear()
        if not pedido._distri_listo_para_facturar():
            raise UserError("El pedido tiene que estar armado, confirmado y con el cliente asignado.")
        if pedido._distri_tiene_factura(contar_canceladas):
            return None

        pedido._distri_entregar_lo_armado()

        lineas = pedido.order_line.filtered(lambda l: not l.display_type)
        if not any(l.qty_to_invoice > 1e-6 for l in lineas):
            pedido.message_post(body="No hay nada para facturar: faltó todo el pedido.")
            pedido._distri_limpiar_error_factura()
            return None

        factura = pedido._create_invoices()
        emitida = pedido._distri_puede_emitir(factura)
        if emitida:
            # the Argentine date: from 21 h the server (UTC) is already on the
            # next day, and the users have no time zone set
            if not factura.invoice_date:
                # imported here: reparto_cliente.py loads after this file
                from .reparto_cliente import today_ar
                factura.invoice_date = today_ar()
            factura.action_post()
        pedido.distri_factura_auto_id = factura
        pedido._distri_limpiar_error_factura()
        if emitida:
            texto = "Factura %s emitida sola al terminar el armado, por lo entregado." % factura.name
        else:
            texto = ("Factura del armado creada en borrador, por lo entregado. "
                     "Administración la revisa y la emite.")
        pedido.message_post(body=texto)
        return factura

    def _distri_entregar_lo_armado(self):
        """Valida la entrega del depósito con lo que se armó.

        La cantidad de cada renglón ya descuenta el faltante, así que la
        demanda de la entrega es lo armado. Sin pedidos pendientes: lo que
        falta no se entrega después.
        """
        self.ensure_one()
        entregas = self.picking_ids.filtered(lambda p: p.state not in ("done", "cancel"))
        for _vuelta in range(3):  # entregas en varios pasos: una detrás de otra
            listas = entregas.filtered(lambda p: p.state not in ("done", "cancel"))
            if not listas:
                return
            for movimiento in listas.move_ids.filtered(lambda m: m.state not in ("done", "cancel")):
                movimiento.quantity = movimiento.product_uom_qty
                movimiento.picked = True
            resultado = listas.with_context(
                skip_backorder=True,
                picking_ids_not_to_backorder=listas.ids,
                skip_sms=True,
            ).button_validate()
            if resultado is not True and isinstance(resultado, dict) and \
                    listas.filtered(lambda p: p.state not in ("done", "cancel")):
                raise UserError("La entrega del depósito pide una confirmación a mano (%s)." % (
                    resultado.get("name") or resultado.get("res_model") or "asistente"))
        if entregas.filtered(lambda p: p.state not in ("done", "cancel")):
            raise UserError("No se pudo validar la entrega del depósito.")

    # ------------------------------------------------------ avisos

    def _distri_usuario_administracion(self):
        grupo = self.env.ref("distri_pedido.grupo_administracion", raise_if_not_found=False)
        if self.user_id and grupo and grupo in self.user_id.all_group_ids:
            return self.user_id
        if grupo:
            usuario = self.env["res.users"].sudo().search([
                ("all_group_ids", "in", grupo.ids), ("share", "=", False),
                ("id", "!=", self.env.ref("base.user_root").id),
            ], order="id", limit=1)
            if usuario:
                return usuario
        return self.env.ref("base.user_admin")

    def _distri_avisar_error_factura(self, exc):
        self.ensure_one()
        motivo = str(exc.args[0]) if isinstance(exc, UserError) and exc.args else str(exc)
        motivo = motivo or exc.__class__.__name__
        self.distri_factura_error = motivo
        self.message_post(body="No salió la factura del armado: %s. El armado quedó "
                               "terminado. Se reintenta con «Facturar lo armado»." % motivo)
        ya = self.activity_ids.filtered(lambda a: a.summary == RESUMEN_ACTIVIDAD)
        if not ya:
            # 19.0.1.116.0: one to-do for each person of administración and
            # gerencia (not the technical Administrator account)
            for usuario in self._distri_avisar_factura_a():
                self.activity_schedule(
                    "mail.mail_activity_data_todo",
                    summary=RESUMEN_ACTIVIDAD,
                    note="No salió la factura del armado: %s" % motivo,
                    user_id=usuario.id,
                )

    def _distri_avisar_factura_a(self):
        """The active users of administración or gerencia who are not
        technical administrators (Settings) nor OdooBot; if there is none,
        the one _distri_usuario_administracion finds."""
        grupos = [self.env.ref(x, raise_if_not_found=False) for x in (
            "distri_pedido.grupo_administracion", "distri_pedido.grupo_gerencia")]
        ids = [g.id for g in grupos if g]
        usuarios = self.env["res.users"]
        if ids:
            sistema = self.env.ref("base.group_system")
            usuarios = self.env["res.users"].sudo().search([
                ("all_group_ids", "in", ids), ("share", "=", False),
                ("id", "!=", self.env.ref("base.user_root").id),
            ], order="id").filtered(lambda u: sistema not in u.all_group_ids)
        return usuarios or self._distri_usuario_administracion()

    def _distri_limpiar_error_factura(self):
        self.ensure_one()
        if self.distri_factura_error:
            self.distri_factura_error = False
        actividades = self.activity_ids.filtered(lambda a: a.summary == RESUMEN_ACTIVIDAD)
        if actividades:
            actividades.action_feedback(feedback="Factura del armado resuelta.")

    # ------------------------------------------------------ botón

    def action_distri_facturar_armado(self):
        """Reintento desde el pedido. Acá el error se muestra."""
        if not (self.env.su or self.env.user.has_group("distri_pedido.grupo_administracion")):
            raise UserError("Solo administración factura lo armado.")
        for pedido in self:
            if pedido._distri_tiene_factura(contar_canceladas=False):
                raise UserError("El pedido %s ya tiene factura." % pedido.name)
            try:
                pedido._distri_facturar_ahora(contar_canceladas=False)
            except UserError as exc:
                # se deshace todo y el administrador ve el motivo; la marca
                # del intento automatico queda como estaba
                raise UserError("No salió la factura de %s: %s" % (
                    pedido.name, exc.args[0] if exc.args else exc)) from exc
        return True
