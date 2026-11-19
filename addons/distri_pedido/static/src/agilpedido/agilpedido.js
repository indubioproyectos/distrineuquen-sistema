/* «Pedidos de AgilPedido» (19.0.1.111.0, views/agilpedido_app.xml,
 * controllers/agilpedido.py). Uses the kit (../kit/dn.js, window.DN).
 *
 * - List: .ag-tab[data-ver] filters the cards in the page.
 * - Load: «Leer el mensaje» (fetch, then the lines page), the customer
 *   search and suggestions, the price list.
 * - Lines (load and order): every action goes by fetch and answers JSON
 *   with the lines rendered again ([data-ag-board] is replaced):
 *   [data-ag-ok] «Está bien», [data-ag-corregir] opens «Corregir producto»
 *   (the right column from 1024 px, a bottom sheet on the phone),
 *   [data-ag-quitar], [data-ag-agregar] «Agregar renglón a mano», and
 *   [data-ag-final] «Crear el presupuesto» / «Aprobar el pedido», disabled
 *   by the server while a line still needs fixing.
 */
(function () {
  'use strict';

  function money(n) {
    var v = Math.round((+n || 0) * 100) / 100;
    var s = v.toFixed(2).split('.');
    var ent = s[0].replace(/\B(?=(\d{3})+(?!\d))/g, '.');
    return '$ ' + ent + (s[1] === '00' ? '' : ',' + s[1]);
  }
  function num(n) {
    var v = Math.round((+n || 0) * 10000) / 10000;
    return String(v).replace('.', ',');
  }
  function parseQty(v) { return parseFloat(String(v || '').replace(',', '.')) || 0; }

  // ---------------------------------------------------------------- list
  function startList(main) {
    var ver = main.getAttribute('data-ver') || 'todos';
    function paint() {
      var shown = 0;
      DN.$$('.ag-card', main).forEach(function (c) {
        var on = ver === 'todos' || (ver === 'corregir' && c.hasAttribute('data-fix'))
          || (ver === 'total' && c.hasAttribute('data-total'));
        c.hidden = !on;
        if (on) { shown += 1; }
      });
      DN.$$('.ag-tab', main).forEach(function (t) {
        var on = t.getAttribute('data-ver') === ver;
        t.classList.toggle('on', on);
        if (on) { t.setAttribute('aria-current', 'true'); } else { t.removeAttribute('aria-current'); }
      });
      var empty = DN.$('[data-ag-empty]', main);
      if (empty) { empty.hidden = shown > 0; }
    }
    DN.$$('.ag-tab', main).forEach(function (t) {
      t.addEventListener('click', function (ev) {
        ev.preventDefault();
        ver = t.getAttribute('data-ver');
        history.replaceState(history.state, '', ver === 'todos' ? '/agilpedido' : '/agilpedido?ver=' + ver);
        paint();
      });
    });
    paint();
  }

  // ---------------------------------------------------------------- lines
  function startLines() {
    var holder = DN.$('[data-ag-board]');
    var panelEl = DN.$('[data-ag-panel]');
    var wide = window.matchMedia('(min-width: 1024px)');
    var sheet = panelEl ? DN.sheet(panelEl) : null;
    var row = null, mode = 'corregir', picked = null, timer = null, seq = 0;

    function board() { return DN.$('.ag-board', holder); }
    function base() { return board().getAttribute('data-base'); }
    function kind() { return board().getAttribute('data-kind'); }
    function setBoard(html) {
      var keep = row && row.getAttribute('data-id');
      holder.innerHTML = html;
      if (keep && wide.matches) {
        var again = DN.$('.ag-row[data-id="' + keep + '"]', holder);
        if (again) { again.classList.add('sel'); }
      }
    }
    function act(url, body, done) {
      var b = board();
      b.classList.add('ag-busy');
      return DN.api(url, {body: body || {}}).then(function (j) {
        if (j.board) { setBoard(j.board); }
        if (j.resumen !== undefined) {
          var r = DN.$('[data-ag-resumen]');
          if (r) { r.textContent = j.resumen; }
        }
        if (j.message) { DN.toast(j.message, 'ok'); }
        if (done) { done(j); }
        return j;
      }, function (e) {
        var bb = board();
        if (bb) { bb.classList.remove('ag-busy'); }
        DN.fail(e);
        throw e;
      });
    }

    // ------------------------------------------------------------ panel
    var P = {};
    if (panelEl) {
      ['vacio', 'form', 'q', 'res', 'uom', 'uom-box', 'otra', 'otra-box', 'factor', 'factor-box', 'cant',
        'cant-box', 'queda', 'queda-txt', 'queda-nota', 'guardar', 'panel-t', 'ro', 'ro-texto', 'ro-cant',
        'ro-importe', 'ro-why', 'ro-why-t'].forEach(function (k) {
        P[k] = DN.$('[data-ag-' + k + ']', panelEl);
      });
      sheet.onclose = function () { clearSel(); };
    }
    function clearSel() {
      DN.$$('.ag-row.sel', holder).forEach(function (r) { r.classList.remove('sel'); });
    }
    function resetPanel() {
      P.vacio.hidden = false;
      P.form.hidden = true;
      row = null;
      picked = null;
      clearSel();
    }
    function selectedUnit() {
      if (!picked) { return null; }
      var id = +P.uom.value;
      return picked.units.filter(function (u) { return u.id === id; })[0] || picked.units[0];
    }
    function preview() {
      var unit = selectedUnit();
      P.guardar.disabled = !picked;
      if (!picked || !unit) { P.queda.hidden = true; return; }
      var factor = mode === 'corregir' && P.otra.checked ? parseQty(P.factor.value) : 1;
      var qty = mode === 'nuevo' ? parseQty(P.cant.value) : parseQty(row.getAttribute('data-cant')) * factor;
      var sub = unit.price * qty;
      var unitTxt = picked.units.length > 1 || /x\s*\d/i.test(unit.name) ? ' (' + unit.name + ')' : '';
      P['queda-txt'].textContent = num(qty) + ' × ' + picked.name + unitTxt + ' = ' + money(sub);
      var importe = row ? parseFloat(row.getAttribute('data-importe')) || 0 : 0;
      var nota = '', ok = true;
      if (mode === 'corregir' && importe > 0) {
        ok = Math.abs(sub - importe) <= 1;
        nota = ok ? 'Coincide con AgilPedido. La próxima vez se carga solo.'
          : 'No coincide con AgilPedido (' + money(importe) + '): revisá cómo viene.';
      } else if (mode === 'corregir') {
        nota = 'La próxima vez se carga solo.';
      }
      P['queda-nota'].textContent = nota;
      P.queda.classList.toggle('ag-queda--no', !ok);
      P.queda.hidden = false;
    }
    function fillUnits(product, uomId) {
      P.uom.innerHTML = product.units.map(function (u) {
        return '<option value="' + u.id + '">' + DN.h(u.name) + ' · ' + DN.h(u.price_text) + '</option>';
      }).join('');
      var want = product.units.some(function (u) { return u.id === uomId; }) ? uomId : product.unit_default;
      P.uom.value = String(want);
      P['uom-box'].hidden = false;
      P['otra-box'].hidden = mode !== 'corregir';
    }
    function pick(product, uomId) {
      picked = product;
      DN.$$('button', P.res).forEach(function (b) { b.classList.toggle('on', +b.getAttribute('data-id') === product.id); });
      fillUnits(product, uomId);
      preview();
    }
    function search(q, autopick) {
      clearTimeout(timer);
      timer = setTimeout(function () {
        if ((q || '').trim().length < 2) { P.res.innerHTML = ''; return; }
        var p = new URLSearchParams();
        p.set('q', q);
        p.set('may', row ? row.getAttribute('data-may') : '0');
        p.set('cant', row ? row.getAttribute('data-cant') : (P.cant.value || '1'));
        p.set('nombre', row ? row.getAttribute('data-texto') : '');
        var mine = ++seq;
        DN.api(base() + '/productos?' + p.toString()).then(function (j) {
          if (mine !== seq) { return; }
          var list = j.productos || [];
          P.res.innerHTML = list.length ? list.map(function (x, i) {
            var u = x.units[0];
            var extra = [u ? u.price_text + ' ' + (x.units.length > 1 ? 'la ' + DN.h(u.name).toLowerCase() : 'la unidad') : '',
              x.stock, x.code].filter(Boolean).join(' · ');
            return '<button type="button" role="option" data-i="' + i + '" data-id="' + x.id + '"><b>' + DN.h(x.name) + '</b>'
              + '<span class="ag-mut ag-small dn-num">' + DN.h(extra) + '</span></button>';
          }).join('') : '<p class="ag-mut ag-small">No hay productos con ese nombre o código.</p>';
          P.res._list = list;
          if (autopick) {
            var want = autopick.product;
            var hit = list.filter(function (x) { return x.id === want; })[0];
            if (hit) { pick(hit, autopick.uom); }
          }
        }, DN.fail);
      }, autopick ? 0 : 300);
    }
    function openPanel(r, how) {
      mode = how;
      row = r;
      picked = null;
      clearSel();
      if (r) { r.classList.add('sel'); }
      P.vacio.hidden = true;
      P.form.hidden = false;
      P.ro.hidden = !r;
      P['panel-t'].textContent = how === 'nuevo' ? 'Agregar renglón a mano' : 'Corregir producto de AgilPedido';
      P.guardar.textContent = how === 'nuevo' ? 'Agregar renglón' : 'Corregir y recordar';
      P['cant-box'].hidden = how !== 'nuevo';
      P['uom-box'].hidden = true;
      P['otra-box'].hidden = true;
      P['factor-box'].hidden = true;
      P.otra.checked = false;
      P.factor.value = '1';
      P.cant.value = '1';
      P.queda.hidden = true;
      P.guardar.disabled = true;
      P.res.innerHTML = '';
      if (r) {
        P['ro-texto'].textContent = r.getAttribute('data-texto') || '(agregado a mano)';
        P['ro-cant'].textContent = num(r.getAttribute('data-cant'));
        P['ro-importe'].textContent = r.getAttribute('data-sub');
        var why = r.getAttribute('data-motivo') || '';
        P['ro-why'].textContent = why;
        P['ro-why'].hidden = P['ro-why-t'].hidden = !why;
        var factor = parseFloat(r.getAttribute('data-factor')) || 1;
        if (factor !== 1) { P.otra.checked = true; P.factor.value = num(factor); P['factor-box'].hidden = false; }
      }
      P.q.value = r ? (r.getAttribute('data-buscar') || '') : '';
      var product = r ? +r.getAttribute('data-product') : 0;
      if (P.q.value) { search(P.q.value, product ? {product: product, uom: +r.getAttribute('data-uom')} : null); }
      if (!wide.matches) { sheet.open(); }
      P.q.focus();
    }
    if (panelEl) {
      P.q.addEventListener('input', function () { search(P.q.value, null); });
      P.q.addEventListener('keydown', function (ev) { if (ev.key === 'Enter') { ev.preventDefault(); search(P.q.value, null); } });
      P.res.addEventListener('click', function (ev) {
        var b = ev.target.closest('button[data-i]');
        if (!b) { return; }
        pick(P.res._list[+b.getAttribute('data-i')], 0);
      });
      P.uom.addEventListener('change', preview);
      P.otra.addEventListener('change', function () {
        P['factor-box'].hidden = !P.otra.checked;
        if (!P.otra.checked) { P.factor.value = '1'; }
        preview();
      });
      P.factor.addEventListener('input', preview);
      P.cant.addEventListener('input', preview);
      P.guardar.addEventListener('click', function () {
        var unit = selectedUnit();
        if (!picked || !unit) { DN.toast('Elegí el producto de Odoo.', 'err'); return; }
        var body = {product_id: picked.id, uom_id: unit.id};
        var url;
        if (mode === 'nuevo') {
          body.cantidad = P.cant.value;
          url = base() + '/agregar';
        } else {
          if (P.otra.checked) { body.otra_cantidad = '1'; body.factor = P.factor.value; }
          url = base() + '/renglon/' + row.getAttribute('data-id') + '/corregir';
        }
        P.guardar.disabled = true;
        act(url, body, function () {
          if (sheet && !wide.matches) { sheet.close(); }
          resetPanel();
        }).catch(function () { P.guardar.disabled = false; });
      });
      DN.$$('[data-dn-close]', panelEl).forEach(function (b) {
        b.addEventListener('click', function () { if (wide.matches) { resetPanel(); } });
      });
    }

    // ------------------------------------------------------------ line buttons
    holder.addEventListener('click', function (ev) {
      var t = ev.target.closest('[data-ag-ok],[data-ag-corregir],[data-ag-quitar],[data-ag-agregar],[data-ag-final]');
      if (!t) { return; }
      var r = t.closest('.ag-row');
      if (t.hasAttribute('data-ag-ok')) {
        t.disabled = true;
        act(base() + '/renglon/' + r.getAttribute('data-id') + '/ok').catch(function () { t.disabled = false; });
      } else if (t.hasAttribute('data-ag-corregir')) {
        openPanel(r, 'corregir');
      } else if (t.hasAttribute('data-ag-quitar')) {
        DN.confirm('¿Quitamos este renglón del pedido?', 'Quitar').then(function (yes) {
          if (yes) { act(base() + '/renglon/' + r.getAttribute('data-id') + '/quitar').catch(function () {}); }
        });
      } else if (t.hasAttribute('data-ag-agregar')) {
        openPanel(null, 'nuevo');
      } else if (t.hasAttribute('data-ag-final')) {
        t.disabled = true;
        var url = kind() === 'carga' ? base() + '/crear' : base() + '/aprobar';
        DN.api(url, {body: {}}).then(function (j) {
          if (j.url) { location.href = j.url; return; }
          if (j.board) { setBoard(j.board); }
          if (j.message) { DN.toast(j.message, 'ok'); }
        }, function (e) { t.disabled = false; DN.fail(e); });
      }
    });

    // ------------------------------------------------------------ load: customer and list
    var imp = document.body.getAttribute('data-imp');
    var cli = DN.$('[data-ag-cli]');
    var cliRes = DN.$('[data-ag-cli-res]');
    function setCliente(id, name) {
      act('/agilpedido/cargar/' + imp + '/datos', {partner_id: id}, function (j) {
        cli.value = j.cliente || name || '';
        cli.setAttribute('data-actual', cli.value);
        cliRes.hidden = true;
        var sug = DN.$('[data-ag-sug]');
        if (sug) { sug.hidden = true; }
      }).catch(function () {});
    }
    if (imp && cli) {
      var ct = null, cseq = 0;
      cli.addEventListener('input', function () {
        clearTimeout(ct);
        ct = setTimeout(function () {
          var q = cli.value.trim();
          if (q.length < 3) { cliRes.hidden = true; return; }
          var mine = ++cseq;
          DN.api('/agilpedido/cargar/' + imp + '/clientes?q=' + encodeURIComponent(q)).then(function (j) {
            if (mine !== cseq) { return; }
            var list = j.clientes || [];
            cliRes.innerHTML = list.length ? list.map(function (c) {
              return '<button type="button" data-ag-cliente="' + c.id + '" data-name="' + DN.h(c.name) + '"><b>' + DN.h(c.name)
                + '</b><span class="ag-mut ag-small">' + DN.h(c.detail) + '</span></button>';
            }).join('') : '<p class="ag-mut ag-small">No hay clientes con ese nombre, CUIT o teléfono.</p>';
            cliRes.hidden = false;
          }, DN.fail);
        }, 300);
      });
      cli.addEventListener('blur', function () {
        setTimeout(function () { if (!cliRes.matches(':hover')) { cliRes.hidden = true; } }, 200);
      });
      document.addEventListener('click', function (ev) {
        var b = ev.target.closest('[data-ag-cliente]');
        if (b) { setCliente(b.getAttribute('data-ag-cliente'), b.getAttribute('data-name')); }
      });
      var lp = DN.$('[data-ag-lista]');
      if (lp) {
        lp.addEventListener('change', function () {
          act('/agilpedido/cargar/' + imp + '/datos', {pricelist_id: lp.value}).catch(function () {});
        });
      }
    }
  }

  // ---------------------------------------------------------------- load: read
  function startRead(form) {
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var text = DN.$('textarea', form).value;
      if (!text.trim()) { DN.toast('Pegá primero el mensaje.', 'err'); return; }
      var btn = DN.$('button[type=submit]', form);
      btn.disabled = true;
      DN.api(form.getAttribute('action'), {body: new FormData(form)}).then(function (j) {
        if (j.url) { location.replace(j.url); return; }
        btn.disabled = false;
        var holder = DN.$('[data-ag-board]');
        if (j.board && holder) { holder.innerHTML = j.board; }
        var r = DN.$('[data-ag-resumen]');
        if (r && j.resumen !== undefined) { r.textContent = j.resumen; }
        if (j.message) { DN.toast(j.message, 'ok'); }
      }, function (e) { btn.disabled = false; DN.fail(e); });
    });
  }

  function start() {
    var list = DN.$('.ag--lista .ag-main');
    if (list) { startList(list); }
    var read = DN.$('[data-ag-leer]');
    if (read) { startRead(read); }
    if (DN.$('[data-ag-board]')) { startLines(); }
  }

  if (window.DN && DN.ready) { start(); } else { document.addEventListener('dn:ready', start); }
})();
