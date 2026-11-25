/* The phone keyboard on every custom page (19.0.1.129.40, owner 25/11).
 * Standalone (no dn.js), ES2017, loaded by the distri_pedido.dn_ajustes macro,
 * so kit screens, /pedido, /cobro, /cuentas, /despacho and the driver app
 * all get it.
 *
 * On a phone the on-screen keyboard stayed open after Enter in a search box
 * or after choosing an option, and had to be closed by hand. Here:
 * - Enter in a one-line input closes the keyboard (blur), after the page's
 *   own handlers ran (keyup): a form that submits on Enter still submits,
 *   and a page that already moved the focus (caja, depósito: Enter = next
 *   field) is left alone. enterkeyhint="next" moves to the next field.
 * - Choosing from a <select> or a <datalist>, or tapping anything that is
 *   not a field (a suggestion, a button, the page), closes it too. Tapping
 *   another field or a label does not.
 * - Inputs without enterkeyhint get one: "search" for search boxes, "done"
 *   for numbers and phones.
 * Never textareas (Enter is a new line) nor contenteditable. A field (or a
 * container) with data-dn-teclado="queda" keeps the keyboard. Only on touch
 * screens (pointer: coarse): a PC with a mouse behaves as before.
 * Pure client side: nothing posted changes, so it has no switch.
 */
(function () {
  'use strict';
  var TEXTY = /^(text|search|tel|number|email|url|password)$/;
  var FIELDS = 'input, textarea, select, label, [contenteditable=""], [contenteditable="true"]';

  function isTexty(el) {
    return !!el && el.tagName === 'INPUT' && TEXTY.test(String(el.type || 'text').toLowerCase());
  }

  function keeps(el) {
    return !!(el && el.closest && el.closest('[data-dn-teclado="queda"]'));
  }

  function hintFor(el) {
    var type = String(el.type || 'text').toLowerCase();
    var mode = String(el.getAttribute('inputmode') || '').toLowerCase();
    if (type === 'search' || mode === 'search') { return 'search'; }
    if (type === 'number' || type === 'tel' || mode === 'numeric' || mode === 'decimal' || mode === 'tel') {
      return 'done';
    }
    return '';
  }

  // enterkeyhint where the page did not set one
  function enhance(scope) {
    var list = scope.querySelectorAll ? scope.querySelectorAll('input:not([enterkeyhint])') : [];
    Array.prototype.forEach.call(list, function (el) {
      if (!isTexty(el)) { return; }
      var hint = hintFor(el);
      if (hint) { el.setAttribute('enterkeyhint', hint); }
    });
  }

  function usable(el) {
    return !el.disabled && !el.readOnly && el.type !== 'hidden' && !el.hidden &&
      !(el.closest && el.closest('[hidden]')) && (el.offsetParent !== null || el.getClientRects().length > 0);
  }

  function nextField(el, doc) {
    var scope = el.form || doc;
    var all = Array.prototype.slice.call(scope.querySelectorAll('input, select, textarea'));
    var i = all.indexOf(el);
    for (var j = i + 1; j < all.length; j++) {
      if (usable(all[j]) && (isTexty(all[j]) || all[j].tagName !== 'INPUT')) { return all[j]; }
    }
    return null;
  }

  function onKeyup(ev, doc) {
    if (!(ev.key === 'Enter' || ev.keyCode === 13) || ev.isComposing) { return false; }
    var t = ev.target;
    if (!isTexty(t) || keeps(t) || doc.activeElement !== t) { return false; }
    if (String(t.getAttribute('enterkeyhint') || '').toLowerCase() === 'next') {
      var n = nextField(t, doc);
      if (n) { n.focus(); return true; }
    }
    t.blur();
    return true;
  }

  function inDatalist(t, doc) {
    var id = t.getAttribute && t.getAttribute('list');
    var dl = id && doc.getElementById(id);
    if (!dl || !t.value) { return false; }
    return Array.prototype.some.call(dl.querySelectorAll('option'), function (o) {
      return (o.value || o.textContent) === t.value;
    });
  }

  // a <select> answered, or a <datalist> suggestion picked
  function onChoice(ev, doc) {
    var t = ev.target;
    if (!t || keeps(t)) { return false; }
    if (t.tagName === 'SELECT' && ev.type === 'change') {
      t.blur();
      return true;
    }
    if (isTexty(t) && doc.activeElement === t && !ev.inputType && inDatalist(t, doc)) {
      // a datalist pick fires «input» without inputType (typing has one)
      t.blur();
      return true;
    }
    return false;
  }

  // a tap on anything that is not a field: a suggestion, a button, the page
  function onTap(ev, doc) {
    var a = doc.activeElement;
    var t = ev.target;
    if (!isTexty(a) || keeps(a) || !t || t === a) { return false; }
    if (t.closest && (t.closest(FIELDS) || keeps(t))) { return false; }
    a.blur();
    return true;
  }

  var api = { isTexty: isTexty, hintFor: hintFor, enhance: enhance, onKeyup: onKeyup,
              onChoice: onChoice, onTap: onTap };
  if (typeof window !== 'undefined') { window.DNTeclado = api; }
  if (typeof document === 'undefined' || typeof window === 'undefined') { return; }
  var doc = document;
  var touch = !window.matchMedia || window.matchMedia('(pointer: coarse)').matches;
  if (!touch) { return; }

  function start() {
    enhance(doc);
    // fields drawn later by the page (lists, sheets)
    if (window.MutationObserver) {
      new MutationObserver(function (records) {
        records.forEach(function (r) {
          Array.prototype.forEach.call(r.addedNodes, function (n) {
            if (n.nodeType === 1) {
              if (n.tagName === 'INPUT' && !n.hasAttribute('enterkeyhint')) { enhance(n.parentNode || doc); }
              else { enhance(n); }
            }
          });
        });
      }).observe(doc.body, {childList: true, subtree: true});
    }
  }
  doc.addEventListener('keyup', function (ev) { onKeyup(ev, doc); });
  doc.addEventListener('change', function (ev) { onChoice(ev, doc); });
  doc.addEventListener('input', function (ev) { onChoice(ev, doc); });
  // capture: before the page's own handler, which may focus another field
  doc.addEventListener('click', function (ev) { onTap(ev, doc); }, true);
  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', start); } else { start(); }
}());
