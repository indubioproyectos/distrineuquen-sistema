/* 19.0.1.129.41 «Contactos» (controllers/contactos.py, ui_v2 only).
 *
 * The list works without this file (a GET form, «Ver más» links). With it:
 * - typing searches by itself, 250 ms after the last key (debounce), and a
 *   filter or a select searches at once; only the newest answer is drawn
 *   (an older one that arrives late is dropped), and the request before it
 *   is aborted when the browser can;
 * - «Ver más» appends the next page instead of opening it;
 * - the URL keeps the search (history.replaceState), so «Volver» from a
 *   customer comes back to the same list; the last search is also kept in
 *   sessionStorage for this tab;
 * - the edit form: «Usar mi ubicación» fills latitude and longitude from the
 *   phone's GPS.
 * The rows are built with textContent: nothing of the data is parsed as HTML.
 * window.DNContactos exposes the pure parts for tests/data/contactos_v129_41.js.
 */
(function (root) {
  'use strict';
  var FILTROS = ['mios', 'deuda', 'confirmar', 'sin_wa', 'sin_gps', 'mayorista', 'archivados'];
  var DELAY = 250;
  var STORE = 'dn:contactos:q';

  /* "q=..&localidad=..&mios=1&page=2" of a search state (empty values out) */
  function query(state, page) {
    var parts = [];
    function add(k, v) {
      if (v !== undefined && v !== null && v !== '' && v !== false) {
        parts.push(encodeURIComponent(k) + '=' + encodeURIComponent(v));
      }
    }
    add('q', (state.q || '').replace(/\s+/g, ' ').trim());
    add('localidad', state.localidad || '');
    if (state.tipo && state.tipo !== 'clientes') { add('tipo', state.tipo); }
    add('vendedor', state.vendedor || '');
    FILTROS.forEach(function (f) { if (state[f]) { add(f, '1'); } });
    if (page) { add('page', page); }
    return parts.join('&');
  }

  /* The search loop, without the DOM: opts.fetch(url) -> Promise(json),
     opts.render(data, append), opts.onState(queryWithoutPage), timers
     injectable for the tests. */
  function createSearch(opts) {
    var setT = opts.setTimeout || root.setTimeout;
    var clearT = opts.clearTimeout || root.clearTimeout;
    var delay = opts.delay === undefined ? DELAY : opts.delay;
    var timer = null;
    var seq = 0;
    var page = 0;
    var state = {};
    var sent = null;
    var controller = null;

    function run(append) {
      var mine = ++seq;
      var key = query(state, 0);
      if (!append) {
        sent = key;
        if (opts.onState) { opts.onState(key); }
      }
      if (controller && controller.abort) { controller.abort(); }
      controller = opts.AbortController ? new opts.AbortController() : null;
      var url = opts.base + '?' + query(state, append ? page + 1 : 0);
      return Promise.resolve(opts.fetch(url, controller ? controller.signal : undefined)).then(
        function (data) {
          if (mine !== seq || !data || !data.ok) { return false; }
          page = data.page || 0;
          opts.render(data, append);
          return true;
        },
        function () {
          if (mine === seq && opts.onError) { opts.onError(); }
          return false;
        });
    }
    var waiting = null;
    function cancel() {
      if (timer !== null) { clearT(timer); timer = null; }
      // the replaced pause answers «nothing drawn»
      if (waiting) { var w = waiting; waiting = null; w(false); }
    }
    return {
      /* a new state: typed text waits for the pause, anything else goes now */
      set: function (next, now) {
        state = next;
        cancel();
        if (query(state, 0) === sent) { return Promise.resolve(false); }
        if (now) { return run(false); }
        return new Promise(function (resolve) {
          waiting = resolve;
          timer = setT(function () { timer = null; waiting = null; resolve(run(false)); }, delay);
        });
      },
      more: function () { cancel(); return run(true); },
      page: function () { return page; },
      pending: function () { return timer !== null; },
      seq: function () { return seq; },
      mark: function (key) { sent = key; },
    };
  }

  function el(doc, tag, cls, text) {
    var node = doc.createElement(tag);
    if (cls) { node.className = cls; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  /* one row, the same markup as views/contactos.xml contactos_fila */
  function buildRow(doc, f) {
    var a = el(doc, 'a', 'dp-row ct-row');
    a.setAttribute('href', f.url);
    a.setAttribute('data-dn', 'down');
    a.setAttribute('data-ct-id', String(f.id));
    var txt = el(doc, 'span', 'dp-row__txt');
    txt.appendChild(el(doc, 'b', 'ct-name', f.nombre));
    txt.appendChild(el(doc, 'span', 'dp-meta',
      [f.codigo, f.localidad, f.vendedor].filter(Boolean).join(' · ')));
    if (f.whatsapp) {
      txt.appendChild(el(doc, 'span', 'dp-meta ct-wa', 'WhatsApp ' + f.whatsapp));
    } else {
      txt.appendChild(el(doc, 'span', 'dp-meta ct-wa ct-wa--no', 'Sin WhatsApp'));
    }
    if (f.confirmar || f.archivado) {
      var chips = el(doc, 'span', 'dp-chips');
      if (f.confirmar) { chips.appendChild(el(doc, 'span', 'dn-chip dn-chip--warn', 'A confirmar')); }
      if (f.archivado) { chips.appendChild(el(doc, 'span', 'dn-chip dn-chip--neutral', 'Archivado')); }
      txt.appendChild(chips);
    }
    a.appendChild(txt);
    if (f.saldo) {
      a.appendChild(el(doc, 'b', 'dp-amt dn-num ct-saldo' + (f.deuda ? ' ct-saldo--deuda' : ''), f.saldo));
    }
    return a;
  }

  function totalText(n) { return n + (n === 1 ? ' contacto' : ' contactos'); }

  root.DNContactos = {query: query, createSearch: createSearch, buildRow: buildRow,
                      totalText: totalText, FILTROS: FILTROS, DELAY: DELAY};

  var doc = root.document;
  if (!doc || !doc.getElementById) { return; }

  function session(op, value) {
    try {
      var s = root.sessionStorage;
      if (op === 'get') { return s.getItem(STORE); }
      s.setItem(STORE, value);
    } catch (e) { /* private mode: the URL keeps it anyway */ }
    return null;
  }

  function bindList() {
    var form = doc.getElementById('ct-form');
    var list = doc.getElementById('ct-list');
    if (!form || !list) { return; }
    var total = doc.getElementById('ct-total');
    var mas = doc.getElementById('ct-mas');
    var empty = doc.getElementById('ct-empty');
    var main = list.parentNode;

    function readState() {
      var st = {};
      ['q', 'localidad', 'tipo', 'vendedor'].forEach(function (name) {
        var f = form.elements[name];
        if (f) { st[name] = f.value; }
      });
      FILTROS.forEach(function (name) {
        var f = form.elements[name];
        if (f) { st[name] = !!f.checked; }
      });
      return st;
    }
    function writeState(params) {
      ['q', 'localidad', 'tipo', 'vendedor'].forEach(function (name) {
        var f = form.elements[name];
        if (f && params.has(name)) { f.value = params.get(name); }
      });
      FILTROS.forEach(function (name) {
        var f = form.elements[name];
        if (f) { f.checked = params.get(name) === '1'; }
      });
    }
    function render(data, append) {
      if (!append) { list.textContent = ''; }
      data.filas.forEach(function (f) { list.appendChild(buildRow(doc, f)); });
      if (total) { total.textContent = totalText(data.total); }
      if (!empty) {
        empty = el(doc, 'p', 'dn-empty ct-empty', 'No hay contactos con esa búsqueda.');
        empty.id = 'ct-empty';
        main.insertBefore(empty, list.nextSibling);
      }
      empty.hidden = data.total > 0;
      if (!mas) {
        mas = el(doc, 'a', 'dn-btn dn-btn--ghost ct-mas', 'Ver más');
        mas.id = 'ct-mas';
        mas.setAttribute('href', '#');
        main.appendChild(mas);
        mas.addEventListener('click', onMore);
      }
      mas.hidden = !data.mas;
      list.setAttribute('aria-busy', 'false');
    }
    var search = createSearch({
      base: '/contactos/buscar',
      fetch: function (url, signal) {
        list.setAttribute('aria-busy', 'true');
        return root.fetch(url, {credentials: 'same-origin', signal: signal,
                                headers: {'X-Distri-Fetch': '1'}}).then(function (r) { return r.json(); });
      },
      AbortController: root.AbortController,
      render: render,
      onState: function (key) {
        try { root.history.replaceState(root.history.state, '', '/contactos' + (key ? '?' + key : '')); } catch (e) { /* ignore */ }
        session('set', key);
      },
      onError: function () { list.setAttribute('aria-busy', 'false'); },
    });
    function onMore(ev) { ev.preventDefault(); search.more(); }
    search.mark(query(readState(), 0));
    if (mas) { mas.addEventListener('click', onMore); }
    form.addEventListener('submit', function (ev) { ev.preventDefault(); search.set(readState(), true); });
    form.addEventListener('input', function (ev) {
      if (ev.target && ev.target.name === 'q') { search.set(readState(), false); }
    });
    form.addEventListener('change', function (ev) {
      if (ev.target && ev.target.name !== 'q') { search.set(readState(), true); }
    });
    // arriving without a search (Inicio, the bar): the last one of this tab
    if (!root.location.search) {
      var last = session('get');
      if (last) {
        writeState(new root.URLSearchParams(last));
        search.set(readState(), true);
      }
    }
  }

  function bindGps() {
    var btn = doc.querySelector('[data-ct-gps]');
    if (!btn || !root.navigator || !root.navigator.geolocation) { return; }
    var msg = doc.querySelector('[data-ct-gps-msg]');
    btn.hidden = false;
    btn.addEventListener('click', function () {
      if (msg) { msg.textContent = 'Buscando la ubicación…'; }
      root.navigator.geolocation.getCurrentPosition(function (pos) {
        doc.getElementById('ct-lat').value = pos.coords.latitude.toFixed(6);
        doc.getElementById('ct-lng').value = pos.coords.longitude.toFixed(6);
        if (msg) {
          msg.textContent = 'Listo (±' + Math.round(pos.coords.accuracy) + ' m). Se guarda al tocar «Guardar».';
        }
      }, function () {
        if (msg) { msg.textContent = 'No se pudo leer la ubicación del teléfono.'; }
      }, {enableHighAccuracy: true, timeout: 15000, maximumAge: 0});
    });
  }

  function start() { bindList(); bindGps(); }
  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', start); } else { start(); }
})(typeof window !== 'undefined' ? window : this);
