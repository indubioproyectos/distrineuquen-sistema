# Shared steps of the restore tests, sourced (not run) by respaldo-probar.sh
# (local daily backup) and respaldo-probar-r2.sh (off-site copy in R2).
# The caller sets COPY, the throwaway database. It is never the production
# database: every function refuses to work on it.
LAB=${LAB:-/srv/odoo-lab}
DB_CONTAINER=${DB_CONTAINER:-odoo-lab-db-1}
ODOO_CONTAINER=${ODOO_CONTAINER:-odoo-lab-odoo-1}
PROD_DB=distri_lab
COMUN_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)

copy_guard() {
  case ${COPY:-} in
    ''|"$PROD_DB") echo "ERROR: COPY='${COPY:-}' no es una base descartable"; exit 4 ;;
  esac
}

# drop the throwaway database and its filestore (also leftovers of a killed run)
drop_copy() {
  copy_guard
  docker exec "$DB_CONTAINER" dropdb -U odoo --if-exists "$COPY" >/dev/null 2>&1 || true
  docker exec "$ODOO_CONTAINER" rm -rf "/var/lib/odoo/filestore/$COPY" || true
  rm -rf "$ADDONS_EXPORT"
}

# restore_dump FILE: a pg_dump -Fc file into a new COPY database
restore_dump() {
  copy_guard
  docker exec "$DB_CONTAINER" createdb -U odoo "$COPY"
  docker exec -i "$DB_CONTAINER" pg_restore -U odoo -d "$COPY" --no-owner --role=odoo --exit-on-error \
    < "$1"
}

# key figures and rows of every table of COPY (respaldo-cifras.sql) to stdout
copy_figures() {
  copy_guard
  docker exec -i "$DB_CONTAINER" psql -U odoo -d "$COPY" -qAtX -v ON_ERROR_STOP=1 \
    < "$COMUN_DIR/respaldo-cifras.sql"
}

# load_filestore TAR_FLAGS < archive: archive of the folder <PROD_DB>/ (as
# the backups write it) becomes COPY's filestore. TAR_FLAGS: xzf or xf.
load_filestore() {
  copy_guard
  docker exec -i "$ODOO_CONTAINER" bash -c "
    set -e; mkdir -p /tmp/resp-$COPY && tar $1 - -C /tmp/resp-$COPY &&
    mv /tmp/resp-$COPY/$PROD_DB /var/lib/odoo/filestore/$COPY && rmdir /tmp/resp-$COPY"
}

# prints "<files> <bytes>" of COPY's filestore
filestore_count() {
  copy_guard
  docker exec "$ODOO_CONTAINER" bash -c \
    "find /var/lib/odoo/filestore/$COPY -type f -printf '%s\n' | awk '{n++; s+=\$1} END {print n+0, s+0}'"
}

# Odoo must open the copy with the module code it was backed up with: the
# current addons/ can be newer than the copy (columns the copy lacks).
# copy_addons reads distri_pedido's installed version in COPY, finds the
# newest commit whose addons/distri_pedido/__manifest__.py has exactly that
# version and exports its addons/ to ADDONS_EXPORT. Sets COPY_ADDONS (the
# folder for lab.sh), ADDONS_NOTE (what was used) and ADDONS_WARN (empty, or
# why it fell back to the current code: a warning, not a failure).
ADDONS_EXPORT=/srv/respaldos/.addons-${COPY:-sin-copia}
COPY_ADDONS= ADDONS_NOTE= ADDONS_WARN=
module_version() {  # module_version NAME: latest_version installed in COPY
  docker exec -i "$DB_CONTAINER" psql -U odoo -d "$COPY" -qAtX -v ON_ERROR_STOP=1 -v m="$1" \
    <<< "select coalesce(latest_version, '') from ir_module_module where name = :'m' and state = 'installed'"
}
manifest_version() {  # manifest_version COMMIT MODULE
  git -C "$LAB" show "$1:addons/$2/__manifest__.py" 2>/dev/null | grep -oP '"version":\s*"\K[^"]+' | head -1
}
copy_addons() {
  copy_guard
  local want tema c found=
  COPY_ADDONS=$LAB/addons ADDONS_NOTE="código actual" ADDONS_WARN=
  rm -rf "$ADDONS_EXPORT"
  want=$(module_version distri_pedido) && [ -n "$want" ] ||
    { ADDONS_WARN="no se leyó la versión de distri_pedido: se abrió con el código actual"; return 0; }
  tema=$(module_version distri_tema || true)
  for c in $(git -C "$LAB" log --format=%H -- addons/distri_pedido/__manifest__.py 2>/dev/null); do
    [ "$(manifest_version "$c" distri_pedido)" = "$want" ] && { found=$c; break; }
  done
  if [ -z "$found" ]; then
    ADDONS_WARN="versión sin commit ($want): se abrió con el código actual"
    return 0
  fi
  # the container's odoo user reads the export: world-readable files
  mkdir -p "$ADDONS_EXPORT" && chmod 755 "$ADDONS_EXPORT"
  if ! (umask 022; git -C "$LAB" archive "$found" addons | tar -x -C "$ADDONS_EXPORT"); then
    rm -rf "$ADDONS_EXPORT"
    ADDONS_WARN="no se pudo exportar el commit ${found:0:7}: se abrió con el código actual"
    return 0
  fi
  COPY_ADDONS=$ADDONS_EXPORT/addons
  ADDONS_NOTE="distri_pedido $want, commit ${found:0:7}"
  if [ -n "$tema" ] && [ "$(manifest_version "$found" distri_tema)" != "$tema" ]; then
    ADDONS_WARN="distri_tema instalado $tema, el commit ${found:0:7} tiene $(manifest_version "$found" distri_tema)"
  fi
}

# odoo_opens TAG: Odoo opens COPY read only (scripts/l10n_ar/snap.py in a
# one-off container) with the code copy_addons chose (the current addons/ if
# it was not called). Prints "PARTNERS n TOTAL x"; returns 1 when it failed.
odoo_opens() {
  copy_guard
  local out
  out=$(ADDONS="${COPY_ADDONS:-$LAB/addons}" NAME="$1-snap" SNAP_TAG="$1" "$LAB/scripts/l10n_ar/lab.sh" \
    odoo shell -d "$COPY" --no-http --log-level=warn < "$LAB/scripts/l10n_ar/snap.py" 2>&1 |
    grep '^SNAP ' || true)
  rm -f "$LAB/scripts/l10n_ar/out/snap_$1.json" 2>/dev/null || true
  [ -n "$out" ] || return 1
  echo "$out" | grep -oP 'PARTNERS \d+ TOTAL \S+'
}
