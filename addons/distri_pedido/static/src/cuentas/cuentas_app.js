/* «Cuentas»: review customer accounts, on the phone and on the PC.
 *
 * Read-only and online only. Three screens, by the URL hash:
 *   (empty)                  the debtors, highest debt first
 *   #cuenta/<id>             the account of one customer
 *   #cuenta/<id>/mov/<key>   one statement row, as a bottom sheet
 * From 1024 px (19.0.1.112.0, mockup «pc-cuentas-deudores») the debtors are
 * a table with the PC filters and the account sits beside it. The account
 * takes a free date range, «Compartir PDF» (Web Share API when the browser
 * can share files, else a download) and «Por WhatsApp» (a public link the
 * server signs). The rules (who sees which customer, who may share) live on
 * the server (models/cuentas_app.py); the only write is a comment.
 */
(function () {
  "use strict";

  var API = "/cuentas/api/";
  var PERIODS = [["1m", "Último mes"], ["3m", "3 meses"], ["all", "Todo"], ["custom", "Fechas"]];
  var AGES = [["0", "Cualquiera"], ["30", "Más de 30 días"], ["60", "Más de 60 días"], ["90", "Más de 90 días"],
    ["180", "Más de 180 días"]];
  var WIDE = window.matchMedia ? window.matchMedia("(min-width: 1024px)") : { matches: false };
  var SELLERS = [["all", "Todos"], ["italo", "Italo"], ["distrineuquen", "Distrineuquén"]];

  var S = {
    online: true,
    auth: true,
    debtors: null,      // the debtors payload
    seller: "all",
    search: "",
    showUncollectible: false,
    showSettled: false,
    showOpts: false,
    showLoad: false,    // the «Última carga» note of the list    // the small «ajustes» panel of the list
    account: null,      // {id, period, from, to, data, rows}
    zone: "",           // PC filters (19.0.1.112.0)
    minBalance: "",
    minAge: "0",
    tab: "debtors",     // PC list: debtors | uncollectible | settled
    sharing: false,
    loadingMore: false,
    line: null,         // the open row detail
    zoom: null,         // receipt or sheet photo url shown full screen
    draft: "",          // comment being written on the account
    lineDraft: "",
    busy: false,
  };

  // ------------------------------------------------------------- format

  function h(value) {
    return String(value == null ? "" : value)
      .replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;").replace(/'/g, "&#39;");
  }
  // $ 1.234.567 / $ 1.234,50 / -$ 1.200 (same as the statement PDF)
  function money(amount) {
    amount = +amount || 0;
    var sign = amount < -0.004 ? "-" : "";
    var parts = Math.abs(amount).toFixed(2).split(".");
    var int = parts[0].replace(/\B(?=(\d{3})+(?!\d))/g, ".");
    return sign + "$ " + int + (parts[1] === "00" ? "" : "," + parts[1]);
  }
  // 2026-11-09 -> 09/11/2026
  function day(iso) {
    if (!iso) return "";
    var p = iso.split("-");
    return p.length === 3 ? p[2] + "/" + p[1] + "/" + p[0] : iso;
  }
  function shortDay(iso) { return day(iso).slice(0, 5); }
  function plural(n, one, many) { return n + " " + (n === 1 ? one : many); }
  function norm(text) {
    return String(text || "").toLowerCase().normalize("NFD").replace(/[̀-ͯ]/g, "");
  }

  // ------------------------------------------------------------- server

  function request(url, options) {
    options = options || {};
    options.credentials = "same-origin";
    options.cache = "no-store";
    var ctrl = window.AbortController ? new AbortController() : null;
    var timer = ctrl ? setTimeout(function () { ctrl.abort(); }, 30000) : null;
    if (ctrl) options.signal = ctrl.signal;
    return fetch(url, options).then(function (res) {
      clearTimeout(timer);
      var type = res.headers.get("Content-Type") || "";
      if (res.redirected || type.indexOf("application/json") < 0) {
        S.auth = false;
        throw new Error("auth");
      }
      S.online = true; S.auth = true;
      return res.json();
    }, function (err) {
      clearTimeout(timer);
      S.online = false;
      throw err;
    });
  }
  function get(path) {
    return request(API + path).then(function (res) {
      if (!res.ok) throw new Error(res.error || "No se pudo leer.");
      return res.data;
    });
  }
  function post(path, body) {
    return request(API + path, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    }).then(function (res) {
      if (!res.ok) throw new Error(res.error || "No se pudo guardar.");
      return res.data;
    });
  }
  function fail(err) {
    if (err && err.message !== "auth") toast(S.online ? err.message : "Sin conexión.", true);
    render();
  }

  // ------------------------------------------------------------- routing

  function route() {
    var m = location.hash.match(/^#cuenta\/(\d+)(?:\/mov\/(.+))?$/);
    return m ? { id: +m[1], key: m[2] ? decodeURIComponent(m[2]) : null } : { id: null, key: null };
  }

  function onRoute() {
    var r = route();
    S.zoom = null;
    if (!r.id) {
      S.line = null;
      if (WIDE.matches) S.account = null;
      if (!S.debtors) loadDebtors(); else render();
      return;
    }
    // the PC shows the list beside the account
    if (WIDE.matches && !S.debtors) loadDebtors();
    if (!S.account || S.account.id !== r.id) {
      S.line = null;
      var prev = S.account || { period: "3m", from: "", to: "" };
      loadAccount(r.id, prev.period, prev.from, prev.to).then(function () {
        if (r.key) loadLine(r.id, r.key);
      });
      return;
    }
    if (r.key && (!S.line || S.line.line_key !== r.key)) loadLine(r.id, r.key);
    else if (!r.key) { S.line = null; render(); }
  }

  // Stale-while-revalidate: the last list this user saw on this phone shows
  // at once while the fresh one loads (balances take about a second on the
  // server). Keyed by user, so another login never sees it.
  var UID = (document.getElementById("app") || {}).dataset ? document.getElementById("app").dataset.uid : "";
  function storeKey(name) { return "cuentas:" + UID + ":" + name; }
  function stored(name) {
    try { return JSON.parse(localStorage.getItem(storeKey(name)) || "null"); } catch (e) { return null; }
  }
  function store(name, value) {
    try { localStorage.setItem(storeKey(name), JSON.stringify(value)); } catch (e) { /* full or blocked */ }
  }

  function debtorQuery() {
    var q = "seller=" + encodeURIComponent(S.seller) + "&settled=" + (S.showSettled ? 1 : 0);
    if (WIDE.matches) {
      q += "&zone=" + encodeURIComponent(S.zone) + "&min_balance=" + encodeURIComponent(S.minBalance || 0) +
        "&min_age=" + encodeURIComponent(S.minAge || 0);
    }
    return q;
  }

  function loadDebtors() {
    var query = debtorQuery();
    var key = "debtors:" + query;
    var cached = stored(key);
    var same = S.debtors && S.debtors._query === query;
    if (cached && !same) S.debtors = cached;
    else if (!cached && !same && S.debtors && S.debtors.seller !== S.seller) S.debtors = null;
    S.refreshing = true;
    render();
    return get("debtors?" + query)
      .then(function (data) {
        data._query = query;
        S.debtors = data;
        store(key, data);
      }).catch(fail).then(function () { S.refreshing = false; render(); });
  }

  // accounts already opened in this visit show at once and refresh behind
  var accountCache = {};

  function accountQuery(acc) {
    var q = "id=" + acc.id + "&period=" + acc.period;
    if (acc.period === "custom") {
      q += "&date_from=" + encodeURIComponent(acc.from || "") + "&date_to=" + encodeURIComponent(acc.to || "");
    }
    return q;
  }

  function loadAccount(id, period, from, to) {
    var acc = { id: id, period: period, from: from || "", to: to || "" };
    var query = accountQuery(acc);
    var cached = accountCache[query];
    acc.data = cached || null;
    acc.rows = cached ? cached.rows : [];
    S.account = acc;
    render();
    return get("account?" + query).then(function (data) {
      accountCache[query] = data;
      if (S.account !== acc) return;
      acc.data = data;
      acc.rows = data.rows;
      render();
    }).catch(function (err) {
      if (S.account && S.account.id === id && err.message !== "auth") S.account.error = err.message;
      fail(err);
    });
  }

  function loadMore() {
    var acc = S.account;
    if (!acc || !acc.data || S.loadingMore) return;
    S.loadingMore = true;
    render();
    get("account?" + accountQuery(acc) + "&offset=" + acc.rows.length)
      .then(function (data) {
        acc.rows = acc.rows.concat(data.rows);
        acc.data.more = data.more;
      }).catch(fail).then(function () { S.loadingMore = false; render(); });
  }

  function loadLine(id, key) {
    S.line = { line_key: key, loading: true };
    S.lineDraft = "";
    render();
    return get("line?id=" + id + "&key=" + encodeURIComponent(key)).then(function (data) {
      S.line = data;
      render();
    }).catch(function (err) { S.line = null; fail(err); });
  }

  // ------------------------------------------------------------- render

  var app = document.getElementById("app");
  var sheetRoot = document.getElementById("sheet-root");

  function toast(text, isError) {
    var el = document.getElementById("toast");
    el.textContent = text;
    el.className = isError ? "err" : "";
    el.hidden = false;
    clearTimeout(toast.t);
    toast.t = setTimeout(function () { el.hidden = true; }, 4000);
  }

  function banner() {
    if (!S.auth) {
      return '<div class="banner err">Tu sesión venció. <a href="/web/login?redirect=/cuentas">Entrar de nuevo</a></div>';
    }
    if (!S.online) return '<div class="banner err">Sin conexión. Conectate a internet y probá de nuevo.</div>';
    return "";
  }

  // replacing the page blurs a focused field, whose «change» may render
  // again from inside this render: that nested call is skipped
  function render() {
    if (render.busy) return;
    render.busy = true;
    try { paint(); } finally { render.busy = false; }
  }

  function paint() {
    var r = route();
    var focus = document.activeElement && document.activeElement.id;
    var caret = focus && document.activeElement.selectionStart;
    var y = window.scrollY;
    var keepScroll = WIDE.matches || render.lastId === (r.id || 0);
    render.lastId = r.id || 0;
    var side = document.querySelector(".dacc");
    var sideY = side && render.lastSide === (r.id || 0) ? side.scrollTop : 0;
    var csRows = document.getElementById("cs-rows");
    var csKeep = csRows && S.line && csRows.dataset.line === S.line.line_key ? csRows.scrollTop : null;
    document.body.classList.toggle("wide", !!WIDE.matches);
    app.innerHTML = WIDE.matches ? viewDesk() : r.id ? viewAccount() : viewDebtors();
    side = document.querySelector(".dacc");
    if (side) side.scrollTop = sideY;
    render.lastSide = r.id || 0;
    sheetRoot.innerHTML = (S.line ? viewLine() : "") + (S.zoom ? viewZoom() : "");
    csRows = document.getElementById("cs-rows");
    if (csRows) {
      csRows.dataset.line = S.line.line_key;
      var hit = document.getElementById("cs-hit");
      if (csKeep != null) csRows.scrollTop = csKeep;
      else if (hit) {
        csRows.scrollTop = Math.max(0, hit.offsetTop - (csRows.clientHeight - hit.offsetHeight) / 2);
        if (hit.scrollIntoView) hit.scrollIntoView({ block: "nearest" });
      }
    }
    document.body.style.overflow = S.line || S.zoom ? "hidden" : "";
    if (focus) {
      var el = document.getElementById(focus);
      if (el) {
        el.focus();
        try { if (caret != null) el.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
      }
    }
    window.scrollTo(0, keepScroll ? y : 0);
  }

  // ---- debtors

  function visibleDebtors(list) {
    var q = norm(S.search).trim();
    if (!q) return list;
    var digits = q.replace(/\D/g, "");
    return list.filter(function (d) {
      if (norm(d.name).indexOf(q) >= 0 || norm(d.ref).indexOf(q) >= 0) return true;
      return digits.length >= 3 && String(d.phone || "").replace(/\D/g, "").indexOf(digits) >= 0;
    });
  }

  function debtorRow(d) {
    var facts = [];
    if (d.zone) facts.push(h(d.zone));
    if (d.ref) facts.push(h(d.ref));
    var last = [];
    last.push(d.last_payment_date ? "Último pago " + day(d.last_payment_date) + " · " +
      money(d.last_payment_amount) : "Sin pagos");
    if (d.last_invoice_date) last.push("Última factura " + day(d.last_invoice_date));
    var badges = "";
    if (d.candidate_count) {
      badges += '<span class="pill warn">' + plural(d.candidate_count, "pago a confirmar", "pagos a confirmar") + "</span>";
    }
    if (d.comment_count) {
      badges += '<span class="pill info">' + plural(d.comment_count, "comentario", "comentarios") + "</span>";
    }
    return '<a class="debtor" href="#cuenta/' + d.id + '">' +
      '<div class="rk">' + d.rank + "</div>" +
      '<div class="who"><b>' + h(d.name) + "</b>" +
      (facts.length ? '<span class="d">' + facts.join(" · ") + "</span>" : "") +
      '<span class="d">' + last.join(" · ") + "</span></div>" +
      '<div class="amt"><b class="num' + (d.balance < 0 ? " credit" : "") + '">' + money(d.balance) + "</b>" +
      (badges ? '<div class="badges">' + badges + "</div>" : "") + "</div></a>";
  }

  function viewDebtors() {
    var D = S.debtors;
    var load = D && D.last_load;
    var head = '<div class="sticky"><div class="head compact"><div class="row"><div class="kicker">Cuentas de clientes</div>' +
      '<span class="headbtns">' +
      (load ? '<button type="button" id="lastload" class="loadbtn' + (S.showLoad ? " on" : "") + '" aria-expanded="' +
        (S.showLoad ? "true" : "false") + '">Última carga</button>' : "") +
      // 19.0.1.92.0: register a cash payment of any customer (controllers/cobro.py)
      '<a class="loadbtn" href="/cobro">Cobro en efectivo</a>' +
      // the only way out for a seller locked to this app (distri_pedido.vendedor_solo_cuentas)
      '<a class="loadbtn logout" href="/web/session/logout?redirect=%2Fweb%2Flogin">Salir</a>' +
      "</span></div>";
    if (load && S.showLoad) {
      head += '<div class="loadinfo">Comprobantes del chat cargados hasta el <b>' + h(load.receipts_until) + "</b>" +
        (load.import_file ? '<span>Zip «' + h(load.import_file) + "», importado el " + h(load.imported_at) +
          (load.imported_by ? " por " + h(load.imported_by) : "") + "</span>" : "") + "</div>";
    }
    if (D) {
      head += '<div class="total num">' + money(D.total_debt) + "</div>" +
        '<div class="sub">' + plural(D.debtor_count, "cliente debe", "clientes deben") +
        " · " + h(D.user) +
        (S.refreshing ? ' · <span class="upd">actualizando…</span>' : "") + "</div>";
    } else {
      head += "<h1>Deudores</h1>";
    }
    // a seller never sees the «incobrable» state (models/cuentas_app.py app_debtors)
    var seeUnc = !!(D && D.can_see_uncollectible);
    var custom = S.seller !== "all" || (seeUnc && S.showUncollectible) || S.showSettled;
    head += "</div>" + banner() + '<div class="tools"><div class="searchrow">' +
      '<input id="q" class="inp" type="search" inputmode="search" autocomplete="off" ' +
      'placeholder="Buscar nombre, ref. o teléfono" value="' + h(S.search) + '"/>' +
      '<button type="button" id="opts" class="iconbtn' + (S.showOpts ? " on" : "") + (custom ? " dot" : "") +
      '" aria-label="Ajustes de la lista" aria-expanded="' + (S.showOpts ? "true" : "false") + '">' +
      '<svg viewBox="0 0 24 24" width="20" height="20" aria-hidden="true"><path fill="currentColor" d="M3 6h11.2a3 3 0 0 1 5.6 0H21v2h-1.2a3 3 0 0 1-5.6 0H3zm14 1a1 1 0 1 0 0-.01zM3 16h3.2a3 3 0 0 1 5.6 0H21v2h-9.2a3 3 0 0 1-5.6 0H3zm6 1a1 1 0 1 0 0-.01z"/></svg>' +
      "</button></div>";
    if (S.showOpts) {
      head += '<div class="opts">';
      if (D && D.can_filter_seller) {
        head += '<div class="seg">' + SELLERS.map(function (s) {
          return '<button type="button" data-seller="' + s[0] + '" class="' + (S.seller === s[0] ? "on" : "") +
            '">' + s[1] + "</button>";
        }).join("") + "</div>";
      }
      head += '<div class="toggles">' +
        (seeUnc ? '<label><input type="checkbox" id="t-unc"' + (S.showUncollectible ? " checked" : "") +
          "/> Incobrables aparte</label>" : "") +
        '<label><input type="checkbox" id="t-set"' + (S.showSettled ? " checked" : "") +
        "/> Saldos a favor o en cero</label></div></div>";
    }
    head += "</div></div>";
    if (!D) return head + '<div class="body"><div class="cargando">Cargando…</div></div>';

    var out = [];
    var debtors = visibleDebtors(D.debtors);
    out.push(debtors.length ? debtors.map(debtorRow).join("") :
      '<div class="empty">' + (S.search ? "Ningún deudor coincide con la búsqueda." : "No hay deudores.") + "</div>");
    if (seeUnc && S.showUncollectible) {
      var unc = visibleDebtors(D.uncollectible);
      out.push('<div class="section-title"><span>Deudas incobrables (aparte)</span><span class="num">' +
        money(D.uncollectible_total) + "</span></div>");
      out.push(unc.length ? unc.map(debtorRow).join("") : '<div class="empty">Sin incobrables.</div>');
    }
    if (S.showSettled) {
      var settled = visibleDebtors(D.settled);
      out.push('<div class="section-title"><span>Saldo a favor o en cero</span><span>' + settled.length + "</span></div>");
      out.push(settled.length ? settled.map(debtorRow).join("") : '<div class="empty">Ninguno.</div>');
    }
    return head + '<div class="body">' + out.join("") + "</div>";
  }

  // ---- account

  function commentView(c) {
    return '<div class="comment' + (c.resolved ? " resolved" : "") + '">' +
      (c.line_label && !S.line ? '<span class="ref">' + h(c.line_label) + "</span>" : "") +
      '<div class="txt">' + h(c.body) + "</div>" +
      '<div class="meta"><span>' + h(c.author) + " · " + h(c.date) + (c.resolved ? " · resuelto" : "") + "</span>" +
      (c.can_delete ? '<button type="button" class="link red" data-delete="' + c.id + '">Borrar</button>' : "") +
      "</div></div>";
  }

  function composeView(id, value) {
    return '<div class="compose"><textarea id="' + id + '" class="inp" maxlength="2000" ' +
      'placeholder="Escribí un comentario (lo ven administración y el vendedor)">' + h(value) + "</textarea>" +
      '<button type="button" class="btn outline" data-send="' + id + '"' + (S.busy ? " disabled" : "") +
      ">Agregar comentario</button></div>";
  }

  function stmtRow(row) {
    var cls = "srow" + (row.provisional ? " prov" : row.owed ? " owed" : "");
    var amount = row.debit ? '<span class="m num">' + money(row.debit) + "</span>" :
      '<span class="m num cr">-' + money(row.credit) + "</span>";
    var title = h(row.doc_type) + (row.number ? " " + h(row.number) : "");
    var tags = "";
    if (row.provisional) tags += ' <span class="pill warn">a controlar</span>';
    if (row.owed) tags += ' <span class="pill err">debe ' + money(row.owed) + "</span>";
    if (row.review_note) tags += ' <span class="pill info" title="' + h(row.review_note) + '">nota de revisión</span>';
    if (row.confirmed) tags += ' <span class="ok-check" style="color:#198754;font-weight:bold" title="Pago confirmado" role="img" aria-label="Pago confirmado">✓</span>';
    if (row.card_id) tags += ' <span class="pill ok">MP</span>';
    if (row.comment_count) tags += ' <span class="pill info">' + plural(row.comment_count, "comentario", "comentarios") + "</span>";
    var concept = [row.remito ? "Remito " + row.remito : "", row.concept].filter(Boolean).join(" · ");
    return '<button type="button" class="' + cls + '" data-line="' + h(row.line_key) + '">' +
      '<div class="l"><span class="t"><span class="dt">' + day(row.date) + "</span>" + title + tags + "</span>" +
      '<span class="c">' + h(concept || row.source_label) + "</span></div>" +
      '<div class="r">' + amount + '<span class="b num">Saldo ' + money(row.balance) + "</span></div></button>";
  }

  // the period: quick buttons and, for «Fechas», the two dates
  function periodView(acc) {
    var seg = '<div class="seg">' + PERIODS.map(function (x) {
      return '<button type="button" data-period="' + x[0] + '" class="' + (acc.period === x[0] ? "on" : "") + '">' +
        x[1] + "</button>";
    }).join("") + "</div>";
    if (acc.period !== "custom") return seg;
    return seg + '<div class="range">' +
      '<label class="fld">Desde<input id="r-from" class="inp" type="date" value="' + h(acc.from) + '"/></label>' +
      '<label class="fld">Hasta<input id="r-to" class="inp" type="date" value="' + h(acc.to) + '"/></label></div>';
  }

  var ICON_PDF = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/>' +
    '<path d="M14 3v5h5M12 11v6M9 14l3 3 3-3"/></svg>';
  var ICON_WA = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M21 12a8.5 8.5 0 0 1-12.6 7.4L3 21l1.6-5.2A8.5 8.5 0 1 1 21 12z"/></svg>';
  var ICON_PRINT = '<svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M7 9V3h10v6M7 17H5a2 2 0 0 1-2-2v-4a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v4a2 2 0 0 1-2 2h-2M7 14h10v7H7z"/></svg>';

  // «Compartir PDF» / «Imprimir» and «Por WhatsApp»: what the server allows
  function shareView(d, wide) {
    var sh = d.share || {};
    var pdf = sh.pdf ?
      '<button type="button" class="btn ghost" id="share-pdf"' + (S.sharing ? " disabled" : "") + ">" +
        (wide ? ICON_PRINT + "Imprimir / PDF" : ICON_PDF + (S.sharing ? "Preparando…" : "Compartir PDF")) + "</button>" :
      '<button type="button" class="btn ghost" disabled>' + ICON_PDF + (wide ? "Imprimir / PDF" : "Compartir PDF") + "</button>";
    var wa = sh.whatsapp_url ?
      '<a class="btn wa" id="share-wa" href="' + h(sh.whatsapp_url) + '" target="_blank" rel="noopener">' + ICON_WA +
        (wide ? "Enviar por WhatsApp" : "Por WhatsApp") + "</a>" :
      '<button type="button" class="btn wa" disabled>' + ICON_WA + (wide ? "Enviar por WhatsApp" : "Por WhatsApp") + "</button>";
    var notes = [];
    if (!sh.pdf && sh.pdf_reason) notes.push(h(sh.pdf_reason));
    else if (!sh.whatsapp_url && sh.whatsapp_reason) notes.push(h(sh.whatsapp_reason));
    else if (sh.expires) notes.push("El link de WhatsApp muestra este período y vence el " + h(sh.expires) + ".");
    return '<div class="share">' + pdf + wa + "</div>" +
      (notes.length ? '<div class="small muted share-note">' + notes.join(" ") + "</div>" : "");
  }

  function candidatesView(d) {
    if (!d.candidates.length) return "";
    return '<div class="label">Pagos a confirmar · falta el comprobante</div><div class="card cand">' +
      d.candidates.map(function (c) {
        return '<div class="ci"><div class="ci-top"><b>' + day(c.date) + " · " + h(c.name) + '</b><b class="num">' +
          money(c.amount) + '</b></div><div class="chips"><span class="pill warn">' +
          (c.has_receipt ? "Llegó el comprobante" : "Sin comprobante") + '</span><span class="pill">no resta</span></div>' +
          (c.note ? '<span class="small muted">' + h(c.note) + "</span>" : "") + "</div>";
      }).join("") +
      '<span class="small muted">Se aprueba cuando el cliente manda el comprobante y coincide con el reporte de Mercado Pago. ' +
      "Saldo si se aprueba" + (d.candidates.length === 1 ? "" : "n") + ": <b class=\"num\">" + money(d.whatif_balance) + "</b>.</span></div>";
  }

  function receiptsView(d) {
    var pr = d.pending_receipts;
    if (!pr || !pr.items.length) return "";
    return '<div class="label">Comprobantes pendientes</div><div class="card cand">' +
      pr.items.map(function (r) {
        return '<div class="ci"><div class="ci-top"><b>' + h(r.order) + " · " + h(r.kind) + '</b><b class="num">' +
          money(r.amount) + '</b></div><div class="chips"><span class="pill ' + (r.overdue ? "err" : "warn") + '">' +
          h(r.status) + "</span>" + (r.delivered ? '<span class="pill">Entregado el ' + h(r.delivered) + "</span>" : "") +
          "</div></div>";
      }).join("") +
      (pr.claim_url ? '<a class="btn ghost" href="' + h(pr.claim_url) + '" target="_blank" rel="noopener">' + ICON_WA +
        "Reclamar por WhatsApp</a>" : "") + "</div>";
  }

  function commentsView(d) {
    var out = '<div class="label">Comentarios de la cuenta</div><div class="comments">' +
      d.comments.map(commentView).join("") + composeView("c-account", S.draft) + "</div>";
    if (d.line_comments.length) {
      out += '<details class="block"><summary class="link">' +
        plural(d.line_comments.length, "comentario en movimientos", "comentarios en movimientos") +
        '</summary><div class="comments">' + d.line_comments.map(commentView).join("") + "</div></details>";
    }
    return out;
  }

  function openingText(d) {
    return d.date_from ? "Saldo anterior al " + day(d.date_from) : "Saldo inicial";
  }

  function totalsView(d) {
    if (d.period === "all" && !d.date_to) return "";
    return '<div class="totals"><span>Debe del período <b class="num">' + money(d.debit) + "</b></span>" +
      "<span>Haber <b class=\"num\">" + money(d.credit) + "</b></span>" +
      "<span>Saldo " + (d.date_to ? "al " + day(d.date_to) : "al cierre") + ' <b class="num">' + money(d.closing) + "</b></span></div>";
  }

  // the statement as cards (phone) or as a table (PC); newest first
  function statementView(acc, wide) {
    var d = acc.data;
    var tail = "";
    if (d.more) {
      tail = '<button type="button" class="btn outline more" id="more"' + (S.loadingMore ? " disabled" : "") + ">" +
        (S.loadingMore ? "Cargando…" : "Ver más movimientos (" + (d.row_count - acc.rows.length) + ")") + "</button>";
    }
    if (wide) {
      var trs = acc.rows.map(function (row) {
        var concept = [row.remito ? "Remito " + row.remito : "", row.concept].filter(Boolean).join(" · ");
        var tags = "";
        if (row.provisional) tags += ' <span class="pill warn">a controlar</span>';
        if (row.confirmed) tags += ' <span class="ok-check" style="color:#198754;font-weight:bold" title="Pago confirmado" role="img" aria-label="Pago confirmado">✓</span>';
        if (row.card_id) tags += ' <span class="pill ok">MP</span>';
        if (row.comment_count) tags += ' <span class="pill info">' + row.comment_count + "</span>";
        return '<tr class="' + (row.provisional ? "prov" : row.owed ? "owed" : "") + '" data-line="' + h(row.line_key) + '">' +
          '<td class="num">' + day(row.date) + "</td><td><b>" + h(row.doc_type) + (row.number ? " " + h(row.number) : "") + "</b>" + tags +
          '<div class="c">' + h(concept || row.source_label) + "</div>" +
          (row.owed ? '<div class="owedtxt">debe ' + money(row.owed) + "</div>" : "") + "</td>" +
          '<td class="r num">' + (row.debit ? money(row.debit) : "") + '</td><td class="r num cr">' +
          (row.credit ? money(row.credit) : "") + '</td><td class="r num">' + money(row.balance) + "</td></tr>";
      }).join("");
      if (!trs) trs = '<tr><td colspan="5" class="empty">Sin movimientos en el período.</td></tr>';
      if (!d.more) {
        trs += '<tr class="opening"><td colspan="4" class="muted">' + openingText(d) + '</td><td class="r num">' +
          money(d.opening) + "</td></tr>";
      }
      return '<div class="tscroll"><table class="tbl stbl"><thead><tr><th>Fecha</th><th>Movimiento</th><th class="r">Debe</th>' +
        '<th class="r">Haber</th><th class="r">Saldo</th></tr></thead><tbody>' + trs + "</tbody></table></div>" + tail;
    }
    var rows = acc.rows.length ? acc.rows.map(stmtRow).join("") : '<div class="empty">Sin movimientos en el período.</div>';
    var opening = d.more ? "" :
      '<div class="srow opening"><div class="l"><span class="t">' + openingText(d) + "</span>" +
      '<span class="c">' + (d.date_from ? "Todo lo anterior al período" : "Antes del primer movimiento") +
      '</span></div><div class="r"><span class="m num">' + money(d.opening) + "</span></div></div>";
    return '<div class="stmt">' + rows + (tail ? "" : opening) + "</div>" + tail;
  }

  function accountFacts(p) {
    var facts = [];
    if (p.ref) facts.push("Ref. " + h(p.ref));
    if (p.address) facts.push(h(p.address));
    if (p.zone) facts.push("Zona " + h(p.zone));
    if (p.seller) facts.push("Vendedor: " + h(p.seller));
    return facts;
  }

  function contactLinks(p, cls) {
    if (!p.phone) return "";
    return (p.whatsapp ? '<a class="' + cls + '" href="https://wa.me/' + h(p.whatsapp) + '" target="_blank" rel="noopener">' +
      ICON_WA + "WhatsApp</a>" : "") +
      '<a class="' + cls + '" href="tel:' + h(p.phone.replace(/[^\d+]/g, "")) + '">Llamar ' + h(p.phone) + "</a>";
  }

  function balanceWord(balance) {
    return balance > 0.005 ? "debe" : balance < -0.005 ? "a favor" : "al día";
  }

  // everything below the header: the same on the phone and on the PC
  function accountBody(acc, wide) {
    var d = acc.data;
    var p = d.partner;
    var boxes = [];
    if (p.uncollectible) {
      boxes.push('<div class="box wide warn"><span>Incobrable</span><b>' + h(p.uncollectible_note || "Marcado como incobrable") + "</b></div>");
    }
    if (d.provisional) {
      boxes.push('<div class="box wide warn"><span>Incluye devoluciones informadas (' + h(d.provisional_note) +
        ')</span><b class="num">' + money(d.provisional) + "</b></div>");
    }
    if (d.candidates.length) {
      boxes.push('<div class="box"><span>' + plural(d.candidates.length, "pago a confirmar", "pagos a confirmar") +
        ' · no restan</span><b class="num">' + money(d.candidate_amount) + "</b></div>" +
        '<div class="box"><span>Saldo si se aprueba' + (d.candidates.length === 1 ? "" : "n") + '</span><b class="num">' +
        money(d.whatif_balance) + "</b></div>");
    }
    var summary = boxes.length ? '<div class="summary">' + boxes.join("") + "</div>" : "";
    var red = d.owed_count ? '<div class="redbox">Facturas adeudadas · ' + d.owed_count + " · en rojo, por <b class=\"num\">" +
      money(d.owed_total) + "</b>. Cada pago cancela primero la factura que concilia o nombra; el resto, de la más vieja a la más nueva.</div>" : "";
    return summary +
      '<div class="label">Período</div>' + periodView(acc) + shareView(d, wide) + red + totalsView(d) +
      '<div class="label">Movimientos · lo más nuevo arriba</div>' + statementView(acc, wide) +
      candidatesView(d) + receiptsView(d) + commentsView(d);
  }

  function viewAccount() {
    var acc = S.account;
    var d = acc && acc.data;
    var p = d && d.partner;
    var head = '<div class="sticky"><div class="head compact">' +
      '<a class="back" href="#">‹ Deudores</a>';
    if (acc && acc.error) {
      return head + "<h1>Cuenta</h1></div>" + banner() + '</div><div class="body"><div class="denied">' +
        '<div class="banner err">' + h(acc.error) + '</div><p><a href="#">Volver a deudores</a></p></div></div>';
    }
    if (!p) {
      return head + "<h1>Cuenta</h1></div>" + banner() + '</div><div class="body"><div class="cargando">Cargando…</div></div>';
    }
    var facts = accountFacts(p);
    head += '<span class="kicker">Cuentas de clientes · Estado de cuenta</span><h1>' + h(p.name) + "</h1>" +
      '<div class="row"><span class="total num">' + money(d.balance) + "</span>" +
      '<span class="sub ' + (d.balance > 0.005 ? "owes" : "") + '">' + balanceWord(d.balance) + "</span></div>" +
      (facts.length ? '<p class="sub facts-line">' + facts.join(" · ") + "</p>" : "") +
      (p.phone ? '<div class="toplinks">' + contactLinks(p, "toplnk") + "</div>" : "") +
      "</div>" + banner() + "</div>";
    return head + '<div class="body">' + accountBody(acc, false) + "</div>";
  }

  // ---- PC (19.0.1.112.0): debtors table with filters, the account beside

  function deskAccount() {
    var acc = S.account;
    if (!acc) {
      return '<aside class="panel dacc empty-side"><div class="label">Estado de cuenta</div>' +
        '<p class="muted">Elegí un cliente de la lista para ver su estado de cuenta.</p></aside>';
    }
    var d = acc.data;
    var p = d && d.partner;
    if (acc.error) {
      return '<aside class="panel dacc"><div class="banner err">' + h(acc.error) + "</div></aside>";
    }
    if (!p) return '<aside class="panel dacc"><div class="cargando">Cargando…</div></aside>';
    var facts = accountFacts(p);
    return '<aside class="panel dacc" aria-label="Estado de cuenta"><div class="dacc-head"><div class="who">' +
      '<span class="label">Estado de cuenta</span><h2>' + h(p.name) + "</h2>" +
      (facts.length ? '<span class="muted small">' + facts.join(" · ") + "</span>" : "") +
      (p.phone ? '<div class="contact">' + contactLinks(p, "clink") + "</div>" : "") + "</div>" +
      '<div class="bal"><span class="muted small">Saldo actual</span><b class="num">' + money(d.balance) + "</b>" +
      '<span class="small ' + (d.balance > 0.005 ? "owes" : "muted") + '">' + balanceWord(d.balance) + "</span></div></div>" +
      accountBody(acc, true) + "</aside>";
  }

  function deskRow(d, selected) {
    var oldest = d.oldest_owed_date ? shortDay(d.oldest_owed_date) + ' <span class="muted">· ' +
      plural(d.oldest_owed_days, "día", "días") + "</span>" : '<span class="muted">—</span>';
    return '<tr class="' + (selected ? "on" : "") + '" data-pick="' + d.id + '">' +
      '<td class="num muted">' + d.rank + "</td>" +
      '<td><a href="#cuenta/' + d.id + '"><b>' + h(d.name) + "</b></a>" + (d.ref || d.comment_count ? '<div class="muted small">' + h(d.ref) +
        (d.comment_count ? ' <span class="pill info">' + plural(d.comment_count, "comentario", "comentarios") + "</span>" : "") + "</div>" : "") + "</td>" +
      "<td>" + h(d.seller_label) + "</td><td>" + h(d.zone) + "</td>" +
      '<td class="r num strong' + (d.balance < 0 ? " cr" : "") + '">' + money(d.balance) + "</td>" +
      '<td class="num">' + oldest + "</td>" +
      '<td class="num">' + (d.last_payment_date ? shortDay(d.last_payment_date) + " · " + money(d.last_payment_amount) : '<span class="muted">Sin pagos</span>') +
      (d.last_invoice_date ? '<div class="muted small">Factura ' + shortDay(d.last_invoice_date) + " · " + money(d.last_invoice_amount) + "</div>" : "") + "</td>" +
      '<td class="r num">' + (d.candidate_amount ? money(d.candidate_amount) : '<span class="muted">—</span>') + "</td></tr>";
  }

  function viewDesk() {
    var D = S.debtors;
    var r = route();
    var seeUnc = !!(D && D.can_see_uncollectible);
    var today = new Date();
    var head = '<header class="dbar"><span class="logo">DN</span><span class="dtitle">Deudores y estado de cuenta</span>' +
      '<span class="dsub">' + (D ? "Al " + String(today.getDate()).padStart(2, "0") + "/" + String(today.getMonth() + 1).padStart(2, "0") +
        " · " + h(D.user) : "") + (S.refreshing ? ' · <span class="upd">actualizando…</span>' : "") + "</span>" +
      '<span class="grow"></span><a class="loadbtn" href="/cobro">Cobro en efectivo</a>' +
      '<a class="loadbtn logout" href="/web/session/logout?redirect=%2Fweb%2Flogin">Salir</a></header>';
    var f = '<div class="dfilters"><div class="frow">';
    if (D && D.can_filter_seller) {
      f += '<label class="fld">Vendedor<select id="f-seller" class="inp">' + SELLERS.map(function (s) {
        return '<option value="' + s[0] + '"' + (S.seller === s[0] ? " selected" : "") + ">" + (s[0] === "all" ? "Todos" : "Clientes de " + s[1]) + "</option>";
      }).join("") + "</select></label>";
    }
    var zones = (D && D.zones) || [];
    f += '<label class="fld">Zona<select id="f-zone" class="inp"><option value="">Todas</option>' + zones.map(function (z) {
      return '<option value="' + h(z) + '"' + (S.zone === z ? " selected" : "") + ">" + h(z) + "</option>";
    }).join("") + "</select></label>" +
      '<label class="fld">Deuda mínima<input id="f-min" class="inp num" type="number" min="0" step="1000" inputmode="numeric" placeholder="$ 0" value="' +
      h(S.minBalance) + '"/></label>' +
      '<label class="fld">Deuda más vieja<select id="f-age" class="inp">' + AGES.map(function (a) {
        return '<option value="' + a[0] + '"' + (S.minAge === a[0] ? " selected" : "") + ">" + a[1] + "</option>";
      }).join("") + "</select></label>" +
      '<label class="fld grow">Buscar<input id="q" class="inp" type="search" autocomplete="off" placeholder="Nombre, ref. o teléfono" value="' +
      h(S.search) + '"/></label></div><div class="frow btns">' +
      '<button type="button" class="btn ghost sm" id="refresh">Actualizar</button>' +
      (D && D.report_url ? '<a class="btn ghost sm" href="' + h(D.report_url) + '" target="_blank" rel="noopener">PDF y Excel (Odoo)</a>' : "") +
      "</div></div>";
    if (!D) return '<div class="desk">' + head + banner() + '<main class="dwrap">' + f + '<div class="cargando">Cargando…</div></main></div>';
    var kpis = '<div class="kpis"><div class="kpi"><span>Clientes que deben</span><b class="num">' + D.debtor_count + "</b></div>" +
      '<div class="kpi"><span>Deuda total</span><b class="num">' + money(D.total_debt) + "</b></div>" +
      '<div class="kpi"><span>Pagos a confirmar</span><b class="num">' + money(D.total_candidates || 0) + "</b></div>" +
      (seeUnc ? '<div class="kpi err"><span>Deuda incobrable (aparte)</span><b class="num">' + money(D.uncollectible_total) + "</b></div>" : "") +
      "</div>";
    var lists = { debtors: D.debtors, uncollectible: D.uncollectible, settled: D.settled };
    var tab = S.tab === "uncollectible" && !seeUnc ? "debtors" : S.tab;
    var tabs = '<nav class="tabs" aria-label="Listas"><button type="button" data-tab="debtors" class="' + (tab === "debtors" ? "on" : "") +
      '">Deudores · ' + D.debtors.length + "</button>" +
      (seeUnc ? '<button type="button" data-tab="uncollectible" class="' + (tab === "uncollectible" ? "on" : "") +
        '">Deudas incobrables · ' + D.uncollectible.length + "</button>" : "") +
      '<button type="button" data-tab="settled" class="' + (tab === "settled" ? "on" : "") + '">Saldo a favor o en cero</button></nav>';
    var list = tab === "settled" && !D.settled_loaded ? null : visibleDebtors(lists[tab] || []);
    var body = list === null ? '<div class="cargando">Cargando…</div>' :
      '<div class="tscroll"><table class="tbl dtbl"><thead><tr><th>Puesto</th><th>Cliente</th><th>Vendedor</th><th>Zona</th>' +
      '<th class="r">Deuda</th><th>Deuda más vieja</th><th>Último pago y factura</th><th class="r">A confirmar</th></tr></thead><tbody>' +
      (list.length ? list.map(function (d) { return deskRow(d, d.id === r.id); }).join("") :
        '<tr><td colspan="8" class="empty">' + (S.search ? "Ningún cliente coincide con la búsqueda." : "Ningún cliente con estos filtros.") + "</td></tr>") +
      "</tbody></table></div>" +
      '<span class="muted small">' + (list.length === (lists[tab] || []).length ? "Mostrando " + list.length : "Mostrando " + list.length + " de " + (lists[tab] || []).length) +
      " · ordenado por deuda</span>";
    return '<div class="desk">' + head + banner() + '<main class="dwrap">' + f + kpis +
      '<div class="dcols"><section class="panel dlist">' + tabs + body + "</section>" + deskAccount() + "</div></main></div>";
  }

  // ---- line detail

  function kv(pairs) {
    var items = pairs.filter(function (x) { return x[1] !== "" && x[1] != null && x[1] !== false; });
    return '<dl class="kv">' + items.map(function (x) {
      return "<dt>" + h(x[0]) + "</dt><dd>" + x[1] + "</dd>";
    }).join("") + "</dl>";
  }

  function viewLine() {
    var L = S.line;
    var body;
    if (L.loading) {
      body = '<div class="cargando">Cargando…</div>';
    } else {
      var parts = [];
      parts.push(kv([
        ["Fecha", day(L.date)],
        ["Tipo", h(L.doc_type)],
        ["Comprobante", h(L.number)],
        ["Remito", h(L.remito)],
        ["Concepto", h(L.concept)],
        ["Origen", h(L.source_label)],
        ["Debe", L.debit ? '<span class="num">' + money(L.debit) + "</span>" : ""],
        ["Haber", L.credit ? '<span class="num">' + money(L.credit) + "</span>" : ""],
        ["Saldo", '<span class="num">' + money(L.balance) + "</span>"],
        ["Diario", h(L.journal || "")],
        ["Estado", L.provisional ? '<span class="pill warn">a controlar</span>' : ""],
      ]));
      if (L.owed || L.review_note) {
        parts.push('<div class="block"><div class="label">' + (L.owed ? "Adeudada" : "Revisión de la cuenta") + "</div>" + kv([
          ["Debe todavía", L.owed ? '<span class="num">' + money(L.owed) + "</span>" : ""],
          ["Nota de la revisión", h(L.review_note)],
        ]) + "</div>");
      }
      if (L.card) {
        var c = L.card;
        var img = c.has_receipt ? "/cuentas/api/receipt?id=" + S.account.id + "&card=" + c.id : "";
        parts.push('<div class="block"><div class="label">Tarjeta de Mercado Pago</div>' + kv([
          ["Cobro", h(c.name)],
          ["Operación MP", h(c.operation)],
          ["Fecha", h(c.date)],
          ["Importe", '<span class="num">' + money(c.amount) + "</span>"],
          ["Pagó", h(c.payer_name)],
          ["Cliente", h(c.partner)],
          ["Estado", h(c.state)],
        ]) + (img ? (c.receipt_is_image ?
          '<img class="receipt" alt="Comprobante" loading="lazy" data-zoom="' + h(img) + '" src="' + h(img) + '"/>' :
          '<a class="btn outline" href="' + h(img) + '" target="_blank" rel="noopener">Ver comprobante</a>') :
          '<div class="note">Sin comprobante adjunto.</div>') + "</div>");
      }
      if (L.cash) parts.push(cashView(L.cash));
      if (L.online) {
        parts.push('<div class="block"><div class="label">Historia de Odoo Online</div>' + kv([
          ["Comprobante", h(L.online.comprobante)],
          ["Concepto", h(L.online.concepto)],
          ["Tipo", h(L.online.tipo)],
          ["Id en Online", L.online.origen_id ? String(L.online.origen_id) : ""],
        ]) + "</div>");
      }
      if (L.route_return) {
        var r = L.route_return;
        parts.push('<div class="block"><div class="label">Devolución informada</div>' + kv([
          ["Pedido", h(r.order)], ["Ruta", h(r.route)], ["Producto", h(r.product)],
          ["Cantidad", String(r.quantity)], ["Motivo", h(r.reason)],
        ]) + "</div>");
      }
      parts.push('<div class="label">Comentarios del movimiento</div><div class="comments">' +
        L.comments.map(commentView).join("") + composeView("c-line", S.lineDraft) + "</div>");
      body = parts.join("");
    }
    var title = L.loading ? "Movimiento" : h(L.doc_type) + " · " + day(L.date);
    return '<div class="overlay" data-close="1"><div class="sheet" role="dialog" aria-modal="true">' +
      '<div class="grab"><b>' + title + '</b><button type="button" class="close" data-close="1" aria-label="Cerrar">×</button></div>' +
      '<div class="inner">' + body + "</div></div></div>";
  }

  // the cash sheet of a cash payment (models/cuentas_app.py _app_cash_sheet):
  // the rows the user may see, the paired one marked and scrolled into view
  function cashView(cash) {
    var sh = cash.sheet;
    if (!sh) {
      return '<div class="block placeholder"><div class="label">Planilla de rendición</div>' +
        '<div class="note">' + h(cash.memo) + "</div>" +
        '<div class="small muted">Pago en efectivo. Todavía no está vinculado a una fila de planilla.</div></div>';
    }
    var head = [h(sh.collector), day(sh.date), h(sh.origin_label)].filter(Boolean).join(" · ");
    var rows = sh.rows.map(function (r) {
      var cls = "cs-row" + (r.hit ? " hit" : "") + (r.kind !== "cliente" || r.after_total ? " other" : "");
      var sub = [r.invoice, r.kind_label, r.after_total ? "debajo del total" : "",
        r.method && r.method !== "efectivo" ? r.method.replace(/_/g, " ") : "", r.note].filter(Boolean);
      return '<div class="' + cls + '"' + (r.hit ? ' id="cs-hit"' : "") + '>' +
        '<div class="l"><span class="t">' + h(r.client || "(sin cliente)") + "</span>" +
        (sub.length ? '<span class="c">' + h(sub.join(" · ")) + "</span>" : "") + "</div>" +
        '<div class="r"><span class="m num">' + (r.paid ? money(r.paid) : "—") + "</span>" +
        (r.invoiced ? '<span class="b num">Fact. ' + money(r.invoiced) + "</span>" : "") + "</div></div>";
    }).join("");
    if (!rows) rows = '<div class="empty">Sin filas para mostrar.</div>';
    var hidden = sh.hidden_count ?
      '<div class="cs-hidden">' + plural(sh.hidden_count, "fila de otros clientes", "filas de otros clientes") + "</div>" : "";
    var files = sh.files.map(function (f) {
      if (f.kind === "image") {
        return '<button type="button" class="btn outline" data-zoom="' + h(f.url) + '">Ver foto</button>';
      }
      return '<a class="btn outline" href="' + h(f.url) + '" download>' +
        (f.kind === "xlsx" ? "Descargar planilla (xlsx)" : "Descargar " + h(f.name)) + "</a>";
    }).join("");
    return '<div class="block"><div class="label">Planilla de rendición</div>' +
      '<div class="cs"><div class="cs-head"><b>' + h(sh.name) + '</b><span>' + head + "</span></div>" +
      '<div class="cs-rows" id="cs-rows" data-sheet="' + sh.id + '">' + rows + "</div>" + hidden +
      '<div class="cs-total"><span>' + h(sh.total_label) + '</span><b class="num">' + money(sh.total) + "</b></div></div>" +
      (files ? '<div class="cs-files">' + files + "</div>" : "") +
      (sh.note ? '<div class="small muted">' + h(sh.note) + "</div>" : "") +
      '<div class="small muted">' + h(cash.memo) + "</div></div>";
  }

  function viewZoom() {
    return '<div class="zoom" id="zoom"><img src="' + h(S.zoom) + '" alt="Imagen ampliada" data-zoomtoggle="1"/>' +
      '<button type="button" class="close" data-unzoom="1" aria-label="Cerrar">×</button></div>';
  }

  // ------------------------------------------------------------- actions

  function sendComment(fieldId) {
    var el = document.getElementById(fieldId);
    var text = el ? el.value.trim() : "";
    if (!text) { toast("Escribí el comentario.", true); return; }
    if (S.busy) return;
    var onLine = fieldId === "c-line";
    S.busy = true;
    post("comment", {
      partner_id: S.account.id, body: text, line_key: onLine ? S.line.line_key : "",
    }).then(function (comment) {
      if (onLine) {
        S.lineDraft = "";
        S.line.comments.unshift(comment);
        S.line.comment_count = S.line.comments.length;
        S.account.rows.forEach(function (row) {
          if (row.line_key === comment.line_key) row.comment_count += 1;
        });
        S.account.data.line_comments.unshift(comment);
      } else {
        S.draft = "";
        S.account.data.comments.unshift(comment);
      }
      bumpDebtorComments(S.account.id, 1);
      toast("Comentario agregado.");
    }).catch(function (err) { toast(err.message === "auth" ? "Tu sesión venció." : err.message, true); })
      .then(function () { S.busy = false; render(); });
  }

  function deleteComment(id) {
    if (!window.confirm("¿Borrar el comentario?")) return;
    post("comment/delete", { id: id }).then(function () {
      var drop = function (list) { return list.filter(function (c) { return c.id !== id; }); };
      var acc = S.account && S.account.data;
      if (acc) {
        var lineComment = acc.line_comments.filter(function (c) { return c.id === id; })[0];
        acc.comments = drop(acc.comments);
        acc.line_comments = drop(acc.line_comments);
        if (lineComment) {
          S.account.rows.forEach(function (row) {
            if (row.line_key === lineComment.line_key) row.comment_count = Math.max(0, row.comment_count - 1);
          });
        }
      }
      if (S.line && S.line.comments) S.line.comments = drop(S.line.comments);
      bumpDebtorComments(S.account.id, -1);
      toast("Comentario borrado.");
      render();
    }).catch(function (err) { toast(err.message, true); });
  }

  function bumpDebtorComments(partnerId, delta) {
    if (!S.debtors) return;
    ["debtors", "uncollectible", "settled"].forEach(function (k) {
      S.debtors[k].forEach(function (d) {
        if (d.id === partnerId) d.comment_count = Math.max(0, d.comment_count + delta);
      });
    });
  }

  function pdfUrl(acc, download) {
    return API + "statement_pdf?" + accountQuery(acc) + (download ? "&download=1" : "");
  }

  // «Compartir PDF» (phone): the report as a file to the share sheet when
  // the browser can share files (Web Share API level 2), else a download.
  // «Imprimir / PDF» (PC): the report in a new tab.
  function sharePdf() {
    var acc = S.account;
    if (!acc || !acc.data || S.sharing) return;
    if (WIDE.matches) {
      window.open(pdfUrl(acc, false), "_blank", "noopener");
      return;
    }
    S.sharing = true;
    render();
    fetch(pdfUrl(acc, true), { credentials: "same-origin", cache: "no-store" }).then(function (res) {
      var type = res.headers.get("Content-Type") || "";
      if (type.indexOf("application/pdf") < 0) {
        return res.json().then(function (j) { throw new Error(j.error || "No se pudo armar el PDF."); },
          function () { S.auth = !res.redirected; throw new Error(res.redirected ? "auth" : "No se pudo armar el PDF."); });
      }
      return res.blob();
    }).then(function (blob) {
      var name = "Estado de cuenta - " + acc.data.partner.name.replace(/[\/:*?"<>|]/g, "-") + ".pdf";
      var file = window.File ? new File([blob], name, { type: "application/pdf" }) : null;
      if (file && navigator.canShare && navigator.canShare({ files: [file] })) {
        return navigator.share({ files: [file], title: name }).catch(function (err) {
          if (err && err.name === "AbortError") return;  // closed the share sheet
          download(blob, name);
        });
      }
      download(blob, name);
    }).catch(function (err) {
      if (err && err.message === "auth") { render(); return; }
      toast(S.online === false ? "Sin conexión." : (err && err.message) || "No se pudo armar el PDF.", true);
    }).then(function () { S.sharing = false; render(); });
  }

  function download(blob, name) {
    var url = URL.createObjectURL(blob);
    var a = document.createElement("a");
    a.href = url;
    a.download = name;
    document.body.appendChild(a);
    a.click();
    setTimeout(function () { URL.revokeObjectURL(url); a.remove(); }, 4000);
    toast("PDF descargado.");
  }

  function setRange() {
    var from = (document.getElementById("r-from") || {}).value || "";
    var to = (document.getElementById("r-to") || {}).value || "";
    var acc = S.account;
    acc.from = from; acc.to = to;
    if (from && to && from > to) { toast("La fecha «Desde» es posterior a «Hasta».", true); return; }
    if (!from && !to) return;
    loadAccount(acc.id, "custom", from, to);
  }

  function closeLine() {
    var r = route();
    S.line = null;
    location.hash = "#cuenta/" + r.id;
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-seller],[data-period],[data-line],[data-close],[data-send],[data-delete]," +
      "[data-zoom],[data-unzoom],[data-zoomtoggle],[data-tab],[data-pick],#more,#opts,#lastload,#share-pdf,#refresh");
    if (!t) return;
    if (t.dataset.pick) {
      if (ev.target.closest("a")) return;  // the name link goes by itself
      location.hash = "#cuenta/" + t.dataset.pick;
    } else if (t.dataset.tab) {
      S.tab = t.dataset.tab;
      if (S.tab === "settled" && !S.showSettled) { S.showSettled = true; loadDebtors(); } else render();
    } else if (t.id === "share-pdf") {
      sharePdf();
    } else if (t.id === "refresh") {
      loadDebtors();
    } else if (t.dataset.seller) {
      if (S.seller !== t.dataset.seller) { S.seller = t.dataset.seller; loadDebtors(); }
    } else if (t.dataset.period) {
      var acc = S.account;
      if (t.dataset.period === "custom") {
        if (acc.period !== "custom") {
          // start from the range on screen, up to today
          acc.period = "custom";
          acc.from = acc.data && acc.data.date_from || "";
          acc.to = acc.to || new Date(Date.now() - new Date().getTimezoneOffset() * 60000).toISOString().slice(0, 10);
          render();
        }
      } else if (acc.period !== t.dataset.period) {
        loadAccount(acc.id, t.dataset.period);
      }
    } else if (t.dataset.line) {
      location.hash = "#cuenta/" + S.account.id + "/mov/" + encodeURIComponent(t.dataset.line);
    } else if (t.dataset.close) {
      if (ev.target === t) closeLine();
    } else if (t.dataset.send) {
      sendComment(t.dataset.send);
    } else if (t.dataset.delete) {
      deleteComment(+t.dataset.delete);
    } else if (t.dataset.zoom) {
      S.zoom = t.dataset.zoom; render();
    } else if (t.dataset.unzoom) {
      S.zoom = null; render();
    } else if (t.dataset.zoomtoggle) {
      document.getElementById("zoom").classList.toggle("big");
    } else if (t.id === "lastload") {
      S.showLoad = !S.showLoad; render();
    } else if (t.id === "opts") {
      S.showOpts = !S.showOpts; render();
    } else if (t.id === "more") {
      loadMore();
    }
  });

  document.addEventListener("input", function (ev) {
    var t = ev.target;
    if (t.id === "q") {
      S.search = t.value;
      clearTimeout(S.searchTimer);
      S.searchTimer = setTimeout(render, 150);
    } else if (t.id === "c-account") {
      S.draft = t.value;
    } else if (t.id === "c-line") {
      S.lineDraft = t.value;
    }
  });

  document.addEventListener("change", function (ev) {
    var id = ev.target.id;
    if (id === "r-from" || id === "r-to") { setRange(); return; }
    if (id === "f-seller") { S.seller = ev.target.value; loadDebtors(); return; }
    if (id === "f-zone") { S.zone = ev.target.value; loadDebtors(); return; }
    if (id === "f-age") { S.minAge = ev.target.value; loadDebtors(); return; }
    if (id === "f-min") { S.minBalance = ev.target.value; loadDebtors(); return; }
    if (ev.target.id === "t-unc") { S.showUncollectible = ev.target.checked; render(); }
    if (ev.target.id === "t-set") {
      S.showSettled = ev.target.checked;
      if (S.showSettled && S.debtors && !S.debtors.settled_loaded) loadDebtors(); else render();
    }
  });

  document.addEventListener("keydown", function (ev) {
    if (ev.key !== "Escape") return;
    if (S.zoom) { S.zoom = null; render(); } else if (S.line) closeLine();
  });

  window.addEventListener("hashchange", onRoute);
  // the PC layout reloads the list with its filters; the phone without them
  if (WIDE.addEventListener) {
    WIDE.addEventListener("change", function () { if (S.debtors) loadDebtors(); else render(); });
  }
  window.addEventListener("online", function () { S.online = true; render(); });
  window.addEventListener("offline", function () { S.online = false; render(); });
  onRoute();
})();
