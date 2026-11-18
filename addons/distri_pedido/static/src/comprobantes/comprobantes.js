/* «Subir comprobante» (19.0.1.99.0, audit 3a): views/comprobantes.xml.
 * Uses the kit (../kit/dn.js, window.DN). Without JavaScript the form posts
 * the chosen files and the server answers with the page.
 *
 * - The two file inputs (camera, gallery) add to one list shown as
 *   thumbnails («Para subir»); a thumbnail is removed with a tap.
 * - «¿De qué cliente?» searches /comprobantes/clientes as you type.
 * - «Subir» sends everything with DN.api (fetch); the answer says per file
 *   «Subido», «Ya estaba cargado» or why it was refused, and refreshes
 *   «Subidos hoy por vos». While a file is being read the list refreshes
 *   itself (/comprobantes/hoy) until the amounts are in.
 */
(function () {
  'use strict';
  var MAX = 10;

  function money(n) {
    return '$ ' + Number(n || 0).toLocaleString('es-AR', {minimumFractionDigits: 0, maximumFractionDigits: 2});
  }

  function init() {
    var form = DN.$('form[data-comp-form]');
    if (!form) { return; }
    var files = [];
    var sel = DN.$('[data-comp-sel]');
    var thumbs = DN.$('[data-comp-thumbs]');
    var shared = DN.$('[name=compartido]', form);
    var sharedCount = shared ? DN.$$('li', thumbs).length : 0;
    var sendBtn = DN.$('[data-comp-send]');

    function paint() {
      DN.$$('li[data-comp-new]', thumbs).forEach(function (li) { li.remove(); });
      files.forEach(function (file, n) {
        var li = document.createElement('li');
        li.className = 'comp-thumb';
        li.setAttribute('data-comp-new', String(n));
        var pdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
        li.innerHTML = (pdf ? '<span class="comp-thumb__pdf">PDF</span>' : '<img alt="Comprobante elegido">') +
          '<span class="comp-thumb__name">' + DN.h(file.name) + '</span>' +
          '<button type="button" class="comp-thumb__x" aria-label="Sacar este archivo" data-comp-x="' + n + '">×</button>';
        if (!pdf) {
          var url = URL.createObjectURL(file);
          var img = li.querySelector('img');
          img.onload = function () { URL.revokeObjectURL(url); };
          img.src = url;
        }
        thumbs.appendChild(li);
      });
      sel.hidden = !files.length && !sharedCount;
      sendBtn.textContent = (files.length + sharedCount) > 1 ? 'Subir ' + (files.length + sharedCount) : 'Subir';
    }

    DN.$$('[data-comp-file]', form).forEach(function (input) {
      input.addEventListener('change', function () {
        Array.prototype.forEach.call(input.files || [], function (f) {
          if (files.length + sharedCount < MAX) { files.push(f); }
          else { DN.toast('Hasta ' + MAX + ' archivos por vez.', 'err'); }
        });
        input.value = '';
        paint();
      });
    });
    thumbs.addEventListener('click', function (ev) {
      var x = ev.target.closest('[data-comp-x]');
      if (!x) { return; }
      files.splice(parseInt(x.getAttribute('data-comp-x'), 10), 1);
      paint();
    });

    // ---- customer search
    var q = DN.$('[data-comp-q]', form);
    var res = DN.$('[data-comp-res]', form);
    var idInput = DN.$('[data-comp-cliente-id]', form);
    var txt = DN.$('[data-comp-cliente-txt]', form);
    var defaultTxt = txt.innerHTML;
    var timer = null, seq = 0;
    function choose(id, name) {
      idInput.value = id || '';
      res.hidden = true;
      if (id) {
        q.value = name;
        txt.innerHTML = 'Queda como comprobante de <b>' + DN.h(name) + '</b>. <button type="button" class="dn-link" data-comp-clear="">Cambiar</button>';
      } else {
        txt.innerHTML = defaultTxt;
      }
    }
    q.addEventListener('input', function () {
      idInput.value = '';
      txt.innerHTML = defaultTxt;
      clearTimeout(timer);
      var text = q.value.trim();
      if (text.length < 2) { res.hidden = true; return; }
      timer = setTimeout(function () {
        var mine = ++seq;
        DN.api('/comprobantes/clientes?q=' + encodeURIComponent(text)).then(function (r) {
          if (mine !== seq) { return; }
          var items = (r && r.results) || [];
          res.innerHTML = items.length ? items.map(function (p) {
            return '<li><button type="button" data-comp-pick="' + p.id + '" data-name="' + DN.h(p.name) + '">' +
              '<b>' + DN.h(p.name) + '</b>' + (p.ref || p.town ? '<small>' + DN.h([p.ref, p.town].filter(Boolean).join(' · ')) + '</small>' : '') +
              '</button></li>';
          }).join('') : '<li class="comp-res__none">Ningún cliente con ese nombre.</li>';
          res.hidden = false;
        }, function () {});
      }, 250);
    });
    form.addEventListener('click', function (ev) {
      var pick = ev.target.closest('[data-comp-pick]');
      if (pick) { choose(pick.getAttribute('data-comp-pick'), pick.getAttribute('data-name')); return; }
      if (ev.target.closest('[data-comp-clear]')) { q.value = ''; choose('', ''); q.focus(); }
    });

    // ---- «Subidos hoy por vos»
    var list = DN.$('[data-comp-list]');
    var count = DN.$('[data-comp-count]');
    var empty = DN.$('[data-comp-empty]');
    var poll = null;
    function paintUploads(rows) {
      count.textContent = rows.length;
      if (empty) { empty.hidden = !!rows.length; }
      list.innerHTML = rows.map(function (u) {
        var open = u.reading || u.state === 'pending' || u.state === 'no_money';
        var mark = open ? '⏳' : (u.state === 'matched' || u.state === 'registered' ? '✓' : '•');
        return '<li class="comp-item"><span class="comp-item__st comp-item__st--' + (u.reading ? 'reading' : DN.h(u.state || 'pending')) + '">' + mark + '</span>' +
          '<span class="comp-item__amt dn-num">' + (u.amount ? money(u.amount) : (u.reading ? 'leyendo…' : 'sin importe')) + '</span>' +
          '<span class="comp-item__who">' + DN.h(u.partner || 'sin cliente') + '</span>' +
          '<small class="comp-item__state">' + DN.h(u.state_label) + '</small></li>';
      }).join('');
      var reading = rows.some(function (u) { return u.reading; });
      clearTimeout(poll);
      if (reading) {
        poll = setTimeout(function () {
          DN.api('/comprobantes/hoy').then(function (r) { if (r && r.uploads) { paintUploads(r.uploads); } }, function () {});
        }, 5000);
      }
    }
    if (DN.$('.comp-item__st--reading', list)) { paintUploads([]); DN.api('/comprobantes/hoy').then(function (r) { paintUploads(r.uploads || []); }, function () {}); }

    // ---- send
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (!files.length && !sharedCount) {
        DN.toast('Primero sacá una foto o elegí el comprobante.', 'err');
        return;
      }
      var body = new FormData();
      body.append('csrf_token', DN.$('[name=csrf_token]', form).value);
      body.append('cliente_id', idInput.value || '');
      if (shared) { body.append('compartido', shared.value); }
      files.forEach(function (f) { body.append('archivos', f, f.name); });
      sendBtn.disabled = true;
      sendBtn.textContent = 'Subiendo…';
      DN.api(form.action, {body: body, timeout: 120000}).then(function (r) {
        sendBtn.disabled = false;
        var results = (r && r.results) || [];
        var fresh = results.filter(function (x) { return x.status === 'nuevo'; }).length;
        var repeated = results.filter(function (x) { return x.status === 'repetido'; });
        var bad = results.filter(function (x) { return x.status === 'error'; });
        var parts = [];
        if (fresh) { parts.push(fresh === 1 ? '1 comprobante subido' : fresh + ' comprobantes subidos'); }
        if (repeated.length) { parts.push(repeated.length === 1 ? '1 ya estaba cargado: ' + repeated[0].message.replace(/^Ya estaba cargado /, '') : repeated.length + ' ya estaban cargados'); }
        if (bad.length) { parts.push(bad.map(function (x) { return x.name + ': ' + x.message; }).join(' · ')); }
        DN.toast(parts.join('. ') + '.', bad.length || (repeated.length && !fresh) ? 'err' : 'ok');
        files = [];
        if (shared) {
          shared.remove(); shared = null; sharedCount = 0;
          DN.$$('li', thumbs).forEach(function (li) { li.remove(); });
          if (history.replaceState) { history.replaceState(history.state, '', '/comprobantes'); }
        }
        q.value = ''; choose('', '');
        paint();
        if (r && r.uploads) { paintUploads(r.uploads); }
      }, function (e) {
        sendBtn.disabled = false;
        paint();
        DN.fail(e);
      });
    });
    paint();
  }
  if (window.DN && DN.ready) { init(); } else { document.addEventListener('dn:ready', init); }
})();
