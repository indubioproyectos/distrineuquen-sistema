"""19.0.1.129.16: the payments board with less noise. No «Cobro sin resolver
en el tablero» activity for the incomes of the report, the open ones are
deleted by the migration, and «Cierre diario» and «Sin identificar» are only
in the menu of grupo_ingresos_reporte.

Every customer, amount and operation here is fictitious."""
import importlib.util
from datetime import datetime, timedelta

from odoo import Command, fields
from odoo.tests import TransactionCase, new_test_user, tagged
from odoo.tools.misc import file_path

from odoo.addons.distri_pedido.models.cobros import ALERT_SUMMARY

GROUP = "distri_pedido.grupo_ingresos_reporte"


@tagged("post_install", "-at_install", "distri_v129_16")
class TestV12916MenosRuido(TransactionCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()

        def user(login, groups):
            return new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                 groups="base.group_user," + groups,
                                 company_id=cls.env.company.id,
                                 company_ids=[Command.set(cls.env.company.ids)])
        # Gonzalo in production: gerencia, administración, cobros, cajero
        cls.gonzalo = user("gonzalo_v129_16", "distri_pedido.grupo_gerencia,"
                           "distri_pedido.grupo_administracion,distri_pedido.grupo_cobros,"
                           "distri_pedido.grupo_cajero")
        cls.owner = user("owner_v129_16", "distri_pedido.grupo_administracion,"
                         "distri_pedido.grupo_cobros," + GROUP)
        cls.Collection = cls.env["distri.mp.collection"].sudo()
        cls.todo = cls.env.ref("mail.mail_activity_data_todo")

    _operation = 716160000000

    def _income(self, **vals):
        type(self)._operation += 1
        values = {"kind": "income", "source_id": str(self._operation), "amount": 1616.0,
                  "date": fields.Datetime.now(), "method_type": "bank_transfer"}
        values.update(vals)
        return self.Collection.create(values)

    def _no_money(self, minute):
        receipt = self.env["distri.whatsapp.message"].sudo().create({
            "wa_message_id": "wamid.test.v129_16.%s" % minute, "direction": "in",
            "wa_id": "5492995016160", "message_type": "image", "body": "$ 3.000",
            "timestamp": datetime(2026, 11, 20, 10, minute), "media_mime_type": "image/jpeg",
            "media_id": "123", "media_state": "pending"})
        return self.Collection.create({"kind": "receipt", "receipt_id": receipt.id,
                                       "amount": 3000, "date": receipt.timestamp})

    def _aged(self, cards, hours=30):
        self.env.flush_all()
        self.env.cr.execute("UPDATE distri_mp_collection SET create_date = %s WHERE id IN %s",
                            (fields.Datetime.now() - timedelta(hours=hours), tuple(cards.ids)))
        cards.invalidate_recordset(["create_date"])

    def _alerts(self, card):
        return card.activity_ids.filtered(lambda a: a.summary == ALERT_SUMMARY)

    def _schedule(self, record, summary=ALERT_SUMMARY, activity_type=None):
        return record.with_context(mail_activity_quick_update=True).sudo().activity_schedule(
            activity_type_id=(activity_type or self.todo).id, summary=summary,
            user_id=self.gonzalo.id)

    def _migrate(self):
        path = file_path("distri_pedido/migrations/19.0.1.129.16/post-migrate.py")
        spec = importlib.util.spec_from_file_location("distri_v129_16_post_migrate", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        module.migrate(self.env.cr, "19.0.1.129.15")
        self.env.invalidate_all()

    # ------------------------------------------------------------ alerts

    def test_no_alert_for_incomes_but_still_for_no_money(self):
        income = self._income()
        no_money = self._no_money(1)
        self.assertEqual((income.column, no_money.column), ("unidentified", "no_money"))
        self._aged(income | no_money)
        alerted = self.Collection._cron_alerts()
        self.assertNotIn(income, alerted)
        self.assertFalse(self._alerts(income))
        self.assertIn(no_money, alerted)
        self.assertEqual(len(self._alerts(no_money)), 1)
        self.assertEqual(self._alerts(no_money).activity_type_id, self.todo)

    # ------------------------------------------------------------ migration

    def test_migration_deletes_only_the_income_alerts(self):
        income = self._income()
        confirmed_income = self._income(state="ignored")
        no_money = self._no_money(2)
        partner = self.env["res.partner"].create({"name": "Almacén Prueba V129.16"})
        doomed = self._schedule(income) | self._schedule(confirmed_income)
        kept = (self._schedule(no_money)
                # another summary on the same income card
                | self._schedule(income, summary="Llamar al cliente")
                # the same summary with another type
                | self._schedule(income, activity_type=self.env.ref(
                    "mail.mail_activity_data_call"))
                # the same summary on another model
                | self._schedule(partner))
        messages = self.env["mail.message"].search_count([
            ("model", "=", "distri.mp.collection"), ("res_id", "in", (income | no_money).ids)])
        self._migrate()
        self.assertFalse(doomed.exists())
        self.assertEqual(kept.exists(), kept)
        self.assertEqual(self.env["mail.message"].search_count([
            ("model", "=", "distri.mp.collection"), ("res_id", "in", (income | no_money).ids)]),
            messages, "nothing in the chatter")
        # a second run finds nothing to do
        self.assertEqual(self.Collection._distri_drop_income_alerts(), 0)

    # ------------------------------------------------------------ menus

    def test_daily_close_and_unknown_menus_only_for_the_owner(self):
        Menu = self.env["ir.ui.menu"]
        group = self.env.ref(GROUP)
        for xmlid in ("distri_pedido.menu_cobros_day", "distri_pedido.menu_cobros_unknown"):
            menu = self.env.ref(xmlid)
            self.assertEqual(menu.group_ids, group, xmlid)
            self.assertNotIn(menu.id, Menu.with_user(self.gonzalo)._visible_menu_ids(), xmlid)
            self.assertIn(menu.id, Menu.with_user(self.owner)._visible_menu_ids(), xmlid)
        # the data stays: the records are still readable by their groups
        self.assertTrue(self.env.ref("distri_pedido.action_mp_day").exists())
        self.env["distri.mp.day"].with_user(self.gonzalo).search([], limit=1)
        # the «Sin identificar» counter of the board follows the menu
        counters = self.env["distri.mp.collection"].with_user(self.gonzalo).distri_board_counters()
        self.assertFalse(counters["unknown"]["visible"])
        counters = self.env["distri.mp.collection"].with_user(self.owner).distri_board_counters()
        self.assertTrue(counters["unknown"]["visible"])
