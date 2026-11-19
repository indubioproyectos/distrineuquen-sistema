{
    "name": "Distri Neuquén - Entorno de práctica",
    "summary": "Landing por rol (y cliente de práctica), ingreso sin clave y aviso de práctica (solo en distri_practica)",
    "description": """
Practice environment of practica.distrineuquen.com. Install ONLY in the
practice database distri_practica. Everything it does (the landing on «/»,
the passwordless role buttons and the orange banner) also requires the
system parameter distri_practica.activo = '1', which only the sanitize step
of /srv/odoo-lab/practica sets; anywhere else the module stays inert.
""",
    "version": "19.0.1.1.0",
    "license": "LGPL-3",
    "author": "Distri Neuquén",
    "depends": ["distri_pedido"],
    "data": [
        "data/usuarios.xml",
        "views/landing.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
