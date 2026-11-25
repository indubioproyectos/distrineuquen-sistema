/* «Cuentas»: review customer accounts, on the phone and on the PC.
 *
 * Read-only and online only. Three screens, by the URL hash:
 *   (empty)                  the debtors, highest debt first
 *   #cuenta/<id>             the account of one customer
 *   #cuenta/<id>/mov/<key>   one statement row, as a bottom sheet
 *   #transferencias[/<id>]   19.0.1.129.8: the loaded transfers (administración,
 *                            gerencia, sistemas; the server checks) and one
 *                            receipt, as a bottom sheet
 * From 1024 px (19.0.1.112.0, mockup «pc-cuentas-deudores») the debtors are
 * a table with the PC filters and the account sits beside it. The account
 * takes a free date range, «Compartir PDF» (Web Share API when the browser
 * can share files, else a download) and «Por WhatsApp» (a public link the
 * server signs). The rules (who sees which customer, who may share) live on
 * the server (models/cuentas_app.py); the only write is a comment.
 * 19.0.1.129.29 (#100): «‹ Deudores», «‹ Transferencias» and closing a row
 * or a receipt go UP one level (history.back() when it was opened from
 * there) instead of adding a history entry.
 * 19.0.1.129.43: with distri_pedido.ui_v2 on (#app[data-v2]) the list is
 * the design system's «Cuentas de clientes»: one list of every customer by
 * pages (/cuentas/api/list), see «list-v2» below. Off, the page is as before.
 */
(function () {
  "use strict";

  var API = "/cuentas/api/";

  // nav-up:start (tests/data/navegacion_v129_29.js runs this block)
  // 19.0.1.129.29 (#100): «Atrás» goes UP one level instead of adding a
  // history entry. Every in-page level remembers the level it was opened
  // from (history.state.dnFrom, set on hashchange): going up to it is
  // history.back(), to any other level it replaces the entry. The same
  // small helper lives in reparto/app3.js and cuentas/cuentas_app.js.
  var NAV = typeof history !== "undefined" && history && history.replaceState ? history : null;
  var upReplacing = false;
  function hashKey(h) { return String(h || "").replace(/^#/, ""); }
  function urlHash(url) { var i = String(url || "").indexOf("#"); return i < 0 ? "" : url.slice(i + 1); }
  function goUp(hash) {
    var st = NAV && NAV.state;
    if (st && typeof st.dnFrom === "string" && st.dnFrom === hashKey(hash)) { NAV.back(); return; }
    if (NAV && location.replace) { upReplacing = true; location.replace("#" + hashKey(hash)); return; }
    location.hash = hash;
  }
  // 19.0.1.129.32: the entry the page loads on has no known parent. Without
  // this mark, coming back to it with the phone's «Atrás» wrote as its parent
  // the screen just left, and go() to that screen stepped back instead of
  // forward (a stale mark survives a reload in history.state, so it is reset).
  function markLoadedEntry() {
    var st = NAV.state;
    if (st && st.dnFrom === null) return;
    var next = {};
    Object.keys(st || {}).forEach(function (k) { next[k] = st[k]; });
    next.dnFrom = null;
    NAV.replaceState(next, "");
  }
  if (NAV) {
    markLoadedEntry();
    document.addEventListener("click", function (ev) {
      var a = ev.target && ev.target.closest && ev.target.closest("a.back");
      if (!a || ev.defaultPrevented || ev.ctrlKey || ev.metaKey || ev.shiftKey) return;
      var href = a.getAttribute("href") || "";
      if (href.charAt(0) !== "#") return;
      ev.preventDefault();
      goUp(href);
    });
    window.addEventListener("hashchange", function (ev) {
      var st = NAV.state;
      if (!st || !("dnFrom" in st)) {
        var next = {};
        Object.keys(st || {}).forEach(function (k) { next[k] = st[k]; });
        next.dnFrom = upReplacing ? null : urlHash(ev.oldURL);
        NAV.replaceState(next, "");
      }
      upReplacing = false;
    });
  }
  // nav-up:end
  var PERIODS = [["1m", "Último mes"], ["3m", "3 meses"], ["all", "Todo"], ["custom", "Fechas"]];
  var AGES = [["0", "Cualquiera"], ["30", "Más de 30 días"], ["60", "Más de 60 días"], ["90", "Más de 90 días"],
    ["180", "Más de 180 días"]];
  var WIDE = window.matchMedia ? window.matchMedia("(min-width: 1024px)") : { matches: false };
  var SELLERS = [["all", "Todos"], ["italo", "Italo"], ["distrineuquen", "Distrineuquén"]];
  // 19.0.1.129.8 «Transferencias»
  var T_PERIODS = [["today", "Hoy"], ["yesterday", "Ayer"], ["week", "Semana"], ["month", "Mes"], ["custom", "Fechas"]];
  var T_STATES = [["all", "Todas"], ["not_matched", "No cruzadas"], ["anomaly", "Con anomalía"], ["no_receipt", "Sin comprobante"]];
  var AUDIT_PILL = { pendiente: "warn", verificado: "ok", diferencia: "err", sin_transferencia: "err", revertido: "err", anulado: "" };

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
    tr: null,           // 19.0.1.129.8: the transfers payload {.., rows}
    trf: { period: "today", from: "", to: "", user: "0", state: "all" },
    trMore: false,      // loading the next page
    trOpen: null,       // the open transfer row
    trPeople: false,    // the «por persona» subtotals unfolded
    lastList: "#",      // where «‹» of an account goes back to
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
  // $ 1.234.568: no cents (the transfers list)
  function moneyInt(amount) {
    amount = Math.round(+amount || 0);
    var sign = amount < 0 ? "-" : "";
    return sign + "$ " + String(Math.abs(amount)).replace(/\B(?=(\d{3})+(?!\d))/g, ".");
  }
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
    var t = location.hash.match(/^#transferencias(?:\/(\d+))?$/);
    if (t) return { id: null, key: null, transfers: true, transfer: t[1] ? +t[1] : null };
    var m = location.hash.match(/^#cuenta\/(\d+)(?:\/mov\/(.+))?$/);
    return m ? { id: +m[1], key: m[2] ? decodeURIComponent(m[2]) : null } : { id: null, key: null };
  }

  function onRoute() {
    var r = route();
    S.zoom = null;
    if (r.transfers) {
      S.line = null;
      S.lastList = "#transferencias";
      S.trOpen = r.transfer && S.tr ? S.tr.rows.filter(function (x) { return x.id === r.transfer; })[0] || null : null;
      if (r.transfer && !S.trOpen && S.tr) { location.replace("#transferencias"); return; }
      if (!S.tr) loadTransfers(); else render();
      return;
    }
    S.trOpen = null;
    if (!r.id) S.lastList = "#";
    if (!r.id) {
      S.line = null;
      if (WIDE.matches) S.account = null;
      if (V2) { if (!L.data && !L.loading) loadList(true); else render(); }
      else if (!S.debtors) loadDebtors(); else render();
      return;
    }
    // the PC shows the list beside the account
    if (WIDE.matches && V2) { if (!L.data && !L.loading) loadList(true); }
    else if (WIDE.matches && !S.debtors) loadDebtors();
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

  // 19.0.1.129.8 «Transferencias»: the filter is a query; «Ver más» adds a page
  function transferQuery() {
    var f = S.trf;
    var q = "period=" + f.period + "&user=" + encodeURIComponent(f.user) + "&state=" + f.state;
    if (f.period === "custom") {
      q += "&date_from=" + encodeURIComponent(f.from || "") + "&date_to=" + encodeURIComponent(f.to || "");
    }
    return q;
  }

  function loadTransfers() {
    var query = transferQuery();
    var people = S.tr && S.tr.people;
    S.trLoading = true;
    if (S.tr) S.tr.error = "";
    render();
    return get("transfers?" + query).then(function (data) {
      if (query !== transferQuery()) return;  // a newer filter is loading
      data.people = data.people || people || [];
      data._query = query;
      S.tr = data;
      var r = route();
      if (r.transfer) {
        S.trOpen = data.rows.filter(function (x) { return x.id === r.transfer; })[0] || null;
        if (!S.trOpen) location.replace("#transferencias");
      }
    }).catch(function (err) {
      if (err.message !== "auth") S.tr = { error: err.message, rows: [], people: people || [], by_person: [] };
      fail(err);
    }).then(function () { S.trLoading = false; render(); });
  }

  function loadMoreTransfers() {
    var T = S.tr;
    if (!T || S.trMore) return;
    S.trMore = true;
    render();
    get("transfers?" + T._query + "&offset=" + T.rows.length).then(function (data) {
      if (S.tr !== T) return;
      T.rows = T.rows.concat(data.rows);
      T.more = data.more;
    }).catch(fail).then(function () { S.trMore = false; render(); });
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
    var rid = r.transfers ? "t" : (r.id || 0);
    var keepScroll = (WIDE.matches && !r.transfers) || render.lastId === rid;
    render.lastId = rid;
    var side = document.querySelector(".dacc");
    var sideY = side && render.lastSide === (r.id || 0) ? side.scrollTop : 0;
    var csRows = document.getElementById("cs-rows");
    var csKeep = csRows && S.line && csRows.dataset.line === S.line.line_key ? csRows.scrollTop : null;
    document.body.classList.toggle("wide", !!WIDE.matches);
    if (V2) document.body.classList.add("cu2");
    app.innerHTML = r.transfers ? viewTransfers() : WIDE.matches ? (V2 ? viewDesk2() : viewDesk()) : r.id ? viewAccount() :
      V2 ? viewList2() : viewDebtors();
    side = document.querySelector(".dacc");
    if (side) side.scrollTop = sideY;
    render.lastSide = r.id || 0;
    sheetRoot.innerHTML = (S.line ? viewLine() : "") + (r.transfers && S.trOpen ? viewTransfer(S.trOpen) : "") +
      (S.zoom ? viewZoom() : "") + (V2 && CU.sheet ? viewSheet() : "");
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
    document.body.style.overflow = S.line || S.zoom || (r.transfers && S.trOpen) || (V2 && CU.sheet) ? "hidden" : "";
    if (focus) {
      var el = document.getElementById(focus);
      if (el) {
        el.focus();
        try { if (caret != null) el.setSelectionRange(caret, caret); } catch (e) { /* not a text field */ }
      }
    }
    window.scrollTo(0, keepScroll ? y : 0);
    if (V2) watchEnd();
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
      // 19.0.1.129.8: only administración, gerencia and sistemas (the API checks again)
      (D && D.can_see_transfers ? '<a class="loadbtn" href="#transferencias">Transferencias</a>' : "") +
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

  // ---- 19.0.1.129.43 («Cuentas de clientes», DistriNeuquén design system):
  // with distri_pedido.ui_v2 on (#app[data-v2]) the list is ONE list of every
  // customer, highest debt first, then the zero balances, then the credit
  // balances, read by pages from /cuentas/api/list (the server searches,
  // filters, orders and adds the totals). Header: title, Ajustes as a gear
  // and the ⋯ menu (Actualizar, PDF, Excel, Transferencias, Cobro en
  // efectivo, Última carga, Salir). No filter is on by default; «Filtrar y
  // ordenar» is a sheet. From 1024 px: the PC bar, a table and the account
  // beside it. The account, a row, the transfers: the same views as before.

  // list-v2:start (tests/data/cuentas_v129_43.js runs this block)
  var LIST_LIMIT = 50;
  var LIST_DEFAULTS = { q: "", state: "all", sort: "debt", seller: "all", zone: "", minBalance: "", minAge: "0" };
  var SORTS = [["debt", "Mayor deuda"], ["oldest", "Más vencido"], ["name", "Nombre"]];
  var SORT_TEXT = { debt: "de mayor a menor deuda", oldest: "la deuda más vieja primero", name: "por nombre" };
  function listFilters(base) {
    var f = {};
    Object.keys(LIST_DEFAULTS).forEach(function (k) { f[k] = base && base[k] != null ? base[k] : LIST_DEFAULTS[k]; });
    return f;
  }
  function listQuery(f, offset, limit, zones) {
    var q = "q=" + encodeURIComponent(f.q || "") + "&state=" + encodeURIComponent(f.state) +
      "&sort=" + encodeURIComponent(f.sort) + "&seller=" + encodeURIComponent(f.seller) +
      "&zone=" + encodeURIComponent(f.zone || "") + "&min_balance=" + encodeURIComponent(+f.minBalance || 0) +
      "&min_age=" + encodeURIComponent(+f.minAge || 0) + "&offset=" + (offset || 0) +
      "&limit=" + (limit == null ? LIST_LIMIT : limit);
    return zones ? q + "&zones=1" : q;
  }
  // the filters of the sheet that are on (the dot of the filter button)
  function sheetFilterCount(f) {
    return (f.sort !== "debt" ? 1 : 0) + (f.seller !== "all" ? 1 : 0) + (f.zone ? 1 : 0) +
      (+f.minBalance > 0 ? 1 : 0) + (+f.minAge > 0 ? 1 : 0);
  }
  // the server searches from the 2nd letter; less is no search
  function searchTerm(text) {
    var t = String(text || "").replace(/\s+/g, " ").trim();
    return t.length >= 2 ? t : "";
  }
  // the status pill of a row: always a word, the colour only goes with it
  function statePill(d) {
    if (d.state === "credit") return ["ok", "A favor"];
    if (d.state === "zero") return ["ok", "Al día"];
    if (d.state === "uncollectible") return ["neutral", "Incobrable"];
    var n = d.oldest_owed_days;
    if (n > 0) return ["err", "Vencido " + n + (n === 1 ? " día" : " días")];
    return ["", "Con deuda"];
  }
  // the ⋯ menu: secondary actions only, the ones this user may use
  function menuItems(D, seller) {
    var items = [{ id: "cu-refresh", label: "Actualizar", icon: "refresh" }];
    if (D && D.can_export) {
      var s = "?seller=" + encodeURIComponent(seller || "all");
      items.push({ href: "/cuentas/deudores.pdf" + s, label: "Descargar PDF", icon: "download", blank: true });
      items.push({ href: "/cuentas/deudores.xlsx" + s, label: "Descargar Excel", icon: "download" });
    }
    if (D && D.can_see_transfers) items.push({ href: "#transferencias", label: "Transferencias", icon: "receipt" });
    items.push({ href: "/cobro", label: "Cobro en efectivo", icon: "cash" });
    if (D && D.last_load) items.push({ id: "cu-lastload", label: "Última carga de comprobantes", icon: "clock" });
    items.push({ sep: true });
    items.push({ href: "/web/session/logout?redirect=%2Fweb%2Flogin", label: "Salir", icon: "logout" });
    return items;
  }
  // one list read by pages: a newer reload drops the answers of an older one;
  // next() asks the following page once, only while there are more
  function makeList(fetchPage, changed, failed) {
    var me = { f: null, rows: [], data: null, more: false, loading: false, loadingMore: false, seq: 0, zones: null };
    function take(seq, append) {
      return function (data) {
        if (seq !== me.seq) return;
        if (data.zones) me.zones = data.zones;
        me.rows = append ? me.rows.concat(data.rows || []) : (data.rows || []);
        me.more = !!data.more;
        if (!append) me.data = data;
      };
    }
    function done(seq, key) {
      return function () { if (seq === me.seq) { me[key] = false; changed(); } };
    }
    function oops(seq) {
      return function (err) { if (seq === me.seq) failed(err); };
    }
    me.reload = function (f) {
      me.f = listFilters(f);
      me.seq += 1;
      var seq = me.seq;
      me.loading = true;
      me.loadingMore = false;
      changed();
      return fetchPage(listQuery(me.f, 0, LIST_LIMIT, !me.zones))
        .then(take(seq, false), oops(seq)).then(done(seq, "loading"));
    };
    me.next = function () {
      if (!me.f || !me.more || me.loading || me.loadingMore) return null;
      var seq = me.seq;
      me.loadingMore = true;
      changed();
      return fetchPage(listQuery(me.f, me.rows.length, LIST_LIMIT, false))
        .then(take(seq, true), oops(seq)).then(done(seq, "loadingMore"));
    };
    return me;
  }
  // list-v2:end

  var V2 = !!(app && app.dataset && app.dataset.v2);
  var LIST_NAME = V2 ? "Cuentas" : "Deudores";
  var L = V2 ? makeList(function (query) { return get("list?" + query); }, render, fail) : null;
  var CU = { f: listFilters(), menu: false, sheet: false, draft: null, draftCount: null, countSeq: 0, lastLoad: false };
  var ICONS = {
    gear: '<circle cx="12" cy="12" r="3"/><path d="M19.4 15a1.7 1.7 0 0 0 .3 1.8l.1.1a2 2 0 1 1-2.8 2.8l-.1-.1a1.7 1.7 0 0 0-1.8-.3 1.7 1.7 0 0 0-1 1.5V21a2 2 0 1 1-4 0v-.1a1.7 1.7 0 0 0-1.1-1.5 1.7 1.7 0 0 0-1.8.3l-.1.1a2 2 0 1 1-2.8-2.8l.1-.1a1.7 1.7 0 0 0 .3-1.8 1.7 1.7 0 0 0-1.5-1H3a2 2 0 1 1 0-4h.1a1.7 1.7 0 0 0 1.5-1.1 1.7 1.7 0 0 0-.3-1.8l-.1-.1a2 2 0 1 1 2.8-2.8l.1.1a1.7 1.7 0 0 0 1.8.3H9a1.7 1.7 0 0 0 1-1.5V3a2 2 0 1 1 4 0v.1a1.7 1.7 0 0 0 1 1.5 1.7 1.7 0 0 0 1.8-.3l.1-.1a2 2 0 1 1 2.8 2.8l-.1.1a1.7 1.7 0 0 0-.3 1.8V9a1.7 1.7 0 0 0 1.5 1H21a2 2 0 1 1 0 4h-.1a1.7 1.7 0 0 0-1.5 1z"/>',
    more: '<circle cx="5" cy="12" r="1.4" fill="currentColor"/><circle cx="12" cy="12" r="1.4" fill="currentColor"/><circle cx="19" cy="12" r="1.4" fill="currentColor"/>',
    filter: '<path d="M4 6h16M7 12h10M10 18h4"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4"/>',
    next: '<path d="M9 5l7 7-7 7"/>',
    chevdown: '<path d="M5 9l7 7 7-7"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.3-4.9L4 8M4 4v4h4M4 13a8 8 0 0 0 14.3 4.9L20 16M20 20v-4h-4"/>',
    download: '<path d="M12 4v11M7 11l5 5 5-5M5 20h14"/>',
    receipt: '<path d="M14 3H6a1 1 0 0 0-1 1v16a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V8z"/><path d="M14 3v5h5M9 15l2 2 4-4"/>',
    cash: '<rect x="3" y="6" width="18" height="12" rx="2"/><circle cx="12" cy="12" r="2.5"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    logout: '<path d="M15 4h4a1 1 0 0 1 1 1v14a1 1 0 0 1-1 1h-4M10 17l5-5-5-5M15 12H3"/>',
    empty: '<circle cx="11" cy="11" r="7"/><path d="M20 20l-4-4M8 11h6"/>',
  };
  function icon(name, size) {
    var s = size || 24;
    return '<svg width="' + s + '" height="' + s + '" viewBox="0 0 24 24" fill="none" stroke="currentColor" ' +
      'stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' + (ICONS[name] || "") + "</svg>";
  }
  // the role's sections, for the PC bar (the same links as the bottom bar)
  var NAV_ITEMS = Array.prototype.map.call(document.querySelectorAll(".dn-bottom--rol .dn-nav a"), function (a) {
    var spans = a.querySelectorAll("span");
    return { href: a.getAttribute("href"), label: spans.length ? spans[spans.length - 1].textContent : a.textContent,
      on: a.classList.contains("on") };
  });

  function initials(name) {
    var words = String(name || "").replace(/[^A-Za-zÁÉÍÓÚÑÜáéíóúñü0-9 ]/g, " ").trim().split(/\s+/);
    return ((words[0] || "").charAt(0) + (words[1] || "").charAt(0)).toUpperCase() || "·";
  }
  function amountClass(d) {
    return d.state === "credit" ? " dn-rowi__amt--fav" : d.state === "zero" ? " dn-rowi__amt--zero" : "";
  }
  // a credit shows its amount in green with «A favor» (the mockups), no sign
  function rowAmount(d) { return money(d.state === "credit" ? Math.abs(d.balance) : d.balance); }
  function pillHtml(d) {
    var p = statePill(d);
    return '<span class="dn-chip' + (p[0] ? " dn-chip--" + p[0] : "") + '">' + h(p[1]) + "</span>";
  }
  function rowSub(d) {
    var facts = [];
    if (d.zone) facts.push(h(d.zone));
    facts.push(d.last_payment_date ? "último pago " + shortDay(d.last_payment_date) : "sin pagos");
    if (d.candidate_count) facts.push(plural(d.candidate_count, "pago a confirmar", "pagos a confirmar"));
    if (d.comment_count) facts.push(plural(d.comment_count, "comentario", "comentarios"));
    return facts.join(" · ");
  }

  function listRow(d) {
    return '<a class="dn-rowi" href="#cuenta/' + d.id + '"><span class="dn-rowi__lead" aria-hidden="true">' +
      h(initials(d.name)) + '</span><span class="dn-rowi__main"><span class="dn-rowi__title">' + h(d.name) +
      '</span><span class="dn-rowi__sub">' + rowSub(d) + '</span></span><span class="dn-rowi__end">' +
      '<span class="dn-rowi__amt' + amountClass(d) + '">' + rowAmount(d) + "</span>" + pillHtml(d) + "</span>" +
      '<span class="dn-rowi__chev">' + icon("next", 20) + "</span></a>";
  }

  function chipsView(D) {
    var c = (D && D.counts) || {};
    var defs = [["all", "Todos"], ["debt", "Con deuda"], ["zero", "Al día"], ["credit", "A favor"]];
    if (D && D.can_see_uncollectible && (c.uncollectible || CU.f.state === "uncollectible")) {
      defs.push(["uncollectible", "Incobrables"]);
    }
    return '<div class="dn-chips" role="group" aria-label="Estado de la cuenta">' + defs.map(function (x) {
      var on = CU.f.state === x[0];
      return '<button type="button" class="dn-fchip' + (on ? " on" : "") + '" aria-pressed="' + on + '" data-cu-state="' +
        x[0] + '">' + x[1] + (D ? " <b>" + (c[x[0]] || 0) + "</b>" : "") + "</button>";
    }).join("") + "</div>";
  }

  function menuView(D) {
    if (!CU.menu) return "";
    return '<div class="dn-menu cu-menu" role="menu" id="cu-menu">' + menuItems(D, CU.f.seller).map(function (it) {
      if (it.sep) return '<div class="dn-menu__sep"></div>';
      var inner = icon(it.icon, 22) + h(it.label);
      if (it.href) {
        return '<a class="dn-menu__item" role="menuitem" href="' + h(it.href) + '"' +
          (it.blank ? ' target="_blank" rel="noopener"' : "") + ">" + inner + "</a>";
      }
      return '<button type="button" class="dn-menu__item" role="menuitem" id="' + it.id + '">' + inner + "</button>";
    }).join("") + "</div>";
  }

  function lastLoadView(D) {
    var load = D && D.last_load;
    if (!CU.lastLoad || !load) return "";
    return '<div class="dn-banner dn-banner--card cu-lastload"><span>Comprobantes del chat cargados hasta el <b>' +
      h(load.receipts_until) + "</b>" + (load.import_file ? ". Zip «" + h(load.import_file) + "», importado el " +
      h(load.imported_at) + (load.imported_by ? " por " + h(load.imported_by) : "") : "") + ".</span>" +
      '<button type="button" class="dn-icon-btn" id="cu-lastload-x" aria-label="Cerrar">×</button></div>';
  }

  function searchView(id) {
    return '<div class="dn-search">' + icon("search", 22) + '<input id="' + id + '" class="dn-input" type="search" ' +
      'inputmode="search" autocomplete="off" enterkeyhint="search" aria-label="Buscar cliente, CUIT o WhatsApp" ' +
      'placeholder="Buscar cliente, CUIT o WhatsApp" value="' + h(S.search) + '"/></div>';
  }

  function filterDot() {
    var n = sheetFilterCount(CU.f);
    return n ? '<span class="dn-icon-btn__dot">' + n + "</span>" : "";
  }

  function countLine(D) {
    if (!D) return "";
    var n = D.total_rows;
    if (!n) return "";
    return '<p class="cu-count">' + plural(n, "cliente", "clientes") + " · " + SORT_TEXT[CU.f.sort] + "</p>";
  }

  function emptyView(D) {
    var why = CU.f.q ? "Ningún cliente coincide con «" + h(CU.f.q) + "»." :
      CU.f.state !== "all" || sheetFilterCount(CU.f) ? "Ningún cliente con estos filtros." : "No hay clientes para mostrar.";
    return '<div class="dn-empty">' + icon("empty", 48) + "<b>" + why + "</b>" +
      (CU.f.q || CU.f.state !== "all" || sheetFilterCount(CU.f) ?
        '<button type="button" class="dn-btn dn-btn--secondary dn-btn--sm dn-btn--auto" id="cu-reset">Ver todos los clientes</button>' : "") +
      "</div>";
  }

  function moreView(wide) {
    if (!L.more) return "";
    var left = Math.max(0, (L.data ? L.data.total_rows : 0) - L.rows.length);
    return '<div id="cu-sentinel" class="cu-sentinel"></div><button type="button" class="dn-btn dn-btn--secondary' +
      (wide ? " dn-btn--auto" : "") + ' cu-next" id="cu-next"' + (L.loadingMore ? " disabled" : "") + ">" +
      (L.loadingMore ? "Cargando…" : "Ver más (" + left + ")") + "</button>";
  }

  function kpiHint(D) {
    var bits = [plural(D.debtor_count, "cliente con deuda", "clientes con deuda")];
    if (D.credit_count) bits.push(D.credit_count + " con saldo a favor");
    return bits.join(" · ");
  }

  function viewList2() {
    var D = L.data;
    var head = '<header class="dn-top cu-top"><div class="dn-top__row"><div class="dn-top__txt">' +
      '<h1 class="dn-top__title">Cuentas de clientes</h1></div>' +
      '<button type="button" class="dn-icon-btn" id="cu-gear" aria-label="Ajustes">' + icon("gear") + "</button>" +
      '<button type="button" class="dn-icon-btn" id="cu-more" aria-label="Más acciones" aria-haspopup="menu" aria-expanded="' +
      CU.menu + '">' + icon("more") + "</button></div>" + menuView(D) + "</header>";
    var out = [banner(), lastLoadView(D)];
    out.push(D ? '<div class="dn-kpi dn-kpi--hero"><span class="dn-kpi__label">Total a cobrar</span>' +
      '<span class="dn-kpi__value dn-num">' + money(D.total_debt) + '</span><span class="dn-kpi__hint">' + kpiHint(D) +
      (D.uncollectible_count ? " · incobrables aparte " + money(D.uncollectible_total) : "") + "</span></div>" :
      '<div class="dn-skel cu-skel-kpi"></div>');
    out.push('<div class="cu-searchrow">' + searchView("q") + '<button type="button" class="dn-icon-btn dn-icon-btn--line" ' +
      'id="cu-filter" aria-label="Filtrar y ordenar" aria-haspopup="dialog">' + icon("filter") + filterDot() + "</button></div>");
    out.push(chipsView(D));
    if (!D) {
      out.push('<div class="dn-skel"></div><div class="dn-skel"></div><div class="dn-skel"></div>');
    } else {
      out.push(countLine(D) + (L.loading ? '<p class="cu-count cu-upd">Actualizando…</p>' : ""));
      out.push(L.rows.length ? '<div class="dn-list">' + L.rows.map(listRow).join("") + "</div>" + moreView(false) :
        L.loading ? "" : emptyView(D));
    }
    return head + '<main class="cu-main">' + out.join("") + "</main>";
  }

  function deskRow2(d, selected) {
    var oldest = d.oldest_owed_date ? shortDay(d.oldest_owed_date) + ' <span class="muted">· ' +
      plural(d.oldest_owed_days, "día", "días") + "</span>" : '<span class="muted">—</span>';
    var extra = [];
    if (d.ref) extra.push(h(d.ref));
    if (d.candidate_count) extra.push(plural(d.candidate_count, "pago a confirmar", "pagos a confirmar"));
    if (d.comment_count) extra.push(plural(d.comment_count, "comentario", "comentarios"));
    var amt = d.state === "credit" ? ' style="color:var(--dn-green-text)"' : d.state === "zero" ? ' style="color:var(--dn-muted)"' : "";
    return '<tr data-pick="' + d.id + '"' + (selected ? ' aria-selected="true"' : "") + ">" +
      '<td class="cu-name"><a href="#cuenta/' + d.id + '">' + h(d.name) + "</a>" +
      (extra.length ? '<span class="cu-extra">' + extra.join(" · ") + "</span>" : "") + "</td>" +
      '<td class="muted">' + h(d.zone) + '</td><td class="muted">' + h(d.seller_label) + "</td>" +
      '<td class="muted">' + (d.last_payment_date ? shortDay(d.last_payment_date) + " · " + money(d.last_payment_amount) : "Sin pagos") + "</td>" +
      '<td class="muted">' + oldest + "</td>" +
      '<td class="num"' + amt + ">" + rowAmount(d) + "</td><td>" + pillHtml(d) + "</td></tr>";
  }

  function viewDesk2() {
    var D = L.data;
    var r = route();
    var nav = NAV_ITEMS.map(function (it) {
      return '<a href="' + h(it.href) + '"' + (it.on ? ' class="on" aria-current="page"' : "") + ">" + h(it.label) + "</a>";
    }).join("");
    var bar = '<header class="dn-appbar"><div class="dn-appbar__brand"><span class="cu-logo" aria-hidden="true">DN</span>' +
      "<span>Distri Neuquén</span></div>" + '<nav class="dn-appbar__nav" aria-label="Secciones">' + nav + "</nav>" +
      (D ? '<span class="dn-appbar__user">' + h(D.user) + "</span>" : "") +
      '<button type="button" class="dn-icon-btn" id="cu-gear" aria-label="Ajustes">' + icon("gear") + "</button></header>";
    var n = sheetFilterCount(CU.f);
    var head = '<div class="dn-pagehead"><div class="dn-pagehead__txt"><h1>Cuentas de clientes</h1><p>' +
      (D ? plural(D.customer_count, "cliente", "clientes") + " · actualizado hoy " + h(D.updated_at) : "Cargando…") +
      (L.loading && D ? " · actualizando…" : "") + "</p></div>" +
      '<div class="cu-pcsearch">' + searchView("q") + "</div>" +
      '<button type="button" class="dn-btn dn-btn--secondary dn-btn--auto" id="cu-filter" aria-haspopup="dialog">' +
      icon("filter", 20) + "Filtros" + (n ? " (" + n + ")" : "") + "</button>" +
      '<span class="cu-morewrap"><button type="button" class="dn-icon-btn dn-icon-btn--line" id="cu-more" ' +
      'aria-label="Más acciones: actualizar, descargar PDF o Excel" aria-haspopup="menu" aria-expanded="' + CU.menu + '">' +
      icon("more") + "</button>" + menuView(D) + "</span></div>";
    var kpis = "";
    if (D) {
      kpis = '<div class="cu-kpis"><div class="dn-kpi dn-kpi--hero"><span class="dn-kpi__label">Total a cobrar</span>' +
        '<span class="dn-kpi__value dn-num">' + money(D.total_debt) + '</span><span class="dn-kpi__hint">' +
        plural(D.debtor_count, "cliente con deuda", "clientes con deuda") + "</span></div>" +
        '<div class="dn-kpi"><span class="dn-kpi__label">Pagos a confirmar</span><span class="dn-kpi__value dn-num">' +
        money(D.total_candidates || 0) + '</span><span class="dn-kpi__hint">no restan de la deuda</span></div>' +
        '<div class="dn-kpi"><span class="dn-kpi__label">Saldos a favor</span><span class="dn-kpi__value dn-num">' +
        money(Math.abs(D.credit_total || 0)) + '</span><span class="dn-kpi__hint">' + plural(D.credit_count, "cliente", "clientes") +
        "</span></div>" +
        (D.can_see_uncollectible ? '<div class="dn-kpi"><span class="dn-kpi__label">Incobrables (aparte)</span>' +
          '<span class="dn-kpi__value dn-num">' + money(D.uncollectible_total) + '</span><span class="dn-kpi__hint">' +
          '<span class="dn-chip dn-chip--neutral">' + plural(D.uncollectible_count, "cliente", "clientes") + "</span></span></div>" : "") +
        "</div>";
    }
    var table;
    if (!D) {
      table = '<div class="dn-skel"></div><div class="dn-skel"></div>';
    } else if (!L.rows.length) {
      table = L.loading ? '<div class="dn-skel"></div>' : emptyView(D);
    } else {
      table = '<div class="dn-table-wrap"><table class="dn-table cu-table"><thead><tr><th>Cliente</th><th>Zona</th>' +
        '<th>Vendedor</th><th>Último pago</th><th>Deuda más vieja</th><th class="num">Saldo</th><th>Estado</th></tr></thead><tbody>' +
        L.rows.map(function (d) { return deskRow2(d, d.id === r.id); }).join("") + "</tbody>" +
        '<tfoot><tr><td colspan="7">' + L.rows.length + " de " + plural(D.total_rows, "cliente", "clientes") + " · " +
        SORT_TEXT[CU.f.sort] + "</td></tr></tfoot></table></div>" + moreView(true);
    }
    return '<div class="cu-desk">' + bar + banner() + '<main class="cu-wrap">' + head + lastLoadView(D) + kpis +
      chipsView(D) + '<div class="cu-cols"><section class="cu-tablecol" aria-label="Clientes">' + table + "</section>" +
      deskAccount() + "</div></main></div>";
  }

  function viewSheet() {
    var d = CU.draft;
    var D = L.data;
    var zones = L.zones || [];
    var field = function (id, label, control) {
      return '<div class="dn-field"><label for="' + id + '">' + label + "</label>" + control + "</div>";
    };
    var select = function (id, options, value) {
      return '<div class="dn-selectwrap"><select class="dn-input" id="' + id + '">' + options.map(function (o) {
        return '<option value="' + h(o[0]) + '"' + (String(value) === String(o[0]) ? " selected" : "") + ">" + h(o[1]) + "</option>";
      }).join("") + "</select>" + icon("chevdown", 20) + "</div>";
    };
    var body = '<div class="dn-field"><span class="dn-field__label">Ordenar por</span><div class="dn-seg" role="group" ' +
      'aria-label="Ordenar por">' + SORTS.map(function (s) {
        return '<button type="button" data-cu-sort="' + s[0] + '" aria-pressed="' + (d.sort === s[0]) + '">' + s[1] + "</button>";
      }).join("") + "</div></div>";
    if (D && D.can_filter_seller) {
      body += field("cu-f-seller", "Vendedor", select("cu-f-seller", SELLERS.map(function (s) {
        return [s[0], s[0] === "all" ? "Todos" : "Clientes de " + s[1]];
      }), d.seller));
    }
    body += field("cu-f-zone", "Zona", select("cu-f-zone", [["", "Todas"]].concat(zones.map(function (z) { return [z, z]; })), d.zone));
    body += field("cu-f-min", "Deuda mínima", '<input id="cu-f-min" class="dn-input dn-num" type="number" min="0" step="1000" ' +
      'inputmode="numeric" placeholder="$ 0" value="' + h(d.minBalance) + '"/>');
    body += field("cu-f-age", "Deuda más vieja", select("cu-f-age", AGES, d.minAge));
    var label = CU.draftCount == null ? "Ver clientes" : "Ver " + plural(CU.draftCount, "cliente", "clientes");
    return '<div class="dn-scrim cu-scrim" data-cu-close="1"></div><div class="dn-sheet cu-sheet" role="dialog" ' +
      'aria-modal="true" aria-labelledby="cu-sheet-t"><span class="dn-sheet__grip" aria-hidden="true"></span>' +
      '<h2 id="cu-sheet-t">Filtrar y ordenar</h2>' + body + '<div class="dn-foot cu-sheet-foot">' +
      '<button type="button" class="dn-btn dn-btn--secondary" id="cu-clear">Limpiar</button>' +
      '<button type="button" class="dn-btn" id="cu-apply">' + label + "</button></div></div>";
  }

  // ---- list actions

  var LIST_CACHE = "list:v2";
  function loadList(keepCache) {
    var plain = JSON.stringify(listFilters(CU.f)) === JSON.stringify(listFilters());
    if (keepCache && plain && !L.data) {
      var cached = stored(LIST_CACHE);
      if (cached && cached.data) { L.data = cached.data; L.rows = cached.rows || []; L.more = !!cached.data.more; }
    }
    var seq;
    var p = L.reload(CU.f);
    seq = L.seq;
    return p.then(function () {
      if (plain && seq === L.seq && L.data && !L.loading) store(LIST_CACHE, { data: L.data, rows: L.rows.slice(0, LIST_LIMIT) });
    });
  }
  function setFilters(next) {
    CU.f = listFilters(next);
    loadList(false);
  }
  function openSheet() {
    CU.menu = false;
    CU.sheet = true;
    CU.draft = listFilters(CU.f);
    CU.draftCount = L.data ? L.data.total_rows : null;
    render();
  }
  function closeSheet() {
    CU.sheet = false;
    CU.draft = null;
    render();
    var b = document.getElementById("cu-filter");
    if (b) b.focus();
  }
  // «Ver N clientes»: the count of the draft filters (limit 0: counts only)
  function countDraft() {
    var seq = ++CU.countSeq;
    clearTimeout(CU.countTimer);
    CU.countTimer = setTimeout(function () {
      get("list?" + listQuery(CU.draft, 0, 0, false)).then(function (data) {
        if (seq !== CU.countSeq || !CU.sheet) return;
        CU.draftCount = data.total_rows;
        render();
      }).catch(function () { /* the button keeps «Ver clientes» */ });
    }, 250);
  }
  // infinite scroll: the next page when the end of the list shows (the
  // «Ver más» button stays for browsers without IntersectionObserver)
  var observer = V2 && window.IntersectionObserver ? new IntersectionObserver(function (entries) {
    if (entries.some(function (e) { return e.isIntersecting; })) L.next();
  }, { rootMargin: "400px 0px" }) : null;
  function watchEnd() {
    if (!observer) return;
    observer.disconnect();
    var s = document.getElementById("cu-sentinel");
    if (s) observer.observe(s);
  }
  function bumpListComments(partnerId, delta) {
    if (!L) return;
    L.rows.forEach(function (d) {
      if (d.id === partnerId) d.comment_count = Math.max(0, d.comment_count + delta);
    });
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
      // 19.0.1.129.2 (owner): «Comprobantes pendientes» above «Movimientos», phone and PC
      receiptsView(d) +
      '<div class="label">Movimientos · lo más nuevo arriba</div>' + statementView(acc, wide) +
      candidatesView(d) + commentsView(d);
  }

  function viewAccount() {
    var acc = S.account;
    var d = acc && acc.data;
    var p = d && d.partner;
    var head = '<div class="sticky"><div class="head compact">' +
      (S.lastList === "#transferencias" ? '<a class="back" href="#transferencias">‹ Transferencias</a>' :
        '<a class="back" href="#">‹ ' + LIST_NAME + "</a>");
    if (acc && acc.error) {
      return head + "<h1>Cuenta</h1></div>" + banner() + '</div><div class="body"><div class="denied">' +
        '<div class="banner err">' + h(acc.error) + '</div><p><a href="#">Volver a ' + LIST_NAME.toLowerCase() +
        "</a></p></div></div>";
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
      '<span class="grow"></span>' +
      (D && D.can_see_transfers ? '<a class="loadbtn" href="#transferencias">Transferencias</a>' : "") +
      '<a class="loadbtn" href="/cobro">Cobro en efectivo</a>' +
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
      (D && D.report_url ? '<a class="btn ghost sm" href="' + h(D.report_url) + '" target="_blank" rel="noopener">PDF y Excel</a>' : "") +
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

  // ---- 19.0.1.129.8 «Transferencias»: loaded transfers, newest first

  function transferBadges(t) {
    var out = "";
    if (t.audit_label) out += '<span class="pill ' + (AUDIT_PILL[t.audit_state] || "") + '">' + h(t.audit_label) + "</span>";
    else if (t.load_state === "pending") out += '<span class="pill warn">' + h(t.load_label) + "</span>";
    if (!t.matched) out += '<span class="pill err">no cruzada</span>';
    if (t.with_anomaly) out += '<span class="pill err">anomalía</span>';
    if (t.without_receipt) out += '<span class="pill warn">sin comprobante</span>';
    return out;
  }

  function transferRow(t) {
    return '<button type="button" class="debtor trow" data-trow="' + t.id + '">' +
      '<div class="who"><b>' + h(t.partner || "Sin cliente") + "</b>" +
      '<span class="d">' + shortDay(t.date) + " " + h(t.time) + " · " + h(t.loaded_by || "Sin usuario") + "</span>" +
      '<div class="chips">' + transferBadges(t) + "</div></div>" +
      '<div class="amt"><b class="num">' + moneyInt(t.amount) + "</b></div></button>";
  }

  function viewTransfers() {
    var T = S.tr;
    var f = S.trf;
    var head = '<div class="sticky"><div class="head compact"><a class="back" href="#">‹ ' + LIST_NAME + "</a>" +
      '<div class="kicker">Cuentas de clientes · Transferencias cargadas</div>';
    if (T && !T.error && T.count != null) {
      head += '<div class="total num">' + moneyInt(T.amount) + "</div>" +
        '<div class="sub">' + plural(T.count, "transferencia", "transferencias") +
        (S.trLoading ? ' · <span class="upd">actualizando…</span>' : "") + "</div>";
    } else {
      head += "<h1>Transferencias</h1>";
    }
    var people = (T && T.people) || [];
    head += "</div>" + banner() + '<div class="tools">' +
      '<div class="seg">' + T_PERIODS.map(function (x) {
        return '<button type="button" data-tperiod="' + x[0] + '" class="' + (f.period === x[0] ? "on" : "") + '">' + x[1] + "</button>";
      }).join("") + "</div>" +
      (f.period === "custom" ? '<div class="range">' +
        '<label class="fld">Desde<input id="t-from" class="inp" type="date" value="' + h(f.from) + '"/></label>' +
        '<label class="fld">Hasta<input id="t-to" class="inp" type="date" value="' + h(f.to) + '"/></label></div>' : "") +
      '<select id="t-user" class="inp" aria-label="Quién la cargó"><option value="0">Todas las personas</option>' +
      people.map(function (p) {
        return '<option value="' + p.id + '"' + (String(p.id) === String(f.user) ? " selected" : "") + ">" + h(p.name) + "</option>";
      }).join("") + "</select>" +
      '<div class="seg">' + T_STATES.map(function (x) {
        return '<button type="button" data-tstate="' + x[0] + '" class="' + (f.state === x[0] ? "on" : "") + '">' + x[1] + "</button>";
      }).join("") + "</div></div></div>";
    var body;
    if (!T) {
      body = '<div class="cargando">Cargando…</div>';
    } else if (T.error) {
      body = '<div class="denied"><div class="banner err">' + h(T.error) + '</div><p><a href="#">Volver a ' +
        LIST_NAME.toLowerCase() + "</a></p></div>";
    } else {
      var out = [];
      if (T.by_person && T.by_person.length > 1) {
        out.push('<details class="block tper"' + (S.trPeople ? " open" : "") + '><summary class="link" id="t-people">Por persona</summary>' +
          '<div class="card">' + T.by_person.map(function (p) {
            return '<div class="ci-top"><span>' + h(p.name) + ' <span class="muted">· ' + p.count + '</span></span><b class="num">' +
              moneyInt(p.amount) + "</b></div>";
          }).join("") + "</div></details>");
      }
      out.push(T.rows.length ? T.rows.map(transferRow).join("") :
        '<div class="empty">No hay transferencias con este filtro.</div>');
      if (T.more) {
        out.push('<button type="button" class="btn outline more" id="tmore"' + (S.trMore ? " disabled" : "") + ">" +
          (S.trMore ? "Cargando…" : "Ver más (" + (T.count - T.rows.length) + ")") + "</button>");
      }
      body = out.join("");
    }
    return '<div class="trf">' + head + '<div class="body">' + body + "</div></div>";
  }

  function viewTransfer(t) {
    var url = API + "transfer_receipt?id=" + t.id;
    var file;
    if (!t.has_file) file = '<div class="note">Sin comprobante adjunto.</div>';
    else if (t.file_kind === "image") {
      file = '<img class="receipt big" alt="Comprobante" data-zoom="' + h(url) + '" src="' + h(url) + '"/>';
    } else if (t.file_kind === "pdf") {
      file = '<iframe class="receipt-pdf" title="Comprobante" src="' + h(url) + '"></iframe>' +
        '<a class="btn outline" href="' + h(url) + '" target="_blank" rel="noopener">Abrir el PDF</a>';
    } else {
      file = '<a class="btn outline" href="' + h(url) + '" download>Descargar el comprobante</a>';
    }
    var details = kv([
      ["Fecha", day(t.date) + " " + h(t.time)],
      ["Cargó o acreditó", h(t.loaded_by)],
      ["Subió el comprobante", t.uploaded_by && t.uploaded_by !== t.loaded_by ? h(t.uploaded_by) : ""],
      ["Carga", h(t.load_label)],
      ["Cliente", h(t.partner)],
      ["Importe", '<span class="num">' + money(t.amount) + "</span>"],
      ["Operación", h(t.operation)],
      ["Paga", h(t.destination)],
      ["Control con MP", t.audit_label ? '<span class="pill ' + (AUDIT_PILL[t.audit_state] || "") + '">' + h(t.audit_label) + "</span>" : ""],
      ["Reporte de MP", t.matched ? "Cruzada" + (t.income_date ? " · " + h(t.income_date) : "") : '<span class="pill err">no cruzada</span>'],
      ["Anomalía", t.with_anomaly || t.anomaly ? h(t.anomaly || "Sí") : ""],
      ["Comprobante", t.without_receipt ? '<span class="pill warn">sin comprobante</span>' : ""],
    ]);
    var link = t.partner_id ? '<a class="btn outline" href="#cuenta/' + t.partner_id + '">Ver estado de cuenta</a>' : "";
    return '<div class="overlay" data-tclose="1"><div class="sheet" role="dialog" aria-modal="true">' +
      '<div class="grab"><b>' + moneyInt(t.amount) + " · " + h(t.partner || "Sin cliente") + '</b>' +
      '<button type="button" class="close" data-tclose="1" aria-label="Cerrar">×</button></div>' +
      '<div class="inner">' + file + details + link + "</div></div></div>";
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
        parts.push('<div class="block"><div class="label">Historia del sistema anterior</div>' + kv([
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
    if (V2) bumpListComments(partnerId, delta);
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
    goUp("#cuenta/" + r.id);
  }

  // 19.0.1.129.43: the clicks of the v2 list (header, ⋯ menu, chips, sheet)
  function v2Click(ev) {
    var t = ev.target.closest && ev.target.closest("#cu-gear,#cu-more,#cu-refresh,#cu-lastload,#cu-lastload-x," +
      "#cu-filter,#cu-next,#cu-reset,#cu-clear,#cu-apply,[data-cu-state],[data-cu-sort],[data-cu-close],#cu-menu a");
    var inMenu = t && (t.id === "cu-more" || (t.closest && t.closest("#cu-menu")));
    if (CU.menu && !inMenu) {
      CU.menu = false;
      render();
    }
    if (!t) return false;
    var f;
    if (t.id === "cu-gear") {
      var gear = document.getElementById("dn-gear");
      if (gear) gear.click();
    } else if (t.id === "cu-more") {
      CU.menu = !CU.menu;
      render();
      var first = CU.menu && document.querySelector("#cu-menu .dn-menu__item");
      if (first) first.focus();
    } else if (t.tagName === "A") {
      // a link of the menu goes by itself; the menu closes
      CU.menu = false;
      setTimeout(render, 0);
    } else if (t.id === "cu-refresh") {
      CU.menu = false;
      loadList(false);
    } else if (t.id === "cu-lastload") {
      CU.menu = false;
      CU.lastLoad = true;
      render();
    } else if (t.id === "cu-lastload-x") {
      CU.lastLoad = false;
      render();
    } else if (t.id === "cu-filter") {
      openSheet();
    } else if (t.id === "cu-next") {
      L.next();
    } else if (t.id === "cu-reset") {
      S.search = "";
      setFilters(listFilters());
    } else if (t.dataset.cuState) {
      if (CU.f.state !== t.dataset.cuState) {
        f = listFilters(CU.f);
        f.state = t.dataset.cuState;
        setFilters(f);
      }
    } else if (t.dataset.cuSort) {
      CU.draft.sort = t.dataset.cuSort;
      render();
    } else if (t.dataset.cuClose) {
      if (ev.target === t) closeSheet();
    } else if (t.id === "cu-clear") {
      f = listFilters();
      f.q = CU.f.q;
      f.state = CU.f.state;
      CU.draft = f;
      render();
      countDraft();
    } else if (t.id === "cu-apply") {
      f = listFilters(CU.draft);
      CU.sheet = false;
      CU.draft = null;
      setFilters(f);
    }
    return true;
  }

  document.addEventListener("click", function (ev) {
    if (V2 && v2Click(ev)) return;
    var t = ev.target.closest("[data-seller],[data-period],[data-line],[data-close],[data-send],[data-delete]," +
      "[data-zoom],[data-unzoom],[data-zoomtoggle],[data-tab],[data-pick],#more,#opts,#lastload,#share-pdf,#refresh," +
      "[data-tperiod],[data-tstate],[data-trow],[data-tclose],#tmore,#t-people");
    if (!t) return;
    if (t.dataset.tperiod) {
      if (t.dataset.tperiod === "custom") {
        if (S.trf.period !== "custom") {
          S.trf.period = "custom";
          S.trf.from = S.trf.from || (S.tr && S.tr.date_from) || "";
          S.trf.to = S.trf.to || (S.tr && S.tr.date_to) || "";
          loadTransfers();
        }
      } else if (S.trf.period !== t.dataset.tperiod) { S.trf.period = t.dataset.tperiod; loadTransfers(); }
    } else if (t.dataset.tstate) {
      if (S.trf.state !== t.dataset.tstate) { S.trf.state = t.dataset.tstate; loadTransfers(); }
    } else if (t.dataset.trow) {
      location.hash = "#transferencias/" + t.dataset.trow;
    } else if (t.dataset.tclose) {
      if (ev.target === t) goUp("#transferencias");
    } else if (t.id === "tmore") {
      loadMoreTransfers();
    } else if (t.id === "t-people") {
      ev.preventDefault();
      S.trPeople = !S.trPeople; render();
    } else if (t.dataset.pick) {
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
    if (t.id === "q" && V2) {
      // 19.0.1.129.43: the server searches, 300 ms after the last key
      S.search = t.value;
      clearTimeout(S.searchTimer);
      S.searchTimer = setTimeout(function () {
        var term = searchTerm(S.search);
        if (term === CU.f.q) return;
        var f = listFilters(CU.f);
        f.q = term;
        setFilters(f);
      }, 300);
    } else if (t.id === "cu-f-min" && CU.draft) {
      CU.draft.minBalance = t.value;
      countDraft();
    } else if (t.id === "q") {
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
    if (V2 && CU.draft && (id === "cu-f-seller" || id === "cu-f-zone" || id === "cu-f-age")) {
      CU.draft[{ "cu-f-seller": "seller", "cu-f-zone": "zone", "cu-f-age": "minAge" }[id]] = ev.target.value;
      countDraft();
      return;
    }
    if (id === "cu-f-min") return;
    if (id === "r-from" || id === "r-to") { setRange(); return; }
    if (id === "t-user") { S.trf.user = ev.target.value; loadTransfers(); return; }
    if (id === "t-from" || id === "t-to") {
      S.trf[id === "t-from" ? "from" : "to"] = ev.target.value;
      if (S.trf.from && S.trf.to && S.trf.from > S.trf.to) { toast("La fecha «Desde» es posterior a «Hasta».", true); return; }
      loadTransfers();
      return;
    }
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
    if (V2 && CU.sheet) { closeSheet(); return; }
    if (V2 && CU.menu) {
      CU.menu = false;
      render();
      var more = document.getElementById("cu-more");
      if (more) more.focus();
      return;
    }
    if (S.zoom) { S.zoom = null; render(); } else if (S.line) closeLine();
    else if (S.trOpen && route().transfers) goUp("#transferencias");
  });

  window.addEventListener("hashchange", onRoute);
  // the PC layout reloads the list with its filters; the phone without them
  if (WIDE.addEventListener) {
    WIDE.addEventListener("change", function () {
      if (V2) render();
      else if (S.debtors) loadDebtors(); else render();
    });
  }
  window.addEventListener("online", function () { S.online = true; render(); });
  window.addEventListener("offline", function () { S.online = false; render(); });
  onRoute();
})();
