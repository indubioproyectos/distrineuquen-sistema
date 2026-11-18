{
    "name": "Distri Neuquen - Toma de pedidos",
    "summary": "Carga de pedidos en una sola pantalla, sin login",
    "version": "19.0.1.99.0",
    "license": "LGPL-3",
    # website_sale aporta `is_published` en product.template, que es el
    # interruptor visible/oculto del catalogo. Sin declararlo, al instalar
    # el campo todavia no existe y la vista no valida.
    # purchase: el rol Gerencia carga compras (security/grupos.xml implica
    # purchase.group_purchase_user); sin declararlo, instalar en una base
    # vacia falla.
    # auth_signup / auth_oauth (19.0.1.92.0): resellers sign up alone, with
    # their email or Google (models/alta_revendedor.py). Updating the module
    # installs auth_oauth; its Google provider stays disabled until the owner
    # pastes the client id. 19.0.1.93.0: that signup is private, by invitation
    # link only (migrations/19.0.1.93.0 creates the secret code).
    # 19.0.1.94.0: the Distrineuquén kit (static/src/kit, controllers/kit.py,
    # views/kit.xml) and /armado redesigned on it; sale.order.line gets
    # distri_listo (the packer's «Listo» tick, a plain new column).
    # 19.0.1.95.0: the legacy pages save without stacking browser history
    # (static/src/js/sin_historial.js, models/ir_http.py) and the new DN mark
    # (static/src/img/iconos/dn-logo.svg, navy). No migration needed.
    # 19.0.1.96.0 (audit 2026-11-17 F1/F2/F9): /pedido/enviar requires the
    # page's fields on the server too, refuses fractions and caps public
    # quantities/totals (data/parametros.xml); an anonymous order is held
    # until a customer is assigned (models/revisar.py); armador and reparto
    # read no customers nor hidden lists through the API (security/
    # reglas.xml); the statement wizard follows the portfolio. No migration.
    # 19.0.1.97.0 (audit 3c, F3, F10): «Caja del día» for the new «Cajero»
    # group (models/caja.py, caja_contabilidad.py, controllers/caja.py, page
    # /caja on the kit): cash sessions, receiving settlements and other
    # users' boxes, outflows, bill count, closing label; the closing writes a
    # cash statement that reconciles the cash payments (owner's option B).
    # «Conciliación inicial de efectivo» is a wizard run by hand. A case can
    # be against a cash session (distri.route.case.caja_id). data/caja.xml:
    # bills and account parameters (noupdate). No migration script needed.
    # 19.0.1.98.0 (audit 3b, F5, F6, F13): /armado «Tradicional» (default,
    # the order as a WhatsApp message) and «Tarjetas», order «Alfabético»
    # (natural) or «Por secciones» (product.category.distri_recorrido),
    # per-user preference on res.users; ticks/shortages/finish saved on the
    # phone and replayed idempotently (models/armado_offline.py); opening
    # an order takes it; «Más tarde» keeps the marks. Customer comment and
    # depot note on the order (/pedido, armado, revisar, driver stop);
    # «Mi vendedor es» (models/vendedor_web.py, data/vendedores_web.xml).
    # «Ajustes» gear on every custom page (views/kit.xml dn_ajustes,
    # static/src/kit/ajustes.*): name/role, Salir, letter size, install,
    # armado view. New columns get their defaults: no migration needed.
    # 19.0.1.99.0 (audit F7, F12, F4, F14, F15, F16, 3a steps 1-2, red
    # invoices): the receipt code exists from the moment the customer places
    # the order and shows on the confirmation and his order page; wrong codes
    # lock it after distri_pedido.codigo_recibido_intentos (staff unlock).
    # «Subir comprobante» (/comprobantes, models/comprobante_subir.py) and
    # «Compartir con Distri» (share_target of the /tablero manifest); PDF
    # also from the driver's app. /cobro asks before an amount over the
    # debt. Red invoices are FIFO (estado_cuenta.mark_owed_fifo); the review
    # marks are only a note. No migration needed (orders with a code from
    # before keep only its hash: «Nuevo código de recibido» shows one).
    "depends": ["sale_management", "sale_stock", "website", "website_sale", "purchase",
                "auth_signup", "auth_oauth"],
    "data": ["security/grupos.xml", "security/ir.model.access.csv", "security/reglas.xml", "views/inicio.xml", "views/producto.xml", "views/templates.xml", "views/portal.xml", "views/partner.xml", "views/cuenta.xml", "views/estado_cuenta.xml", "views/importar.xml", "views/categoria.xml", "views/roles.xml", "views/presentacion.xml", "views/presentacion_cobranza.xml", "views/presentacion_reparto.xml", "views/proyecto_paginas.xml", "views/presentacion_migracion.xml", "views/presentacion_presupuesto.xml", "views/proyecto.xml", "views/mercado_pago.xml", "views/whatsapp.xml", "views/factura.xml", "data/whatsapp_cron.xml", "views/privacidad.xml", "views/kit.xml", "views/armado.xml", "views/reparto.xml", "views/deudas_empleados.xml", "views/etiquetas.xml", "views/reparto_gps.xml", "views/reparto_app.xml", "views/deposito.xml", "views/despacho.xml", "views/socio_generico.xml", "views/cobros.xml", "views/pedido_whatsapp.xml", "views/whatsapp_cliente.xml", "views/revisar.xml", "views/comprobante_app.xml", "views/pedido_link.xml", "views/gastos_encomiendas.xml", "views/agilpedido_wa.xml", "views/agil_equivalencia.xml", "views/puesta_al_dia.xml", "views/revision_cliente.xml", "views/ajuste_cuenta.xml", "views/tarjeta_confirmacion.xml", "views/migracion.xml", "views/visor_chat.xml", "views/deudores.xml", "views/cuentas.xml", "views/cash_sheet.xml", "views/tablero.xml", "views/login.xml", "data/cobros_cron.xml", "data/zonas.xml", "data/parametros.xml", "data/proyecto.xml", "data/zona_horaria.xml", "data/encomiendas.xml", "data/ventas_sin_registrar.xml", "data/ajuste_cuenta.xml", "data/iconos.xml", "views/whatsapp_numero.xml", "views/catalogo_agil.xml", "data/catalogo_agil.xml", "views/respaldo.xml", "views/cobro.xml", "views/alta_revendedor.xml", "data/caja.xml", "views/caja.xml", "views/caja_backend.xml",
             "views/vendedor_web.xml", "data/vendedores_web.xml",
             "views/comprobantes.xml", "views/errores.xml"],
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
