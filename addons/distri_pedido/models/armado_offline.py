"""/armado without signal (19.0.1.98.0, audit F6).

The «Tradicional» view saves every tick, shortage and the finish on the
phone first (static/src/armado/armado.js, localStorage) and sends them in
order, as events with a UUID generated on the phone, to
/armado/pedido/<id>/sync (the same idea as the driver app's outbox,
models/reparto_sync.py). Every event is applied once: its UUID is kept here
and a replay (the answer was lost, the phone sent it again) answers
«duplicate» without touching the order, so an old tick never undoes a newer
one. The values are absolute (ticked or not, how many there are), so the
same event applied twice would give the same order anyway.
"""
import logging
import math
import re

from psycopg2 import IntegrityError

from odoo import api, fields, models
from odoo.exceptions import UserError

from .armado import MAX_BULTOS

_logger = logging.getLogger(__name__)

TIPOS = [("listo", "Tilde"), ("hay", "Cuántas hay"), ("terminar", "Terminar")]
UUID = re.compile(r"^[0-9a-zA-Z-]{8,64}$")
# events kept for this many days: a phone offline longer than that is not
# a real case, and a replay would still be harmless (absolute values)
DIAS_EVENTOS = 30
MAX_EVENTOS = 300


class DistriArmadoEvento(models.Model):
    _name = "distri.armado.evento"
    _description = "Evento del armado guardado en el celular"
    _order = "id desc"

    uuid = fields.Char(required=True, readonly=True)
    pedido_id = fields.Many2one("sale.order", required=True, readonly=True, ondelete="cascade",
                                index=True)
    linea_id = fields.Many2one("sale.order.line", readonly=True, ondelete="set null")
    tipo = fields.Selection(TIPOS, required=True, readonly=True)
    valor = fields.Float(readonly=True)
    user_id = fields.Many2one("res.users", readonly=True, default=lambda self: self.env.user)
    capturado = fields.Char(string="En el celular", readonly=True)

    _uuid_unico = models.Constraint("unique(uuid)", "Ese evento ya se aplicó.")

    @api.autovacuum
    def _gc_eventos_viejos(self):
        limite = fields.Datetime.subtract(fields.Datetime.now(), days=DIAS_EVENTOS)
        self.sudo().search([("create_date", "<", limite)]).unlink()


class SaleOrder(models.Model):
    _inherit = "sale.order"

    def distri_sync_eventos(self, eventos, puede_tocar):
        """Apply the phone's events in order. Returns one result per event:
        {uuid, status: ok | duplicate | error, error, retry}.

        `puede_tocar(pedido)` is the controller's check (who may pack it),
        asked again before every event because a finish changes it."""
        self.ensure_one()
        Evento = self.env["distri.armado.evento"].sudo()
        resultados = []
        for ev in (eventos or [])[:MAX_EVENTOS]:
            ev = ev if isinstance(ev, dict) else {}
            uuid = str(ev.get("uuid") or "")
            if not UUID.match(uuid):
                resultados.append({"uuid": uuid, "status": "error", "retry": False,
                                   "error": "Evento sin identificador."})
                continue
            if Evento.search_count([("uuid", "=", uuid)]):
                resultados.append({"uuid": uuid, "status": "duplicate"})
                continue
            try:
                with self.env.cr.savepoint():
                    linea, valor = self._distri_aplicar_evento(ev, puede_tocar)
                    Evento.create({
                        "uuid": uuid, "pedido_id": self.id, "linea_id": linea.id if linea else False,
                        "tipo": ev.get("tipo"), "valor": valor,
                        "capturado": str(ev.get("at") or "")[:40],
                    })
            except IntegrityError:
                # the same event arrived twice at the same time: the other won
                resultados.append({"uuid": uuid, "status": "duplicate"})
                continue
            except UserError as exc:
                resultados.append({"uuid": uuid, "status": "error", "retry": False,
                                   "error": str(exc)})
                continue
            resultados.append({"uuid": uuid, "status": "ok"})
        return resultados

    def _distri_aplicar_evento(self, ev, puede_tocar):
        """(line, value) of an applied event; UserError when refused."""
        tipo = ev.get("tipo")
        if tipo == "terminar":
            bultos = self._distri_entero(ev.get("valor"))
            if (self.distri_armado == "armado" and self.state != "cancel"
                    and self.distri_armador_id == self.env.user):
                return None, bultos
            if not puede_tocar(self):
                raise UserError(self._distri_por_que_no_arma())
            if not 1 <= bultos <= MAX_BULTOS:
                raise UserError("Poné en cuántos bultos quedó el pedido.")
            error = self.distri_terminar(bultos)
            if error:
                raise UserError(error)
            return None, bultos
        if tipo not in ("listo", "hay"):
            raise UserError("No entendimos lo que se guardó en el celular.")
        if not puede_tocar(self):
            raise UserError(self._distri_por_que_no_arma())
        try:
            linea = self.env["sale.order.line"].sudo().browse(int(ev.get("linea") or 0)).exists()
        except (TypeError, ValueError):
            linea = None
        if not linea or linea.order_id != self:
            raise UserError("Ese renglón no es de este pedido.")
        if tipo == "listo":
            listo = bool(ev.get("valor"))
            # a tick on a short line first takes the shortage back
            if listo and linea.distri_faltante:
                self.distri_marcar(linea, 0.0)
            self.distri_marcar_listo(linea, listo)
            return linea, 1.0 if listo else 0.0
        try:
            hay = float(ev.get("valor"))
        except (TypeError, ValueError):
            hay = -1.0
        pedida = linea.distri_pedida
        if not math.isfinite(hay) or hay < 0 or hay > pedida + 1e-6:
            raise UserError("Poné cuántas hay: de 0 a %s." % ("{:g}".format(pedida)))
        self.distri_marcar(linea, pedida - hay)
        # «están todas» is a checked line; a shortage is not a tick
        self.distri_marcar_listo(linea, hay >= pedida - 1e-6)
        return linea, hay

    @staticmethod
    def _distri_entero(valor):
        try:
            return int(float(valor))
        except (TypeError, ValueError):
            return 0

    def _distri_por_que_no_arma(self):
        if self.state == "cancel":
            return "Este pedido se canceló."
        if self.distri_armado == "pendiente":
            return "El pedido volvió a pendiente: abrilo de nuevo para tomarlo."
        if self.distri_armado == "armado":
            return "Este pedido ya está armado."
        return "Este pedido lo está armando %s." % (self.distri_armador_id.name or "otro armador")
