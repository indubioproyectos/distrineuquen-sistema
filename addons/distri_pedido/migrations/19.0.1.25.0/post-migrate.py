"""«Comprobantes pendientes» (see pre-migrate.py): the amount transferred of
the old visits, the date an old overdue promise expired, and the /proyecto
card of «Transferencias prometidas» reworded (only if nobody edited it)."""
import logging

_logger = logging.getLogger(__name__)

OLD_CARD = ("Cuando el cliente dice que transfiere, aparece una alerta en Odoo y el "
            "administrador le recuerda con el mensaje ya escrito. La promesa pasa sola por "
            "«Sin comprobante», «Vencidas» y «Cobranza (48 h)», y se cumple al confirmar el "
            "cobro.")
NEW_CARD = ("El repartidor reclama el comprobante de cada transferencia en la entrega y lo "
            "reenvía a la empresa. Vendedor, administración y gerencia ven los comprobantes "
            "pendientes y los reclaman por WhatsApp. El que no llega para la rendición del día "
            "siguiente vence y se carga a la cuenta del repartidor, salvo que el reporte de "
            "Mercado Pago traiga la transferencia.")


def migrate(cr, version):
    cr.execute("""
        UPDATE distri_route_stop s SET transfer_amount = p.amount
          FROM distri_promised_transfer p
         WHERE p.stop_id = s.id AND s.transfer = 'yes'
           AND COALESCE(s.transfer_amount, 0) = 0""")
    cr.execute("""
        UPDATE distri_promised_transfer
           SET overdue_since = COALESCE(state_changed_at, write_date)::date
         WHERE state = 'overdue' AND overdue_since IS NULL""")
    cr.execute("""
        UPDATE distri_project_module
           SET name = 'Comprobantes pendientes', description = %s,
               presentation_url = NULL
         WHERE code = 'prometidas' AND description = %s""", (NEW_CARD, OLD_CARD))
    _logger.info("Comprobantes pendientes: project card updated: %s", cr.rowcount)
