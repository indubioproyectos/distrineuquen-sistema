# Shared functions of the practice environment scripts (sourced, not run).
# Everything here acts on the compose project odoo-practica only: its own
# Postgres (odoo-practica-db), Odoo (odoo-practica) and volumes. Production
# (/srv/odoo-lab compose project, odoo-lab-*) is only ever READ, by
# prod_dump(), under the production maintenance lock.
P=/srv/odoo-lab/practica
LAB=/srv/odoo-lab
DB=distri_practica
GOLDEN=$P/golden
WORK=$P/work
URL=http://127.0.0.1:8070
HOST_HDR=practica.distrineuquen.com

log() { printf '%s %s\n' "$(date '+%F %T')" "$*"; }
die() { log "ERROR: $*"; exit 1; }

dc() { docker compose --project-directory "$P" -f "$P/compose.yaml" "$@"; }
pg() { docker exec -i odoo-practica-db psql -U odoo -X -q -v ON_ERROR_STOP=1 "$@"; }

db_up() {
  dc up -d db >/dev/null
  for _ in $(seq 1 60); do
    docker exec odoo-practica-db pg_isready -U odoo -q && return 0
    sleep 1
  done
  die "practice Postgres not ready"
}

odoo_stop() { dc stop odoo >/dev/null 2>&1 || true; }

odoo_start() {
  dc up -d odoo >/dev/null
  for _ in $(seq 1 90); do
    code=$(curl -s -o /dev/null -w '%{http_code}' -H "Host: $HOST_HDR" "$URL/web/login" || true)
    [[ "$code" == 200 ]] && { log "odoo-practica up ($URL)"; return 0; }
    sleep 2
  done
  die "odoo-practica not answering; docker logs odoo-practica"
}

# one-off Odoo process with the practice config, addons and volumes.
# db_host/db_port are also forced on the command line: Odoo 19 silently drops
# every option of odoo.conf when one of them is invalid (e.g. an unreadable
# addons_path entry) and then falls back to the local unix socket. "db" only
# resolves inside the odoo-practica network (the practice Postgres).
odoo_run() { dc run --rm --no-deps -T odoo odoo "$@" --db_host=db --db_port=5432; }

db_recreate() {
  [[ "$DB" == distri_practica ]] || die "bad DB $DB"
  # the practice cluster must never be the production one
  if pg -d postgres -Atc "select 1 from pg_database where datname='distri_lab'" | rg -q 1; then
    die "refusing: distri_lab exists in this cluster"
  fi
  pg -d postgres -c "drop database if exists distri_practica with (force)"
  pg -d postgres -c "create database distri_practica owner odoo"
}

db_restore() {  # $1 = custom-format dump on the host
  docker exec -i odoo-practica-db pg_restore -U odoo --no-owner --no-privileges --exit-on-error \
    -d "$DB" < "$1"
}

# replace the practice filestore with the content of a tar (top dir distri_practica/)
filestore_restore() {  # $1 = tar on the host
  dc run --rm --no-deps -T --user root --entrypoint bash odoo -c '
    set -e; mkdir -p /var/lib/odoo/filestore; cd /var/lib/odoo/filestore
    rm -rf distri_practica .nuevo; mkdir .nuevo; tar xf - -C .nuevo
    mv .nuevo/distri_practica distri_practica; rmdir .nuevo
    chown -R odoo:odoo /var/lib/odoo' < "$1"
}

filestore_dump() {  # $1 = tar to write on the host
  dc run --rm --no-deps -T --user root --entrypoint tar odoo cf - -C /var/lib/odoo/filestore distri_practica > "$1"
}

# commit of the repository whose addons have the distri_pedido version
# installed in production (newest commit with that version)
prod_installed_version() {
  docker exec odoo-lab-db-1 psql -U odoo -d distri_lab -Atc \
    "select latest_version from ir_module_module where name='distri_pedido' and state='installed'"
}
commit_for_version() {  # $1 = version
  local v=$1 c
  for c in $(git -C "$LAB" log --format=%H -- addons/distri_pedido); do
    if git -C "$LAB" show "$c:addons/distri_pedido/__manifest__.py" 2>/dev/null \
        | rg -q "\"version\": \"$v\""; then
      echo "$c"; return 0
    fi
  done
  return 1
}

# export the addons of a commit into snapshots/<commit> and point addons-prod at it
snapshot_use() {  # $1 = commit
  local c
  c=$(git -C "$LAB" rev-parse --verify "$1^{commit}") || die "unknown commit $1"
  if [[ ! -d "$P/snapshots/$c/addons/distri_pedido" ]]; then
    rm -rf "$P/snapshots/$c.tmp"
    # the callers run with umask 077, but the container user (odoo, uid 100)
    # must be able to read the addons: extract world-readable (public code)
    ( umask 022; mkdir -p "$P/snapshots/$c.tmp"
      git -C "$LAB" archive "$c" addons | tar -x --no-same-owner -C "$P/snapshots/$c.tmp" )
    mv "$P/snapshots/$c.tmp" "$P/snapshots/$c"
  fi
  chmod -R a+rX "$P/snapshots/$c"   # also repairs snapshots made with umask 077
  ln -sfn "snapshots/$c/addons" "$P/addons-prod"
  dc run --rm --no-deps -T --entrypoint test odoo -r /mnt/extra-addons/distri_pedido/__manifest__.py \
    >/dev/null 2>&1 || die "addons snapshot $c not readable inside the odoo-practica container"
  log "addons snapshot: $c ($(rg -o '"version": "[^"]+"' "$P/snapshots/$c/addons/distri_pedido/__manifest__.py"))"
}

snapshot_current() { local l; l=$(readlink "$P/addons-prod"); l=${l#snapshots/}; echo "${l%/addons}"; }

health_check() {
  local body
  body=$(curl -s -H "Host: $HOST_HDR" "$URL/") || die "landing not reachable"
  rg -q 'Entorno de práctica de Distri Neuquén' <<<"$body" || die "landing does not show the practice page"
  rg -q 'dn-practica-banner' <<<"$body" || die "banner missing on the landing"
  log "health check OK (landing + banner)"
}
