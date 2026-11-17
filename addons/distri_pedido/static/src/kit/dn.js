/* Distrineuquén kit v1 (19.0.1.94.0): shared helpers for the standalone
 * staff pages. Vanilla ES2017, no framework, <= 6 KB gzip. Loaded with
 * <script defer> from the distri_pedido.dn_head macro; exposes window.DN.
 *
 * Helpers
 *   DN.h(text)                 HTML-escape a value.
 *   DN.toast(text, kind)       Bottom toast; kind 'ok' | 'err' | ''. Errors
 *                              stay until tapped, the rest hide after 3 s.
 *   DN.banner(text, kind)      Status line under the top bar (#dn-banner);
 *                              DN.banner('') hides it. Offline/online is wired.
 *   DN.api(url, opts)          fetch() wrapper -> Promise of the JSON body
 *                              (or text for HTML). opts: method, body
 *                              (FormData | URLSearchParams | object), timeout
 *                              ms (default 20000). Sends X-Distri-Fetch: 1 and,
 *                              for an object body, the page CSRF token
 *                              (<meta name="csrf-token">). An expired session
 *                              (redirect to /web/login) sends the whole page
 *                              to the login and back. Rejects with an Error
 *                              carrying .status and .reload (the page is stale).
 *   DN.sheet(el)               Bottom sheet: .open() / .close(). Opening goes
 *                              DOWN one level (phone back closes it).
 *   DN.confirm(text, ok)       Promise<boolean> with a generated sheet.
 *   DN.skeleton(el, n)         Fill el with n placeholder rows.
 *   DN.once(form)              Disable the submit button after the first
 *                              submit of a plain POST form (double tap).
 *
 * Navigation (owner rule): «back» goes UP one level to the parent, never to
 * the previously visited sibling.
 *   <body data-parent="/armado">  declares the parent of the page ('' = top).
 *   a[data-dn="down"]   normal link to a child: one new history entry.
 *   a[data-dn="side"]   sibling (tabs, other category): location.replace,
 *                       no new entry; the sibling keeps the same parent.
 *   a[data-dn="up"], .dn-top [data-dn-back]   go to the parent.
 *   DN.up(url)          to the parent (or url, which must be the parent page
 *                       with another query). If the previous entry is the
 *                       parent it goes back there and reloads it fresh;
 *                       otherwise location.replace(parent).
 *   DN.side(url)        location.replace for a sibling page.
 *   DN.push(state, hash) / DN.replace(state, hash)   in-page levels (an item
 *                       inside the page, a sheet): push = down, replace =
 *                       sideways (next item). DN.back() leaves the deepest
 *                       in-page level, or goes UP when there is none.
 *   DN.onPop(fn)        fn(state) when the phone back lands on an in-page
 *                       level of this page ({} = the page itself).
 *   The phone back follows the same rule: when the page was not opened from
 *   its parent, a guard entry below the page catches it and replaces the
 *   page with the parent.
 */
(function () {
  'use strict';
  var DN = window.DN = {};
  var doc = document;
  var root = doc.documentElement;
  var KEY = 'dn:nav';
  root.classList.add('dn-js');

  function $(sel, el) { return (el || doc).querySelector(sel); }
  DN.$ = $;
  DN.$$ = function (sel, el) { return Array.prototype.slice.call((el || doc).querySelectorAll(sel)); };

  DN.h = function (v) {
    return String(v == null ? '' : v).replace(/[&<>"']/g, function (c) {
      return {'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;'}[c];
    });
  };

  // ------------------------------------------------------------ toast
  var toastEl, toastTimer;
  DN.toast = function (text, kind) {
    if (!toastEl) {
      toastEl = doc.createElement('div');
      toastEl.id = 'dn-toast';
      toastEl.setAttribute('role', 'status');
      toastEl.addEventListener('click', function () {
        if (toastEl.onTap) { toastEl.onTap(); } else { toastEl.hidden = true; }
      });
      doc.body.appendChild(toastEl);
    }
    toastEl.textContent = text;
    toastEl.className = kind || '';
    toastEl.onTap = null;
    toastEl.hidden = false;
    clearTimeout(toastTimer);
    if (kind !== 'err') { toastTimer = setTimeout(function () { toastEl.hidden = true; }, 3000); }
    return toastEl;
  };

  // ----------------------------------------------------------- banner
  DN.banner = function (text, kind) {
    var el = $('#dn-banner');
    if (!el) {
      el = doc.createElement('div');
      el.id = 'dn-banner';
      el.setAttribute('role', 'status');
      var top = $('.dn-top');
      if (top) { top.after(el); } else { doc.body.prepend(el); }
    }
    el.className = 'dn-banner' + (kind ? ' dn-banner--' + kind : '');
    el.textContent = text || '';
    el.hidden = !text;
  };
  window.addEventListener('offline', function () { DN.banner('Sin señal: esperá a que vuelva para guardar.', 'warn'); });
  window.addEventListener('online', function () { DN.banner(''); });

  // -------------------------------------------------------------- api
  function fail(text, status, reload) {
    var e = new Error(text);
    e.status = status || 0;
    e.reload = !!reload;
    return e;
  }
  function toLogin() {
    location.href = '/web/login?redirect=' + encodeURIComponent(location.pathname + location.search);
  }
  DN.api = function (url, opts) {
    opts = opts || {};
    var body = opts.body;
    var method = opts.method || (body ? 'POST' : 'GET');
    if (body && !(body instanceof FormData) && !(body instanceof URLSearchParams)) {
      var fd = new FormData();
      Object.keys(body).forEach(function (k) { fd.append(k, body[k]); });
      body = fd;
    }
    if (body && !body.has('csrf_token')) {
      var meta = $('meta[name="csrf-token"]');
      if (meta) { body.append('csrf_token', meta.content); }
    }
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl && setTimeout(function () { ctrl.abort(); }, opts.timeout || 20000);
    return fetch(url, {
      method: method, body: body, credentials: 'same-origin', signal: ctrl && (opts.signal || ctrl.signal),
      headers: {'X-Distri-Fetch': '1', 'Accept': 'application/json, text/html'}
    }).then(function (r) {
      clearTimeout(timer);
      if (r.redirected && /\/web\/login/.test(r.url)) {
        toLogin();
        throw fail('Se cerró la sesión. Entrá de nuevo.', 401);
      }
      var type = r.headers.get('Content-Type') || '';
      if (type.indexOf('application/json') !== -1) {
        return r.json().then(function (j) {
          if (!r.ok || j.ok === false) {
            throw fail(j.error || 'No se pudo guardar.', r.status, r.status === 409 || r.status === 404);
          }
          return j;
        });
      }
      if (r.ok && type.indexOf('text/html') !== -1) { return r.text(); }
      throw fail(r.status === 400 ? 'La página venció. Tocá acá para recargar.'
        : 'Hubo un error. Tocá acá para recargar.', r.status, true);
    }, function (e) {
      clearTimeout(timer);
      if (e.name === 'AbortError' && opts.signal && opts.signal.aborted) { throw e; }
      throw fail('Sin conexión. No se guardó: probá de nuevo.', 0);
    });
  };
  // toast for an api error; a stale page reloads on tap
  DN.fail = function (e) {
    if (e.name === 'AbortError') { return; }
    var t = DN.toast(e.message, 'err');
    if (e.reload) { t.onTap = function () { location.reload(); }; }
  };

  // -------------------------------------------------------- navigation
  var parent = '';
  var popFns = [];
  function state() { return (history.state && history.state.dn) || null; }
  function setState(s, push, hash) {
    var url = hash == null ? location.href : location.pathname + location.search + hash;
    history[push ? 'pushState' : 'replaceState']({dn: s}, '', url);
  }
  function path(url) { var a = doc.createElement('a'); a.href = url; return a.pathname; }
  function stash(v) { try { sessionStorage.setItem(KEY, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  function unstash() {
    try {
      var v = JSON.parse(sessionStorage.getItem(KEY) || 'null');
      sessionStorage.removeItem(KEY);
      return v;
    } catch (e) { return null; }
  }

  DN.up = function (url) {
    var target = url || parent;
    if (!target) { return; }
    var s = state();
    if (s && s.pb) {
      stash({up: target});
      history.go(-((s.d || 0) + 1));
    } else {
      location.replace(target);
    }
  };
  DN.side = function (url) {
    var s = state();
    stash({side: path(url), pb: !!(s && s.pb), g: !!(s && s.g)});
    location.replace(url);
  };
  DN.push = function (extra, hash) {
    var s = Object.assign({}, state(), extra || {});
    s.d = (s.d || 0) + 1;
    setState(s, true, hash);
  };
  DN.replace = function (extra, hash) {
    var s = Object.assign({}, state(), extra || {});
    setState(s, false, hash);
  };
  DN.back = function () {
    var s = state();
    if (s && s.d) { history.back(); } else { DN.up(); }
  };
  DN.depth = function () { var s = state(); return (s && s.d) || 0; };
  DN.onPop = function (fn) { popFns.push(fn); };

  function initNav() {
    parent = doc.body.getAttribute('data-parent') || '';
    var mark = unstash();
    var here = location.href.split('#')[0];
    if (mark && mark.up && path(mark.up) === location.pathname) {
      // arrived here going UP: show it with the query of the action
      var a = doc.createElement('a');
      a.href = mark.up;
      if (a.href !== here) { stash({keep: state(), at: location.pathname}); location.replace(mark.up); return; }
    }
    if (mark && mark.keep && mark.at === location.pathname) { setState(mark.keep, false); }
    if (state() && state().guard) { location.replace(parent || '/'); return; }
    if (!state()) {
      var pb = false, g = false;
      if (mark && mark.down === location.pathname && parent && mark.from === path(parent)) { pb = true; }
      if (mark && mark.side === location.pathname) { pb = mark.pb; g = mark.g; }
      if (parent && !pb && !g) {
        // guard entry: the phone back from here lands on it -> parent
        setState({guard: 1}, false);
        setState({pb: false, d: 0, g: true}, true);
      } else {
        setState({pb: pb, d: 0, g: g}, false);
      }
    }
    window.addEventListener('popstate', function (ev) {
      var s = ev.state && ev.state.dn;
      if (s && s.guard) { location.replace(parent); return; }
      popFns.forEach(function (fn) { fn(s || {}); });
    });
    window.addEventListener('pageshow', function (ev) {
      var m = ev.persisted && unstash();
      if (m && m.up) { stash({keep: state(), at: location.pathname}); location.replace(m.up); }
    });
    doc.addEventListener('click', function (ev) {
      var a = ev.target.closest && ev.target.closest('a[data-dn],[data-dn-back]');
      if (!a || ev.ctrlKey || ev.metaKey || ev.shiftKey) { return; }
      if (a.hasAttribute('data-dn-back')) { ev.preventDefault(); DN.back(); return; }
      var how = a.getAttribute('data-dn');
      if (how === 'down') { stash({down: a.pathname, from: location.pathname}); return; }
      ev.preventDefault();
      if (how === 'side') { DN.side(a.href); } else if (how === 'up') { DN.up(a.getAttribute('href')); }
    });
  }

  // ------------------------------------------------------------ sheet
  var scrim;
  DN.sheet = function (el) {
    function close(fromPop) {
      if (!el.classList.contains('open')) { return; }
      el.classList.remove('open');
      if (scrim) { scrim.hidden = true; }
      if (!fromPop && state() && state().sheet === el.id) { history.back(); }
      if (api.onclose) { api.onclose(); }
    }
    var api = {
      open: function () {
        if (!scrim) {
          scrim = doc.createElement('div');
          scrim.className = 'dn-scrim';
          scrim.addEventListener('click', function () { var o = $('.dn-sheet.open'); if (o) { o.dnSheet.close(); } });
          doc.body.appendChild(scrim);
        }
        scrim.hidden = false;
        el.classList.add('open');
        DN.push({sheet: el.id});
        var f = $('input:not([type=hidden]),button', el);
        if (f) { f.focus(); }
      },
      close: function () { close(false); }
    };
    el.dnSheet = api;
    DN.onPop(function (s) { if (s.sheet !== el.id) { close(true); } });
    el.addEventListener('keydown', function (ev) {
      if (ev.key === 'Escape') { api.close(); }
      if (ev.key === 'Tab') {
        // focus trap
        var f = DN.$$('button,input,a[href],select,textarea', el).filter(function (x) { return !x.disabled && x.offsetParent; });
        if (!f.length) { return; }
        if (ev.shiftKey && doc.activeElement === f[0]) { ev.preventDefault(); f[f.length - 1].focus(); }
        else if (!ev.shiftKey && doc.activeElement === f[f.length - 1]) { ev.preventDefault(); f[0].focus(); }
      }
    });
    DN.$$('[data-dn-close]', el).forEach(function (b) {
      b.addEventListener('click', function (ev) { ev.preventDefault(); api.close(); });
    });
    return api;
  };

  DN.confirm = function (text, ok) {
    return new Promise(function (resolve) {
      var el = doc.createElement('div');
      el.className = 'dn-sheet';
      el.id = 'dn-confirm';
      el.setAttribute('role', 'dialog');
      el.innerHTML = '<p>' + DN.h(text) + '</p><div class="dn-grid2"><button type="button" class="dn-btn dn-btn--ghost" data-dn-close>Cancelar</button>'
        + '<button type="button" class="dn-btn">' + DN.h(ok || 'Sí, seguir') + '</button></div>';
      doc.body.appendChild(el);
      var s = DN.sheet(el);
      var answer = false;
      s.onclose = function () { el.remove(); resolve(answer); };
      el.querySelector('.dn-btn:not([data-dn-close])').addEventListener('click', function () { answer = true; s.close(); });
      s.open();
    });
  };

  DN.skeleton = function (el, n) {
    el.innerHTML = new Array((n || 3) + 1).join('<div class="dn-skel" aria-hidden="true"></div>');
  };

  DN.once = function (form) {
    form.addEventListener('submit', function (ev) {
      if (ev.defaultPrevented) { return; }
      var b = form.querySelector('button[type="submit"],button:not([type])');
      if (b) { setTimeout(function () { b.disabled = true; }, 0); }
    });
  };
  window.addEventListener('pageshow', function () {
    DN.$$('form[method="post"] button:disabled').forEach(function (b) { b.disabled = false; });
  });

  function init() {
    initNav();
    DN.$$('form[method="post"]:not([data-fetch])').forEach(DN.once);
    if (navigator.onLine === false) { DN.banner('Sin señal: esperá a que vuelva para guardar.', 'warn'); }
    doc.dispatchEvent(new Event('dn:ready'));
    DN.ready = true;
  }
  if (doc.readyState === 'loading') { doc.addEventListener('DOMContentLoaded', init); } else { init(); }
})();
