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
  function send(form, ev) {
    if (ev) { ev.preventDefault(); }
    var buttons = DN.$$('button[type=submit]', form).concat(
      form.id ? DN.$$('button[form="' + form.id + '"]') : []);
    buttons.forEach(function (b) { b.disabled = true; });
    var done = function () { buttons.forEach(function (b) { b.disabled = false; }); };
    DN.api(form.action, {body: new FormData(form), timeout: 60000}).then(function (r) {
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

  function init() {
    DN.$$('form[data-caja-form]').forEach(function (form) {
      form.addEventListener('submit', function (ev) { send(form, ev); });
      if (form.hasAttribute('data-caja-cierre')) { closing(form); }
      if (form.hasAttribute('data-caja-recibir')) { receiving(form); }
      if (form.action.indexOf('/caja/abrir') !== -1) { opening(form); }
    });
  }
  if (window.DN && DN.ready) { init(); } else { document.addEventListener('dn:ready', init); }
})();
