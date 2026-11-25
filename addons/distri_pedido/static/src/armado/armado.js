/* /armado on the kit (19.0.1.94.0). Needs ../kit/dn.js (window.DN).
 *
 * List: refreshes itself every 60 s while the page is visible.
 * Order: every action goes through fetch() (the server answers JSON for
 * X-Distri-Fetch: 1) and the page never reloads; without JavaScript the
 * same forms post and redirect. Navigation follows the kit rule (dn.js):
 * opening a line is one level down (#r<id>, phone back closes it), moving to
 * the next line replaces that level, «Terminar» and «Más tarde» go up to the
 * list with the result banner.
 *
 * 19.0.1.98.0 «Tradicional» (data-page="trad", the default view): the whole
 * order as a list. Tap toggles the strike-through, long-press or swipe left
 * opens «¿Cuántas hay?», «Todo listo» asks the packages. Every change is
 * shown at once, saved on the phone (localStorage, one queue per order and
 * user) and sent in order to /armado/pedido/<id>/sync as events with a UUID
 * made here: on reload, offline or with a lost answer nothing is lost or
 * applied twice (the server keeps the UUIDs, models/armado_offline.py).
 * Same approach as the driver app's outbox (static/src/reparto/app.js).
 *
 * 19.0.1.129.42 (ui_v2): a finished order stays on its own page («Imprimir
 * etiquetas», «Siguiente pedido»: location.replace, irDespues), and the list
 * has state chips («Para armar · En armado · Armados hoy») that filter the
 * cards together with the search.
 */
(function () {
  'use strict';
  var REFRESH = 60000;

  // 19.0.1.129.42 (ui_v2): pure helpers, also used by the Node harness
  // (tests/data/armado_v129_42.js).
  // - destino: a finished order answers its own page (/armado/pedido/<id>
  //   ?recien=1): it replaces this one (back still goes to the list); any
  //   other answer goes up to the list as before.
  // - mostrar: a list card is shown when it matches every typed word and
  //   the chosen state chip ('' = all).
  var Armado = window.DNArmado = {
    destino: function (url) { return /^\/armado\/pedido\/\d+/.test(url || '') ? 'replace' : 'up'; },
    mostrar: function (text, words, estado, estadoCard) {
      return words.every(function (w) { return text.indexOf(w) !== -1; }) &&
        (!estado || estado === estadoCard);
    }
  };
  function irDespues(DN, url) {
    if (Armado.destino(url) === 'replace') { location.replace(url); } else { DN.up(url); }
  }

  function start() {
    var DN = window.DN;
    var page = document.body.getAttribute('data-page');
    if (page === 'lista') { lista(DN); }
    if (page === 'pedido') { pedido(DN); }
    if (page === 'trad') { trad(DN); }
    prefs(DN);
    confirma(DN);
  }

  // 19.0.1.129.35: «Ya lo armé» / «Ya lo armé y ya se entregó» as a
  // correction. Each form says which one it is before it goes; only then
  // its `confirmo` is filled (the server refuses the post without it).
  function confirma(DN) {
    DN.$$('form[data-arm-confirma]').forEach(function (form) {
      form.addEventListener('submit', function (ev) {
        var campo = form.querySelector('input[name=confirmo]');
        if (campo.value) { return; }
        ev.preventDefault();
        if (form.checkValidity && !form.checkValidity()) { form.reportValidity(); return; }
        var cual = form.getAttribute('data-arm-confirma');
        DN.confirm(form.getAttribute('data-arm-texto'),
          cual === 'entregado' ? 'Sí, ya se entregó' : 'Sí, solo armado').then(function (ok) {
          if (!ok) { return; }
          campo.value = cual;
          form.submit();
        });
      });
    });
  }

  // the view/order toggles of an order (also in «Ajustes»)
  function prefs(DN) {
    DN.$$('.arm-pref form[data-aj-pref]').forEach(function (form) {
      form.addEventListener('click', function (ev) {
        var b = ev.target.closest('button');
        if (!b || !window.fetch) { return; }
        ev.preventDefault();
        var data = new FormData(form);
        data.append(b.name, b.value);
        DN.api(form.action, {body: data}).then(function () {
          location.replace(location.pathname);
        }, DN.fail);
      });
    });
  }

  // ------------------------------------------------- «Tradicional»
  function trad(DN) {
    var body = document.body;
    var pedidoId = body.getAttribute('data-pedido');
    var syncUrl = body.getAttribute('data-sync');
    var puede = body.getAttribute('data-puede') === '1';
    var tope = parseInt(body.getAttribute('data-tope'), 10) || 12;
    var KEY = 'dn:arm:' + pedidoId + ':' + body.getAttribute('data-uid');
    var sheets = {};
    DN.$$('.dn-sheet').forEach(function (el) { sheets[el.id] = DN.sheet(el); });
    var queue = load();
    var sending = false;
    var finished = false;

    function load() {
      try { return JSON.parse(localStorage.getItem(KEY) || '[]') || []; } catch (e) { return []; }
    }
    function save() {
      try {
        if (queue.length) { localStorage.setItem(KEY, JSON.stringify(queue)); } else { localStorage.removeItem(KEY); }
      } catch (e) { /* private mode: it still goes out while the page is open */ }
    }
    function uuid() {
      if (window.crypto && crypto.randomUUID) { return crypto.randomUUID(); }
      return 'xxxxxxxx-xxxx-4xxx-yxxx-xxxxxxxxxxxx'.replace(/[xy]/g, function (c) {
        var r = Math.random() * 16 | 0;
        return (c === 'x' ? r : (r & 3 | 8)).toString(16);
      });
    }
    function num(v) { return parseFloat(String(v).replace(',', '.')); }
    function fmt(n) { return String(Math.round(n * 1000) / 1000); }
    function rows() { return DN.$$('li.tl'); }
    function row(id) { return document.getElementById('t' + id); }

    // ---- what the line looks like
    function paint(li) {
      var pedida = num(li.dataset.pedida);
      var hay = num(li.dataset.hay);
      var estado = li.dataset.estado;
      li.className = 'tl tl--' + estado;
      var cant = li.querySelector('.tl__cant');
      cant.textContent = estado === 'falta' ? fmt(hay) + '/' + fmt(pedida) : fmt(pedida);
      var f = li.querySelector('.tl__falta');
      if (estado === 'falta') {
        if (!f) { f = document.createElement('span'); f.className = 'tl__falta'; li.querySelector('.tl__nom').appendChild(f); }
        f.textContent = 'FALTAN ' + fmt(pedida - hay);
      } else if (f) { f.remove(); }
      li.querySelector('.dn-sr').textContent = estado === 'listo' ? 'Tildado.' : (estado === 'falta' ? 'Con faltante.' : '');
      var b = li.querySelector('.tl__b');
      b.disabled = !puede;
      b.setAttribute('aria-pressed', estado === 'listo' ? 'true' : 'false');
    }
    function progreso() {
      var all = rows();
      var ok = all.filter(function (r) { return r.dataset.estado === 'listo'; }).length;
      var short = all.filter(function (r) { return r.dataset.estado === 'falta'; }).length;
      var el = DN.$('.js-progreso');
      if (el) {
        el.textContent = (ok || short ? ' · ' + ok + (ok === 1 ? ' tachado' : ' tachados') : '') + (short ? ' · ' + short + (short === 1 ? ' con faltante' : ' con faltantes') : '');
      }
    }
    // apply an event to the screen (also the queued ones after a reload)
    function show(ev) {
      if (ev.tipo === 'terminar') { return; }
      var li = row(ev.linea);
      if (!li) { return; }
      if (ev.tipo === 'listo') {
        if (ev.valor && li.dataset.estado === 'falta') { li.dataset.hay = li.dataset.pedida; }
        li.dataset.estado = ev.valor ? 'listo' : 'pend';
      } else if (ev.tipo === 'hay') {
        li.dataset.hay = fmt(ev.valor);
        li.dataset.estado = ev.valor >= num(li.dataset.pedida) ? 'listo' : 'falta';
      }
      paint(li);
    }

    // ---- the queue
    function status() {
      var n = queue.filter(function (e) { return e.tipo !== 'terminar'; }).length;
      var fin = queue.some(function (e) { return e.tipo === 'terminar'; });
      if (!queue.length) {
        if (!finished) {
          DN.banner(navigator.onLine === false ? 'Sin señal: seguí armando, lo que marques se guarda en el celular.' : '',
            'warn');
        }
        return;
      }
      var what = (fin ? 'el pedido terminado' : n + (n === 1 ? ' cambio' : ' cambios')) + ' guardado' +
        (fin || n === 1 ? '' : 's') + ' en el celular';
      if (navigator.onLine === false) {
        DN.banner('Sin señal · ' + what + '. Se envía solo cuando vuelva.', 'warn');
      } else {
        DN.banner('Enviando · ' + what + '…', '');
      }
    }
    function enqueue(tipo, linea, valor) {
      var ev = {uuid: uuid(), tipo: tipo, linea: linea || 0, valor: valor, at: new Date().toISOString()};
      // a newer change of the same line replaces the one not sent yet
      // (never one already in flight: it keeps its UUID)
      if (tipo !== 'terminar') {
        queue = queue.filter(function (e) { return e.sent || e.tipo === 'terminar' || e.linea !== ev.linea; });
      }
      queue.push(ev);
      save();
      show(ev);
      progreso();
      status();
      flush();
      return ev;
    }
    function flush() {
      if (sending || !queue.length) { return Promise.resolve(); }
      sending = true;
      var batch = queue.slice(0, 200);
      batch.forEach(function (e) { e.sent = true; });
      save();
      var data = new FormData();
      data.append('eventos', JSON.stringify(batch.map(function (e) {
        return {uuid: e.uuid, tipo: e.tipo, linea: e.linea, valor: e.valor, at: e.at};
      })));
      return DN.api(syncUrl, {body: data, timeout: 15000}).then(function (j) {
        var done = {};
        var errores = [];
        (j.resultados || []).forEach(function (r) {
          if (r.status === 'error' && r.retry) { return; }
          done[r.uuid] = true;
          if (r.status === 'error') { errores.push(r.error); }
        });
        queue = queue.filter(function (e) { return !done[e.uuid]; });
        save();
        sending = false;
        status();
        if (errores.length) {
          // the server did not take it (released, finished by someone
          // else...): show why and what the server has now
          var t = DN.toast(errores[0] + ' Tocá para ver cómo quedó.', 'err');
          t.onTap = function () { location.reload(); };
          return;
        }
        if (j.url) { finished = true; irDespues(DN, j.url); return; }
        if (queue.length) { return flush(); }
      }, function (e) {
        sending = false;
        queue.forEach(function (x) { x.sent = false; });
        save();
        status();
        if (e.status === 400 || e.status === 401) {
          // stale page or session: it stays saved; reload when online
          DN.banner('La página venció. Lo marcado sigue guardado en el celular: recargá cuando tengas señal.', 'warn');
        }
      });
    }
    window.addEventListener('online', function () { setTimeout(flush, 500); });
    window.addEventListener('offline', status);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'visible') { flush(); } });
    setInterval(flush, 20000);
    queue.forEach(function (e) { e.sent = false; show(e); });
    rows().forEach(paint);
    progreso();
    status();
    flush();

    // ---- gestures: tap, long-press, swipe left
    var press = null;
    function startPress(li, x, y) {
      cancelPress();
      press = {li: li, x: x, y: y, long: false};
      press.timer = setTimeout(function () {
        if (!press) { return; }
        press.long = true;
        if (navigator.vibrate) { navigator.vibrate(15); }
        openHay(li);
      }, 500);
    }
    function cancelPress() { if (press) { clearTimeout(press.timer); } }
    var lista = DN.$('#trad');
    lista.addEventListener('pointerdown', function (ev) {
      var li = ev.target.closest('li.tl');
      if (!li || !puede || ev.button > 0) { return; }
      startPress(li, ev.clientX, ev.clientY);
    });
    lista.addEventListener('pointermove', function (ev) {
      if (!press) { return; }
      var dx = ev.clientX - press.x;
      var dy = ev.clientY - press.y;
      if (Math.abs(dy) > 12) { cancelPress(); press = null; return; }
      if (Math.abs(dx) > 12) { clearTimeout(press.timer); }
      press.li.style.transform = dx < 0 ? 'translateX(' + Math.max(dx, -96) + 'px)' : '';
      if (dx < -70 && !press.long) { press.long = true; press.li.style.transform = ''; openHay(press.li); }
    });
    function endPress() {
      if (!press) { return; }
      cancelPress();
      press.li.style.transform = '';
      if (!press.long) { press.done = false; } else { press.swallow = true; }
    }
    lista.addEventListener('pointerup', endPress);
    lista.addEventListener('pointercancel', function () { if (press) { cancelPress(); press.li.style.transform = ''; press = null; } });
    lista.addEventListener('contextmenu', function (ev) { if (ev.target.closest('li.tl')) { ev.preventDefault(); } });
    lista.addEventListener('click', function (ev) {
      var li = ev.target.closest('li.tl');
      if (!li) { return; }
      ev.preventDefault();
      var swallow = press && press.swallow;
      press = null;
      if (swallow || !puede || finished) { return; }
      if (li.dataset.estado === 'falta') { openHay(li); return; }
      enqueue('listo', parseInt(li.dataset.id, 10), li.dataset.estado !== 'listo');
    });
    // keyboard: Enter/Space on the line toggles (the button), «F» = shortage
    lista.addEventListener('keydown', function (ev) {
      var li = ev.target.closest('li.tl');
      if (li && (ev.key === 'f' || ev.key === 'F')) { ev.preventDefault(); openHay(li); }
    });

    // ---- «¿Cuántas hay?»
    var hayEl = DN.$('#hay');
    var hayLi = null;
    function openHay(li) {
      if (!hayEl || finished) { return; }
      hayLi = li;
      var pedida = num(li.dataset.pedida);
      hayEl.querySelector('.hay__nom').textContent = li.dataset.nombre;
      hayEl.querySelector('.hay__pidio').textContent = 'Pidió ' + fmt(pedida);
      var foto = hayEl.querySelector('.hay__foto');
      foto.src = li.dataset.foto;
      var nums = hayEl.querySelector('.hay__nums');
      var otra = hayEl.querySelector('.hay__otra');
      nums.innerHTML = '';
      if (pedida <= tope && pedida === Math.floor(pedida)) {
        for (var i = 0; i <= pedida; i++) {
          var b = document.createElement('button');
          b.type = 'button';
          b.className = 'dn-btn hay__n' + (i === pedida ? ' hay__n--todas' : '');
          b.textContent = i === pedida ? i + ' · todas' : String(i);
          b.setAttribute('data-hay', i);
          nums.appendChild(b);
        }
        otra.hidden = true;
      } else {
        var t = document.createElement('button');
        t.type = 'button';
        t.className = 'dn-btn hay__n hay__n--todas';
        t.textContent = 'Están todas (' + fmt(pedida) + ')';
        t.setAttribute('data-hay', pedida);
        nums.appendChild(t);
        otra.hidden = false;
        otra.querySelector('input').max = pedida;
        otra.querySelector('input').value = li.dataset.estado === 'falta' ? li.dataset.hay : '';
      }
      sheets.hay.open();
    }
    function setHay(v) {
      var pedida = num(hayLi.dataset.pedida);
      if (!(v >= 0) || v > pedida) { DN.toast('Poné cuántas hay: de 0 a ' + fmt(pedida) + '.', 'err'); return; }
      var li = hayLi;
      sheets.hay.close();
      enqueue('hay', parseInt(li.dataset.id, 10), v);
    }
    if (hayEl) {
      hayEl.querySelector('.hay__nums').addEventListener('click', function (ev) {
        var b = ev.target.closest('[data-hay]');
        if (b) { setHay(num(b.getAttribute('data-hay'))); }
      });
      hayEl.querySelector('.hay__otra').addEventListener('submit', function (ev) {
        ev.preventDefault();
        setHay(num(ev.target.querySelector('input').value));
      });
    }

    // ---- footer sheets
    DN.$$('[data-sheet]').forEach(function (a) {
      a.addEventListener('click', function (ev) {
        var id = a.getAttribute('data-sheet');
        if (!sheets[id]) { return; }
        ev.preventDefault();
        if (id === 'todo-listo') {
          var pend = rows().filter(function (r) { return r.dataset.estado === 'pend'; }).length;
          var q = DN.$('#todo-listo .tl-quedan');
          q.textContent = pend === 1 ? 'Queda 1 sin tildar. ¿Se entrega completo? Elegí los bultos y listo.'
            : 'Quedan ' + pend + ' sin tildar. ¿Se entregan completos? Elegí los bultos y listo.';
          q.hidden = !pend;
        }
        sheets[id].open();
      });
    });
    var fin = DN.$('form[data-trad="terminar"]');
    if (fin) {
      fin.querySelector('.bul__mas').addEventListener('click', function () {
        var otro = fin.querySelector('.bul__otro');
        otro.hidden = false;
        otro.querySelector('input').focus();
      });
      fin.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var b = ev.submitter || fin.ultimo;
        var n = b && b.name === 'bultos' ? parseInt(b.value, 10) : parseInt(fin.bultos_otro.value, 10);
        if (!(n >= 1 && n <= 999)) { DN.toast('Poné en cuántos bultos quedó el pedido.', 'err'); return; }
        sheets['todo-listo'].close();
        finished = true;
        enqueue('terminar', 0, n);
        body.classList.add('arm-fin');
        DN.banner('Terminado en ' + n + (n === 1 ? ' bulto' : ' bultos') +
          (navigator.onLine === false ? '. Guardado en el celular: se envía solo cuando vuelva la señal.' : '. Enviando…'), 'ok');
      });
      fin.addEventListener('click', function (ev) {
        var b = ev.target.closest('button[type="submit"]');
        if (b) { fin.ultimo = b; }
      });
    }
    var tarde = DN.$('form[data-trad="mas-tarde"]');
    if (tarde) {
      tarde.addEventListener('submit', function (ev) {
        ev.preventDefault();
        var btn = tarde.querySelector('button[type="submit"]');
        btn.disabled = true;
        // what is still on the phone goes first, then the release
        flush().then(function () {
          if (queue.length) { throw new Error('Sin señal: «Más tarde» necesita conexión. Lo marcado sigue guardado.'); }
          return DN.api(tarde.action, {body: new FormData(tarde)});
        }).then(function (j) { DN.up(j.url); }, function (e) { btn.disabled = false; DN.fail(e); });
      });
    }
    // the other forms of the page (tomar, corregir bultos) as in «Tarjetas»
    document.addEventListener('submit', function (ev) {
      var form = ev.target;
      var how = form.dataset.fetch;
      if (!how || !window.fetch) { return; }
      ev.preventDefault();
      var b = form.querySelector('button[type="submit"]');
      if (b) { b.disabled = true; }
      DN.api(form.action, {body: new FormData(form)}).then(function (j) {
        if (how === 'bultos') {
          b.disabled = false;
          sheets.corregir.close();
          DN.toast('Guardado: ' + j.bultos + (j.bultos === 1 ? ' bulto.' : ' bultos.'), 'ok');
        } else { location.reload(); }
      }, function (e) { if (b) { b.disabled = false; } DN.fail(e); });
    });
    // «Agregar» (by phone) needs signal; the new line shows after a reload
    var buscador = DN.$('form[data-buscar]');
    var resultados = DN.$('#resultados');
    if (buscador) {
      var espera;
      var buscar = function () {
        var q = buscador.q.value.trim();
        if (q.length < 2) { resultados.innerHTML = ''; return; }
        DN.api(buscador.dataset.buscar + '?q=' + encodeURIComponent(q)).then(function (html) { resultados.innerHTML = html; }, DN.fail);
      };
      buscador.q.addEventListener('input', function () { clearTimeout(espera); espera = setTimeout(buscar, 300); });
      buscador.addEventListener('submit', function (ev) { ev.preventDefault(); buscar(); });
      resultados.addEventListener('submit', function (ev) {
        var form = ev.target;
        if (form.dataset.fetch !== 'agregar') { return; }
        ev.preventDefault();
        ev.stopPropagation();
        flush().then(function () { return DN.api(form.action, {body: new FormData(form)}); })
          .then(function () { location.reload(); }, DN.fail);
      }, true);
    }
  }

  // ------------------------------------------------------------ list
  // 19.0.1.127.0: «Buscar cliente o pedido» hides the cards that do not
  // match (customer name or order number, without accents) and the zones
  // left empty; the query survives the refresh of #lista.
  function fold(text) {
    return String(text || '').toLowerCase().normalize('NFD').replace(/[̀-ͯ]/g, '');
  }
  function buscar(DN, box) {
    var input = DN.$('[data-arm-buscar]');
    var nada = DN.$('[data-arm-nada]');
    // 19.0.1.129.42 (ui_v2): the state chips; one at a time, tap again = all
    var chips = DN.$$('[data-arm-estado]');
    var estado = '';
    chips.forEach(function (chip) {
      chip.addEventListener('click', function () {
        var key = chip.getAttribute('data-arm-estado');
        estado = estado === key ? '' : key;
        chips.forEach(function (c) {
          c.setAttribute('aria-pressed', c.getAttribute('data-arm-estado') === estado ? 'true' : 'false');
        });
        apply();
      });
    });
    function apply() {
      if (!input) { return; }
      var words = fold(input.value).split(/\s+/).filter(Boolean);
      var shown = 0;
      DN.$$('.arm-zona', box).forEach(function (zona) {
        var visible = 0;
        DN.$$('.arm-ped', zona).forEach(function (card) {
          var nom = card.querySelector('.arm-ped__nom');
          var text = fold((nom ? nom.textContent : '') + ' ' + (card.getAttribute('data-pedido') || ''));
          var ok = Armado.mostrar(text, words, estado, card.getAttribute('data-estado') || '');
          card.hidden = !ok;
          // the «Imprimir etiquetas» link under an «Armado» card goes with it
          var next = card.nextElementSibling;
          if (next && next.classList.contains('arm-ped__print')) { next.hidden = !ok; }
          if (ok) { visible += 1; }
        });
        zona.hidden = visible === 0;
        shown += visible;
      });
      if (nada) { nada.hidden = !((words.length || estado) && shown === 0); }
    }
    if (input) {
      input.addEventListener('input', apply);
      // 19.0.1.129.16: Enter only closes the phone keyboard (like /pedido)
      input.addEventListener('keydown', function (ev) {
        if (ev.key === 'Enter' || ev.keyCode === 13) {
          ev.preventDefault();
          input.blur();
        }
      });
    }
    return apply;
  }

  function lista(DN) {
    var box = DN.$('#lista');
    var filtrar = buscar(DN, box);
    var last = Date.now();
    var busy = false;
    function refresh() {
      if (busy || document.visibilityState !== 'visible') { return; }
      busy = true;
      DN.api(location.pathname).then(function (html) {
        var doc = new DOMParser().parseFromString(html, 'text/html');
        var fresh = doc.getElementById('lista');
        if (fresh) { box.innerHTML = fresh.innerHTML; filtrar(); }
        // 19.0.1.129.42: the chips' counts follow the list
        DN.$$('[data-arm-estado]').forEach(function (chip) {
          var twin = doc.querySelector('[data-arm-estado="' + chip.getAttribute('data-arm-estado') + '"] .arm-chip__n');
          var n = chip.querySelector('.arm-chip__n');
          if (twin && n) { n.textContent = twin.textContent; }
        });
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
        sending(form, DN.api(form.action, {body: new FormData(form)}).then(function (j) { irDespues(DN, j.url); }));
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
      var pend = all.filter(function (r) { return r.dataset.estado === 'pend'; }).length;
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
