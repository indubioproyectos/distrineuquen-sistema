/* «Ajustes» (19.0.1.98.0): the gear sheet of every custom page. Standalone
 * (no dn.js needed), ES2017, wired by the distri_pedido.dn_ajustes macro.
 *
 * - Opening is one level down: a history entry, so the phone back closes
 *   it (same rule as the kit sheets). Without JavaScript #dn-aj:target.
 * - «Tamaño de letra»: --dn-scale on <html>, saved per device in
 *   localStorage «dn:letra» (the macro applies it before paint).
 * - «Instalar la app»: Android/Chrome beforeinstallprompt; on iOS the short
 *   Safari instructions; hidden when the app already runs installed. A page
 *   without a manifest gets the one in data-manifest.
 * - Armado preferences post to /armado/preferencias; with fetch the page
 *   reloads to show the new view.
 */
(function () {
  'use strict';
  var KEY = 'dn:letra';
  var doc = document;
  var root = doc.documentElement;
  var deferred = null;
  var el, scrimClick;

  function store(v) { try { localStorage.setItem(KEY, v); } catch (e) { /* private mode */ } }
  function stored() { try { return localStorage.getItem(KEY); } catch (e) { return null; } }
  function standalone() {
    return (window.matchMedia && matchMedia('(display-mode: standalone)').matches) || navigator.standalone === true;
  }
  function ios() {
    return /iphone|ipad|ipod/i.test(navigator.userAgent) || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
  }

  window.addEventListener('beforeinstallprompt', function (ev) { ev.preventDefault(); deferred = ev; paintInstall(); });
  window.addEventListener('appinstalled', function () { deferred = null; paintInstall(); });

  function paintInstall() {
    if (!el) { return; }
    var box = el.querySelector('.dn-aj__inst');
    var tip = el.querySelector('.dn-aj__ios');
    var btn = el.querySelector('[data-aj-instalar]');
    var isIos = ios() && !standalone();
    box.hidden = standalone() || !(deferred || isIos);
    btn.hidden = !deferred;
    tip.hidden = !isIos || !!deferred;
  }

  function paintLetter() {
    var v = stored() || '1';
    el.querySelectorAll('[data-letra]').forEach(function (b) {
      b.setAttribute('aria-pressed', b.getAttribute('data-letra') === v ? 'true' : 'false');
    });
  }

  function isOpen() { return el.classList.contains('open'); }
  function open() {
    if (isOpen()) { return; }
    el.classList.add('open');
    var st = history.state || {};
    var dn = Object.assign({}, st.dn || {});
    dn.d = (dn.d || 0) + 1;
    history.pushState({dn: dn, aj: 1}, '');
    var f = el.querySelector('button,a[href]');
    if (f) { f.focus(); }
    paintLetter();
    paintInstall();
  }
  function close(fromPop) {
    if (!isOpen()) { return; }
    el.classList.remove('open');
    if (!fromPop && history.state && history.state.aj) { history.back(); }
    var g = doc.getElementById('dn-gear');
    if (g) { g.focus(); }
  }

  function init() {
    el = doc.getElementById('dn-aj');
    var gear = doc.getElementById('dn-gear');
    if (!el || !gear) { return; }
    doc.body.appendChild(el);
    if (gear.classList.contains('dn-gear--float')) { doc.body.classList.add('dn-gear-float'); }
    if (location.hash === '#dn-aj') { history.replaceState(history.state, '', location.pathname + location.search); }
    gear.addEventListener('click', function (ev) { ev.preventDefault(); open(); });
    el.addEventListener('click', function (ev) {
      if (ev.target === el || ev.target.closest('[data-aj-close]')) { ev.preventDefault(); close(false); return; }
      var p = ev.target.closest('form[data-aj-pref] button');
      if (p) { p.form.ultimo = p; }
      var b = ev.target.closest('[data-letra]');
      if (b) {
        var v = b.getAttribute('data-letra');
        root.style.setProperty('--dn-scale', v);
        store(v);
        paintLetter();
        return;
      }
      if (ev.target.closest('[data-aj-instalar]') && deferred) {
        deferred.prompt();
        deferred.userChoice.then(function () { deferred = null; paintInstall(); });
      }
    });
    el.addEventListener('keydown', function (ev) { if (ev.key === 'Escape') { close(false); } });
    window.addEventListener('popstate', function () { if (isOpen() && !(history.state && history.state.aj)) { close(true); } });
    // armado preferences: save and show the page in the new view
    el.querySelectorAll('form[data-aj-pref]').forEach(function (form) {
      form.addEventListener('submit', function (ev) {
        if (!window.fetch) { return; }
        ev.preventDefault();
        var data = new FormData(form);
        var btn = ev.submitter || form.ultimo;
        if (btn) { data.append(btn.name, btn.value); }
        fetch(form.action, {method: 'POST', body: data, credentials: 'same-origin',
                            headers: {'X-Distri-Fetch': '1'}}).then(function (r) {
          if (!r.ok) { throw new Error(r.status); }
          location.replace(location.pathname + location.search);
        }).catch(function () { form.submit(); });
      });
    });
    // «Instalar la app» needs a manifest: the page's own, else the default
    var mf = el.getAttribute('data-manifest');
    if (mf && !doc.querySelector('link[rel="manifest"]')) {
      var link = doc.createElement('link');
      link.rel = 'manifest';
      link.href = mf;
      doc.head.appendChild(link);
    }
    paintLetter();
    paintInstall();
  }
  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
