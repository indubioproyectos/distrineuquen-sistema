# Shared by the paso*.sh wrappers of the 24/10/2026 cross (sourced, not run).
# Private inputs never enter the repository: each run copies them (600) into
# a temporary folder the container user (uid 100) reads through an ACL,
# mounted read only at /cruce and removed at the end. The run log (created
# ids, RESULT lines) goes to the private folder carga-lab/ (700, files 600).
INSUMOS=/srv/docs/distrineuquen-modulo/insumos
CRUCE=$INSUMOS/cruce-2026-10-24
LOGDIR=$CRUCE/carga-lab
LAB=/srv/odoo-lab/scripts/l10n_ar/lab.sh
COMMIT=0; [ "${DO_COMMIT:-0}" = 1 ] && COMMIT=1
MODE=$([ "$COMMIT" = 1 ] && echo commit || echo prueba)

lab_lock() {
  exec 9>/srv/odoo-lab/run/mantenimiento.lock
  flock -w 3600 9
}

new_tmp() {
  TMP=$(mktemp -d /srv/odoo-lab/run/cruce-XXXXXX)
  trap 'rm -rf "$TMP"' EXIT
}

# private_copy <source> <name inside /cruce>
private_copy() {
  mkdir -p "$(dirname "$TMP/$2")"
  install -m 600 "$1" "$TMP/$2"
}

open_acl() {
  setfacl -R -m u:100:rX "$TMP"
}

log_file() {
  (umask 077; mkdir -p "$LOGDIR")
  chmod 700 "$LOGDIR"
  echo "$LOGDIR/$1-$(date +%Y%m%d-%H%M%S)-$MODE.log"
}

# keep_lines <step> <output file>: the log lines, into the private log
keep_lines() {
  local log
  log=$(log_file "$1")
  (umask 077; rg -N -e '^(LOG|RESULT|SKIP|WARN|ABORT|LIMPIEZA|COMMITTED|NOT COMMITTED)' \
     -e 'Traceback' -e 'Error' "$2" > "$log" || true)
  chmod 600 "$log"   # the folder's default ACL would leave it 664
  [ "${KEEP_SALIDA:-0}" = 1 ] && (umask 077; cp "$2" "${log%.log}.salida.txt")
  echo "log: $log"
}

# run_body <step> <body.py> [python env assignments, e.g. ", SIMULAR='1'"]
run_body() {
  local step=$1 body=$2 extra=${3:-}
  local out="$TMP/salida.txt"
  LAB_MOUNTS="-v $TMP:/cruce:ro" NAME=distri-cruce-$step-$$ DO_COMMIT=$COMMIT \
    "$LAB" odoo shell -d "$DB" --no-http --log-level=warn \
    < <(printf '%s\n' "import os; os.environ.update(IN_DIR='/cruce', DO_COMMIT='$COMMIT'$extra)"; \
        cat "$body") > "$out" 2>&1 || true
  keep_lines "$step" "$out"
  rg -N -e '^(RESULT|SKIP|WARN|ABORT|LIMPIEZA|COMMITTED|NOT COMMITTED)' -e 'Traceback' -e 'Error' \
    "$out" || true
  rg -q '^(COMMITTED|NOT COMMITTED)' "$out"
}
