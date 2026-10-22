#!/usr/bin/env bash
# Writes a WhatsApp secret into the env file without echoing it.
# Usage: cargar-clave-whatsapp.sh [VARIABLE]  (default: WHATSAPP_APP_SECRET)
set -euo pipefail
f=/srv/odoo-lab/.whatsapp_test.env
var=${1:-WHATSAPP_APP_SECRET}
case "$var" in WHATSAPP_APP_SECRET|WHATSAPP_ACCESS_TOKEN) ;; *) echo "Variable no permitida: $var"; exit 1;; esac
read -rsp "Pegá la clave y apretá Enter (no se va a ver): " WA_SECRET; echo
[ -n "$WA_SECRET" ] || { echo "Quedó vacía, no cambié nada."; exit 1; }
WA_SECRET="$WA_SECRET" WA_VAR="$var" python3 - "$f" <<'PY'
import os, sys
path, var, secret = sys.argv[1], os.environ["WA_VAR"], os.environ["WA_SECRET"].strip()
lines = open(path).read().splitlines()
lines = [(var + "=" + secret) if l.startswith(var + "=") else l for l in lines]
open(path, "w").write("\n".join(lines) + "\n")
PY
echo "Listo, $var guardada (${#WA_SECRET} caracteres)."
