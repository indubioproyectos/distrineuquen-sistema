/* «Nueva venta» (19.0.1.129.30, controllers/nueva_venta.py): the customer
 * (/pedido/clientes), the cart priced with his list (/nueva-venta/catalogo),
 * «Pegar mensaje de AgilPedido» (/nueva-venta/pegar) and the exit. The form
 * posts by fetch (DN.api) and goes where the answer says. */
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
  var espera = null;

  function plata(n) {
    return '$ ' + Math.round(n).toLocaleString('es-AR');
  }

  function error(text) {
    var el = $('nv-error');
    el.querySelector('span').textContent = text || '';
    el.hidden = !text;
    if (text) { el.scrollIntoView({block: 'center'}); }
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

  function elegirCliente(f) {
    error('');
    DN.api('/nueva-venta/catalogo?cliente_id=' + f.id).then(function (data) {
      $('nv-cliente-id').value = data.cliente.id;
      $('nv-cliente-nombre').textContent = data.cliente.nombre;
      $('nv-cliente-meta').textContent = [data.cliente.domicilio, data.cliente.whatsapp].filter(Boolean).join(' · ');
      $('nv-lista').textContent = data.cliente.lista ? '· precios de ' + data.cliente.lista : '';
      $('nv-telefono').value = data.cliente.whatsapp || '';
      $('nv-domicilio').value = data.cliente.domicilio || '';
      $('nv-elegido').hidden = false;
      $('nv-buscar-cliente').hidden = true;
      productos = data.productos;
      porId = {};
      productos.forEach(function (p) { porId[p.id] = p; });
      // what is not in this customer's catalog leaves the cart
      Object.keys(carrito).forEach(function (id) { if (!porId[id]) { delete carrito[id]; } });
      $('nv-paso-carrito').hidden = false;
      $('nv-paso-salida').hidden = false;
      pintarProductos();
      pintarCarrito();
    }).catch(function (e) { error(e.message); });
  }

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
  }
  DN.$$('input[name=salida]', form).forEach(function (r) { r.addEventListener('change', listo); });

  var hoy = new Date();
  var iso = hoy.getFullYear() + '-' + String(hoy.getMonth() + 1).padStart(2, '0') + '-' +
    String(hoy.getDate()).padStart(2, '0');
  $('nv-fecha').value = iso;
  $('nv-fecha').max = iso;

  form.addEventListener('submit', function (ev) {
    ev.preventDefault();
    error('');
    if (salida() === 'entregado' && !$('nv-motivo').value.trim()) {
      error('Poné el motivo de la entrega fuera del sistema.');
      return;
    }
    var ok = $('nv-ok');
    ok.disabled = true;
    DN.api('/nueva-venta/crear', {body: new FormData(form), timeout: 60000}).then(function (r) {
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
    $('nv-buscar-cliente').hidden = false;
    $('nv-q-cliente').focus();
    listo();
    buscarClientes();
  });
  buscarClientes();
}());
