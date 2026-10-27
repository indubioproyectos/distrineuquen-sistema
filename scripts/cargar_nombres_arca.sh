#!/usr/bin/env bash
# Loads «Nombre en ARCA» (models/arca.py) for the Mercado Pago payers: the
# legal name of each payer CUIT, from ARCA's public padrón already downloaded
# in the private folder. Nothing is sent anywhere.
# Usage: [DO_COMMIT=1] cargar_nombres_arca.sh [DB] [ZIP]
#   DB   default distri_lab (pass a copy name to try it on a copy)
#   ZIP  default /srv/docs/distrineuquen-modulo/insumos/ARCA/apellidoNombreDenominacion.zip
# Dry run by default (rolls back); DO_COMMIT=1 saves. Idempotent.
#
# The odoo container does not mount /srv/docs, so the zip is read here on the
# host: only the lines of the CUITs that paid through Mercado Pago (a few
# hundred) go into the container, embedded in the shell script on stdin. No
# intermediate file is written. Prints only counts.
set -euo pipefail
umask 077
DB=${1:-distri_lab}
ZIP=${2:-/srv/docs/distrineuquen-modulo/insumos/ARCA/apellidoNombreDenominacion.zip}
DO_COMMIT=${DO_COMMIT:-0}
cd /srv/odoo-lab
[ -r "$ZIP" ] || { echo "No se puede leer $ZIP" >&2; exit 1; }

payer_cuits() {
  docker compose exec -T db psql -U odoo -d "$DB" -AtX -v ON_ERROR_STOP=1 -c \
    "SELECT DISTINCT payer_cuit FROM distri_mp_collection WHERE payer_cuit ~ '^[0-9]{11}\$'"
}

python3 -I - "$ZIP" "$DO_COMMIT" 3< <(payer_cuits) <<'PY' \
  | docker compose exec -T odoo odoo shell -c /etc/odoo/odoo.conf -d "$DB" --no-http \
      --log-level=warn | tail -1
import io, os, sys, zipfile
path, do_commit = sys.argv[1], sys.argv[2] == "1"
with os.fdopen(3) as fd:
    wanted = {line.strip() for line in fd if line.strip()}
lines = []
with zipfile.ZipFile(path) as archive:
    for member in archive.infolist():
        if member.is_dir():
            continue
        with archive.open(member) as raw:
            for line in io.TextIOWrapper(raw, encoding="latin-1", newline=""):
                if line[:11] in wanted:
                    lines.append(line)
print("lines = %r" % lines)
print("counts = env['distri.arca.name']._load_from_padron(lines)")
print("env.cr.commit()" if do_commit else "env.cr.rollback()")
print("print('%s · pagadores con CUIT: %%(wanted)s · en el padrón: %%(found)s · nuevos: "
      "%%(created)s · actualizados: %%(updated)s · sin cambios: %%(unchanged)s' %% counts)"
      % ("GUARDADO" if do_commit else "PRUEBA (sin guardar; DO_COMMIT=1 para guardar)"))
PY
