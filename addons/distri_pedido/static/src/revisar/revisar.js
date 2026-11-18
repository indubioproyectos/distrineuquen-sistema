/* «Pedidos para revisar» (19.0.1.108.0, views/revisar_app.xml,
 * controllers/revisar.py). Uses the kit (../kit/dn.js, window.DN). Every
 * action goes by fetch and answers JSON with the card rendered again, which
 * replaces the old one: the page never reloads.
 *
 * - .rv-tab[data-ver]: filters the cards in the page (Todos / Rojos /
 *   Amarillos / Retenidos) and keeps ?ver= in the address.
 * - [data-rv-copiar]: copies the WhatsApp code line (Clipboard API, then
 *   the old execCommand; when both fail the line stays selected).
 * - [data-rv-retener]: the «Retener» sheet with the reason.
 * - form[data-rv-liberar]: asks first (DN.confirm), then «Liberar».
 * - [data-rv-pegar]: the «Pegar número» sheet; what is pasted (or typed in
 *   Área / Número) is read by the server as you type.
 * - form[data-rv-asignar], [data-rv-pegar-asignar]: «Asignar cliente» opens
 *   the backend wizard (the URL comes in the answer).
 */
(function () {
  'use strict';

  function start() {
    var body = document.body;
    var main = DN.$('.rv-main');
    if (!main) { return; }
    var ver = main.getAttribute('data-ver') || 'todos';

    // ------------------------------------------------------------ tabs
    function matches(card, v) {
      if (v === 'rojos') { return card.getAttribute('data-color') === 'rojo'; }
      if (v === 'amarillos') { return card.getAttribute('data-color') === 'amarillo'; }
      if (v === 'retenidos') { return card.hasAttribute('data-ret'); }
      return true;
    }
    function paint() {
      var cards = DN.$$('.rv-card', main);
      var counts = {todos: 0, rojos: 0, amarillos: 0, retenidos: 0};
      var shown = 0;
      cards.forEach(function (card) {
        Object.keys(counts).forEach(function (k) { if (matches(card, k)) { counts[k] += 1; } });
        var on = matches(card, ver);
        card.hidden = !on;
        if (on) { shown += 1; }
      });
      Object.keys(counts).forEach(function (k) {
        var el = DN.$('[data-count="' + k + '"]');
        if (el) { el.textContent = counts[k]; }
      });
      DN.$$('.rv-tab').forEach(function (t) {
        var on = t.getAttribute('data-ver') === ver;
        t.classList.toggle('on', on);
        if (on) { t.setAttribute('aria-current', 'true'); } else { t.removeAttribute('aria-current'); }
      });
      var empty = DN.$('[data-rv-empty]');
      if (empty) { empty.hidden = shown > 0; }
    }
    DN.$$('.rv-tab').forEach(function (t) {
      t.addEventListener('click', function (ev) {
        ev.preventDefault();
        ver = t.getAttribute('data-ver');
        main.setAttribute('data-ver', ver);
        history.replaceState(history.state, '', ver === 'todos' ? '/revisar' : '/revisar?ver=' + ver);
        paint();
      });
    });
    paint();

    function swap(card, html) {
      var box = document.createElement('div');
      box.innerHTML = html;
      var fresh = box.firstElementChild;
      if (card && fresh) { card.replaceWith(fresh); }
      paint();
      return fresh;
    }
    function cardOf(id) { return DN.$('#rv-' + id); }

    // ------------------------------------------------------------ copy
    function copyOld(text) {
      var area = document.createElement('textarea');
      area.value = text;
      area.setAttribute('readonly', '');
      area.style.position = 'fixed';
      area.style.opacity = '0';
      document.body.appendChild(area);
      area.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
      document.body.removeChild(area);
      return ok;
    }
    function copied(btn) {
      var label = DN.$('span', btn);
      var use = DN.$('use', btn);
      var old = label.textContent;
      var oldIcon = use && use.getAttribute('href');
      label.textContent = 'Copiado';
      if (use) { use.setAttribute('href', oldIcon.replace(/#.*$/, '#check')); }
      btn.classList.add('rv-btn-ok');
      setTimeout(function () {
        label.textContent = old;
        if (use) { use.setAttribute('href', oldIcon); }
        btn.classList.remove('rv-btn-ok');
      }, 2500);
    }
    function copy(btn) {
      var text = btn.getAttribute('data-rv-copiar');
      var done = function () { copied(btn); DN.toast('Copiado: buscalo en WhatsApp Web.', 'ok'); };
      var fallback = function () {
        if (copyOld(text)) { done(); return; }
        var line = DN.$('.rv-code__txt', btn.closest('.rv-card'));
        if (line) {
          var range = document.createRange();
          range.selectNodeContents(line);
          var sel = window.getSelection();
          sel.removeAllRanges();
          sel.addRange(range);
        }
        DN.toast('No se pudo copiar solo: quedó marcado, copialo a mano.', 'err');
      };
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(text).then(done, fallback);
      } else {
        fallback();
      }
    }

    // ------------------------------------------------------------ retener
    var retEl = DN.$('#rv-retener');
    retEl.hidden = false;
    var retSheet = DN.sheet(retEl);
    var retId = null;
    function openRetener(id) {
      var card = cardOf(id);
      if (!card) { return; }
      retId = id;
      retEl.setAttribute('action', '/revisar/' + id + '/retener');
      DN.$('[data-rv-quien]', retEl).textContent = card.getAttribute('data-name') + ' · ' + card.getAttribute('data-cliente');
      DN.$('textarea', retEl).value = '';
      retSheet.open();
      DN.$('textarea', retEl).focus();
    }
    retEl.addEventListener('submit', function (ev) {
      ev.preventDefault();
      var motivo = DN.$('textarea', retEl).value.trim();
      if (!motivo) { DN.toast('Escribí por qué retenés el pedido.', 'err'); return; }
      var btn = DN.$('button[type=submit]', retEl);
      btn.disabled = true;
      DN.api(retEl.getAttribute('action'), {body: {motivo: motivo}}).then(function (j) {
        retSheet.close();
        swap(cardOf(retId), j.card);
        DN.toast(j.message, 'ok');
      }, DN.fail).then(function () { btn.disabled = false; });
    });
    if (location.search.indexOf('retener=') !== -1) {
      var m = /retener=(\d+)/.exec(location.search);
      history.replaceState(history.state, '', ver === 'todos' ? '/revisar' : '/revisar?ver=' + ver);
      if (m && cardOf(m[1])) { openRetener(m[1]); }
    }

    // ------------------------------------------------------------ pegar número
    var pnEl = DN.$('#rv-pegar');
    pnEl.hidden = false;
    var pnSheet = DN.sheet(pnEl);
    var pn = {id: null, timer: null, seq: 0, generic: false};
    var inPaste = DN.$('[data-rv-pn]', pnEl);
    var inArea = DN.$('[data-rv-area]', pnEl);
    var inNum = DN.$('[data-rv-numero]', pnEl);
    var btnSave = DN.$('[data-rv-guardar]', pnEl);
    var btnAssign = DN.$('[data-rv-pegar-asignar]', pnEl);
    function show(sel, on) { DN.$(sel, pnEl).hidden = !on; }
    function clearPreview() {
      show('[data-rv-error]', false);
      show('[data-rv-read]', false);
      show('[data-rv-aviso]', false);
      show('[data-rv-ya]', false);
    }
    function render(j) {
      var err = DN.$('[data-rv-error]', pnEl);
      err.textContent = j.error || '';
      err.hidden = !j.error;
      DN.$('[data-rv-preview]', pnEl).textContent = j.preview || '';
      show('[data-rv-read]', !!j.preview);
      DN.$('[data-rv-aviso-txt]', pnEl).textContent = j.aviso || '';
      show('[data-rv-aviso]', !!j.aviso);
      var rows = [];
      if (j.del_cliente) {
        rows.push('<div class="rv-ya__row"><span class="rv-ya__txt"><b>' + DN.h(j.cliente) + '</b><span>El cliente del pedido</span></span>'
          + '<span class="dn-chip dn-chip--ok">Ya lo tiene</span></div>');
      }
      (j.clientes || []).forEach(function (c) {
        rows.push('<div class="rv-ya__row"><span class="rv-ya__txt"><b>' + DN.h(c.name) + '</b><span>' + DN.h(c.detail) + '</span></span>'
          + '<span class="dn-chip dn-chip--ok">Mismo número</span></div>');
      });
      DN.$('[data-rv-ya-list]', pnEl).innerHTML = rows.join('');
      show('[data-rv-ya]', rows.length > 0);
      if (j.area !== undefined && inPaste.value.trim()) { inArea.value = j.area; inNum.value = j.numero; }
    }
    function read(fromBoxes) {
      clearTimeout(pn.timer);
      pn.timer = setTimeout(function () {
        var q = new URLSearchParams();
        if (fromBoxes) { q.set('area', inArea.value); q.set('numero', inNum.value); } else { q.set('pegado', inPaste.value); }
        if (!inPaste.value.trim() && !inArea.value.trim() && !inNum.value.trim()) { clearPreview(); return; }
        var seq = ++pn.seq;
        DN.api('/revisar/' + pn.id + '/numero?' + q.toString()).then(function (j) {
          if (seq === pn.seq) { render(j); }
        }, DN.fail);
      }, 250);
    }
    inPaste.addEventListener('input', function () { read(false); });
    [inArea, inNum].forEach(function (el) {
      el.addEventListener('input', function () { inPaste.value = ''; read(true); });
    });
    function openPegar(id) {
      var card = cardOf(id);
      if (!card) { return; }
      pn.id = id;
      pn.generic = card.hasAttribute('data-generic');
      DN.$('[data-rv-quien]', pnEl).textContent = 'Pedido ' + card.getAttribute('data-name') + ' · ' + card.getAttribute('data-cliente');
      inPaste.value = inArea.value = inNum.value = '';
      clearPreview();
      btnAssign.hidden = !pn.generic;
      pnSheet.open();
      inPaste.focus();
    }
    function values() {
      return {pegado: inPaste.value, area: inArea.value, numero: inNum.value};
    }
    btnSave.addEventListener('click', function () {
      if (!inPaste.value.trim() && !(inArea.value.trim() || inNum.value.trim())) {
        DN.toast('Pegá el número que copiaste del chat.', 'err');
        return;
      }
      btnSave.disabled = true;
      DN.api('/revisar/' + pn.id + '/numero', {body: values()}).then(function (j) {
        pnSheet.close();
        swap(cardOf(pn.id), j.card);
        DN.toast(j.message, 'ok');
      }, DN.fail).then(function () { btnSave.disabled = false; });
    });
    function assign(id, pegado, btn) {
      if (btn) { btn.disabled = true; }
      DN.api('/revisar/' + id + '/asignar', {body: {pegado: pegado || ''}}).then(function (j) {
        location.href = j.url;
      }, function (e) { if (btn) { btn.disabled = false; } DN.fail(e); });
    }
    btnAssign.addEventListener('click', function () {
      var pasted = inPaste.value.trim() || (inArea.value + ' ' + inNum.value).trim();
      assign(pn.id, pasted, btnAssign);
    });

    // ------------------------------------------------------------ clicks on cards
    main.addEventListener('click', function (ev) {
      var t = ev.target.closest('[data-rv-copiar],[data-rv-retener],[data-rv-pegar]');
      if (!t) { return; }
      ev.preventDefault();
      if (t.hasAttribute('data-rv-copiar')) { copy(t); }
      else if (t.hasAttribute('data-rv-retener')) { openRetener(t.getAttribute('data-rv-retener')); }
      else { openPegar(t.getAttribute('data-rv-pegar')); }
    });
    main.addEventListener('submit', function (ev) {
      var form = ev.target;
      var card = form.closest('.rv-card');
      if (!card) { return; }
      ev.preventDefault();
      var id = card.getAttribute('data-id');
      if (form.hasAttribute('data-rv-asignar')) { assign(id, '', DN.$('button', form)); return; }
      if (!form.hasAttribute('data-rv-liberar')) { return; }
      DN.confirm(body.getAttribute('data-liberar'), 'Liberar').then(function (yes) {
        if (!yes) { return; }
        card.classList.add('rv-busy');
        DN.api(form.getAttribute('action'), {body: {}}).then(function (j) {
          swap(card, j.card);
          DN.toast(j.message, 'ok');
        }, function (e) { card.classList.remove('rv-busy'); DN.fail(e); });
      });
    });
  }

  if (window.DN && DN.ready) { start(); } else { document.addEventListener('dn:ready', start); }
})();
