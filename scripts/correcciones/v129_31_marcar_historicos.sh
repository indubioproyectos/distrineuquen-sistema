#!/usr/bin/env bash
# 19.0.1.129.31: marks «histórico» the pending incomes of the Mercado Pago
# report before distri_pedido.verificacion_historico_antes (default
# 15/09/2026; owner, 24/11/2026). models/verificacion.py
# _distri_v2_mark_historic: only the column `historic` is written. Nothing is
# deleted, confirmed or ignored, no payment and no balance changes; the board
# hides them with its default filter «Sin históricos» and they go to the
# Sistemas queue «Históricos y migración».
# DRY RUN by default: it prints how many and how much. --aplicar writes.
# Refused while distri_pedido.verificacion_v2 is off (run
# v129_31_activar_verificacion.sh first). distri_lab (production) is refused
# unless --produccion is given on purpose.
#   scripts/correcciones/v129_31_marcar_historicos.sh <base>             dry run
#   scripts/correcciones/v129_31_marcar_historicos.sh <base> --aplicar   mark them
set -euo pipefail
DB="" APPLY=0 PROD=0
for a in "$@"; do
  case $a in
    --aplicar) APPLY=1 ;;
    --produccion) PROD=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--aplicar] [--produccion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
if [[ "$DB" == distri_lab && $PROD -ne 1 ]]; then
  echo "distri_lab is production: pass --produccion only on the owner's order" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)
APPLY_PY=$([[ $APPLY -eq 1 ]] && echo True || echo False)

PY=$(cat <<PY
from odoo.addons.distri_pedido.models.verificacion import historic_before
env = env(user=2)
Collection = env["distri.mp.collection"].sudo()
count, total = Collection._distri_v2_mark_historic(apply=$APPLY_PY)
if $APPLY_PY:
    env.cr.commit()
print("HISTORICOS", "MARCADOS" if $APPLY_PY else "DRY-RUN", count, "ingresos",
      "%.2f" % total, "antes del", historic_before(env), "en", env.cr.dbname)
PY
)
FILTER=(rg -e '^HISTORICOS' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-historicos-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
