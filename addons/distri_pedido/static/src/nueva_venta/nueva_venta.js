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
 * («Seguir» / «Descartar») until the sale is loaded. */
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

  function plata(n) {
    return '$ ' + Math.round(n).toLocaleString('es-AR');
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

  function guardarBorrador() {
    if (restaurando || !cliente) { return; }
    var campos = {};
    CAMPOS.forEach(function (n) { var el = form.elements[n]; if (el) { campos[n] = el.value; } });
    var data = {t: Date.now(), cliente: cliente, carrito: carrito, salida: salida(), campos: campos,
      encomienda: $('nv-encomienda').checked, sin_wa: !!($('nv-sin-wa') && $('nv-sin-wa').checked)};
    try { localStorage.setItem(KEY, JSON.stringify(data)); } catch (e) { /* full or private */ }
  }

  function ofrecerBorrador() {
    var b = leerBorrador();
    if (!b) { return; }
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
  }

  // ------------------------------------------------------------ customer

  function buscarClientes() {
    var q = $('nv-q-cliente').value;
    DN.api('/pedido/clientes?q=' + encodeURIComponent(q)).then(function (filas) {
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
      $('nv-cliente-nombre').textContent = c.nombre;
      $('nv-cliente-meta').textContent = [c.domicilio, c.whatsapp].filter(Boolean).join(' · ');
      $('nv-lista').textContent = c.lista ? '· precios de ' + c.lista : '';
      $('nv-telefono').value = c.whatsapp || '';
      $('nv-domicilio').value = c.domicilio || '';
      $('nv-elegido').hidden = false;
      $('nv-buscar-cliente').hidden = true;
      $('nv-cliente-extra').hidden = false;
      $('nv-cuenta').hidden = !c.cuenta;
      if (c.cuenta) { $('nv-cuenta').href = c.cuenta; }
      $('nv-wa').hidden = !c.sin_whatsapp;
      if (c.sin_whatsapp && !$('nv-wa-area').value && !$('nv-wa-numero').value) {
        $('nv-wa-area').value = c.wa_area || '';
        $('nv-wa-numero').value = c.wa_numero || '';
      }
      productos = data.productos;
      porId = {};
      productos.forEach(function (p) { porId[p.id] = p; });
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

  // #59 «Repetir»: the lines of one of his last orders replace the cart
  function pintarAnteriores(lista) {
    anteriores = lista;
    $('nv-anteriores-box').hidden = !lista.length;
    $('nv-anteriores').innerHTML = lista.map(function (a, i) {
      return '<div class="nv-item nv-anterior"><span><b>' + h(a.nombre) + '</b><small>' +
        h([a.fecha, a.total, a.renglones.length + (a.renglones.length === 1 ? ' producto' : ' productos')]
          .join(' · ')) + (a.fuera ? h(' · ' + a.fuera + ' ya no se pueden repetir') : '') +
        '</small></span><button type="button" class="dn-btn dn-btn--outline nv-btn-sm" data-rep="' + i +
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

  function fila(p, enCarrito) {
    var n = carrito[p.id] || 0;
    return '<div class="nv-prod' + (n ? ' on' : '') + '" data-id="' + p.id + '">' +
      '<span class="nv-prod__txt"><b>' + h(p.nombre) + '</b><small>' +
      h([p.unidad + (p.contiene ? ' x ' + p.contiene : ''),
         p.precio_texto ? '$ ' + p.precio_texto : 'precio a confirmar'].join(' · ')) +
      (enCarrito ? '' : (p.seccion ? ' · ' + h(p.seccion) : '')) + '</small></span>' +
      '<span class="nv-qty">' +
      '<button type="button" class="dn-icon-btn" data-d="-1" aria-label="Uno menos">−</button>' +
      '<input class="dn-input dn-num" inputmode="numeric" aria-label="Cantidad" value="' + (n || '') + '"/>' +
      '<button type="button" class="dn-icon-btn" data-d="1" aria-label="Uno más">+</button></span></div>';
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
    ids.forEach(function (id) { total += carrito[id] * (porId[id].precio || 0); unidades += carrito[id]; });
    $('nv-total').textContent = plata(total);
    $('nv-conteo').textContent = ids.length ? ids.length + (ids.length === 1 ? ' producto' : ' productos') +
      ', ' + unidades + (unidades === 1 ? ' unidad' : ' unidades') : '';
    $('nv-renglones').value = JSON.stringify(ids.map(function (id) {
      return {id: +id, cantidad: carrito[id]};
    }));
    listo();
  }

  function cambiar(id, valor) {
    var n = Math.max(0, Math.min(10000, Math.floor(+valor || 0)));
    if (n) { carrito[id] = n; } else { delete carrito[id]; }
    pintarCarrito();
    pintarProductos();
  }

  function enLista(ev) {
    var row = ev.target.closest('.nv-prod');
    if (!row) { return; }
    var id = row.dataset.id;
    var btn = ev.target.closest('button[data-d]');
    if (btn && ev.type === 'click') { cambiar(id, (carrito[id] || 0) + (+btn.dataset.d)); }
    if (ev.type === 'change' && ev.target.tagName === 'INPUT') { cambiar(id, ev.target.value); }
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

  function faltaWhatsapp() {
    if ($('nv-wa').hidden || salida() === 'presupuesto') { return false; }
    if ($('nv-wa-area').value.trim() || $('nv-wa-numero').value.trim()) { return false; }
    return !($('nv-sin-wa') && $('nv-sin-wa').checked);
  }

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    error('');
    if (salida() === 'entregado' && !$('nv-motivo').value.trim()) {
      error('Poné el motivo de la entrega fuera del sistema.');
      return;
    }
    if (faltaWhatsapp()) {
      error(document.body.getAttribute('data-wa-requerido') === '1'
        ? 'El cliente no tiene WhatsApp: poné el código de área y el número.'
        : 'El cliente no tiene WhatsApp: poné el código de área y el número, o marcá «No tiene WhatsApp».');
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
    $('nv-buscar-cliente').hidden = false;
    $('nv-q-cliente').focus();
    listo();
    buscarClientes();
  });
  ofrecerBorrador();
  buscarClientes();
}());
