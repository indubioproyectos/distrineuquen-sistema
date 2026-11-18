/* «Caja del día» (19.0.1.97.0): the cashier's pages (views/caja.xml).
 * Uses the kit (../kit/dn.js, window.DN). Without JavaScript every form
 * posts and the server answers with the page or a redirect.
 *
 * - form[data-caja-form]: sent with DN.api (fetch). {url} goes UP to the
 *   parent with its message (DN.up), or replaces the page when the answer is
 *   this same page; {ask, question} asks with DN.confirm and sends again
 *   with that field set to 1; an error stays on screen as a toast.
 * - [data-caja-billetes]: the bill grid; - / + and typed quantities keep the
 *   row totals and the counted total live.
 * - form[data-caja-cierre]: counted, difference and «se retira» live.
 * - form[data-caja-recibir]: what the driver handed vs what he has to hand,
 *   the expenses without paper said aloud BEFORE sending (audit F10), and
 *   the «Contar» sheet that fills «Entregó».
 * - [data-caja-hoja="<id>"] (19.0.1.105.0): opens that sheet («Reabrir mi
 *   caja»); a page opened with #reabrir opens it right away.
 */
(function () {
  'use strict';

  function amount(text) {
    var raw = String(text || '').replace(/\$/g, '').replace(/\s/g, '');
    if (!raw) { return 0; }
    if (raw.indexOf(',') !== -1) { raw = raw.replace(/\./g, '').replace(',', '.'); }
    else if (/^\d{1,3}(\.\d{3})+$/.test(raw)) { raw = raw.replace(/\./g, ''); }
    var n = parseFloat(raw);
    return isFinite(n) ? n : 0;
  }
  function money(n) {
    var neg = n < 0;
    var t = Math.abs(n).toLocaleString('es-AR', {minimumFractionDigits: 0, maximumFractionDigits: 2});
    return (neg ? '− ' : '') + '$ ' + t;
  }
  function qty(input) {
    var n = parseInt(String(input.value || '').replace(/\D/g, ''), 10);
    return isFinite(n) ? n : 0;
  }

  // ------------------------------------------------------------ bill grid
  function grid(box, onChange) {
    function total() {
      var sum = 0;
      DN.$$('input[data-valor]', box).forEach(function (input) {
        var sub = qty(input) * parseInt(input.getAttribute('data-valor'), 10);
        sum += sub;
        var row = input.closest('.caja-bill__row');
        var cell = row && DN.$('[data-caja-sub]', row);
        if (cell) { cell.textContent = money(sub).replace('$ ', ''); }  // the column says TOTAL
      });
      var other = DN.$('[data-caja-otros]', box);
      return sum + (other ? amount(other.value) : 0);
    }
    box.addEventListener('click', function (ev) {
      var b = ev.target.closest('[data-caja-step]');
      if (!b) { return; }
      var input = DN.$('input', b.parentNode);
      var n = Math.max(0, qty(input) + parseInt(b.getAttribute('data-caja-step'), 10));
      input.value = n ? String(n) : '';
      onChange(total());
    });
    box.addEventListener('input', function () { onChange(total()); });
    onChange(total());
    return {total: total};
  }

  // ------------------------------------------------------------ closing
  function closing(form) {
    var expected = parseFloat(form.getAttribute('data-esperado')) || 0;
    var counted = 0;
    var left = DN.$('[name=queda]', form);
    function paint() {
      var diff = Math.round((counted - expected) * 100) / 100;
      DN.$$('[data-caja-contado]', form).forEach(function (el) { el.textContent = money(counted); });
      var d = DN.$('[data-caja-diferencia]', form);
      d.textContent = diff === 0 ? '$ 0' : (diff < 0 ? 'Faltan ' : 'Sobran ') + money(Math.abs(diff));
      d.className = 'dn-num ' + (diff === 0 ? 'ok' : (diff < 0 ? 'falta' : 'sobra'));
      DN.$('[data-caja-retira]', form).textContent = money(counted - amount(left.value));
      var reason = DN.$('#motivo', form);
      reason.required = diff !== 0;
    }
    grid(DN.$('[data-caja-billetes]', form), function (t) { counted = t; paint(); });
    left.addEventListener('input', paint);
    if (form.hasAttribute('data-caja-enter')) { enterFlow(form); }
  }

  // 19.0.1.109.0 (form[data-caja-enter]): «número y Enter pasa al siguiente
  // billete». Enter in a field never sends the closing: it moves to the next
  // bill, then «Monedas / otros», «¿Qué queda para mañana?» and the reason.
  // A field takes the whole number when it gets the focus.
  function enterFlow(form) {
    var fields = DN.$$('[data-caja-billetes] input, [name=queda], [name=motivo]', form);
    form.addEventListener('keydown', function (ev) {
      var i = fields.indexOf(ev.target);
      if (ev.key !== 'Enter' || i === -1 || ev.target.tagName === 'TEXTAREA') { return; }
      ev.preventDefault();
      var next = fields[i + 1];
      if (next) { next.focus(); }
    });
    form.addEventListener('focusin', function (ev) {
      if (fields.indexOf(ev.target) !== -1 && ev.target.tagName === 'INPUT') {
        setTimeout(function () { try { ev.target.select(); } catch (e) { /* not selectable */ } }, 0);
      }
    });
  }

  // ------------------------------------------------------------ receiving
  function receiving(form) {
    var expected = parseFloat(form.getAttribute('data-esperado')) || 0;
    var driver = form.getAttribute('data-repartidor') || 'el repartidor';
    var handed = DN.$('[name=entrego]', form);
    var out = DN.$('[data-caja-dif]', form);
    var modes = DN.$('[data-caja-modo]', form);
    var warn = DN.$('[data-caja-sinpapel]', form);
    function paint() {
      if (!handed.value.trim()) { out.textContent = ''; modes.hidden = true; return; }
      var diff = Math.round((amount(handed.value) - expected) * 100) / 100;
      out.className = 'caja-dif ' + (diff === 0 ? 'ok' : (diff < 0 ? 'falta' : 'sobra'));
      out.textContent = diff === 0 ? 'Entregó justo.' : (diff < 0 ? 'Faltan ' : 'Sobran ') + money(Math.abs(diff));
      modes.hidden = diff === 0;
    }
    function papers() {
      var missing = DN.$$('[data-caja-papel]', form).filter(function (c) { return !c.checked; });
      if (!missing.length) { warn.hidden = true; return; }
      var sum = missing.reduce(function (s, c) { return s + (parseFloat(c.getAttribute('data-monto')) || 0); }, 0);
      warn.textContent = (missing.length === 1 ? '1 gasto sin el papel' : missing.length + ' gastos sin el papel')
        + ' (' + money(sum) + '): si recibís así, ' + (missing.length === 1 ? 'se rechaza' : 'se rechazan')
        + ' y queda a cargo de ' + driver + '.';
      warn.hidden = false;
    }
    handed.addEventListener('input', paint);
    form.addEventListener('change', function (ev) { if (ev.target.hasAttribute('data-caja-papel')) { papers(); } });
    paint();
    papers();

    var sheetEl = DN.$('#caja-contar');
    var openBtn = DN.$('[data-caja-contar]', form);
    if (sheetEl && openBtn) {
      var sheet = DN.sheet(sheetEl);
      var counted = 0;
      var total = DN.$('[data-caja-contado]', sheetEl);
      grid(DN.$('[data-caja-billetes]', sheetEl), function (t) { counted = t; total.textContent = money(t); });
      openBtn.addEventListener('click', function () { sheet.open(); });
      DN.$('[data-caja-usar]', sheetEl).addEventListener('click', function () {
        handed.value = String(Math.round(counted * 100) / 100).replace('.', ',');
        sheet.close();
        paint();
      });
    }
  }

  // ------------------------------------------------------------ opening
  function opening(form) {
    var input = DN.$('[name=fondo]', form);
    var box = DN.$('[data-caja-motivo]', form);
    var proposed = amount(input.defaultValue);
    if (!box) { return; }
    input.addEventListener('input', function () {
      box.hidden = !box.querySelector('input').value && amount(input.value) === proposed;
    });
  }

  // ------------------------------------------------------------ sending
  // 19.0.1.109.0 (form[data-caja-imprimir], on the depot PC): «Cerrar e
  // imprimir» opens the closing label by itself. The tab is opened right on
  // the click (a tab opened after the answer is blocked as a popup) and gets
  // the label once the closing went fine; otherwise it closes again.
  function printTab(form) {
    if (!form.hasAttribute('data-caja-imprimir') || !window.matchMedia
        || !window.matchMedia('(min-width: 1024px)').matches) { return null; }
    var tab = window.open('', '_blank');
    if (tab) {
      try { tab.document.title = 'Etiqueta de cierre'; tab.document.body.textContent = 'Cerrando la caja…'; } catch (e) { /* not ready */ }
    }
    return tab;
  }

  function send(form, ev) {
    if (ev) { ev.preventDefault(); }
    var tab = printTab(form);
    var buttons = DN.$$('button[type=submit]', form).concat(
      form.id ? DN.$$('button[form="' + form.id + '"]') : []);
    buttons.forEach(function (b) { b.disabled = true; });
    var done = function () {
      buttons.forEach(function (b) { b.disabled = false; });
      if (tab) { tab.close(); tab = null; }
    };
    DN.api(form.action, {body: new FormData(form), timeout: 60000}).then(function (r) {
      var closed = r && r.url && /[?&]cerrada=(\d+)/.exec(r.url);
      if (tab && closed) { tab.location.href = '/caja/' + closed[1] + '/etiqueta'; tab = null; }
      if (r && r.ask) {
        done();
        return DN.confirm(r.question, 'Sí, recibir así').then(function (yes) {
          if (!yes) { return; }
          var field = form.querySelector('[name="' + r.ask + '"]');
          if (field) { field.value = '1'; send(form); }
        });
      }
      if (r && r.url) {
        var a = document.createElement('a');
        var up = document.createElement('a');
        a.href = r.url;
        up.href = document.body.getAttribute('data-parent') || '/';
        // the parent: back there in the history (DN.up). This page, or a
        // level further up (a settlement received lands on /caja): replace
        // it; that page's own guard sends the phone back to its parent.
        if (a.pathname === up.pathname && a.pathname !== location.pathname) { DN.up(r.url); }
        else { location.replace(r.url); }
        return;
      }
      done();
    }, function (e) {
      done();
      DN.fail(e);
      var err = DN.$('#caja-error');
      if (err) { err.hidden = true; }
      if (/diferencia/i.test(e.message)) {
        var modes = DN.$('[data-caja-modo]', form);
        if (modes) { modes.hidden = false; }
        var reason = DN.$('#motivo', form) || DN.$('[name=motivo]', form);
        if (reason) {
          var wrap = reason.closest('[data-caja-motivo]');
          if (wrap) { wrap.hidden = false; }
          reason.focus();
        }
      }
    });
  }

  // ------------------------------------------------------------ sheets
  function sheets() {
    var first = null;
    DN.$$('[data-caja-hoja]').forEach(function (b) {
      var el = document.getElementById(b.getAttribute('data-caja-hoja'));
      if (!el) { return; }
      var sheet = el.dnSheet || DN.sheet(el);
      var open = function () {
        sheet.open();
        var field = el.querySelector('textarea,input:not([type=hidden])');
        if (field) { field.focus(); }
      };
      first = first || open;
      b.addEventListener('click', function (ev) { ev.preventDefault(); open(); });
    });
    if (first && location.hash === '#reabrir') {
      history.replaceState(history.state, '', location.pathname + location.search);
      first();
    }
  }

  function init() {
    sheets();
    DN.$$('form[data-caja-form]').forEach(function (form) {
      form.addEventListener('submit', function (ev) { send(form, ev); });
      if (form.hasAttribute('data-caja-cierre')) { closing(form); }
      if (form.hasAttribute('data-caja-recibir')) { receiving(form); }
      if (form.action.indexOf('/caja/abrir') !== -1) { opening(form); }
    });
  }
  if (window.DN && DN.ready) { init(); } else { document.addEventListener('dn:ready', init); }
})();
