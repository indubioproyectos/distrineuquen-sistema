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
 * - 19.0.1.100.0, only when the page says the server takes them
 *   (data-comp-api="100" on the form: this file is served before the module
 *   is updated):
 *   · an optional «Importe» under each file, checked here with the /cobro
 *     rules (150.000, 150000,50) and again on the server;
 *   · «Girar» on each photo: the preview turns 90° clockwise per tap and the
 *     server stores the photo turned (the camera may hand over a sideways
 *     photo without EXIF orientation);
 *   sent as one «importes» / «giros» per new file and one
 *   «importe_compartido» / «giro_compartido» per shared file.
 *   · a row of «Subidos hoy por vos» opens «Ver comprobante» (row.url).
 */
(function () {
  'use strict';
  var MAX = 10;

  function money(n) {
    // like _money on the server: no cents when round, else both digits
    var value = Number(n || 0);
    var cents = Math.round(value * 100) % 100 ? 2 : 0;
    return '$ ' + value.toLocaleString('es-AR', {minimumFractionDigits: cents, maximumFractionDigits: 2});
  }

  // the Argentine way of /cobro (distri.cash.collection._cash_amount): the
  // number, 0 when empty, null when it cannot be understood
  var THOUSANDS = /^\d{1,3}(\.\d{3})+$/, DOT_DECIMAL = /^\d{4,}\.\d{1,2}$/;
  function parseAmount(text) {
    var raw = String(text || '').replace(/[$\s]/g, '');
    if (!raw) { return 0; }
    if (raw.indexOf(',') >= 0) {
      var whole = raw.split(',')[0];
      if (whole.indexOf('.') >= 0 && !THOUSANDS.test(whole)) { return null; }
      raw = raw.replace(/\./g, '').replace(',', '.');
    } else if (THOUSANDS.test(raw)) {
      raw = raw.replace(/\./g, '');
    } else if (raw.indexOf('.') >= 0 && !DOT_DECIMAL.test(raw)) {
      return null;
    }
    if (!/^\d+(\.\d+)?$/.test(raw)) { return null; }
    var value = Math.round(parseFloat(raw) * 100) / 100;
    return value > 0 && value <= 100000000 ? value : null;
  }
  var AMOUNT_HELP = 'Escribí 150000 o 150.000, con coma para los centavos (150000,50).';

  function amountField(value) {
    return '<label class="comp-thumb__amt"><span>Importe (opcional)</span>' +
      '<input class="dn-input dn-num" type="text" inputmode="decimal" autocomplete="off"' +
      ' maxlength="20" placeholder="Ej.: 150.000" enterkeyhint="done" data-comp-amt=""' +
      ' value="' + DN.h(value || '') + '"></label>';
  }

  // marks the input when its amount cannot be understood; true when fine
  function checkAmount(input) {
    var bad = parseAmount(input.value) === null;
    var label = input.closest('.comp-thumb__amt');
    var msg = label && label.querySelector('.comp-thumb__err');
    input.setAttribute('aria-invalid', bad ? 'true' : 'false');
    if (bad && !msg && label) {
      msg = document.createElement('small');
      msg.className = 'comp-thumb__err';
      msg.textContent = AMOUNT_HELP;
      label.appendChild(msg);
    } else if (!bad && msg) {
      msg.remove();
    }
    return !bad;
  }

  function turnPreview(li, degrees) {
    var img = li.querySelector('img');
    if (img) { img.style.transform = degrees ? 'rotate(' + degrees + 'deg)' : ''; }
    li.setAttribute('data-turn', String(degrees));
  }

  function init() {
    var form = DN.$('form[data-comp-form]');
    if (!form) { return; }
    var v100 = Number(form.getAttribute('data-comp-api') || 0) >= 100;
    var files = [];  // {file, amount, turn}
    var sel = DN.$('[data-comp-sel]');
    var thumbs = DN.$('[data-comp-thumbs]');
    var shared = DN.$('[name=compartido]', form);
    var sharedCount = shared ? DN.$$('li', thumbs).length : 0;
    var sendBtn = DN.$('[data-comp-send]');

    function paint() {
      DN.$$('li[data-comp-new]', thumbs).forEach(function (li) { li.remove(); });
      files.forEach(function (item, n) {
        var file = item.file;
        var li = document.createElement('li');
        li.className = 'comp-thumb';
        li.setAttribute('data-comp-new', String(n));
        var pdf = file.type === 'application/pdf' || /\.pdf$/i.test(file.name);
        li.innerHTML = (pdf ? '<span class="comp-thumb__pdf">PDF</span>' : '<img alt="Comprobante elegido">') +
          '<span class="comp-thumb__name">' + DN.h(file.name) + '</span>' +
          (v100 && !pdf ? '<button type="button" class="dn-btn dn-btn--outline comp-thumb__turn" data-comp-rot="" aria-label="Girar la foto">↻ Girar</button>' : '') +
          (v100 ? amountField(item.amount) : '') +
          '<button type="button" class="comp-thumb__x" aria-label="Sacar este archivo" data-comp-x="' + n + '">×</button>';
        if (!pdf) {
          var url = URL.createObjectURL(file);
          var img = li.querySelector('img');
          img.onload = function () { URL.revokeObjectURL(url); };
          img.src = url;
          turnPreview(li, item.turn);
        }
        thumbs.appendChild(li);
      });
      sel.hidden = !files.length && !sharedCount;
      sendBtn.textContent = (files.length + sharedCount) > 1 ? 'Subir ' + (files.length + sharedCount) : 'Subir';
    }

    DN.$$('[data-comp-file]', form).forEach(function (input) {
      input.addEventListener('change', function () {
        Array.prototype.forEach.call(input.files || [], function (f) {
          if (files.length + sharedCount < MAX) { files.push({file: f, amount: '', turn: 0}); }
          else { DN.toast('Hasta ' + MAX + ' archivos por vez.', 'err'); }
        });
        input.value = '';
        paint();
      });
    });
    thumbs.addEventListener('click', function (ev) {
      var x = ev.target.closest('[data-comp-x]');
      if (x) {
        files.splice(parseInt(x.getAttribute('data-comp-x'), 10), 1);
        paint();
        return;
      }
      var rot = ev.target.closest('[data-comp-rot]');
      if (!rot) { return; }
      var li = rot.closest('li');
      var degrees = (parseInt(li.getAttribute('data-turn') || '0', 10) + 90) % 360;
      turnPreview(li, degrees);
      var mine = li.getAttribute('data-comp-new');
      if (mine !== null) { files[parseInt(mine, 10)].turn = degrees; }
      var hidden = li.querySelector('[data-comp-turn]');
      if (hidden) { hidden.value = String(degrees); }
    });
    thumbs.addEventListener('input', function (ev) {
      var input = ev.target.closest('[data-comp-amt]');
      if (!input) { return; }
      var li = input.closest('li[data-comp-new]');
      if (li) { files[parseInt(li.getAttribute('data-comp-new'), 10)].amount = input.value; }
      if (input.getAttribute('aria-invalid') === 'true') { checkAmount(input); }
    });
    thumbs.addEventListener('focusout', function (ev) {
      var input = ev.target.closest('[data-comp-amt]');
      if (input) { checkAmount(input); }
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
        var amount = u.amount || u.amount_typed;  // amount_typed: 19.0.1.100.0 servers
        var note = '';
        if (u.amount_differs) {
          note = '<small class="comp-item__note comp-item__note--warn">Cargaste ' + money(u.amount_typed) +
            ' y el comprobante dice ' + money(u.amount) + ': lo revisa administración.</small>';
        } else if (u.amount_typed && u.reading) {
          note = '<small class="comp-item__note">Importe cargado por vos · leyendo el comprobante…</small>';
        }
        var inner = '<span class="comp-item__st comp-item__st--' + (u.reading ? 'reading' : DN.h(u.state || 'pending')) + '">' + mark + '</span>' +
          '<span class="comp-item__amt dn-num">' + (amount ? money(amount) : (u.reading ? 'leyendo…' : 'sin importe')) + '</span>' +
          '<span class="comp-item__who">' + DN.h(u.partner || 'sin cliente') + '</span>' +
          '<small class="comp-item__state">' + DN.h(u.state_label) + '</small>' + note;
        return u.url
          ? '<li><a class="comp-item comp-item--link" href="' + DN.h(u.url) + '" data-dn="down">' + inner + '</a></li>'
          : '<li><div class="comp-item">' + inner + '</div></li>';
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
      var wrong = DN.$$('[data-comp-amt]', thumbs).filter(function (input) { return !checkAmount(input); });
      if (wrong.length) {
        var name = wrong[0].closest('li').querySelector('.comp-thumb__name');
        DN.toast('Revisá el importe' + (name ? ' de «' + name.textContent + '»' : '') + '. ' + AMOUNT_HELP, 'err');
        wrong[0].focus();
        return;
      }
      var body = new FormData();
      body.append('csrf_token', DN.$('[name=csrf_token]', form).value);
      body.append('cliente_id', idInput.value || '');
      if (shared) {
        body.append('compartido', shared.value);
        if (v100) {
          DN.$$('li:not([data-comp-new])', thumbs).forEach(function (li) {
            var amt = li.querySelector('[data-comp-amt]');
            body.append('importe_compartido', amt ? amt.value.trim() : '');
            body.append('giro_compartido', li.getAttribute('data-turn') || '0');
          });
        }
      }
      files.forEach(function (item) {
        body.append('archivos', item.file, item.file.name);
        if (v100) {
          body.append('importes', (item.amount || '').trim());
          body.append('giros', String(item.turn || 0));
        }
      });
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
