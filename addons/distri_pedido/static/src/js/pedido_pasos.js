/* /pedido's steps in the browser history (19.0.1.129.49, owner 26/11).
 *
 * The order page (views/templates.xml «pantalla») has three steps on one
 * URL: the catalog (1), «Tu pedido» (2, #p2) and «Tus datos» (3, #p3). They
 * were only CSS classes, so the phone's back button on step 2 or 3 left the
 * page (to the site's home) and the cart looked lost. Now going forward to a
 * step pushes a history entry ({dnPaso: n}, same URL) and the page's own
 * «Seguir comprando» / «Volver» buttons go back through the history, so the
 * back button and those buttons do the same thing. Back from the catalog
 * behaves as before (leaves the page). «Ajustes» (kit/ajustes.js) keeps its
 * own entry ({aj: 1}) on top and is ignored here.
 *
 * The page script calls DNPedidoPasos.crear(window, mostrar) and then uses
 * ir(n); `mostrar(n)` draws step n (the page's pintarDetalle etc.). A reload
 * on step 2 or 3 starts again on the catalog (the cart comes back through
 * the device draft, pedido_borrador.js), so the entry is reset to step 1.
 * Loaded before the page script (not deferred). No server side.
 */
(function () {
  'use strict';

  function pasoDe(estado) {
    var n = estado && +estado.dnPaso;
    return n === 2 || n === 3 ? n : 1;
  }

  function crear(win, mostrar) {
    var h = win.history;
    var actual = 1;
    function estado() { return (h && h.state) || {}; }
    function pintar(n) { actual = n; mostrar(n); }
    function conPaso(n) { return Object.assign({}, estado(), {dnPaso: n, aj: undefined}); }

    // a reload (or a return from another page) on step 2 or 3: the catalog
    if (h && pasoDe(estado()) !== 1) {
      try { h.replaceState(conPaso(1), ''); } catch (e) { /* old browser */ }
    }

    function ir(n) {
      n = n === 2 || n === 3 ? n : 1;
      if (n === actual) { return; }
      if (!h || !h.pushState) { pintar(n); return; }
      if (n > actual) {
        // forward: one entry per step (1 → 3 at once never happens)
        h.pushState(conPaso(n), '');
        pintar(n);
      } else if (pasoDe(estado()) === actual && actual > 1) {
        // back one step: the history's own back, so both ways agree; the
        // popstate below draws it
        h.back();
      } else {
        try { h.replaceState(conPaso(n), ''); } catch (e) { /* old browser */ }
        pintar(n);
      }
    }

    function alVolver(ev) {
      var st = ev && ev.state !== undefined ? ev.state : estado();
      if (st && st.aj) { return; }  // the Ajustes sheet's own entry
      var n = pasoDe(st);
      if (n !== actual) { pintar(n); }
    }
    if (win.addEventListener) { win.addEventListener('popstate', alVolver); }
    return {ir: ir, alVolver: alVolver, actual: function () { return actual; }};
  }

  var api = {crear: crear, pasoDe: pasoDe};
  if (typeof window !== 'undefined') { window.DNPedidoPasos = api; }
}());
