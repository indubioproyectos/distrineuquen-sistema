/* «Cuentas (celular)»: review customer accounts on the phone.
 *
 * Read-only and online only. Three screens, by the URL hash:
 *   (empty)                  the debtors, highest debt first
 *   #cuenta/<id>             the account of one customer
 *   #cuenta/<id>/mov/<key>   one statement row, as a bottom sheet
 * The rules (who sees which customer) live on the server
 * (models/cuentas_app.py); the only write is a comment.
 */
(function () {
  "use strict";

  var API = "/cuentas/api/";
  var PERIODS = [["1m", "Último mes"], ["3m", "3 meses"], ["all", "Todo"]];
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
    account: null,      // {id, period, data, rows}
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
      if (!S.debtors) loadDebtors(); else render();
      return;
    }
    if (!S.account || S.account.id !== r.id) {
      S.line = null;
      loadAccount(r.id, S.account && S.account.period || "3m").then(function () {
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

  function loadDebtors() {
    var key = "debtors:" + S.seller + ":" + (S.showSettled ? 1 : 0);
    var cached = stored(key);
    if (cached && (!S.debtors || S.debtors.seller !== S.seller)) S.debtors = cached;
    else if (!cached && (!S.debtors || S.debtors.seller !== S.seller)) S.debtors = null;
    S.refreshing = true;
    render();
    return get("debtors?seller=" + encodeURIComponent(S.seller) + "&settled=" + (S.showSettled ? 1 : 0))
      .then(function (data) {
        S.debtors = data;
        store(key, data);
      }).catch(fail).then(function () { S.refreshing = false; render(); });
  }

  // accounts already opened in this visit show at once and refresh behind
  var accountCache = {};

  function loadAccount(id, period) {
    var cacheKey = id + ":" + period;
    var cached = accountCache[cacheKey];
    S.account = { id: id, period: period, data: cached || null, rows: cached ? cached.rows : [] };
    render();
    return get("account?id=" + id + "&period=" + period).then(function (data) {
      accountCache[cacheKey] = data;
      if (!S.account || S.account.id !== id || S.account.period !== period) return;
      S.account.data = data;
      S.account.rows = data.rows;
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
    get("account?id=" + acc.id + "&period=" + acc.period + "&offset=" + acc.rows.length)
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

  function render() {
    var r = route();
    var focus = document.activeElement && document.activeElement.id;
    var caret = focus && document.activeElement.selectionStart;
    var y = window.scrollY;
    var keepScroll = render.lastId === (r.id || 0);
    render.lastId = r.id || 0;
    var csRows = document.getElementById("cs-rows");
    var csKeep = csRows && S.line && csRows.dataset.line === S.line.line_key ? csRows.scrollTop : null;
    app.innerHTML = r.id ? viewAccount() : viewDebtors();
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
    if (row.card_id) tags += ' <span class="pill ok">MP</span>';
    if (row.comment_count) tags += ' <span class="pill info">' + plural(row.comment_count, "comentario", "comentarios") + "</span>";
    var concept = [row.remito ? "Remito " + row.remito : "", row.concept].filter(Boolean).join(" · ");
    return '<button type="button" class="' + cls + '" data-line="' + h(row.line_key) + '">' +
      '<div class="l"><span class="t"><span class="dt">' + day(row.date) + "</span>" + title + tags + "</span>" +
      '<span class="c">' + h(concept || row.source_label) + "</span></div>" +
      '<div class="r">' + amount + '<span class="b num">Saldo ' + money(row.balance) + "</span></div></button>";
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
    head += "<h1>" + h(p.name) + "</h1>" +
      '<div class="row"><span class="total num">' + money(d.balance) + "</span>" +
      '<span class="sub">' + (d.balance > 0.005 ? "debe" : d.balance < -0.005 ? "a favor" : "al día") + "</span></div>" +
      "</div>" + banner() + "</div>";

    var facts = [];
    if (p.ref) facts.push("<span>" + h(p.ref) + "</span>");
    if (p.address) facts.push("<span>" + h(p.address) + "</span>");
    var line2 = [p.zone && "Zona " + h(p.zone), p.seller && "Vendedor: " + h(p.seller)].filter(Boolean);
    if (line2.length) facts.push("<span>" + line2.join(" · ") + "</span>");
    var contact = "";
    if (p.phone) {
      contact = '<div class="contact">' +
        (p.whatsapp ? '<a href="https://wa.me/' + h(p.whatsapp) + '" target="_blank" rel="noopener">WhatsApp</a>' : "") +
        '<a href="tel:' + h(p.phone.replace(/[^\d+]/g, "")) + '">Llamar ' + h(p.phone) + "</a></div>";
    }
    var info = '<div class="card strong"><div class="facts">' + facts.join("") + "</div>" + contact + "</div>";

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
        '<div class="box"><span>Saldo si se aprueban</span><b class="num">' + money(d.whatif_balance) + "</b></div>");
    }
    if (d.owed_count) {
      boxes.push('<div class="box wide"><span>Facturas adeudadas (revisadas)</span><b>' + d.owed_count + "</b></div>");
    }
    var summary = boxes.length ? '<div class="summary">' + boxes.join("") + "</div>" : "";

    var candidates = "";
    if (d.candidates.length) {
      candidates = '<div class="label">Pagos a confirmar · falta el comprobante</div><div class="stmt">' +
        d.candidates.map(function (c) {
          return '<div class="srow opening"><div class="l"><span class="t"><span class="dt">' + day(c.date) + "</span>" +
            h(c.name) + "</span><span class=\"c\">" + h(c.note || (c.has_receipt ? "Llegó el comprobante" : "Sin comprobante")) +
            '</span></div><div class="r"><span class="m num">(' + money(c.amount) + ')</span><span class="b">no resta</span></div></div>';
        }).join("") + "</div>";
    }

    var comments = '<div class="label">Comentarios de la cuenta</div><div class="comments">' +
      d.comments.map(commentView).join("") + composeView("c-account", S.draft) + "</div>";
    if (d.line_comments.length) {
      comments += '<details class="block"><summary class="link">' +
        plural(d.line_comments.length, "comentario en movimientos", "comentarios en movimientos") +
        '</summary><div class="comments">' + d.line_comments.map(commentView).join("") + "</div></details>";
    }

    var periods = '<div class="seg">' + PERIODS.map(function (x) {
      return '<button type="button" data-period="' + x[0] + '" class="' + (acc.period === x[0] ? "on" : "") + '">' +
        x[1] + "</button>";
    }).join("") + "</div>";

    var rows = acc.rows.length ? acc.rows.map(stmtRow).join("") : '<div class="empty">Sin movimientos en el período.</div>';
    var tail = "";
    if (d.more) {
      tail = '<button type="button" class="btn outline more" id="more"' + (S.loadingMore ? " disabled" : "") + ">" +
        (S.loadingMore ? "Cargando…" : "Ver más movimientos (" + (d.row_count - acc.rows.length) + ")") + "</button>";
    }
    var opening = d.more ? "" :
      '<div class="srow opening"><div class="l"><span class="t">' +
      (d.date_from ? "Saldo anterior al " + day(d.date_from) : "Saldo inicial") + "</span>" +
      '<span class="c">' + (d.date_from ? "Todo lo anterior al período" : "Antes del primer movimiento") +
      '</span></div><div class="r"><span class="m num">' + money(d.opening) + "</span></div></div>";

    return head + '<div class="body">' + info + summary + comments + candidates +
      '<div class="label">Movimientos · lo más nuevo arriba</div>' + periods +
      '<div class="stmt">' + rows + (tail ? "" : opening) + "</div>" + tail + "</div>";
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
      if (L.owed) {
        parts.push('<div class="block"><div class="label">Adeudada (revisada)</div>' + kv([
          ["Debe todavía", '<span class="num">' + money(L.owed) + "</span>"],
          ["Nota", h(L.owed_note)],
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

  function closeLine() {
    var r = route();
    S.line = null;
    location.hash = "#cuenta/" + r.id;
  }

  document.addEventListener("click", function (ev) {
    var t = ev.target.closest("[data-seller],[data-period],[data-line],[data-close],[data-send],[data-delete]," +
      "[data-zoom],[data-unzoom],[data-zoomtoggle],#more,#opts,#lastload");
    if (!t) return;
    if (t.dataset.seller) {
      if (S.seller !== t.dataset.seller) { S.seller = t.dataset.seller; loadDebtors(); }
    } else if (t.dataset.period) {
      if (S.account.period !== t.dataset.period) loadAccount(S.account.id, t.dataset.period);
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
  window.addEventListener("online", function () { S.online = true; render(); });
  window.addEventListener("offline", function () { S.online = false; render(); });
  onRoute();
})();
