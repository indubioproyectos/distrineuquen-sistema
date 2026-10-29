#!/usr/bin/env python3
"""Extracts the receipt files named by carga_final.csv from the WhatsApp
exports, for paso3-cargar-mp.sh. Runs on the host with `python3 -I`.

  extraer_comprobantes.py <carga_final.csv> <cruce_final.csv> <zip admin> <zip gerente> <out dir>

Each receipt (column «comprobante») is looked up in cruce_final.csv by file
name and Mercado Pago operation to know its export (admin | gerente), then
read from that export by its exact name and written as
<out dir>/<export>__<file> (600). Only plain file names are accepted. Stops
if a receipt is missing or ambiguous. Prints a count, never names.
"""
import csv
import os
import re
import sys
import zipfile

SAFE_NAME = re.compile(r"^[\w .()+,-]+$")


def main(carga, cruce, zip_admin, zip_manager, out):
    exports = {}
    with open(cruce, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            exports.setdefault((row["archivo"], row["mp_op"]), set()).add(row["export"])
    zips = {"admin": zipfile.ZipFile(zip_admin), "gerente": zipfile.ZipFile(zip_manager)}
    members = {}
    for export, archive in zips.items():
        for name in archive.namelist():
            members.setdefault(export, {}).setdefault(os.path.basename(name), []).append(name)
    done = 0
    with open(carga, encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            name = row["comprobante"]
            if not name:
                continue
            if not SAFE_NAME.match(name) or name.startswith("."):
                sys.exit("nombre de comprobante no válido (operación %s)" % row["mp_op"])
            found = exports.get((name, row["mp_op"]), set())
            if len(found) != 1:
                sys.exit("comprobante sin export único (operación %s)" % row["mp_op"])
            export = found.pop()
            paths = members.get(export, {}).get(name, [])
            if len(paths) != 1:
                sys.exit("comprobante no está en el export %s (operación %s)" % (export, row["mp_op"]))
            target = os.path.join(out, "%s__%s" % (export, name))
            if os.path.exists(target):
                continue
            data = zips[export].read(paths[0])
            fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as dest:
                dest.write(data)
            done += 1
    print("comprobantes extraídos: %s" % done)


if __name__ == "__main__":
    if len(sys.argv) != 6:
        sys.exit(__doc__)
    main(*sys.argv[1:])
