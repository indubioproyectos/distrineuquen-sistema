/* «Depósito» (19.0.1.109.0): the depot pages (views/deposito_app.xml). Uses
 * the kit (../kit/dn.js, window.DN). Without JavaScript every form posts and
 * the server answers with the page or a redirect.
 *
 * - form[data-dp-form]: sent with DN.api (fetch); a [data-confirm] form (or
 *   submit button) asks first with DN.confirm. {url}: UP to the parent with
 *   its message (DN.up), DOWN when the answer says so (counter sale -> its
 *   delivery form), else replaces this page. Errors stay as a toast.
 * - [data-dp-show="name=value"]: shown only while that radio/checkbox of
 *   the form has that value ("name=" = unchecked/empty; "a|b" = either).
 * - form[data-dp-entrega]: «Devolución», «Saldo total» and «Queda debiendo»
 *   live; «Importe transferido» proposed like the backend wizard (the order
 *   minus the returns minus the cash) when «Sí» is chosen.
 * - form[data-dp-cierre]: the difference live and «Abrir caso por la
 *   diferencia» only when there is one.
 * - [data-dp-tab]: the tabs of the box (panels [data-dp-panel]).
 * - input[data-dp-buscar]: filters [data-dp-item] by data-texto as you type.
 * - In the delivery and closing forms Enter moves to the next field (it
 *   never sends them).
 * - 19.0.1.129.37 (nueva_venta): input[data-dp-files] («Subir comprobante»,
 *   several photos or PDF) lists the chosen files under it
 *   ([data-dp-files-list]) and says at once which one cannot be a receipt
 *   (fileProblem: type, more than 10 MB); the server checks them again and
 *   refuses the whole form with a message per file.
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
  function round(n) { return Math.round(n * 100) / 100; }

  // ------------------------------------------------------------ show / hide
  function value(form, name) {
    var els = DN.$$('[name="' + name + '"]', form);
    for (var i = 0; i < els.length; i++) {
      var el = els[i];
      if (el.type === 'radio' || el.type === 'checkbox') { if (el.checked) { return el.value; } }
      else { return el.value; }
    }
    return '';
  }
  function toggles(form) {
    DN.$$('[data-dp-show]', form).forEach(function (el) {
      var rule = el.getAttribute('data-dp-show').split('=');
      var wanted = (rule[1] || '').split('|');
      el.hidden = wanted.indexOf(value(form, rule[0])) === -1;
    });
  }

  // ------------------------------------------------------------ delivery
  function delivery(form) {
    var order = parseFloat(form.getAttribute('data-pedido')) || 0;
    var debt = parseFloat(form.getAttribute('data-deuda')) || 0;
    var transfer = DN.$('[name=importe_transf]', form);
    var cash = DN.$('[name=efectivo]', form);
    function returns() {
      if (value(form, 'devolvio') !== 'yes') { return 0; }
      return DN.$$('[data-dp-unit]', form).reduce(function (s, input) {
        return s + amount(input.value) * (parseFloat(input.getAttribute('data-dp-unit')) || 0);
      }, 0);
    }
    function paint() {
      toggles(form);
      var ret = round(returns());
      var total = round(order + debt - ret);
      DN.$$('[data-dp-devolucion]', form).forEach(function (el) { el.textContent = money(ret); });
      DN.$$('[data-dp-saldo]', form).forEach(function (el) { el.textContent = money(total); });
      var paid = amount(cash.value) + (value(form, 'transferencia') === 'yes' ? amount(transfer.value) : 0);
      var owes = round(total - paid);
      var box = DN.$('[data-dp-debe]', form);
      if (box) {
        box.className = 'dp-debe' + (owes > 0 ? '' : ' ok');
        DN.$('span', box).textContent = owes > 0 ? 'Queda debiendo' : (owes < 0 ? 'Queda a favor' : 'Queda al día');
        DN.$('b', box).textContent = money(Math.abs(owes));
      }
      var none = DN.$('[data-dp-nopago]', form);
      if (none) {
        none.hidden = amount(cash.value) > 0 || value(form, 'transferencia') !== 'no';
        if (none.hidden) { DN.$('input', none).checked = false; }
      }
    }
    form.addEventListener('change', function (ev) {
      if (ev.target.name === 'transferencia' && ev.target.value === 'yes' && !transfer.value.trim()) {
        var proposed = round(Math.max(order - returns() - amount(cash.value), 0));
        if (proposed) { transfer.value = String(proposed).replace('.', ','); }
      }
      paint();
    });
    form.addEventListener('input', paint);
    paint();
  }

  // ------------------------------------------------------------ receipt files
  // 19.0.1.129.37: the same limits as the server (models/comprobante_subir.py)
  var RECEIPT_MAX_BYTES = 10 * 1024 * 1024;
  var RECEIPT_TYPES = /^(image\/(jpeg|png|webp)|application\/pdf)$/;
  var RECEIPT_NAMES = /\.(jpe?g|png|webp|pdf)$/i;
  function fileProblem(file) {
    if (!file) { return 'No hay archivo.'; }
    if (file.size > RECEIPT_MAX_BYTES) { return 'Es demasiado grande (más de 10 MB).'; }
    if (!RECEIPT_TYPES.test(file.type || '') && !RECEIPT_NAMES.test(file.name || '')) {
      return 'No es una foto (JPG, PNG o WEBP) ni un PDF.';
    }
    return '';
  }
  function receiptFiles(form) {
    DN.$$('input[data-dp-files]', form).forEach(function (input) {
      var list = input.closest('form').querySelector('[data-dp-files-list]');
      input.addEventListener('change', function () {
        if (!list) { return; }
        list.innerHTML = Array.prototype.map.call(input.files || [], function (f) {
          var bad = fileProblem(f);
          return '<li class="' + (bad ? 'bad' : 'ok') + '"><b>' + DN.h(f.name) + '</b> ' +
            (bad ? DN.h(bad) : Math.max(1, Math.round(f.size / 1024)) + ' KB') + '</li>';
        }).join('');
      });
    });
  }

  // ------------------------------------------------------------ closing
  function closing(form) {
    var expected = parseFloat(form.getAttribute('data-esperado')) || 0;
    var counted = DN.$('[name=contado]', form);
    var out = DN.$('[data-dp-dif]', form);
    function paint() {
      var typed = counted.value.trim() !== '';
      var diff = round(amount(counted.value) - expected);
      out.hidden = !typed;
      out.className = 'dp-dif ' + (diff === 0 ? 'ok' : (diff < 0 ? 'falta' : 'sobra'));
      DN.$('span', out).textContent = diff === 0 ? 'Sin diferencia' : (diff < 0 ? 'Diferencia · falta' : 'Diferencia · sobra');
      DN.$('b', out).textContent = diff === 0 ? '$ 0' : (diff < 0 ? '− ' : '+ ') + money(Math.abs(diff));
      DN.$$('[data-dp-con-dif]', form.ownerDocument).forEach(function (el) { el.hidden = !typed || diff === 0; });
    }
    counted.addEventListener('input', paint);
    paint();
  }

  // Enter in a field goes to the next one instead of sending the form (the
  // phone's «Ir» key on the code or the cash must not register a delivery)
  function enterMovesOn(form) {
    form.addEventListener('keydown', function (ev) {
      var t = ev.target;
      if (ev.key !== 'Enter' || t.tagName !== 'INPUT' || t.type === 'submit') { return; }
      ev.preventDefault();
      var fields = DN.$$('input:not([type=hidden]),textarea,select', form).filter(function (el) {
        return !el.closest('[hidden]') && !el.disabled;
      });
      var next = fields[fields.indexOf(t) + 1];
      if (next) { next.focus(); } else { t.blur(); }
    });
  }

  // ------------------------------------------------------------ sending
  function send(form, ev) {
    ev.preventDefault();
    var submitter = ev.submitter || null;
    var action = (submitter && submitter.getAttribute('formaction')) || form.action;
    var ask = (submitter && submitter.getAttribute('data-confirm')) || form.getAttribute('data-confirm');
    var go = function () {
      var buttons = DN.$$('button[type=submit]', form).concat(
        form.id ? DN.$$('button[form="' + form.id + '"]') : []);
      buttons.forEach(function (b) { b.disabled = true; });
      var done = function () { buttons.forEach(function (b) { b.disabled = false; }); };
      var body = new FormData(form);
      if (submitter && submitter.name) { body.append(submitter.name, submitter.value); }
      DN.api(action, {body: body, timeout: 60000}).then(function (r) {
        if (r && r.url) {
          if (r.down) { location.href = r.url; return; }
          var a = document.createElement('a');
          var up = document.createElement('a');
          a.href = r.url;
          up.href = document.body.getAttribute('data-parent') || '/';
          if (a.pathname === up.pathname && a.pathname !== location.pathname) { DN.up(r.url); }
          else { location.replace(r.url); }
          return;
        }
        done();
      }, function (e) {
        done();
        DN.fail(e);
        var err = DN.$('#dp-error');
        if (err) { err.hidden = true; }
      });
    };
    if (ask) {
      DN.confirm(ask, (submitter && submitter.getAttribute('data-confirm-ok')) || 'Sí, seguir').then(function (yes) {
        if (yes) { go(); }
      });
    } else { go(); }
  }

  // ------------------------------------------------------------ tabs, search
  function tabs() {
    var buttons = DN.$$('[data-dp-tab]');
    if (!buttons.length) { return; }
    function show(key) {
      buttons.forEach(function (b) {
        var on = b.getAttribute('data-dp-tab') === key;
        b.classList.toggle('on', on);
        b.setAttribute('aria-selected', on ? 'true' : 'false');
      });
      DN.$$('[data-dp-panel]').forEach(function (p) { p.hidden = p.getAttribute('data-dp-panel') !== key; });
    }
    buttons.forEach(function (b) {
      b.addEventListener('click', function () { show(b.getAttribute('data-dp-tab')); });
    });
    show(buttons[0].getAttribute('data-dp-tab'));
  }
  function search() {
    DN.$$('input[data-dp-buscar]').forEach(function (input) {
      var list = DN.$$('[data-dp-item]');
      var empty = DN.$('[data-dp-nada]');
      input.addEventListener('input', function () {
        var q = input.value.trim().toLowerCase();
        var shown = 0;
        list.forEach(function (el) {
          var hit = !q || (el.getAttribute('data-texto') || '').indexOf(q) !== -1;
          el.hidden = !hit;
          if (hit) { shown += 1; }
        });
        if (empty) { empty.hidden = shown > 0 || !list.length; }
      });
    });
  }

  function init() {
    tabs();
    search();
    DN.$$('form[data-dp-form]').forEach(function (form) {
      form.addEventListener('submit', function (ev) { send(form, ev); });
      receiptFiles(form);
      if (form.hasAttribute('data-dp-entrega')) { delivery(form); enterMovesOn(form); }
      else if (form.hasAttribute('data-dp-cierre')) { closing(form); enterMovesOn(form); }
      else if (DN.$('[data-dp-show]', form)) {
        toggles(form);
        form.addEventListener('change', function () { toggles(form); });
      }
    });
  }
  if (window.DN && DN.ready) { init(); } else { document.addEventListener('dn:ready', init); }
})();
