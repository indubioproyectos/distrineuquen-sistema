"""Chat viewer (19.0.1.46.0, models/visor_chat.py).

Whose phone exported each chat already imported, by its file name: the
manager's export was imported as «chat_gerente.zip» (the same file as
insumos/whatsapp-gerente-2026-10-24, checked by its hash on 28/10/2026) and
the user's own as «... - Alejandro.zip». Any other import stays without an
owner: the viewer then shows only its photos and PDF, never its texts.
The order inside each minute is set afterwards by
scripts/visor-chat-2026-10-28/ordenar-minuto.sh (it needs the exports).
"""
import logging

_logger = logging.getLogger(__name__)

OWNERS = [("manager", "chat_gerente.zip"), ("own", "% - Alejandro.zip")]


def migrate(cr, version):
    for owner, pattern in OWNERS:
        cr.execute("""UPDATE distri_whatsapp_chat_import SET distri_export_owner = %s
                       WHERE distri_export_owner IS NULL AND name LIKE %s""", (owner, pattern))
        _logger.info("Chat importado como %s: %s", owner, cr.rowcount)
    cr.execute("SELECT count(*) FROM distri_whatsapp_chat_import WHERE distri_export_owner IS NULL")
    _logger.info("Chats importados sin dueño: %s", cr.fetchone()[0])
