/* Saves without extra history entries on the legacy pages (19.0.1.95.0).
 * A native POST + redirect added one entry per save, so «back» needed N+1
 * presses. Same POST forms and CSRF, sent with fetch(); the reply replaces
 * this page instead of stacking on it. Rules and audit: models/ir_http.py.
 * Opt-outs: form[data-native]. Links: a[data-side] (sibling: replace),
 * a[data-up] (parent: back when it is the previous entry, else replace);
 * GET form[data-side] replaces too. ES5 + fetch; without fetch, all native.
 */
(function () {
  'use strict';
  var W = window, doc = document, KEY = 'sh:carry', FRESH = 'sh:fresh', lastBtn = null;
  if (W.DN || !W.fetch || !W.FormData || !history.replaceState) { return; }

  function abs(u) { var a = doc.createElement('a'); a.href = u; return a; }
  function bare(u) { return abs(u).href.split('#')[0]; }
  function same(u) { var a = abs(u); return a.protocol === location.protocol && a.host === location.host; }
  function put(k, v) { try { sessionStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private mode */ } }
  function take(k) {
    try { var v = JSON.parse(sessionStorage.getItem(k) || 'null'); sessionStorage.removeItem(k); return v; } catch (e) { return null; }
  }
  function st() { return history.state || {}; }
  // replace this entry; the new page keeps our previous entry as its parent
  function replaceTo(url) {
    if (!same(url)) { location.assign(url); return; }
    if (bare(url) === bare(location.href)) {
      // only the #fragment differs: replace() would not reload
      history.replaceState(history.state, '', abs(url).href);
      location.reload();
      return;
    }
    put(KEY, {to: bare(url), prev: st().shPrev || null});
    location.replace(url);
  }

  function notice(text) {
    var el = doc.getElementById('sh-aviso');
    if (!el) {
      el = doc.createElement('div');
      el.id = 'sh-aviso';
      el.setAttribute('role', 'alert');
      el.style.cssText = 'position:fixed;left:12px;right:12px;bottom:12px;z-index:9999;background:#a1281e;color:#fff;' +
        'border-radius:12px;padding:12px 48px 12px 14px;font:15px/1.4 sans-serif';
      el.innerHTML = '<span></span><button type="button" aria-label="Cerrar" style="position:absolute;right:2px;top:2px;' +
        'width:44px;height:44px;border:0;background:none;color:#fff;font-size:24px">×</button>';
      el.lastChild.onclick = function () { el.parentNode.removeChild(el); };
      doc.body.appendChild(el);
    }
    el.firstChild.textContent = text;
  }

  // urlencoded like the native submit; null when a file goes with it
  function encode(fd) {
    var q = [], file = false;
    fd.forEach(function (v, k) {
      if (typeof v !== 'string') { file = true; }
      q.push(encodeURIComponent(k) + '=' + encodeURIComponent(v).replace(/%20/g, '+'));
    });
    return file ? null : q.join('&');
  }
  // the page's own onsubmit may have disabled its button too: enable them all
  function lock(form, on) {
    var b = form.querySelectorAll('button,input[type=submit]'), i;
    form.shBusy = on;
    for (i = 0; i < b.length; i++) { b[i].disabled = on; }
  }
  function release(form) { lock(form, false); }
  function reply(r, form) {
    var loc = r.headers.get('X-Distri-Location'), type = r.headers.get('Content-Type') || '',
      disp = r.headers.get('Content-Disposition') || '';
    if (loc) { return replaceTo(loc); }
    if (/html/i.test(type) && !/attachment/i.test(disp)) {
      if (r.redirected) { return replaceTo(r.url); }
      return r.text().then(function (html) {
        if (r.status >= 400 && !/<form[\s>]/i.test(html)) {
          // a one-line error page («… Volver»): keep this page and what was typed
          var t = new DOMParser().parseFromString(html, 'text/html').body.textContent
            .replace(/\s+/g, ' ').replace(/ ?Volver ?$/, '').trim();
          release(form);
          return notice(/csrf/i.test(t) ? 'La página venció: recargala y probá de nuevo.' : t || 'No se pudo guardar.');
        }
        // the same page with an error or a question: same address, no new entry
        doc.open();
        doc.write(html);
        doc.close();
      });
    }
    return r.blob().then(function (blob) {
      var m = /filename="?([^";]+)/i.exec(disp), a = doc.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = m ? m[1] : 'archivo';
      doc.body.appendChild(a);
      a.click();
      release(form);
    });
  }

  function onSubmit(ev) {
    var form = ev.target;
    if (ev.defaultPrevented || form.nodeName !== 'FORM' || form.hasAttribute('data-native')) { return; }
    // getAttribute: a field named «action» or «target» shadows the property
    var post = /^post$/i.test(form.getAttribute('method') || ''), target = form.getAttribute('target'),
      action = abs(form.getAttribute('action') || location.href).href,
      btn = ev.submitter || (lastBtn && lastBtn.form === form ? lastBtn : null), data, body;
    lastBtn = null;
    if ((target && target !== '_self') || !same(action) || (!post && !form.hasAttribute('data-side'))) { return; }
    ev.preventDefault();
    data = new FormData(form);
    if (btn && btn.name && btn.type !== 'image') { data.append(btn.name, btn.value); }
    if (!post) { body = encode(data); return replaceTo(action.split(/[?#]/)[0] + (body ? '?' + body : '')); }
    if (form.shBusy) { return; }
    if (navigator.onLine === false) { return notice('Sin señal: no se envió. Probá de nuevo cuando vuelva.'); }
    body = /multipart/i.test(form.getAttribute('enctype')) ? null : encode(data);
    var ctrl = W.AbortController && new AbortController(),
      timer = ctrl && setTimeout(function () { ctrl.abort(); }, body === null ? 180000 : 45000),
      headers = {'X-Distri-Nav': '1', Accept: 'text/html,*/*;q=0.8'};
    if (body !== null) { headers['Content-Type'] = 'application/x-www-form-urlencoded'; }
    lock(form, true);
    fetch(action, {method: 'POST', body: body === null ? data : body, credentials: 'same-origin',
      redirect: 'follow', headers: headers, signal: ctrl ? ctrl.signal : undefined})
      .then(function (r) { clearTimeout(timer); return reply(r, form); })['catch'](function () {
        // never re-sent by itself: it may have reached the server
        clearTimeout(timer);
        release(form);
        notice('No se pudo enviar (sin señal o tardó mucho). Fijate si quedó guardado y, si no, probá de nuevo.');
      });
  }

  function onClick(ev) {
    var t = ev.target, b = t.closest && t.closest('button,input'), a = t.closest && t.closest('a[data-side],a[data-up]');
    if (b && (b.type === 'submit' || b.type === 'image')) { lastBtn = b; }
    if (!a || ev.defaultPrevented || ev.button || ev.ctrlKey || ev.metaKey || ev.shiftKey || !same(a.href)) { return; }
    ev.preventDefault();
    var prev = st().shPrev;
    if (a.hasAttribute('data-side')) {
      replaceTo(a.href);
    } else if (prev && abs(prev).pathname === a.pathname && history.length > 1) {
      put(FRESH, a.href);
      history.back();
    } else {
      put(KEY, {to: bare(a.href), prev: null});
      location.replace(a.href);
    }
  }

  // back on the parent after a[data-up]: show it fresh, with the link's query
  function onShow(ev) {
    var want = take(FRESH), nav = W.performance && performance.getEntriesByType &&
      performance.getEntriesByType('navigation')[0], f = doc.forms, i;
    if (want && abs(want).pathname === location.pathname) {
      if (bare(want) !== bare(location.href)) { return replaceTo(want); }
      if (ev.persisted || !nav || nav.transferSize === 0) { return location.reload(); }
    }
    for (i = 0; i < f.length; i++) { if (f[i].shBusy) { release(f[i]); } }
  }

  // the previous entry: inherited through replaceTo(), else the linking page
  if (!('shPrev' in st())) {
    var c = take(KEY), ref = doc.referrer, s = {}, o = st(), k;
    for (k in o) { if (o.hasOwnProperty(k)) { s[k] = o[k]; } }
    s.shPrev = c && c.to === bare(location.href) ? c.prev : (ref && same(ref) ? bare(ref) : null);
    history.replaceState(s, '', location.href);
  }
  // a page written by doc.write() runs this file again: never two handlers
  var old = W.shOn;
  if (old) {
    doc.removeEventListener('submit', old[0]);
    doc.removeEventListener('click', old[1]);
    W.removeEventListener('pageshow', old[2]);
  }
  W.shOn = [onSubmit, onClick, onShow];
  // bubble phase: after the page's own checks (they may preventDefault)
  doc.addEventListener('submit', onSubmit);
  doc.addEventListener('click', onClick);
  W.addEventListener('pageshow', onShow);
})();
