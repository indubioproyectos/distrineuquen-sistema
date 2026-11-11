"""Cash sheets into the lab, step 1 (host side, standard library only): read
the processed 2026 sheet archive, the pairings, the cash loader rows, the
photo index and the Drive downloads and write one input bundle for the Odoo
shell step (cargar_planillas_body.py), with the originals copied next to it.

    python3 -I preparar.py <out dir>

What becomes a sheet: one per tab of an xlsx of the archive that has rows
(_procesado/v2/movimientos.csv), so one per distinct file content. Skipped
(listed as OMITIDO in the log):
- exact copies: dup_status duplicate_file_of / duplicate_sheet_of (the
  primary copy is loaded; CAMBIOS.md §3);
- files that are not a rendición (inventory type Odoo export, docx, debt
  summary, template, retiros, other) and the 7 daily files with origen
  odoo_export (exports of Odoo journal items with CSH numbers: their
  payments are already in Odoo; CAMBIOS.md §6). The planilla_formato_odoo
  lists (not confirmed as exports) are loaded with a note;
- files with no rows, or whose sha256 no longer matches the inventory.
Near-duplicate sheets (near_duplicate_sheet_of/row_of) are distinct content:
loaded, with the status on each row and a note on the sheet.
Every row with a client, a document or an amount becomes a line, also the
expense, freight, summary and doubtful rows (with their row_kind); the sheet
total counts only client rows above the TOTAL.
Customer of a row: the Online customer of the cross (pareos.csv, any date,
or mapeo-clientes.csv alta/media for rows from 2026-03-01 on, the owner's
cutoff of the cross); older names stay without customer.
Photos: the 23/10 photos with the invoices of planillas_revisadas.csv and
the 27/10 photos with the invoices read from them (fotos-planillas.csv); the
Odoo step attaches each to the one sheet that has those invoices.
Nothing here touches Odoo.
"""
import csv
import glob
import hashlib
import json
import os
import re
import shutil
import sys

INSUMOS = "/srv/docs/distrineuquen-modulo/insumos"
ARCHIVE = os.path.join(INSUMOS, "planillas-efectivo-2026")
V2 = os.path.join(ARCHIVE, "_procesado", "v2")
CRUCE = os.path.join(ARCHIVE, "_cruce-online")
LOADER_GLOBS = [os.path.join(INSUMOS, "planillas-2026-10-28", "filas*.jsonl"),
                os.path.join(CRUCE, "carga-lab", "filas-*.jsonl")]
DRIVE_DIR = os.path.join(INSUMOS, "planillas-2026-11-06", "descargas")
PHOTOS_23 = "/srv/docs/distrineuquen-modulo/cobranza/efectivo"
PHOTOS_27 = os.path.join(INSUMOS, "planillas-efectivo-2026-10-27")
HERE = os.path.dirname(os.path.abspath(__file__))
# owner decision of the cross (_cruce-online/resumen.md): client names are
# mapped only for rows from March 2026 on
MAP_FROM = "2026-03-01"
SKIP_DUP = ("duplicate_file_of", "duplicate_sheet_of")
NOISE = {"", "-", "--", "OK", "X", "FIRMADO", "FIRMA", "SI", "NO"}
DRIVE_RE = re.compile(r"^(\d{4})\.(\d\d)\.(\d\d)\s*-?\s*(?:(\d+)\s*-)?\s*(.+?)\s*\.txt$")


def rows(path):
    with open(path, encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle))


def num(text):
    try:
        return round(float(text), 2) if text not in ("", None) else 0.0
    except ValueError:
        return 0.0


def notes_of(raw):
    """«HEADER@G7=DEVOLVIO PILAS - FIRMADO | …» -> «DEVOLVIO PILAS - FIRMADO»;
    signatures, ticks and the Ítalo label are left out."""
    out = []
    for part in (raw or "").split(" | "):
        head, _, value = part.partition("=")
        value = value.strip()
        if not value or value.upper() in NOISE or head.startswith("Etiqueta@"):
            continue
        out.append(value)
    return " · ".join(out)


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def stage(path, out_dir, name):
    target = os.path.join(out_dir, "files", name)
    if not os.path.exists(target):
        shutil.copyfile(path, target)
    return "files/" + name


def main(out_dir):
    os.makedirs(os.path.join(out_dir, "files"), exist_ok=True)
    inventory = {r["path"]: r for r in rows(os.path.join(V2, "inventario.csv"))}
    movements = rows(os.path.join(V2, "movimientos.csv"))
    pairings = rows(os.path.join(CRUCE, "pareos.csv"))
    mapping = {r["client_raw"]: int(r["partner_id"]) for r in rows(os.path.join(CRUCE, "mapeo-clientes.csv"))
               if r["usado_para_parear"] == "si" and r["partner_id"].isdigit()
               and r["confidence"] in ("alta", "media")}
    online_partner_of_mov = {p["mov_id"]: int(p["partner_id"]) for p in pairings
                             if p["partner_id"].isdigit()}
    skipped = {}

    def skip(path, reason):
        skipped.setdefault(reason, set()).add(path)

    for path, inv in inventory.items():
        if inv["type"] != "daily rendición sheet":
            skip(path, "no es una planilla de rendición (%s)" % inv["type"])
        elif inv["origen"] == "odoo_export":
            skip(path, "exportación de Odoo (origen odoo_export): sus pagos ya están en Odoo")
        elif inv["rows_extracted"] in ("", "0"):
            skip(path, "planilla sin filas")

    tabs = {}
    for mov in movements:
        path = mov["source_file"]
        inv = inventory[path]
        if inv["type"] != "daily rendición sheet" or inv["origen"] == "odoo_export":
            continue
        status = mov["dup_status"].split(":")[0]
        if status in SKIP_DUP:
            skip("%s [%s]" % (path, mov["sheet"]), "copia exacta (%s)" % status)
            continue
        if not (mov["client_raw"] or mov["doc_ref"] or mov["amount_paid"] or mov["amount_invoiced"]):
            continue
        tabs.setdefault((path, mov["sheet"]), []).append(mov)

    sheets = []
    tab_count = {}
    for path, _tab in tabs:
        tab_count[path] = tab_count.get(path, 0) + 1
    for (path, tab), movs in sorted(tabs.items()):
        inv = inventory[path]
        source = os.path.join(ARCHIVE, path)
        if not os.path.exists(source):
            skip(path, "falta el archivo")
            continue
        if sha256(source) != inv["sha256"]:
            skip(path, "el archivo cambió desde el inventario (sha256 distinto)")
            continue
        days = sorted({m["movement_date"] for m in movs if m["movement_date"]})
        sheet_day = days[0] if len(days) == 1 else (inv["date"] or (days[-1] if days else ""))
        if not sheet_day:
            skip(path, "sin fecha")
            continue
        near = sorted({m["dup_status"] for m in movs if m["dup_status"].startswith("near_duplicate_sheet_of")})
        note = []
        if near:
            note.append("Copia casi igual de otra planilla: %s" % near[0].split(":", 1)[1].split("[")[0].strip())
        if len(days) > 1:
            note.append("Planilla de varios días (%s a %s): cada fila tiene su fecha." % (days[0], days[-1]))
        if inv["origen"] == "planilla_formato_odoo":
            note.append("Lista con columnas de Odoo (Fecha|Etiqueta|Empresa|Importe): puede ser una exportación.")
        lines = []
        for mov in movs:
            client = mov["client_raw"].strip()
            online = online_partner_of_mov.get(mov["mov_id"])
            if not online and client and mov["movement_date"] >= MAP_FROM:
                online = mapping.get(client)
            cell = mov["paid_cell"] or mov["invoiced_cell"] or mov["client_cell"]
            lines.append({
                "key": mov["mov_id"],
                "sequence": int(mov["source_row"] or 0) * 10 + int(mov["split_index"] or 0),
                "cell": "fila %s%s" % (mov["source_row"], " · %s" % cell if cell else ""),
                "client": client,
                "online_partner": online,
                "invoice": mov["doc_ref"].strip(),
                "invoiced": num(mov["amount_invoiced"]),
                "paid": num(mov["amount_paid"]),
                "method": mov["metodo_inferido"] or mov["payment_method_norm"],
                "notes": notes_of(mov["notes_raw"]),
                "kind": mov["row_kind"] or "dudoso",
                "after_total": mov["after_total"] == "1",
                "dup": mov["dup_status"].split(":")[0] if mov["dup_status"] != "primary" else "",
                "date": mov["movement_date"] or sheet_day,
            })
        base = os.path.basename(path)
        sheets.append({
            "key": "xlsx:%s:%s" % (inv["sha256"], tab),
            "name": base[:-5].strip() + (" [%s]" % tab if tab_count[path] > 1 else ""),
            "collector": inv["collector"] or inv["collector_folder"] or "SIN COBRADOR",
            "date": sheet_day,
            "path": path,
            "tab": tab,
            "sha256": inv["sha256"],
            "file": stage(source, out_dir, "%s.xlsx" % inv["sha256"]),
            "file_name": base,
            "note": "\n".join(note),
            "lines": lines,
        })

    pairs = [{"mov_id": p["mov_id"], "match_kind": p["match_kind"], "odoo_source": p["odoo_source"],
              "odoo_id": p["odoo_id"], "odoo_name": p["odoo_name"]} for p in pairings]

    loader, seen = [], set()
    for pattern in LOADER_GLOBS:
        for path in sorted(glob.glob(pattern)):
            with open(path, encoding="utf-8") as handle:
                for line in handle:
                    if not line.strip():
                        continue
                    item = json.loads(line)
                    if item["clave"] in seen:
                        continue
                    seen.add(item["clave"])
                    item["source_file"] = os.path.relpath(path, INSUMOS)
                    loader.append(item)

    photos, by_photo = [], {}
    for item in rows(os.path.join(PHOTOS_23, "planillas_revisadas.csv")):
        if item["factura"].strip():
            by_photo.setdefault(item["foto"], set()).add(item["factura"].strip())
    for name, invoices in sorted(by_photo.items()):
        path = os.path.join(PHOTOS_23, "Planillas", name)
        if os.path.exists(path):
            photos.append({"name": name, "source": os.path.relpath(path, "/srv/docs/distrineuquen-modulo"),
                           "file": stage(path, out_dir, name), "invoices": sorted(invoices),
                           "evidence": "planillas_revisadas.csv"})
    for item in rows(os.path.join(HERE, "fotos-planillas.csv")):
        path = os.path.join(PHOTOS_27, item["foto"])
        if os.path.exists(path):
            photos.append({"name": item["foto"], "source": os.path.relpath(path, "/srv/docs/distrineuquen-modulo"),
                           "file": stage(path, out_dir, item["foto"]),
                           "invoices": ["0001/2026/%s" % n for n in item["facturas"].split()],
                           "evidence": "fotos-planillas.csv (leídas a mano)"})

    drive = []
    for path in sorted(glob.glob(os.path.join(DRIVE_DIR, "*.txt"))):
        match = DRIVE_RE.match(os.path.basename(path))
        if not match:
            continue
        year, month, day, index, rest = match.groups()
        collector = rest.replace("NEUQUEN", "").strip(" -").split(" - ")[0].strip()
        drive.append({"name": os.path.basename(path), "file": stage(path, out_dir, "drive-%s" % os.path.basename(path)),
                      "collector": collector, "date": "%s-%s-%s" % (year, month, day),
                      "index": int(index or 1)})

    bundle = {"sheets": sheets, "pairs": pairs, "loader": loader, "photos": photos, "drive": drive,
              "skipped": {k: sorted(v) for k, v in sorted(skipped.items())}}
    with open(os.path.join(out_dir, "entrada.json"), "w", encoding="utf-8") as handle:
        json.dump(bundle, handle, ensure_ascii=False)
    print("PREPARADO", json.dumps({
        "planillas": len(sheets), "filas": sum(len(s["lines"]) for s in sheets),
        "pareos": len(pairs), "filas_carga": len(loader), "fotos": len(photos), "drive": len(drive),
        "omitidos": {k: len(v) for k, v in skipped.items()},
    }, ensure_ascii=False))


if __name__ == "__main__":
    main(sys.argv[1])
