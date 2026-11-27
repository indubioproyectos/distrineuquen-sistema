/* «Nueva venta» (19.0.1.129.30, controllers/nueva_venta.py): the customer
 * (/pedido/clientes), the cart priced with his list (/nueva-venta/catalogo),
 * «Pegar mensaje de AgilPedido» (/nueva-venta/pegar) and the exit. The form
 * posts by fetch (DN.api) and goes where the answer says.
 *
 * 19.0.1.129.35: once the customer is picked, «Repetir» one of his last
 * orders, his account, «¿Cuál es el WhatsApp del cliente?» when he has none,
 * «Nombre y apellido» and «Registrar la ubicación (GPS)» (#56), «Va por
 * encomienda», and the draft kept on the phone (#59): customer, cart, exit
 * and answers go to localStorage on every change and are offered back
 * («Seguir» / «Descartar») until the sale is loaded.
 *
 * 19.0.1.129.40 («más rápida»): picking a customer is enough (his data are
 * in «Datos del cliente», a sheet, and «Ver ficha»; «+ Nuevo cliente» next to
 * the search); the WhatsApp question only at an exit that confirms;
 * «Elegir productos» opens /pedido's catalog on the whole screen
 * (/nueva-venta/productos, nueva_venta_productos.js), which writes the cart
 * into the draft and comes back here (?volver=1): the draft is then taken
 * up without asking, at the exit step. Tapping a product found in the
 * search adds it (or finds its line) and puts the cursor in its quantity.
 *
 * 19.0.1.129.42: the customer list shows only from the 2nd letter typed
 * (not on open, not on «Cambiar»); before that, a one-line hint.
 *
 * 19.0.1.129.59 (administración only, body data-precios="1"; the server
 * checks it again): each cart line has its unit price to change (the
 * customer's list price by default) and «Producto nuevo» creates a product
 * (/nueva-venta/producto) and puts it in the cart at its price. A changed
 * price goes with its line as `precio`; both are kept in the draft. */
(function () {
  'use strict';
  var DN = window.DN;
  var form = DN.$('[data-nv-form]');
  if (!form) { return; }
  var $ = function (id) { return document.getElementById(id); };
  var h = DN.h;
  var productos = [];
  var porId = {};
  var carrito = {};   // template id -> quantity
  var anteriores = [];
  var cliente = null; // {id, nombre}
  var espera = null;
  var KEY = 'dn:nv:borrador:' + (document.body.getAttribute('data-uid') || '0');
  var DIAS_BORRADOR = 7;
  var MAX_PRECISION = 150;  // m, models/datos_entrega.py MAX_ACCURACY_M
  // the answers kept with the draft (never the CSRF token nor `envio`)
  var CAMPOS = ['entrega', 'telefono', 'domicilio', 'comentario', 'nota_deposito', 'fecha', 'motivo',
    'contacto', 'wa_area', 'wa_numero', 'enc_nombre', 'enc_dni', 'enc_localidad'];
  var restaurando = false;
  var datos = null;   // the picked customer as /nueva-venta/catalogo sent it
  var VOLVER = 'dn:nv:volver';
  // 19.0.1.129.59
  var PRECIOS = document.body.getAttribute('data-precios') === '1';
  var precios = {};   // template id -> typed unit price
  var extras = {};    // template id -> row of a product outside the catalog

  function plata(n) {
    return '$ ' + Math.round(n).toLocaleString('es-AR');
  }

  // 19.0.1.129.59: a typed price (models/nueva_venta.py precio_valido)
  function leerPrecio(v) {
    var t = String(v == null ? '' : v).replace(/[$\s]/g, '');
    if (t.indexOf(',') !== -1) { t = t.replace(/\./g, '').replace(',', '.'); }
    var n = +t;
    return t && isFinite(n) && n > 0 && n <= 1e8 ? Math.round(n * 100) / 100 : 0;
  }
  function precioDe(id) {
    if (precios[id]) { return precios[id]; }
    return (porId[id] && porId[id].precio) || 0;
  }
  function sumarExtras() {
    Object.keys(extras).forEach(function (id) {
      if (!porId[id]) { productos.push(extras[id]); porId[id] = extras[id]; }
    });
  }

  function error(text) {
    var el = $('nv-error');
    el.querySelector('span').textContent = text || '';
    el.hidden = !text;
    if (text) { el.scrollIntoView({block: 'center'}); }
  }

  // ------------------------------------------------------------ draft (#59)

  function leerBorrador() {
    try {
      var b = JSON.parse(localStorage.getItem(KEY) || 'null');
      if (!b || !b.cliente || (Date.now() - (b.t || 0)) > DIAS_BORRADOR * 864e5) { return null; }
      return b;
    } catch (e) { return null; }
  }

  function borrarBorrador() {
    try { localStorage.removeItem(KEY); } catch (e) { /* private mode */ }
  }

  // 19.0.1.129.40: back from the picker or the customer screen
  function marcarVuelta() {
    try { sessionStorage.setItem(VOLVER, '1'); } catch (e) { /* private mode */ }
  }
  function esVuelta() {
    var url = (window.location && window.location.search) || '';
    var flag = false;
    try { flag = sessionStorage.getItem(VOLVER) === '1'; sessionStorage.removeItem(VOLVER); } catch (e) {
      /* private mode */ }
    if (/[?&]volver=/.test(url) && window.history && history.replaceState) {
      try { history.replaceState(history.state, '', window.location.pathname); } catch (e) { /* old */ }
    }
    return flag || /[?&]volver=/.test(url);
  }

  function guardarBorrador() {
    if (restaurando || !cliente) { return; }
    var campos = {};
    CAMPOS.forEach(function (n) { var el = form.elements[n]; if (el) { campos[n] = el.value; } });
    var data = {t: Date.now(), cliente: cliente, carrito: carrito, salida: salida(), campos: campos,
      precios: PRECIOS ? precios : {}, extras: PRECIOS ? extras : {},
      encomienda: $('nv-encomienda').checked, sin_wa: !!($('nv-sin-wa') && $('nv-sin-wa').checked)};
    try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (e) { /* full or private */ }
  }

  function ofrecerBorrador() {
    var b = leerBorrador();
    if (!b) { return; }
    if (esVuelta()) {
      // 19.0.1.129.40: coming back from «Elegir productos» or the customer
      // screen: the same sale goes on, nothing to ask
      elegirCliente(b.cliente, b);
      return;
    }
    var n = Object.keys(b.carrito || {}).length;
    $('nv-borrador-txt').textContent = 'Tenés una venta sin terminar: ' + b.cliente.nombre +
      (n ? ', ' + n + (n === 1 ? ' producto' : ' productos') : '') + '.';
    $('nv-borrador').hidden = false;
    $('nv-borrador-seguir').addEventListener('click', function () {
      $('nv-borrador').hidden = true;
      elegirCliente(b.cliente, b);
    });
    $('nv-borrador-descartar').addEventListener('click', function () {
      $('nv-borrador').hidden = true;
      borrarBorrador();
    });
  }

  function restaurar(b) {
    restaurando = true;
    Object.keys(b.carrito || {}).forEach(function (id) { if (porId[id]) { carrito[id] = b.carrito[id]; } });
    if (PRECIOS) {
      Object.keys(b.precios || {}).forEach(function (id) {
        if (porId[id] && leerPrecio(b.precios[id])) { precios[id] = leerPrecio(b.precios[id]); }
      });
    }
    Object.keys(b.campos || {}).forEach(function (n) {
      var el = form.elements[n];
      if (el && b.campos[n] != null) { el.value = b.campos[n]; }
    });
    if (b.salida) {
      var r = DN.$('input[name=salida][value="' + b.salida + '"]', form);
      if (r) { r.checked = true; }
    }
    $('nv-encomienda').checked = !!b.encomienda;
    if ($('nv-sin-wa')) { $('nv-sin-wa').checked = !!b.sin_wa; }
    restaurando = false;
    encomienda();
    pintarCarrito();
    if (b.paso === 'salida' && $('nv-paso-salida').scrollIntoView) {
      $('nv-paso-salida').scrollIntoView({block: 'start'});
    }
  }

  // ------------------------------------------------------------ customer

  // 19.0.1.129.42 (audit 1): no list until the 2nd letter is typed, on open
  // and on «Cambiar»; only the answer to the newest search is drawn
  var MIN_LETRAS = 2;
  var AVISO_BUSCAR = 'Escribí el nombre, el CUIT o el WhatsApp.';
  var ultimaBusqueda = 0;
  function debeBuscar(q) { return String(q || '').trim().length >= MIN_LETRAS; }
  window.DNNuevaVenta = {debeBuscar: debeBuscar, MIN_LETRAS: MIN_LETRAS};

  function buscarClientes() {
    var q = $('nv-q-cliente').value;
    var n = ++ultimaBusqueda;
    if (!debeBuscar(q)) {
      $('nv-clientes').innerHTML = '<p class="nv-meta">' + h(AVISO_BUSCAR) + '</p>';
      return;
    }
    DN.api('/pedido/clientes?q=' + encodeURIComponent(q.trim())).then(function (filas) {
      if (n !== ultimaBusqueda) { return; }
      var box = $('nv-clientes');
      if (!filas.length) { box.innerHTML = '<p class="nv-meta">Ningún cliente coincide.</p>'; return; }
      box.innerHTML = filas.map(function (f, i) {
        return '<button type="button" class="nv-item" data-i="' + i + '"><b>' + h(f.nombre) +
          '</b><small>' + h([f.ciudad, f.whatsapp, f.mio ? 'tu cliente' : ''].filter(Boolean).join(' · ')) +
          '</small></button>';
      }).join('');
      DN.$$('button', box).forEach(function (b) {
        b.addEventListener('click', function () { elegirCliente(filas[+b.dataset.i]); });
      });
    }).catch(DN.fail);
  }

  function elegirCliente(f, borrador) {
    error('');
    DN.api('/nueva-venta/catalogo?cliente_id=' + f.id).then(function (data) {
      var c = data.cliente;
      if (cliente && cliente.id !== c.id) {
        // another customer: his answers are not the previous one's
        ['wa_area', 'wa_numero', 'contacto', 'lat', 'lng', 'precision'].forEach(function (n) {
          if (form.elements[n]) { form.elements[n].value = ''; }
        });
        $('nv-gps-txt').textContent = 'Opcional: tocalo estando en el comercio.';
      }
      cliente = {id: c.id, nombre: c.nombre};
      $('nv-cliente-id').value = c.id;
      pintarCliente(c);
      $('nv-elegido').hidden = false;
      $('nv-buscar-cliente').hidden = true;
      $('nv-cliente-extra').hidden = false;
      if (c.sin_whatsapp && !$('nv-wa-area').value && !$('nv-wa-numero').value) {
        $('nv-wa-area').value = c.wa_area || '';
        $('nv-wa-numero').value = c.wa_numero || '';
      }
      productos = data.productos;
      porId = {};
      productos.forEach(function (p) { porId[p.id] = p; });
      // 19.0.1.129.59: the products made on the spot stay (any customer)
      if (PRECIOS && borrador && borrador.extras) {
        Object.keys(borrador.extras).forEach(function (id) { extras[id] = borrador.extras[id]; });
      }
      if (PRECIOS) { sumarExtras(); }
      // what is not in this customer's catalog leaves the cart
      Object.keys(carrito).forEach(function (id) { if (!porId[id]) { delete carrito[id]; } });
      pintarAnteriores(data.anteriores || []);
      $('nv-paso-carrito').hidden = false;
      $('nv-paso-salida').hidden = false;
      if (borrador) { restaurar(borrador); }
      pintarProductos();
      pintarCarrito();
    }).catch(function (e) { error(e.message); });
  }

  // what the screen shows of him (also after «Datos del cliente»)
  function pintarCliente(c) {
    datos = c;
    $('nv-cliente-nombre').textContent = c.nombre;
    $('nv-cliente-meta').textContent = [c.domicilio, c.whatsapp].filter(Boolean).join(' · ');
    $('nv-lista').textContent = c.lista ? '· precios de ' + c.lista : '';
    $('nv-telefono').value = c.whatsapp || '';
    $('nv-domicilio').value = c.domicilio || '';
    $('nv-entrega-resumen').textContent = [c.domicilio, c.whatsapp].filter(Boolean).join(' · ') ||
      '(su ficha no tiene domicilio ni teléfono)';
    $('nv-cuenta').hidden = !c.cuenta;
    if (c.cuenta) { $('nv-cuenta').href = c.cuenta; }
    $('nv-cliente-acc').hidden = false;
    $('nv-ficha').hidden = !c.ficha;
    if (c.ficha) { $('nv-ficha').href = c.ficha; }
    $('nv-elegir').href = '/nueva-venta/productos?cliente_id=' + encodeURIComponent(c.id);
    listo();
  }

  // #59 «Repetir»: the lines of one of his last orders replace the cart
  function pintarAnteriores(lista) {
    anteriores = lista;
    $('nv-anteriores-box').hidden = !lista.length;
    $('nv-anteriores').innerHTML = lista.map(function (a, i) {
      return '<div class="nv-item nv-anterior"><span><b>' + h(a.nombre) + '</b><small>' +
        h([a.fecha, a.total, a.renglones.length + (a.renglones.length === 1 ? ' producto' : ' productos')]
          .join(' · ')) + (a.fuera ? h(' · ' + a.fuera + ' ya no se pueden repetir') : '') +
        '</small></span><button type="button" class="dn-btn dn-btn--outline dn-btn--sm" data-rep="' + i +
        '">Repetir</button></div>';
    }).join('');
  }
  $('nv-anteriores').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-rep]');
    if (!b) { return; }
    var a = anteriores[+b.dataset.rep];
    carrito = {};
    a.renglones.forEach(function (r) { if (porId[r.id]) { carrito[r.id] = r.cantidad; } });
    pintarCarrito();
    pintarProductos();
    DN.toast('Se cargó ' + a.nombre + ': revisá las cantidades.', 'ok');
  });

  // #56: the phone's GPS at the shop
  $('nv-gps').addEventListener('click', function () {
    var txt = $('nv-gps-txt');
    if (!navigator.geolocation) { txt.textContent = 'Este celular no da la ubicación.'; return; }
    txt.textContent = 'Buscando la ubicación…';
    navigator.geolocation.getCurrentPosition(function (pos) {
      var c = pos.coords;
      if (c.accuracy > MAX_PRECISION) {
        txt.textContent = 'La ubicación no es precisa (±' + Math.round(c.accuracy) +
          ' m): salí a la vereda o prendé el GPS y probá de nuevo.';
        return;
      }
      $('nv-lat').value = c.latitude;
      $('nv-lng').value = c.longitude;
      $('nv-precision').value = c.accuracy;
      txt.textContent = 'Ubicación registrada (±' + Math.round(c.accuracy) + ' m). Se guarda con la venta.';
    }, function () {
      txt.textContent = 'No se pudo leer la ubicación: revisá el permiso del GPS.';
    }, {enableHighAccuracy: true, timeout: 20000, maximumAge: 0});
  });

  // ------------------------------------------------------------ cart

  // 19.0.1.129.59: the unit price of a cart line, to change (administración)
  function campoPrecio(p) {
    var cambiado = !!precios[p.id] && !p.extra;
    return '<label class="nv-precio' + (cambiado ? ' nv-precio--cambiado' : '') + '">' +
      '<span>Precio unitario $</span><input class="dn-input dn-num" data-precio="' + p.id +
      '" inputmode="decimal" enterkeyhint="done" autocomplete="off" aria-label="Precio unitario de ' + h(p.nombre) +
      '" value="' + (precioDe(p.id) ? String(precioDe(p.id)).replace('.', ',') : '') + '"/>' +
      (cambiado ? '<small>Lista: ' + (p.precio_texto ? '$ ' + h(p.precio_texto) : 'sin precio') + '</small>' : '') +
      '</label>';
  }

  function fila(p, enCarrito) {
    var n = carrito[p.id] || 0;
    var editable = PRECIOS && enCarrito;
    return '<div class="nv-prod' + (n ? ' on' : '') + (editable ? ' nv-prod--precio' : '') + '" data-id="' + p.id + '">' +
      '<span class="nv-prod__txt"' + (enCarrito ? '' : ' data-elige="1" role="button" tabindex="0"') +
      '><b>' + h(p.nombre) + '</b><small>' +
      h([p.unidad + (p.contiene ? ' x ' + p.contiene : ''),
         p.precio_texto ? '$ ' + p.precio_texto : 'precio a confirmar'].join(' · ')) +
      (enCarrito ? '' : (p.seccion ? ' · ' + h(p.seccion) : '')) + '</small></span>' +
      '<span class="nv-qty">' +
      '<button type="button" class="dn-icon-btn" data-d="-1" aria-label="Uno menos">−</button>' +
      '<input class="dn-input dn-num" inputmode="numeric" enterkeyhint="done" aria-label="Cantidad" value="' +
      (n || '') + '"/>' +
      '<button type="button" class="dn-icon-btn" data-d="1" aria-label="Uno más">+</button></span>' +
      (editable ? campoPrecio(p) : '') + '</div>';
  }

  function pintarProductos() {
    var q = $('nv-q-prod').value.trim().toLowerCase();
    var box = $('nv-productos');
    if (!q) { box.innerHTML = ''; return; }
    var vistos = productos.filter(function (p) {
      return (p.nombre + ' ' + p.seccion).toLowerCase().indexOf(q) !== -1;
    }).slice(0, 40);
    box.innerHTML = vistos.length ? vistos.map(function (p) { return fila(p, false); }).join('')
      : '<p class="nv-meta">Ningún producto coincide.</p>';
  }

  function pintarCarrito() {
    var ids = Object.keys(carrito).filter(function (id) { return carrito[id] > 0 && porId[id]; });
    $('nv-carrito').innerHTML = ids.map(function (id) { return fila(porId[id], true); }).join('');
    $('nv-carrito-vacio').hidden = ids.length > 0;
    var total = 0;
    var unidades = 0;
    ids.forEach(function (id) { total += carrito[id] * precioDe(id); unidades += carrito[id]; });
    $('nv-total').textContent = plata(total);
    $('nv-conteo').textContent = ids.length ? ids.length + (ids.length === 1 ? ' producto' : ' productos') +
      ', ' + unidades + (unidades === 1 ? ' unidad' : ' unidades') : '';
    $('nv-renglones').value = JSON.stringify(ids.map(function (id) {
      var r = {id: +id, cantidad: carrito[id]};
      if (PRECIOS && precios[id]) { r.precio = precios[id]; }
      return r;
    }));
    listo();
  }

  function cambiar(id, valor) {
    var n = Math.max(0, Math.min(10000, Math.floor(+valor || 0)));
    if (n) { carrito[id] = n; } else { delete carrito[id]; if (porId[id] && !porId[id].extra) { delete precios[id]; } }
    pintarCarrito();
    pintarProductos();
  }

  function enLista(ev) {
    var row = ev.target.closest('.nv-prod');
    if (!row) { return; }
    var id = row.dataset.id;
    var btn = ev.target.closest('button[data-d]');
    if (btn && ev.type === 'click') { cambiar(id, (carrito[id] || 0) + (+btn.dataset.d)); }
    if (!btn && ev.type === 'click' && ev.target.closest('[data-elige]')) { elegirProducto(id); }
    if (ev.type === 'change' && ev.target.hasAttribute('data-precio')) { cambiarPrecio(id, ev.target.value); return; }
    if (ev.type === 'change' && ev.target.tagName === 'INPUT') { cambiar(id, ev.target.value); }
  }
  // 19.0.1.129.59: the list price again = no change; empty = the list's
  function cambiarPrecio(id, valor) {
    var p = porId[id];
    if (!PRECIOS || !p) { return; }
    var n = leerPrecio(valor);
    error(String(valor).trim() && !n ? 'El precio de ' + p.nombre + ' tiene que ser un número mayor que cero.' : '');
    if (p.extra) {
      if (n) { precios[id] = n; }
    } else if (n && Math.abs(n - (p.precio || 0)) >= 0.005) {
      precios[id] = n;
    } else {
      delete precios[id];
    }
    pintarCarrito();
  }

  // 19.0.1.129.40: a product found in the search: in the cart once (the
  // same product again finds its line), the search cleared and the cursor
  // in its quantity, ready to type it
  function elegirProducto(id) {
    if (!porId[id]) { return; }
    if (!carrito[id]) { carrito[id] = 1; }
    $('nv-q-prod').value = '';
    pintarCarrito();
    pintarProductos();
    var row = DN.$('#nv-carrito .nv-prod[data-id="' + id + '"] input');
    if (row) {
      row.focus();
      if (row.select) { row.select(); }
    }
  }
  ['nv-productos', 'nv-carrito'].forEach(function (id) {
    $(id).addEventListener('click', enLista);
    $(id).addEventListener('change', enLista);
  });

  // ------------------------------------------------------------ exit

  function salida() {
    var r = DN.$('input[name=salida]:checked', form);
    return r ? r.value : '';
  }

  function listo() {
    var s = salida();
    DN.$$('[data-nv-para]', form).forEach(function (el) {
      el.hidden = el.dataset.nvPara.split(' ').indexOf(s) === -1;
    });
    // 19.0.1.129.40: «¿Cuál es el WhatsApp?» only at an exit that confirms
    $('nv-wa').hidden = !(datos && datos.sin_whatsapp && s && s !== 'presupuesto');
    var hay = $('nv-renglones').value !== '[]';
    $('nv-ok').disabled = !(s && hay && $('nv-cliente-id').value);
    $('nv-ok').textContent = {
      armar: 'Confirmar y mandar a Armado', mostrador: 'Vender en el mostrador',
      entregado: 'Cargar como entregada', presupuesto: 'Guardar el presupuesto'
    }[s] || 'Cargar la venta';
    encomienda();
    guardarBorrador();
  }
  DN.$$('input[name=salida]', form).forEach(function (r) { r.addEventListener('change', listo); });
  form.addEventListener('input', guardarBorrador);
  form.addEventListener('change', guardarBorrador);

  function encomienda() {
    var on = $('nv-encomienda').checked;
    var cuenta = on && (salida() === 'armar' || salida() === 'presupuesto');
    $('nv-enc').hidden = !on;
    ['nv-enc-nombre', 'nv-enc-dni', 'nv-enc-localidad'].forEach(function (id) { $(id).required = cuenta; });
  }
  $('nv-encomienda').addEventListener('change', encomienda);

  var hoy = new Date();
  var iso = hoy.getFullYear() + '-' + String(hoy.getMonth() + 1).padStart(2, '0') + '-' +
    String(hoy.getDate()).padStart(2, '0');
  $('nv-fecha').value = iso;
  $('nv-fecha').max = iso;

  // 19.0.1.129.40: needed only while distri_pedido.pedido_requiere_whatsapp is on
  function faltaWhatsapp() {
    if ($('nv-wa').hidden || salida() === 'presupuesto') { return false; }
    if (document.body.getAttribute('data-wa-requerido') !== '1') { return false; }
    return !($('nv-wa-area').value.trim() || $('nv-wa-numero').value.trim());
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    error('');
    if (salida() === 'entregado' && !$('nv-motivo').value.trim()) {
      error('Poné el motivo de la entrega fuera del sistema.');
      return;
    }
    if (faltaWhatsapp()) {
      error('El cliente no tiene WhatsApp y hace falta para confirmar: poné el código de área y el número.');
      $('nv-wa-area').focus();
      return;
    }
    if (form.checkValidity && !form.checkValidity()) { form.reportValidity(); return; }
    var ok = $('nv-ok');
    ok.disabled = true;
    DN.api('/nueva-venta/crear', {body: new FormData(form), timeout: 60000}).then(function (r) {
      borrarBorrador();
      window.location.assign(r.url);
    }).catch(function (e) {
      ok.disabled = false;
      error(e.message);
    });
  });

  // ------------------------------------------------------------ AgilPedido

  var sheet = $('nv-pegar');
  if (sheet) {
    var hoja = DN.sheet(sheet);
    $('nv-pegar-abrir').addEventListener('click', function () {
      $('nv-pegar-aviso').textContent = '';
      hoja.open();
    });
    DN.$$('[data-dn-close]', sheet).forEach(function (b) { b.addEventListener('click', hoja.close); });
    $('nv-pegar-leer').addEventListener('click', function () {
      DN.api('/nueva-venta/pegar', {body: {mensaje: $('nv-mensaje').value}}).then(function (r) {
        var faltan = [];
        r.renglones.forEach(function (x) {
          if (porId[x.id]) { carrito[x.id] = (carrito[x.id] || 0) + x.cantidad; } else { faltan.push(x.id); }
        });
        pintarCarrito();
        var avisos = r.sin_identificar.slice();
        if (faltan.length) { avisos.push(faltan.length + ' fuera del catálogo de este cliente'); }
        if (avisos.length) {
          $('nv-pegar-aviso').textContent = 'Agregalos a mano: ' + avisos.join(', ') + '.';
          DN.toast('Se cargaron ' + (r.renglones.length - faltan.length) + ' productos. Revisá los que faltan.', '');
        } else {
          hoja.close();
          DN.toast('Se cargaron ' + r.renglones.length + ' productos.', 'ok');
        }
      }).catch(function (e) { $('nv-pegar-aviso').textContent = e.message; });
    });
  }

  // ------------------------------------------------------------ «Producto nuevo» (129.59)

  var hojaProducto = PRECIOS && $('nv-producto') && DN.sheet ? DN.sheet($('nv-producto')) : null;
  if (hojaProducto) {
    DN.$$('[data-dn-close]', $('nv-producto')).forEach(function (b) { b.addEventListener('click', hojaProducto.close); });
    $('nv-producto-abrir').addEventListener('click', function () {
      $('nv-p-nombre').value = $('nv-q-prod').value.trim();
      $('nv-p-precio').value = '';
      $('nv-p-cantidad').value = '1';
      $('nv-producto-aviso').textContent = '';
      hojaProducto.open();
    });
    $('nv-producto-crear').addEventListener('click', function () {
      var nombre = $('nv-p-nombre').value.trim();
      var precio = leerPrecio($('nv-p-precio').value);
      var cantidad = Math.max(1, Math.min(10000, Math.floor(+$('nv-p-cantidad').value || 1)));
      if (nombre.length < 2) { $('nv-producto-aviso').textContent = 'Escribí el nombre del producto.'; return; }
      if (!precio) { $('nv-producto-aviso').textContent = 'Poné el precio: un número mayor que cero.'; return; }
      var btn = $('nv-producto-crear');
      btn.disabled = true;
      DN.api('/nueva-venta/producto', {body: {nombre: nombre, precio: precio}}).then(function (r) {
        btn.disabled = false;
        var p = r.producto;
        extras[p.id] = p;
        sumarExtras();
        carrito[p.id] = cantidad;
        precios[p.id] = p.precio;
        $('nv-q-prod').value = '';
        hojaProducto.close();
        pintarCarrito();
        pintarProductos();
        DN.toast('Listo: se creó ' + p.nombre + ' y está en la venta.', 'ok');
      }).catch(function (e) {
        btn.disabled = false;
        $('nv-producto-aviso').textContent = e.message;
      });
    });
  }

  // ------------------------------------------------------------ customer data (129.40)

  function hojaDe(id) {
    var el = $(id);
    if (!el || !DN.sheet) { return null; }
    var hoja = DN.sheet(el);
    DN.$$('[data-dn-close]', el).forEach(function (b) { b.addEventListener('click', hoja.close); });
    return hoja;
  }
  var hojaDatos = hojaDe('nv-datos');
  var hojaNuevo = hojaDe('nv-nuevo');

  $('nv-datos-abrir').addEventListener('click', function () {
    if (!datos || !hojaDatos) { return; }
    $('nv-datos-nombre').textContent = datos.nombre;
    $('nv-d-area').value = '';
    $('nv-d-numero').value = '';
    $('nv-d-wa-actual').textContent = datos.whatsapp ? 'WhatsApp actual: ' + datos.whatsapp +
      '. Poné otro solo si cambió.' : 'No tiene WhatsApp cargado.';
    $('nv-d-calle').value = datos.calle || '';
    $('nv-d-ciudad').value = datos.ciudad || '';
    $('nv-datos-aviso').textContent = '';
    hojaDatos.open();
  });
  $('nv-datos-guardar').addEventListener('click', function () {
    if (!datos) { return; }
    var btn = $('nv-datos-guardar');
    btn.disabled = true;
    DN.api('/nueva-venta/cliente/' + datos.id, {body: {
      wa_area: $('nv-d-area').value, wa_numero: $('nv-d-numero').value,
      calle: $('nv-d-calle').value, ciudad: $('nv-d-ciudad').value}}).then(function (r) {
      btn.disabled = false;
      pintarCliente(r.cliente);
      if (!r.cliente.sin_whatsapp) { $('nv-wa-area').value = ''; $('nv-wa-numero').value = ''; }
      hojaDatos.close();
      DN.toast('Listo: se guardaron los datos del cliente.', 'ok');
    }).catch(function (e) {
      btn.disabled = false;
      $('nv-datos-aviso').textContent = e.message;
    });
  });
  // leaving for the customer screen or the picker: the draft goes on when back
  $('nv-ficha').addEventListener('click', function () { guardarBorrador(); marcarVuelta(); });
  $('nv-elegir').addEventListener('click', function () { guardarBorrador(); marcarVuelta(); });

  var nuevoOk = false;
  function nuevoReset() {
    nuevoOk = false;
    $('nv-parecidos').hidden = true;
    $('nv-parecidos').innerHTML = '';
    $('nv-nuevo-crear').textContent = 'Crear y elegir';
    $('nv-nuevo-aviso').textContent = '';
  }
  $('nv-nuevo-abrir').addEventListener('click', function () {
    if (!hojaNuevo) { return; }
    nuevoReset();
    ['nv-n-area', 'nv-n-numero', 'nv-n-calle', 'nv-n-ciudad'].forEach(function (id) { $(id).value = ''; });
    // what was searched is most likely the name
    $('nv-n-nombre').value = $('nv-q-cliente').value.trim();
    hojaNuevo.open();
  });
  ['nv-n-nombre', 'nv-n-area', 'nv-n-numero'].forEach(function (id) {
    $(id).addEventListener('input', nuevoReset);
  });
  $('nv-parecidos').addEventListener('click', function (ev) {
    var b = ev.target.closest('button[data-cli]');
    if (!b) { return; }
    hojaNuevo.close();
    elegirCliente({id: +b.dataset.cli});
  });
  $('nv-nuevo-crear').addEventListener('click', function () {
    var btn = $('nv-nuevo-crear');
    btn.disabled = true;
    DN.api('/nueva-venta/cliente', {body: {
      nombre: $('nv-n-nombre').value, wa_area: $('nv-n-area').value, wa_numero: $('nv-n-numero').value,
      calle: $('nv-n-calle').value, ciudad: $('nv-n-ciudad').value, nuevo_ok: nuevoOk ? '1' : ''}})
      .then(function (r) {
        btn.disabled = false;
        if (r.parecidos) {
          $('nv-parecidos').innerHTML = '<p class="nv-meta"><b>Puede que ya esté cargado.</b> Si es uno de ' +
            'estos, elegilo:</p>' + r.parecidos.map(function (p) {
              return '<button type="button" class="nv-item" data-cli="' + (+p.id) + '"><b>' + h(p.name) +
                '</b><small>' + h([p.why, p.detail].filter(Boolean).join(' · ')) + '</small></button>';
            }).join('');
          $('nv-parecidos').hidden = false;
          nuevoOk = true;
          btn.textContent = 'No es ninguno: crear igual';
          return;
        }
        hojaNuevo.close();
        DN.toast('Listo: se creó ' + r.cliente.nombre + '.', 'ok');
        elegirCliente(r.cliente);
      }).catch(function (e) {
        btn.disabled = false;
        $('nv-nuevo-aviso').textContent = e.message;
      });
  });

  // ------------------------------------------------------------ start

  $('nv-q-cliente').addEventListener('input', function () {
    clearTimeout(espera);
    espera = setTimeout(buscarClientes, 250);
  });
  $('nv-q-prod').addEventListener('input', pintarProductos);
  $('nv-cambiar').addEventListener('click', function () {
    $('nv-cliente-id').value = '';
    $('nv-elegido').hidden = true;
    $('nv-cliente-extra').hidden = true;
    $('nv-cliente-acc').hidden = true;
    $('nv-buscar-cliente').hidden = false;
    $('nv-q-cliente').value = '';
    $('nv-q-cliente').focus();
    listo();
    buscarClientes();
  });
  // 129.49 hotfix «Agregar a este cliente» (order screen, counter
  // delivery): /nueva-venta?cliente=<id>[&salida=mostrador] starts a new
  // sale with that customer picked (the server checks he may be sold to)
  // and, when this user has it, the suggested exit. The query is dropped
  // once read, so a reload offers the draft as usual.
  function preseleccion() {
    var q = (window.location && window.location.search) || '';
    var c = /[?&]cliente=(\d+)/.exec(q);
    if (!c || /[?&]volver=/.test(q)) { return null; }
    var s = /[?&]salida=([a-z]+)/.exec(q);
    if (window.history && history.replaceState) {
      try { history.replaceState(history.state, '', window.location.pathname); } catch (e) { /* old */ }
    }
    return {id: +c[1], salida: s ? s[1] : ''};
  }

  // the cursor in the search when the sale starts empty (not when a draft
  // is offered or taken up)
  var pre = preseleccion();
  var sinBorrador = !leerBorrador();
  if (pre) {
    var radio = pre.salida && DN.$('input[name=salida][value="' + pre.salida + '"]', form);
    if (radio) { radio.checked = true; }
    elegirCliente({id: pre.id});
    sinBorrador = false;
  } else {
    ofrecerBorrador();
  }
  buscarClientes();
  if (sinBorrador) { $('nv-q-cliente').focus(); }
}());
