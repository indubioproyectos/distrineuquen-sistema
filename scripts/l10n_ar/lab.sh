#!/usr/bin/env bash
# One-off Odoo process for the Argentine copy. It never touches the live
# container (odoo-lab-odoo-1), its config or its addons: it runs a separate
# odoo:19 container on the lab network with its own addons path.
#   lab.sh odoo shell -d <copy> ...
#
# WHICH CODE IT LOADS (mounted read only at /mnt/extra-addons):
#   1. LAB_ADDONS=<dir>  if set, always wins (ADDONS=<dir> is the old name, same effect);
#   2. -d distri_lab     the PRODUCTION snapshot /srv/odoo-lab/prod/addons-current,
#                        resolved at launch (scripts/prod-snapshot.sh), never the
#                        working tree;
#   3. any other DB      (distri_prueba, copies, tests): the working tree
#                        /srv/odoo-lab/addons.
# An override that makes distri_lab load anything other than the production
# snapshot prints a warning. The resolved folder reaches the container as
# LAB_ADDONS_SOURCE.
#
# IMAGE: the lab's Odoo image with Tesseract (/srv/odoo-lab/image); the
# receipt OCR tests need it. IMAGE=odoo:19 runs the plain image.
# LAB_MOUNTS: extra docker -v options, for example a private folder read only
# (LAB_MOUNTS="-v /srv/docs/distrineuquen-modulo/insumos:/insumos:ro").
set -euo pipefail
WORKTREE=/srv/odoo-lab/addons
PROD_CURRENT=/srv/odoo-lab/prod/addons-current

# database named on the command line (-d X, -dX, --database X, --database=X)
DBARG= prev=
for a in "$@"; do
  case $prev in -d|--database) DBARG=$a ;; esac
  case $a in --database=*) DBARG=${a#--database=} ;; -d?*) DBARG=${a#-d} ;; esac
  prev=$a
done

OVERRIDE=${LAB_ADDONS:-${ADDONS:-}}
if [[ -n "$OVERRIDE" ]]; then
  SRC=$OVERRIDE
elif [[ "$DBARG" == distri_lab ]]; then
  [[ -L "$PROD_CURRENT" ]] || { echo "lab.sh: no production snapshot ($PROD_CURRENT); see scripts/prod-snapshot.sh" >&2; exit 1; }
  SRC=$PROD_CURRENT
else
  SRC=$WORKTREE
fi
# pin the real folder now: a snapshot switch during this run cannot change it
SRC=$(readlink -e "$SRC") || { echo "lab.sh: addons folder not found: ${OVERRIDE:-$SRC}" >&2; exit 1; }
if [[ "$DBARG" == distri_lab && -L "$PROD_CURRENT" && "$SRC" != "$(readlink -e "$PROD_CURRENT")" ]]; then
  echo "lab.sh: WARNING: distri_lab with addons $SRC, not the production snapshot" >&2
fi

NAME=${NAME:-distri-ar-oneoff-$$}
IMAGE=${IMAGE:-distri-odoo:19-ocr}
exec docker run --rm -i --name "$NAME" --network odoo-lab_default \
  -v "$SRC":/mnt/extra-addons:ro \
  -v /srv/odoo-lab/oca-addons:/mnt/oca-addons:ro \
  -v odoo-lab_odoo-data:/var/lib/odoo \
  -v /srv/odoo-lab/scripts/l10n_ar:/scripts \
  ${LAB_MOUNTS:-} \
  -e HOST=db -e USER=odoo -e PASSWORD=odoo -e SNAP_TAG -e DO_COMMIT -e NEXT_NUMBER -e LIVE_DB \
  -e LAB_ADDONS_SOURCE="$SRC" \
  "$IMAGE" "$@" --addons-path=/mnt/extra-addons,/mnt/oca-addons
