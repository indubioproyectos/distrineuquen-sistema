"""19.0.1.129.45 (release rehearsal, findings 3, 4 and 11), with ui_v2:

- «Comprobantes pendientes» for sellers: the page read the route of each
  receipt as the user and sellers have no access to distri.route, so every
  seller with a receipt in his portfolio got a 403. He now sees his
  portfolio's receipts (the record rules), read-only for the route: no
  link to the route screen, no «Anular», and never another seller's;
- saving a case from a form opened before somebody else changed it (the
  cashier's «Lo trajo») is refused instead of reverting «Repuesto», and
  «Lo trajo» is idempotent: a case never gets a second «ajuste»;
- ?ok= shows fixed notices only: free text in the URL is not echoed.
Synthetic data only."""
from odoo import Command
from odoo.exceptions import UserError
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.controllers import rendiciones_v2 as rv2
from odoo.addons.distri_pedido.tests.test_v129_44_rendiciones import CSRF_RE, V12944Mixin


@tagged("post_install", "-at_install", "distri_v129_45")
class TestV12945Rendiciones(V12944Mixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        Tag = cls.env["res.partner.category"]
        cls.tag = Tag.create({"name": "Cartera V145"})
        cls.tag_other = Tag.create({"name": "Cartera Otra V145"})
        cls.seller.sudo().etiqueta_cartera_id = cls.tag
        cls.other_seller = new_test_user(
            cls.env, login="vendedor_otro_v145", name="Vendedor Otro V145",
            groups="base.group_user,distri_pedido.grupo_vendedor", **company)
        cls.other_seller.sudo().etiqueta_cartera_id = cls.tag_other
        cls.partner.category_id = [Command.link(cls.tag.id)]
        cls.other.category_id = [Command.link(cls.tag_other.id)]

    # ------------------------------------------------------------ comprobantes

    def test_seller_sees_his_portfolio_pending_receipts(self):
        mine = self._closed_route(cash=0, transfer=1000).promise_ids
        theirs = self._closed_route(cash=0, transfer=700, driver=self.other_driver,
                                    partner=self.other).promise_ids
        self.assertTrue(mine and theirs)
        # the scenario: he reads the receipt, not its route
        self.assertTrue(mine.with_user(self.seller).has_access("read"))
        self.assertFalse(theirs.with_user(self.seller).has_access("read"))
        self.assertFalse(self.env["distri.route"].with_user(self.seller).has_access("read"))
        snap = self._snapshot(mine.route_id | theirs.route_id)
        self._login(self.seller)
        for query in ("", "?estado=todas", "?tipo=done",
                      "?repartidor=%s" % self.driver.id):
            res = self.url_open("/rendiciones/comprobantes" + query, allow_redirects=False)
            self.assertEqual(res.status_code, 200, (query, res.text[:300]))
            html = res.text
            self.assertIn('data-rd-promise="%s"' % mine.id, html, query)
            self.assertNotIn('data-rd-promise="%s"' % theirs.id, html, query)
            self.assertNotIn(self.other.name, html, "never another seller's customer")
        html = self.url_open("/rendiciones/comprobantes").text
        self.assertIn(self.partner.name, html)
        self.assertIn("Marcar llegó el comprobante", html)
        self.assertNotIn("/anular", html, "no «Anular» for sellers")
        self.assertNotIn('href="/rendiciones/ruta/', html, "no link to a screen he cannot open")
        # another seller's receipt by id: 404
        token = CSRF_RE.search(html).group(1)
        res = self.url_open("/rendiciones/comprobantes/%s/llego" % theirs.id,
                            data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 404)
        self.assertEqual(self._snapshot(mine.route_id | theirs.route_id), snap,
                         "reading changes nothing")
        # «Marcar llegó» of his own one (asked first), as the backend lets him
        before = self._ledger()
        url = "/rendiciones/comprobantes/%s/llego" % mine.id
        res = self.url_open(url, data={"csrf_token": token}, allow_redirects=False)
        self.assertEqual(res.status_code, 200, res.text[:300])
        self.assertIn(rv2.CONFIRM_ARRIVED, res.text)
        res = self.url_open(url, data={"csrf_token": token, "confirmar": "1"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertEqual(mine.state, "fulfilled")
        self.assertEqual(theirs.state, "open")
        self.assertEqual(self._ledger(), before, "no money moves")
        # administración still gets the route link and «Anular»
        self._login(self.admin)
        html = self.url_open("/rendiciones/comprobantes").text
        self.assertIn('href="/rendiciones/ruta/%s"' % theirs.route_id.id, html)
        self.assertIn("/rendiciones/comprobantes/%s/anular" % theirs.id, html)

    # ------------------------------------------------------------ stale case form

    def _shortage(self):
        """A settlement received short with «Lo trae mañana»: an open cash
        case waiting for «Lo trajo»."""
        caja = self._caja().open_session("0")
        route = self._closed_route(cash=600)
        movement = caja.with_user(self.cashier).receive_route(
            route, "500", difference_mode="manana")
        self.assertEqual(movement.route_difference, -100)
        case = movement.case_id
        self.assertTrue(case)
        self.assertFalse(case.replenished)
        return caja, movement, case

    def test_stale_case_form_does_not_revert_lo_trajo(self):
        caja, movement, case = self._shortage()
        self._login(self.admin)
        url = "/casos/%s" % case.id
        html = self.url_open(url).text
        token = CSRF_RE.search(html).group(1)
        version = rv2.case_version(case)
        self.assertIn('name="version" value="%s"' % version, html)
        # meanwhile the cashier takes the money: «Lo trajo»
        caja.with_user(self.cashier).bring_shortage(movement)
        self.assertTrue(case.replenished)
        Mov = self.env["distri.caja.movimiento"]
        adjustments = Mov.search_count([("kind", "=", "ajuste"), ("case_id", "=", case.id)])
        self.assertEqual(adjustments, 1)
        # administración saves the form she opened before, checkbox unticked
        res = self.url_open(url, data={"csrf_token": token, "version": version,
                                       "accion": "guardar", "investigacion": "Hablé con él"},
                            allow_redirects=False)
        self.assertEqual(res.status_code, 409, res.text[:300])
        self.assertIn("cambió mientras lo tenías abierto", res.text)
        self.assertIn("Hablé con él", res.text, "what she typed stays on the page")
        self.env.invalidate_all()
        self.assertTrue(case.replenished, "«Lo trajo» is not reverted")
        self.assertFalse(case.investigation)
        # the shortage is no longer pending and cannot come in twice
        self.assertNotIn(movement, self.env["distri.caja"].pending_shortages())
        with self.assertRaises(UserError):
            caja.with_user(self.cashier).bring_shortage(movement)
        # with the fresh form she saves normally
        res = self.url_open(url, data={"csrf_token": token, "version": rv2.case_version(case),
                                       "accion": "guardar", "investigacion": "Hablé con él",
                                       "repuesto": "1"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertEqual(case.investigation, "Hablé con él")
        self.assertTrue(case.replenished)
        self.assertEqual(Mov.search_count([("kind", "=", "ajuste"), ("case_id", "=", case.id)]),
                         1)

    def test_lo_trajo_is_idempotent(self):
        caja, movement, case = self._shortage()
        Caja = caja.with_user(self.cashier)
        Caja.bring_shortage(movement)
        expected = caja.expected
        # «Repuesto» unticked afterwards (e.g. the backend form): still no
        # second adjustment
        case.sudo().replenished = False
        self.assertNotIn(movement, self.env["distri.caja"].pending_shortages())
        with self.assertRaisesRegex(UserError, "ya no está pendiente|ya lo trajo"):
            Caja.bring_shortage(movement)
        self.assertEqual(caja.expected, expected)
        self.assertEqual(self.env["distri.caja.movimiento"].search_count(
            [("kind", "=", "ajuste"), ("case_id", "=", case.id)]), 1)

    def test_case_form_without_version_is_refused(self):
        _caja, _movement, case = self._shortage()
        self._login(self.admin)
        url = "/casos/%s" % case.id
        token = CSRF_RE.search(self.url_open(url).text).group(1)
        res = self.url_open(url, data={"csrf_token": token, "accion": "guardar",
                                       "investigacion": "x"}, allow_redirects=False)
        self.assertEqual(res.status_code, 409)
        self.assertFalse(case.investigation)

    # ------------------------------------------------------------ ?ok=

    def test_ok_notice_is_never_free_text(self):
        route = self._closed_route()
        case = self.env["distri.route.case"].sudo().create(
            {"route_id": route.id, "kind": "cash", "amount": -10})
        self._login(self.admin)
        fake = "Ruta X: rendida y cobrada"
        for url in ("/rendiciones", "/rendiciones/ruta/%s" % route.id, "/casos/%s" % case.id,
                    "/rendiciones/comprobantes"):
            html = self.url_open(url + "?ok=" + fake.replace(" ", "+")).text
            self.assertNotIn(fake, html, url)
            self.assertNotIn("dn-banner--ok", html, url)
        html = self.url_open("/rendiciones/ruta/%s?ok=reabierta" % route.id).text
        self.assertIn("%s: reabierta para el repartidor." % route.name, html)
        self.assertIn("Guardado.", self.url_open("/casos/%s?ok=guardado" % case.id).text)
        # the actions redirect with a code, not with text
        token = CSRF_RE.search(self.url_open("/casos/%s" % case.id).text).group(1)
        res = self.url_open("/casos/%s" % case.id, data={
            "csrf_token": token, "version": rv2.case_version(case), "accion": "guardar",
            "investigacion": "Contado"}, allow_redirects=False)
        self.assertEqual(res.status_code, 303, res.text[:300])
        self.assertIn("ok=guardado", res.headers["Location"])
