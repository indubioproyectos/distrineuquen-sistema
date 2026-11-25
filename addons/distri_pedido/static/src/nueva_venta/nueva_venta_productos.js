/* «Elegir productos» of «Nueva venta» (19.0.1.129.40): /pedido's own catalog
 * page (template distri_pedido.pantalla and its inline script: rows,
 * search, sections, −/+ and the bar) rendered by /nueva-venta/productos with
 * the customer's list prices. This file only:
 * - types the cart of the «Nueva venta» draft on the phone (localStorage
 *   dn:nv:borrador:<uid>, nueva_venta.js) into the rows, when the draft is
 *   this customer's;
 * - «Confirmar productos»: writes the rows' quantities back as the draft's
 *   cart and returns to /nueva-venta?volver=1, which takes the draft up at
 *   the exit step. Nothing is posted: the page's form never submits here.
 */
(function () {
  'use strict';
  var DIAS_BORRADOR = 7;  // nueva_venta.js DIAS_BORRADOR

  function leer(store, key, now) {
    try {
      var b = JSON.parse(store.getItem(key) || 'null');
      if (!b || !b.cliente || (now - (b.t || 0)) > DIAS_BORRADOR * 864e5) { return null; }
      return b;
    } catch (e) { return null; }
  }

  // {template id: quantity} of the rows (a product drawn twice adds up)
  function carritoDe(doc) {
    var carrito = {};
    Array.prototype.forEach.call(doc.querySelectorAll('.fila'), function (f) {
      var inp = f.querySelector('.cant');
      var n = parseInt((inp && inp.value) || 0, 10);
      if (n > 0) {
        var id = String(f.getAttribute('data-id'));
        carrito[id] = (carrito[id] || 0) + n;
      }
    });
    return carrito;
  }

  // the draft's quantities into the first row of each product on the page
  function poner(doc, carrito) {
    var hecho = {};
    Array.prototype.forEach.call(doc.querySelectorAll('.fila'), function (f) {
      var id = String(f.getAttribute('data-id'));
      var inp = f.querySelector('.cant');
      if (!inp || hecho[id]) { return; }
      hecho[id] = true;
      inp.value = carrito[id] > 0 ? String(carrito[id]) : '';
    });
  }

  // the draft with this cart, for this customer, back at the exit step
  function confirmar(b, cliente, carrito, now) {
    var mismo = b && b.cliente && String(b.cliente.id) === String(cliente.id);
    var out = mismo ? b : {cliente: cliente, salida: '', campos: {}, encomienda: false, sin_wa: false};
    out.cliente = cliente;
    out.carrito = carrito;
    out.t = now;
    out.paso = 'salida';
    return out;
  }

  var api = {leer: leer, carritoDe: carritoDe, poner: poner, confirmar: confirmar};
  if (typeof window !== 'undefined') { window.DNNvProductos = api; }
  if (typeof document === 'undefined') { return; }

  function start() {
    var doc = document;
    var box = doc.getElementById('nv-picker');
    var form = doc.getElementById('frm');
    var ok = doc.getElementById('nv-confirmar');
    if (!box || !form || !ok) { return; }
    var key = 'dn:nv:borrador:' + (box.getAttribute('data-uid') || '0');
    var cliente = {id: +box.getAttribute('data-cliente-id'), nombre: box.getAttribute('data-cliente') || ''};
    var store = null;
    try { store = window.localStorage; } catch (e) { store = null; }
    var b = store ? leer(store, key, Date.now()) : null;
    if (b && String(b.cliente.id) === String(cliente.id)) {
      poner(doc, b.carrito || {});
      var c = form.querySelector('.cant');
      // the page's own total and counters (its «input» listener)
      if (c) { c.dispatchEvent(new Event('input', {bubbles: true})); }
    }
    // never posts from here (Enter in a quantity would submit the form)
    form.addEventListener('submit', function (ev) { ev.preventDefault(); ev.stopImmediatePropagation(); }, true);
    ok.addEventListener('click', function () {
      var nuevo = confirmar(store ? leer(store, key, Date.now()) : null, cliente, carritoDe(doc), Date.now());
      try { store.setItem(key, JSON.stringify(nuevo)); } catch (e) { /* full or private */ }
      try { window.sessionStorage.setItem('dn:nv:volver', '1'); } catch (e) { /* private mode */ }
      window.location.assign('/nueva-venta?volver=1');
    });
  }
  if (document.readyState === 'loading') { document.addEventListener('DOMContentLoaded', start); } else { start(); }
}());
