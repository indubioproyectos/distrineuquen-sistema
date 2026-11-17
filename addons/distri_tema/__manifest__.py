{
    "name": "Distri Neuquén - Apariencia",
    "summary": "Colores, tipografía, menús y uso en el celular (app instalable con la marca)",
    "author": "Distri Neuquén",
    "version": "19.0.1.1.0",
    "license": "LGPL-3",
    "depends": ["web", "account", "sale_management"],
    "data": [
        "data/menus.xml",
        "views/res_partner_views.xml",
        "views/webclient_templates.xml",
    ],
    "assets": {
        # Variables de color: se anteponen para que ganen a los !default de
        # web. Este paquete lo usan backend, login y reportes; las páginas
        # públicas del proyecto (/proyecto, /presentacion, /pedido, /reparto)
        # tienen su propio CSS y no lo cargan.
        "web._assets_primary_variables": [
            ("prepend", "distri_tema/static/src/scss/primary_variables.scss"),
        ],
        # La fuente se sirve desde el propio Odoo (sin Google Fonts) y solo
        # en el backend; en otros lados cae a la fuente del sistema.
        # backend.scss: marca en todos los tamaños; movil.scss: celular
        # (< 768px) y app instalada (zonas seguras de la pantalla).
        "web.assets_backend": [
            "distri_tema/static/src/scss/figtree.scss",
            "distri_tema/static/src/scss/backend.scss",
            "distri_tema/static/src/scss/movil.scss",
        ],
    },
    "installable": True,
    "application": False,
}
