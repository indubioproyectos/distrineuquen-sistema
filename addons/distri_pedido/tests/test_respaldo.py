"""«Respaldos» status card (19.0.1.90.0, models/respaldo.py): the state the
backup script's values produce, the R2 usage against the free tier and that
only Settings administrators see it."""
from datetime import datetime, timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from odoo.addons.distri_pedido.models.respaldo import (
    R2_FREE_BYTES, human_age, human_size, human_time,
)


@tagged("post_install", "-at_install", "distri_respaldo")
class TestBackupStatus(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.Status = cls.env["distri.backup.status"].sudo()
        # the real row, if any, is not ours to judge: start from an empty table
        cls.Status.search([]).unlink()

    def _status(self, **values):
        return self.Status.create(values)

    def _ago(self, **delta):
        return fields.Datetime.now() - timedelta(**delta)

    # ------------------------------------------------------------ state

    def test_recent_copy_is_ok(self):
        status = self._status(local_ok=self._ago(minutes=10), r2_ok=self._ago(hours=1))
        self.assertEqual(status.state, "ok")
        self.assertIn("al día", status.state_message)

    def test_copy_older_than_26_hours_is_stale(self):
        self.assertEqual(self._status(r2_ok=self._ago(hours=25, minutes=50)).state, "ok")
        status = self._status(r2_ok=self._ago(hours=26, minutes=10))
        self.assertEqual(status.state, "stale")
        self.assertIn("más de 26 horas", status.state_message)

    def test_error_wins_over_a_recent_copy(self):
        status = self._status(r2_ok=self._ago(hours=1),
                              r2_error="2026-11-16T10:15:02-03:00 copy to R2 failed, "
                                       "see logs/respaldo-hora.log")
        self.assertEqual(status.state, "error")
        self.assertIn("copy to R2 failed", status.state_message)

    def test_missing_data_is_stale(self):
        status = self._status()
        self.assertEqual(status.state, "stale")
        self.assertEqual(status.r2_ok_display, "Sin datos")
        self.assertEqual(status.local_ok_display, "Sin datos")
        self.assertEqual(status.r2_size_display, "Sin datos")
        self.assertEqual(status.r2_usage_percent, 0.0)
        # a local backup alone does not make the off-site copy fine
        self.assertEqual(self._status(local_ok=self._ago(minutes=5)).state, "stale")

    # ------------------------------------------------------------ R2 usage

    def test_usage_percent_of_the_free_tier(self):
        status = self._status(r2_ok=self._ago(hours=1), r2_bytes=2.5 * 1000 ** 3,
                              r2_snapshots=41, r2_checked=self._ago(hours=3))
        self.assertAlmostEqual(status.r2_usage_percent, 25.0)
        self.assertEqual(status.r2_size_display, "2,5 GB de 10,0 GB")
        over = self._status(r2_bytes=R2_FREE_BYTES * 1.2, r2_checked=self._ago(hours=3))
        self.assertAlmostEqual(over.r2_usage_percent, 120.0)

    def test_human_formats(self):
        self.assertEqual(human_size(512), "512 B")
        self.assertEqual(human_size(1_234_567), "1,2 MB")
        self.assertEqual(human_size(False), "Sin datos")
        self.assertEqual(human_age(timedelta(seconds=20)), "recién")
        self.assertEqual(human_age(timedelta(minutes=5)), "hace 5 min")
        self.assertEqual(human_age(timedelta(hours=30)), "hace 30 h")
        self.assertEqual(human_age(timedelta(days=3)), "hace 3 días")
        # 13:15 UTC is 10:15 in Buenos Aires (UTC-3, no daylight saving)
        self.assertEqual(human_time(datetime(2026, 11, 16, 13, 15), datetime(2026, 11, 16, 15, 15)),
                         "16/11/2026 10:15 (hace 2 h)")

    # ------------------------------------------------------------ access

    def test_open_creates_the_single_row_once(self):
        admin = new_test_user(self.env, login="sistema_respaldo", groups="base.group_system")
        Status = self.env["distri.backup.status"].with_user(admin)
        first = Status.action_open_status()
        second = Status.action_open_status()
        self.assertEqual(first["res_id"], second["res_id"])
        self.assertEqual(self.Status.search_count([]), 1)
        self.assertEqual(Status.browse(first["res_id"]).state, "stale")

    def test_only_system_administrators_read_it(self):
        status = self._status(r2_ok=self._ago(hours=1))
        manager = new_test_user(self.env, login="gerencia_respaldo",
                                groups="distri_pedido.grupo_gerencia")
        portal = new_test_user(self.env, login="portal_respaldo", groups="base.group_portal")
        for user in (manager, portal):
            Status = self.env["distri.backup.status"].with_user(user)
            with self.assertRaises(AccessError):
                Status.browse(status.id).read(["state"])
            with self.assertRaises(AccessError):
                Status.action_open_status()
        admin = new_test_user(self.env, login="sistema_respaldo2", groups="base.group_system")
        self.assertEqual(self.env["distri.backup.status"].with_user(admin)
                         .browse(status.id).state, "ok")
        # the script owns the values: not even an administrator edits them
        with self.assertRaises(AccessError):
            self.env["distri.backup.status"].with_user(admin).browse(status.id).write(
                {"r2_error": False})
