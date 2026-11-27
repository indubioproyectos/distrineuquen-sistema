"""19.0.1.129.55: the aggregate customer debt only for gerencia,
administración and Sistemas/the owner (models/cuentas_app.py ve_total_deuda).

1. The rule: gerencia, administración, Settings, the user «admin» and the
   owner's group (grupo_ingresos_reporte) see it; a seller, a seller who is
   also a cashier, a driver and an external seller do not.
2. /cuentas: the allowed roles get «Total a cobrar» and the debtor count;
   the others get the rows with every customer's balance and no total.
3. Backend lists: the «Total» footers of the partner balances and of
   «Cuenta corriente › Movimientos» only in the allowed users' views; the
   columns themselves stay for everybody who had them.

Display only: nothing here writes a payment, a move or a balance."""
from lxml import etree

from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.models.cuentas_app import (
    TOTAL_KEYS, TOTAL_VIEW_GROUPS, can_see_totals, ve_total_deuda)
from odoo.addons.distri_pedido.tests.common_pedido import ensure_company_chart

INCOME_GROUP = "distri_pedido.grupo_ingresos_reporte"


def _ledger(env):
    return env["distri.promised.transfer"]._distri_ledger_fingerprint()


@tagged("post_install", "-at_install", "distri_v129_55")
class TestTotalDeuda(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_company_chart(cls.env)
        company = cls.env.company

        def user(login, groups, **vals):
            new = new_test_user(cls.env, login=login, name=login.replace("_", " ").title(),
                                groups="base.group_user" + ("," + groups if groups else ""),
                                company_id=company.id, company_ids=[Command.set(company.ids)])
            if vals:
                new.write(vals)
            return new

        cls.allowed = {
            "gerencia": user("t55_gerencia", "distri_pedido.grupo_gerencia"),
            "administracion": user("t55_admin", "distri_pedido.grupo_administracion"),
            "sistemas": user("t55_sistemas", "base.group_system"),
            # the owner's group alone (no Settings): a seller who is the owner
            "owner": user("t55_owner", "distri_pedido.grupo_vendedor," + INCOME_GROUP),
        }
        cls.denied = {
            "vendedor": user("t55_vendedor", "distri_pedido.grupo_vendedor"),
            "vendedor_cajero": user("t55_vend_cajero",
                                    "distri_pedido.grupo_vendedor,distri_pedido.grupo_cajero"),
            "cajero": user("t55_cajero", "distri_pedido.grupo_cajero"),
            "repartidor": user("t55_reparto", "distri_pedido.grupo_reparto"),
            "externo": user("t55_externo", "distri_pedido.grupo_vendedor",
                            distri_tipo_vendedor="externo"),
        }
        cls.customer = cls.env["res.partner"].create({"name": "T55 CLIENTE DEUDOR",
                                                      "customer_rank": 1})
        cls.env["distri.cuenta.movimiento"].sudo().create([dict(
            partner_id=cls.customer.id, fecha="2025-04-10", comprobante="0001/2025/55001",
            concepto="Factura", tipo="factura", debe=955_000_000.0, haber=0.0, origen_id=55001)])
        cls.env["sale.order"].sudo().create({"partner_id": cls.customer.id,
                                             "user_id": cls.denied["externo"].id})

    def setUp(self):
        super().setUp()
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1")

    # ------------------------------------------------------------ the rule

    def test_rule(self):
        self.assertIs(can_see_totals, ve_total_deuda)
        for role, user in self.allowed.items():
            self.assertTrue(ve_total_deuda(self.env(user=user)), role)
        self.assertTrue(ve_total_deuda(self.env(user=self.env.ref("base.user_admin"))))
        for role, user in self.denied.items():
            self.assertFalse(ve_total_deuda(self.env(user=user)), role)
        self.assertFalse(ve_total_deuda(self.env(user=self.env.ref("base.public_user"))))

    # ------------------------------------------------------------ /cuentas

    def test_accounts_totals_only_for_the_allowed(self):
        before = _ledger(self.env)
        Admin = self.env["distri.account.app"].with_user(self.env.ref("base.user_admin"))
        self.assertGreaterEqual(Admin.app_list()["total_debt"], 955_000_000.0)
        for role, user in self.allowed.items():
            data = self.env["distri.account.app"].with_user(user).app_list()
            self.assertTrue(data["can_see_totals"], role)
            self.assertGreaterEqual(data["total_debt"], 955_000_000.0, role)
            self.assertIn("debtor_count", data, role)
        for role, user in self.denied.items():
            if role == "cajero":
                continue  # a cashier alone does not open /cuentas (app_scope)
            App = self.env["distri.account.app"].with_user(user)
            for data in (App.app_list(), App.app_list(limit=0), App.app_debtors()):
                self.assertFalse(data["can_see_totals"], role)
                self.assertFalse(set(TOTAL_KEYS) & set(data), role)
            row = next(r for r in App.app_list()["rows"] if r["id"] == self.customer.id)
            self.assertEqual(row["balance"], 955_000_000.0, role)
        self.assertEqual(_ledger(self.env), before)

    def test_accounts_http(self):
        for role in ("owner", "gerencia", "vendedor", "vendedor_cajero"):
            user = self.allowed.get(role) or self.denied[role]
            self.authenticate(user.login, user.login)
            res = self.url_open("/cuentas/api/list")
            self.assertEqual(res.status_code, 200, role)
            self.assertIn("T55 CLIENTE DEUDOR", res.text, role)
            self.assertEqual('"total_debt"' in res.text, role in self.allowed, role)
        self.authenticate(self.denied["cajero"].login, self.denied["cajero"].login)
        self.assertEqual(self.url_open("/cuentas/api/list").status_code, 403)

    # ------------------------------------------------------------ backend footers

    def _list_arch(self, model, view_xmlid, user):
        view = self.env.ref(view_xmlid)
        arch = self.env[model].with_user(user).get_views([(view.id, "list")])[
            "views"]["list"]["arch"]
        return etree.fromstring(arch)

    def _sum_of(self, arch, field):
        nodes = arch.xpath("//field[@name='%s']" % field)
        return nodes[0].get("sum") if nodes else None

    def test_backend_footers(self):
        cases = (("res.partner", "base.view_partner_tree", ("saldo_odoo", "monto_sin_conciliar")),
                 ("distri.cuenta.movimiento", "distri_pedido.view_cuenta_list",
                  ("debe", "haber")))
        # the allowed users that read these columns at all (VE_SALDOS, the
        # cuenta corriente's access rights)
        allowed = [self.allowed["gerencia"], self.allowed["administracion"],
                   self.allowed["owner"]]
        for model, xmlid, columns in cases:
            for user in allowed:
                arch = self._list_arch(model, xmlid, user)
                for column in columns:
                    self.assertTrue(self._sum_of(arch, column), "%s %s %s" % (
                        model, column, user.login))
            for role in ("vendedor", "vendedor_cajero", "externo"):
                arch = self._list_arch(model, xmlid, self.denied[role])
                for column in columns:
                    # the column stays (each row's balance), its footer does not
                    self.assertTrue(arch.xpath("//field[@name='%s']" % column),
                                    "%s %s %s" % (model, column, role))
                    self.assertFalse(self._sum_of(arch, column), "%s %s %s" % (
                        model, column, role))

    def test_footer_views_follow_the_rule(self):
        groups = {self.env.ref(x) for x in TOTAL_VIEW_GROUPS}
        for xmlid in ("distri_pedido.view_partner_list_saldo_total",
                      "distri_pedido.view_cuenta_list_total"):
            self.assertEqual(set(self.env.ref(xmlid).group_ids), groups, xmlid)
