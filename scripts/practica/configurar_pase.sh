#!/usr/bin/env bash
# 19.0.1.129.42, one-time setup: the shared key of «Ir a practicar» with a
# signed pass (addons/distri_pedido/controllers/practica_pase.py). The same
# random key goes to the two places that use it:
#   - production (or the base given): system parameter
#     distri_pedido.practica_pase_secret, written with set_param through
#     `odoo shell` (scripts/l10n_ar/lab.sh), so the running server drops its
#     cached value. Its name contains «secret»: practica/sanitizar.sql blanks
#     it in the practice database, which therefore never holds a usable key;
#   - practice: the file practica/config/distri_practica_pase.key (mounted
#     read only at /etc/odoo/ in the odoo-practica container, read on each
#     pass by distri_practica; git-ignored as *.key). Mode 0644: the
#     container's odoo user must read it, like odoo.conf next to it.
# The practice access code is never read or sent: production never learns it.
# The key is never printed nor put on a command line.
# Without the key (both places empty) «Ir a practicar» is the plain link and
# the practice landing asks the code, as before 19.0.1.129.42.
#
#   scripts/practica/configurar_pase.sh <base>                        dry run: says what it would do
#   scripts/practica/configurar_pase.sh <base> --apply                writes (new key only if missing)
#   scripts/practica/configurar_pase.sh <base> --apply --nueva        writes a new key (old links stop working)
#   scripts/practica/configurar_pase.sh <base> --apply --quitar       blanks both: back to the code
# distri_lab is production: it needs --produccion (dry run too), only on the
# owner's order.
set -euo pipefail
umask 077
DB="" APPLY=0 PROD=0 NEW=0 REMOVE=0
for a in "$@"; do
  case $a in
    --apply) APPLY=1 ;;
    --produccion) PROD=1 ;;
    --nueva) NEW=1 ;;
    --quitar) REMOVE=1 ;;
    -*) echo "unknown option $a" >&2; exit 2 ;;
    *) DB=$a ;;
  esac
done
[[ -n "$DB" ]] || { echo "usage: $0 <base> [--apply] [--nueva|--quitar] [--produccion]" >&2; exit 2; }
[[ "$DB" =~ ^[A-Za-z0-9_]+$ ]] || { echo "bad base name $DB" >&2; exit 2; }
if [[ "$DB" == distri_lab && $PROD -ne 1 ]]; then
  echo "distri_lab is production: pass --produccion only on the owner's order" >&2
  exit 1
fi
if [[ "$DB" == distri_practica ]]; then
  echo "distri_practica is the practice database: its key is the file, not a parameter" >&2
  exit 1
fi
(( NEW && REMOVE )) && { echo "--nueva and --quitar together make no sense" >&2; exit 2; }

HERE=$(cd "$(dirname "$0")" && pwd)
KEYFILE=/srv/odoo-lab/practica/config/distri_practica_pase.key
PARAM=distri_pedido.practica_pase_secret

file_state() { [[ -s "$KEYFILE" ]] && echo "present ($(wc -c < "$KEYFILE" | tr -d ' ') bytes)" || echo "missing"; }

# the parameter's state in the base, without its value
param_state() {
  NAME=distri-pase-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn <<PY 2>&1 \
    | rg -e '^PASS_' -e 'Traceback' -e 'Error' || true
v = (env["ir.config_parameter"].sudo().get_param("$PARAM") or "").strip()
print("PASS_PARAM", "set (%s chars)" % len(v) if v else "empty")
env.cr.rollback()
PY
}

echo "base:            $DB"
echo "practice file:   $KEYFILE: $(file_state)"
param_state
if (( ! APPLY )); then
  if (( REMOVE )); then
    echo "DRY RUN: would blank $PARAM in $DB and remove the practice key file"
  elif (( NEW )) || [[ ! -s "$KEYFILE" ]]; then
    echo "DRY RUN: would generate a new key into the practice file and set $PARAM in $DB"
  else
    echo "DRY RUN: would set $PARAM in $DB to the practice file's key"
  fi
  echo "nothing written (pass --apply)"
  exit 0
fi

if (( REMOVE )); then
  KEY=""
else
  if (( NEW )) || [[ ! -s "$KEYFILE" ]]; then
    tmp=$(mktemp "$KEYFILE.XXXXXX")
    python3 -c 'import secrets; print(secrets.token_urlsafe(36))' > "$tmp"
    chmod 0644 "$tmp"
    mv "$tmp" "$KEYFILE"
    echo "practice file: new key written"
  fi
  KEY=$(tr -d '[:space:]' < "$KEYFILE")
  (( ${#KEY} >= 32 )) || { echo "the practice key file holds less than 32 characters" >&2; exit 1; }
fi

# the key travels on stdin (the shell script below), never on argv
NAME=distri-pase-$$ "$HERE/../l10n_ar/lab.sh" odoo shell -d "$DB" --no-http --log-level=warn <<PY 2>&1 \
  | rg -e '^PASS_' -e 'Traceback' -e 'Error'
env["ir.config_parameter"].sudo().set_param("$PARAM", "$KEY")
env.cr.commit()
v = (env["ir.config_parameter"].sudo().get_param("$PARAM") or "").strip()
print("PASS_PARAM", "set (%s chars)" % len(v) if v else "empty")
print("PASS_COMMITTED")
PY
unset KEY
if (( REMOVE )); then
  rm -f -- "$KEYFILE"
  echo "practice file removed: the landing asks the code again"
fi
echo "done. Check: production /proyecto «Ir a practicar» (ui_v2 on) lands on the practice"
echo "landing with «Entraste desde Capacitación» and the user's role first."
