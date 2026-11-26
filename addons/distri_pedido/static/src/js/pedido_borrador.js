/* /pedido's cart kept on the device (19.0.1.129.40, owner 25/11).
 *
 * The order page (views/templates.xml «pantalla») kept the cart only in the
 * page: a visitor who went back by mistake, reloaded or whose session ended
 * lost it. Now, on every change, the quantities of each row (by product, and
 * by «Distribuidoras» row) and the fields already typed (name, phone,
 * address, comment, parcel, depot note, «Mi vendedor es», delivery way; never
 * a password, there is none) go to localStorage, under one key per user id
 * or «visitante» and per price list (form[data-borrador]). Back on the page
 * it offers «Seguir con el pedido anterior» / «Descartar».
 *
 * Restoring only types quantities into the rows the server drew now: a
 * product that is no longer in the catalog is dropped, and every price is
 * the one on the page (nothing stored is trusted). It is cleared when the
 * order arrives (19.0.1.129.49: the confirmation page «gracias» removes it;
 * before, the tap on «Enviar» did, so a send refused by the server lost the
 * cart) or the draft is discarded, and expires after 3 days. Not used
 * while changing an order that exists (the server has it) nor in «Nueva
 * venta»'s picker. Pure client side: the server receives exactly what it
 * received before, so it has no switch.
 *
 * 19.0.1.129.49: the draft also keeps the page token it was filled on
 * (hidden `envio`). Back on the page it first asks the server whether an
 * order with that token already arrived (GET /pedido/envio-estado): if so,
 * no resend is offered, the draft goes and a link to that order shows.
 * Otherwise «Seguir» puts the old token back with `envio_borrador=1`, and
 * /pedido/enviar returns the order of that token instead of a second one.
 */
(function () {
  'use strict';
  var DIAS = 3;
  var PREFIJO = 'dn:pedido:borrador:';
  var CAMPOS = ['nombre', 'comercio', 'telefono', 'domicilio', 'comentario', 'nota_deposito', 'enc_nombre', 'enc_dni',
    'enc_localidad', 'vendedor_web_id', 'entrega'];

  function filaClave(f) { return String(f.getAttribute('data-id')) + (f.getAttribute('data-may') ? 'm' : ''); }

  function cantidades(form) {
    var out = {};
    Array.prototype.forEach.call(form.querySelectorAll('.fila'), function (f) {
      var k = filaClave(f);
      var inp = f.querySelector('.cant');
      var n = parseInt((inp && inp.value) || 0, 10);
      (out[k] = out[k] || []).push(n > 0 ? n : 0);
    });
    Object.keys(out).forEach(function (k) {
      if (!out[k].some(function (n) { return n > 0; })) { delete out[k]; }
    });
    return out;
  }

  function leer(store, key, now) {
    try {
      var b = JSON.parse(store.getItem(key) || 'null');
      if (!b || !b.cantidades || !Object.keys(b.cantidades).length) { return null; }
      if ((now - (b.t || 0)) > DIAS * 864e5) { store.removeItem(key); return null; }
      return b;
    } catch (e) { return null; }
  }

  function guardar(store, key, form, now) {
    var data = {t: now, cantidades: cantidades(form), campos: {}};
    var envio = form.elements.envio;
    if (envio && envio.value) { data.envio = envio.value; }
    CAMPOS.forEach(function (n) {
      var el = form.elements[n];
      if (el && el.type !== 'password' && el.value) { data.campos[n] = el.value; }
    });
    var enc = form.elements.encomienda;
    data.encomienda = !!(enc && enc.checked);
    try {
      if (Object.keys(data.cantidades).length) { store.setItem(key, JSON.stringify(data)); }
      else { store.removeItem(key); }
    } catch (e) { /* full or private */ }
    return data;
  }

  // types the stored quantities into the rows drawn now; returns how many
  // products came back (the ones no longer on the page are dropped)
  function restaurar(form, b) {
    var vistos = {};
    var puestos = 0;
    Array.prototype.forEach.call(form.querySelectorAll('.fila'), function (f) {
      var k = filaClave(f);
      var i = vistos[k] = (vistos[k] === undefined ? 0 : vistos[k] + 1);
      var n = ((b.cantidades || {})[k] || [])[i] || 0;
      var inp = f.querySelector('.cant');
      if (inp && n > 0) { inp.value = String(n); puestos++; }
    });
    Object.keys(b.campos || {}).forEach(function (n) {
      var el = form.elements[n];
      if (el && el.type !== 'password' && el.type !== 'hidden' && !el.value) { el.value = b.campos[n]; }
    });
    return puestos;
  }

  // 19.0.1.129.49: where to ask whether the draft's order already arrived
  function estadoUrl(b) {
    var e = b && typeof b.envio === 'string' ? b.envio.replace(/[^0-9a-f]/gi, '') : '';
    return e.length >= 16 ? '/pedido/envio-estado?e=' + encodeURIComponent(e) : '';
  }
  // a restored draft resends with the token it was first sent with
  function reenvio(form, b, doc) {
    if (!b || !b.envio || !form.elements.envio) { return false; }
    form.elements.envio.value = b.envio;
    var flag = form.elements.envio_borrador;
    if (!flag) {
      flag = doc.createElement('input');
      flag.type = 'hidden';
      flag.name = 'envio_borrador';
      form.appendChild(flag);
    }
    flag.value = '1';
    return true;
  }

  var api = {estadoUrl: estadoUrl, reenvio: reenvio, clave: function (form) { return PREFIJO + (form.getAttribute('data-borrador') || 'visitante'); },
    cantidades: cantidades, leer: leer, guardar: guardar, restaurar: restaurar, DIAS: DIAS};
  if (typeof window !== 'undefined') { window.DNPedidoBorrador = api; }
  if (typeof document === 'undefined') { return; }

  function start() {
    var doc = document;
    var form = doc.getElementById('frm');
    if (!form || !form.getAttribute('data-borrador')) { return; }
    var store;
    try { store = window.localStorage; store.getItem('x'); } catch (e) { return; }
    var key = api.clave(form);
    var oferta = null;
    function avisar(tipo) {
      var c = form.querySelector('.cant');
      if (c) { c.dispatchEvent(new Event(tipo, {bubbles: true})); }
    }
    function cerrarOferta() { if (oferta) { oferta.remove(); oferta = null; } }
    var b = leer(store, key, Date.now());
    // the server already drew a cart (an expired form sent back): no offer
    var yaCargado = Array.prototype.some.call(form.querySelectorAll('.cant'), function (i) { return !!i.value; });
    // a change before the server answered started another order
    var cambiado = false;
    function ofrecer() {
      if (cambiado) { return; }
      var n = Object.keys(b.cantidades).length;
      oferta = doc.createElement('div');
      oferta.className = 'aviso aviso-borrador';
      oferta.setAttribute('role', 'status');
      oferta.innerHTML = '<p>Tenés un pedido sin terminar: ' + n + (n === 1 ? ' producto' : ' productos') +
        '.</p><p class="aviso-borrador__btns"><button type="button" class="btn btn-p" data-borrador-seguir="">' +
        'Seguir con el pedido anterior</button> <button type="button" class="btn btn-s" data-borrador-descartar="">' +
        'Descartar</button></p>';
      var donde = doc.getElementById('aviso');
      (donde && donde.parentNode ? donde.parentNode : form.parentNode).insertBefore(oferta, donde || form);
      oferta.querySelector('[data-borrador-seguir]').addEventListener('click', function () {
        cerrarOferta();
        var puestos = restaurar(form, b);
        reenvio(form, b, doc);
        var enc = form.elements.encomienda;
        if (enc && b.encomienda && !enc.checked) { enc.checked = true; enc.dispatchEvent(new Event('change', {bubbles: true})); }
        if (b.campos && b.campos.entrega === 'retira') {
          var ret = doc.getElementById('op_ret');
          if (ret) { ret.click(); }
        }
        avisar('input');
        if (puestos < n) {
          var av = doc.getElementById('aviso');
          if (av) { av.textContent = (n - puestos) + (n - puestos === 1 ? ' producto ya no está' : ' productos ya no están') + ' en el catálogo.'; }
        }
        guardar(store, key, form, Date.now());
      });
      oferta.querySelector('[data-borrador-descartar]').addEventListener('click', function () {
        cerrarOferta();
        try { store.removeItem(key); } catch (e) { /* private mode */ }
      });
    }
    // the order of that draft already arrived: show it, never resend
    function llego(r) {
      if (!cambiado) {
        try { store.removeItem(key); } catch (e) { /* private mode */ }
      }
      oferta = doc.createElement('div');
      oferta.className = 'aviso aviso-borrador';
      oferta.setAttribute('role', 'status');
      var p = doc.createElement('p');
      p.appendChild(doc.createTextNode('Tu pedido anterior ya nos llegó. '));
      var a = doc.createElement('a');
      a.href = r.url;
      a.textContent = 'Ver el pedido ' + r.pedido;
      p.appendChild(a);
      oferta.appendChild(p);
      var donde = doc.getElementById('aviso');
      (donde && donde.parentNode ? donde.parentNode : form.parentNode).insertBefore(oferta, donde || form);
    }
    if (b && !yaCargado) {
      var url = estadoUrl(b);
      if (url && window.fetch) {
        window.fetch(url, {credentials: 'same-origin', cache: 'no-store'})
          .then(function (r) { return r.ok ? r.json() : null; })
          .then(function (r) {
            if (r && r.pedido && r.url && r.url.charAt(0) === '/') { llego(r); } else { ofrecer(); }
          }, ofrecer);
      } else { ofrecer(); }
    }
    function cambio(ev) {
      if (!form.contains(ev.target) && ev.target.id !== 'op_dom' && ev.target.id !== 'op_ret') { return; }
      // a change before answering starts another order: the old one goes
      cambiado = true;
      cerrarOferta();
      guardar(store, key, form, Date.now());
    }
    doc.addEventListener('input', cambio);
    doc.addEventListener('change', cambio);
    // the delivery way is a tap on a card (hidden input #entrega)
    doc.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('.opt')) { setTimeout(function () { guardar(store, key, form, Date.now()); }, 0); }
    });
    // 19.0.1.129.49: sending keeps the draft; the confirmation page
    // (templates.xml «gracias») removes it once the order exists
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); } else { start(); }
}());
