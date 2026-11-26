"""19.0.1.129.45 (release rehearsal, findings 5 and 10):

- /cuentas/api/list and /cuentas/deudores.(pdf|xlsx) belong to the 129.43
  screen: with ui_v2 off they answer 404 (the rest of /cuentas is as
  before); with it on, the same permissions as in 129.43;
- `min_age` / `min_balance` of «inf» or «nan» in a hand-made URL no longer
  give a 500: they are ignored like any other non-number.
Synthetic data only; nothing is written to the ledger."""
from odoo import Command
from odoo.tests import HttpCase, new_test_user, tagged

from odoo.addons.distri_pedido.controllers.kit import PARAM_UI_V2
from odoo.addons.distri_pedido.models.cuentas_app import _number, filter_debtors
from odoo.addons.distri_pedido.tests.common_pedido import ensure_company_chart


@tagged("post_install", "-at_install", "distri_v129_45")
class TestV12945Cuentas(HttpCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ensure_company_chart(cls.env)
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.admin_u = new_test_user(
            cls.env, login="cu45_admin", name="Admin Cu45",
            groups="base.group_user,distri_pedido.grupo_administracion", **company)
        cls.seller_u = new_test_user(
            cls.env, login="cu45_vendedor", name="Vendedor Cu45",
            groups="base.group_user,distri_pedido.grupo_vendedor", **company)

    def _flag(self, on):
        self.env["ir.config_parameter"].sudo().set_param(PARAM_UI_V2, "1" if on else "0")

    def _ledger(self):
        return self.env["distri.promised.transfer"]._distri_ledger_fingerprint()

    def test_list_and_exports_need_ui_v2(self):
        before = self._ledger()
        reports = self.env["distri.debtor.report"].sudo().search_count([])
        self._flag(False)
        self.authenticate(self.admin_u.login, self.admin_u.login)
        for url in ("/cuentas/api/list", "/cuentas/deudores.xlsx", "/cuentas/deudores.pdf"):
            self.assertEqual(self.url_open(url, allow_redirects=False).status_code, 404, url)
        self.assertEqual(self.env["distri.debtor.report"].sudo().search_count([]), reports,
                         "flag off: the export creates nothing")
        # the rest of /cuentas is as before
        self.assertEqual(self.url_open("/cuentas").status_code, 200)
        self.assertEqual(self.url_open("/cuentas/api/debtors").status_code, 200)
        # flag on: the 129.43 permissions
        self._flag(True)
        self.assertEqual(self.url_open("/cuentas/api/list").status_code, 200)
        res = self.url_open("/cuentas/deudores.xlsx", allow_redirects=False)
        self.assertIn(res.status_code, (302, 303))
        self.authenticate(self.seller_u.login, self.seller_u.login)
        self.assertEqual(self.url_open("/cuentas/api/list").status_code, 200)
        self.assertEqual(self.url_open("/cuentas/deudores.xlsx", allow_redirects=False)
                         .status_code, 403)
        self._flag(False)
        self.assertEqual(self.url_open("/cuentas/api/list").status_code, 404)
        self.assertEqual(self._ledger(), before)

    def test_non_finite_filters_are_ignored(self):
        for value in ("inf", "-inf", "nan", "Infinity", "1e400"):
            self.assertEqual(_number(value), 0.0, value)
        self.assertEqual(_number("30"), 30.0)
        self.assertEqual(filter_debtors([], min_balance="inf", min_age="nan"), [])
        self._flag(True)
        self.authenticate(self.admin_u.login, self.admin_u.login)
        for query in ("min_age=inf", "min_age=nan", "min_balance=inf&min_age=-inf"):
            res = self.url_open("/cuentas/api/list?" + query)
            self.assertEqual(res.status_code, 200, (query, res.text[:300]))
            data = res.json()["data"]
            self.assertEqual((data["min_age"], data["min_balance"]), (0, 0.0), query)
            res = self.url_open("/cuentas/api/debtors?" + query)
            self.assertEqual(res.status_code, 200, (query, res.text[:300]))
