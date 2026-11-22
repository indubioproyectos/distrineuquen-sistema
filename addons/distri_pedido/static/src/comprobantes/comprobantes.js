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
 * - 19.0.1.101.0, only on a data-comp-api="101" page: the answers also carry
 *   «attempts» (files refused because they were already loaded); they are
 *   drawn among the uploads, newest first («ts»), marked «No se cargó · ya
 *   estaba en el sistema», and open the receipt that was already there. The
 *   count stays the one of «uploads».
 * - 19.0.1.126.1, only on a data-comp-api="126" page:
 *   · the photo to upload is large (contain, never cropped), ↺ ↻ overlaid,
 *     a tap opens it full screen (<dialog>, pinch-zoom; click-to-zoom on a
 *     desktop);
 *   · each chosen file is read by /comprobantes/sugerir (nothing stored):
 *     the amount read fills an empty «Importe», and up to 3 «Sugerido:
 *     cliente (por CUIT / por pedido / por nombre)» chips appear. A chip
 *     only fills the customer when tapped: nothing is chosen by itself;
 *   · the customer is mandatory: the button stays disabled until one is
 *     chosen or «No está en el sistema» is checked (then «¿Quién lo
 *     mandó?» is required);
 *   · «Subir y acreditar» (data-comp-credit) credits in the same request;
 *     the answer per file is shown in the results, a warning with
 *     «Acreditar igual» (/comprobantes/<id>/acreditar, nothing uploaded
 *     again);
 *   · the rows show who sent it (no customer) and «Cancelar o cambiar la
 *     foto».
 * - 19.0.1.126.2: a result «completar» (the user's own receipt pending
 *   assignment uploaded again: nothing created) shows that receipt (photo,
 *   amount), the suggested customers and «Completar y acreditar», which
 *   opens /comprobantes/<id>/completar (with the customer chosen now, or the
 *   tapped suggestion); a row with «complete_url» links it too. On «Ver
 *   comprobante» the «Completar» form uses the customer search of
 *   «Acreditar» and its suggestion chips fill the customer.
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
    if (img) {
      var scale = 1;
      if (degrees % 180 && img.clientWidth && img.clientHeight) {
        // a quarter turn keeps the photo inside its box
        scale = Math.min(img.clientWidth, img.clientHeight) / Math.max(img.clientWidth, img.clientHeight);
      }
      img.style.transform = degrees ? 'rotate(' + degrees + 'deg)' + (scale < 1 ? ' scale(' + scale.toFixed(3) + ')' : '') : '';
    }
    li.setAttribute('data-turn', String(degrees));
  }

  // 19.0.1.126.1: the photo full screen, pinch-zoom on a phone, click-to-zoom on a desktop
  var zoomDialog = null;
  function openZoom(src, degrees) {
    if (!zoomDialog) {
      zoomDialog = document.createElement('dialog');
      zoomDialog.className = 'comp-zoom';
      zoomDialog.innerHTML = '<button type="button" class="comp-zoom__x" aria-label="Cerrar">×</button>' +
        '<div class="comp-zoom__wrap"><img alt="Comprobante en pantalla completa"></div>';
      document.body.appendChild(zoomDialog);
      zoomDialog.addEventListener('click', function (ev) {
        if (ev.target.closest('.comp-zoom__x')) { zoomDialog.close(); return; }
        if (ev.target.tagName === 'IMG' && window.matchMedia && window.matchMedia('(hover: hover)').matches) {
          zoomDialog.classList.toggle('comp-zoom--big');
        }
      });
    }
    var img = zoomDialog.querySelector('img');
    img.src = src;
    img.style.transform = degrees ? 'rotate(' + degrees + 'deg)' : '';
    zoomDialog.classList.remove('comp-zoom--big');
    if (zoomDialog.showModal) { zoomDialog.showModal(); } else { window.open(src, '_blank'); }
  }

  function init() {
    var form = DN.$('form[data-comp-form]');
    if (!form) { return; }
    var api = Number(form.getAttribute('data-comp-api') || 0);
    var v100 = api >= 100;
    var v101 = api >= 101;
    var v126 = api >= 126;
    var mayCredit = form.hasAttribute('data-comp-credit');
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
        if (v126) {
          li.innerHTML = '<div class="comp-thumb__media">' +
            (pdf ? '<span class="comp-thumb__pdf">PDF</span>' : '<img alt="Comprobante elegido" data-comp-zoom="">' +
              '<div class="comp-thumb__tools"><button type="button" class="comp-thumb__tool" data-comp-rotl="" aria-label="Girar a la izquierda">↺</button>' +
              '<button type="button" class="comp-thumb__tool" data-comp-rot="" aria-label="Girar a la derecha">↻</button></div>') +
            '</div><span class="comp-thumb__name">' + DN.h(file.name) + '</span>' + amountField(item.amount);
        } else {
          li.innerHTML = (pdf ? '<span class="comp-thumb__pdf">PDF</span>' : '<img alt="Comprobante elegido">') +
            '<span class="comp-thumb__name">' + DN.h(file.name) + '</span>' +
            (v100 && !pdf ? '<button type="button" class="dn-btn dn-btn--outline comp-thumb__turn" data-comp-rot="" aria-label="Girar la foto">↻ Girar</button>' : '') +
            (v100 ? amountField(item.amount) : '');
        }
        li.innerHTML +=
          '<button type="button" class="comp-thumb__x" aria-label="Sacar este archivo" data-comp-x="' + n + '">×</button>';
        if (!pdf) {
          // kept for the zoom while the file is in the list
          item.url = item.url || URL.createObjectURL(file);
          var img = li.querySelector('img');
          img.onload = function () { turnPreview(li, item.turn); };
          img.src = item.url;
          turnPreview(li, item.turn);
        }
        thumbs.appendChild(li);
      });
      sel.hidden = !files.length && !sharedCount;
      updateSend();
    }

    // 19.0.1.126.1: the button says what it does and waits for the customer
    var noesta = DN.$('[data-comp-noesta]', form);
    var noestaBox = DN.$('[data-comp-noesta-box]', form);
    var remitente = DN.$('[data-comp-remitente]', form);
    function updateSend() {
      var n = files.length + sharedCount;
      var label = 'Subir';
      if (v126 && mayCredit && !(noesta && noesta.checked)) { label = 'Subir y acreditar'; }
      sendBtn.textContent = n > 1 ? label + ' ' + n : label;
      if (v126) {
        var chosen = (idInput && idInput.value) || (noesta && noesta.checked);
        sendBtn.disabled = !chosen;
        sendBtn.setAttribute('aria-disabled', chosen ? 'false' : 'true');
      }
    }

    DN.$$('[data-comp-file]', form).forEach(function (input) {
      input.addEventListener('change', function () {
        Array.prototype.forEach.call(input.files || [], function (f) {
          if (files.length + sharedCount < MAX) {
            var item = {file: f, amount: '', turn: 0};
            files.push(item);
            if (v126) { suggestFor(item); }
          } else { DN.toast('Hasta ' + MAX + ' archivos por vez.', 'err'); }
        });
        input.value = '';
        paint();
      });
    });
    thumbs.addEventListener('click', function (ev) {
      var x = ev.target.closest('[data-comp-x]');
      if (x) {
        var gone = files.splice(parseInt(x.getAttribute('data-comp-x'), 10), 1)[0];
        if (gone && gone.url) { URL.revokeObjectURL(gone.url); }
        paint();
        return;
      }
      var zoom = ev.target.closest('[data-comp-zoom]');
      if (zoom) {
        openZoom(zoom.src, parseInt(zoom.closest('li').getAttribute('data-turn') || '0', 10));
        return;
      }
      var rot = ev.target.closest('[data-comp-rot], [data-comp-rotl]');
      if (!rot) { return; }
      var li = rot.closest('li');
      var step = rot.hasAttribute('data-comp-rotl') ? 270 : 90;
      var degrees = (parseInt(li.getAttribute('data-turn') || '0', 10) + step) % 360;
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
        if (noesta && noesta.checked) { noesta.checked = false; showNoesta(); }
      } else {
        txt.innerHTML = defaultTxt;
      }
      paintSug();
      updateSend();
    }

    // ---- 19.0.1.126.1: suggested customers and the amount read
    var sugBox = DN.$('[data-comp-sug]', form);
    var sugs = [];  // {id, name, reason}, best first, up to 3
    var reading = 0;
    function paintSug() {
      if (!sugBox) { return; }
      var chosen = idInput.value;
      var html = sugs.map(function (sg) {
        return '<button type="button" class="comp-sug__chip" data-comp-sug-id="' + sg.id + '" data-name="' + DN.h(sg.name) + '"' +
          ' aria-pressed="' + (String(sg.id) === chosen ? 'true' : 'false') + '">Sugerido: ' + DN.h(sg.name) +
          '<small>' + DN.h(sg.reason) + '</small></button>';
      }).join('');
      if (reading) { html += '<span class="comp-sug__hint">Leyendo el comprobante…</span>'; }
      else if (sugs.length && !chosen) { html += '<span class="comp-sug__hint">Tocá la sugerencia si es ese cliente, o buscalo.</span>'; }
      sugBox.innerHTML = html;
      sugBox.hidden = !html;
    }
    function takePreview(r, amtInput, item) {
      (r.suggestions || []).forEach(function (sg) {
        if (sugs.length < 3 && !sugs.some(function (o) { return o.id === sg.id; })) { sugs.push(sg); }
      });
      if (r.amount_text && amtInput && !amtInput.value.trim()) {
        amtInput.value = r.amount_text;
        if (item) { item.amount = r.amount_text; }
        checkAmount(amtInput);
      } else if (r.notice && amtInput && !amtInput.value.trim()) {
        // 19.0.1.128.0: the reading gave up at the time cap: typed by hand
        amtInput.placeholder = 'Escribí el importe';
        amtInput.title = r.notice;
      }
    }
    function suggestFor(item) {
      var body = new FormData();
      body.append('csrf_token', DN.$('[name=csrf_token]', form).value);
      body.append('archivo', item.file, item.file.name);
      reading++; paintSug();
      DN.api('/comprobantes/sugerir', {body: body, timeout: 40000}).then(function (r) {
        reading--;
        var n = files.indexOf(item);
        var li = n >= 0 ? DN.$('li[data-comp-new="' + n + '"]', thumbs) : null;
        if (n >= 0) { takePreview(r, li && li.querySelector('[data-comp-amt]'), item); }
        paintSug();
      }, function () { reading--; paintSug(); });
    }
    function suggestShared(li) {
      var body = new FormData();
      body.append('csrf_token', DN.$('[name=csrf_token]', form).value);
      body.append('compartido', shared.value);
      body.append('indice', li.getAttribute('data-comp-index') || '0');
      reading++; paintSug();
      DN.api('/comprobantes/sugerir', {body: body, timeout: 40000}).then(function (r) {
        reading--;
        takePreview(r, li.querySelector('[data-comp-amt]'), null);
        paintSug();
      }, function () { reading--; paintSug(); });
    }
    if (sugBox) {
      sugBox.addEventListener('click', function (ev) {
        var chip = ev.target.closest('[data-comp-sug-id]');
        if (chip) { choose(chip.getAttribute('data-comp-sug-id'), chip.getAttribute('data-name')); }
      });
    }
    function showNoesta() {
      if (!noestaBox) { return; }
      noestaBox.hidden = !noesta.checked;
      if (noesta.checked) {
        idInput.value = ''; q.value = ''; txt.innerHTML = defaultTxt; res.hidden = true;
        paintSug();
        remitente.focus();
      }
      updateSend();
    }
    if (noesta) { noesta.addEventListener('change', showNoesta); noestaBox.hidden = !noesta.checked; }

    q.addEventListener('input', function () {
      idInput.value = '';
      txt.innerHTML = defaultTxt;
      paintSug();
      updateSend();
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
    function attemptRow(u) {
      return '<li><a class="comp-item comp-item--link comp-item--attempt" href="' + DN.h(u.url) + '" data-dn="down">' +
        '<span class="comp-item__st comp-item__st--attempt" aria-hidden="true">⊘</span>' +
        '<span class="comp-item__amt dn-num">' + (u.amount ? money(u.amount) : 'sin importe') + '</span>' +
        '<span class="comp-item__who">' + DN.h(u.partner || 'sin cliente') + '</span>' +
        '<small class="comp-item__state comp-item__flag">' + DN.h(u.flag) + '</small>' +
        '<small class="comp-item__note">Llegó el ' + DN.h(u.existing_when) + ' · ' + DN.h(u.existing_channel) +
        ' · ' + DN.h(u.bucket_label) + ' · intentado ' + DN.h(u.date) + '</small></a></li>';
    }
    function rowExtras(u) {
      return u.sender_note ? '<small class="comp-item__note">Lo mandó: ' + DN.h(u.sender_note) + '</small>' : '';
    }
    function manageLink(u) {
      var complete = u.complete_url ? '<a class="dn-link comp-item__manage" href="' + DN.h(u.complete_url) + '" data-dn="down">Completar y acreditar</a>' : '';
      return complete + (u.can_manage && u.url ? '<a class="dn-link comp-item__manage" href="' + DN.h(u.url) + '#gestionar" data-dn="down">Cancelar o cambiar la foto</a>' : '');
    }
    function paintUploads(rows, attempts) {
      count.textContent = rows.length;
      var all = rows;
      if (v101 && attempts && attempts.length) {
        all = rows.concat(attempts).sort(function (a, b) {
          return (a.ts || '') < (b.ts || '') ? 1 : ((a.ts || '') > (b.ts || '') ? -1 : 0);
        });
      }
      if (empty) { empty.hidden = !!all.length; }
      list.innerHTML = all.map(function (u) {
        if (u.attempt) { return attemptRow(u); }
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
          '<small class="comp-item__state">' + DN.h(u.state_label) + '</small>' + note + rowExtras(u);
        return u.url
          ? '<li><a class="comp-item comp-item--link" href="' + DN.h(u.url) + '" data-dn="down">' + inner + '</a>' + manageLink(u) + '</li>'
          : '<li><div class="comp-item">' + inner + '</div></li>';
      }).join('');
      var reading = rows.some(function (u) { return u.reading; });
      clearTimeout(poll);
      if (reading) {
        poll = setTimeout(function () {
          DN.api('/comprobantes/hoy').then(function (r) { if (r && r.uploads) { paintUploads(r.uploads, r.attempts); } }, function () {});
        }, 5000);
      }
    }
    if (DN.$('.comp-item__st--reading', list)) { paintUploads([]); DN.api('/comprobantes/hoy').then(function (r) { paintUploads(r.uploads || [], r.attempts); }, function () {}); }

    // ---- send
    form.addEventListener('submit', function (ev) {
      ev.preventDefault();
      if (!files.length && !sharedCount) {
        DN.toast('Primero sacá una foto o elegí el comprobante.', 'err');
        return;
      }
      if (v126 && !idInput.value && !(noesta && noesta.checked)) {
        DN.toast('Elegí el cliente, o marcá «No está en el sistema».', 'err');
        q.focus();
        return;
      }
      if (v126 && noesta && noesta.checked && !remitente.value.trim()) {
        DN.toast('Escribí quién mandó el comprobante (nombre, negocio o teléfono).', 'err');
        remitente.focus();
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
      if (v126) {
        if (noesta && noesta.checked) {
          body.append('no_en_sistema', '1');
          body.append('remitente', remitente.value.trim());
        }
        if (mayCredit) { body.append('acreditar', '1'); }
      }
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
        var own = results.filter(function (x) { return x.status === 'completar'; }).length;
        var parts = [];
        if (fresh) { parts.push(fresh === 1 ? '1 comprobante subido' : fresh + ' comprobantes subidos'); }
        if (own) { parts.push(own === 1 ? 'Ya lo habías subido y quedó sin acreditar: completalo abajo' : own + ' ya los habías subido y quedaron sin acreditar: completalos abajo'); }
        if (repeated.length) { parts.push(repeated.length === 1 ? '1 ya estaba cargado: ' + repeated[0].message.replace(/^Ya estaba cargado:?\s*/, '') : repeated.length + ' ya estaban cargados'); }
        if (repeated.length && v101) { parts.push('No se cargó de nuevo: queda en la lista para que lo revises'); }
        if (bad.length) { parts.push(bad.map(function (x) { return x.name + ': ' + x.message; }).join(' · ')); }
        if (v126) { paintResults(results); }
        DN.toast(parts.join('. ') + '.', bad.length || (repeated.length && !fresh && !own) ? 'err' : 'ok');
        files.forEach(function (item) { if (item.url) { URL.revokeObjectURL(item.url); } });
        files = [];
        sugs = [];
        if (noesta) { noesta.checked = false; remitente.value = ''; noestaBox.hidden = true; }
        if (shared) {
          shared.remove(); shared = null; sharedCount = 0;
          DN.$$('li', thumbs).forEach(function (li) { li.remove(); });
          if (history.replaceState) { history.replaceState(history.state, '', '/comprobantes'); }
        }
        q.value = ''; choose('', '');
        paint();
        if (r && r.uploads) { paintUploads(r.uploads, r.attempts); }
      }, function (e) {
        sendBtn.disabled = false;
        paint();
        DN.fail(e);
      });
    });

    // ---- 19.0.1.126.2: the own receipt pending assignment, to complete
    function completeCard(r) {
      var link = function (id) { return DN.h(r.url + (id ? '?cliente_id=' + encodeURIComponent(id) : '')); };
      var photo = r.image && r.file_url
        ? '<a class="comp-ver__photo" href="' + DN.h(r.file_url) + '" target="_blank" rel="noopener"><img src="' + DN.h(r.file_url) + '" alt="Comprobante que ya subiste"></a>'
        : (r.file_url ? '<a class="dn-link" href="' + DN.h(r.file_url) + '" target="_blank" rel="noopener">Abrir el PDF</a>' : '');
      var chips = (r.suggestions || []).map(function (sg) {
        return '<a class="comp-sug__chip" href="' + link(sg.id) + '" data-dn="down">Sugerido: ' + DN.h(sg.name) + '<small>' + DN.h(sg.reason) + '</small></a>';
      }).join('');
      return '<div class="comp-r__complete">' + photo +
        '<p class="comp-muted">Importe: <b class="dn-num">' + (r.amount ? money(r.amount) : 'sin importe leído') + '</b>' +
        (r.sender_note ? ' · Lo mandó: ' + DN.h(r.sender_note) : '') + '</p>' +
        (chips ? '<div class="comp-sug">' + chips + '</div>' : '') +
        '<a class="dn-btn" href="' + link(r.partner_id) + '" data-dn="down">Completar y acreditar' +
        (r.partner_name ? ' a ' + DN.h(r.partner_name) : '') + '</a></div>';
    }

    // ---- 19.0.1.126.1: the answer per file, «Acreditar igual» without uploading again
    var resultsBox = DN.$('[data-comp-results]');
    function paintResults(results) {
      if (!resultsBox) { return; }
      resultsBox.innerHTML = results.map(function (r) {
        var confirm = '';
        if (r.credit === 'aviso') {
          confirm = '<div class="comp-r__confirm"><ul>' + (r.warnings || []).map(function (w) { return '<li>' + DN.h(w) + '</li>'; }).join('') +
            '</ul><button type="button" class="dn-btn" data-comp-confirm="' + r.id + '" data-importe="' + DN.h(r.amount_text || '') +
            '" data-cliente="' + DN.h(String(r.partner_id || '')) + '">Acreditar igual</button></div>';
        }
        if (r.status === 'completar') { confirm = completeCard(r); }
        return '<li class="comp-r comp-r--' + DN.h(r.status) + ' comp-r--c-' + DN.h(r.credit || 'none') + '"><b>' + DN.h(r.name) + '</b> ' +
          '<span data-comp-msg="">' + DN.h(r.message) + '</span>' + confirm + '</li>';
      }).join('');
      resultsBox.hidden = !results.length;
    }
    if (resultsBox) {
      resultsBox.addEventListener('click', function (ev) {
        var btn = ev.target.closest('[data-comp-confirm]');
        if (!btn) { return; }
        ev.preventDefault();
        btn.disabled = true;
        DN.api('/comprobantes/' + btn.getAttribute('data-comp-confirm') + '/acreditar', {body: {
          csrf_token: DN.$('[name=csrf_token]', form).value, importe: btn.getAttribute('data-importe'),
          cliente_id: btn.getAttribute('data-cliente'), confirmado: '1'}}).then(function (r) {
          var li = btn.closest('li');
          li.querySelector('[data-comp-msg]').textContent = r.message || '';
          li.className = 'comp-r comp-r--nuevo comp-r--c-' + (r.credited ? 'acreditado' : 'pendiente');
          btn.closest('.comp-r__confirm').remove();
          DN.toast(r.message || 'Listo.', r.credited ? 'ok' : 'err');
          if (r.uploads) { paintUploads(r.uploads, r.attempts); }
        }, function (e) { btn.disabled = false; DN.fail(e); });
      });
    }
    if (v126 && shared) { DN.$$('li[data-comp-index]', thumbs).forEach(suggestShared); }
    paint();
  }
  // 19.0.1.126.0: «Acreditar» on «Ver comprobante» (form.comp-credit): the
  // customer search of the upload form; without JavaScript the customer
  // proposed for the receipt is kept.
  function initCredit() {
    var form = DN.$('form.comp-credit');
    if (!form) { return; }
    var q = DN.$('[data-comp-q]', form);
    var res = DN.$('[data-comp-res]', form);
    var idInput = DN.$('[data-comp-cliente-id]', form);
    var txt = DN.$('[data-comp-cliente-txt]', form);
    var initialId = idInput.value, initialTxt = txt.textContent;
    var timer = null, seq = 0;
    q.addEventListener('input', function () {
      clearTimeout(timer);
      var text = q.value.trim();
      if (text.length < 2) { res.hidden = true; idInput.value = initialId; txt.textContent = initialTxt; return; }
      idInput.value = '';
      txt.textContent = 'Elegí el cliente de la lista.';
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
      // 19.0.1.126.2: a suggestion chip of «Completar» fills the customer
      var pick = ev.target.closest('[data-comp-pick]') || ev.target.closest('[data-comp-sug-id]');
      if (!pick) { return; }
      if (pick.hasAttribute('data-comp-sug-id')) {
        ev.preventDefault();
        initialId = pick.getAttribute('data-comp-sug-id');
        initialTxt = 'Se acredita a ' + pick.getAttribute('data-name') + '.';
        DN.$$('[data-comp-sug-id]', form).forEach(function (b) { b.setAttribute('aria-pressed', b === pick ? 'true' : 'false'); });
        idInput.value = initialId;
        q.value = '';
        txt.textContent = initialTxt;
        res.hidden = true;
        return;
      }
      idInput.value = pick.getAttribute('data-comp-pick');
      q.value = pick.getAttribute('data-name');
      txt.textContent = 'Se acredita a ' + pick.getAttribute('data-name') + '.';
      res.hidden = true;
    });
  }
  function start() { init(); initCredit(); }
  if (window.DN && DN.ready) { start(); } else { document.addEventListener('dn:ready', start); }
})();
