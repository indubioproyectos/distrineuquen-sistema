#!/usr/bin/env bash
# Chat viewer (19.0.1.46.0): order inside the minute for the chats imported
# before that version. Reads each export again (the same files the imports
# came from) and sets only distri.whatsapp.message.distri_chat_minute_pos
# (models/visor_chat.py, _distri_backfill_minute_pos). Nothing else changes.
#   scripts/visor-chat-2026-10-28/ordenar-minuto.sh [base]     default: distri_lab
#   DO_COMMIT=1 ...   writes; otherwise rolls back
# The exports stay on the server: a private copy the container user (uid 100)
# reads through an ACL, mounted read only and removed at the end.
set -euo pipefail
DB=${1:-distri_lab}
INSUMOS=/srv/docs/distrineuquen-modulo/insumos
MANAGER="$INSUMOS/cruce-2026-10-24/chat_prueba/chat_gerente.zip"
OWN="$INSUMOS/actualizacion-2026-10-27/Chat de WhatsApp con Comprobantes de Transferencias - Alejandro.zip"
exec 9>/srv/odoo-lab/run/mantenimiento.lock
flock -w 3600 9
TMP=$(mktemp -d /srv/odoo-lab/run/visor-XXXXXX)
trap 'rm -rf "$TMP"' EXIT
install -m 600 "$MANAGER" "$TMP/manager.zip"
install -m 600 "$OWN" "$TMP/own.zip"
setfacl -R -m u:100:rX "$TMP"
LAB_MOUNTS="-v $TMP:/chat:ro" NAME=distri-visor-orden-$$ DO_COMMIT=${DO_COMMIT:-0} \
  /srv/odoo-lab/scripts/l10n_ar/lab.sh odoo shell -d "$DB" --no-http --log-level=warn \
  < "$(dirname "$0")/ordenar_minuto_body.py" 2>&1 | rg -N -e '^(RESULT|ABORT|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error'
