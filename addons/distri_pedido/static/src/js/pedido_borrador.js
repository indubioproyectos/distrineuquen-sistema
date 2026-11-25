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
 * order is sent or the draft discarded, and expires after 3 days. Not used
 * while changing an order that exists (the server has it) nor in «Nueva
 * venta»'s picker. Pure client side: the server receives exactly what it
 * received before, so it has no switch.
 */
(function () {
  'use strict';
  var DIAS = 3;
  var PREFIJO = 'dn:pedido:borrador:';
  var CAMPOS = ['nombre', 'telefono', 'domicilio', 'comentario', 'nota_deposito', 'enc_nombre', 'enc_dni',
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

  var api = {clave: function (form) { return PREFIJO + (form.getAttribute('data-borrador') || 'visitante'); },
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
    if (b && !yaCargado) {
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
    function cambio(ev) {
      if (!form.contains(ev.target) && ev.target.id !== 'op_dom' && ev.target.id !== 'op_ret') { return; }
      // a change before answering starts another order: the old one goes
      cerrarOferta();
      guardar(store, key, form, Date.now());
    }
    doc.addEventListener('input', cambio);
    doc.addEventListener('change', cambio);
    // the delivery way is a tap on a card (hidden input #entrega)
    doc.addEventListener('click', function (ev) {
      if (ev.target.closest && ev.target.closest('.opt')) { setTimeout(function () { guardar(store, key, form, Date.now()); }, 0); }
    });
    // sent: the page's own checks passed (not prevented)
    doc.addEventListener('submit', function (ev) {
      if (ev.target === form && !ev.defaultPrevented) {
        try { store.removeItem(key); } catch (e) { /* private mode */ }
      }
    });
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); } else { start(); }
}());
