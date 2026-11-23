#!/usr/bin/env bash
# Loads the reviewed allocations («Imputación revisada», distri.owed.allocation,
# 19.0.1.129.9) of one or more propuesta.json files into a database and shows,
# per customer, the red rows before -> after (cargar_imputaciones_body.py).
# Rolls back unless DO_COMMIT=1.
#   scripts/rojo/cargar-imputaciones.sh <base> propuesta.json [más.json ...]
#     base: practica (the practice environment, odoo-practica) or a lab
#           database name for scripts/l10n_ar/lab.sh (distri_prueba, distri_lab…)
#   DO_COMMIT=1 ...                 writes
#   CONFIANZA=segura,probable       confidences taken (default)
#   ORIGEN="Revisión Fable 23/11/2026"  source; the previous active
#                                   allocations of that source of the same
#                                   customers are archived (default: today's)
#   APROBADO_POR="Ale"              approved_by of the new allocations
# The JSON files are read HERE, on the host, and travel inside the shell's
# stdin (base64): no mount, nothing written in the container.
set -euo pipefail
[[ $# -ge 2 ]] || { sed -n '2,15p' "$0" >&2; exit 2; }
BASE=$1; shift
HERE=$(cd "$(dirname "$0")" && pwd)
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
for f in "$@"; do [[ -r "$f" ]] || { echo "cannot read $f" >&2; exit 1; }; done

prelude() {
  python3 - "$@" <<'PY'
import base64, json, os, sys
files = []
for path in sys.argv[1:]:
    with open(path, encoding="utf-8") as fh:
        files.append([os.path.basename(path), json.load(fh)])
env = {k: os.environ.get(k, "") for k in ("CONFIANZA", "ORIGEN", "APROBADO_POR", "DO_COMMIT")}
blob = base64.b64encode(json.dumps({"files": files, "env": env}).encode()).decode()
print("IMPUTACIONES_B64 = %r" % blob)
PY
}
export DO_COMMIT=$COMMIT CONFIANZA=${CONFIANZA:-segura,probable} \
  ORIGEN=${ORIGEN:-"Revisión Fable $(date +%d/%m/%Y)"} APROBADO_POR=${APROBADO_POR:-}
INPUT=$(prelude "$@"; cat "$HERE/cargar_imputaciones_body.py")

if [[ "$BASE" == practica ]]; then
  source /srv/odoo-lab/practica/comun.sh
  run() { odoo_run shell -d "$DB" --no-http --log-level=error; }
else
  run() { NAME=distri-imputaciones-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$BASE" \
            --no-http --log-level=error; }
fi
# keep the report and any error; drop Odoo's INFO/WARNING log entries with
# their continuation lines (the py.warnings stacks)
run <<<"$INPUT" 2>&1 | awk '
  /^[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] [0-9:,]+ [0-9]+ / { skip = ($4 ~ /^(INFO|WARNING|DEBUG)$/) }
  /^(IMP|COMMITTED|NOT COMMITTED|Traceback)/ { skip = 0 }
  !skip'
