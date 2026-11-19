#!/usr/bin/env bash
# Screenshots at 390 px of the landing and each role's screen, with the
# sessions opened by probar.py (out/e2e.json). Renderer: wkhtmltoimage from
# the production Odoo image (the distri-playwright:1.52 image is not on this
# server). Old WebKit: pages drawn by modern JavaScript may look incomplete.
#   ./capturas.sh out/e2e.json
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
OUT=/srv/odoo-lab/practica/capturas
JSON=${1:-$HERE/out/e2e.json}
mkdir -p "$OUT"
shot() {  # name path [session]
  local cookie=()
  [[ -n "${3:-}" ]] && cookie=(--cookie session_id "$3")
  docker run --rm --network host --add-host practica.distrineuquen.com:127.0.0.1 \
    --user "$(id -u):$(id -g)" -e HOME=/tmp -v "$OUT":/out --entrypoint wkhtmltoimage \
    distri-odoo:19-ocr --quiet --width 390 --javascript-delay 4000 --format png \
    "${cookie[@]}" "http://practica.distrineuquen.com:8070$2" "/out/$1.png" || echo "WARN $1"
  echo "captura $1 <- $2"
}
shot 00-landing /
python3 -I - "$JSON" <<'EOF' | while read -r name path sid; do shot "$name" "$path" "$sid"; done
import json, sys
d = json.load(open(sys.argv[1]))["sessions"]
order = ["armador", "repartidor", "deposito", "cajero", "administracion", "gerencia", "vendedor"]
for i, k in enumerate(order, 1):
    s = d[k]
    print("%02d-%s %s %s" % (i, k, s["path"], s["session_id"]))
print("08-backend-administracion /odoo %s" % d["administracion"]["session_id"])
print("09-landing-con-codigos / %s" % d["administracion"]["session_id"])
EOF
