"""Package labels are done (19.0.1.33.0): the /proyecto card of
data/proyecto.xml (noupdate) still said they were pending. Updated only if
nobody edited it in Odoo."""
from odoo import SUPERUSER_ID, api

OLD_STEP = "El gerente confirma qué sistema o impresora de etiquetas usa."
NEW = {
    "description": "Una etiqueta de 100 x 50 mm por bulto para la impresora térmica del "
                   "depósito: cliente, localidad, pedido, «Bulto 2 de 3» y código de barras. "
                   "Se imprimen al terminar el armado, desde el pedido o todas las de un viaje "
                   "desde la ruta.",
    "stage": "lab",
    "next_step": "Imprimir la etiqueta de prueba en la impresora real del depósito y "
                 "calibrarla.",
}


def migrate(cr, version):
    env = api.Environment(cr, SUPERUSER_ID, {})
    card = env.ref("distri_pedido.project_etiquetas", raise_if_not_found=False)
    if card and card.stage == "pending" and (card.next_step or "").strip() == OLD_STEP:
        card.write(NEW)
