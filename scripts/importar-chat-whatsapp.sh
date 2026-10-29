#!/usr/bin/env bash
# Imports a WhatsApp chat export (.zip with media, or the folder it unzips to)
# into a lab database, on the server: for exports too big for the browser
# (Cobros › Puesta al día › Importar chat de WhatsApp takes up to 100 MB).
#   scripts/importar-chat-whatsapp.sh <zip o carpeta> <desde AAAA-MM-DD> <hasta AAAA-MM-DD> [base]
#   EXPORTADO_POR=manager|admin|own (default own): whose phone exported it;
#   the chat viewer picks the source of each period with it.
# The file stays on the server: its folder is mounted read only. Idempotent
# (one message per file hash): it can run again after an interruption. Reads
# every photo with OCR, so it takes a few seconds per file.
set -euo pipefail
SRC=$(readlink -f "$1"); DESDE=$2; HASTA=$3; DB=${4:-distri_lab}
OWNER=${EXPORTADO_POR:-own}
case "$OWNER" in manager|admin|own) ;; *) echo "EXPORTADO_POR: manager, admin u own" >&2; exit 1;; esac
BASE=$(basename "$SRC")
exec 9>/srv/odoo-lab/run/mantenimiento.lock
flock -w 3600 9
# a private copy the container user (uid 100) can read through an ACL; the
# original keeps its permissions. Removed at the end.
TMP=$(mktemp -d /srv/odoo-lab/run/chat-XXXXXX)
trap 'rm -rf "$TMP"' EXIT
cp -r "$SRC" "$TMP/"
setfacl -R -m u:100:rX "$TMP"
LAB_MOUNTS="-v $TMP:/chat:ro" NAME=distri-chat-import-$$ \
  /srv/odoo-lab/scripts/l10n_ar/lab.sh odoo shell -d "$DB" --no-http --log-level=warn <<PY 2>&1 | grep -E '^(RESULT|SUMMARY)|Traceback|Error'
import json
from datetime import date
rec = env['distri.whatsapp.chat.import'].sudo()._distri_import(
    $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$BASE"),
    date.fromisoformat('$DESDE'), date.fromisoformat('$HASTA'),
    path='/chat/' + $(python3 -c 'import json,sys; print(json.dumps(sys.argv[1]))' "$BASE"),
    commit_every=20, owner='$OWNER')
env.cr.commit()
print('RESULT', json.dumps({'id': rec.id, 'mensajes': rec.message_count, 'archivos': rec.file_count,
      'repetidos': rec.duplicate_count, 'otros': rec.skipped_count, 'faltan': rec.missing_count,
      'comprobantes': rec.receipt_count, 'unidos': rec.matched_count}))
print('SUMMARY', rec.summary.replace(chr(10), ' | '))
PY
