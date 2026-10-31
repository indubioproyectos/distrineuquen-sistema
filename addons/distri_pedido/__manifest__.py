{
    "name": "Distri Neuquen - Toma de pedidos",
    "summary": "Carga de pedidos en una sola pantalla, sin login",
    "version": "19.0.1.63.0",
    "license": "LGPL-3",
    # website_sale aporta `is_published` en product.template, que es el
    # interruptor visible/oculto del catalogo. Sin declararlo, al instalar
    # el campo todavia no existe y la vista no valida.
    # purchase: el rol Gerencia carga compras (security/grupos.xml implica
    # purchase.group_purchase_user); sin declararlo, instalar en una base
    # vacia falla.
    "depends": ["sale_management", "sale_stock", "website", "website_sale", "purchase"],
    "data": ["security/grupos.xml", "security/ir.model.access.csv", "security/reglas.xml", "views/inicio.xml", "views/producto.xml", "views/templates.xml", "views/portal.xml", "views/partner.xml", "views/cuenta.xml", "views/estado_cuenta.xml", "views/importar.xml", "views/categoria.xml", "views/roles.xml", "views/presentacion.xml", "views/presentacion_cobranza.xml", "views/presentacion_reparto.xml", "views/proyecto_paginas.xml", "views/presentacion_migracion.xml", "views/presentacion_presupuesto.xml", "views/proyecto.xml", "views/mercado_pago.xml", "views/whatsapp.xml", "views/factura.xml", "data/whatsapp_cron.xml", "views/privacidad.xml", "views/armado.xml", "views/reparto.xml", "views/deudas_empleados.xml", "views/etiquetas.xml", "views/reparto_gps.xml", "views/reparto_app.xml", "views/deposito.xml", "views/despacho.xml", "views/socio_generico.xml", "views/cobros.xml", "views/pedido_whatsapp.xml", "views/gastos_encomiendas.xml", "views/agilpedido_wa.xml", "views/agil_equivalencia.xml", "views/puesta_al_dia.xml", "views/revision_cliente.xml", "views/ajuste_cuenta.xml", "views/tarjeta_confirmacion.xml", "views/migracion.xml", "views/visor_chat.xml", "views/deudores.xml", "data/cobros_cron.xml", "data/zonas.xml", "data/parametros.xml", "data/proyecto.xml", "data/zona_horaria.xml", "data/encomiendas.xml", "data/ventas_sin_registrar.xml", "data/ajuste_cuenta.xml"],
    "assets": {
        # ver el comentario dentro del archivo: cambia el hash de la URL del
        # paquete para esquivar un 502 que quedo cacheado en Cloudflare
        "web.assets_frontend_lazy": [
            "distri_pedido/static/src/js/cache_buster.js",
        ],
        "web.assets_backend": [
            "distri_pedido/static/src/css/cobros_backend.css",
            "distri_pedido/static/src/css/asignar_cliente.css",
            "distri_pedido/static/src/js/cobros_board.js",
            "distri_pedido/static/src/js/cobros_board.xml",
            "distri_pedido/static/src/css/chat_viewer.css",
            "distri_pedido/static/src/js/chat_viewer.js",
            "distri_pedido/static/src/js/chat_viewer.xml",
            "distri_pedido/static/src/js/deudores_form.js",
            "distri_pedido/static/src/css/card_confirm.css",
            "distri_pedido/static/src/js/card_chat.js",
            "distri_pedido/static/src/js/card_chat.xml",
        ],
    },
    "installable": True,
    "application": False,
}
