# Production code snapshots (sourced, not run). Same idea as the practice
# environment (practica/comun.sh snapshot_use): production never mounts the
# working tree /srv/odoo-lab/addons, it mounts a `git archive` export of one
# commit, so editing files in the working tree can never reach a production
# restart.
#
#   prod/snapshots/<short-commit>/addons   export of `git archive <commit> addons`
#   prod/snapshots/<short-commit>/COMMIT   full hash of that commit
#   prod/addons-current -> snapshots/<short-commit>/addons   (relative symlink)
#
# compose.yaml mounts prod/addons-current read only. Docker resolves the
# symlink when the container STARTS (verified: a running container keeps its
# old target; `docker compose stop odoo && docker compose start odoo` picks the
# new one, no recreate needed). Switching the symlink alone changes nothing in
# a running container.
PROD_LAB=/srv/odoo-lab
PROD_DIR=$PROD_LAB/prod
PROD_SNAPS=$PROD_DIR/snapshots
PROD_CURRENT=$PROD_DIR/addons-current
PROD_KEEP=${PROD_KEEP:-10}

# prod_short COMMIT: verified short hash (directory name of its snapshot)
prod_short() {
  git -C "$PROD_LAB" rev-parse --verify --short "$1^{commit}"
}

# prod_snapshot_export COMMIT: export the addons of COMMIT once (idempotent);
# prints the snapshot name. Extracted world-readable (the container user odoo,
# uid 100, reads it) and then made non-writable, so nobody edits it by mistake.
prod_snapshot_export() {
  local full short dir
  full=$(git -C "$PROD_LAB" rev-parse --verify "$1^{commit}") || return 1
  short=$(prod_short "$full") || return 1
  dir=$PROD_SNAPS/$short
  if [[ ! -f "$dir/COMMIT" ]]; then
    ( umask 022
      mkdir -p "$PROD_SNAPS"
      rm -rf "$dir.tmp"; mkdir "$dir.tmp"
      git -C "$PROD_LAB" archive "$full" addons | tar -x --no-same-owner -C "$dir.tmp"
      [[ -f "$dir.tmp/addons/distri_pedido/__manifest__.py" ]] || { echo "export of $full has no distri_pedido" >&2; exit 1; }
      echo "$full" > "$dir.tmp/COMMIT"
      mv "$dir.tmp" "$dir" ) || return 1
  fi
  chmod -R a+rX,a-w "$dir"
  echo "$short"
}

# prod_snapshot_current: name of the active snapshot (empty if none yet)
prod_snapshot_current() {
  local l
  l=$(readlink "$PROD_CURRENT" 2>/dev/null) || return 0
  l=${l#snapshots/}; echo "${l%/addons}"
}

# prod_snapshot_switch NAME: point addons-current at an existing snapshot
# (atomic rename of a new symlink). Only takes effect at the next container start.
prod_snapshot_switch() {
  [[ -f "$PROD_SNAPS/$1/addons/distri_pedido/__manifest__.py" ]] || { echo "no snapshot $1" >&2; return 1; }
  ln -sfn "snapshots/$1/addons" "$PROD_CURRENT.new"
  mv -T "$PROD_CURRENT.new" "$PROD_CURRENT"
  touch -h "$PROD_SNAPS/$1"   # newest = most recently used, for pruning
}

# prod_snapshot_version NAME: distri_pedido version in a snapshot
prod_snapshot_version() {
  rg -o '"version": "([^"]+)"' -r '$1' "$PROD_SNAPS/$1/addons/distri_pedido/__manifest__.py"
}

# prod_snapshot_prune [KEEP_ALSO...]: keep the PROD_KEEP most recently used
# snapshots, never the active one nor the names given.
prod_snapshot_prune() {
  local cur n=0 name
  cur=$(prod_snapshot_current)
  while read -r _ name; do
    n=$((n + 1))
    (( n <= PROD_KEEP )) && continue
    [[ "$name" == "$cur" || " $* " == *" $name "* ]] && continue
    chmod -R u+w "$PROD_SNAPS/$name" && rm -rf "${PROD_SNAPS:?}/$name" && echo "snapshot pruned: $name"
  done < <(for d in "$PROD_SNAPS"/*/; do
             d=${d%/}; [[ -f "$d/COMMIT" ]] && stat -c '%Y %n' "$d"
           done | sort -rn | sed 's#[^ ]*/##')
}
