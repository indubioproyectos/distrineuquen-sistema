/* «Asignar cliente» (19.0.1.111.0, views/asignar_app.xml,
 * controllers/asignar.py). Uses the kit (../kit/dn.js, window.DN). Without
 * JavaScript the page still works: every «Buscar» / «Elegir este» re-renders
 * it by GET and the final buttons post the form.
 *
 * - [data-as-tab]: «Cliente existente» / «Crear cliente nuevo» in the page.
 * - [data-as-pegado], [data-as-busqueda]: «Pegar número» reads as you type
 *   (and with «Buscar»); the candidates come rendered by the server.
 * - [data-as-elegir]: chooses a candidate (the card turns «Elegido»).
 * - [data-as-accion]: «Asignar y aprobar» / «Solo asignar» by fetch; on
 *   success the list it came from opens with the result.
 */
(function () {
  'use strict';

  function start() {
    var form = DN.$('[data-as-form]');
    if (!form) { return; }
    var orderId = document.body.getAttribute('data-order');
    var list = DN.$('[data-as-list]', form);
    var inPegado = DN.$('[data-as-pegado]', form);
    var inBusqueda = DN.$('[data-as-busqueda]', form);
    var inElegido = DN.$('[data-as-elegido]', form);
    var inModo = DN.$('[data-as-modo]', form);
    var seq = 0, timer = null;

    // ------------------------------------------------------------ tabs
    function modo(m) {
      inModo.value = m;
      form.setAttribute('data-modo', m);
      DN.$$('[data-as-tab]').forEach(function (t) {
        var on = t.getAttribute('data-as-tab') === m;
        t.classList.toggle('on', on);
        t.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      DN.$$('[data-as-pane]').forEach(function (p) { p.hidden = p.getAttribute('data-as-pane') !== m; });
    }
    DN.$$('[data-as-tab]').forEach(function (t) {
      t.addEventListener('click', function (ev) {
        ev.preventDefault();
        ev.stopPropagation();
        modo(t.getAttribute('data-as-tab'));
      }, true);
    });

    // ------------------------------------------------------------ choose
    function elegir(id) {
      inElegido.value = id || '';
      DN.$$('.as-cand', list).forEach(function (c) {
        c.classList.toggle('as-cand--on', c.getAttribute('data-id') === String(id));
      });
    }

    // ------------------------------------------------------------ search
    function show(sel, text) {
      var el = DN.$(sel, form);
      if (el) { el.hidden = !text; }
      return el;
    }
    function buscar(fromPaste, now) {
      clearTimeout(timer);
      timer = setTimeout(function () {
        var q = new URLSearchParams();
        q.set('pegado', inPegado.value);
        if (!fromPaste) { q.set('busqueda', inBusqueda.value); }
        if (inElegido.value && !fromPaste) { q.set('elegido', inElegido.value); }
        var mine = ++seq;
        list.classList.add('as-busy');
        DN.api('/revisar/' + orderId + '/asignar/candidatos?' + q.toString()).then(function (j) {
          if (mine !== seq) { return; }
          list.classList.remove('as-busy');
          list.innerHTML = j.html;
          if (fromPaste && j.busqueda) { inBusqueda.value = j.busqueda; }
          inElegido.value = j.elegido || '';
          DN.$('[data-as-preview-txt]', form).textContent = j.preview || '';
          show('[data-as-preview]', j.preview);
          show('[data-as-perror]', j.error).textContent = j.error || '';
          show('[data-as-aviso]', j.aviso).textContent = j.aviso || '';
          show('[data-as-guardar]', j.preview);
          var tel = DN.$('[data-as-nuevo-tel]', form);
          if (tel && j.nuevo_telefono && fromPaste && j.preview) { tel.value = j.nuevo_telefono; }
        }, function (e) { list.classList.remove('as-busy'); DN.fail(e); });
      }, now ? 0 : 350);
    }
    inPegado.addEventListener('input', function () {
      if (!inPegado.value.trim()) {
        ['[data-as-preview]', '[data-as-perror]', '[data-as-aviso]', '[data-as-guardar]'].forEach(function (s) { show(s, ''); });
        return;
      }
      buscar(true, false);
    });
    inBusqueda.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); buscar(false, true); }
    });
    inPegado.addEventListener('keydown', function (ev) {
      if (ev.key === 'Enter') { ev.preventDefault(); buscar(true, true); }
    });

    form.addEventListener('click', function (ev) {
      var t = ev.target.closest('[data-as-elegir],[data-as-buscar],[data-as-buscar-pegado]');
      if (!t) { return; }
      ev.preventDefault();
      if (t.hasAttribute('data-as-elegir')) { elegir(t.getAttribute('data-as-elegir')); }
      else { buscar(t.hasAttribute('data-as-buscar-pegado'), true); }
    });

    // ------------------------------------------------------------ assign
    form.addEventListener('submit', function (ev) {
      var btn = ev.submitter;
      if (!btn || !btn.hasAttribute('data-as-accion')) { return; }
      ev.preventDefault();
      if (inModo.value === 'existente' && !inElegido.value) {
        DN.toast('Elegí el cliente, o tocá «Crear cliente nuevo».', 'err');
        return;
      }
      var body = new FormData(form);
      body.set('accion', btn.getAttribute('data-as-accion'));
      var buttons = DN.$$('[data-as-accion]', form);
      buttons.forEach(function (b) { b.disabled = true; });
      DN.api(form.getAttribute('action'), {body: body}).then(function (j) {
        DN.toast(j.message, 'ok');
        DN.up(j.url);
      }, function (e) {
        buttons.forEach(function (b) { b.disabled = false; });
        DN.fail(e);
      });
    });
  }

  if (window.DN && DN.ready) { start(); } else { document.addEventListener('dn:ready', start); }
})();
