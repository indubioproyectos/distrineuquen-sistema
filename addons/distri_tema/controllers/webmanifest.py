import json

from odoo import http
from odoo.tools import file_path

from odoo.addons.web.controllers.webmanifest import WebManifest

# Navy of the brand bar: the phone paints its status bar and splash screen with it.
NAVY = "#10243f"
# Icons are owned by distri_pedido (same files as the /tablero home-screen app).
ICONS = "/distri_pedido/static/src/img/iconos/"
APP_ICON_192 = ICONS + "app-192.png"
APPLE_TOUCH_ICON = ICONS + "app-192.png"


class DistriWebManifest(WebManifest):
    """Brand the backend PWA (Odoo's «Install app») as Distri Neuquén.

    Only presentation keys change; scope, start_url, display and shortcuts
    keep Odoo's behaviour so the installed app still opens the backend.
    """

    def _get_webmanifest(self):
        manifest = super()._get_webmanifest()
        manifest.update({
            "name": "Distri Neuquén",
            "short_name": "Distri",
            "lang": "es-AR",
            "background_color": NAVY,
            "theme_color": NAVY,
            "icons": [
                {"src": ICONS + "app-192.png", "sizes": "192x192", "type": "image/png", "purpose": "any"},
                {"src": ICONS + "app-512.png", "sizes": "512x512", "type": "image/png", "purpose": "any"},
                {"src": ICONS + "app-maskable-512.png", "sizes": "512x512", "type": "image/png",
                 "purpose": "maskable"},
            ],
        })
        return manifest

    @http.route()
    def scoped_app_manifest(self, app_id, path, app_name=""):
        # single-app shortcuts ("Install Ventas"): same navy instead of Odoo purple
        response = super().scoped_app_manifest(app_id, path, app_name)
        data = json.loads(response.get_data())
        data.update(background_color=NAVY, theme_color=NAVY)
        response.set_data(json.dumps(data))
        return response

    def _icon_path(self):
        # Logo of the offline page and fallback icon of scoped apps. The file
        # belongs to distri_pedido: fall back to Odoo's if it is not around.
        path = APP_ICON_192.lstrip("/")
        try:
            file_path(path)
        except FileNotFoundError:
            return super()._icon_path()
        return path
