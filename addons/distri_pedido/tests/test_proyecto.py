from odoo.exceptions import AccessError
from odoo.tests import HttpCase, new_test_user, tagged


@tagged("post_install", "-at_install")
class TestProyecto(HttpCase):

    def test_hub_shows_new_modules(self):
        body = self.url_open("/proyecto").text
        expected = {
            "despacho": "lab", "datos_entrega": "lab", "agilpedido_wa": "lab",
            "precios_costo": "designed", "normalizacion": "designed",
            "compras": "pending", "cambios_producto": "designed",
        }
        rows = {r["code"]: r for r in self.env["distri.project.module"].project_rows()}
        for code, stage in expected.items():
            self.assertEqual(rows[code]["stage"], stage, code)
            self.assertIn(rows[code]["name"], body)
        self.assertIn("Despacho con doble control de carga", body)

    def test_every_presentation_links_back_to_hub(self):
        for url in ("/presentacion", "/presentacion/cobranza", "/presentacion/reparto",
                    "/presentacion/migracion", "/presentacion/presupuesto"):
            response = self.url_open(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn("Volver al centro del proyecto", response.text, url)
            # 19.0.1.114.0: the stage strip goes back to the project history
            self.assertIn('href="/proyecto#historia"', response.text, url)
            self.assertIn("Volver a Capacitación", response.text, url)

    def test_budget_page_has_no_development_price(self):
        body = self.url_open("/presentacion/presupuesto").text
        self.assertNotIn("3.000.000", body)

    def test_presentations_show_their_stage(self):
        module = self.env.ref("distri_pedido.project_reparto")
        module.stage = "lab"
        for url in ("/presentacion", "/presentacion/cobranza",
                    "/presentacion/reparto", "/presentacion/migracion"):
            response = self.url_open(url)
            self.assertEqual(response.status_code, 200, url)
            self.assertIn('href="/proyecto"', response.text, url)
        self.assertIn("Probado en el lab", self.url_open("/presentacion/reparto").text)

    def test_role_maps_render(self):
        cobranza = self.url_open("/presentacion/cobranza").text
        self.assertIn('id="quien-hace-que"', cobranza)
        self.assertIn('href="#conciliacion"', cobranza)
        self.assertIn('id="conciliacion"', cobranza)
        presentacion = self.url_open("/presentacion").text
        self.assertIn('id="armador"', presentacion)
        self.assertNotIn('Etiquetas de bultos <span class="luego">Pendiente', presentacion)

    def test_strip_follows_record(self):
        rows = {r["code"]: r for r in self.env["distri.project.module"].project_rows()}
        self.assertEqual(rows["catalogo"]["stage_index"], 2)
        # 19.0.1.36.0: the labels are done (migration for the noupdate card)
        self.assertEqual(rows["etiquetas"]["stage_index"], 2)
        self.assertEqual(rows["stock"]["stage_index"], -1)
        self.assertEqual(set(rows["migracion"]) - {"code", "name", "description", "stage",
                                                   "stage_label", "stage_index", "next_step",
                                                   "url", "depends_on"}, set())

    def test_only_administration_edits(self):
        seller = new_test_user(self.env, login="vendedor_proyecto",
                               groups="base.group_user,distri_pedido.grupo_vendedor")
        with self.assertRaises(AccessError):
            self.env["distri.project.module"].with_user(seller).search([])
        admin = new_test_user(self.env, login="admin_proyecto",
                              groups="base.group_user,distri_pedido.grupo_administracion")
        module = self.env.ref("distri_pedido.project_stock").with_user(admin)
        module.stage = "designed"
        self.assertEqual(module.stage, "designed")

    def test_hub_shows_collections_status(self):
        body = self.url_open("/proyecto").text
        self.assertIn("Fase 2 del reporte", body)
        self.assertIn("Cobros por transferencia", body)
        self.assertNotIn("6:00", body)
