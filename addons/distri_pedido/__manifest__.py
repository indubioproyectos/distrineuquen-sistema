{
    "name": "Distri Neuquen - Toma de pedidos",
    "summary": "Carga de pedidos en una sola pantalla, sin login",
    "version": "19.0.1.129.10",
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
    # 19.0.1.116.0 (stress test on the practice copy): a pasted AgilPedido
    # «May …» line whose full name is a temporary product takes it at its own
    # price, and «Corregir producto» always resolves the line (also to a
    # «May …» product or the same one proposed); /rutas says when the
    # customer is missing; cash amounts up to $ 10.000.000.000; the route's
    # remitos follow the single remito's access rule; a failed invoice at
    # packing warns every person of administración/gerencia and shows on
    # /tablero. No new stored fields, no migration.
    # 19.0.1.117.0: «Cobros en efectivo cargados en Online» on /caja: a cash
    # payment the cashier registered in Odoo Online (the read-only mirror,
    # Online cash journals in distri_pedido.online_diarios_efectivo) enters
    # his open caja once («Traer a mi caja», kind «online», unique
    # distri.caja.movimiento.online_payment_id) without a lab payment nor an
    # account row; «Sacar de mi caja» while open; cancelled in Online, it
    # leaves the open caja. The closing books it against the «cobros de
    # Online» account (suspense by default), never the receivable. New
    # column and index created by the upgrade; migrations/19.0.1.117.0 sets
    # distri_pedido.caja_online_desde (the list starts at the first caja).
    # Also in 19.0.1.117.0 (real case S00239): /armado lists «Facturados sin
    # armar» (confirmed orders invoiced by hand before packing, last 14
    # days), which the normal list hides and route planning cannot take;
    # «Ya lo armé» asks the bultos, marks the order packed and validates its
    # depot delivery as ordered, without a second invoice
    # (models/factura_sin_armar.py). No new stored fields.
    # 19.0.1.119.0: orders of the same customer travel together on the
    # routes, each with its own invoice (never merged): /rutas shows them
    # side by side with the address, moving one moves the customer's other
    # movable orders, and a route whose customer has another open order off
    # it is warned on the board and before «Controlado» in /despacho
    # (models/planificacion.py). Also: «Bajar al depósito» on /despacho is
    # one tap (the reason is optional, «Bajado en el despacho»); /proyecto
    # and /presentacion* need an internal user (controllers/proyecto.py
    # staff_only: logged out → login, portal → 404); Inicio gets the
    # «Capacitación» tile and /proyecto «Ir a practicar». No new fields, no
    # migration.
    # 19.0.1.120.0 (owner, day 1): every packer and driver lands on Inicio
    # (res.users._distri_landing) with a tile for each screen he may open
    # (Cuentas for whoever opens the accounts app), and every staff screen
    # has «Inicio» (button next to the floating gear and first link of the
    # «Ajustes» sheet, views/kit.xml dn_ajustes); a seller locked to /cuentas
    # who also packs or drives lands on Inicio without backend tiles. Wholesale
    # customers: administración and gerencia see them and their balance in
    # /cuentas (deudores.direct_customers wholesale=True); the driver
    # delivers and collects their stops like any other, «Ver cuenta» of a
    # wholesale customer stays with administración. No new fields, no
    # migration.
    # 19.0.1.121.0 (owner, from real use): the colour of «Pedidos para
    # revisar» is the order status (sale.order.distri_revisar_color, stored,
    # from state and customer): green confirmed, yellow not confirmed, red
    # not confirmed and without a real customer, grey cancelled; sorted by
    # distri_revisar_orden. /revisar: tabs «Por confirmar» (default),
    # «Confirmados», «Retenidos», «Todos»; the risk reasons are chips of the
    # orders not confirmed yet. The risk (distri_semaforo) stays and feeds
    # those chips and the gerencia alert, minus «Sin WhatsApp», which is no
    # longer a risk. Inicio's «Pedidos para revisar» badge counts the orders
    # missing confirmation; the banner, the ones without customer. Two new
    # stored fields, computed by the upgrade from state and customer (no
    # searches, no alerts); no migration.
    # Also in 19.0.1.121.0 (owner, real order S00296): the package label of
    # a parcel (encomienda) shows only the recipient (who picks it up, DNI,
    # the parcel's town, the order's phone) and the «Remite» block (the DN
    # logo in black, the sender from the new editable parameter
    # distri_pedido.encomienda_remitente, the company line under it); the
    # package box, the order number and the barcode stay. Other labels are
    # unchanged.
    # 19.0.1.122.0 (owner): the home page «/» no longer shows the «Farmacia ·
    # Venta libre» card (controllers/sitio.py CATEGORIAS_OCULTAS_INICIO, by
    # the section key); /pedido is unchanged. «Rearmar» on the sale order
    # form (models/rearmar.py, administración; gerencia implies it): an
    # invoiced order not delivered and without payments goes back to /armado
    # as the script of 20/11/2026 did (invoice and order cancelled, a
    # confirmed copy back in the packing queue, both chatters linked). An
    # invoice with a CAE of ARCA is refused. «Inicio» lists its tiles in
    # the order of the daily work (controllers/tablero.py
    # DEFAULT_TILE_ORDER, one list a per-user order can replace later).
    # The driver app gets «Rutas anteriores» (/reparto/anteriores,
    # controllers/reparto_anteriores.py): his own routes of the last 30
    # days, read only, with each visit's result and what he collected,
    # never a customer's balance. No new fields, no migration.
    # 19.0.1.123.0 (owner, invoice 0001/2026/08157 paid by PCSH1/2026/00469
    # still red): the red of the statement is allocated by priority passes
    # (models/estado_cuenta.py mark_owed): reconciliations of this Odoo, then
    # the invoice a memo names (full number or 5-digit tail), then an exact
    # amount nearest in date, then FIFO. The red total still equals the
    # balance. Statement rows carry line_id, reconciled and memo (never
    # shown). «Deudores» keeps dating «Deuda más vieja» FIFO
    # (mark_owed_fifo). No new fields, no migration.
    # 19.0.1.124.0 (owner): a cash payment the cashier registers on the
    # invoice enters his open caja when it is posted (models/caja_pago.py,
    # same 19.0.1.118.0 rule; the route and depot box flows opt out); with
    # no open caja a note and a notice say it goes to the next one.
    # «Registrar pago» proposes the caja's journal (CSH1) to a cashier, «Banco»
    # stays. Confirming a Mercado Pago income already registered by hand in a
    # bank journal of the same customer, amount and +-2 days ties the card to
    # that payment (no second payment); two candidates stop it
    # (models/cobros.py _bank_twin_payments). Forward only: no migration, past
    # payments and cajas untouched.
    # 19.0.1.125.0 (pendientes L2): /cobro lists the invoices the statement
    # still marks owed, this Odoo's and the old Odoo Online ones («Online»),
    # and pays an Online one on its own: an unreconciled payment whose memo
    # ends with the invoice number and whose new
    # account.payment.distri_history_invoice_id links it by id to the history
    # row (never oldest first, never lab_payment_id, so the caja keeps it).
    # mark_owed clears that row by id before any memo match (pass 1b): an
    # Online number may also be a lab number (L4). Same box and journal as
    # any /cobro cash. New column only: no migration, past records untouched.
    # 19.0.1.126.0 (D11 replaced, analisis-cobro-un-paso-2026-11-21): a
    # receipt is credited when validated («Acreditar», models/acreditar.py):
    # payment in «Mercado Pago» at once, card «Acreditado · a verificar»
    # (credited_by_id, credited_at, audit_state); the report later ties to it
    # by operation, operation with one OCR slip, amount and time, or amount,
    # customer and day (unique), never a second payment. «Acreditar
    # comprobantes»: cajero, reparto, vendedor, administración, gerencia,
    # admin; customer-sent receipts wait in «Para acreditar» for
    # administración, gerencia or admin; drivers' receipts follow
    # distri_pedido.acreditar_repartidor (inmediato by default). Warning over
    # $ 100.000 in favor (distri_pedido.acreditar_aviso_saldo_a_favor). The
    # statements show «Pago confirmado» (✓) once the report tied the payment.
    # One-time notice on /tablero (res.users.distri_aviso_acreditar_visto).
    # New columns are prefetch=False and guarded (models/esquema_v126.py) so
    # a restart before -u keeps v125 behaviour. No migration: past payments,
    # cards and balances untouched.
    # 19.0.1.126.1 (practice of v126: receipts uploaded without customer or
    # left uncredited): on the staff upload (/comprobantes and «Compartir con
    # Distri», same form) the customer is mandatory; «No está en el sistema»
    # + who sent it (distri_whatsapp_message.distri_sender_note) leaves it
    # «Pendiente de asignación», no customer created, assigned and credited
    # by administración, gerencia or admin. «Subir y acreditar» uploads and
    # credits in one step with _distri_credit (same defenses; a warning is
    # confirmed with «Acreditar igual» without uploading again). Customers are
    # only SUGGESTED from the reading (payer CUIT, order/invoice number, payer
    # name; /comprobantes/sugerir before the upload), never set by
    # themselves on a staff upload. The uploader cancels (soft, image kept,
    # credit undone), replaces or turns his upload the same day while not
    # verified by MP; administración, gerencia and admin always. A cancelled
    # file can be uploaded again. Large photo with zoom on the upload screen.
    # models/subir_v126_1.py. New column prefetch=False and guarded
    # (v126_1_ready). No migration.
    # 19.0.1.126.2 (practice of v126.1: a cashier re-uploading his own photo
    # «No está en el sistema» was refused, and after cancelling it the attempt
    # still said «ya estaba»): a cancelled upload is never a duplicate on any
    # path of _distri_upload_duplicate; one still holding its unique
    # wa_message_id frees it (CANCEL_KEY) on cancel and lazily on the next
    # duplicate check. The same bytes as one's own uncredited receipt pending
    # assignment offer «Ya subiste este comprobante y quedó sin acreditar.
    # Completalo:» (/comprobantes/<id>/completar: photo, amount, suggestions,
    # customer picker, «Completar y acreditar» through _distri_credit with
    # every defense and warning; no new record). Someone else's or a credited
    # receipt is still refused, naming who uploaded it and its state.
    # models/subir_v126_2.py. No new column, no migration.
    # 19.0.1.126.3 (owner, SALAZAR YANET LORENA: invoice 0001/2026/05777 paid
    # the same day by PCSH1/2026/01889 + PBNK1/2026/03670 still red): the red
    # rule gets pass 3b «pago dividido» after the same amount and before FIFO
    # (models/estado_cuenta.py _split_payments): 2 or 3 open credits whose
    # remaining amounts add up exactly (to the cent) to an open invoice, all
    # dated from its day to 7 days after, pay it. Invoices by date; smallest
    # maximum gap, then fewer credits, then the earliest; a full tie is
    # skipped (never guessed). At most the 12 nearest credits per invoice.
    # The red total still equals the balance. No new fields, no migration.
    # 19.0.1.127.0 (owner, small improvements; models/paquete_v127.py):
    # /armado «Buscar cliente o pedido» filters the cards on the phone by
    # customer or order and hides empty zones, outside #lista so the 60 s
    # refresh keeps the query. The packer: «Armó: Juan» under the package box
    # of the common label, packed_by/packed_at/missing (also fully missing
    # lines) on the driver's stop with a compact card in app3.js, and the
    # «Armado» tab banner with /armado's sentence (sale.order
    # distri_armado_resumen, computed, not stored). Receipt photos zoom (form
    # and WhatsApp message) with «Foto en pestaña nueva»; the receipt list no
    # longer edits the amount (open_form_view). Home subtitle and meta
    # description without brands nor «farmacia». Tile «Reparto» is
    # «Rendiciones y casos» on «Rendición de caja» (the Reparto menu bar stays);
    # menu «Planificación (PC)» removed (/rutas gets «Rendiciones»).
    # Self-dispatch: administración and gerencia may control their own
    # route's load (models/despacho.py can_self_dispatch), noted once on the
    # route «Autodespacho: X controló su propia carga»; a plain driver never.
    # Customer suggestion by «0001/2026/NNNNN» and a bare 5-digit tail (the
    # v123 memo parser) in lab invoices and Online history; a tail only when
    # it names one customer. No new stored column, no migration.
    # 19.0.1.127.1 (owner): the tile «Rendiciones y casos» opens the kit page
    # /rendiciones (controllers/rendiciones.py, views/rendiciones_app.xml,
    # /deposito's look) instead of the backend «Rendición de caja»: «Recibir
    # rendiciones» (/caja/rendicion), «Rutas para rendir» (/rendiciones/rutas,
    # each route opens its backend form), «Casos», «Cargos al repartidor»,
    # «Comprobantes pendientes» (backend menus) with count badges, and
    # «Ajustes de reparto (PC)». Same access as the tile (the menu); the tile
    # still hangs from that menu, so «Reparto» stays out of the app list.
    # Until the module is updated /rendiciones sends to the backend menu.
    # 19.0.1.128.0 (docs cobros/analisis-cobro-un-paso-2026-11-21.md, audit
    # 2026-11-22 «En v128»): «Cobrar» in one step from the customer invoice
    # (wizard distri.cobrar.wizard, models/cobrar_wizard.py) and /cobro: cash
    # plus one or more transfers, «A cuenta» / «Factura del sistema» /
    # «Factura de Online», engine distri.cobro.collect (models/cobrar.py). A
    # cashier's cash goes to CSH1 and his open caja at once (v124 hook); each
    # transfer needs its receipt and is credited by the v126 machinery
    # (_distri_credit). A cashier loses «Pagar» and the «Banco» journals
    # (Gerencia/Sistemas keep them; Administración too unless also Cajero,
    # parameter distri_pedido.cajero_admin_conserva_banco). Interactive OCR
    # gives up after distri_pedido.ocr_segundos (default 7). /cobro only for
    # cajero, reparto, vendedor, administración, gerencia and Sistemas. The
    # payment opens its receipt card and its collection, the card the
    # collection's payments. The v126 rollout guards (models/esquema_v126.py)
    # are gone. New tables only (distri.cobro and its links): no migration.
    # 19.0.1.129.0 (docs cobros/analisis-cobro-un-paso-2026-11-21.md «Objetivo
    # 2», decisions 5-7; audit 2026-11-22 «En v129»): every Mercado Pago report
    # upload runs the full collections audit (models/auditoria.py): every
    # credited receipt and every electronic payment up to the last movement of
    # the reports, the match cascade, audit_state complete, «sin comprobante»,
    # duplicates, anomalies in «Auditoría de cobros» («Marcar revisado»,
    # «Anular pago»), the 3-day alert to Gerencia (by report coverage) and the
    # daily summary per person (cron). A report income that may be a credited
    # receipt of the same customer and amount (±$1, ±2 days) is never
    # confirmed with a second payment. Unique partial index on the operation
    # of credited cards plus the missing indexes of distri_mp_collection. The
    # bank-twin guard proposes the tie, a person confirms it. «Pago a
    # proveedor de Distri» (models/pago_proveedor.py): group
    # grupo_pago_proveedor, journal PPCC, list «Autorizados por Gerencia».
    # SFTP fetcher of the report (models/mp_sftp.py), cron disabled.
    # migrations/19.0.1.129.0 grants the new group to Denis and the admins.
    # 19.0.1.129.1 (owner's UX fixes to the v128 «Cobrar»): the wizard opens
    # with one empty transfer row and a clear «+ Agregar transferencia» button;
    # an empty row (no file, no amount) is ignored on collect; «Total
    # transferencias» shows only when a transfer has an amount. Each transfer
    # opens a form with its receipt big (image, or PDF in the PDF.js viewer;
    # widget distri_receipt_preview, static/src/js/receipt_preview.js). /cobro
    # previews the picked receipt big (<img>, or <iframe> for a PDF) with a tap
    # to full screen. Views and assets only: no migration.
    # 19.0.1.129.2 (owner's design of 22/11/2026, models/efectivo_a_rendir.py):
    # the cash a driver marks at a visit becomes the customer payment at once,
    # applied oldest invoice first, in the driver's own «Efectivo a rendir ·
    # <driver>» cash journal (created the first time, like «Caja depósito»),
    # linked to the stop and the route and marked «a rendir» (statement:
    # «Efectivo cobrado por el repartidor, a rendir»). An edit of the visit
    # (other amount, «No pagó», not delivered) cancels and remakes it; a resend
    # changes nothing; one live payment per stop (unique partial index). The
    # settlement reuses it (no second payment), adds it to the route's
    # payments and verifies it (✓ «Pago confirmado»); the caja counts the route
    # and its closing reconciles those payments as before, a shortfall stays
    # the driver's cash case. The audit flags an «a rendir» payment older than
    # distri_pedido.auditoria_dias_efectivo_a_rendir days (default 1) and
    # alerts Gerencia; never «sin comprobante», never matched against Mercado
    # Pago, never «Anular pago». migrations/19.0.1.129.2 leaves the routes on
    # the road or closed at the upgrade on the old path (payments at
    # settlement, `distri_cash_at_settlement`).
    # 19.0.1.129.3 (owner): the WhatsApp text the customer sends after ordering
    # follows AgilPedido's format (controllers/main.py _mensaje_whatsapp):
    # «Soy *NOMBRE* y quiero hacer el siguiente pedido hacia *DOMICILIO*:»
    # (or «para retirar en el depósito», «por encomienda a la terminal de»),
    # «- N x Producto ($ 13.760,00)» with AgilPedido's money format (thousands
    # dot from 10.000 up), «*Total: $ ...*», «Comentario:», then the compact
    # extras and, at the end, the code line and ONE link: «Tu pedido (link
    # personal, no lo compartas): <base>/p/<token>». /p/ now sends the team to
    # the backend order and everyone else to «Tu pedido» (/pedido/<access
    # token>); «Enviar al cliente» and the thank-you page use /p/ too. The
    # AgilPedido auto-import skips the page's own text (its code line or /p/
    # link, models/agilpedido_wa.py es_mensaje_de_la_pagina). No migration.
    # 19.0.1.129.4 (owner): no user-visible «Odoo»; the system is
    # «DistriNeuquén» (base Odoo is Community, LGPL). views/marca.xml: tab
    # title and favicon of web.layout, no «Powered by Odoo» (brand promotion,
    # core login card, portal sidebar, notification emails, password email,
    # digest), PDF report title, offline page. static/src/js/marca.js(.xml):
    # backend tab title, user menu without Help/Shortcuts/«My Odoo.com
    # Account», error dialog titles, session expired, toasts, «Install Odoo».
    # models/marca.py + data/marca.xml (every update): core email templates
    # cleaned in every language, system partner «DistriNeuquén», mail_bot
    # silent (no onboarding chat). controllers/marca.py: /web/database/*
    # answers 404 while list_db is off. The module's own texts: «Odoo
    # Online» is «el sistema anterior», this Odoo is «el sistema»;
    # migrations/19.0.1.129.4 renames them in the noupdate project records.
    # New dependencies (already installed): mail_bot, digest.
    # 19.0.1.129.5 (owner, after testing 19.0.1.129.3): a cleaner order
    # WhatsApp text (models/enviar_empresa.py _distri_company_message): field
    # lines «*Cliente:*», «*Dirección:*» (or «*Retira:* En el depósito», or
    # «*Encomienda:* terminal de X» + «*Retira:* nombre, DNI n»),
    # «*Teléfono:*»; one blank line between blocks; money without cents and
    # always with the thousands dot («$ 4.240»); «*Comentario:*»; at the end
    # the code line, «Tu pedido (link personal, no lo compartas):» and the
    # /p/ link on its own line. AgilPedido's money format (_plata_agil) is
    # gone. Every order reaches the company's WhatsApp (a plain phone, the
    # server cannot send to it): «Enviar a la empresa» on the seller's
    # confirmation (a pending step until tapped), on the backend order form,
    # and the customer's «Enviar por WhatsApp»; any of them sets
    # sale.order.distri_enviado_empresa + _el (POST /pedido/<token>/
    # enviado-empresa from the page). Confirmed orders not sent show a
    # warning; «Pedidos para revisar» gets the «No enviado a la empresa»
    # filter and column. migrations/19.0.1.129.5 marks the earlier orders as
    # sent (the step did not exist).
    # 19.0.1.129.6 (owner): «Enviar al cliente» sends the same text as the
    # company's WhatsApp (fields, lines, total, code and the /p/ link), so the
    # customer checks his order line by line as with AgilPedido; the link label
    # names the account only when the customer sees it. Both texts open with
    # «*Pedido:* S0xxxx» above «*Cliente:*».
    # 19.0.1.129.7 (owner): «Transferencias cargadas» under «Auditoría de
    # cobros» (now a menu with «Anomalías» and this list), for Administración,
    # Gerencia and Sistemas: every transfer receipt the staff credited
    # (credited_at, also annulled ones) or uploaded and not credited yet,
    # newest first, with who, customer, amount, operation, destination
    # invoice or «a cuenta», payment, audit_state, «sin comprobante», the
    # report income it crossed with, ✓ «Pago confirmado» and the photo
    # (models/transferencias_cargadas.py, a read-only SQL model). Report
    # incomes and customer receipts nobody credited stay out. Filters by user,
    # day, week, month, range, customer, audit state, «no cruzadas» and «con
    # anomalía»; groups with totals. «Resumen diario por persona» lines open
    # it filtered by that person and day («Ver transferencias»).
    # 19.0.1.129.8 (owner): «Transferencias» inside /cuentas, so Gerencia
    # reviews the loaded transfers from the phone: a «Transferencias» button
    # in the header (only Administración, Gerencia and Sistemas; the API
    # /cuentas/api/transfers checks the same groups and answers 403 to anyone
    # else), newest first, 50 per page with «Ver más»; period Hoy / Ayer /
    # Semana / Mes / Fechas, person and the chips Todas / No cruzadas / Con
    # anomalía / Sin comprobante, with the count, the amount and «Por
    # persona» of the filter. A row opens its receipt big (image or PDF), its
    # details and the customer's statement. Read-only, over the
    # distri.loaded.transfer model of 19.0.1.129.7 (models/
    # cuentas_transferencias.py). No migration.
    # 19.0.1.129.9 «Imputación revisada»: reviewed allocations of the red
    # rows (models/imputacion_revisada.py, distri.owed.allocation: customer,
    # credit and debit line_key, amount, confidence, reason, source,
    # approved_by, active). mark_owed applies the customer's active ones
    # first (pass 0, by the rows' stable line_key, each up to what both rows
    # still have); unknown keys or a wrong side are skipped and counted; the
    # other passes run unchanged on what is left, so the red total still
    # equals the balance. Staff see «Revisado: <reason>» in the row's review
    # note; the customer views never read it. Sellers read (their portfolio);
    # administración, gerencia and Sistemas write. Loaded with
    # scripts/rojo/cargar-imputaciones.sh (dry-run unless DO_COMMIT=1). New
    # model only: no migration.
    # Also (owner): the home «Condiciones de compra» block says «Reparto de
    # lunes a viernes, 9 a 19 hs · Feriados: consultar» and the footer adds
    # «Feriados: consultar» under the delivery line (views/sitio.xml).
    # Also (owner): /pedido follows the AgilPedido order exactly: products with
    # no section there (e.g. «Cerveza Corona 710 x 12 u.») show FIRST, always
    # open and with no heading, not last under «Otros» (agil_tienda.
    # SIN_SECCION_ORDEN = -1, the sync updates it; then section posicion and
    # the feed order inside each section, as before).
    # migrations/19.0.1.129.9 moves the stored 9999 rows to -1.
    # 19.0.1.129.10 (owner, 23/11/2026): the AgilPedido section «Vent espec
    # Distribuidoras + $ 10.000.000» stays in AgilPedido only and no longer
    # shows on /pedido (agil_tienda.SECCIONES_SOLO_AGIL, by name prefix); the
    # hidden DISTRIBUIDORAS list for authorized buyers is untouched; whoever sees the
    # hidden list (Gerencia, Administración, flagged customers) still sees it. Home:
    # «Envíos al interior · a toda la provincia del Neuquén».
    "depends": ["sale_management", "sale_stock", "website", "website_sale", "purchase",
                "auth_signup", "auth_oauth", "mail_bot", "digest"],
    "data": ["security/grupos.xml", "security/ir.model.access.csv", "security/reglas.xml", "views/sitio.xml", "views/producto.xml", "views/templates.xml", "views/portal.xml", "views/partner.xml", "views/cuenta.xml", "views/estado_cuenta.xml", "views/importar.xml", "views/categoria.xml", "views/roles.xml", "views/presentacion.xml", "views/presentacion_cobranza.xml", "views/presentacion_reparto.xml", "views/proyecto_paginas.xml", "views/presentacion_migracion.xml", "views/presentacion_presupuesto.xml", "views/proyecto.xml", "views/mercado_pago.xml", "views/whatsapp.xml", "views/factura.xml", "data/whatsapp_cron.xml", "views/privacidad.xml", "views/kit.xml", "views/armado.xml", "views/reparto.xml", "views/deudas_empleados.xml", "views/etiquetas.xml", "views/reparto_gps.xml", "views/reparto_app.xml", "views/deposito.xml", "views/despacho.xml", "views/socio_generico.xml", "views/cobros.xml", "views/pedido_whatsapp.xml", "views/whatsapp_cliente.xml", "views/revisar.xml", "views/comprobante_app.xml", "views/pedido_link.xml", "views/gastos_encomiendas.xml", "views/agilpedido_wa.xml", "views/agil_equivalencia.xml", "views/puesta_al_dia.xml", "views/revision_cliente.xml", "views/ajuste_cuenta.xml", "views/tarjeta_confirmacion.xml", "views/migracion.xml", "views/visor_chat.xml", "views/deudores.xml", "views/cuentas.xml", "views/cash_sheet.xml", "views/tablero.xml", "views/login.xml", "data/cobros_cron.xml", "data/zonas.xml", "data/parametros.xml", "data/proyecto.xml", "data/zona_horaria.xml", "data/encomiendas.xml", "data/ventas_sin_registrar.xml", "data/ajuste_cuenta.xml", "data/iconos.xml", "views/whatsapp_numero.xml", "views/catalogo_agil.xml", "data/catalogo_agil.xml", "views/respaldo.xml", "views/cobro.xml", "views/alta_revendedor.xml", "data/caja.xml", "views/caja.xml", "views/caja_backend.xml", "views/factura_sin_armar.xml",
             "views/vendedor_web.xml", "data/vendedores_web.xml",
             "views/comprobantes.xml", "views/errores.xml", "views/acreditar.xml",
             "views/cobrar.xml",
             "views/pegar_numero.xml", "views/comprobante_cliente.xml",
             "views/cliente_provisorio.xml", "views/revisar_app.xml", "views/deposito_app.xml",
             "views/rutas_app.xml", "views/hoja_ruta.xml", "views/asignar_app.xml",
             "views/agilpedido_app.xml", "views/reparto_anteriores.xml",
             "views/rendiciones_app.xml", "views/auditoria.xml", "data/auditoria_cron.xml",
             "views/efectivo_a_rendir.xml", "views/marca.xml", "data/marca.xml",
             "views/enviar_empresa.xml", "views/transferencias_cargadas.xml"],
    "assets": {
        # ver el comentario dentro del archivo: cambia el hash de la URL del
        # paquete para esquivar un 502 que quedo cacheado en Cloudflare
        "web.assets_frontend_lazy": [
            "distri_pedido/static/src/js/cache_buster.js",
        ],
        "web.assets_frontend": [
            "distri_pedido/static/src/js/marca_frontend.js",
        ],
        "web.assets_backend": [
            "distri_pedido/static/src/css/cobros_backend.css",
            "distri_pedido/static/src/css/asignar_cliente.css",
            "distri_pedido/static/src/js/cobros_board.js",
            "distri_pedido/static/src/js/cobros_board.xml",
            "distri_pedido/static/src/js/receipt_preview.js",
            "distri_pedido/static/src/js/receipt_preview.xml",
            "distri_pedido/static/src/css/chat_viewer.css",
            "distri_pedido/static/src/js/chat_viewer.js",
            "distri_pedido/static/src/js/chat_viewer.xml",
            "distri_pedido/static/src/js/deudores_form.js",
            "distri_pedido/static/src/css/card_confirm.css",
            "distri_pedido/static/src/js/card_chat.js",
            "distri_pedido/static/src/js/card_chat.xml",
            "distri_pedido/static/src/js/copiar_codigo.js",
            "distri_pedido/static/src/js/copiar_codigo.xml",
            "distri_pedido/static/src/js/marca.js",
            "distri_pedido/static/src/js/marca.xml",
        ],
    },
    "installable": True,
    "application": False,
}
