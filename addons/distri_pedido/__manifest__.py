{
    "name": "Distri Neuquen - Toma de pedidos",
    "summary": "Carga de pedidos en una sola pantalla, sin login",
    "version": "19.0.1.115.0",
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
    # 19.0.1.100.0 (owner reports on /comprobantes): optional «Importe» per
    # file (distri_amount_typed; used when the reading has none, a different
    # reading stays and is flagged «Cargado: $ X · leído: $ Y»), «Girar» per
    # photo and EXIF orientation applied before the reading (duplicates
    # still by the original bytes), «Girar» for administración on the
    # receipt form and the board card, «Ver comprobante» (/comprobantes/<id>)
    # and «Mis comprobantes» (/comprobantes/mios, last 30 days). New columns
    # only: no migration needed.
    # 19.0.1.103.0 (audit WhatsApp Web 2026-11-18, 3a step 3): «Pegar número»
    # in «WhatsApp del cliente», on the order and «Pedidos para revisar», and
    # in «Asignar cliente» (models/pegar_numero.py, views/pegar_numero.xml);
    # the phone search of «Asignar cliente» compares the 10 national digits
    # (asignar_cliente._distri_clientes_por_telefono); «Copiar código»
    # (static/src/js/copiar_codigo.*). The customer uploads his transfer
    # receipt from /pedido/<token> (models/comprobante_cliente.py,
    # views/comprobante_cliente.xml). «Salida de caja» takes a PDF too
    # (distri.caja.movimiento.document). «Cliente nuevo» in /cobro: a
    # provisional customer with the cash «a cuenta», confirmed or merged by
    # administración from /caja, the cash closing forms and Cobros › Clientes
    # nuevos a confirmar (models/cliente_provisorio.py). New columns only: no
    # migration.
    # 19.0.1.104.0 (owner decisions 17/11/2026, website phase 1): the public
    # site «/» (controllers/sitio.py, views/sitio.xml, static/src/sitio/):
    # DN header, cover, who we are, catalog categories and brands (Red Point,
    # Master and Kiel first; owner review 18/11/2026), how to order, delivery zones
    # (distri.zona), contact and the tobacco legal notice. /revendedor
    # without its code is the public «Quiero ser revendedor» page (the
    # signup stays invitation-only). Odoo's sample /terms leaves the
    # sitemap. migrations/19.0.1.104.0: the website domain moves from the
    # old piadevs host to web.base.url (robots.txt said «Disallow: /»).
    # 19.0.1.105.0 (owner request, «Reabrir mi caja»): the cashier reopens
    # his own latest closed cash session from /caja with a reason, no
    # gerente (same guard: only the latest one and no later or open one);
    # the next closing rebuilds the statement, the case and the label and
    # REPLACES the previous closing, kept in distri.caja.cierre (models/
    # caja.py). «Cajas cerradas» (/caja/cerradas, last 60 days, one session
    # read only with its movements, count, case and replaced closings) and
    # «Historial de cajas» for gerencia (/caja/historial, every cashier;
    # tile on /tablero, menu Caja). New model and columns only: no migration.
    # 19.0.1.106.0 (owner review of the site): new cover, Pexels 10834810
    # (static/src/sitio/img/portada2-*, og-distri-neuquen-2.jpg; the files
    # of the previous cover stay until this version is installed, since
    # static files are served live). «Quiero ser revendedor» becomes «Soy
    # distribuidora»: the public /revendedor page addresses only other
    # distributors; kiosks and stores order on /pedido. The invitation
    # signup (/revendedor?c=...) is unchanged. No migration.
    # 19.0.1.107.0 (owner's pick): cover Pixabay 8914266 (static/src/sitio/img
    # portada3-*, og-distri-neuquen-3.jpg; 1280 px at most, the free size).
    # On phones the cover is a photo card below the buttons on plain navy (no
    # photo behind the text); desktop keeps the card right of the text.
    # The v105 cover files are deleted; portada2-* stays until this version
    # is installed (static files are served live). No migration.
    # 19.0.1.108.0 (batch 1 of the staff screens moving to the kit, owner-
    # approved mockups): «Pedidos para revisar» on the phone (/revisar,
    # controllers/revisar.py, views/revisar_app.xml, static/src/revisar/:
    # semaphore cards, Retener with its reason, Liberar, Copiar código,
    # Abrir chat, Pegar número with the server's reading, Asignar cliente
    # through the backend wizard for now) and the new «Inicio» (/tablero on
    # the kit: glyph tiles with cheap count badges, the «pedidos en rojo»
    # banner, «Tomar un pedido» and «Pedidos para revisar»). New static
    # files and sprite symbols only; no new fields, no migration.
    # 19.0.1.109.0 (batch 2, owner-approved mockups): «Depósito» on the phone
    # (/deposito, controllers/deposito.py, views/deposito_app.xml,
    # static/src/deposito/): today's depot box, «Entrega en depósito» (with
    # or without the code, returns, cash / transfer / nothing), «Venta en
    # mostrador» and «Cierre de caja depósito», all through the backend's
    # own wizard and methods; the depot role's bottom nav (Armado · Despacho
    # · Cobro · Entregas, kit.depot_nav) and the «Depósito» tile on /tablero.
    # /caja/cierre on the depot PC (>= 1024 px): the day, the bill count and
    # the closing in one screen, Enter to the next bill, «Cerrar e imprimir»
    # opens the label. Town prefixes «C -» (Cutral Có) and «S -»
    # (Senillosa). Owner's decision: the depot cash always goes through a
    # cashier: nobody closes a depot box by hand (action_close_depot
    # refuses, the backend button is gone); a cashier receives it in /caja
    # «Recibir plata» (now also with its returns), or it closes with its own
    # cashier's «Caja del día». No new fields, no migration.
    # 19.0.1.110.0 (owner-approved mockup «pc-reparto-planificacion» and the
    # print designs of 18/11/2026): «Planificación de rutas» on the PC
    # (/rutas, controllers/rutas.py, models/planificacion.py,
    # views/rutas_app.xml, static/src/rutas/): the packed orders without a
    # route by zone (held ones locked), one column per route of the day,
    # drag or «Mover a…» an order between the depot and the routes until the
    # route is «Controlado» or leaves (an order ticked on the truck stays),
    # reorder by drag, «Nueva ruta» empty waiting for its driver (driver_id
    # is no longer required: only NOT NULL is dropped), the driver picked in
    # the column. «Hoja de ruta y pagos» and «Remitos» as QWeb PDF reports
    # (models/hoja_ruta.py, views/hoja_ruta.xml), from the board, the route
    # form and the order's Print menu; the alias holder is the new parameter
    # distri_pedido.alias_titular. No new stored fields, no migration.
    # 19.0.1.111.0 (owner-approved mockups «03 Asignar cliente», «04 Pedidos
    # de AgilPedido», «04 Cargar pedido de AgilPedido» and «pc-agil-corregir»):
    # «Asignar cliente» as a kit screen (/revisar/<id>/asignar,
    # controllers/asignar.py, views/asignar_app.xml, static/src/asignar/) on
    # the backend wizard's rules (candidates and marks, «Pegar número», the
    # verified WhatsApp, «Cliente nuevo», «Asignar y aprobar» / «Solo
    # asignar»); /revisar links to it and the v108 bridge (the server action
    # action_revisar_asignar_cliente) is gone. «Pedidos de AgilPedido» on the
    # kit (/agilpedido, controllers/agilpedido.py, views/agilpedido_app.xml,
    # static/src/agilpedido/): the orders that came by WhatsApp, an order's
    # lines with «Está bien» / «Corregir producto» and «Aprobar», and «Cargar
    # un pedido de AgilPedido» (paste, read, correct and remember, add by
    # hand, create the quotation only when nothing is left to fix); from
    # 1024 px the lines are a table with the correction panel beside it.
    # «AgilPedido» tile on /tablero with today's orders. No new stored
    # fields, no migration.
    # 19.0.1.112.0 (owner-approved mockups «05 Estado de cuenta» and
    # «pc-cuentas-deudores»): the «Cuentas» statement takes a free date range
    # besides «Último mes / 3 meses / Todo», shows the period's totals, the
    # open «Comprobantes pendientes» and «Pagos a confirmar», «Compartir PDF»
    # (the existing report_estado_cuenta through the statement wizard and its
    # F9 rules; Web Share API, else a download) and «Por WhatsApp» (a public
    # read-only link /estado-de-cuenta/<token>, signed with the database
    # secret, nothing stored, expiring after distri_pedido.estado_link_dias
    # days, 7 by default, only while the account is shown to the customer;
    # it renders the customer's own «Mi cuenta» page). From 1024 px /cuentas
    # is «Deudores y estado de cuenta»: the debtors (debtor_entries, plus the
    # oldest red invoice, FIFO) with zone, seller, minimum balance and age
    # filters beside the selected statement. The page loads
    # static/src/cuentas/cuentas_app.*; cuentas.* stay until the next version
    # (static files are served live). Bundled: the v106 cover files
    # (portada2-*, og-distri-neuquen-2.jpg) are deleted; the v108 bridge to
    # the backend «Asignar cliente» wizard is gone (the wizard stays); bulto
    # labels print the customer's own city when it is loaded, the name
    # prefix only when the city is empty; depot panel rows. No new stored
    # fields, no migration.
    # 19.0.1.113.0 (owner's tests of 18-19/11/2026): the driver app is
    # static/src/reparto/app2.js (app.js stays until the next version, static
    # files are served live): its state on the phone is kept per user and per
    # route (IndexedDB «reparto»: another user's route is never shown and
    # their pending changes are never sent as someone else; /reparto/api/sync
    # refuses a batch of another user), «Tus rutas de hoy» opens a second
    # route of the day without settling the first, and a route that is gone
    # (/reparto/api/route?id=…) answers "reset" with the current route instead
    # of a 404, keeping its pending changes. «Anular ruta» in the route form
    # (administración, gerencia; models/anular_ruta.py): a test or mistaken
    # route without money is undone (visits dropped, orders free, chatter on
    # each order) and deleted. Planning warns (never blocks) when a driver has
    # two routes of the same date and trip. A cancelled order closes its
    # «Pedido para revisar (rojo)» activities. AgilPedido: an aborted live sync
    # opens one incident (activities for administración and gerencia, a red
    # banner on /tablero) that the next good sync closes; the state lives in
    # the parameter distri_pedido.agil_incidente; a run without changes leaves
    # no record. cuentas.js / cuentas.css are no longer referenced (the files
    # are removed with the install). The pasted AgilPedido message
    # (models/importar_pedido.py): an exact full name with a pack text sold per
    # unit («… Display x 12 u.») takes its own unit; among same-name products the
    # temporary AgilPedido copy wins while the switch is on; «Corregir y
    # recordar» with the product and unit it already had learns nothing.
    # No new stored fields, no migration.
    # 19.0.1.114.0 (go-live 19/11/2026): /proyecto becomes «Capacitación Distri
    # Neuquén», the owner-approved staff training page (role picker, steps,
    # «Si pasa esto…», «Ya lo sé hacer», order cycle, «Practicá sin miedo»);
    # the former project hub lives collapsed in «Historia del proyecto».
    # Styles and script: static/src/proyecto/ through the kit route. The
    # presentations' strip goes back to /proyecto#historia. No new stored
    # fields, no migration.
    # 19.0.1.115.0 (go-live fixes, owner-approved review): package labels
    # are concurrency-safe; the driver app is static/src/reparto/app3.js
    # (Argentine amounts without Intl, a reopened route opens again; app2.js
    # stays for the cached page) and a visit's cash/transfer has a sanity
    # bound; the caja refuses a box already counted in another session and
    # drops a collection whose payment was reset or cancelled; a route is
    # settled only from a cashier's caja; remitos and wholesale customers
    # («Es mayorista», lista oculta) only for who may see them; every staff
    # role uploads receipts; staff orders take the customer's phone on file;
    # one tap = one order; «Anular ruta» only before departure; multi-role
    # users land on Inicio; «Salida de caja» ignores a twin post. No new
    # stored fields, no migration.
    "depends": ["sale_management", "sale_stock", "website", "website_sale", "purchase",
                "auth_signup", "auth_oauth"],
    "data": ["security/grupos.xml", "security/ir.model.access.csv", "security/reglas.xml", "views/sitio.xml", "views/producto.xml", "views/templates.xml", "views/portal.xml", "views/partner.xml", "views/cuenta.xml", "views/estado_cuenta.xml", "views/importar.xml", "views/categoria.xml", "views/roles.xml", "views/presentacion.xml", "views/presentacion_cobranza.xml", "views/presentacion_reparto.xml", "views/proyecto_paginas.xml", "views/presentacion_migracion.xml", "views/presentacion_presupuesto.xml", "views/proyecto.xml", "views/mercado_pago.xml", "views/whatsapp.xml", "views/factura.xml", "data/whatsapp_cron.xml", "views/privacidad.xml", "views/kit.xml", "views/armado.xml", "views/reparto.xml", "views/deudas_empleados.xml", "views/etiquetas.xml", "views/reparto_gps.xml", "views/reparto_app.xml", "views/deposito.xml", "views/despacho.xml", "views/socio_generico.xml", "views/cobros.xml", "views/pedido_whatsapp.xml", "views/whatsapp_cliente.xml", "views/revisar.xml", "views/comprobante_app.xml", "views/pedido_link.xml", "views/gastos_encomiendas.xml", "views/agilpedido_wa.xml", "views/agil_equivalencia.xml", "views/puesta_al_dia.xml", "views/revision_cliente.xml", "views/ajuste_cuenta.xml", "views/tarjeta_confirmacion.xml", "views/migracion.xml", "views/visor_chat.xml", "views/deudores.xml", "views/cuentas.xml", "views/cash_sheet.xml", "views/tablero.xml", "views/login.xml", "data/cobros_cron.xml", "data/zonas.xml", "data/parametros.xml", "data/proyecto.xml", "data/zona_horaria.xml", "data/encomiendas.xml", "data/ventas_sin_registrar.xml", "data/ajuste_cuenta.xml", "data/iconos.xml", "views/whatsapp_numero.xml", "views/catalogo_agil.xml", "data/catalogo_agil.xml", "views/respaldo.xml", "views/cobro.xml", "views/alta_revendedor.xml", "data/caja.xml", "views/caja.xml", "views/caja_backend.xml", "views/factura_sin_armar.xml",
             "views/vendedor_web.xml", "data/vendedores_web.xml",
             "views/comprobantes.xml", "views/errores.xml",
             "views/pegar_numero.xml", "views/comprobante_cliente.xml",
             "views/cliente_provisorio.xml", "views/revisar_app.xml", "views/deposito_app.xml",
             "views/rutas_app.xml", "views/hoja_ruta.xml", "views/asignar_app.xml",
             "views/agilpedido_app.xml"],
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
            "distri_pedido/static/src/js/copiar_codigo.js",
            "distri_pedido/static/src/js/copiar_codigo.xml",
        ],
    },
    "installable": True,
    "application": False,
}
