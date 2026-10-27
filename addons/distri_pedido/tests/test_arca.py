"""19.0.1.40.0: «Nombre en ARCA» on the unknown payers and the payment card.
Synthetic CUITs and names only; no file of the real padrón is read."""
import os
import tempfile
import zipfile
from datetime import timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, tagged

CUIT_PAYER = "30999000071"    # a payer that is in the padrón
CUIT_ABSENT = "20999000062"   # a payer that is not
CUIT_STRANGER = "30999000055"  # in the padrón, never paid: must not be kept


def padron_line(cuit, name, flags="ACACNINS00"):
    return "%s%-30s%s\n" % (cuit, name[:30], flags)


@tagged("post_install", "-at_install", "distri_arca")
class TestArcaName(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env.user.sudo().write({"group_ids": [
            Command.link(cls.env.ref("distri_pedido.grupo_administracion").id)]})
        cls.Collection = cls.env["distri.mp.collection"]
        cls.Name = cls.env["distri.arca.name"]
        cls.seq = 0

    def _income(self, cuit="", name="", amount=1000.0):
        type(self).seq += 1
        card = self.Collection.create({
            "kind": "income", "source_id": "98%010d" % self.seq, "amount": amount,
            "date": fields.Datetime.now() - timedelta(days=1),
            "payer_cuit": cuit, "payer_name": name, "method_type": "bank_transfer",
            "transaction_type": "SETTLEMENT",
        })
        card._propose()
        return card

    def _zip(self, lines):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        path = os.path.join(folder.name, "padron.zip")
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("padron.tmp", "".join(lines).encode("latin-1"))
        return path

    def _bucket(self, cuit):
        self.env.flush_all()
        return self.env["distri.mp.payer.bucket"].search([("payer_cuit", "=", cuit)])

    def test_loader_keeps_only_payers_and_is_idempotent(self):
        self._income(CUIT_PAYER, "Tabac")
        self._income(CUIT_ABSENT, "Juan")
        path = self._zip([
            padron_line(CUIT_STRANGER, "OTRA EMPRESA SA"),
            padron_line(CUIT_PAYER, "TABACALERA  DEL  SUR SRL"),
        ])
        counts = self.Name._load_from_padron(path)
        self.assertEqual(counts["lines"], 2)
        self.assertEqual((counts["found"], counts["created"], counts["updated"]), (1, 1, 0))
        self.assertEqual(self.Name.search([("cuit", "=", CUIT_PAYER)]).name,
                         "TABACALERA DEL SUR SRL", "inner spaces collapsed")
        self.assertFalse(self.Name.search([("cuit", "in", (CUIT_STRANGER, CUIT_ABSENT))]),
                         "only the CUITs that paid are kept")
        again = self.Name._load_from_padron(path)
        self.assertEqual((again["created"], again["updated"], again["unchanged"]), (0, 0, 1))
        self.assertEqual(self.Name.search_count([("cuit", "=", CUIT_PAYER)]), 1)
        # a changed name in a newer padrón is updated, from plain lines too
        renamed = self.Name._load_from_padron([padron_line(CUIT_PAYER, "TABACALERA SUR SA")])
        self.assertEqual((renamed["created"], renamed["updated"]), (0, 1))
        self.assertEqual(self.Name.search([("cuit", "=", CUIT_PAYER)]).name, "TABACALERA SUR SA")

    def test_bucket_and_card_show_the_arca_name(self):
        card = self._income(CUIT_PAYER, "Tabac")
        self.assertEqual(card.column, "unidentified")
        self.Name._load_from_padron([padron_line(CUIT_PAYER, "TABACALERA DEL SUR SRL")])
        bucket = self._bucket(CUIT_PAYER)
        self.assertEqual(bucket.payer_name, "Tabac")
        self.assertEqual(bucket.arca_name, "TABACALERA DEL SUR SRL")
        self.assertIn(bucket, self.env["distri.mp.payer.bucket"].search(
            [("arca_name", "ilike", "tabacalera")]), "searchable")
        card.invalidate_recordset(["arca_name"])
        self.assertEqual(card.arca_name, "TABACALERA DEL SUR SRL")

    def test_empty_when_not_in_the_padron(self):
        card = self._income(CUIT_ABSENT, "Juan")
        no_cuit = self._income("", "SIN CUIT")
        self.Name._load_from_padron([padron_line(CUIT_STRANGER, "OTRA EMPRESA SA")])
        bucket = self._bucket(CUIT_ABSENT)
        self.assertEqual(len(bucket), 1)
        self.assertFalse(bucket.arca_name)
        self.assertFalse(card.arca_name)
        self.assertFalse(no_cuit.arca_name)
