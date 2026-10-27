"""Nombre en ARCA (19.0.1.40.0).

The Mercado Pago report only brings the payer's nickname («Tabac»), so a
wholesaler is hard to recognise in «Sin identificar». ARCA publishes the
padrón of legal names (apellidoNombreDenominacion.zip: fixed-width latin-1
lines, chars 0-10 the CUIT, 11-40 the name, then flags). This keeps only the
names of the CUITs that already paid through Mercado Pago, never the full
padrón, and shows them on the payer buckets and on the payment card.

Loaded by hand with scripts/cargar_nombres_arca.sh (dry run by default); no
external service is ever called. Most individuals are not in the padrón:
an empty name is normal.
"""
import io
import zipfile

from odoo import api, fields, models

from .cobros import digits

CUIT_END = 11
NAME_END = 41


def normalize_cuit(value):
    # same rule as mayoristas.normalize_cuit; this file loads first so the
    # table exists before the payer bucket view joins it
    number = digits(value or "")
    return number if len(number) == 11 else ""


class DistriArcaName(models.Model):
    _name = "distri.arca.name"
    _description = "Nombre en el padrón de ARCA"
    _order = "cuit"
    _rec_name = "name"

    cuit = fields.Char(string="CUIT", required=True, index=True, readonly=True)
    name = fields.Char(string="Nombre en ARCA", required=True, readonly=True)

    _cuit_unique = models.Constraint("UNIQUE(cuit)", "Ya hay un nombre para ese CUIT.")

    @api.model
    def _wanted_cuits(self):
        """The CUITs of the Mercado Pago payers: the only ones kept."""
        self.env["distri.mp.collection"].flush_model(["payer_cuit"])
        self.env.cr.execute("SELECT DISTINCT payer_cuit FROM distri_mp_collection "
                            "WHERE COALESCE(payer_cuit, '') <> ''")
        return {cuit for cuit in (normalize_cuit(r[0]) for r in self.env.cr.fetchall()) if cuit}

    @api.model
    def _padron_lines(self, source):
        """Text lines from a path (the ARCA zip, or a plain latin-1 file with the
        same layout) or from any iterable of lines (str or bytes)."""
        if not isinstance(source, str):
            for line in source:
                yield line.decode("latin-1") if isinstance(line, bytes) else line
            return
        if zipfile.is_zipfile(source):
            with zipfile.ZipFile(source) as archive:
                for member in archive.infolist():
                    if member.is_dir():
                        continue
                    with archive.open(member) as raw:
                        yield from io.TextIOWrapper(raw, encoding="latin-1", newline="")
        else:
            with open(source, encoding="latin-1", newline="") as text:
                yield from text

    @api.model
    def _load_from_padron(self, source):
        """Upsert the names of the wanted CUITs found in `source`. Returns the
        counts only (no CUIT nor name), so it can be printed anywhere."""
        wanted = self._wanted_cuits()
        found = {}
        read = 0
        for line in self._padron_lines(source):
            read += 1
            cuit = line[:CUIT_END]
            if cuit not in wanted:
                continue
            name = " ".join(line[CUIT_END:NAME_END].split())
            if name:
                found[cuit] = name
        Name = self.sudo()
        existing = {r.cuit: r for r in Name.search([("cuit", "in", list(found))])}
        created = updated = 0
        to_create = []
        for cuit, name in sorted(found.items()):
            record = existing.get(cuit)
            if not record:
                to_create.append({"cuit": cuit, "name": name})
            elif record.name != name:
                record.name = name
                updated += 1
        if to_create:
            Name.create(to_create)
            created = len(to_create)
        return {"lines": read, "wanted": len(wanted), "found": len(found),
                "created": created, "updated": updated,
                "unchanged": len(found) - created - updated}


class DistriMpCollectionArca(models.Model):
    _inherit = "distri.mp.collection"

    arca_name = fields.Char(string="Nombre en ARCA", compute="_compute_arca_name",
                            help="Nombre legal del padrón público de ARCA para el CUIT de quien "
                                 "pagó. Vacío si el CUIT no figura (es lo normal en personas "
                                 "sin inscripción).")

    @api.depends("payer_cuit")
    def _compute_arca_name(self):
        cuits = {normalize_cuit(c) for c in self.mapped("payer_cuit")} - {""}
        names = dict(self.env["distri.arca.name"].sudo().search(
            [("cuit", "in", list(cuits))]).mapped(lambda r: (r.cuit, r.name))) if cuits else {}
        for card in self:
            card.arca_name = names.get(normalize_cuit(card.payer_cuit), False)
