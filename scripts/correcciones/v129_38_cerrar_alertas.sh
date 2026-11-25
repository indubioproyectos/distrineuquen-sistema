#!/usr/bin/env bash
# 19.0.1.129.38 (plan unificado, Etapa 5, #18 «Alertas del tablero solo para
# tarjetas nuevas» and the «Menos ruido» step): closes the open «Cobro sin
# resolver en el tablero» To-Dos the board no longer raises
# (models/verificacion_tramo2.py `_distri_v2_old_alerts`):
#   - «ingreso del reporte»: on an income card of the report (19.0.1.129.16
#     stopped raising them; its migration deleted the ones it found);
#   - «ya resuelta»: the card is no longer in «Sin transferencia» nor an
#     authorized delivery waiting for the report;
#   - «anterior al corte»: the card was created before --antes (default
#     distri_pedido.verificacion_historico_antes, 15/09/2026).
# A live alert of a current card stays. Each closed alert is marked done with
# a note (the way the board closes one): only the activity and its note are
# written. Never a card, a payment, an entry or a reconciliation; the script
# compares the ledger before and after and rolls back if anything moved.
# DRY RUN by default: it prints how many per reason. --apply writes.
# --sin-resumen also counts the To-Dos whose summary AND note are empty: the
# practice copy erases them (practica/sanitizar.sql), production never.
# distri_lab (production) is refused unless --produccion is given on purpose.
#   scripts/correcciones/v129_38_cerrar_alertas.sh <base>                      dry run
#   scripts/correcciones/v129_38_cerrar_alertas.sh <base> --apply              close them
#   scripts/correcciones/v129_38_cerrar_alertas.sh <base> --antes 2026-10-01   another cut
set -euo pipefail
DB="" APPLY=0 PROD=0 BLANK=0 BEFORE="" NEXT=""
for a in "$@"; do
  if [[ -n "$NEXT" ]]; then BEFORE=$a; NEXT=""; continue; fi
  case $a in
    --apply|--aplicar) APPLY=1 ;;
    --produccion) PROD=1 ;;
    --sin-resumen) BLANK=1 ;;
    --antes) NEXT=1 ;;
    --antes=*) BEFORE=${a#--antes=} ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--apply] [--antes AAAA-MM-DD] [--sin-resumen] [--produccion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
[[ -z "$BEFORE" || "$BEFORE" =~ ^[0-9]{4}-[0-9]{2}-[0-9]{2}$ ]] || { echo "bad date $BEFORE" >&2; exit 2; }
if [[ "$DB" == distri_lab && $PROD -ne 1 ]]; then
  echo "distri_lab is production: pass --produccion only on the owner's order" >&2
  exit 1
fi
HERE=$(cd "$(dirname "$0")" && pwd)
APPLY_PY=$([[ $APPLY -eq 1 ]] && echo True || echo False)
BLANK_PY=$([[ $BLANK -eq 1 ]] && echo True || echo False)
BEFORE_PY=$([[ -n "$BEFORE" ]] && echo "\"$BEFORE\"" || echo None)

PY=$(cat <<PY
env = env(user=2)
Collection = env["distri.mp.collection"].sudo()
if not hasattr(Collection, "_distri_v2_close_old_alerts"):
    raise SystemExit("ALERTAS ERROR: la base no tiene distri_pedido 19.0.1.129.38")
Promise = env["distri.promised.transfer"].sudo()
ledger = Promise._distri_ledger_fingerprint()
CARDS = ("SELECT md5(string_agg(id || ':' || state || ':' || coalesce(payment_id, 0)"
         " || ':' || coalesce(partner_id, 0), ',' ORDER BY id)) FROM distri_mp_collection")
env.cr.execute(CARDS)
cards = env.cr.fetchone()[0]
counts = Collection._distri_v2_close_old_alerts(apply=$APPLY_PY, before=$BEFORE_PY,
                                                include_blank=$BLANK_PY)
env.flush_all()
env.cr.execute(CARDS)
after_cards = env.cr.fetchone()[0]
after = Promise._distri_ledger_fingerprint()
if after != ledger or after_cards != cards:
    env.cr.rollback()
    raise SystemExit("ALERTAS ERROR: algo distinto de las alertas cambió; nada se guardó")
from odoo.addons.distri_pedido.models.verificacion import historic_before
label = "CERRADAS" if $APPLY_PY else "DRY-RUN"
for key, text in (("income", "ingreso del reporte"), ("resolved", "ya resuelta"),
                  ("old", "anterior al corte")):
    print("ALERTAS", label, key, counts.get(key, 0), "(%s)" % text)
print("ALERTAS", label, "total", sum(counts.values()), "corte", $BEFORE_PY or historic_before(env),
      "sin_resumen" if $BLANK_PY else "solo_resumen", "en", env.cr.dbname)
print("ALERTAS libro sin cambios", ledger)
if $APPLY_PY:
    env.cr.commit()
PY
)
FILTER=(rg -e '^ALERTAS' -e 'Traceback' -e 'Error')
if [[ "$DB" == distri_practica ]]; then
  # shellcheck source=../../practica/comun.sh
  source "$HERE/../../practica/comun.sh"
  odoo_run shell -d "$DB" --no-http --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
else
  NAME=distri-alertas-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http \
    --log-level=warn <<<"$PY" 2>&1 | "${FILTER[@]}"
fi
