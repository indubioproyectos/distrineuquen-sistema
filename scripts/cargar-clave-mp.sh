#!/usr/bin/env bash
# Writes the Mercado Pago client secret into the env file without echoing it.
# Usage: cargar-clave-mp.sh
set -euo pipefail
f=/srv/odoo-lab/.mercadopago_test.env
var=MP_CLIENT_SECRET
read -rsp "Pegá la Client Secret y apretá Enter (no se va a ver): " MP_SECRET; echo
[ -n "$MP_SECRET" ] || { echo "Quedó vacía, no cambié nada."; exit 1; }
MP_SECRET="$MP_SECRET" MP_VAR="$var" python3 - "$f" <<'PY'
import os, sys
path, var, secret = sys.argv[1], os.environ["MP_VAR"], os.environ["MP_SECRET"].strip()
lines = open(path).read().splitlines()
if not any(l.startswith(var + "=") for l in lines):
    lines.append(var + "=")
lines = [(var + "=" + secret) if l.startswith(var + "=") else l for l in lines]
open(path, "w").write("\n".join(lines) + "\n")
PY
echo "Listo, $var guardada (${#MP_SECRET} caracteres)."
