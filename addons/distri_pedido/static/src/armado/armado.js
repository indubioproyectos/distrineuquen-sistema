/* /armado on the kit (19.0.1.94.0). Needs ../kit/dn.js (window.DN).
 *
 * List: refreshes itself every 60 s while the page is visible.
 * Order: every action goes through fetch() (the server answers JSON for
 * X-Distri-Fetch: 1) and the page never reloads; without JavaScript the
 * same forms post and redirect. Navigation follows the kit rule (dn.js):
 * opening a line is one level down (#r<id>, phone back closes it), moving to
 * the next line replaces that level, «Terminar» and «Más tarde» go up to the
 * list with the result banner.
 */
(function () {
  'use strict';
  var REFRESH = 60000;

  function start() {
    var DN = window.DN;
    var page = document.body.getAttribute('data-page');
    if (page === 'lista') { lista(DN); }
    if (page === 'pedido') { pedido(DN); }
  }

  // ------------------------------------------------------------ list
  function lista(DN) {
    var box = DN.$('#lista');
    var last = Date.now();
    var busy = false;
    function refresh() {
      if (busy || document.visibilityState !== 'visible') { return; }
      busy = true;
      DN.api(location.pathname).then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById('lista');
        if (fresh) { box.innerHTML = fresh.innerHTML; }
        last = Date.now();
      }, function () { /* keep what is shown; next round tries again */ }).then(function () { busy = false; });
    }
    setInterval(refresh, REFRESH);
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'visible' && Date.now() - last > REFRESH) { refresh(); }
    });
  }

  // ----------------------------------------------------------- order
  function pedido(DN) {
    var renglones = DN.$('#renglones');
    var sheets = {};
    DN.$$('.dn-sheet').forEach(function (el) { sheets[el.id] = DN.sheet(el); });

    function rows() { return DN.$$('.ren', renglones); }
    function num(v) { return parseFloat(String(v).replace(',', '.')); }
    function fmt(n) { return String(Math.round(n * 1000) / 1000); }
    function nodo(html) {
      var t = document.createElement('template');
      t.innerHTML = html.trim();
      return t.content.firstElementChild;
    }

    // progress in the top bar, recounted from the lines on screen
    function progreso() {
      var box = DN.$('#progreso');
      if (!box) { return; }
      var all = rows();
      var done = all.filter(function (r) { return r.dataset.estado !== 'pend'; }).length;
      var short = all.filter(function (r) { return r.dataset.estado === 'falta'; }).length;
      box.querySelector('.js-hechos').textContent = done;
      var f = box.querySelector('.js-faltan');
      f.textContent = short + (short === 1 ? ' faltante' : ' faltantes');
      f.hidden = !short;
      var bar = box.querySelector('.dn-progress');
      bar.setAttribute('aria-valuenow', done);
      bar.firstElementChild.style.width = (all.length ? Math.round(100 * done / all.length) : 0) + '%';
    }

    // ---- items: open = one level down, next = sideways
    function opened() { return renglones.querySelector('details.ren[open]'); }
    function show(ren, how) {
      rows().forEach(function (o) { if (o !== ren && o.open) { o.open = false; } });
      if (!ren) { return; }
      ren.open = true;
      var st = history.state && history.state.dn;
      if (how === 'down') {
        if (st && st.item) { DN.replace({item: ren.id}, '#' + ren.id); } else { DN.push({item: ren.id}, '#' + ren.id); }
      } else if (how === 'side' && st && st.item) {
        DN.replace({item: ren.id}, '#' + ren.id);
      }
      ren.scrollIntoView({block: 'nearest', behavior: 'smooth'});
    }
    function hide(ren) {
      var st = history.state && history.state.dn;
      if (st && st.item && DN.depth() > 0) { history.back(); return; }
      ren.open = false;
      if (st && st.item) { DN.replace({item: 0}, ''); }
    }
    function nextPending(after) {
      var all = rows();
      var i = after ? all.indexOf(after) : -1;
      var later = all.slice(i + 1).concat(all.slice(0, Math.max(i, 0)));
      return later.filter(function (r) { return r.dataset.estado === 'pend' && r.tagName === 'DETAILS'; })[0];
    }
    renglones.addEventListener('click', function (ev) {
      var sum = ev.target.closest('summary.ren__top');
      if (!sum) { return; }
      ev.preventDefault();
      var ren = sum.parentNode;
      if (ren.open) { hide(ren); } else { show(ren, 'down'); }
    });
    DN.onPop(function (st) {
      if (st.sheet) { return; }
      var ren = st.item && document.getElementById(st.item);
      if (ren) { show(ren); return; }
      var o = opened();
      if (o) { o.open = false; }
    });
    // «Falta» is a <label> for a checkbox (works without JavaScript); Enter
    // and Space on it as on a button
    renglones.addEventListener('keydown', function (ev) {
      var l = ev.target.closest('label.dn-btn');
      if (l && (ev.key === 'Enter' || ev.key === ' ')) { ev.preventDefault(); l.click(); }
    });
    // a line given by the URL (#r123) opens on load
    if (/^#r\d+$/.test(location.hash)) {
      var first = document.getElementById(location.hash.slice(1));
      if (first && first.tagName === 'DETAILS') {
        show(first);
        DN.replace({item: first.id});
      }
    }

    // ---- line actions
    function replaceRow(ren, html, advance) {
      var fresh = nodo(html);
      var wasOpen = ren.open;
      ren.replaceWith(fresh);
      progreso();
      if (advance) {
        var next = nextPending(fresh);
        if (next) { show(next, 'side'); } else { if (wasOpen) { hide(fresh); } DN.toast('Revisaste todo. Tocá «Terminar» cuando esté.', 'ok'); }
      }
      return fresh;
    }
    function lineAction(form, accion) {
      var ren = form.closest('.ren');
      var data = new FormData(form);
      data.set('accion', accion);
      if (form.dataset.fetch === 'falta' && accion === 'faltan') {
        var pedida = num(ren.dataset.pedida);
        var n = num(form.faltan.value);
        if (!(n > 0)) { DN.toast('Poné cuántas unidades faltan.', 'err'); return; }
        if (n > pedida) { DN.toast('Faltan como mucho ' + fmt(pedida) + '.', 'err'); return; }
      }
      ren.classList.add('guardando');
      DN.api(form.action, {body: data}).then(function (j) {
        replaceRow(ren, j.html, accion !== 'deshacer');
      }, function (e) {
        ren.classList.remove('guardando');
        DN.fail(e);
      });
    }

    function agregar(form) {
      var cantidad = num(form.cantidad.value);
      if (!(cantidad > 0)) { DN.toast('Poné una cantidad.', 'err'); return; }
      var boton = form.querySelector('button');
      var vacio = renglones.querySelector(':scope > .dn-empty');
      boton.disabled = true;
      DN.api(form.action, {body: new FormData(form)}).then(function (j) {
        if (vacio) { vacio.remove(); }
        var nuevo = nodo(j.html);
        nuevo.classList.add('nuevo');
        var same = nuevo.id && document.getElementById(nuevo.id);
        if (same) { same.replaceWith(nuevo); } else { renglones.appendChild(nuevo); }
        progreso();
        form.cantidad.value = 1;
        // clave nueva para el proximo "Agregar"; si fallo se reintenta con la
        // misma y el servidor no lo duplica
        form.clave.value = String(Date.now()) + Math.random().toString(16).slice(2);
        DN.toast('Agregaste ' + form.dataset.nombre + ' × ' + fmt(cantidad), 'ok');
      }, DN.fail).then(function () { boton.disabled = false; });
    }

    function sending(form, promise) {
      var b = form.querySelector('button[type="submit"]');
      if (b) { b.disabled = true; }
      return promise.then(null, function (e) {
        if (b) { b.disabled = false; }
        DN.fail(e);
      });
    }

    var actions = {
      listo: function (form, btn) { lineAction(form, btn ? btn.value : 'listo'); },
      falta: function (form, btn) { lineAction(form, btn ? btn.value : 'faltan'); },
      agregar: agregar,
      tomar: function (form) {
        sending(form, DN.api(form.action, {body: new FormData(form)}).then(function () { location.reload(); }));
      },
      terminar: function (form) {
        var n = parseInt(form.bultos.value, 10);
        if (!(n >= 1)) { DN.toast('Poné en cuántos bultos quedó el pedido.', 'err'); form.bultos.focus(); return; }
        sending(form, DN.api(form.action, {body: new FormData(form)}).then(function (j) { DN.up(j.url); }));
      },
      'mas-tarde': function (form) {
        sending(form, DN.api(form.action, {body: new FormData(form)}).then(function (j) { DN.up(j.url); }));
      },
      bultos: function (form) {
        sending(form, DN.api(form.action, {body: new FormData(form)}).then(function (j) {
          var b = DN.$('.js-bultos');
          var txt = j.bultos + (j.bultos === 1 ? ' bulto' : ' bultos');
          if (b) { b.textContent = txt; }
          form.querySelector('button[type="submit"]').disabled = false;
          sheets.corregir.close();
          DN.toast('Guardado: ' + txt + '.', 'ok');
        }));
      }
    };
    document.addEventListener('submit', function (ev) {
      var form = ev.target;
      var fn = actions[form.dataset.fetch];
      if (!fn || !window.fetch) { return; }
      ev.preventDefault();
      fn(form, ev.submitter || form.ultimo);
    });
    // Safari viejo no tiene ev.submitter
    document.addEventListener('click', function (ev) {
      var b = ev.target.closest && ev.target.closest('button[name="accion"]');
      if (b) { b.form.ultimo = b; }
    });

    // ---- sheets from the footer
    DN.$$('[data-sheet]').forEach(function (a) {
      a.addEventListener('click', function (ev) {
        var s = sheets[a.getAttribute('data-sheet')];
        if (!s) { return; }
        ev.preventDefault();
        var o = opened();
        if (o && history.state && history.state.dn && history.state.dn.item) { o.open = false; DN.replace({item: 0}, ''); }
        prepare(a.getAttribute('data-sheet'));
        s.open();
      });
    });
    // what changed without reloading is counted when the sheet opens
    function prepare(id) {
      var all = rows();
      var short = all.filter(function (r) { return r.dataset.estado === 'falta'; }).length;
      var pend = all.filter(function (r) { return r.dataset.estado === 'pend'; }).length;
      if (id === 'mas-tarde') {
        var borra = DN.$('#mas-tarde .arm-borra');
        borra.textContent = short === 1 ? 'Se va a borrar el faltante que marcaste.'
          : 'Se van a borrar los ' + short + ' faltantes que marcaste.';
        borra.hidden = !short;
      }
      if (id === 'terminar') {
        var q = DN.$('#terminar .arm-quedan');
        q.textContent = pend === 1 ? 'Te queda 1 producto sin revisar: se entrega completo.'
          : 'Te quedan ' + pend + ' productos sin revisar: se entregan completos.';
        q.hidden = !pend;
      }
    }
    // the bultos stepper
    DN.$$('[data-paso]').forEach(function (b) {
      b.addEventListener('click', function () {
        var input = b.parentNode.querySelector('input');
        var v = (parseInt(input.value, 10) || 0) + parseInt(b.getAttribute('data-paso'), 10);
        input.value = Math.min(Math.max(v, 1), 999);
      });
    });

    // ---- search while typing
    var buscador = DN.$('form[data-buscar]');
    var resultados = DN.$('#resultados');
    var espera, pendiente;
    function buscar() {
      var q = buscador.q.value.trim();
      clearTimeout(espera);
      if (pendiente) { pendiente.abort(); }
      if (q.length < 2) { resultados.innerHTML = ''; return; }
      pendiente = new AbortController();
      DN.api(buscador.dataset.buscar + '?q=' + encodeURIComponent(q), {signal: pendiente.signal})
        .then(function (html) { resultados.innerHTML = html; }, DN.fail);
    }
    if (buscador) {
      buscador.q.addEventListener('input', function () {
        clearTimeout(espera);
        espera = setTimeout(buscar, 300);
      });
      buscador.addEventListener('submit', function (ev) { ev.preventDefault(); buscar(); });
    }
  }

  if (window.DN && window.DN.ready) { start(); } else { document.addEventListener('dn:ready', start); }
})();
