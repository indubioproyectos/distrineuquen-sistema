"""19.0.1.129.45 (release rehearsal, findings 2 and 8), «Contactos» with ui_v2:

- an internal seller without a portfolio tag who creates a contact used to
  get a 404 (he cannot read it: security/reglas.xml). The customer is his
  (salesperson) and «a confirmar»; he now goes back to /contactos with a
  fixed notice;
- administración's «Editar» no longer clears the customer's salesperson
  when he is not among the options (left the seller group, archived): the
  current one is always offered, an empty or missing value keeps it, and
  only «Sin vendedor» (an explicit choice) clears it.
Synthetic data only; the ledger is unchanged."""
import re

from odoo import Command
from odoo.tests import new_test_user, tagged

from odoo.addons.account.tests.common import AccountTestInvoicingHttpCommon
from odoo.addons.distri_pedido.models import contactos as ct
from odoo.addons.distri_pedido.tests.test_v129_41_contactos import V12941Mixin

SELECT_RE = re.compile(r'<select[^>]*name="vendedor"[^>]*>(.*?)</select>', re.S)
OPTION_RE = re.compile(r'<option([^>]*)>')
VALUE_RE = re.compile(r'value="([^"]*)"')


@tagged("post_install", "-at_install", "distri_v129_45")
class TestV12945Contactos(V12941Mixin, AccountTestInvoicingHttpCommon):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        company = {"company_id": cls.env.company.id,
                   "company_ids": [Command.set(cls.env.company.ids)]}
        cls.sin_cartera = new_test_user(
            cls.env, login="sincartera_v145", name="Sin Cartera V145",
            groups="base.group_user,distri_pedido.grupo_vendedor", **company)
        # a former seller: no longer in the seller group
        cls.ex_vendedor = new_test_user(
            cls.env, login="exvendedor_v145", name="Ex Vendedor V145",
            groups="base.group_user", **company)
        cls.del_ex = cls.env["res.partner"].create({
            "name": "Almacen Del Ex V145", "company_type": "company", "customer_rank": 1,
            "user_id": cls.ex_vendedor.id, "city": "Neuquén"})

    def setUp(self):
        super().setUp()
        self._flag()

    def _browser_vendedor(self, html):
        """What a browser posts for the «Vendedor» select: the selected
        option, or the first one when none is selected."""
        options = OPTION_RE.findall(SELECT_RE.search(html).group(1))
        chosen = next((o for o in options if "selected" in o), options[0])
        return VALUE_RE.search(chosen).group(1)

    # ------------------------------------------------------------ new contact

    def test_seller_without_portfolio_creates_and_lands_on_the_list(self):
        before = self._ledger()
        self.assertFalse(self.sin_cartera.etiqueta_cartera_id)
        self._login(self.sin_cartera)
        res = self._post("/contactos/nuevo", nombre="Despensa Sin Cartera V145", nuevo_ok="1",
                         wa_area="299", wa_numero="4511450", calle="Alem 145",
                         ciudad="Neuquén")
        self.assertEqual(res.status_code, 303, res.text[:300])
        nuevo = self.env["res.partner"].search([("name", "=", "Despensa Sin Cartera V145")])
        self.assertEqual(len(nuevo), 1)
        self.assertEqual((nuevo.user_id, nuevo.create_uid), (self.sin_cartera, self.sin_cartera))
        self.assertTrue(nuevo.distri_cliente_provisorio, "«a confirmar»")
        self.assertEqual(nuevo.distri_provisorio_user_id, self.sin_cartera)
        location = res.headers["Location"]
        self.assertIn("/contactos?hecho=creado_confirmar", location)
        page = self.url_open(location)
        self.assertEqual(page.status_code, 200, page.text[:300])
        self.assertIn("quedó cargado a tu nombre", page.text)
        self.assertIn("a confirmar", page.text)
        # the notice is a fixed text: free text in the URL is not shown
        page = self.url_open("/contactos?hecho=Cliente+falso+cargado")
        self.assertEqual(page.status_code, 200)
        self.assertNotIn("Cliente falso cargado", page.text)
        self.assertNotIn("dn-banner--ok", page.text)
        self.assertEqual(self._ledger(), before)

    def test_seller_with_portfolio_still_opens_his_new_customer(self):
        self._login(self.vendedor)
        res = self._post("/contactos/nuevo", nombre="Despensa Con Cartera V145", nuevo_ok="1")
        self.assertEqual(res.status_code, 303, res.text[:300])
        nuevo = self.env["res.partner"].search([("name", "=", "Despensa Con Cartera V145")])
        self.assertIn("/clientes/%s" % nuevo.id, res.headers["Location"])
        self.assertEqual(self.url_open(res.headers["Location"]).status_code, 200)

    # ------------------------------------------------------------ edit

    def test_edit_keeps_a_salesperson_who_is_not_an_option(self):
        before = self._ledger()
        self._login(self.admin)
        url = "/clientes/%s/editar" % self.del_ex.id
        html = self.url_open(url).text
        # the current salesperson is offered and selected
        self.assertEqual(self._browser_vendedor(html), str(self.ex_vendedor.id))
        self.assertIn("Ex Vendedor V145 (ya no es vendedor)", html)
        # «Guardar» as the browser sends it, only the phone changed
        res = self._post(url, nombre=self.del_ex.name, ciudad="Neuquén",
                         telefono="0299 442-1450", vendedor=self._browser_vendedor(html))
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertTrue(self.del_ex.phone)
        self.assertEqual(self.del_ex.user_id, self.ex_vendedor, "the salesperson stays")
        # an empty or missing value keeps him too
        for extra in ({"vendedor": ""}, {}):
            res = self._post(url, nombre=self.del_ex.name, ciudad="Neuquén", **extra)
            self.assertEqual(res.status_code, 303, res.text[:500])
            self.assertEqual(self.del_ex.user_id, self.ex_vendedor, extra)
        # «Sin vendedor», chosen on purpose, clears him
        res = self._post(url, nombre=self.del_ex.name, ciudad="Neuquén",
                         vendedor=ct.SIN_VENDEDOR)
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertFalse(self.del_ex.user_id)
        html = self.url_open(url).text
        self.assertEqual(self._browser_vendedor(html), ct.SIN_VENDEDOR)
        self.assertNotIn("(ya no es vendedor)", html)
        # and picking a seller still works
        res = self._post(url, nombre=self.del_ex.name, vendedor=str(self.vendedor.id))
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertEqual(self.del_ex.user_id, self.vendedor)
        # a value that is not an option is refused
        res = self._post(url, nombre=self.del_ex.name, vendedor=str(self.armador.id))
        self.assertEqual(res.status_code, 400)
        self.assertEqual(self.del_ex.user_id, self.vendedor)
        self.assertEqual(self._ledger(), before)

    def test_edit_of_an_archived_salesperson_keeps_him(self):
        gone = new_test_user(self.env, login="baja_v145", name="Dado De Baja V145",
                             groups="base.group_user,distri_pedido.grupo_vendedor")
        partner = self.env["res.partner"].create({
            "name": "Kiosco Del Que Se Fue V145", "company_type": "company",
            "customer_rank": 1, "user_id": gone.id})
        gone.sudo().active = False
        self._login(self.admin)
        url = "/clientes/%s/editar" % partner.id
        html = self.url_open(url).text
        self.assertEqual(self._browser_vendedor(html), str(gone.id))
        res = self._post(url, nombre=partner.name, vendedor=self._browser_vendedor(html),
                         calle="Roca 145")
        self.assertEqual(res.status_code, 303, res.text[:500])
        self.assertEqual(partner.street, "Roca 145")
        self.assertEqual(partner.with_context(active_test=False).user_id, gone)
