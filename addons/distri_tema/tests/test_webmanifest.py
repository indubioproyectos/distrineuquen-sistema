from odoo.tests import HttpCase, new_test_user, tagged

NAVY = "#10243f"
ICONS = "/distri_pedido/static/src/img/marca/"


@tagged("post_install", "-at_install")
class TestWebManifest(HttpCase):

    def test_manifest_is_branded(self):
        response = self.url_open("/web/manifest.webmanifest")
        self.assertEqual(response.status_code, 200)
        self.assertIn("application/manifest+json", response.headers["Content-Type"])
        data = response.json()
        self.assertEqual(data["name"], "Distri Neuquén")
        self.assertEqual(data["short_name"], "Distri")
        self.assertEqual(data["background_color"], NAVY)
        self.assertEqual(data["theme_color"], NAVY)
        # Odoo's behaviour is kept: the installed app opens the backend
        self.assertEqual(data["scope"], "/odoo")
        self.assertEqual(data["start_url"], "/odoo")
        self.assertEqual(data["display"], "standalone")
        self.assertIn("shortcuts", data)
        icons = {(icon["src"], icon["sizes"], icon["purpose"]) for icon in data["icons"]}
        self.assertEqual(icons, {
            (ICONS + "dn-icono-192.png", "192x192", "any"),
            (ICONS + "dn-icono-512.png", "512x512", "any"),
        })
        self.assertNotIn("714B67", response.text.upper())

    def test_scoped_app_manifest_is_navy(self):
        data = self.url_open("/web/manifest.scoped_app_manifest?app_id=sale&path=/odoo/sales").json()
        self.assertEqual(data["theme_color"], NAVY)
        self.assertEqual(data["background_color"], NAVY)

    def test_backend_head_is_branded(self):
        new_test_user(self.env, login="distri_tema_head", password="distri_tema_head_1",
                      groups="base.group_user")
        self.authenticate("distri_tema_head", "distri_tema_head_1")
        # bare /odoo is redirected to /tablero by distri_pedido: open a backend URL
        html = self.url_open("/odoo/discuss").text
        self.assertIn('<meta name="theme-color" content="#10243f"/>', html)
        self.assertIn('rel="apple-touch-icon" href="%sdn-icono-180.png?v=129.12"' % ICONS, html)
        self.assertIn('rel="shortcut icon" href="%sfavicon-32.png?v=129.12"' % ICONS, html)
        self.assertIn("viewport-fit=cover", html)
        self.assertNotIn("#71639e", html)


@tagged("post_install", "-at_install", "distri_v129_57")
class TestScopedManifestWithoutArguments(HttpCase):
    """19.0.1.1.2 (general test run of 27/11/2026): the public
    /web/manifest.scoped_app_manifest answered 500 (TypeError, traceback in
    the log) when asked without app_id or path."""

    def test_missing_arguments_are_a_bad_request(self):
        for query in ("", "?app_id=sale", "?path=/odoo/sales", "?app_id=&path="):
            with self.subTest(query=query):
                response = self.url_open("/web/manifest.scoped_app_manifest" + query)
                self.assertEqual(response.status_code, 400)

    def test_complete_request_still_answers(self):
        response = self.url_open("/web/manifest.scoped_app_manifest?app_id=sale&path=/odoo/sales")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["theme_color"], NAVY)
