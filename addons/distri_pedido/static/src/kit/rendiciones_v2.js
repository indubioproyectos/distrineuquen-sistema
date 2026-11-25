/* 19.0.1.129.44: «Rendiciones» and «Casos» on the design system
   (controllers/rendiciones_v2.py, views/rendiciones_v2.xml), ui_v2 only.
   The pages work without this file: every action is a form post and a
   question the server asks back. With it:
   - a form with data-rd-confirm asks first in the kit's sheet (DN.confirm);
     «Sí» sets its hidden confirmar=1 and sends it, «Cancelar» sends nothing
     and gives the button back;
   - the ⋯ menu (<details>) closes on a tap outside or Escape;
   - a <select data-rd-auto> sends its GET form when it changes;
   - a table row with data-href opens it; the PC gear opens «Ajustes». */
(function () {
  'use strict';
  var doc = document;

  // confirm:start
  function askFirst(form, confirm) {
    // returns a submit handler for `form`; `confirm(text, ok)` is DN.confirm
    return function (ev) {
      var yes = form.querySelector('input[name="confirmar"]');
      if (!yes || yes.value === '1') { return; }
      ev.preventDefault();
      var buttons = form.querySelectorAll('button');
      var text = form.getAttribute('data-rd-confirm');
      var ok = form.getAttribute('data-rd-ok') || 'Sí, seguir';
      return confirm(text, ok).then(function (answer) {
        for (var i = 0; i < buttons.length; i++) { buttons[i].disabled = false; }
        if (!answer) { return false; }
        yes.value = '1';
        form.submit();
        return true;
      });
    };
  }
  // confirm:end

  function init() {
    var DN = window.DN;
    Array.prototype.forEach.call(doc.querySelectorAll('form[data-rd-confirm]'), function (form) {
      if (DN && DN.confirm) { form.addEventListener('submit', askFirst(form, DN.confirm)); }
    });
    Array.prototype.forEach.call(doc.querySelectorAll('select[data-rd-auto]'), function (sel) {
      sel.addEventListener('change', function () { if (sel.form) { sel.form.submit(); } });
    });
    Array.prototype.forEach.call(doc.querySelectorAll('tr[data-href]'), function (tr) {
      tr.addEventListener('click', function (ev) {
        if (ev.target.closest('a,button,form')) { return; }
        location.href = tr.getAttribute('data-href');
      });
    });
    Array.prototype.forEach.call(doc.querySelectorAll('[data-rd-gear]'), function (a) {
      a.addEventListener('click', function (ev) {
        var gear = doc.getElementById('dn-gear');
        if (gear) { ev.preventDefault(); gear.click(); }
      });
    });
    doc.addEventListener('click', function (ev) {
      Array.prototype.forEach.call(doc.querySelectorAll('details.rd-more[open]'), function (d) {
        if (!d.contains(ev.target)) { d.removeAttribute('open'); }
      });
    });
    doc.addEventListener('keydown', function (ev) {
      if (ev.key !== 'Escape') { return; }
      Array.prototype.forEach.call(doc.querySelectorAll('details.rd-more[open]'), function (d) {
        d.removeAttribute('open');
        var s = d.querySelector('summary');
        if (s) { s.focus(); }
      });
    });
  }
  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
