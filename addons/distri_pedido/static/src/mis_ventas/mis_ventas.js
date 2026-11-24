/* «Mis ventas» (19.0.1.129.18, views/mis_ventas_app.xml,
 * controllers/mis_ventas.py). Uses the kit (../kit/dn.js, window.DN). Read
 * only: nothing is sent but the filters (plain links and a GET form).
 *
 * - [data-mv-buscar]: filters the loaded cards by order number or customer
 *   as you type and recomputes the totals; Enter only closes the phone
 *   keyboard (like /pedido and /armado).
 * - [data-mv-vendedor] (Gerencia): picking a seller reloads the page.
 */
(function () {
  'use strict';

  function money(n) {
    // same as models/reparto.py _money: «$ 62.300», «$ 62.300,50»
    var parts = (Math.round((n || 0) * 100) / 100).toFixed(2).split('.');
    var whole = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return '$ ' + whole + (parts[1] === '00' ? '' : ',' + parts[1]);
  }

  function norm(text) {
    return (text || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
  }

  function start() {
    var input = DN.$('[data-mv-buscar]');
    var cards = DN.$$('.mv-card');
    var nada = DN.$('[data-mv-nada]');

    function set(sel, value) {
      var el = DN.$(sel);
      if (el) { el.textContent = value; }
    }

    function apply() {
      var words = norm(input ? input.value : '').split(/\s+/).filter(Boolean);
      var n = 0, importe = 0, facturado = 0, acobrar = 0, shown = 0;
      cards.forEach(function (card) {
        var text = norm(card.getAttribute('data-buscar'));
        var ok = words.every(function (w) { return text.indexOf(w) !== -1; });
        card.hidden = !ok;
        if (!ok) { return; }
        shown += 1;
        if (card.hasAttribute('data-cancelado')) { return; }
        n += 1;
        importe += parseFloat(card.getAttribute('data-importe')) || 0;
        facturado += parseFloat(card.getAttribute('data-facturado')) || 0;
        acobrar += parseFloat(card.getAttribute('data-acobrar')) || 0;
      });
      set('[data-mv-n]', String(n));
      set('[data-mv-importe]', money(importe));
      set('[data-mv-cobrado]', money(facturado - acobrar));
      set('[data-mv-acobrar]', money(acobrar));
      if (nada) { nada.hidden = !(words.length && cards.length && shown === 0); }
    }

    if (input) {
      input.addEventListener('input', apply);
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.keyCode === 13) {
          ev.preventDefault();
          input.blur();
        }
      });
    }

    var vendedor = DN.$('[data-mv-vendedor]');
    if (vendedor && vendedor.form) {
      vendedor.addEventListener('change', function () { vendedor.form.submit(); });
    }
  }

  if (window.DN && DN.ready) { start(); } else { document.addEventListener('dn:ready', start); }
})();
