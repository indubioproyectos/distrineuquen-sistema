/* «Planificación de rutas» (19.0.1.110.0, controllers/rutas.py). Vanilla JS
 * on the kit (window.DN). Every change is one POST to /rutas/accion/<action>
 * answered with the board's HTML, which replaces #rt-board; the rules live
 * on the server (models/planificacion.py). Drag and drop on the PC; every
 * drag also has a button in the «Mover a…» menu of the card (touch and
 * keyboard). The board refreshes itself every 15 s while nobody is using it.
 */
(function () {
  'use strict';
  var REFRESH_MS = 15000;
  var doc = document;
  var board = doc.getElementById('rt-board');
  if (!board) { return; }
  var fecha = doc.body.getAttribute('data-fecha') || '';
  var busy = false;
  var drag = null;
  var lastTouch = Date.now();

  function ready(fn) {
    if (window.DN && DN.ready) { fn(); } else { doc.addEventListener('dn:ready', fn); }
  }
  function toast(text, kind) {
    if (window.DN) { DN.toast(text, kind); } else if (text) { window.alert(text); }
  }

  // ------------------------------------------------------------ server

  function paint(html) {
    if (typeof html !== 'string') { return; }
    var q = doc.querySelector('[data-rt-buscar]');
    var text = q ? q.value : '';
    var on = doc.querySelector('.rt-zchip.on');
    var zone = on ? on.getAttribute('data-rt-zona') : '';
    board.innerHTML = html;
    var q2 = doc.querySelector('[data-rt-buscar]');
    if (q2 && text) { q2.value = text; }
    if (zone) {
      var chip = doc.querySelector('.rt-zchip[data-rt-zona="' + zone + '"]');
      if (chip) { pickZone(chip); }
    }
    filter();
  }

  function send(action, data) {
    if (busy) { return Promise.resolve(); }
    busy = true;
    board.classList.add('rt-busy');
    var body = new FormData();
    body.append('fecha', fecha);
    Object.keys(data || {}).forEach(function (k) { body.append(k, data[k]); });
    return DN.api('/rutas/accion/' + action, { body: body }).then(function (j) {
      paint(j.html);
      if (j.message) { toast(j.message, 'ok'); }
    }, function (e) {
      DN.fail(e);
      return refresh(true);
    }).then(function () {
      busy = false;
      board.classList.remove('rt-busy');
      lastTouch = Date.now();
    });
  }

  function idle() {
    return !busy && !drag && !doc.querySelector('.rt-board details[open], .rt-new[open]') &&
      !(doc.activeElement && board.contains(doc.activeElement) &&
        /^(SELECT|INPUT|TEXTAREA)$/.test(doc.activeElement.tagName));
  }

  function refresh(force) {
    if (!force && (!idle() || doc.visibilityState !== 'visible' || Date.now() - lastTouch < 4000)) {
      return Promise.resolve();
    }
    return DN.api('/rutas/tablero?fecha=' + encodeURIComponent(fecha)).then(function (j) {
      if (force || idle()) { paint(j.html); }
    }, function () { /* offline: the next refresh tries again */ });
  }

  // ------------------------------------------------------- filter (depot)

  function pickZone(chip) {
    doc.querySelectorAll('.rt-zchip').forEach(function (c) {
      var on = c === chip;
      c.classList.toggle('on', on);
      c.setAttribute('aria-pressed', on ? 'true' : 'false');
    });
  }
  function filter() {
    var q = doc.querySelector('[data-rt-buscar]');
    var text = q ? q.value.trim().toLowerCase() : '';
    var chip = doc.querySelector('.rt-zchip.on');
    var zone = chip ? chip.getAttribute('data-rt-zona') : '';
    doc.querySelectorAll('.rt-group').forEach(function (g) {
      var shown = 0;
      g.querySelectorAll('.rt-ord').forEach(function (o) {
        var ok = !text || (o.getAttribute('data-search') || '').indexOf(text) !== -1;
        o.hidden = !ok;
        if (ok) { shown += 1; }
      });
      g.hidden = (zone && g.getAttribute('data-zona') !== zone) || !shown;
    });
  }

  // ------------------------------------------------------------- reorder

  function stopIds(list) {
    return Array.prototype.map.call(list.querySelectorAll('.rt-stop[data-stop]'), function (li) {
      return li.getAttribute('data-stop');
    });
  }

  function step(btn) {
    var routeId = btn.getAttribute('data-route');
    var stop = btn.getAttribute('data-stop');
    var col = board.querySelector('.rt-col[data-route="' + routeId + '"] .rt-stops');
    if (!col) { return; }
    var ids = stopIds(col);
    var i = ids.indexOf(stop);
    var j = i + parseInt(btn.getAttribute('data-rt-step'), 10);
    if (i < 0 || j < 0 || j >= ids.length) { return; }
    ids.splice(j, 0, ids.splice(i, 1)[0]);
    send('orden', { route_id: routeId, stop_ids: ids.join(',') });
  }

  // ------------------------------------------------------------- dragging

  var ph = doc.createElement('div');
  ph.className = 'rt-ph';
  ph.setAttribute('aria-hidden', 'true');

  function dropZone(el) { return el && el.closest ? el.closest('[data-drop]') : null; }
  function beforeItem(zone, y) {
    var items = zone.querySelectorAll('.rt-stop[data-stop]');
    for (var i = 0; i < items.length; i += 1) {
      if (items[i] === drag.el) { continue; }
      var r = items[i].getBoundingClientRect();
      if (y < r.top + r.height / 2) { return items[i]; }
    }
    return null;
  }
  function clearOver() {
    doc.querySelectorAll('.rt-over').forEach(function (z) { z.classList.remove('rt-over'); });
    if (ph.parentNode) { ph.parentNode.removeChild(ph); }
  }

  board.addEventListener('dragstart', function (ev) {
    var el = ev.target.closest && ev.target.closest('[draggable="true"][data-order]');
    if (!el) { return; }
    drag = { el: el, order: el.getAttribute('data-order'), stop: el.getAttribute('data-stop') || '',
             from: el.getAttribute('data-from') || '0' };
    el.classList.add('rt-dragging');
    ev.dataTransfer.effectAllowed = 'move';
    try { ev.dataTransfer.setData('text/plain', drag.order); } catch (e) { /* old browsers */ }
  });
  board.addEventListener('dragend', function () {
    if (drag && drag.el) { drag.el.classList.remove('rt-dragging'); }
    clearOver();
    drag = null;
  });
  board.addEventListener('dragover', function (ev) {
    if (!drag) { return; }
    var zone = dropZone(ev.target);
    if (!zone) { return; }
    ev.preventDefault();
    ev.dataTransfer.dropEffect = 'move';
    doc.querySelectorAll('.rt-over').forEach(function (z) { if (z !== zone) { z.classList.remove('rt-over'); } });
    zone.classList.add('rt-over');
    if (zone.getAttribute('data-drop') !== '0') {
      var before = beforeItem(zone, ev.clientY);
      if (before) { zone.insertBefore(ph, before); } else { zone.appendChild(ph); }
    }
  });
  board.addEventListener('dragleave', function (ev) {
    var zone = dropZone(ev.target);
    if (zone && !zone.contains(ev.relatedTarget)) { zone.classList.remove('rt-over'); }
  });
  board.addEventListener('drop', function (ev) {
    if (!drag) { return; }
    var zone = dropZone(ev.target);
    if (!zone) { return; }
    ev.preventDefault();
    var target = zone.getAttribute('data-drop');
    var before = target !== '0' ? beforeItem(zone, ev.clientY) : null;
    var d = drag;
    clearOver();
    if (target === d.from) {
      if (target === '0' || !d.stop) { return; }
      var ids = stopIds(zone).filter(function (x) { return x !== d.stop; });
      var at = before ? ids.indexOf(before.getAttribute('data-stop')) : ids.length;
      ids.splice(at < 0 ? ids.length : at, 0, d.stop);
      send('orden', { route_id: target, stop_ids: ids.join(',') });
      return;
    }
    send('mover', { order_id: d.order, route_id: target,
                    before_id: before ? before.getAttribute('data-stop') : '' });
  });

  // -------------------------------------------------------------- clicks

  doc.addEventListener('click', function (ev) {
    var t = ev.target;
    var el;
    lastTouch = Date.now();
    // one menu open at a time; a click outside closes them
    doc.querySelectorAll('.rt-menu[open], .rt-new[open]').forEach(function (d) {
      if (!d.contains(t)) { d.removeAttribute('open'); }
    });
    if ((el = t.closest('[data-rt-move]'))) {
      el.closest('details').removeAttribute('open');
      send('mover', { order_id: el.getAttribute('data-order'), route_id: el.getAttribute('data-rt-move') });
    } else if ((el = t.closest('[data-rt-step]'))) {
      el.closest('details').removeAttribute('open');
      step(el);
    } else if ((el = t.closest('[data-rt-zona]'))) {
      pickZone(el);
      filter();
    } else if ((el = t.closest('[data-rt-assign]'))) {
      DN.confirm('Se suman a la ruta los pedidos armados de las zonas elegidas que no están en otra ruta. ¿Seguir?',
                 'Sí, asignar').then(function (yes) {
        if (yes) { send('asignar', { route_id: el.getAttribute('data-route') }); }
      });
    } else if ((el = t.closest('[data-rt-delete]'))) {
      DN.confirm('La ruta no tiene pedidos. ¿La borramos?', 'Sí, borrar').then(function (yes) {
        if (yes) { send('borrar', { route_id: el.getAttribute('data-route') }); }
      });
    } else if ((el = t.closest('[data-rt-next]'))) {
      send('nueva', { trip: el.getAttribute('data-trip'), driver_id: el.getAttribute('data-driver') });
    }
  });

  doc.addEventListener('change', function (ev) {
    var t = ev.target;
    if (t.matches('[data-rt-driver]')) {
      send('repartidor', { route_id: t.getAttribute('data-route'), driver_id: t.value });
    } else if (t.matches('[data-rt-fecha]') && t.value) {
      location.replace('/rutas?fecha=' + encodeURIComponent(t.value));
    }
  });

  doc.addEventListener('input', function (ev) {
    if (ev.target.matches('[data-rt-buscar]')) { filter(); }
  });

  doc.addEventListener('submit', function (ev) {
    var form = ev.target;
    var kind = form.getAttribute('data-rt-form');
    if (!kind) { return; }
    ev.preventDefault();
    if (kind === 'nueva') {
      var d = form.closest('details');
      if (d) { d.removeAttribute('open'); }
      send('nueva', { trip: form.trip.value, driver_id: form.driver_id.value });
    } else if (kind === 'zonas') {
      var ids = Array.prototype.filter.call(form.querySelectorAll('input[name=zone]'), function (x) {
        return x.checked;
      }).map(function (x) { return x.value; });
      send('zonas', { route_id: form.route_id.value, zone_ids: ids.join(',') });
    }
  });

  doc.addEventListener('keydown', function (ev) {
    if (ev.key === 'Escape') {
      doc.querySelectorAll('.rt-menu[open], .rt-new[open]').forEach(function (d) { d.removeAttribute('open'); });
    }
  });

  ready(function () {
    filter();
    setInterval(refresh, REFRESH_MS);
    doc.addEventListener('visibilitychange', function () {
      if (doc.visibilityState === 'visible') { refresh(); }
    });
  });
})();
