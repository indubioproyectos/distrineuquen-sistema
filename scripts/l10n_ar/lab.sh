#!/usr/bin/env bash
# One-off Odoo process for the Argentine copy. It never touches the live
# container (odoo-lab-odoo-1), its config or its addons: it runs a separate
# odoo:19 container on the lab network with its own addons path.
#   ADDONS=/srv/odoo-lab/addons lab.sh odoo shell -d <copy> ...
# IMAGE: the lab's Odoo image with Tesseract (/srv/odoo-lab/image); the
# receipt OCR tests need it. IMAGE=odoo:19 runs the plain image.
# LAB_MOUNTS: extra docker -v options, for example a private folder read only
# (LAB_MOUNTS="-v /srv/docs/distrineuquen-modulo/insumos:/insumos:ro").
set -euo pipefail
ADDONS=${ADDONS:-/srv/odoo-lab/addons}
NAME=${NAME:-distri-ar-oneoff-$$}
IMAGE=${IMAGE:-distri-odoo:19-ocr}
exec docker run --rm -i --name "$NAME" --network odoo-lab_default \
  -v "$ADDONS":/mnt/extra-addons:ro \
  -v /srv/odoo-lab/oca-addons:/mnt/oca-addons:ro \
  -v odoo-lab_odoo-data:/var/lib/odoo \
  -v /srv/odoo-lab/scripts/l10n_ar:/scripts \
  ${LAB_MOUNTS:-} \
  -e HOST=db -e USER=odoo -e PASSWORD=odoo -e SNAP_TAG -e DO_COMMIT -e NEXT_NUMBER -e LIVE_DB \
  "$IMAGE" "$@" --addons-path=/mnt/extra-addons,/mnt/oca-addons
